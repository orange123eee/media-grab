"""X (Twitter) extractor: highest-bitrate MP4 videos, GIFs, and original (:orig) images."""

import json
import math
import queue
import re
import socket
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from ..config import Config
from ..http_client import HttpClient, warm_urls
from ..utils import dedupe_keep_order, normalize_http_url
from . import ExtractResult, ExtractionError, MediaAsset

_STATUS_RE = re.compile(r"/(?:([A-Za-z0-9_]{1,30})/)?status(?:es)?/(\d+)")
_RES_HINT_RE = re.compile(r"(?:/|_)(\d{2,5})x(\d{2,5})(?:/|_|\.)")
_META_OR_JS_REDIRECT_RE = re.compile(
    r'(?:url\s*=|location\.replace\s*\(\s*["\']|location\.href\s*=\s*["\'])(https?://[^\s"\'<>)]+)',
    re.IGNORECASE,
)

_RESULT_CACHE_TTL = 60.0
_RESULT_CACHE_MAX = 32
_RESULT_CACHE_LOCK = threading.Lock()
_RESULT_CACHE: Dict[str, Tuple[float, ExtractResult]] = {}


def _extract_status_info(url: str) -> Tuple[str, str]:
    """Return (screen_name, status_id) extracted from an X/Twitter status URL."""
    m = _STATUS_RE.search(urlparse(url).path or "")
    if not m:
        return "", ""
    user = (m.group(1) or "").strip()
    if user.lower() in ("i", "web"):
        user = ""
    return user, m.group(2)


def _syndication_token(status_id: str) -> str:
    """Compute token expected by cdn.syndication.twimg.com/tweet-result."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    val = (int(status_id) / 1e15) * math.pi
    ip = int(val)
    fp = val - ip
    int_chars: List[str] = []
    if ip == 0:
        int_chars.append("0")
    while ip > 0:
        int_chars.append(digits[ip % 36])
        ip //= 36
    out = "".join(reversed(int_chars))
    frac_chars: List[str] = []
    for _ in range(12):
        fp *= 36
        d = int(fp)
        frac_chars.append(digits[d])
        fp -= d
        if fp <= 1e-12:
            break
    token = (out + "".join(frac_chars)).replace("0", "")
    return token or "1"


def _build_x_image_variants(raw_url: str) -> Tuple[List[str], List[str], str]:
    """Build original (:orig) full-res URLs, fast preview URLs, and default extension."""
    norm = normalize_http_url(raw_url)
    if not norm:
        return [], [], ".jpg"

    parsed = urlparse(norm)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    path = parsed.path

    # Handle legacy suffix like /media/AbC.jpg:large
    clean_path = re.sub(r":(?:orig|large|medium|small|thumb)$", "", path, flags=re.IGNORECASE)
    ext_match = re.search(r"\.(jpg|jpeg|png|webp)$", clean_path, re.IGNORECASE)
    fmt = (qs.get("format", [""])[0] or (ext_match.group(1) if ext_match else "jpg")).lower()
    if fmt == "jpeg":
        fmt = "jpg"
    default_ext = f".{fmt}" if fmt in ("jpg", "png", "webp") else ".jpg"

    is_pbs_media = "pbs.twimg.com" in (parsed.hostname or "").lower() and "/media/" in clean_path
    if not is_pbs_media:
        return [norm], [norm], default_ext

    stem_path = re.sub(r"\.(jpg|jpeg|png|webp)$", "", clean_path, flags=re.IGNORECASE)
    orig_q = urlencode({"format": fmt, "name": "orig"})
    large_q = urlencode({"format": fmt, "name": "large"})
    med_q = urlencode({"format": fmt, "name": "medium"})
    small_q = urlencode({"format": fmt, "name": "small"})

    orig_url = urlunparse((parsed.scheme or "https", parsed.netloc, stem_path, "", orig_q, ""))
    large_url = urlunparse((parsed.scheme or "https", parsed.netloc, stem_path, "", large_q, ""))
    colon_orig = urlunparse((parsed.scheme or "https", parsed.netloc, f"{clean_path}:orig", "", "", ""))
    med_url = urlunparse((parsed.scheme or "https", parsed.netloc, stem_path, "", med_q, ""))
    small_url = urlunparse((parsed.scheme or "https", parsed.netloc, stem_path, "", small_q, ""))

    full_candidates: List[str] = []
    if fmt == "webp":
        jpg_orig_q = urlencode({"format": "jpg", "name": "orig"})
        full_candidates.append(urlunparse((parsed.scheme or "https", parsed.netloc, stem_path, "", jpg_orig_q, "")))
    full_candidates.extend([orig_url, colon_orig, large_url, norm])
    full_urls = dedupe_keep_order(full_candidates)
    thumb_urls = dedupe_keep_order([small_url, med_url, norm, orig_url])
    return full_urls, thumb_urls, ".jpg" if fmt == "webp" else default_ext


def _video_resolution_score(url: str) -> int:
    m = _RES_HINT_RE.search(url)
    if not m:
        return 0
    return int(m.group(1)) * int(m.group(2))


def _collect_video_variants(variants: Any, direct_url: str = "") -> List[str]:
    """Order direct MP4 stream variants from highest pixel resolution and bitrate down."""
    scored: List[Tuple[Tuple[int, int], str]] = []
    if isinstance(variants, list):
        for v in variants:
            if not isinstance(v, dict):
                continue
            u = normalize_http_url(v.get("url") or "")
            if not u:
                continue
            ct = str(v.get("content_type") or v.get("type") or "").lower()
            is_mp4 = "mp4" in ct or ".mp4" in urlparse(u).path.lower()
            if not is_mp4:
                continue
            br = int(v.get("bitrate") or 0)
            px = max(
                _video_resolution_score(u),
                int(v.get("width") or 0) * int(v.get("height") or 0),
            )
            scored.append(((px, br), u))

    scored.sort(key=lambda item: item[0], reverse=True)
    urls = [u for _, u in scored]
    if direct_url:
        d_norm = normalize_http_url(direct_url)
        if d_norm and ".mp4" in urlparse(d_norm).path.lower():
            urls.append(d_norm)
    return dedupe_keep_order(urls)


def _first_nonempty_text(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def parse_fxtwitter_tweet(payload: Dict[str, Any], preferred_id: str = "") -> Optional[ExtractResult]:
    """Parse api.fxtwitter.com / api.fixupx.com JSON payload into ExtractResult."""
    tweet = payload.get("tweet")
    if not isinstance(tweet, dict):
        return None

    item_id = _first_nonempty_text(tweet.get("id"), preferred_id, "x")
    title = _first_nonempty_text((tweet.get("raw_text") or {}).get("text"), tweet.get("text"))
    author_obj = tweet.get("author") or {}
    author = _first_nonempty_text(
        author_obj.get("name"),
        f"@{author_obj['screen_name']}" if author_obj.get("screen_name") else "",
    )

    media_box = tweet.get("media")
    if not isinstance(media_box, dict) and isinstance(tweet.get("quote"), dict):
        quote_media = tweet["quote"].get("media")
        if isinstance(quote_media, dict):
            media_box = quote_media
    if not isinstance(media_box, dict):
        return None

    entries: List[Dict[str, Any]] = []
    if isinstance(media_box.get("all"), list) and media_box["all"]:
        entries = [e for e in media_box["all"] if isinstance(e, dict)]
    else:
        for p in media_box.get("photos") or []:
            if isinstance(p, dict):
                entries.append(dict(p, type=p.get("type") or "photo"))
        for v in media_box.get("videos") or []:
            if isinstance(v, dict):
                entries.append(dict(v, type=v.get("type") or "video"))

    assets: List[MediaAsset] = []
    for item in entries:
        kind = str(item.get("type") or "").lower()
        if kind == "photo":
            full_urls, thumb_urls, ext = _build_x_image_variants(item.get("url") or "")
            if full_urls:
                assets.append(
                    MediaAsset(
                        kind="image",
                        urls=full_urls,
                        default_ext=ext,
                        headers={},
                        mobile=False,
                        thumb_urls=thumb_urls,
                    )
                )
        elif kind in ("video", "gif", "animated_gif"):
            v_urls = _collect_video_variants(item.get("variants"), direct_url=str(item.get("url") or ""))
            if not v_urls:
                continue
            thumb_raw = str(item.get("thumbnail_url") or "")
            _, thumb_urls, _ = _build_x_image_variants(thumb_raw) if thumb_raw else ([], [], ".jpg")
            assets.append(
                MediaAsset(
                    kind="video",
                    urls=v_urls,
                    default_ext=".mp4",
                    headers={},
                    mobile=False,
                    thumb_urls=thumb_urls,
                )
            )

    if not assets:
        return None

    return ExtractResult(
        platform="x",
        item_id=item_id,
        title=title or f"x_{item_id}",
        author=author,
        assets=assets,
    )


def parse_syndication_tweet(payload: Dict[str, Any], preferred_id: str = "") -> Optional[ExtractResult]:
    """Parse cdn.syndication.twimg.com/tweet-result JSON payload into ExtractResult."""
    if not isinstance(payload, dict) or payload.get("__typename") == "TweetTombstone":
        return None

    tweet = payload
    media_details = tweet.get("mediaDetails")
    if not isinstance(media_details, list) or not media_details:
        quoted = tweet.get("quoted_tweet")
        if isinstance(quoted, dict) and isinstance(quoted.get("mediaDetails"), list) and quoted["mediaDetails"]:
            media_details = quoted["mediaDetails"]

    item_id = _first_nonempty_text(tweet.get("id_str"), tweet.get("id"), preferred_id, "x")
    title = _first_nonempty_text(tweet.get("text"), tweet.get("full_text"))
    user = tweet.get("user") or {}
    author = _first_nonempty_text(
        user.get("name"),
        f"@{user['screen_name']}" if user.get("screen_name") else "",
    )

    assets: List[MediaAsset] = []
    if isinstance(media_details, list) and media_details:
        for item in media_details:
            if not isinstance(item, dict):
                continue
            mtype = str(item.get("type") or "").lower()
            if mtype == "photo":
                raw = item.get("media_url_https") or item.get("media_url") or ""
                full_urls, thumb_urls, ext = _build_x_image_variants(str(raw))
                if full_urls:
                    assets.append(
                        MediaAsset(
                            kind="image",
                            urls=full_urls,
                            default_ext=ext,
                            headers={},
                            mobile=False,
                            thumb_urls=thumb_urls,
                        )
                    )
            elif mtype in ("video", "animated_gif", "gif"):
                v_info = item.get("video_info") or {}
                v_urls = _collect_video_variants(v_info.get("variants"))
                if not v_urls:
                    continue
                thumb_raw = str(item.get("media_url_https") or item.get("media_url") or "")
                _, thumb_urls, _ = _build_x_image_variants(thumb_raw) if thumb_raw else ([], [], ".jpg")
                assets.append(
                    MediaAsset(
                        kind="video",
                        urls=v_urls,
                        default_ext=".mp4",
                        headers={},
                        mobile=False,
                        thumb_urls=thumb_urls,
                    )
                )

    if not assets and isinstance(tweet.get("photos"), list):
        for p in tweet["photos"]:
            if not isinstance(p, dict) or not p.get("url"):
                continue
            full_urls, thumb_urls, ext = _build_x_image_variants(str(p["url"]))
            if full_urls:
                assets.append(
                    MediaAsset(
                        kind="image",
                        urls=full_urls,
                        default_ext=ext,
                        headers={},
                        mobile=False,
                        thumb_urls=thumb_urls,
                    )
                )

    if not assets:
        return None

    return ExtractResult(
        platform="x",
        item_id=item_id,
        title=title or f"x_{item_id}",
        author=author,
        assets=assets,
    )


def _asset_from_x_photo(raw_url: str) -> Optional[MediaAsset]:
    full_urls, thumb_urls, ext = _build_x_image_variants(raw_url)
    if not full_urls:
        return None
    return MediaAsset(
        kind="image",
        urls=full_urls,
        default_ext=ext,
        headers={},
        mobile=False,
        thumb_urls=thumb_urls,
    )


def _asset_from_x_video(video_urls: List[str], thumb_raw: str) -> Optional[MediaAsset]:
    if not video_urls:
        return None
    _, thumb_urls, _ = _build_x_image_variants(thumb_raw) if thumb_raw else ([], [], ".jpg")
    return MediaAsset(
        kind="video",
        urls=video_urls,
        default_ext=".mp4",
        headers={},
        mobile=False,
        thumb_urls=thumb_urls,
    )


def parse_vxtwitter_tweet(payload: Dict[str, Any], preferred_id: str = "") -> Optional[ExtractResult]:
    """Parse api.vxtwitter.com compact JSON payload into ExtractResult."""
    if not isinstance(payload, dict) or payload.get("error"):
        return None

    tweet = payload
    media_ext = tweet.get("media_extended")
    media_urls = tweet.get("mediaURLs")
    if (not isinstance(media_ext, list) or not media_ext) and (not isinstance(media_urls, list) or not media_urls):
        for nested_key in ("qrt", "retweet"):
            nested = tweet.get(nested_key)
            if isinstance(nested, dict) and (nested.get("media_extended") or nested.get("mediaURLs")):
                media_ext = nested.get("media_extended")
                media_urls = nested.get("mediaURLs")
                break

    item_id = _first_nonempty_text(tweet.get("tweetID"), tweet.get("conversationID"), preferred_id, "x")
    title = _first_nonempty_text(tweet.get("text"))
    author = _first_nonempty_text(
        tweet.get("user_name"),
        f"@{tweet['user_screen_name']}" if tweet.get("user_screen_name") else "",
    )

    assets: List[MediaAsset] = []
    if isinstance(media_ext, list):
        for item in media_ext:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").lower()
            raw_u = str(item.get("url") or "")
            if kind in ("image", "photo"):
                asset = _asset_from_x_photo(raw_u)
                if asset:
                    assets.append(asset)
            elif kind in ("video", "gif", "animated_gif"):
                v_urls = _collect_video_variants(item.get("variants"), direct_url=raw_u)
                if not v_urls and raw_u:
                    v_norm = normalize_http_url(raw_u)
                    if v_norm:
                        v_urls = [v_norm]
                if v_urls:
                    asset = _asset_from_x_video(v_urls, str(item.get("thumbnail_url") or ""))
                    if asset:
                        assets.append(asset)

    if not assets and isinstance(media_urls, list):
        for raw_u in media_urls:
            norm = normalize_http_url(str(raw_u or ""))
            if not norm:
                continue
            if ".mp4" in urlparse(norm).path.lower():
                asset = _asset_from_x_video([norm], "")
            else:
                asset = _asset_from_x_photo(norm)
            if asset:
                assets.append(asset)

    if not assets:
        return None

    return ExtractResult(
        platform="x",
        item_id=item_id,
        title=title or f"x_{item_id}",
        author=author,
        assets=assets,
    )


def _resolve_status_url(url: str, client: HttpClient, timeout: int) -> Tuple[str, str]:
    screen_name, status_id = _extract_status_info(url)
    if status_id:
        return screen_name, status_id

    hop_timeout = min(max(1, int(timeout)), 5)
    curr = url
    for _ in range(4):
        resp = client.get(curr, mobile=False, follow_redirects=False, timeout=hop_timeout)
        target = resp.headers.get("location") or resp.url or ""
        if target:
            full_loc = urljoin(curr, target)
            sn, sid = _extract_status_info(full_loc)
            if sid:
                return sn, sid
            if resp.status in (301, 302, 303, 307, 308) and full_loc != curr:
                curr = full_loc
                continue

        html = (resp.text() if resp.body else "").replace("\\/", "/")
        m = _STATUS_RE.search(html)
        if m:
            user = (m.group(1) or "").strip()
            if user.lower() in ("i", "web"):
                user = ""
            return user, m.group(2)

        rm = _META_OR_JS_REDIRECT_RE.search(html)
        if rm:
            nxt = urljoin(curr, rm.group(1).strip())
            sn, sid = _extract_status_info(nxt)
            if sid:
                return sn, sid
            if nxt != curr:
                curr = nxt
                continue
        break
    return "", ""


def prewarm(config: Optional[Config] = None) -> None:
    """Warm DNS and Keep-Alive TLS connections for X API and media CDNs."""
    warm_urls(
        [
            "https://cdn.syndication.twimg.com/",
            "https://api.fxtwitter.com/",
            "https://api.vxtwitter.com/",
            "https://pbs.twimg.com/",
            "https://video.twimg.com/",
        ],
        timeout=3.0,
    )


def _video_quality_rank(res: ExtractResult) -> Tuple[int, int]:
    """Rank ExtractResult for video richness (pixel resolution hint, variant candidate count)."""
    max_px = 0
    max_vars = 0
    for a in res.assets:
        if a.kind == "video":
            max_vars = max(max_vars, len(a.urls))
            for u in a.urls:
                max_px = max(max_px, _video_resolution_score(u))
    return (max_px, max_vars)


def _run_parallel_first_hit(tasks: List[Any]) -> Tuple[Optional[ExtractResult], List[str]]:
    """Run callables on daemon threads; return first hit immediately, or wait briefly if single-URL video arrives before multi-variant endpoints."""
    q: queue.Queue = queue.Queue()
    for fn in tasks:
        def _worker(func=fn) -> None:
            try:
                q.put((True, func()))
            except Exception as exc:
                q.put((False, str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    errors: List[str] = []
    best: Optional[ExtractResult] = None
    pending = len(tasks)
    while pending > 0:
        ok, val = q.get()
        pending -= 1
        if ok and val and val.assets:
            if best is None or _video_quality_rank(val) > _video_quality_rank(best):
                best = val
            needs_upgrade = any(a.kind == "video" and len(a.urls) <= 1 for a in best.assets)
            if not needs_upgrade or pending == 0:
                return best, errors
            deadline = time.monotonic() + 0.25
            while pending > 0:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                try:
                    ok2, val2 = q.get(timeout=rem)
                except queue.Empty:
                    break
                pending -= 1
                if ok2 and val2 and val2.assets:
                    if _video_quality_rank(val2) > _video_quality_rank(best):
                        best = val2
                        if not any(a.kind == "video" and len(a.urls) <= 1 for a in best.assets):
                            break
                elif not ok2:
                    errors.append(str(val2))
            return best, errors
        if not ok:
            errors.append(str(val))
    return best, errors


def _get_cached_result(status_id: str) -> Optional[ExtractResult]:
    now = time.monotonic()
    with _RESULT_CACHE_LOCK:
        entry = _RESULT_CACHE.get(status_id)
        if not entry:
            return None
        ts, res = entry
        if now - ts > _RESULT_CACHE_TTL:
            _RESULT_CACHE.pop(status_id, None)
            return None
        return res


def _put_cached_result(status_id: str, res: ExtractResult) -> None:
    now = time.monotonic()
    with _RESULT_CACHE_LOCK:
        _RESULT_CACHE[status_id] = (now, res)
        if len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
            oldest = min(_RESULT_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _RESULT_CACHE.pop(oldest, None)


def extract_x(url: str, client: HttpClient, config: Config) -> ExtractResult:
    """Fetch X (Twitter) tweet metadata concurrently from public endpoints and extract media."""
    api_timeout = min(int(config.timeout), 6)
    screen_name, status_id = _resolve_status_url(url, client, api_timeout)
    if not status_id:
        raise ExtractionError("未在链接里识别到 X 推文 ID（需要包含 /status/数字 的帖子链接）。")

    use_cache = type(client) is HttpClient
    if use_cache:
        cached = _get_cached_result(status_id)
        if cached is not None:
            return cached

    synd_url = (
        f"https://cdn.syndication.twimg.com/tweet-result"
        f"?id={status_id}&lang=en&token={_syndication_token(status_id)}"
    )
    user_part = screen_name or "i"
    fx_url = f"https://api.fxtwitter.com/{user_part}/status/{status_id}"
    vx_url = f"https://api.vxtwitter.com/{user_part}/status/{status_id}"
    fixup_url = f"https://api.fixupx.com/{user_part}/status/{status_id}"

    saw_tweet = False

    def _call_synd() -> Optional[ExtractResult]:
        nonlocal saw_tweet
        resp = client.get(
            synd_url,
            headers={
                "Accept": "application/json",
                "Referer": "https://platform.twitter.com/",
                "Origin": "https://platform.twitter.com",
            },
            mobile=False,
            follow_redirects=True,
            timeout=api_timeout,
        )
        data = json.loads(resp.text())
        if isinstance(data, dict) and data.get("__typename") not in (None, "TweetTombstone"):
            saw_tweet = True
        return parse_syndication_tweet(data, status_id)

    def _call_fx(target_url: str) -> Optional[ExtractResult]:
        nonlocal saw_tweet
        resp = client.get(
            target_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "media-grab/1.0 (+https://github.com/orange123eee/media-grab)",
            },
            mobile=False,
            follow_redirects=True,
            timeout=api_timeout,
        )
        data = json.loads(resp.text())
        if isinstance(data, dict) and isinstance(data.get("tweet"), dict):
            saw_tweet = True
        return parse_fxtwitter_tweet(data, status_id)

    def _call_vx() -> Optional[ExtractResult]:
        nonlocal saw_tweet
        resp = client.get(
            vx_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "media-grab/1.0 (+https://github.com/orange123eee/media-grab)",
            },
            mobile=False,
            follow_redirects=True,
            timeout=api_timeout,
        )
        data = json.loads(resp.text())
        if isinstance(data, dict) and data.get("tweetID") and not data.get("error"):
            saw_tweet = True
        return parse_vxtwitter_tweet(data, status_id)

    res, errors = _run_parallel_first_hit([_call_synd, _call_vx, lambda: _call_fx(fx_url)])
    if res and res.assets:
        if use_cache:
            _put_cached_result(status_id, res)
        return res

    if not saw_tweet:
        try:
            res_fb = _call_fx(fixup_url)
            if res_fb and res_fb.assets:
                if use_cache:
                    _put_cached_result(status_id, res_fb)
                return res_fb
        except Exception as e:
            errors.append(str(e))

    if saw_tweet or not errors:
        raise ExtractionError("该 X 推文中未检测到可提取的视频、GIF 或图片。")
    raise ExtractionError(
        f"未能获取该 X 推文的视频或图片（可能为仅关注者可见、受限或已删除内容：{errors[0]}）。"
    )

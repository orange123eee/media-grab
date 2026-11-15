"""Xiaohongshu extractor: watermark-free high-res images, live photos, and highest-bitrate videos."""

import hashlib
import html as html_lib
import json
import queue
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

from ..config import Config
from ..http_client import HttpClient, HttpError, warm_urls
from ..utils import dedupe_keep_order, extract_balanced_json, loads_relaxed_json, normalize_http_url
from . import ExtractResult, ExtractionError, MediaAsset

XHS_DL_HEADERS = {"Referer": "https://www.xiaohongshu.com/"}


def _extract_note_id(url: str) -> str:
    m = re.search(r"/(?:explore|discovery/item)/([0-9a-fA-F]+)", url)
    return m.group(1) if m else ""


def _parse_xhs_state(html: str) -> Optional[Dict[str, Any]]:
    raw = extract_balanced_json(html, r"(?:window\.)?__INITIAL_STATE__\s*=")
    if not raw:
        return None
    return loads_relaxed_json(raw)


def _parse_xhs_setup_state(html: str) -> Optional[Dict[str, Any]]:
    raw = extract_balanced_json(html, r"(?:window\.)?__SETUP_SERVER_STATE__\s*=")
    if not raw:
        return None
    return loads_relaxed_json(raw)


def _looks_like_note_dict(obj: Any) -> bool:
    return isinstance(obj, dict) and bool(obj) and any(obj.get(k) for k in ("imageList", "video", "noteId"))


def _find_note_obj(state: Dict[str, Any], preferred_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(state, dict):
        return None

    note_section = state.get("note")
    if isinstance(note_section, dict):
        nd_map = note_section.get("noteDetailMap")
        if isinstance(nd_map, dict):
            if preferred_id and isinstance(nd_map.get(preferred_id), dict):
                entry = nd_map[preferred_id]
                n = entry.get("note")
                if isinstance(n, dict) and n:
                    return n
                if _looks_like_note_dict(entry):
                    return entry
            for val in nd_map.values():
                if isinstance(val, dict):
                    n = val.get("note")
                    if isinstance(n, dict) and n:
                        return n
                    if _looks_like_note_dict(val):
                        return val
        for key in ("noteDetail", "currentNote"):
            candidate = note_section.get(key)
            if isinstance(candidate, dict) and candidate:
                return candidate

    note_data = state.get("noteData")
    if isinstance(note_data, dict):
        data = note_data.get("data")
        if isinstance(data, dict):
            nested = data.get("noteData")
            if isinstance(nested, dict) and nested:
                return nested
            if _looks_like_note_dict(data):
                return data
        direct = note_data.get("noteData")
        if isinstance(direct, dict) and direct:
            return direct

    ssr_page = state.get("LAUNCHER_SSR_STORE_PAGE_DATA")
    if isinstance(ssr_page, dict):
        ssr_note = ssr_page.get("noteData")
        if isinstance(ssr_note, dict) and ssr_note:
            return ssr_note
    return None


_XHS_RES_HINT_RE = re.compile(r"(4k|2k|2160|1440|1080|720|540|480|uhd|fhd)", re.IGNORECASE)


def _infer_stream_pixels(st: Dict[str, Any], urls: List[str]) -> int:
    pixels = int(st.get("width") or 0) * int(st.get("height") or 0)
    if pixels > 0:
        return pixels
    hint_text = " ".join(
        [
            str(st.get("streamDesc") or ""),
            str(st.get("qualityType") or ""),
            urls[0] if urls else "",
        ]
    )
    m = _XHS_RES_HINT_RE.search(hint_text)
    if not m:
        return 0
    tok = m.group(1).lower()
    mapping = {
        "4k": 3840 * 2160,
        "uhd": 3840 * 2160,
        "2160": 3840 * 2160,
        "2k": 2560 * 1440,
        "1440": 2560 * 1440,
        "1080": 1920 * 1080,
        "fhd": 1920 * 1080,
        "720": 1280 * 720,
        "540": 960 * 540,
        "480": 854 * 480,
    }
    return mapping.get(tok, 0)


def _collect_stream_urls(stream_dict: Dict[str, Any]) -> List[str]:
    """Rank H.264/H.265/AV1 streams by resolution, bitrate, and size descending."""
    ranked: List[tuple] = []
    codec_pref = {"h264": 2, "h265": 2, "av1": 1}

    for codec, entries in stream_dict.items():
        if not isinstance(entries, list):
            continue
        pref = codec_pref.get(str(codec).lower(), 0)
        for st in entries:
            if not isinstance(st, dict):
                continue
            urls = []
            if st.get("masterUrl"):
                urls.append(normalize_http_url(st["masterUrl"]))
            for bu in st.get("backupUrls") or []:
                urls.append(normalize_http_url(bu))
            urls = [u for u in urls if u]
            if not urls:
                continue
            pixels = _infer_stream_pixels(st, urls)
            bitrate = int(st.get("videoBitrate") or st.get("avgBitrate") or 0)
            size = int(st.get("size") or 0)
            ranked.append(((pixels, bitrate, size, pref), urls))

    ranked.sort(key=lambda item: item[0], reverse=True)
    merged: List[str] = []
    for _, urls in ranked:
        merged.extend(urls)
    return dedupe_keep_order(merged)


_SIGNED_PREFIX_RE = re.compile(r"^(?:\d{10,14}/[0-9a-fA-F]{16,64}/|\d{4}/)")


def _extract_image_token(url: str) -> str:
    """Extract clean path/trace token before '!' or query params when not under /spectrum/."""
    path = urlparse(normalize_http_url(url)).path.lstrip("/")
    if not path or "spectrum/" in path:
        return ""
    path = _SIGNED_PREFIX_RE.sub("", path)
    return path.split("!", 1)[0]


def _collect_raw_image_urls(img: Dict[str, Any], prefer_preview: bool = False) -> List[str]:
    """Collect raw infoList/fallback CDN URLs ordered by high-res or preview preference."""
    raw_urls: List[str] = []
    info_list = img.get("infoList")
    scenes = ("WB_PRV", "WB_DFT") if prefer_preview else ("WB_DFT", "WB_PRV")

    def _entry_score(entry: Dict[str, Any]) -> tuple:
        px = int(entry.get("width") or 0) * int(entry.get("height") or 0)
        sz = int(entry.get("fileSize") or entry.get("size") or 0)
        return (px, sz)

    if isinstance(info_list, list):
        valid_entries = [e for e in info_list if isinstance(e, dict) and e.get("url")]
        if not prefer_preview:
            for scene in scenes:
                matched = [e for e in valid_entries if e.get("imageScene") == scene]
                matched.sort(key=_entry_score, reverse=True)
                for entry in matched:
                    raw_urls.append(normalize_http_url(entry["url"]))
            rest = [e for e in valid_entries if e.get("imageScene") not in scenes]
            rest.sort(key=_entry_score, reverse=True)
            for entry in rest:
                raw_urls.append(normalize_http_url(entry["url"]))
        else:
            for scene in scenes:
                for entry in valid_entries:
                    if entry.get("imageScene") == scene:
                        raw_urls.append(normalize_http_url(entry["url"]))
            for entry in valid_entries:
                raw_urls.append(normalize_http_url(entry["url"]))

    keys = (
        ("urlPre", "urlDefault", "url")
        if prefer_preview
        else ("originUrl", "urlOrigin", "originalUrl", "urlDefault", "urlPre", "url")
    )
    for key in keys:
        if img.get(key):
            raw_urls.append(normalize_http_url(img[key]))
    return dedupe_keep_order(raw_urls)


def _collect_image_tokens(img: Dict[str, Any], raw_urls: List[str]) -> List[str]:
    tokens: List[str] = []
    primary = str(img.get("fileId") or img.get("traceId") or "").strip()
    if primary:
        tokens.append(primary)
    for u in raw_urls:
        token = _extract_image_token(u)
        if token:
            tokens.append(token)
    return dedupe_keep_order(tokens)


def _build_image_candidates(img: Dict[str, Any]) -> List[str]:
    """Generate candidate URLs prioritizing fast watermark-free PNG before slower origin/default links."""
    raw_urls = _collect_raw_image_urls(img, prefer_preview=False)
    tokens = _collect_image_tokens(img, raw_urls)

    candidates: List[str] = []
    primary = str(img.get("fileId") or img.get("traceId") or "").strip()
    for token in tokens:
        candidates.append(f"https://sns-img-hw.xhscdn.com/{token}?imageView2/2/w/format/png")

    for u in raw_urls:
        # Try removing watermark suffix (!nd_dft_wlteh_...) when path has no signed timestamp prefix
        if "!" in u:
            u_path = urlparse(u).path.lstrip("/")
            if not _SIGNED_PREFIX_RE.match(u_path):
                base = u.split("!", 1)[0].split("?", 1)[0]
                if "/spectrum/" not in base:
                    candidates.append(f"{base}?imageView2/2/w/format/png")

    for token in tokens:
        candidates.append(f"https://ci.xiaohongshu.com/{token}?imageView2/2/w/format/png")
        if token == primary:
            candidates.append(f"https://ci.xiaohongshu.com/{token}")

    candidates.extend(raw_urls)
    return dedupe_keep_order(candidates)


def _build_thumb_candidates(img: Dict[str, Any]) -> List[str]:
    """Generate fast 720w WebP preview URLs, preferring edge CDNs before origin ci.xiaohongshu.com."""
    raw_urls = _collect_raw_image_urls(img, prefer_preview=True)
    tokens = _collect_image_tokens(img, raw_urls)

    thumbs: List[str] = []
    for token in tokens:
        thumbs.append(f"https://sns-img-hw.xhscdn.com/{token}?imageView2/2/w/720/format/webp")
    thumbs.extend(raw_urls)
    for token in tokens:
        thumbs.append(f"https://ci.xiaohongshu.com/{token}?imageView2/2/w/720/format/webp")
    return dedupe_keep_order(thumbs)


_LD_JSON_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_META_TAG_RE = re.compile(r"<meta\b[^>]+>", re.IGNORECASE)
_META_ATTR_RE = re.compile(r"([a-zA-Z_:.-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_NOTE_PATH_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]+)")


def _clean_xhs_media_url(raw_url: str) -> str:
    u = html_lib.unescape(str(raw_url or "")).strip()
    if u.startswith("//"):
        u = "https:" + u
    m = re.match(r"^https?://(?:ci\.xiaohongshu\.com|[^/]+\.xhscdn\.com)/(https?://.+)$", u)
    if m:
        u = m.group(1)
    return normalize_http_url(u)


def _is_note_cdn_url(u: str) -> bool:
    if not u:
        return False
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not (host.endswith("xhscdn.com") or host == "ci.xiaohongshu.com"):
        return False
    if "picasso-static" in host:
        return False
    return not any(skip in path for skip in ("/fe-platform/", "/fe-static/", "/avatar/"))


def _extract_meta_tags(html: str) -> List[Tuple[str, str]]:
    tags: List[Tuple[str, str]] = []
    for m in _META_TAG_RE.finditer(html):
        attrs = {k.lower(): html_lib.unescape(v) for k, _, v in _META_ATTR_RE.findall(m.group(0))}
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        val = (attrs.get("content") or "").strip()
        if key and val:
            tags.append((key, val))
    return tags


def _parse_ld_and_meta_note(html: str, preferred_id: str) -> Optional[Dict[str, Any]]:
    """Fallback parser extracting media URLs from JSON-LD and OpenGraph meta tags."""
    title = ""
    video_urls: List[str] = []
    img_urls: List[str] = []
    thumb_urls: List[str] = []

    for match in _LD_JSON_RE.finditer(html):
        raw = html_lib.unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            if not title:
                title = str(obj.get("headline") or obj.get("name") or obj.get("description") or "").strip()
            c_url = _clean_xhs_media_url(obj.get("contentUrl") or "")
            if c_url and urlparse(c_url).hostname:
                video_urls.append(c_url)
            t_val = obj.get("thumbnailUrl")
            t_list = t_val if isinstance(t_val, list) else [t_val]
            for tu in t_list:
                cleaned = _clean_xhs_media_url(tu)
                if _is_note_cdn_url(cleaned):
                    thumb_urls.append(cleaned)
            im_val = obj.get("image")
            im_list = im_val if isinstance(im_val, list) else [im_val]
            for iu in im_list:
                cleaned = _clean_xhs_media_url(iu)
                if _is_note_cdn_url(cleaned):
                    img_urls.append(cleaned)

    for key, val in _extract_meta_tags(html):
        if key in ("og:video", "og:video:url", "og:video:secure_url"):
            cleaned = _clean_xhs_media_url(val)
            if cleaned and urlparse(cleaned).hostname:
                video_urls.append(cleaned)
        elif key == "og:image":
            cleaned = _clean_xhs_media_url(val)
            if _is_note_cdn_url(cleaned):
                img_urls.append(cleaned)
        elif key in ("og:title", "og:description") and not title:
            title = val

    video_urls = dedupe_keep_order(video_urls)
    img_urls = dedupe_keep_order(img_urls or thumb_urls)
    if not video_urls and not img_urls:
        return None

    note: Dict[str, Any] = {
        "noteId": preferred_id or "xhs",
        "title": title,
        "type": "video" if video_urls else "normal",
        "imageList": [{"urlDefault": u, "url": u} for u in img_urls],
    }
    if video_urls:
        note["video"] = {"media": {"stream": {"h264": [{"masterUrl": u} for u in video_urls]}}}
    return note


def _parse_note_dict(note: Dict[str, Any], preferred_id: str, config: Config) -> ExtractResult:
    """Convert a resolved Xiaohongshu note dictionary into an ExtractResult."""
    item_id = str(note.get("noteId") or note.get("id") or preferred_id or "xhs")
    raw_title = str(note.get("title") or note.get("desc") or "").strip()
    title = re.sub(r"\s+-\s*小红书$", "", raw_title).strip() or raw_title
    user = note.get("user")
    if isinstance(user, dict):
        author = str(user.get("nickname") or user.get("nickName") or user.get("name") or "").strip()
    else:
        author = ""

    assets: List[MediaAsset] = []
    image_list = note.get("imageList")
    video_obj = note.get("video")
    if isinstance(video_obj, dict) and video_obj:
        stream = (video_obj.get("media") or {}).get("stream") or {}
        v_urls = _collect_stream_urls(stream) if isinstance(stream, dict) else []
        origin_key = (video_obj.get("consumer") or {}).get("originVideoKey")
        if origin_key:
            v_urls = dedupe_keep_order(
                v_urls
                + [
                    f"https://sns-video-hw.xhscdn.com/{origin_key}",
                    f"https://sns-video-bd.xhscdn.com/{origin_key}",
                ]
            )
        if v_urls:
            cover_thumbs: List[str] = []
            if isinstance(image_list, list) and image_list and isinstance(image_list[0], dict):
                cover_thumbs = _build_thumb_candidates(image_list[0])
            assets.append(
                MediaAsset(
                    kind="video",
                    urls=v_urls,
                    default_ext=".mp4",
                    headers=XHS_DL_HEADERS,
                    mobile=False,
                    thumb_urls=cover_thumbs,
                )
            )

    # Extract images & live photos for image notes, or if video extraction yielded nothing.
    if (not assets or note.get("type") != "video") and isinstance(image_list, list):
        for img in image_list:
            if not isinstance(img, dict):
                continue
            img_urls = _build_image_candidates(img)
            if img_urls:
                assets.append(
                    MediaAsset(
                        kind="image",
                        urls=img_urls,
                        default_ext=".jpg",
                        headers=XHS_DL_HEADERS,
                        mobile=False,
                        thumb_urls=_build_thumb_candidates(img),
                    )
                )
            if config.save_live_photo_video and (img.get("livePhoto") or isinstance(img.get("stream"), dict)):
                live_stream = img.get("stream")
                if not isinstance(live_stream, dict) and isinstance(img.get("livePhoto"), dict):
                    lp = img["livePhoto"]
                    live_stream = (lp.get("media") or {}).get("stream") or lp.get("stream")
                live_urls = _collect_stream_urls(live_stream) if isinstance(live_stream, dict) else []
                if not live_urls and isinstance(img.get("livePhoto"), dict):
                    direct = img["livePhoto"].get("videoUrl") or img["livePhoto"].get("mediaUrl")
                    if direct:
                        live_urls = [normalize_http_url(direct)]
                if live_urls:
                    assets.append(
                        MediaAsset(
                            kind="live_video",
                            urls=live_urls,
                            default_ext=".mp4",
                            headers=XHS_DL_HEADERS,
                            mobile=False,
                        )
                    )

    if not assets:
        raise ExtractionError("已打开小红书页面，但未提取到视频或图片直链，建议更新 xhs_cookie 后重试。")

    return ExtractResult(
        platform="xiaohongshu",
        item_id=item_id,
        title=title or f"xhs_{item_id}",
        author=author,
        assets=assets,
    )


def parse_xhs_state(state: Dict[str, Any], preferred_id: str, config: Config) -> ExtractResult:
    """Parse decoded __INITIAL_STATE__ into ExtractResult."""
    note = _find_note_obj(state, preferred_id)
    if not note:
        raise ExtractionError("未在小红书页面中找到笔记内容，可能是链接已失效、需要登录态或触发了滑块验证。")
    return _parse_note_dict(note, preferred_id, config)


def _recover_target_url(resp_url: str, fallback_url: str) -> str:
    """Recover canonical note URL with xsec_token even when redirected to /404 or /login."""
    raw = resp_url or ""
    for candidate in (raw, unquote(raw)):
        m = re.search(r"(?:redirectPath|originalUrl)=([^&\s]+)", candidate)
        if m:
            inner = unquote(m.group(1))
            if inner.startswith("/"):
                inner = "https://www.xiaohongshu.com" + inner
            elif inner.startswith("http://www.xiaohongshu.com/"):
                inner = "https://" + inner[len("http://") :]
            if _NOTE_PATH_RE.search(inner):
                return inner
    path = (urlparse(raw).path or "").lower()
    if raw and "/404" not in path and "/login" not in path and _NOTE_PATH_RE.search(raw):
        return raw
    return fallback_url


def _alt_route_url(note_url: str) -> str:
    """Swap between /discovery/item/ and /explore/ while preserving query params like xsec_token."""
    if "/discovery/item/" in note_url:
        return note_url.replace("/discovery/item/", "/explore/", 1)
    if "/explore/" in note_url:
        return note_url.replace("/explore/", "/discovery/item/", 1)
    return ""


def _is_blocked_xhs_response(resp_url: str, html: str) -> bool:
    path = (urlparse(resp_url or "").path or "").lower()
    if "/404" in path or "/login" in path or "/website-login" in path:
        return True
    return "安全限制" in html or "验证码" in html


def _try_extract_ssr(html: str, preferred_id: str, config: Config) -> Optional[ExtractResult]:
    for parser in (_parse_xhs_state, _parse_xhs_setup_state):
        try:
            state = parser(html)
        except Exception:
            state = None
        if not state:
            continue
        note = _find_note_obj(state, preferred_id)
        if not note:
            continue
        try:
            return _parse_note_dict(note, preferred_id, config)
        except ExtractionError:
            continue
    return None


def _try_extract_meta(html: str, preferred_id: str, config: Config) -> Optional[ExtractResult]:
    meta_note = _parse_ld_and_meta_note(html, preferred_id)
    if not meta_note:
        return None
    try:
        return _parse_note_dict(meta_note, preferred_id, config)
    except ExtractionError:
        return None


_SHORTLINK_HOSTS = ("xhslink.com", "xhslink.cn")
_RESULT_CACHE_TTL = 60.0
_RESULT_CACHE_MAX = 32
_SHORTLINK_CACHE_TTL = 120.0
_SHORTLINK_CACHE_MAX = 64
_ROUTE_PREF_TTL = 180.0

_CACHE_LOCK = threading.Lock()
_RESULT_CACHE: Dict[Tuple[str, str, bool], Tuple[float, ExtractResult]] = {}
_SHORTLINK_CACHE: Dict[str, Tuple[float, str]] = {}
_PREFERRED_ROUTE: Dict[str, Tuple[float, bool, bool]] = {}


def _cookie_fingerprint(url: str, client: Any, config: Config) -> str:
    cookie_fn = getattr(client, "cookie_for_url", None)
    raw = str(config.xhs_cookie or "")
    if not raw and callable(cookie_fn):
        raw = str(cookie_fn("https://www.xiaohongshu.com/") or cookie_fn(url) or "")
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16] if raw else ""


def _get_cached_result(lookup_key: str, cookie_fp: str, save_live: bool) -> Optional[ExtractResult]:
    key = (lookup_key, cookie_fp, save_live)
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _RESULT_CACHE.get(key)
        if not entry:
            return None
        exp, res = entry
        if now >= exp:
            _RESULT_CACHE.pop(key, None)
            return None
        return res


def _put_cached_result(keys: List[str], cookie_fp: str, save_live: bool, res: ExtractResult) -> None:
    exp = time.monotonic() + _RESULT_CACHE_TTL
    with _CACHE_LOCK:
        for lk in keys:
            if not lk:
                continue
            _RESULT_CACHE[(lk, cookie_fp, save_live)] = (exp, res)
        while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
            oldest = min(_RESULT_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _RESULT_CACHE.pop(oldest, None)


def _get_cached_shortlink(short_url: str) -> str:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _SHORTLINK_CACHE.get(short_url)
        if not entry:
            return ""
        exp, resolved = entry
        if now >= exp:
            _SHORTLINK_CACHE.pop(short_url, None)
            return ""
        return resolved


def _put_cached_shortlink(short_url: str, resolved_url: str) -> None:
    if not short_url or not resolved_url or not _extract_note_id(resolved_url):
        return
    exp = time.monotonic() + _SHORTLINK_CACHE_TTL
    with _CACHE_LOCK:
        _SHORTLINK_CACHE[short_url] = (exp, resolved_url)
        while len(_SHORTLINK_CACHE) > _SHORTLINK_CACHE_MAX:
            oldest = min(_SHORTLINK_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _SHORTLINK_CACHE.pop(oldest, None)


def _get_preferred_route(cookie_fp: str) -> Tuple[bool, bool]:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _PREFERRED_ROUTE.get(cookie_fp)
        if not entry:
            return False, True
        exp, mobile, use_proxy = entry
        if now >= exp:
            _PREFERRED_ROUTE.pop(cookie_fp, None)
            return False, True
        return mobile, use_proxy


def _set_preferred_route(cookie_fp: str, mobile: bool, use_proxy: bool) -> None:
    exp = time.monotonic() + _ROUTE_PREF_TTL
    with _CACHE_LOCK:
        _PREFERRED_ROUTE[cookie_fp] = (exp, bool(mobile), bool(use_proxy))


def prewarm(config: Optional[Config] = None) -> None:
    """Warm DNS + TLS Keep-Alive connections for Xiaohongshu pages and CDNs."""
    warm_urls(
        [
            "https://www.xiaohongshu.com/",
            "https://xhslink.cn/",
            "https://sns-img-hw.xhscdn.com/",
            "https://sns-webpic-qc.xhscdn.com/",
        ],
        timeout=3.0,
    )


def _is_shortlink(url: str) -> bool:
    if _extract_note_id(url):
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _SHORTLINK_HOSTS)


def _resolve_shortlink_once(
    url: str,
    client: HttpClient,
    config: Config,
    probe_timeout: int,
    has_proxy: bool,
    prefer_proxy: bool,
) -> Tuple[str, Optional[ExtractResult]]:
    """Resolve xhslink once (with cache) so fallback probes skip redundant shortlink hops."""
    cached = _get_cached_shortlink(url)
    if cached:
        return cached, None

    hop_timeout = min(probe_timeout, 5)
    proxy_order = [prefer_proxy]
    if has_proxy:
        proxy_order.append(not prefer_proxy)

    for use_proxy in proxy_order:
        try:
            resp = client.get(
                url,
                mobile=True,
                follow_redirects=False,
                timeout=hop_timeout,
                use_proxy=use_proxy,
            )
        except HttpError:
            continue

        loc = resp.headers.get("location") or ""
        if loc:
            full_loc = urljoin(url, loc)
            recovered = _recover_target_url(full_loc, full_loc)
            _put_cached_shortlink(url, recovered)
            return recovered, None

        if resp.status < 300 and resp.body:
            html = resp.text()
            pref_id = _extract_note_id(resp.url or url)
            ssr = _try_extract_ssr(html, pref_id, config)
            if ssr is not None:
                return (resp.url or url), ssr
            recovered = _recover_target_url(resp.url or "", url)
            if recovered != url:
                _put_cached_shortlink(url, recovered)
                return recovered, None
        break
    return url, None


def _fetch_xhs(
    client: Any,
    target_url: str,
    headers: Dict[str, str],
    mobile: bool,
    use_proxy: bool,
    timeout: Optional[int] = None,
):
    if timeout is not None and isinstance(client, HttpClient):
        if not use_proxy:
            return client.get(
                target_url,
                headers=headers,
                mobile=mobile,
                follow_redirects=True,
                timeout=timeout,
                use_proxy=False,
            )
        return client.get(
            target_url,
            headers=headers,
            mobile=mobile,
            follow_redirects=True,
            timeout=timeout,
        )
    if not use_proxy:
        try:
            return client.get(target_url, headers=headers, mobile=mobile, follow_redirects=True, use_proxy=False)
        except TypeError:
            pass
    return client.get(target_url, headers=headers, mobile=mobile, follow_redirects=True)


def extract_xiaohongshu(url: str, client: HttpClient, config: Config) -> ExtractResult:
    """Fetch Xiaohongshu share link across PC web and mobile H5 SSR with fast caching and parallel fallbacks."""
    url = url.strip()
    is_real_http = isinstance(client, HttpClient)
    use_cache = type(client) is HttpClient
    cookie_fp = _cookie_fingerprint(url, client, config) if is_real_http else ""

    if use_cache:
        cached = _get_cached_result(f"url:{url}", cookie_fp, config.save_live_photo_video)
        if cached is not None:
            return cached
        direct_id = _extract_note_id(url)
        if direct_id:
            cached = _get_cached_result(f"id:{direct_id}", cookie_fp, config.save_live_photo_video)
            if cached is not None:
                return cached

    state_lock = threading.Lock()
    first_resp_url = ""
    meta_fallback: Optional[ExtractResult] = None
    saw_block = False
    saw_html = False
    last_http_error: Optional[Exception] = None
    tried: Set[Tuple[str, bool, bool, bool]] = set()

    def _run_attempt(
        target_url: str,
        headers: Dict[str, str],
        mobile: bool,
        use_proxy: bool,
        timeout: Optional[int] = None,
    ) -> Optional[ExtractResult]:
        nonlocal first_resp_url, meta_fallback, saw_block, saw_html, last_http_error
        strip_cookie = "Cookie" in headers and headers["Cookie"] == ""
        key = (target_url, mobile, strip_cookie, use_proxy)
        if not target_url:
            return None
        with state_lock:
            if key in tried:
                return None
            tried.add(key)

        try:
            resp = _fetch_xhs(client, target_url, headers, mobile, use_proxy, timeout=timeout)
        except HttpError as e:
            with state_lock:
                last_http_error = e
            return None

        html = resp.text()
        resp_url = resp.url or target_url
        pref_id = _extract_note_id(resp_url) or _extract_note_id(url)

        path = (urlparse(resp_url).path or "").lower()
        is_error_route = "/404" in path or "/login" in path or "/website-login" in path
        ssr_res: Optional[ExtractResult] = None
        meta_cand: Optional[ExtractResult] = None
        blocked = False

        if not is_error_route:
            ssr_res = _try_extract_ssr(html, pref_id, config)
            if ssr_res is None:
                meta_cand = _try_extract_meta(html, pref_id, config)
        if ssr_res is None:
            blocked = _is_blocked_xhs_response(resp_url, html)

        with state_lock:
            saw_html = True
            if not first_resp_url:
                first_resp_url = resp_url
            if ssr_res is None and meta_fallback is None and meta_cand is not None:
                meta_fallback = meta_cand
            if blocked:
                saw_block = True

        return ssr_res

    def _cache_and_return(res: ExtractResult, extra_url: str = "") -> ExtractResult:
        if use_cache:
            keys = [f"url:{url}", f"id:{res.item_id}"]
            if extra_url and extra_url != url:
                keys.append(f"url:{extra_url}")
            _put_cached_result(keys, cookie_fp, config.save_live_photo_video, res)
        return res

    has_proxy = bool(urllib.request.getproxies())
    cookie_fn = getattr(client, "cookie_for_url", None)

    if is_real_http:
        probe_timeout = max(4, min(int(config.timeout), 8))
        prefer_mobile, prefer_proxy = _get_preferred_route(cookie_fp)
        if not has_proxy:
            prefer_proxy = True

        working_url = url
        if _is_shortlink(url):
            working_url, immediate_res = _resolve_shortlink_once(
                url,
                client,
                config,
                probe_timeout,
                has_proxy=has_proxy,
                prefer_proxy=prefer_proxy,
            )
            if immediate_res is not None:
                return _cache_and_return(immediate_res, working_url)
            resolved_id = _extract_note_id(working_url)
            if use_cache and resolved_id:
                cached = _get_cached_result(f"id:{resolved_id}", cookie_fp, config.save_live_photo_video)
                if cached is not None:
                    return cached

        primary_hdrs = {} if prefer_mobile else XHS_DL_HEADERS
        res = _run_attempt(working_url, primary_hdrs, mobile=prefer_mobile, use_proxy=prefer_proxy, timeout=probe_timeout)
        if res is not None:
            return _cache_and_return(res, working_url)

        recovered_url = _recover_target_url(first_resp_url or working_url, working_url)
        alt_url = _alt_route_url(recovered_url)
        has_cookie = bool(
            callable(cookie_fn)
            and (
                cookie_fn(recovered_url)
                or cookie_fn(working_url)
                or cookie_fn(url)
                or cookie_fn("https://www.xiaohongshu.com/")
            )
        )

        wave_a: List[Tuple[str, Dict[str, str], bool, bool]] = [
            (recovered_url, XHS_DL_HEADERS if prefer_mobile else {}, not prefer_mobile, prefer_proxy),
        ]
        if working_url != recovered_url:
            wave_a.append((working_url, {}, True, prefer_proxy))
        if alt_url:
            wave_a.append((alt_url, {}, True, prefer_proxy))
            wave_a.append((alt_url, XHS_DL_HEADERS, False, prefer_proxy))
        if has_cookie:
            wave_a.append((recovered_url, {"Cookie": ""}, True, prefer_proxy))

        wave_b: List[Tuple[str, Dict[str, str], bool, bool]] = []
        if has_cookie:
            wave_b.append((recovered_url, {**XHS_DL_HEADERS, "Cookie": ""}, False, prefer_proxy))
        if has_proxy:
            alt_proxy = not prefer_proxy
            wave_b.append((recovered_url, {}, True, alt_proxy))
            wave_b.append((recovered_url, XHS_DL_HEADERS, False, alt_proxy))
            if alt_url:
                wave_b.append((alt_url, {}, True, alt_proxy))
            if has_cookie:
                wave_b.append((recovered_url, {"Cookie": ""}, True, alt_proxy))

        def _run_parallel_wave(batch: List[Tuple[str, Dict[str, str], bool, bool]]) -> Optional[ExtractResult]:
            if not batch:
                return None
            q: queue.Queue = queue.Queue()
            for target_u, hdrs, mob, use_px in batch:
                def _worker(tu=target_u, hd=hdrs, mb=mob, up=use_px) -> None:
                    found_item: Optional[ExtractResult] = None
                    try:
                        found_item = _run_attempt(tu, hd, mobile=mb, use_proxy=up, timeout=probe_timeout)
                    except Exception:
                        found_item = None
                    finally:
                        q.put((found_item, mb, up))

                threading.Thread(target=_worker, daemon=True).start()

            for _ in range(len(batch)):
                found, mob, use_px = q.get()
                if found is not None:
                    _set_preferred_route(cookie_fp, mob, use_px)
                    return found
            return None

        for wave in (wave_a, wave_b):
            hit = _run_parallel_wave(wave)
            if hit is not None:
                return _cache_and_return(hit, recovered_url)
        if working_url != url:
            with _CACHE_LOCK:
                _SHORTLINK_CACHE.pop(url, None)
    else:
        res = _run_attempt(url, XHS_DL_HEADERS, mobile=False, use_proxy=True)
        if res is not None:
            return res

        recovered_url = _recover_target_url(first_resp_url, url)
        alt_url = _alt_route_url(recovered_url)
        has_cookie = bool(
            callable(cookie_fn)
            and (
                cookie_fn(recovered_url)
                or cookie_fn(url)
                or cookie_fn("https://www.xiaohongshu.com/")
            )
        )

        attempts: List[Tuple[str, Dict[str, str], bool, bool]] = [
            (recovered_url, {}, True, True),
        ]
        if url != recovered_url:
            attempts.append((url, {}, True, True))
        if alt_url:
            attempts.append((alt_url, {}, True, True))
            attempts.append((alt_url, XHS_DL_HEADERS, False, True))
        if has_cookie:
            attempts.append((recovered_url, {"Cookie": ""}, True, True))
            attempts.append((recovered_url, {**XHS_DL_HEADERS, "Cookie": ""}, False, True))
        if has_proxy:
            attempts.append((recovered_url, {}, True, False))
            attempts.append((recovered_url, XHS_DL_HEADERS, False, False))

        for target_url, headers, mobile, use_proxy in attempts:
            res = _run_attempt(target_url, headers, mobile=mobile, use_proxy=use_proxy)
            if res is not None:
                return res

    if meta_fallback is not None:
        return _cache_and_return(meta_fallback)

    if saw_block:
        raise ExtractionError("小红书页面被拦截或需要登录验证（若开启了代理/VPN 请尝试分流或直连，或在 config.json 配置有效 xhs_cookie 后重试）。")
    if last_http_error is not None and not saw_html:
        raise last_http_error
    raise ExtractionError("未在小红书页面中找到笔记内容，可能是链接已失效、需要登录态或触发了滑块验证。")

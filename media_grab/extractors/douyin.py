"""Douyin extractor: watermark-free highest-bitrate videos, image slides, and live photos."""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from ..config import DEFAULT_CONFIG_DIR, Config
from ..http_client import HttpClient, warm_urls
from ..utils import dedupe_keep_order, extract_balanced_json, loads_relaxed_json, normalize_http_url
from . import ExtractResult, ExtractionError, MediaAsset

logger = logging.getLogger("media_grab.douyin")


def _configure_log_from_env() -> None:
    """Enable douyin extractor debug/info logging on demand via MEDIA_GRAB_LOG."""
    raw = os.environ.get("MEDIA_GRAB_LOG", "").strip().upper()
    if not raw:
        return
    level = getattr(logging, raw, None)
    if not isinstance(level, int):
        return
    logger.setLevel(level)
    if not getattr(logger, "_media_grab_env_handler", False) and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger._media_grab_env_handler = True  # type: ignore[attr-defined]
        logger.propagate = False


_configure_log_from_env()

DOUYIN_DL_HEADERS = {"Referer": "https://www.douyin.com/"}


def _extract_aweme_id(url: str) -> str:
    for pattern in (
        r"/(?:video|note|slides)/(\d+)",
        r"[?&](?:modal_id|aweme_id)=(\d+)",
    ):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return ""


def _parse_douyin_json(html: str) -> Optional[Dict[str, Any]]:
    raw = extract_balanced_json(html, r'<script[^>]+id=["\']_ROUTER_DATA["\'][^>]*>')
    if raw:
        return loads_relaxed_json(raw)

    raw = extract_balanced_json(html, r"window\._ROUTER_DATA\s*=")
    if raw:
        return loads_relaxed_json(raw)

    m = re.search(r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        decoded = unquote(m.group(1).strip())
        if decoded.startswith("{"):
            return json.loads(decoded)
    return None


def _find_aweme_item(data: Any) -> Optional[Dict[str, Any]]:
    """Locate the aweme item dict from _ROUTER_DATA / RENDER_DATA regardless of route key changes."""
    if isinstance(data, dict):
        loader = data.get("loaderData")
        if isinstance(loader, dict):
            for val in loader.values():
                if not isinstance(val, dict):
                    continue
                res = val.get("videoInfoRes")
                if isinstance(res, dict) and isinstance(res.get("item_list"), list) and res["item_list"]:
                    return res["item_list"][0]
                if isinstance(val.get("awemeDetail"), dict) and val["awemeDetail"].get("aweme_id"):
                    return val["awemeDetail"]

        if data.get("aweme_id") and ("video" in data or "images" in data):
            return data

        for val in data.values():
            found = _find_aweme_item(val)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_aweme_item(item)
            if found:
                return found
    return None


_GEAR_RES_RE = re.compile(r"(4k|2k|1080|720|540|480|360)", re.IGNORECASE)
_VIDEO_ID_QS_RE = re.compile(r"[?&]video_id=([^&\s]+)")
_IMG_RESIZE_TOKEN_RE = re.compile(
    r"(?:~tplv-[^.?]*?:(?:w:)?(\d{3,5})(?::(\d{1,5}))?|[?&](?:w|width)=(\d{3,5})\b)",
    re.IGNORECASE,
)


def _clean_video_url(url: str) -> str:
    u = normalize_http_url(url)
    u = re.sub(r"([/?&=])playwm([/?&=]|$)", r"\1play\2", u)
    u = re.sub(r"([?&])watermark=[1-9]\d*\b", r"\1watermark=0", u)
    return u


def _is_suspected_watermarked_video(url: str) -> int:
    low = (url or "").lower()
    if any(tok in low for tok in ("/mps/logo/", "playwm", "watermark=1", "logo_name=")):
        return 1
    return 0


def _gear_resolution_score(gear_name: str, urls: Optional[List[str]] = None) -> int:
    text = f"{gear_name or ''} {' '.join(urls or ())}"
    m = _GEAR_RES_RE.search(text)
    if not m:
        return 0
    tok = m.group(1).lower()
    if tok == "4k":
        return 2160
    if tok == "2k":
        return 1440
    return int(tok)


def _collect_video_urls(video_obj: Dict[str, Any]) -> List[str]:
    """Order video stream URLs from highest resolution/bitrate (original master first) down."""
    candidates: List[str] = []

    bit_rates = video_obj.get("bit_rate")
    bit_rate_urls: List[str] = []
    if isinstance(bit_rates, list) and bit_rates:
        def _score(entry: Dict[str, Any]) -> tuple:
            pa = entry.get("play_addr") or {}
            pixels = int(pa.get("width") or 0) * int(pa.get("height") or 0)
            urls = pa.get("url_list") or []
            gear_hint = _gear_resolution_score(str(entry.get("gear_name") or ""), urls)
            br = int(entry.get("bit_rate") or 0)
            size = int(pa.get("data_size") or 0)
            return (pixels, gear_hint, br, size)

        for entry in sorted((e for e in bit_rates if isinstance(e, dict)), key=_score, reverse=True):
            pa = entry.get("play_addr") or {}
            for u in pa.get("url_list") or []:
                bit_rate_urls.append(_clean_video_url(u))

    addr_candidates: List[tuple] = []
    fallback_uri = ""
    for key in ("play_addr_265", "play_addr_h264", "play_addr"):
        addr = video_obj.get(key)
        if not isinstance(addr, dict):
            continue
        if not fallback_uri and addr.get("uri"):
            fallback_uri = str(addr["uri"]).strip()
        urls = [_clean_video_url(u) for u in (addr.get("url_list") or []) if u]
        if not urls:
            continue
        pixels = int(addr.get("width") or 0) * int(addr.get("height") or 0)
        size = int(addr.get("data_size") or 0)
        addr_candidates.append(((pixels, size), urls))

    if not fallback_uri and not bit_rate_urls:
        for _, urls in addr_candidates:
            for u in urls:
                m = _VIDEO_ID_QS_RE.search(u)
                if m:
                    fallback_uri = m.group(1)
                    break
            if fallback_uri:
                break

    if fallback_uri:
        # Douyin's ratio=default&improve_bitrate=1 serves un-capped original master (often 1080p/2K/HEVC)
        # even when feed bit_rate / SSR play_addr omits 1080p or hard-caps to 720p/540p.
        candidates.append(
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={fallback_uri}"
            "&ratio=default&improve_bitrate=1&watermark=0&line=0"
        )
        max_dim = max(int(video_obj.get("width") or 0), int(video_obj.get("height") or 0))
        if max_dim > 1920:
            candidates.append(
                f"https://aweme.snssdk.com/aweme/v1/play/?video_id={fallback_uri}"
                "&ratio=4k&improve_bitrate=1&watermark=0&line=0"
            )
        candidates.append(
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={fallback_uri}"
            "&ratio=1080p&improve_bitrate=1&watermark=0&line=0"
        )

    candidates.extend(bit_rate_urls)
    addr_candidates.sort(key=lambda item: item[0], reverse=True)
    for _, urls in addr_candidates:
        candidates.extend(urls)

    return sorted(dedupe_keep_order(candidates), key=_is_suspected_watermarked_video)


def _rank_image_urls(urls: List[str], prefer_webp: bool = False) -> List[str]:
    """Order watermark-free URLs preferring original/highest-res JPG/PNG or WebP for previews."""
    cleaned = [normalize_http_url(u) for u in urls if u]

    def _priority(u: str) -> tuple:
        low = u.lower()
        has_wm = 1 if (
            "watermark" in low
            or "_wm" in low
            or "/wm/" in low
            or "tplv-dy-water" in low
            or "aweme-water" in low
            or "water-mark" in low
            or "water_mark" in low
        ) else 0
        is_webp = 1 if ".webp" in low else 0
        fmt_rank = (0 if is_webp else 1) if prefer_webp else is_webp

        if prefer_webp:
            return (has_wm, fmt_rank, 0, 0)

        is_noop = "~noop" in low or "tplv-dy-noop" in low or "/origin/" in low
        if is_noop:
            return (has_wm, 0, 0, fmt_rank)

        m = _IMG_RESIZE_TOKEN_RE.search(low)
        width = int(m.group(1) or m.group(3) or 0) if m else 0
        height = int(m.group(2) or 0) if m and m.group(2) else 0
        dim = max(width, height)
        is_small_tag = "shrink" in low or "thumb" in low
        if is_small_tag and dim < 1080:
            tier = 3
        elif dim >= 1080 or dim == 0:
            tier = 1
        else:
            tier = 2
        return (has_wm, tier, -dim, fmt_rank)

    return dedupe_keep_order(sorted(cleaned, key=_priority))


def parse_douyin_payload(data: Dict[str, Any], fallback_id: str, config: Config) -> ExtractResult:
    """Parse an already-decoded Douyin _ROUTER_DATA / RENDER_DATA object into ExtractResult."""
    item = _find_aweme_item(data)
    if not item:
        raise ExtractionError("未在页面数据中找到作品详情，可能是作品已删除或触发了风控验证。")

    item_id = str(item.get("aweme_id") or fallback_id or "douyin")
    title = str(item.get("desc") or "").strip()
    author = str((item.get("author") or {}).get("nickname") or "").strip()

    assets: List[MediaAsset] = []
    images = item.get("images")
    if isinstance(images, list) and images:
        for img in images:
            if not isinstance(img, dict):
                continue
            origin_urls = list((img.get("origin_url") or {}).get("url_list") or []) if isinstance(img.get("origin_url"), dict) else []
            # On Douyin image slides, download_url_list points to watermarked variants while origin_url/url_list are clean.
            clean_urls = origin_urls + list(img.get("url_list") or [])
            dl_urls = list(img.get("download_url_list") or [])
            raw_urls = clean_urls or dl_urls
            ranked = _rank_image_urls(raw_urls, prefer_webp=False)
            if ranked:
                assets.append(
                    MediaAsset(
                        kind="image",
                        urls=ranked,
                        default_ext=".jpg",
                        headers=DOUYIN_DL_HEADERS,
                        mobile=True,
                        thumb_urls=_rank_image_urls(raw_urls, prefer_webp=True),
                    )
                )
            if config.save_live_photo_video and isinstance(img.get("video"), dict):
                live_urls = _collect_video_urls(img["video"])
                if live_urls:
                    assets.append(
                        MediaAsset(
                            kind="live_video",
                            urls=live_urls,
                            default_ext=".mp4",
                            headers=DOUYIN_DL_HEADERS,
                            mobile=True,
                        )
                    )
        if assets:
            return ExtractResult(
                platform="douyin",
                item_id=item_id,
                title=title or f"douyin_{item_id}",
                author=author,
                assets=assets,
            )

    video = item.get("video")
    if isinstance(video, dict):
        v_urls = _collect_video_urls(video)
        cover_raw: List[str] = []
        for cov_key in ("cover", "origin_cover"):
            cov = video.get(cov_key)
            if isinstance(cov, dict) and isinstance(cov.get("url_list"), list):
                cover_raw.extend(cov["url_list"])
        thumb_urls = _rank_image_urls(cover_raw, prefer_webp=True) if cover_raw else []
        if v_urls:
            assets.append(
                MediaAsset(
                    kind="video",
                    urls=v_urls,
                    default_ext=".mp4",
                    headers=DOUYIN_DL_HEADERS,
                    mobile=True,
                    thumb_urls=thumb_urls,
                )
            )

    if not assets:
        raise ExtractionError("解析到了抖音作品节点，但没有提取到可下载的视频或图片流。")

    return ExtractResult(
        platform="douyin",
        item_id=item_id,
        title=title or f"douyin_{item_id}",
        author=author,
        assets=assets,
    )


_DOUYIN_FEED_URLS = (
    "https://api5-normal-c-hl.amemv.com/aweme/v1/feed/?aweme_id={aweme_id}&aid=1128",
    "https://aweme.snssdk.com/aweme/v1/feed/?aweme_id={aweme_id}&aid=1128",
)
_TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_TTL_SECONDS = 6 * 3600
_TTWID_LOCK = threading.Lock()
_TTWID_CACHE: Dict[str, Any] = {"value": "", "expires_at": 0.0}


def _ttwid_cache_file() -> Path:
    cfg_dir = Path(os.environ.get("MEDIA_GRAB_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    return cfg_dir / ".douyin_ttwid.json"


def _load_ttwid_disk_cache(now: float) -> str:
    try:
        path = _ttwid_cache_file()
        if not path.is_file():
            return ""
        raw = json.loads(path.read_text(encoding="utf-8"))
        val = str(raw.get("value") or "").strip()
        exp = float(raw.get("expires_at") or 0.0)
        if val and now < exp:
            _TTWID_CACHE["value"] = val
            _TTWID_CACHE["expires_at"] = exp
            return val
    except Exception:
        pass
    return ""


def _save_ttwid_disk_cache(val: str, expires_at: float) -> None:
    try:
        path = _ttwid_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"value": val, "expires_at": expires_at}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _has_aweme_item(data: Any) -> bool:
    return bool(data and _find_aweme_item(data))


def _merge_cookie_header(existing_cookie: str, name: str, value: str) -> str:
    """Append name=value to a Cookie header string unless already present."""
    if not existing_cookie:
        return f"{name}={value}"
    if re.search(rf"(?:^|;\s*){re.escape(name)}=", existing_cookie):
        return existing_cookie
    return f"{existing_cookie.rstrip('; ')}; {name}={value}"


def _has_cookie_key(cookie_str: str, name: str) -> bool:
    return bool(cookie_str and re.search(rf"(?:^|;\s*){re.escape(name)}=", cookie_str))


def _register_ttwid(config: Config) -> str:
    """Register an anonymous ttwid cookie for iesdouyin SSR share pages."""
    import urllib.request
    from ..http_client import MOBILE_UA

    payload = json.dumps(
        {
            "region": "cn",
            "aid": 1128,
            "needFid": False,
            "service": "www.iesdouyin.com",
            "migrate_info": {"ticket": "", "source": "node"},
            "cbUrlProtocol": "https",
            "union": True,
        }
    ).encode("utf-8")
    timeout = max(5, min(int(getattr(config, "timeout", 25) or 25), 10))
    req = urllib.request.Request(
        _TTWID_REGISTER_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": MOBILE_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            set_cookies = resp.headers.get_all("Set-Cookie") or []
    except Exception:
        return ""

    for sc in set_cookies:
        pair = sc.split(";", 1)[0].strip()
        if pair.startswith("ttwid="):
            return pair.split("=", 1)[1].strip()
    return ""


def peek_cached_ttwid(use_disk_cache: bool = True) -> str:
    """Return in-memory (or optional disk-cached) ttwid without performing network I/O."""
    now = time.time()
    with _TTWID_LOCK:
        cached = str(_TTWID_CACHE.get("value") or "")
        if cached and now < float(_TTWID_CACHE.get("expires_at") or 0.0):
            return cached
        if use_disk_cache:
            return _load_ttwid_disk_cache(now)
    return ""


def _get_anonymous_ttwid(
    config: Config,
    force_refresh: bool = False,
    use_disk_cache: bool = True,
) -> str:
    """Return a cached anonymous ttwid cookie value if available."""
    if not force_refresh:
        cached = peek_cached_ttwid(use_disk_cache=use_disk_cache)
        if cached:
            return cached

    val = _register_ttwid(config)
    if val:
        exp = time.time() + _TTWID_TTL_SECONDS
        with _TTWID_LOCK:
            _TTWID_CACHE["value"] = val
            _TTWID_CACHE["expires_at"] = exp
        if use_disk_cache:
            _save_ttwid_disk_cache(val, exp)
    return val


def prewarm(config: Optional[Config] = None) -> None:
    """Best-effort background warmup of anonymous ttwid cache and Douyin TLS routes."""
    warm_urls(
        [
            "https://www.iesdouyin.com/",
            "https://v.douyin.com/",
            "https://aweme.snssdk.com/",
        ],
        timeout=3.0,
    )
    _get_anonymous_ttwid(config or Config(), use_disk_cache=True)


def _fetch_aweme_from_feed(aweme_id: str, client: HttpClient) -> Optional[Dict[str, Any]]:
    """Fetch exact matching aweme item from Douyin's mobile feed API."""
    target_id = str(aweme_id)
    for template in _DOUYIN_FEED_URLS:
        feed_url = template.format(aweme_id=target_id)
        try:
            feed_resp = client.get(feed_url, mobile=True, follow_redirects=True)
            payload = json.loads(feed_resp.text())
        except Exception as exc:
            logger.debug("douyin feed request failed url=%s: %s", feed_url, exc)
            continue
        aweme_list = payload.get("aweme_list") if isinstance(payload, dict) else None
        if not isinstance(aweme_list, list):
            continue
        for item in aweme_list:
            if isinstance(item, dict) and str(item.get("aweme_id") or "") == target_id:
                return {"aweme_detail": item}
    return None


def _share_url_for(target_url: str, aweme_id: str) -> str:
    low = (target_url or "").lower()
    if "iesdouyin.com" in low or "m.douyin.com/share/" in low:
        return target_url.split("?", 1)[0]
    return f"https://www.iesdouyin.com/share/video/{aweme_id}/"


@dataclass
class _DouyinContext:
    url: str
    client: HttpClient
    config: Config
    is_real_http: bool
    cookie_fn: Any
    direct_id: str
    html: str = ""
    data: Optional[Dict[str, Any]] = None
    resolved_url: str = ""
    aweme_id: str = ""
    used_cached_ttwid: bool = False
    initial_cookie: str = ""
    cached_ttwid: str = ""
    refresh_base_share: str = ""
    refresh_cookie: str = ""


def _describe_parse_miss(data: Optional[Dict[str, Any]]) -> str:
    if data is None:
        return "no _ROUTER_DATA or RENDER_DATA JSON in page"
    return "JSON parsed cleanly but aweme item missing"


def _apply_primary_response(ctx: _DouyinContext, resp: Any, update_id: bool = True) -> str:
    ctx.resolved_url = resp.url
    ctx.html = resp.text()
    ctx.data = _parse_douyin_json(ctx.html)
    if update_id:
        ctx.aweme_id = _extract_aweme_id(ctx.resolved_url) or ctx.direct_id
    return "" if _has_aweme_item(ctx.data) else _describe_parse_miss(ctx.data)


def _step_direct_share_fast(ctx: _DouyinContext) -> str:
    # Skip heavy desktop SSR HTML on www.douyin.com when aweme_id is already in URL.
    target_share = _share_url_for(ctx.url, ctx.direct_id)
    had_cached = bool(peek_cached_ttwid(use_disk_cache=True))
    ttwid = _get_anonymous_ttwid(ctx.config, use_disk_cache=True)
    ctx.used_cached_ttwid = bool(ttwid and had_cached)
    hdrs = {"Cookie": f"ttwid={ttwid}"} if ttwid else None
    resp = ctx.client.get(target_share, headers=hdrs, mobile=True, follow_redirects=True)
    return _apply_primary_response(ctx, resp, update_id=False)


def _step_cookie_ttwid_page(ctx: _DouyinContext) -> str:
    resp = ctx.client.get(ctx.url, mobile=True, follow_redirects=True)
    return _apply_primary_response(ctx, resp)


def _step_cached_ttwid_page(ctx: _DouyinContext) -> str:
    ctx.used_cached_ttwid = True
    hdrs = {"Cookie": _merge_cookie_header(ctx.initial_cookie, "ttwid", ctx.cached_ttwid)}
    resp = ctx.client.get(ctx.url, headers=hdrs, mobile=True, follow_redirects=True)
    return _apply_primary_response(ctx, resp)


def _step_cold_start_parallel(ctx: _DouyinContext) -> str:
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_nr = pool.submit(ctx.client.get, ctx.url, None, True, False)
        fut_tok = pool.submit(_get_anonymous_ttwid, ctx.config, False, True)
        nr_resp = fut_nr.result()
        ttwid = fut_tok.result()

    loc = nr_resp.headers.get("location") or nr_resp.url or ctx.url
    ctx.resolved_url = loc
    ctx.aweme_id = _extract_aweme_id(loc) or ctx.direct_id
    if nr_resp.status < 300 and nr_resp.body:
        ctx.html = nr_resp.text()
        ctx.data = _parse_douyin_json(ctx.html)
    elif not ctx.aweme_id and loc and loc != ctx.url:
        hdrs = {"Cookie": f"ttwid={ttwid}"} if ttwid else None
        resp = ctx.client.get(loc, headers=hdrs, mobile=True, follow_redirects=True)
        _apply_primary_response(ctx, resp)
    if not _has_aweme_item(ctx.data) and ctx.aweme_id:
        target_share = _share_url_for(loc, ctx.aweme_id)
        share_cookie = ctx.cookie_fn(target_share) if callable(ctx.cookie_fn) else ""
        hdrs = {"Cookie": _merge_cookie_header(share_cookie, "ttwid", ttwid)} if ttwid else None
        resp = ctx.client.get(target_share, headers=hdrs, mobile=True, follow_redirects=True)
        _apply_primary_response(ctx, resp, update_id=False)
    return "" if _has_aweme_item(ctx.data) else _describe_parse_miss(ctx.data)


def _step_plain_page(ctx: _DouyinContext) -> str:
    resp = ctx.client.get(ctx.url, mobile=True, follow_redirects=True)
    return _apply_primary_response(ctx, resp)


def _select_primary_strategy(ctx: _DouyinContext) -> Tuple[str, Callable[[_DouyinContext], str]]:
    if ctx.is_real_http and ctx.direct_id and not ctx.config.douyin_cookie:
        return "direct_share_fast", _step_direct_share_fast
    if ctx.is_real_http:
        ctx.initial_cookie = ctx.cookie_fn(ctx.url) if callable(ctx.cookie_fn) else ""
        if _has_cookie_key(ctx.initial_cookie, "ttwid"):
            return "cookie_ttwid_page", _step_cookie_ttwid_page
        ctx.cached_ttwid = peek_cached_ttwid(use_disk_cache=True)
        if ctx.cached_ttwid:
            return "cached_ttwid_page", _step_cached_ttwid_page
        return "cold_start_parallel", _step_cold_start_parallel
    return "plain_page", _step_plain_page


def _can_iesdouyin_fallback(ctx: _DouyinContext) -> bool:
    return bool(ctx.aweme_id) and "iesdouyin.com" not in (ctx.resolved_url or "").lower()


def _step_iesdouyin_fallback(ctx: _DouyinContext) -> str:
    share_url = f"https://www.iesdouyin.com/share/video/{ctx.aweme_id}/"
    try:
        resp2 = ctx.client.get(share_url, mobile=True, follow_redirects=True)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    ctx.html = resp2.text()
    ctx.resolved_url = resp2.url
    cand = _parse_douyin_json(ctx.html)
    if _has_aweme_item(cand) or not ctx.data:
        ctx.data = cand
    return "" if _has_aweme_item(ctx.data) else _describe_parse_miss(cand)


def _can_refresh_ttwid_fallback(ctx: _DouyinContext) -> bool:
    if not ctx.aweme_id:
        return False
    ctx.refresh_base_share = _share_url_for(ctx.resolved_url, ctx.aweme_id)
    ctx.refresh_cookie = ctx.cookie_fn(ctx.refresh_base_share) if callable(ctx.cookie_fn) else ""
    return not _has_cookie_key(ctx.refresh_cookie, "ttwid")


def _step_refresh_ttwid_fallback(ctx: _DouyinContext) -> str:
    ttwid = _get_anonymous_ttwid(
        ctx.config,
        force_refresh=ctx.used_cached_ttwid,
        use_disk_cache=ctx.is_real_http,
    )
    if not ttwid:
        return "failed to obtain anonymous ttwid"
    try:
        hdrs = {"Cookie": _merge_cookie_header(ctx.refresh_cookie, "ttwid", ttwid)}
        resp3 = ctx.client.get(ctx.refresh_base_share, headers=hdrs, mobile=True, follow_redirects=True)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    ctx.html = resp3.text()
    cand = _parse_douyin_json(ctx.html)
    if _has_aweme_item(cand) or not ctx.data:
        ctx.data = cand
    return "" if _has_aweme_item(ctx.data) else _describe_parse_miss(cand)


def _can_feed_fallback(ctx: _DouyinContext) -> bool:
    return bool(ctx.aweme_id)


def _step_feed_fallback(ctx: _DouyinContext) -> str:
    feed_data = _fetch_aweme_from_feed(ctx.aweme_id, ctx.client)
    if feed_data:
        ctx.data = feed_data
        return ""
    return "feed API returned no matching aweme_id"


_FALLBACK_STRATEGIES: Tuple[
    Tuple[str, Callable[[_DouyinContext], bool], Callable[[_DouyinContext], str]],
    ...,
] = (
    ("iesdouyin_share_fallback", _can_iesdouyin_fallback, _step_iesdouyin_fallback),
    ("refresh_ttwid_fallback", _can_refresh_ttwid_fallback, _step_refresh_ttwid_fallback),
    ("feed_api_fallback", _can_feed_fallback, _step_feed_fallback),
)


def extract_douyin(url: str, client: HttpClient, config: Config) -> ExtractResult:
    """Fetch Douyin item through an ordered strategy pipeline with fallbacks and per-hop logging."""
    _configure_log_from_env()
    direct_id = _extract_aweme_id(url)
    ctx = _DouyinContext(
        url=url,
        client=client,
        config=config,
        is_real_http=isinstance(client, HttpClient),
        cookie_fn=getattr(client, "cookie_for_url", None),
        direct_id=direct_id,
        resolved_url=url,
        aweme_id=direct_id,
    )

    primary_name, primary_fn = _select_primary_strategy(ctx)
    pipeline: List[Tuple[str, Callable[[_DouyinContext], bool], Callable[[_DouyinContext], str]]] = [
        (primary_name, lambda _ctx: True, primary_fn),
        *_FALLBACK_STRATEGIES,
    ]

    attempted_hop = 0
    for name, is_eligible, run_step in pipeline:
        if not is_eligible(ctx):
            logger.debug("douyin skip %s (precondition not met)", name)
            continue
        attempted_hop += 1
        logger.debug("douyin hop #%d trying %s url=%s", attempted_hop, name, ctx.resolved_url or ctx.url)
        fail_reason = run_step(ctx)
        if _has_aweme_item(ctx.data):
            logger.info(
                "douyin extract succeeded at hop #%d (%s) aweme_id=%s",
                attempted_hop,
                name,
                ctx.aweme_id or "unknown",
            )
            return parse_douyin_payload(ctx.data, ctx.aweme_id, config)
        logger.debug("douyin hop #%d failed (%s): %s", attempted_hop, name, fail_reason or "no aweme item")

    if any(k in ctx.html for k in ("验证码", "安全验证", "captcha", "waf")):
        raise ExtractionError("抖音触发了安全验证（滑块/风控），请在 config.json 配置 douyin_cookie 后重试。")
    if ctx.data:
        raise ExtractionError("未在页面数据中找到作品详情，可能是接口结构已调整或作品已失效。")
    raise ExtractionError("未能从抖音分享页提取作品数据，请检查链接是否有效或已设为私密。")

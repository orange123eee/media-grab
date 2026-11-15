"""Xiaohongshu extractor: watermark-free high-res images, live photos, and highest-bitrate videos."""

import html as html_lib
import json
import re
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

from ..config import Config
from ..http_client import HttpClient, HttpError
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


def _fetch_xhs(
    client: HttpClient,
    target_url: str,
    headers: Dict[str, str],
    mobile: bool,
    use_proxy: bool,
):
    if not use_proxy:
        try:
            return client.get(target_url, headers=headers, mobile=mobile, follow_redirects=True, use_proxy=False)
        except TypeError:
            pass
    return client.get(target_url, headers=headers, mobile=mobile, follow_redirects=True)


def extract_xiaohongshu(url: str, client: HttpClient, config: Config) -> ExtractResult:
    """Fetch Xiaohongshu share link across PC web and mobile H5 SSR with resilient fallbacks."""
    url = url.strip()
    first_resp_url = ""
    meta_fallback: Optional[ExtractResult] = None
    saw_block = False
    saw_html = False
    last_http_error: Optional[Exception] = None
    tried: Set[Tuple[str, bool, bool, bool]] = set()

    def _run_attempt(target_url: str, headers: Dict[str, str], mobile: bool, use_proxy: bool) -> Optional[ExtractResult]:
        nonlocal first_resp_url, meta_fallback, saw_block, saw_html, last_http_error
        strip_cookie = "Cookie" in headers and headers["Cookie"] == ""
        key = (target_url, mobile, strip_cookie, use_proxy)
        if not target_url or key in tried:
            return None
        tried.add(key)

        try:
            resp = _fetch_xhs(client, target_url, headers, mobile, use_proxy)
        except HttpError as e:
            last_http_error = e
            return None

        saw_html = True
        if not first_resp_url:
            first_resp_url = resp.url or target_url
        html = resp.text()
        pref_id = _extract_note_id(resp.url or target_url) or _extract_note_id(url)

        path = (urlparse(resp.url or "").path or "").lower()
        is_error_route = "/404" in path or "/login" in path or "/website-login" in path
        if not is_error_route:
            ssr_res = _try_extract_ssr(html, pref_id, config)
            if ssr_res is not None:
                return ssr_res
            if meta_fallback is None:
                meta_fallback = _try_extract_meta(html, pref_id, config)

        if _is_blocked_xhs_response(resp.url or "", html):
            saw_block = True
        return None

    # Primary fast path: desktop PC Web SSR (highest video bitrate + author.nickname).
    res = _run_attempt(url, XHS_DL_HEADERS, mobile=False, use_proxy=True)
    if res is not None:
        return res

    recovered_url = _recover_target_url(first_resp_url, url)
    alt_url = _alt_route_url(recovered_url)
    cookie_fn = getattr(client, "cookie_for_url", None)
    has_cookie = bool(
        callable(cookie_fn)
        and (
            cookie_fn(recovered_url)
            or cookie_fn(url)
            or cookie_fn("https://www.xiaohongshu.com/")
        )
    )
    has_proxy = bool(urllib.request.getproxies())

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
        return meta_fallback

    if saw_block:
        raise ExtractionError("小红书页面被拦截或需要登录验证（若开启了代理/VPN 请尝试分流或直连，或在 config.json 配置有效 xhs_cookie 后重试）。")
    if last_http_error is not None and not saw_html:
        raise last_http_error
    raise ExtractionError("未在小红书页面中找到笔记内容，可能是链接已失效、需要登录态或触发了滑块验证。")

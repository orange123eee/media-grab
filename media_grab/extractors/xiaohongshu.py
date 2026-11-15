"""Xiaohongshu extractor: watermark-free high-res images, live photos, and highest-bitrate videos."""

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..config import Config
from ..http_client import HttpClient
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


def _find_note_obj(state: Dict[str, Any], preferred_id: str) -> Optional[Dict[str, Any]]:
    note_section = state.get("note")
    if isinstance(note_section, dict):
        nd_map = note_section.get("noteDetailMap")
        if isinstance(nd_map, dict):
            if preferred_id and isinstance(nd_map.get(preferred_id), dict):
                n = nd_map[preferred_id].get("note")
                if isinstance(n, dict) and n:
                    return n
            for val in nd_map.values():
                if isinstance(val, dict) and isinstance(val.get("note"), dict) and val["note"]:
                    return val["note"]

    note_data = state.get("noteData")
    if isinstance(note_data, dict):
        data = note_data.get("data")
        if isinstance(data, dict) and isinstance(data.get("noteData"), dict):
            return data["noteData"]
    return None


def _collect_stream_urls(stream_dict: Dict[str, Any]) -> List[str]:
    """Rank H.264/H.265/AV1 streams by resolution and bitrate descending."""
    ranked: List[tuple] = []
    codec_pref = {"h264": 2, "h265": 2, "av1": 1}

    for codec, entries in stream_dict.items():
        if not isinstance(entries, list):
            continue
        pref = codec_pref.get(str(codec).lower(), 0)
        for st in entries:
            if not isinstance(st, dict):
                continue
            pixels = int(st.get("width") or 0) * int(st.get("height") or 0)
            bitrate = int(st.get("videoBitrate") or st.get("avgBitrate") or 0)
            urls = []
            if st.get("masterUrl"):
                urls.append(normalize_http_url(st["masterUrl"]))
            for bu in st.get("backupUrls") or []:
                urls.append(normalize_http_url(bu))
            urls = [u for u in urls if u]
            if urls:
                ranked.append(((pixels, bitrate, pref), urls))

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
    if isinstance(info_list, list):
        for scene in scenes:
            for entry in info_list:
                if isinstance(entry, dict) and entry.get("imageScene") == scene and entry.get("url"):
                    raw_urls.append(normalize_http_url(entry["url"]))
        for entry in info_list:
            if isinstance(entry, dict) and entry.get("url"):
                raw_urls.append(normalize_http_url(entry["url"]))

    keys = ("urlPre", "urlDefault", "url") if prefer_preview else ("urlDefault", "urlPre", "url")
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
    """Generate candidate URLs prioritizing watermark-free original PNG/JPG before default CDN links."""
    raw_urls = _collect_raw_image_urls(img, prefer_preview=False)
    tokens = _collect_image_tokens(img, raw_urls)

    candidates: List[str] = []
    primary = str(img.get("fileId") or img.get("traceId") or "").strip()
    for token in tokens:
        candidates.append(f"https://sns-img-hw.xhscdn.com/{token}?imageView2/2/w/format/png")
        candidates.append(f"https://ci.xiaohongshu.com/{token}?imageView2/2/w/format/png")
        if token == primary:
            candidates.append(f"https://ci.xiaohongshu.com/{token}")

    for u in raw_urls:
        # Try removing watermark suffix (!nd_dft_wlteh_...) when path has no signed timestamp prefix
        if "!" in u:
            u_path = urlparse(u).path.lstrip("/")
            if not _SIGNED_PREFIX_RE.match(u_path):
                base = u.split("!", 1)[0].split("?", 1)[0]
                if "/spectrum/" not in base:
                    candidates.append(f"{base}?imageView2/2/w/format/png")

    candidates.extend(raw_urls)
    return dedupe_keep_order(candidates)


def _build_thumb_candidates(img: Dict[str, Any]) -> List[str]:
    """Generate fast watermark-free 720w WebP preview URLs for quick gallery loading."""
    raw_urls = _collect_raw_image_urls(img, prefer_preview=True)
    tokens = _collect_image_tokens(img, raw_urls)

    thumbs: List[str] = []
    for token in tokens:
        thumbs.append(f"https://sns-img-hw.xhscdn.com/{token}?imageView2/2/w/720/format/webp")
        thumbs.append(f"https://ci.xiaohongshu.com/{token}?imageView2/2/w/720/format/webp")
    thumbs.extend(raw_urls)
    return dedupe_keep_order(thumbs)


def parse_xhs_state(state: Dict[str, Any], preferred_id: str, config: Config) -> ExtractResult:
    """Parse decoded __INITIAL_STATE__ into ExtractResult."""
    note = _find_note_obj(state, preferred_id)
    if not note:
        raise ExtractionError("未在小红书页面中找到笔记内容，可能是链接已失效、需要登录态或触发了滑块验证。")

    item_id = str(note.get("noteId") or note.get("id") or preferred_id or "xhs")
    title = str(note.get("title") or note.get("desc") or "").strip()
    author = str((note.get("user") or {}).get("nickname") or "").strip()

    assets: List[MediaAsset] = []
    video_obj = note.get("video")
    if isinstance(video_obj, dict) and video_obj:
        stream = (video_obj.get("media") or {}).get("stream") or {}
        v_urls = _collect_stream_urls(stream) if isinstance(stream, dict) else []
        origin_key = (video_obj.get("consumer") or {}).get("originVideoKey")
        if origin_key:
            v_urls = dedupe_keep_order(v_urls + [f"https://sns-video-bd.xhscdn.com/{origin_key}"])
        if v_urls:
            assets.append(
                MediaAsset(
                    kind="video",
                    urls=v_urls,
                    default_ext=".mp4",
                    headers=XHS_DL_HEADERS,
                    mobile=False,
                )
            )

    # Extract images & live photos for image notes, or if video extraction yielded nothing.
    image_list = note.get("imageList")
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


def extract_xiaohongshu(url: str, client: HttpClient, config: Config) -> ExtractResult:
    """Fetch Xiaohongshu share link and extract watermark-free images/videos."""
    resp = client.get(url, headers=XHS_DL_HEADERS, mobile=False, follow_redirects=True)
    html = resp.text()

    if "/404" in resp.url or "安全限制" in html or "验证码" in html:
        raise ExtractionError("小红书页面被拦截或需要登录验证，请在 config.json 配置 xhs_cookie 后重试。")

    state = _parse_xhs_state(html)
    if not state:
        raise ExtractionError("未找到小红书笔记状态数据 (__INITIAL_STATE__)，可能触发风控或链接缺少访问参数。")

    note_id = _extract_note_id(resp.url) or _extract_note_id(url)
    return parse_xhs_state(state, note_id, config)

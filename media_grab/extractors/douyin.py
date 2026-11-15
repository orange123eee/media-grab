"""Douyin extractor: watermark-free highest-bitrate videos, image slides, and live photos."""

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from ..config import Config
from ..http_client import HttpClient
from ..utils import dedupe_keep_order, extract_balanced_json, loads_relaxed_json, normalize_http_url
from . import ExtractResult, ExtractionError, MediaAsset

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


def _clean_video_url(url: str) -> str:
    u = normalize_http_url(url)
    u = re.sub(r"([/?&=])playwm([/?&=]|$)", r"\1play\2", u)
    u = re.sub(r"([?&])watermark=1\b", r"\1watermark=0", u)
    return u


def _collect_video_urls(video_obj: Dict[str, Any]) -> List[str]:
    """Order video stream URLs from highest resolution/bitrate to fallback endpoints."""
    candidates: List[str] = []

    bit_rates = video_obj.get("bit_rate")
    if isinstance(bit_rates, list) and bit_rates:
        def _score(entry: Dict[str, Any]) -> tuple:
            pa = entry.get("play_addr") or {}
            pixels = int(pa.get("width") or 0) * int(pa.get("height") or 0)
            br = int(entry.get("bit_rate") or 0)
            return (pixels, br)

        for entry in sorted((e for e in bit_rates if isinstance(e, dict)), key=_score, reverse=True):
            pa = entry.get("play_addr") or {}
            for u in pa.get("url_list") or []:
                candidates.append(_clean_video_url(u))

    play_addr = video_obj.get("play_addr") or {}
    for u in play_addr.get("url_list") or []:
        candidates.append(_clean_video_url(u))

    uri = play_addr.get("uri")
    if uri:
        candidates.append(
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}"
            "&ratio=default&improve_bitrate=1&watermark=0&line=0"
        )

    return dedupe_keep_order(candidates)


def _rank_image_urls(urls: List[str], prefer_webp: bool = False) -> List[str]:
    """Order watermark-free URLs preferring JPG/PNG for downloads or WebP for lightweight previews."""
    cleaned = [normalize_http_url(u) for u in urls if u]

    def _priority(u: str) -> tuple:
        low = u.lower()
        has_wm = 1 if ("watermark" in low or "_wm" in low) else 0
        is_webp = 1 if ".webp" in low else 0
        fmt_rank = (0 if is_webp else 1) if prefer_webp else is_webp
        return (has_wm, fmt_rank)

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
            raw_urls = list(img.get("url_list") or []) + list(img.get("download_url_list") or [])
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
        if v_urls:
            assets.append(
                MediaAsset(
                    kind="video",
                    urls=v_urls,
                    default_ext=".mp4",
                    headers=DOUYIN_DL_HEADERS,
                    mobile=True,
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


import threading
import time

_DOUYIN_FEED_URLS = (
    "https://api5-normal-c-hl.amemv.com/aweme/v1/feed/?aweme_id={aweme_id}&aid=1128",
    "https://aweme.snssdk.com/aweme/v1/feed/?aweme_id={aweme_id}&aid=1128",
)
_TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_TTL_SECONDS = 6 * 3600
_TTWID_LOCK = threading.Lock()
_TTWID_CACHE: Dict[str, Any] = {"value": "", "expires_at": 0.0}


def _has_aweme_item(data: Any) -> bool:
    return bool(data and _find_aweme_item(data))


def _merge_cookie_header(existing_cookie: str, name: str, value: str) -> str:
    """Append name=value to a Cookie header string unless already present."""
    if not existing_cookie:
        return f"{name}={value}"
    if re.search(rf"(?:^|;\s*){re.escape(name)}=", existing_cookie):
        return existing_cookie
    return f"{existing_cookie.rstrip('; ')}; {name}={value}"


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


def _get_anonymous_ttwid(config: Config, force_refresh: bool = False) -> str:
    """Return a cached anonymous ttwid cookie value if available."""
    now = time.time()
    with _TTWID_LOCK:
        cached = str(_TTWID_CACHE.get("value") or "")
        if cached and not force_refresh and now < float(_TTWID_CACHE.get("expires_at") or 0.0):
            return cached

    val = _register_ttwid(config)
    if val:
        with _TTWID_LOCK:
            _TTWID_CACHE["value"] = val
            _TTWID_CACHE["expires_at"] = time.time() + _TTWID_TTL_SECONDS
    return val


def _fetch_aweme_from_feed(aweme_id: str, client: HttpClient) -> Optional[Dict[str, Any]]:
    """Fetch exact matching aweme item from Douyin's mobile feed API."""
    target_id = str(aweme_id)
    for template in _DOUYIN_FEED_URLS:
        feed_url = template.format(aweme_id=target_id)
        try:
            feed_resp = client.get(feed_url, mobile=True, follow_redirects=True)
            payload = json.loads(feed_resp.text())
        except Exception:
            continue
        aweme_list = payload.get("aweme_list") if isinstance(payload, dict) else None
        if not isinstance(aweme_list, list):
            continue
        for item in aweme_list:
            if isinstance(item, dict) and str(item.get("aweme_id") or "") == target_id:
                return {"aweme_detail": item}
    return None


def extract_douyin(url: str, client: HttpClient, config: Config) -> ExtractResult:
    """Fetch Douyin share page and extract highest-quality watermark-free media."""
    resp = client.get(url, mobile=True, follow_redirects=True)
    html = resp.text()
    data = _parse_douyin_json(html)

    aweme_id = _extract_aweme_id(resp.url) or _extract_aweme_id(url)
    if not _has_aweme_item(data) and aweme_id and "iesdouyin.com" not in (resp.url or "").lower():
        share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        try:
            resp2 = client.get(share_url, mobile=True, follow_redirects=True)
            html = resp2.text()
            cand = _parse_douyin_json(html)
            if _has_aweme_item(cand) or not data:
                data = cand
        except Exception:
            pass

    if not _has_aweme_item(data) and aweme_id:
        feed_data = _fetch_aweme_from_feed(aweme_id, client)
        if feed_data:
            data = feed_data

    if not _has_aweme_item(data) and aweme_id:
        base_share = resp.url.split("?", 1)[0] if "iesdouyin.com" in (resp.url or "").lower() else f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        cookie_fn = getattr(client, "cookie_for_url", None)
        existing_cookie = cookie_fn(base_share) if callable(cookie_fn) else ""
        if not re.search(r"(?:^|;\s*)ttwid=", existing_cookie):
            ttwid = _get_anonymous_ttwid(config)
            if ttwid:
                try:
                    hdrs = {"Cookie": _merge_cookie_header(existing_cookie, "ttwid", ttwid)}
                    resp3 = client.get(base_share, headers=hdrs, mobile=True, follow_redirects=True)
                    html = resp3.text()
                    cand = _parse_douyin_json(html)
                    if _has_aweme_item(cand) or not data:
                        data = cand
                except Exception:
                    pass

    if not _has_aweme_item(data):
        if any(k in html for k in ("验证码", "安全验证", "captcha", "waf")):
            raise ExtractionError("抖音触发了安全验证（滑块/风控），请在 config.json 配置 douyin_cookie 后重试。")
        if data:
            raise ExtractionError("未在页面数据中找到作品详情，可能是接口结构已调整或作品已失效。")
        raise ExtractionError("未能从抖音分享页提取作品数据，请检查链接是否有效或已设为私密。")

    return parse_douyin_payload(data, aweme_id, config)

"""Shared helpers: URL extraction, balanced JS object extraction, filename sanitization, and media sniffing."""

import json
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# Matches ASCII URL chars and stops cleanly before Chinese share copy or full-width punctuation.
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")

PRIORITY_HOSTS = (
    "douyin.com",
    "iesdouyin.com",
    "xhslink.com",
    "xiaohongshu.com",
    "x.com",
    "twitter.com",
)


def extract_url(text: str) -> Optional[str]:
    """Extract the target media URL from messy app share copy."""
    if not text:
        return None
    candidates = []
    for raw in URL_RE.findall(text.strip()):
        cleaned = raw.rstrip(".,;:!?)\"'")
        if cleaned:
            candidates.append(cleaned)
    if not candidates:
        return None

    for url in candidates:
        host = (urlparse(url).hostname or "").lower()
        if any(host == dom or host.endswith("." + dom) for dom in PRIORITY_HOSTS):
            return url
    return candidates[0]


def detect_platform(url: str) -> Optional[str]:
    """Identify supported platform from URL hostname."""
    host = (urlparse(url).hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in ("douyin.com", "iesdouyin.com")):
        return "douyin"
    if any(host == d or host.endswith("." + d) for d in ("xhslink.com", "xiaohongshu.com")):
        return "xiaohongshu"
    if any(host == d or host.endswith("." + d) for d in ("x.com", "twitter.com", "t.co")):
        return "x"
    return None


def extract_balanced_json(text: str, anchor_pattern: str) -> Optional[str]:
    """Find anchor_pattern in text and extract the balanced {...} block following it."""
    m = re.search(anchor_pattern, text)
    if not m:
        return None
    start = text.find("{", m.end() - 1 if text[m.end() - 1 : m.end()] == "{" else m.end())
    if start < 0:
        return None

    depth = 0
    in_str = ""
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = ""
            continue

        if ch in ('"', "'"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def loads_relaxed_json(raw: str) -> Any:
    """Parse JSON that may contain unquoted JS tokens like undefined, NaN, !0, !1."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if raw[j] == "\\":
                    j += 2
                    continue
                if raw[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(raw[i:j])
            i = j
            continue

        if raw.startswith("undefined", i) and _is_boundary(raw, i, 9):
            out.append("null")
            i += 9
        elif raw.startswith("NaN", i) and _is_boundary(raw, i, 3):
            out.append("null")
            i += 3
        elif raw.startswith("-Infinity", i) and _is_boundary(raw, i, 9):
            out.append("null")
            i += 9
        elif raw.startswith("Infinity", i) and _is_boundary(raw, i, 8):
            out.append("null")
            i += 8
        elif raw.startswith("!0", i) and _is_boundary(raw, i, 2):
            out.append("true")
            i += 2
        elif raw.startswith("!1", i) and _is_boundary(raw, i, 2):
            out.append("false")
            i += 2
        else:
            out.append(ch)
            i += 1

    return json.loads("".join(out))


def _is_boundary(s: str, pos: int, length: int) -> bool:
    before = s[pos - 1] if pos > 0 else " "
    after = s[pos + length] if pos + length < len(s) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def sanitize_filename(name: str, fallback: str = "media", max_len: int = 60) -> str:
    """Make a title safe for Android/FAT/POSIX filenames."""
    s = re.sub(r"[#\[\]\n\r\t]+", " ", str(name or ""))
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s*_\s*", "_", s)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" ._")
    return s or fallback


def guess_ext_from_bytes(head: bytes, content_type: str = "", default: str = ".bin") -> str:
    """Detect actual image/video file extension from magic bytes or Content-Type."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return ".gif"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"avic"):
            return ".heic"
        if brand == b"qt  ":
            return ".mov"
        return ".mp4"

    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
    }
    return mapping.get(ct, default)


def normalize_http_url(url: str) -> str:
    """Ensure protocol-relative URLs use https."""
    u = str(url or "").strip()
    if u.startswith("//"):
        return "https:" + u
    return u


def dedupe_keep_order(items: list[str]) -> list[str]:
    """Deduplicate non-empty strings while preserving order."""
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out

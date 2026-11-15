"""Pure stdlib HTTP client with gzip/deflate decoding, host-scoped cookies, and validated streaming."""

import gzip
import ssl
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

from .config import Config
from .utils import guess_ext_from_bytes

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class HttpError(RuntimeError):
    """Raised when an HTTP request fails or returns unexpected content."""


@dataclass
class HttpResponse:
    status: int
    url: str
    headers: Dict[str, str]
    body: bytes

    def text(self, default_encoding: str = "utf-8") -> str:
        ct = self.headers.get("content-type", "")
        enc = default_encoding
        for part in ct.split(";"):
            part = part.strip()
            if part.lower().startswith("charset="):
                enc = part.split("=", 1)[1].strip().strip("\"'") or default_encoding
        return self.body.decode(enc, errors="replace")


@dataclass
class MediaStream:
    """Validated open HTTP stream for a media asset."""

    resp: Any
    first_chunk: bytes
    ext: str
    content_type: str
    status: int
    content_length: str
    content_range: str
    accept_ranges: str
    winning_url: str

    def close(self) -> None:
        try:
            self.resp.close()
        except Exception:
            pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decompress(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower().strip()
    if enc == "gzip":
        return gzip.decompress(raw)
    if enc == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


class HttpClient:
    """Lightweight HTTP helper backed solely by Python standard library."""

    def __init__(self, config: Config):
        self.config = config
        self._ssl_ctx = ssl.create_default_context()

    def cookie_for_url(self, url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return ""
        if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
            if self.config.douyin_cookie:
                return self.config.douyin_cookie
        if host.endswith("xiaohongshu.com") or host.endswith("xhslink.com") or host.endswith("xhslink.cn"):
            if self.config.xhs_cookie:
                return self.config.xhs_cookie

        matched = []
        for dom, pair in self.config.cookies_by_domain.items():
            if host == dom or host.endswith("." + dom):
                matched.append(pair)
        return "; ".join(matched)

    def _build_headers(self, url: str, headers: Optional[Dict[str, str]], mobile: bool) -> Dict[str, str]:
        merged = {
            "User-Agent": MOBILE_UA if mobile else DESKTOP_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        cookie = self.cookie_for_url(url)
        if cookie:
            merged["Cookie"] = cookie
        if headers:
            merged.update(headers)
        return merged

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        mobile: bool = False,
        follow_redirects: bool = True,
        timeout: Optional[int] = None,
    ) -> HttpResponse:
        req = urllib.request.Request(url, headers=self._build_headers(url, headers, mobile), method="GET")
        handlers = [urllib.request.HTTPSHandler(context=self._ssl_ctx)]
        if not follow_redirects:
            handlers.append(_NoRedirect())
        opener = urllib.request.build_opener(*handlers)
        req_timeout = self.config.timeout if timeout is None else max(1, int(timeout))

        try:
            with opener.open(req, timeout=req_timeout) as resp:
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                body = _decompress(resp.read(), hdrs.get("content-encoding", ""))
                return HttpResponse(status=resp.status, url=resp.geturl(), headers=hdrs, body=body)
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            if not follow_redirects and e.code in (301, 302, 303, 307, 308):
                return HttpResponse(status=e.code, url=e.geturl(), headers=hdrs, body=b"")
            raise HttpError(f"HTTP {e.code}: {url}") from e
        except urllib.error.URLError as e:
            raise HttpError(f"网络请求失败: {e.reason}") from e

    def open_media_candidates(
        self,
        urls: Iterable[str],
        default_ext: str,
        expected_prefix: str,
        headers: Optional[Dict[str, str]] = None,
        mobile: bool = False,
        range_header: Optional[str] = None,
    ) -> MediaStream:
        """Open first candidate URL returning valid media bytes (rejects HTML/JSON error pages)."""
        cand_list = [c for c in urls if c]
        last_err: Optional[Exception] = None
        for idx, cand in enumerate(cand_list):
            is_last = idx == len(cand_list) - 1
            cand_timeout = self.config.timeout if is_last else min(self.config.timeout, 6)
            try:
                return self._open_one(
                    cand,
                    default_ext=default_ext,
                    expected_prefix=expected_prefix,
                    headers=headers,
                    mobile=mobile,
                    range_header=range_header,
                    timeout=cand_timeout,
                )
            except urllib.error.HTTPError as e:
                if e.code == 403 and headers and any(k.lower() == "referer" for k in headers):
                    stripped = {k: v for k, v in headers.items() if k.lower() != "referer"}
                    try:
                        return self._open_one(
                            cand,
                            default_ext=default_ext,
                            expected_prefix=expected_prefix,
                            headers=stripped,
                            mobile=mobile,
                            range_header=range_header,
                            timeout=cand_timeout,
                        )
                    except Exception as retry_err:
                        last_err = retry_err
                        continue
                last_err = e
                continue
            except Exception as e:
                last_err = e
                continue
        raise HttpError(f"所有候选地址均不可用: {last_err}")

    def _open_one(
        self,
        url: str,
        default_ext: str,
        expected_prefix: str,
        headers: Optional[Dict[str, str]] = None,
        mobile: bool = False,
        range_header: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> MediaStream:
        req_headers = self._build_headers(url, headers, mobile)
        req_headers["Accept-Encoding"] = "identity"
        if range_header:
            req_headers["Range"] = range_header

        req = urllib.request.Request(url, headers=req_headers, method="GET")
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self._ssl_ctx))
        resp = opener.open(req, timeout=self.config.timeout if timeout is None else max(1, int(timeout)))
        try:
            status = int(getattr(resp, "status", 200) or 200)
            ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct.startswith("text/html") or ct.startswith("application/json"):
                raise HttpError(f"响应类型异常 ({ct})，可能触发了防盗链或风控")

            first_chunk = resp.read(8192)
            if not first_chunk:
                raise HttpError("响应内容为空")

            # Mid-stream 206 byte ranges do not start with file magic headers.
            content_range = resp.headers.get("Content-Range") or ""
            is_start = (status != 206) or content_range.startswith("bytes 0-")
            ext = default_ext
            if is_start:
                head = first_chunk[:32]
                if head.lstrip().startswith((b"<html", b"<!DOCTYPE", b"<!doctype", b"{")):
                    raise HttpError("返回了网页或 JSON 而非媒体文件")
                ext = guess_ext_from_bytes(head, ct, default=default_ext)
                if expected_prefix == "image" and ext in (".mp4", ".mov"):
                    raise HttpError("预期图片但返回了视频流")
                if expected_prefix == "video" and ext not in (".mp4", ".mov"):
                    raise HttpError(f"预期视频但收到 {ext}")

            if not ct:
                ct = "video/mp4" if ext in (".mp4", ".mov") else f"image/{ext.lstrip('.')}"

            return MediaStream(
                resp=resp,
                first_chunk=first_chunk,
                ext=ext,
                content_type=ct,
                status=status,
                content_length=resp.headers.get("Content-Length") or "",
                content_range=content_range,
                accept_ranges=resp.headers.get("Accept-Ranges") or "",
                winning_url=url,
            )
        except Exception:
            try:
                resp.close()
            except Exception:
                pass
            raise

    def download_candidates(
        self,
        urls: Iterable[str],
        dest_dir: Path,
        stem: str,
        default_ext: str,
        expected_prefix: str,
        headers: Optional[Dict[str, str]] = None,
        mobile: bool = False,
    ) -> Path:
        """Try candidate URLs in priority order and save atomically once valid media bytes arrive."""
        stream = self.open_media_candidates(
            urls,
            default_ext=default_ext,
            expected_prefix=expected_prefix,
            headers=headers,
            mobile=mobile,
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = dest_dir / f"{stem}{stream.ext}"
        if final_path.exists() and final_path.stat().st_size > 1024:
            stream.close()
            return final_path

        part_path = dest_dir / f".{stem}.part"
        try:
            with open(part_path, "wb") as fh:
                fh.write(stream.first_chunk)
                while True:
                    chunk = stream.resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
            part_path.replace(final_path)
            return final_path
        except Exception:
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            stream.close()

    def fetch_media_bytes(
        self,
        urls: Iterable[str],
        default_ext: str,
        expected_prefix: str,
        headers: Optional[Dict[str, str]] = None,
        mobile: bool = False,
    ) -> Tuple[bytes, str]:
        """Download a validated media asset into memory (used for ZIP bundling)."""
        stream = self.open_media_candidates(
            urls,
            default_ext=default_ext,
            expected_prefix=expected_prefix,
            headers=headers,
            mobile=mobile,
        )
        chunks = [stream.first_chunk]
        try:
            while True:
                chunk = stream.resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks), stream.ext
        finally:
            stream.close()

"""Pure stdlib HTTP client with Keep-Alive connection pooling, gzip/deflate, host cookies, and stream validation."""

import gzip
import http.client
import select
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
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


def _is_socket_idle_alive(conn: http.client.HTTPConnection) -> bool:
    """Return True when an idle HTTP/HTTPS connection socket is open with no unread EOF or stray bytes."""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    if getattr(conn, "_HTTPConnection__state", None) != http.client._CS_IDLE:
        return False
    resp_ref = getattr(conn, "_HTTPConnection__response", None)
    if resp_ref is not None and not resp_ref.isclosed():
        return False
    try:
        if hasattr(sock, "pending") and sock.pending() > 0:
            return False
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(sock, select.POLLIN | select.POLLERR | select.POLLHUP | select.POLLNVAL)
            return not poller.poll(0)
        rlist, _, xlist = select.select([sock], [], [sock], 0)
        return not rlist and not xlist
    except (OSError, ValueError):
        return False


class _PooledHTTPResponse(http.client.HTTPResponse):
    """HTTPResponse subclass that returns clean Keep-Alive connections to the pool at EOF."""

    _pool_release_cb: Optional[Callable[[bool], None]] = None
    _closing_dirty: bool = False

    def _release_to_pool(self, clean_eof: bool) -> None:
        cb = self._pool_release_cb
        if cb is not None:
            self._pool_release_cb = None
            cb(clean_eof)

    def _close_conn(self) -> None:
        try:
            super()._close_conn()
        finally:
            self._release_to_pool(not self._closing_dirty)

    def close(self) -> None:
        # Unconsumed response bodies leave bytes on the wire; never reuse the socket if truncated.
        if self.fp is not None and not (self.length == 0 and not self.chunked):
            self._closing_dirty = True
        try:
            super().close()
        finally:
            self._release_to_pool(not self._closing_dirty)


class _PooledHTTPConnection(http.client.HTTPConnection):
    response_class = _PooledHTTPResponse


class _PooledHTTPSConnection(http.client.HTTPSConnection):
    response_class = _PooledHTTPResponse


class ConnectionPool:
    """Thread-safe bounded HTTP/1.1 Keep-Alive connection pool."""

    def __init__(self, max_per_key: int = 6, max_total: int = 24, idle_ttl: float = 45.0):
        self.max_per_key = max_per_key
        self.max_total = max_total
        self.idle_ttl = idle_ttl
        self._lock = threading.Lock()
        self._buckets: Dict[tuple, List[Tuple[http.client.HTTPConnection, float]]] = {}

    @staticmethod
    def _close_quiet(conn: http.client.HTTPConnection) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def _evict_expired_locked(self, now: float) -> List[http.client.HTTPConnection]:
        to_close: List[http.client.HTTPConnection] = []
        empty_keys: List[tuple] = []
        for k, items in self._buckets.items():
            alive: List[Tuple[http.client.HTTPConnection, float]] = []
            for conn, ts in items:
                if (now - ts) > self.idle_ttl:
                    to_close.append(conn)
                else:
                    alive.append((conn, ts))
            if alive:
                self._buckets[k] = alive
            else:
                empty_keys.append(k)
        for k in empty_keys:
            self._buckets.pop(k, None)
        return to_close

    def _total_count_locked(self) -> int:
        return sum(len(v) for v in self._buckets.values())

    def has_idle(self, key: tuple) -> bool:
        now = time.monotonic()
        with self._lock:
            items = self._buckets.get(key)
            if not items:
                return False
            return any((now - ts) <= self.idle_ttl for _, ts in items)

    def acquire(
        self,
        key: tuple,
        factory: Callable[[], http.client.HTTPConnection],
        timeout: Optional[float],
    ) -> Tuple[http.client.HTTPConnection, bool]:
        """Pop a live idle connection for key, or construct a fresh disconnected one."""
        now = time.monotonic()
        candidates: List[http.client.HTTPConnection] = []
        stale_to_close: List[http.client.HTTPConnection] = []

        with self._lock:
            stale_to_close.extend(self._evict_expired_locked(now))
            items = self._buckets.get(key)
            while items:
                conn, _ = items.pop()
                candidates.append(conn)
            if not items:
                self._buckets.pop(key, None)

        chosen: Optional[http.client.HTTPConnection] = None
        for idx, conn in enumerate(candidates):
            if _is_socket_idle_alive(conn):
                chosen = conn
                stale_to_close.extend(candidates[idx + 1 :])
                break
            stale_to_close.append(conn)

        for conn in stale_to_close:
            self._close_quiet(conn)

        if chosen is not None:
            chosen.timeout = timeout
            try:
                if chosen.sock is not None:
                    chosen.sock.settimeout(timeout)
                return chosen, True
            except OSError:
                self._close_quiet(chosen)

        return factory(), False

    def release(self, key: tuple, conn: http.client.HTTPConnection) -> None:
        """Return a clean idle connection to the pool or close it if full/unhealthy."""
        if not _is_socket_idle_alive(conn):
            self._close_quiet(conn)
            return

        now = time.monotonic()
        to_close: List[http.client.HTTPConnection] = []
        with self._lock:
            to_close.extend(self._evict_expired_locked(now))
            bucket = self._buckets.setdefault(key, [])
            while len(bucket) >= self.max_per_key:
                old_conn, _ = bucket.pop(0)
                to_close.append(old_conn)

            while self._total_count_locked() >= self.max_total:
                oldest_k: Optional[tuple] = None
                oldest_ts = float("inf")
                for k, items in self._buckets.items():
                    if items and items[0][1] < oldest_ts:
                        oldest_ts = items[0][1]
                        oldest_k = k
                if oldest_k is None:
                    break
                old_conn, _ = self._buckets[oldest_k].pop(0)
                if not self._buckets[oldest_k]:
                    self._buckets.pop(oldest_k, None)
                to_close.append(old_conn)

            self._buckets.setdefault(key, []).append((conn, now))

        for old_conn in to_close:
            self._close_quiet(old_conn)

    def close_all(self) -> None:
        with self._lock:
            conns = [conn for items in self._buckets.values() for conn, _ in items]
            self._buckets.clear()
        for conn in conns:
            self._close_quiet(conn)


_DEFAULT_SSL_CTX = ssl.create_default_context()
_DEFAULT_POOL = ConnectionPool(max_per_key=8, max_total=32, idle_ttl=45.0)


def close_idle_connections() -> None:
    """Close all idle Keep-Alive upstream sockets held in the default pool."""
    _DEFAULT_POOL.close_all()


def _make_conn(
    http_class: type,
    host: str,
    timeout: Optional[float],
    tunnel_host: Optional[str],
    tunnel_headers: Dict[str, str],
    http_conn_args: Dict[str, Any],
) -> http.client.HTTPConnection:
    conn = http_class(host, timeout=timeout, **http_conn_args)
    if tunnel_host:
        conn.set_tunnel(tunnel_host, headers=tunnel_headers)
    return conn


def _do_open_pooled(
    pool: ConnectionPool,
    scheme: str,
    http_class: type,
    req: urllib.request.Request,
    debuglevel: int = 0,
    **http_conn_args: Any,
) -> http.client.HTTPResponse:
    host = req.host
    if not host:
        raise urllib.error.URLError("no host given")

    headers = dict(req.unredirected_hdrs)
    headers.update({k: v for k, v in req.headers.items() if k not in headers})
    if not any(k.lower() == "connection" for k in headers):
        headers["Connection"] = "keep-alive"
    headers = {name.title(): val for name, val in headers.items()}

    tunnel_headers: Dict[str, str] = {}
    tunnel_host = getattr(req, "_tunnel_host", None)
    if tunnel_host and "Proxy-Authorization" in headers:
        tunnel_headers["Proxy-Authorization"] = headers.pop("Proxy-Authorization")

    ctx = http_conn_args.get("context")
    key = (scheme, host, tunnel_host or "", ctx, tunnel_headers.get("Proxy-Authorization", ""))
    timeout = req.timeout
    can_retry_stale = (req.data is None) or isinstance(req.data, (bytes, bytearray))
    encoded_chunked = req.has_header("Transfer-encoding")

    last_resp: Optional[http.client.HTTPResponse] = None
    for attempt in range(2):
        conn, from_pool = pool.acquire(
            key,
            factory=lambda: _make_conn(http_class, host, timeout, tunnel_host, tunnel_headers, http_conn_args),
            timeout=timeout,
        )
        conn.set_debuglevel(debuglevel)
        try:
            conn.request(
                req.get_method(),
                req.selector,
                req.data,
                headers,
                encode_chunked=encoded_chunked,
            )
            last_resp = conn.getresponse()
        except Exception as err:
            try:
                conn.close()
            except Exception:
                pass
            if from_pool and attempt == 0 and can_retry_stale:
                continue
            if isinstance(err, urllib.error.URLError):
                raise
            if isinstance(err, OSError):
                raise urllib.error.URLError(err) from err
            raise

        if not last_resp.will_close and isinstance(last_resp, _PooledHTTPResponse):
            conn_ref = conn

            def _on_done(clean_eof: bool, c=conn_ref, k=key) -> None:
                if clean_eof:
                    pool.release(k, c)
                else:
                    try:
                        c.close()
                    except Exception:
                        pass

            last_resp._pool_release_cb = _on_done

        last_resp.url = req.get_full_url()
        last_resp.msg = last_resp.reason
        return last_resp

    raise urllib.error.URLError("connection pool retry exhausted")


class _PooledHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pool: ConnectionPool, debuglevel: int = 0):
        super().__init__(debuglevel=debuglevel)
        self._pool = pool

    def http_open(self, req):
        return _do_open_pooled(self._pool, "http", _PooledHTTPConnection, req, debuglevel=self._debuglevel)


class _PooledHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pool: ConnectionPool, debuglevel: Optional[int] = None, context=None, check_hostname=None):
        super().__init__(debuglevel=debuglevel, context=context, check_hostname=check_hostname)
        self._pool = pool

    def https_open(self, req):
        return _do_open_pooled(
            self._pool,
            "https",
            _PooledHTTPSConnection,
            req,
            debuglevel=self._debuglevel,
            context=self._context,
        )


def _warm_single_https(
    url: str,
    pool: ConnectionPool,
    ssl_context: ssl.SSLContext,
    timeout: float,
) -> None:
    try:
        req = urllib.request.Request(url, method="HEAD")
        ph = urllib.request.ProxyHandler()
        if hasattr(ph, "https_open"):
            try:
                ph.https_open(req)
            except AttributeError:
                pass
        host = req.host
        if not host:
            return
        tunnel_host = getattr(req, "_tunnel_host", None)
        tunnel_headers: Dict[str, str] = {}
        for k, v in req.headers.items():
            if k.lower() == "proxy-authorization":
                tunnel_headers["Proxy-Authorization"] = v
        key = ("https", host, tunnel_host or "", ssl_context, tunnel_headers.get("Proxy-Authorization", ""))
        if pool.has_idle(key):
            return
        conn = _make_conn(
            _PooledHTTPSConnection,
            host,
            timeout,
            tunnel_host,
            tunnel_headers,
            {"context": ssl_context},
        )
        try:
            conn.connect()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return
        pool.release(key, conn)
    except Exception:
        pass


def warm_urls(
    urls: Sequence[str],
    timeout: float = 3.0,
    pool: Optional[ConnectionPool] = None,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> None:
    """Best-effort parallel TCP+TLS preconnect into the Keep-Alive pool (zero HTTP request overhead)."""
    target_pool = pool if pool is not None else _DEFAULT_POOL
    target_ctx = ssl_context if ssl_context is not None else _DEFAULT_SSL_CTX
    for u in urls:
        if u and u.startswith("https://"):
            threading.Thread(
                target=_warm_single_https,
                args=(u, target_pool, target_ctx, timeout),
                daemon=True,
            ).start()


class HttpClient:
    """Lightweight pooled HTTP helper backed solely by Python standard library."""

    def __init__(
        self,
        config: Config,
        pool: Optional[ConnectionPool] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        self.config = config
        self._ssl_ctx = ssl_context if ssl_context is not None else _DEFAULT_SSL_CTX
        self._pool = pool if pool is not None else _DEFAULT_POOL

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
        if not merged.get("Cookie"):
            merged.pop("Cookie", None)
        return merged

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        mobile: bool = False,
        follow_redirects: bool = True,
        timeout: Optional[int] = None,
        use_proxy: bool = True,
    ) -> HttpResponse:
        req = urllib.request.Request(url, headers=self._build_headers(url, headers, mobile), method="GET")
        handlers: List[Any] = [
            _PooledHTTPHandler(self._pool),
            _PooledHTTPSHandler(self._pool, context=self._ssl_ctx),
        ]
        if not use_proxy:
            handlers.insert(0, urllib.request.ProxyHandler({}))
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
            code = e.code
            final_url = e.geturl()
            try:
                e.close()
            except Exception:
                pass
            if not follow_redirects and code in (301, 302, 303, 307, 308):
                return HttpResponse(status=code, url=final_url, headers=hdrs, body=b"")
            raise HttpError(f"HTTP {code}: {url}") from e
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
                try:
                    e.close()
                except Exception:
                    pass
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
        opener = urllib.request.build_opener(
            _PooledHTTPHandler(self._pool),
            _PooledHTTPSHandler(self._pool, context=self._ssl_ctx),
        )
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

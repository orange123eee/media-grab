"""Mobile-first web UI & tokenized media proxy server (pure Python stdlib)."""

import argparse
import gc
import io
import json
import os
import secrets
import socket
import threading
import time
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlparse

from . import android
from .config import DEFAULT_CONFIG_DIR, Config, load_config
from .extractors import ExtractResult, ExtractionError, MediaAsset, extract
from .http_client import HttpClient, HttpError, close_idle_connections
from .utils import dedupe_keep_order, extract_url, sanitize_filename

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _load_static_bytes(filename: str) -> bytes:
    """Load a static web UI asset verbatim as bytes."""
    return (_STATIC_DIR / filename).read_bytes()


APP_ICON_SVG_BYTES = _load_static_bytes("icon.svg")
MANIFEST_JSON_BYTES = _load_static_bytes("manifest.json")
SW_JS_BYTES = _load_static_bytes("sw.js")
INDEX_HTML_BYTES = _load_static_bytes("index.html")


_MAX_THUMB_CACHE_BYTES = 768 * 1024


@dataclass
class StoredAsset:
    asset_id: str
    index: int
    pair_index: int
    stem: str
    asset: MediaAsset
    platform: str = ""
    resolved_url: Optional[str] = None
    resolved_ext: Optional[str] = None
    resolved_thumb_url: Optional[str] = None
    thumb_bytes: Optional[bytes] = None
    thumb_content_type: Optional[str] = None
    thumb_ext: Optional[str] = None


@dataclass
class StoredJob:
    job_id: str
    result: ExtractResult
    assets: List[StoredAsset] = field(default_factory=list)


class JobStore:
    """Thread-safe bounded in-memory registry of parsed media tokens (blocks open SSRF)."""

    def __init__(self, max_jobs: int = 30):
        self.max_jobs = max_jobs
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, StoredJob] = OrderedDict()
        self._assets: Dict[str, StoredAsset] = {}
        self._preferred_hosts: Dict[Tuple[str, bool], str] = {}
        self._thumb_inflight: Dict[str, threading.Event] = {}

    @staticmethod
    def _prioritize_urls(urls: List[str], exact: Optional[str], pref_host: Optional[str]) -> List[str]:
        ordered: List[str] = []
        if exact:
            ordered.append(exact)
        if pref_host:
            for u in urls:
                if (urlparse(u).hostname or "").lower() == pref_host:
                    ordered.append(u)
        ordered.extend(urls)
        return dedupe_keep_order(ordered)

    def put_result(self, result: ExtractResult) -> StoredJob:
        job_id = secrets.token_urlsafe(9)
        total = len(result.assets)
        safe_title = sanitize_filename(result.title, fallback=result.platform, max_len=42)
        short_id = sanitize_filename(result.item_id, fallback="id", max_len=16)

        stored_assets: List[StoredAsset] = []
        last_image_idx = 0
        for idx, asset in enumerate(result.assets, start=1):
            if asset.kind == "image":
                last_image_idx = idx
            pair_idx = last_image_idx if asset.kind == "live_video" else idx
            suffix = f"_{idx:02d}" if total > 1 else ""
            if asset.kind == "live_video":
                suffix += "_live"
            stem = f"{result.platform}_{safe_title}_{short_id}{suffix}"
            stored_assets.append(
                StoredAsset(
                    asset_id=secrets.token_urlsafe(10),
                    index=idx,
                    pair_index=pair_idx,
                    stem=stem,
                    asset=asset,
                    platform=result.platform,
                )
            )

        job = StoredJob(job_id=job_id, result=result, assets=stored_assets)
        with self._lock:
            self._jobs[job_id] = job
            for sa in stored_assets:
                self._assets[sa.asset_id] = sa
            while len(self._jobs) > self.max_jobs:
                _, evicted = self._jobs.popitem(last=False)
                for sa in evicted.assets:
                    sa.thumb_bytes = None
                    self._assets.pop(sa.asset_id, None)
        return job

    def get_job(self, job_id: str) -> Optional[StoredJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_asset(self, asset_id: str) -> Optional[StoredAsset]:
        with self._lock:
            return self._assets.get(asset_id)

    def candidate_urls_for(self, sa: StoredAsset) -> List[str]:
        with self._lock:
            exact = sa.resolved_url
        return self._prioritize_urls(list(sa.asset.urls), exact, None)

    def thumb_urls_for(self, sa: StoredAsset) -> List[str]:
        if not sa.asset.thumb_urls:
            return self.candidate_urls_for(sa)
        if sa.asset.kind in ("video", "live_video"):
            base = list(sa.asset.thumb_urls)
        else:
            base = list(sa.asset.thumb_urls) + list(sa.asset.urls)
        with self._lock:
            exact = sa.resolved_thumb_url
            pref = self._preferred_hosts.get((sa.platform, True))
        return self._prioritize_urls(base, exact, pref)

    def remember_winning(self, sa: StoredAsset, url: str, ext: str, is_thumb: bool = False) -> None:
        host = (urlparse(url).hostname or "").lower()
        with self._lock:
            if host and sa.platform and is_thumb:
                self._preferred_hosts[(sa.platform, True)] = host
            if is_thumb:
                sa.resolved_thumb_url = url
            else:
                sa.resolved_url = url
                sa.resolved_ext = ext

    def get_cached_thumb(self, sa: StoredAsset) -> Optional[Tuple[bytes, str, str]]:
        with self._lock:
            if sa.thumb_bytes is None:
                return None
            return sa.thumb_bytes, (sa.thumb_content_type or "image/jpeg"), (sa.thumb_ext or ".jpg")

    def cache_thumb(self, sa: StoredAsset, data: bytes, content_type: str, ext: str) -> None:
        if not data or len(data) > _MAX_THUMB_CACHE_BYTES:
            return
        with self._lock:
            if sa.asset_id not in self._assets:
                return
            sa.thumb_bytes = data
            sa.thumb_content_type = content_type
            sa.thumb_ext = ext

    def acquire_thumb_slot(self, asset_id: str) -> Tuple[bool, threading.Event]:
        """Deduplicate concurrent upstream thumbnail fetches for the same asset."""
        with self._lock:
            ev = self._thumb_inflight.get(asset_id)
            if ev is not None:
                return False, ev
            ev = threading.Event()
            self._thumb_inflight[asset_id] = ev
            return True, ev

    def release_thumb_slot(self, asset_id: str, ev: threading.Event) -> None:
        with self._lock:
            if self._thumb_inflight.get(asset_id) is ev:
                self._thumb_inflight.pop(asset_id, None)
        ev.set()

    def clear(self) -> None:
        with self._lock:
            for sa in self._assets.values():
                sa.thumb_bytes = None
            self._jobs.clear()
            self._assets.clear()
            self._preferred_hosts.clear()
            for ev in self._thumb_inflight.values():
                ev.set()
            self._thumb_inflight.clear()


def _filter_job_assets(job: StoredJob, selected_ids: Optional[Sequence[str]]) -> List[StoredAsset]:
    """Filter job assets to requested IDs, or return all when selected_ids is None."""
    if selected_ids is None:
        return list(job.assets)
    wanted = {str(x).strip() for x in selected_ids if str(x).strip()}
    return [sa for sa in job.assets if sa.asset_id in wanted]


def _build_zip_bytes(
    job: StoredJob,
    store: JobStore,
    client: HttpClient,
    config: Config,
    target_assets: Optional[List[StoredAsset]] = None,
) -> Tuple[str, bytes]:
    assets = list(job.assets) if target_assets is None else list(target_assets)
    buf = io.BytesIO()
    files: List[Tuple[int, str, bytes]] = []

    def _fetch(sa: StoredAsset) -> Tuple[int, str, bytes]:
        expected = "video" if sa.asset.kind in ("video", "live_video") else "image"
        data, ext = client.fetch_media_bytes(
            store.candidate_urls_for(sa),
            default_ext=sa.asset.default_ext,
            expected_prefix=expected,
            headers=sa.asset.headers,
            mobile=sa.asset.mobile,
        )
        return sa.index, f"{sa.stem}{ext}", data

    workers = min(config.max_workers, max(1, len(assets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_fetch, sa) for sa in assets]
        for fut in as_completed(futs):
            files.append(fut.result())

    files.sort(key=lambda x: x[0])
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for _, fname, data in files:
            zf.writestr(fname, data)

    title = sanitize_filename(job.result.title, fallback=job.result.platform, max_len=36)
    suffix = f"_sel{len(assets)}" if target_assets is not None and len(assets) < len(job.assets) else ""
    zip_name = f"{job.result.platform}_{title}_{job.result.item_id}{suffix}.zip"
    return zip_name, buf.getvalue()


def _save_web_config(updates: Dict[str, Any], config_dir: Optional[Path] = None) -> Config:
    cfg = load_config(config_dir)
    cfg_dir = config_dir or Path(os.environ.get("MEDIA_GRAB_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    cfg_file = cfg_dir / "config.json"
    raw = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
    for key in ("douyin_cookie", "xhs_cookie"):
        if key in updates:
            raw[key] = str(updates[key] or "").strip()
    if "save_live_photo_video" in updates:
        raw["save_live_photo_video"] = bool(updates["save_live_photo_video"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_config(cfg_dir)


def _save_job_to_output(
    job: StoredJob,
    store: JobStore,
    client: HttpClient,
    config: Config,
    target_assets: Optional[List[StoredAsset]] = None,
) -> Tuple[List[Path], List[str]]:
    assets = list(job.assets) if target_assets is None else list(target_assets)
    saved: List[Path] = []
    errors: List[str] = []

    def _dl(sa: StoredAsset) -> Path:
        expected = "video" if sa.asset.kind in ("video", "live_video") else "image"
        return client.download_candidates(
            store.candidate_urls_for(sa),
            dest_dir=config.output_dir,
            stem=sa.stem,
            default_ext=sa.asset.default_ext,
            expected_prefix=expected,
            headers=sa.asset.headers,
            mobile=sa.asset.mobile,
        )

    workers = min(config.max_workers, max(1, len(assets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_dl, sa): sa for sa in assets}
        for fut in as_completed(futs):
            sa = futs[fut]
            try:
                saved.append(fut.result())
            except Exception as e:
                errors.append(f"第{sa.index}项: {e}")

    if saved:
        android.scan_media(saved, wait=False)
    return saved, errors


def _prewarm_extractors(config_dir: Optional[Path] = None) -> None:
    cfg = load_config(config_dir)
    try:
        from .extractors.x import prewarm as _prewarm_x

        _prewarm_x(cfg)
    except Exception:
        pass
    try:
        from .extractors.douyin import prewarm as _prewarm_douyin

        _prewarm_douyin(cfg)
    except Exception:
        pass
    try:
        from .extractors.xiaohongshu import prewarm as _prewarm_xhs

        _prewarm_xhs(cfg)
    except Exception:
        pass


def _prefetch_job_thumbs(job: StoredJob, state: "WebAppState", max_assets: int = 12) -> None:
    """Warm lightweight thumbnails into JobStore RAM right after extract completes."""
    targets = [sa for sa in job.assets if sa.asset.thumb_urls][:max_assets]
    if not targets:
        return
    _, client = state.client()

    def _warm_one(sa: StoredAsset) -> None:
        if state.is_hibernated() or state.store.get_asset(sa.asset_id) is None:
            return
        if state.store.get_cached_thumb(sa) is not None:
            return
        owns_slot, slot_ev = state.store.acquire_thumb_slot(sa.asset_id)
        if not owns_slot:
            return
        try:
            if state.store.get_cached_thumb(sa) is not None:
                return
            stream = client.open_media_candidates(
                state.store.thumb_urls_for(sa),
                default_ext=sa.asset.default_ext,
                expected_prefix="image",
                headers=sa.asset.headers,
                mobile=sa.asset.mobile,
            )
            try:
                state.store.remember_winning(sa, stream.winning_url, stream.ext, is_thumb=True)
                try:
                    cl_val = int(stream.content_length) if stream.content_length else 0
                except ValueError:
                    cl_val = 0
                if cl_val > _MAX_THUMB_CACHE_BYTES or len(stream.first_chunk) > _MAX_THUMB_CACHE_BYTES:
                    return
                buf = bytearray(stream.first_chunk)
                while True:
                    chunk = stream.resp.read(65536)
                    if not chunk:
                        break
                    if len(buf) + len(chunk) > _MAX_THUMB_CACHE_BYTES:
                        return
                    buf.extend(chunk)
                if buf:
                    state.store.cache_thumb(sa, bytes(buf), stream.content_type, stream.ext)
            finally:
                stream.close()
        except Exception:
            pass
        finally:
            state.store.release_thumb_slot(sa.asset_id, slot_ev)

    workers = min(4, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_warm_one, sa) for sa in targets]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass


class WebAppState:
    """Shared server state holding job registry, config path, and activity lifecycle."""

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        can_shutdown: bool = False,
        hibernate_only: bool = False,
        prewarm_on_wake: bool = False,
        prefetch_thumbs: bool = False,
    ):
        self.config_dir = config_dir
        self.can_shutdown = can_shutdown
        self.hibernate_only = hibernate_only
        self.prewarm_on_wake = prewarm_on_wake
        self.prefetch_thumbs = prefetch_thumbs
        self.hibernated = False
        self.store = JobStore()
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self.active_requests = 0
        self.leave_deadline: Optional[float] = None
        self.last_activity = time.monotonic()
        self._last_prewarm = 0.0
        self.server: Optional[ThreadingHTTPServer] = None

    def _maybe_prewarm_on_wake(self) -> None:
        if not self.prewarm_on_wake:
            return
        now = time.monotonic()
        with self._lock:
            if (now - self._last_prewarm) < 30.0:
                return
            self._last_prewarm = now
        threading.Thread(target=_prewarm_extractors, args=(self.config_dir,), daemon=True).start()

    def touch(self) -> None:
        woke = False
        with self._lock:
            self.last_activity = time.monotonic()
            self.leave_deadline = None
            if self.hibernated:
                woke = True
                self.hibernated = False
            self._wake_event.set()
        if woke:
            self._maybe_prewarm_on_wake()

    def mark_leave(self, delay_sec: float) -> None:
        with self._lock:
            self.leave_deadline = time.monotonic() + max(0.1, delay_sec)
            self._wake_event.set()

    def hibernate(self) -> None:
        with self._lock:
            if self.active_requests > 0:
                return
            self.store.clear()
            self.leave_deadline = None
            self.hibernated = True
            self._wake_event.clear()
        close_idle_connections()
        gc.collect()

    @contextmanager
    def busy(self):
        with self._lock:
            self.active_requests += 1
            self.last_activity = time.monotonic()
            self.leave_deadline = None
            self.hibernated = False
            self._wake_event.set()
        try:
            yield
        finally:
            with self._lock:
                self.active_requests = max(0, self.active_requests - 1)
                self.last_activity = time.monotonic()

    def is_hibernated(self) -> bool:
        with self._lock:
            return self.hibernated

    def should_shutdown(self, idle_timeout: int) -> bool:
        with self._lock:
            if self.active_requests > 0:
                return False
            now = time.monotonic()
            if self.leave_deadline is not None and now >= self.leave_deadline:
                return True
            if idle_timeout > 0 and (now - self.last_activity) > idle_timeout:
                return True
            return False

    def client(self) -> Tuple[Config, HttpClient]:
        cfg = load_config(self.config_dir)
        return cfg, HttpClient(cfg)


def make_handler(state: WebAppState):
    """Build a request handler class bound to WebAppState."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 65

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
                return

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            extra: Optional[Dict[str, str]] = None,
            close_conn: bool = False,
        ) -> None:
            if close_conn:
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if not extra or "Cache-Control" not in extra:
                self.send_header("Cache-Control", "no-store")
            if close_conn:
                self.send_header("Connection", "close")
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, obj: Any, close_conn: bool = False) -> None:
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                close_conn=close_conn,
            )

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 256 * 1024:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path != "/sw.js":
                state.touch()

            if path in ("/", "/index.html"):
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML_BYTES)
                return
            if path == "/manifest.json":
                self._send_bytes(HTTPStatus.OK, "application/manifest+json; charset=utf-8", MANIFEST_JSON_BYTES)
                return
            if path == "/icon.svg":
                self._send_bytes(HTTPStatus.OK, "image/svg+xml; charset=utf-8", APP_ICON_SVG_BYTES)
                return
            if path == "/sw.js":
                self._send_bytes(HTTPStatus.OK, "application/javascript; charset=utf-8", SW_JS_BYTES)
                return
            if path == "/api/config":
                cfg = load_config(state.config_dir)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "douyin_cookie_set": bool(cfg.douyin_cookie or cfg.cookies_by_domain),
                        "xhs_cookie_set": bool(cfg.xhs_cookie or cfg.cookies_by_domain),
                        "save_live_photo_video": cfg.save_live_photo_video,
                        "can_shutdown": state.can_shutdown,
                        "output_dir": str(cfg.output_dir),
                    },
                    close_conn=True,
                )
                return
            if path.startswith("/api/media/"):
                asset_id = path[len("/api/media/") :]
                qs = parse_qs(parsed.query)
                as_dl = qs.get("dl", [""])[0] == "1"
                as_thumb = qs.get("thumb", [""])[0] == "1"
                self._handle_media(asset_id, as_dl, as_thumb=as_thumb)
                return
            if path.startswith("/api/zip/"):
                job_id = path[len("/api/zip/") :]
                qs = parse_qs(parsed.query, keep_blank_values=True)
                selected_ids: Optional[List[str]] = None
                if "ids" in qs:
                    raw_ids = ",".join(qs["ids"])
                    selected_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
                self._handle_zip(job_id, selected_ids=selected_ids)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def _drain_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if 0 < length <= 65536:
                self.rfile.read(length)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path != "/api/leave":
                state.touch()

            if path == "/api/heartbeat":
                self._drain_body()
                self._send_json(HTTPStatus.OK, {"ok": True}, close_conn=True)
                return

            if path == "/api/leave":
                self._drain_body()
                mode = parse_qs(parsed.query).get("mode", ["hidden"])[0]
                delay = 30.0 if mode == "close" else 300.0
                state.mark_leave(delay)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": mode}, close_conn=True)
                return

            if path == "/api/extract":
                try:
                    payload = self._read_json()
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"}, close_conn=True)
                    return
                raw_input = str(payload.get("input") or "")
                url = extract_url(raw_input)
                if not url:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未在文本里识别到有效链接"}, close_conn=True)
                    return

                cfg, client = state.client()
                try:
                    with state.busy():
                        result = extract(url, client, cfg)
                except (ExtractionError, HttpError) as e:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)}, close_conn=True)
                    return
                except Exception as e:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"解析异常: {e}"}, close_conn=True)
                    return

                job = state.store.put_result(result)
                if state.prefetch_thumbs and any(sa.asset.thumb_urls for sa in job.assets):
                    threading.Thread(target=_prefetch_job_thumbs, args=(job, state), daemon=True).start()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "job_id": job.job_id,
                        "platform": result.platform,
                        "item_id": result.item_id,
                        "title": result.title,
                        "author": result.author,
                        "zip_url": f"/api/zip/{job.job_id}",
                        "assets": [
                            {
                                "id": sa.asset_id,
                                "index": sa.index,
                                "pair_index": sa.pair_index,
                                "kind": sa.asset.kind,
                                "thumb_url": (
                                    f"/api/media/{sa.asset_id}?thumb=1"
                                    if sa.asset.thumb_urls
                                    else f"/api/media/{sa.asset_id}"
                                ),
                                "preview_url": f"/api/media/{sa.asset_id}",
                                "download_url": f"/api/media/{sa.asset_id}?dl=1",
                            }
                            for sa in job.assets
                        ],
                    },
                    close_conn=True,
                )
                return

            if path == "/api/config":
                try:
                    payload = self._read_json()
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"}, close_conn=True)
                    return
                _save_web_config(payload, state.config_dir)
                self._send_json(HTTPStatus.OK, {"ok": True}, close_conn=True)
                return

            if path.startswith("/api/save/"):
                job_id = path[len("/api/save/") :]
                try:
                    payload = self._read_json()
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"}, close_conn=True)
                    return
                if not isinstance(payload, dict):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"}, close_conn=True)
                    return
                job = state.store.get_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在或已过期，请重新提取"}, close_conn=True)
                    return
                selected_ids: Optional[List[str]] = None
                if payload.get("asset_ids") is not None:
                    if not isinstance(payload["asset_ids"], list):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "asset_ids 参数必须为数组"}, close_conn=True)
                        return
                    selected_ids = [str(x) for x in payload["asset_ids"]]
                target_assets = _filter_job_assets(job, selected_ids)
                if not target_assets:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未选中任何有效文件"}, close_conn=True)
                    return
                cfg, client = state.client()
                with state.busy():
                    saved, errs = _save_job_to_output(job, state.store, client, cfg, target_assets=target_assets)
                if not saved:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "; ".join(errs) or "保存失败"}, close_conn=True)
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "saved": len(saved),
                        "total": len(target_assets),
                        "dir": cfg.output_dir.name,
                        "errors": errs,
                    },
                    close_conn=True,
                )
                return

            if path == "/api/shutdown":
                if not state.can_shutdown or (not state.hibernate_only and state.server is None):
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "当前模式不允许远程关闭"}, close_conn=True)
                    return
                if state.hibernate_only:
                    state.hibernate()
                    self._send_json(HTTPStatus.OK, {"ok": True, "hibernated": True}, close_conn=True)
                    return
                self._send_json(HTTPStatus.OK, {"ok": True}, close_conn=True)
                threading.Thread(target=state.server.shutdown, daemon=True).start()
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def _handle_media(self, asset_id: str, as_download: bool, as_thumb: bool = False) -> None:
            sa = state.store.get_asset(asset_id)
            if not sa:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "资源不存在或已过期，请重新提取"})
                return

            range_hdr = self.headers.get("Range")
            use_thumb = bool(as_thumb and sa.asset.thumb_urls)
            allow_thumb_cache = bool(use_thumb and not as_download and not range_hdr)
            slot_ev: Optional[threading.Event] = None
            owns_slot = False

            if allow_thumb_cache:
                cached = state.store.get_cached_thumb(sa)
                if cached is not None:
                    self._send_bytes(
                        HTTPStatus.OK,
                        cached[1],
                        cached[0],
                        extra={"Cache-Control": "private, max-age=300"},
                    )
                    return
                owns_slot, slot_ev = state.store.acquire_thumb_slot(sa.asset_id)
                if not owns_slot:
                    slot_ev.wait(timeout=12.0)
                    cached = state.store.get_cached_thumb(sa)
                    if cached is not None:
                        self._send_bytes(
                            HTTPStatus.OK,
                            cached[1],
                            cached[0],
                            extra={"Cache-Control": "private, max-age=300"},
                        )
                        return
                    owns_slot, slot_ev = state.store.acquire_thumb_slot(sa.asset_id)

            try:
                _, client = state.client()
                expected = (
                    "image"
                    if use_thumb
                    else ("video" if sa.asset.kind in ("video", "live_video") else "image")
                )
                urls = state.store.thumb_urls_for(sa) if use_thumb else state.store.candidate_urls_for(sa)
                with state.busy():
                    try:
                        stream = client.open_media_candidates(
                            urls,
                            default_ext=sa.asset.default_ext,
                            expected_prefix=expected,
                            headers=sa.asset.headers,
                            mobile=sa.asset.mobile,
                            range_header=range_hdr,
                        )
                    except Exception as e:
                        self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": f"拉取媒体失败: {e}"})
                        return

                    state.store.remember_winning(sa, stream.winning_url, stream.ext, is_thumb=use_thumb)
                    thumb_buf: Optional[bytearray] = None
                    if allow_thumb_cache and stream.status == 200:
                        try:
                            cl_val = int(stream.content_length) if stream.content_length else 0
                        except ValueError:
                            cl_val = 0
                        if cl_val <= _MAX_THUMB_CACHE_BYTES and len(stream.first_chunk) <= _MAX_THUMB_CACHE_BYTES:
                            thumb_buf = bytearray(stream.first_chunk)

                    try:
                        self.send_response(stream.status)
                        self.send_header("Content-Type", stream.content_type)
                        if stream.content_length:
                            self.send_header("Content-Length", stream.content_length)
                        else:
                            self.send_header("Connection", "close")
                            self.close_connection = True
                        if stream.content_range:
                            self.send_header("Content-Range", stream.content_range)
                        if stream.accept_ranges:
                            self.send_header("Accept-Ranges", stream.accept_ranges)
                        if as_download:
                            ext = sa.resolved_ext or stream.ext
                            fname = f"{sa.stem}{ext}"
                            ascii_fb = f"media_{sa.index}{ext}"
                            self.send_header(
                                "Content-Disposition",
                                f"attachment; filename=\"{ascii_fb}\"; filename*=UTF-8''{quote(fname)}",
                            )
                        self.send_header("Cache-Control", "private, max-age=300")
                        self.end_headers()

                        self.wfile.write(stream.first_chunk)
                        eof = False
                        while True:
                            chunk = stream.resp.read(65536)
                            if not chunk:
                                eof = True
                                break
                            if thumb_buf is not None:
                                if len(thumb_buf) + len(chunk) <= _MAX_THUMB_CACHE_BYTES:
                                    thumb_buf.extend(chunk)
                                else:
                                    thumb_buf = None
                            self.wfile.write(chunk)
                        if eof and thumb_buf:
                            state.store.cache_thumb(sa, bytes(thumb_buf), stream.content_type, stream.ext)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    finally:
                        stream.close()
            finally:
                if owns_slot and slot_ev is not None:
                    state.store.release_thumb_slot(sa.asset_id, slot_ev)

        def _handle_zip(self, job_id: str, selected_ids: Optional[Sequence[str]] = None) -> None:
            job = state.store.get_job(job_id)
            if not job:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在或已过期，请重新提取"})
                return
            target_assets = _filter_job_assets(job, selected_ids)
            if not target_assets:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未选中可打包文件"})
                return
            cfg, client = state.client()
            try:
                with state.busy():
                    zip_name, zip_bytes = _build_zip_bytes(job, state.store, client, cfg, target_assets=target_assets)
            except Exception as e:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": f"打包失败: {e}"})
                return

            self._send_bytes(
                HTTPStatus.OK,
                "application/zip",
                zip_bytes,
                extra={
                    "Content-Disposition": f"attachment; filename=\"media_bundle.zip\"; filename*=UTF-8''{quote(zip_name)}"
                },
            )

    return Handler


def _guess_lan_ips() -> Tuple[Optional[str], List[str]]:
    primary: Optional[str] = None
    others: List[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith("127."):
            primary = ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip != primary and ip not in others:
                others.append(ip)
    except OSError:
        pass
    return primary, others


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64


def serve(
    host: str = "0.0.0.0",
    port: int = 8765,
    idle_timeout: int = 0,
    allow_shutdown: bool = False,
    hibernate_on_idle: bool = False,
) -> None:
    """Start the local web UI server."""
    can_stop = allow_shutdown or idle_timeout > 0 or host == "127.0.0.1"
    state = WebAppState(
        can_shutdown=can_stop,
        hibernate_only=hibernate_on_idle,
        prewarm_on_wake=True,
        prefetch_thumbs=True,
    )
    srv = ReusableThreadingHTTPServer((host, port), make_handler(state))
    state.server = srv
    android.wake_lock()

    with state._lock:
        state._last_prewarm = time.monotonic()
    threading.Thread(target=_prewarm_extractors, args=(state.config_dir,), daemon=True).start()

    if idle_timeout > 0:
        def _watchdog() -> None:
            while True:
                if hibernate_on_idle and state.is_hibernated():
                    state._wake_event.wait()
                time.sleep(1)
                if state.should_shutdown(idle_timeout):
                    if hibernate_on_idle:
                        state.hibernate()
                    else:
                        print("[media-grab] 页面已关闭或闲置超时，自动退出。")
                        srv.shutdown()
                        break

        threading.Thread(target=_watchdog, daemon=True).start()

    primary_ip, other_ips = _guess_lan_ips()
    print("=" * 56)
    print("  media-grab 网页版已启动（抖音 · 小红书 · X 无水印提取）")
    print(f"  本机访问: http://127.0.0.1:{port}")
    if host != "127.0.0.1":
        if primary_ip:
            print(f"  手机访问: http://{primary_ip}:{port}  （优先用这个，需同 Wi-Fi）")
        for ip in other_ips:
            print(f"  备用网卡: http://{ip}:{port}")
    print("  在手机 X浏览器打开后点菜单「添加到桌面」，即成独立快捷图标")
    print("  按 Ctrl+C 可停止服务")
    print("=" * 56)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[media-grab] 已停止服务。")
    finally:
        srv.server_close()
        android.wake_unlock()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动无水印媒体提取网页版")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0 供同局域网手机访问）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--idle-timeout", type=int, default=0, help="闲置多少秒后自动退出（0 为不自动退出）")
    parser.add_argument("--allow-shutdown", action="store_true", help="允许网页右上角「关闭后台」按钮关闭服务")
    parser.add_argument(
        "--hibernate-on-idle",
        action="store_true",
        help="闲置或关闭请求时清理内存进入休眠而不退出进程（重开网页秒级响应）",
    )
    args = parser.parse_args(argv)
    serve(
        host=args.host,
        port=args.port,
        idle_timeout=args.idle_timeout,
        allow_shutdown=args.allow_shutdown,
        hibernate_on_idle=args.hibernate_on_idle,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Unit tests for resumable downloads, Range validation, and mid-stream retry."""

import http.server
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from media_grab.config import Config
from media_grab.http_client import ConnectionPool, HttpClient, HttpError


def _make_png(size: int = 32768, salt: int = 1) -> bytes:
    head = b"\x89PNG\r\n\x1a\n"
    body = bytes(((i * 31 + salt) % 251) for i in range(max(0, size - len(head))))
    return head + body


def _make_mp4(size: int = 48000, salt: int = 7) -> bytes:
    head = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    body = bytes(((i * 17 + salt) % 251) for i in range(max(0, size - len(head))))
    return head + body


class _RangeFixtureServer:
    """Configurable local HTTP/1.1 Range-capable server for resume tests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.routes = {}
        self.requests = []
        parent = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return

            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                range_hdr = self.headers.get("Range")
                with parent.lock:
                    parent.requests.append((self.path, range_hdr))
                    spec = parent.routes.get(self.path)
                    if spec is None:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    payload = spec["payload"]
                    ct = spec.get("content_type", "image/png")
                    etag = spec.get("etag")
                    last_mod = spec.get("last_modified")
                    ignore_range = spec.get("ignore_range", False)
                    bad_content_range = spec.get("bad_content_range")
                    drop_schedule = list(spec.get("drop_schedule") or [])
                    drop_at = drop_schedule.pop(0) if drop_schedule else None
                    spec["drop_schedule"] = drop_schedule

                total = len(payload)
                start = 0
                status = 200
                body = payload
                content_range = None

                if range_hdr and not ignore_range and range_hdr.startswith("bytes="):
                    raw = range_hdr[len("bytes=") :].split("-", 1)[0]
                    if raw.isdigit():
                        req_start = int(raw)
                        if req_start >= total:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{total}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        start = req_start
                        status = 206
                        body = payload[start:]
                        content_range = bad_content_range or f"bytes {start}-{total - 1}/{total}"

                self.send_response(status)
                self.send_header("Content-Type", ct)
                self.send_header("Accept-Ranges", "none" if ignore_range else "bytes")
                if etag:
                    self.send_header("ETag", etag)
                if last_mod:
                    self.send_header("Last-Modified", last_mod)
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

                to_send = body if drop_at is None else body[:drop_at]
                if drop_at is not None and drop_at < len(body):
                    self.close_connection = True
                if to_send:
                    self.wfile.write(to_send)
                    self.wfile.flush()

        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HttpResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _RangeFixtureServer()
        self.addCleanup(self.fixture.close)
        self.pool = ConnectionPool(max_per_key=4, max_total=8, idle_ttl=30.0)
        self.addCleanup(self.pool.close_all)
        self.client = HttpClient(Config(timeout=5), pool=self.pool)

    def test_resume_concatenates_exact_bytes_and_cleans_meta(self):
        payload = _make_png(36000, salt=3)
        url = f"{self.fixture.base_url}/photo.png"
        self.fixture.routes["/photo.png"] = {
            "payload": payload,
            "content_type": "image/png",
            "etag": '"png-v1"',
        }

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            part = dest / ".clip1.part"
            meta = dest / ".clip1.part.meta"
            part.write_bytes(payload[:12000])
            meta.write_text(
                json.dumps(
                    {
                        "winning_url": url,
                        "etag": '"png-v1"',
                        "last_modified": "",
                        "ext": ".png",
                        "total_size": len(payload),
                    }
                ),
                encoding="utf-8",
            )

            saved = self.client.download_candidates([url], dest, "clip1", ".jpg", "image")
            self.assertEqual(saved.name, "clip1.png")
            self.assertEqual(saved.read_bytes(), payload)
            self.assertFalse(part.exists())
            self.assertFalse(meta.exists())

        self.assertEqual(self.fixture.requests, [("/photo.png", "bytes=12000-")])

    def test_server_200_on_range_truncates_part_and_downloads_fresh(self):
        payload = _make_png(28000, salt=9)
        url = f"{self.fixture.base_url}/no_range.png"
        self.fixture.routes["/no_range.png"] = {
            "payload": payload,
            "content_type": "image/png",
            "etag": '"nr-v1"',
            "ignore_range": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            part = dest / ".nr.part"
            meta = dest / ".nr.part.meta"
            part.write_bytes(b"OLD_CORRUPT_PREFIX_" * 500)
            meta.write_text(
                json.dumps(
                    {
                        "winning_url": url,
                        "etag": '"nr-v1"',
                        "last_modified": "",
                        "ext": ".png",
                        "total_size": len(payload),
                    }
                ),
                encoding="utf-8",
            )

            saved = self.client.download_candidates([url], dest, "nr", ".png", "image")
            self.assertEqual(saved.read_bytes(), payload)
            self.assertFalse(part.exists())
            self.assertFalse(meta.exists())

        self.assertEqual(self.fixture.requests, [("/no_range.png", f"bytes={len(b'OLD_CORRUPT_PREFIX_' * 500)}-")])

    def test_etag_and_validator_changes_refuse_resume_and_redownload(self):
        old_payload = _make_png(30000, salt=1)
        new_payload = _make_png(32000, salt=2)
        self.assertNotEqual(old_payload[:10000], new_payload[:10000])

        url = f"{self.fixture.base_url}/changed.png"
        self.fixture.routes["/changed.png"] = {
            "payload": new_payload,
            "content_type": "image/png",
            "etag": '"v2"',
        }

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            part = dest / ".chg.part"
            meta = dest / ".chg.part.meta"
            part.write_bytes(old_payload[:10000])
            meta.write_text(
                json.dumps(
                    {
                        "winning_url": url,
                        "etag": '"v1"',
                        "last_modified": "",
                        "ext": ".png",
                        "total_size": len(old_payload),
                    }
                ),
                encoding="utf-8",
            )

            saved = self.client.download_candidates([url], dest, "chg", ".png", "image")
            self.assertEqual(saved.read_bytes(), new_payload)
            self.assertFalse(part.exists())
            self.assertFalse(meta.exists())

        self.assertEqual(
            self.fixture.requests,
            [("/changed.png", "bytes=10000-"), ("/changed.png", None)],
        )

        # Last-Modified mismatch also refuses resume.
        self.fixture.requests.clear()
        self.fixture.routes["/changed_lm.png"] = {
            "payload": new_payload,
            "content_type": "image/png",
            "last_modified": "Tue, 02 Jan 2024 00:00:00 GMT",
        }
        lm_url = f"{self.fixture.base_url}/changed_lm.png"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / ".lm.part").write_bytes(old_payload[:9000])
            (dest / ".lm.part.meta").write_text(
                json.dumps(
                    {
                        "winning_url": lm_url,
                        "etag": "",
                        "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT",
                        "ext": ".png",
                        "total_size": len(old_payload),
                    }
                ),
                encoding="utf-8",
            )
            saved = self.client.download_candidates([lm_url], dest, "lm", ".png", "image")
            self.assertEqual(saved.read_bytes(), new_payload)
        self.assertEqual(
            self.fixture.requests,
            [("/changed_lm.png", "bytes=9000-"), ("/changed_lm.png", None)],
        )

    def test_mismatched_url_or_content_range_refuses_append(self):
        old_payload = _make_png(26000, salt=4)
        new_payload = _make_png(26000, salt=5)
        self.fixture.routes["/other.png"] = {
            "payload": new_payload,
            "content_type": "image/png",
            "etag": '"same"',
        }
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / ".u.part").write_bytes(old_payload[:8500])
            (dest / ".u.part.meta").write_text(
                json.dumps(
                    {
                        "winning_url": f"{self.fixture.base_url}/orig.png",
                        "etag": '"same"',
                        "last_modified": "",
                        "ext": ".png",
                        "total_size": len(old_payload),
                    }
                ),
                encoding="utf-8",
            )
            saved = self.client.download_candidates([f"{self.fixture.base_url}/other.png"], dest, "u", ".png", "image")
            self.assertEqual(saved.read_bytes(), new_payload)
        self.assertEqual(self.fixture.requests, [("/other.png", None)])

        # Server returning wrong Content-Range prefix must also be rejected and restarted cleanly.
        self.fixture.requests.clear()
        self.fixture.routes["/badcr.png"] = {
            "payload": new_payload,
            "content_type": "image/png",
            "etag": '"b1"',
            "bad_content_range": f"bytes 1234-{len(new_payload) - 1}/{len(new_payload)}",
        }
        badcr_url = f"{self.fixture.base_url}/badcr.png"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / ".cr.part").write_bytes(old_payload[:8500])
            (dest / ".cr.part.meta").write_text(
                json.dumps(
                    {
                        "winning_url": badcr_url,
                        "etag": '"b1"',
                        "last_modified": "",
                        "ext": ".png",
                        "total_size": len(old_payload),
                    }
                ),
                encoding="utf-8",
            )
            saved = self.client.download_candidates([badcr_url], dest, "cr", ".png", "image")
            self.assertEqual(saved.read_bytes(), new_payload)
        self.assertEqual(self.fixture.requests, [("/badcr.png", "bytes=8500-"), ("/badcr.png", None)])

    def test_midstream_drop_retries_three_times_and_succeeds(self):
        payload = _make_mp4(50000, salt=11)
        url = f"{self.fixture.base_url}/video.mp4"
        self.fixture.routes["/video.mp4"] = {
            "payload": payload,
            "content_type": "video/mp4",
            "etag": '"vid-etag"',
            "last_modified": "Wed, 03 Jan 2024 00:00:00 GMT",
            "drop_schedule": [10000, 12000, 13000],
        }

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            saved = self.client.download_candidates([url], dest, "clip", ".mp4", "video")
            self.assertEqual(saved.name, "clip.mp4")
            self.assertEqual(saved.read_bytes(), payload)
            self.assertFalse((dest / ".clip.part").exists())
            self.assertFalse((dest / ".clip.part.meta").exists())

        self.assertEqual(
            self.fixture.requests,
            [
                ("/video.mp4", None),
                ("/video.mp4", "bytes=10000-"),
                ("/video.mp4", "bytes=22000-"),
                ("/video.mp4", "bytes=35000-"),
            ],
        )

    def test_exhausted_retries_cleans_up_part_and_meta_and_raises(self):
        payload = _make_mp4(40000, salt=5)
        url = f"{self.fixture.base_url}/flaky.mp4"
        self.fixture.routes["/flaky.mp4"] = {
            "payload": payload,
            "content_type": "video/mp4",
            "etag": '"flaky-1"',
            "drop_schedule": [9000, 5000, 5000, 5000],
        }

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            with self.assertRaises(HttpError):
                self.client.download_candidates([url], dest, "flaky", ".mp4", "video")
            self.assertFalse((dest / ".flaky.part").exists())
            self.assertFalse((dest / ".flaky.part.meta").exists())

        self.assertEqual(len(self.fixture.requests), 4)

    def test_oserror_midstream_retries_and_raises_original_oserror_on_exhaustion(self):
        payload = _make_mp4(60000, salt=8)

        class _ErrResp:
            def __init__(self, start: int, send_len: int, fail_read: bool):
                self.status = 206 if start > 0 else 200
                self.headers = {
                    "Content-Type": "video/mp4",
                    "ETag": '"os1"',
                    "Content-Length": str(len(payload) - start),
                }
                if start > 0:
                    self.headers["Content-Range"] = f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                self._buf = io.BytesIO(payload[start : start + send_len])
                self._fail_read = fail_read

            def read(self, n=-1):
                chunk = self._buf.read(n)
                if chunk:
                    return chunk
                if self._fail_read:
                    raise ConnectionResetError("peer reset mid-stream")
                return b""

            def close(self):
                self._buf.close()

        calls = []

        class _FakeOpener:
            def __init__(self, fail_all: bool):
                self.fail_all = fail_all

            def open(self, req, timeout=10):
                rng = req.headers.get("Range")
                calls.append(rng)
                start = int(rng[len("bytes=") :].split("-", 1)[0]) if (rng and rng.startswith("bytes=")) else 0
                if self.fail_all:
                    return _ErrResp(start, 8500, fail_read=True)
                if len(calls) == 1:
                    return _ErrResp(start, 9000, fail_read=True)
                if len(calls) == 2:
                    return _ErrResp(start, 8500, fail_read=True)
                return _ErrResp(start, len(payload) - start, fail_read=False)

        # Case 1: OSError mid-stream recovers on retry
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            with patch("urllib.request.build_opener", return_value=_FakeOpener(fail_all=False)):
                saved = self.client.download_candidates(["https://cdn.example/v.mp4"], dest, "osok", ".mp4", "video")
                self.assertEqual(saved.read_bytes(), payload)
                self.assertFalse((dest / ".osok.part").exists())
                self.assertFalse((dest / ".osok.part.meta").exists())
            self.assertEqual(calls, [None, "bytes=9000-", "bytes=17500-"])

        # Case 2: All retries raise ConnectionResetError -> cleans up & re-raises ConnectionResetError
        calls.clear()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            with patch("urllib.request.build_opener", return_value=_FakeOpener(fail_all=True)):
                with self.assertRaises(ConnectionResetError):
                    self.client.download_candidates(["https://cdn.example/v.mp4"], dest, "osfail", ".mp4", "video")
                self.assertFalse((dest / ".osfail.part").exists())
                self.assertFalse((dest / ".osfail.part.meta").exists())
            self.assertEqual(len(calls), 4)

    def test_existing_final_file_and_416_fallback(self):
        payload = _make_png(22000, salt=12)
        url = f"{self.fixture.base_url}/existing.png"
        self.fixture.routes["/existing.png"] = {
            "payload": payload,
            "content_type": "image/png",
            "etag": '"ex1"',
        }
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            target = dest / "done.png"
            target.write_bytes(payload)
            saved = self.client.download_candidates([url], dest, "done", ".png", "image")
            self.assertEqual(saved, target)
            self.assertEqual(saved.read_bytes(), payload)

        # HTTP 416 when .part is larger than new remote file recovers by re-downloading cleanly.
        self.fixture.requests.clear()
        shorter = _make_png(12000, salt=15)
        self.fixture.routes["/shrank.png"] = {
            "payload": shorter,
            "content_type": "image/png",
            "etag": '"shrank"',
        }
        shrank_url = f"{self.fixture.base_url}/shrank.png"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / ".shrank.part").write_bytes(b"X" * 20000)
            (dest / ".shrank.part.meta").write_text(
                json.dumps(
                    {
                        "winning_url": shrank_url,
                        "etag": '"shrank"',
                        "last_modified": "",
                        "ext": ".png",
                        "total_size": 20000,
                    }
                ),
                encoding="utf-8",
            )
            saved = self.client.download_candidates([shrank_url], dest, "shrank", ".png", "image")
            self.assertEqual(saved.read_bytes(), shorter)
        self.assertEqual(self.fixture.requests, [("/shrank.png", "bytes=20000-"), ("/shrank.png", None)])


if __name__ == "__main__":
    unittest.main(verbosity=2)

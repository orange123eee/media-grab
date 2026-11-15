"""End-to-end HTTP tests for media_grab.web server and tokenized media proxy."""

import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from media_grab.extractors import ExtractResult, MediaAsset
from media_grab.gate import OnDemandGate
from media_grab.web import ReusableThreadingHTTPServer, WebAppState, make_handler


class _FakeRawResp:
    def __init__(self, ct: str, payload: bytes, status: int = 200, content_range: str = ""):
        self.status = status
        self.headers = {"Content-Type": ct, "Content-Length": str(len(payload))}
        if content_range:
            self.headers["Content-Range"] = content_range
        self._bio = io.BytesIO(payload)

    def read(self, n: int = -1) -> bytes:
        return self._bio.read(n)

    def close(self) -> None:
        self._bio.close()


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_dir = Path(self._tmp.name)
        self.state = WebAppState(config_dir=self.cfg_dir)
        self.srv = ReusableThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def _req(self, path: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_static_pages_and_manifest(self):
        st, hdrs, body = self._req("/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", hdrs.get("Content-Type", ""))
        self.assertIn("无水印提取", body.decode("utf-8"))

        st2, _, mf = self._req("/manifest.json")
        self.assertEqual(st2, 200)
        self.assertEqual(json.loads(mf.decode("utf-8"))["name"], "无水印提取")

    def test_extract_proxy_fallback_zip_and_ssrf_guard(self):
        # Unknown token must 404 (no arbitrary URL proxying).
        st404, _, _ = self._req("/api/media/nonexistent-token")
        self.assertEqual(st404, 404)

        bad_st, _, bad_body = self._req("/api/extract", method="POST", body={"input": "没有链接的文本"})
        self.assertEqual(bad_st, 400)
        self.assertFalse(json.loads(bad_body)["ok"])

        fake_result = ExtractResult(
            platform="xiaohongshu",
            item_id="note88",
            title="山海日落",
            author="摄影师",
            assets=[
                MediaAsset(
                    kind="image",
                    urls=["https://bad.example/1", "https://good.example/1.png"],
                    default_ext=".jpg",
                    thumb_urls=["https://thumb.example/1.webp"],
                ),
                MediaAsset(kind="image", urls=["https://good.example/2.jpg"], default_ext=".jpg"),
            ],
        )

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"P" * 1500
        jpg_bytes = b"\xff\xd8\xff\xe0" + b"J" * 1500
        webp_bytes = b"RIFF1234WEBP" + b"W" * 1500

        class FakeOpener:
            def open(self, req, timeout=10):
                u = req.full_url
                if "bad.example" in u:
                    return _FakeRawResp("text/html", b"<html>403</html>")
                if "thumb.example" in u:
                    return _FakeRawResp("image/webp", webp_bytes)
                if "1.png" in u:
                    return _FakeRawResp("image/png", png_bytes)
                return _FakeRawResp("image/jpeg", jpg_bytes)

        with patch("media_grab.web.extract", return_value=fake_result):
            st, _, raw = self._req("/api/extract", method="POST", body={"input": "分享 http://xhslink.com/a/abc"})
        self.assertEqual(st, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["assets"]), 2)
        self.assertTrue(data["assets"][0]["thumb_url"].endswith("?thumb=1"))

        with patch("urllib.request.build_opener", return_value=FakeOpener()):
            tst, _, tbody = self._req(data["assets"][0]["thumb_url"])
            self.assertEqual(tst, 200)
            self.assertEqual(tbody, webp_bytes)

            mst, mhdrs, mbody = self._req(data["assets"][0]["download_url"])
            self.assertEqual(mst, 200)
            self.assertEqual(mbody, png_bytes)
            self.assertIn("attachment", mhdrs.get("Content-Disposition", ""))
            self.assertIn(".png", mhdrs.get("Content-Disposition", ""))

            zst, zhdrs, zbody = self._req(data["zip_url"])
            self.assertEqual(zst, 200)
            self.assertEqual(zhdrs.get("Content-Type"), "application/zip")
            with zipfile.ZipFile(io.BytesIO(zbody)) as zf:
                names = sorted(zf.namelist())
                self.assertEqual(len(names), 2)
                self.assertTrue(any(n.endswith("_01.png") for n in names))
                self.assertTrue(any(n.endswith("_02.jpg") for n in names))

    def test_config_roundtrip(self):
        st, _, raw = self._req("/api/config")
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(raw)["xhs_cookie_set"])

        pst, _, praw = self._req(
            "/api/config",
            method="POST",
            body={"xhs_cookie": "a1=test123", "save_live_photo_video": False},
        )
        self.assertEqual(pst, 200)
        self.assertTrue(json.loads(praw)["ok"])

        st2, _, raw2 = self._req("/api/config")
        cfg = json.loads(raw2)
        self.assertTrue(cfg["xhs_cookie_set"])
        self.assertFalse(cfg["save_live_photo_video"])
        self.assertFalse(cfg["can_shutdown"])

    def test_save_local_and_shutdown(self):
        self.state.can_shutdown = True
        self.state.server = self.srv

        fake_result = ExtractResult(
            platform="douyin",
            item_id="dy99",
            title="本地直存",
            author="u",
            assets=[MediaAsset(kind="image", urls=["https://good.example/1.jpg"], default_ext=".jpg")],
        )
        jpg_bytes = b"\xff\xd8\xff\xe0" + b"J" * 1500

        class FakeOpener:
            def open(self, req, timeout=10):
                return _FakeRawResp("image/jpeg", jpg_bytes)

        with patch("media_grab.web.extract", return_value=fake_result):
            st, _, raw = self._req("/api/extract", method="POST", body={"input": "https://v.douyin.com/abc/"})
        self.assertEqual(st, 200)
        job_id = json.loads(raw.decode("utf-8"))["job_id"]

        out_dir = self.cfg_dir / "DCIM"
        with (
            patch("media_grab.web.load_config") as mock_cfg,
            patch("urllib.request.build_opener", return_value=FakeOpener()),
            patch("media_grab.web.android.scan_media") as mock_scan,
        ):
            from media_grab.config import Config

            mock_cfg.return_value = Config(output_dir=out_dir)
            sst, _, sraw = self._req(f"/api/save/{job_id}", method="POST")
        self.assertEqual(sst, 200)
        sdata = json.loads(sraw.decode("utf-8"))
        self.assertTrue(sdata["ok"])
        self.assertEqual(sdata["saved"], 1)
        self.assertEqual(mock_scan.call_count, 1)
        self.assertTrue((out_dir / "douyin_本地直存_dy99.jpg").is_file())

        dst, _, draw = self._req("/api/shutdown", method="POST")
        self.assertEqual(dst, 200)
        self.assertTrue(json.loads(draw.decode("utf-8"))["ok"])

    def test_sw_and_lifecycle_heartbeat_leave(self):
        st, hdrs, body = self._req("/sw.js")
        self.assertEqual(st, 200)
        self.assertIn("javascript", hdrs.get("Content-Type", ""))
        sw_text = body.decode("utf-8")
        self.assertIn("media-grab-shell", sw_text)
        self.assertIn("fetchWithTimeout", sw_text)

        lst, _, lraw = self._req("/api/leave?mode=close", method="POST")
        self.assertEqual(lst, 200)
        self.assertEqual(json.loads(lraw.decode("utf-8"))["mode"], "close")
        self.assertIsNotNone(self.state.leave_deadline)
        self.assertFalse(self.state.should_shutdown(90))

        # Busy download/stream protects worker even if leave deadline already passed.
        with self.state.busy():
            self.state.leave_deadline = time.monotonic() - 1.0
            self.assertFalse(self.state.should_shutdown(90))
        # Once busy transfer completes, overdue leave signal allows shutdown.
        self.assertTrue(self.state.should_shutdown(90))

        # Heartbeat clears pending leave deadline and resets activity.
        hst, _, hraw = self._req("/api/heartbeat", method="POST")
        self.assertEqual(hst, 200)
        self.assertTrue(json.loads(hraw.decode("utf-8"))["ok"])
        self.assertIsNone(self.state.leave_deadline)
        self.assertFalse(self.state.should_shutdown(90))

    def test_hibernate_on_shutdown_keeps_server_alive(self):
        self.state.can_shutdown = True
        self.state.hibernate_only = True
        job = self.state.store.put_result(
            ExtractResult(
                platform="douyin",
                item_id="hib1",
                title="休眠清理",
                author="u",
                assets=[MediaAsset(kind="image", urls=["https://good.example/1.jpg"], default_ext=".jpg")],
            )
        )
        self.assertIsNotNone(self.state.store.get_job(job.job_id))

        sst, _, sraw = self._req("/api/shutdown", method="POST")
        self.assertEqual(sst, 200)
        sdata = json.loads(sraw.decode("utf-8"))
        self.assertTrue(sdata["ok"])
        self.assertTrue(sdata["hibernated"])
        self.assertTrue(self.state.is_hibernated())
        self.assertIsNone(self.state.store.get_job(job.job_id))

        # Server keeps listening so next page open wakes immediately with 200 OK.
        st_idx, _, _ = self._req("/")
        self.assertEqual(st_idx, 200)
        self.assertFalse(self.state.is_hibernated())

    def test_reload_resilience_and_start_web_script(self):
        # Simulate aborted client sockets (e.g. page reload / SW timeout) before normal /api/config.
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as raw_sock:
            raw_sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            raw_sock.recv(128)
        with socket.create_connection(("127.0.0.1", self.port), timeout=2):
            pass

        st, _, raw = self._req("/api/config")
        self.assertEqual(st, 200)
        self.assertIn("xhs_cookie_set", json.loads(raw.decode("utf-8")))

        _, _, idx_raw = self._req("/")
        idx_html = idx_raw.decode("utf-8")
        self.assertIn("scheduleReconnect", idx_html)
        self.assertIn("pageshow", idx_html)
        self.assertIn("cache: 'no-store'", idx_html)

        _, _, sw_raw = self._req("/sw.js")
        sw_text = sw_raw.decode("utf-8")
        self.assertIn("SW_OFFLINE_HTML", sw_text)
        self.assertNotIn("fetch(req,", sw_text)

        start_sh = Path(__file__).resolve().parent.parent / "bin" / "start-web.sh"
        sh_bytes = start_sh.read_bytes()
        self.assertNotIn(b"\r\n", sh_bytes)
        sh_text = sh_bytes.decode("utf-8")
        self.assertNotIn(r"media_grab\.\(", sh_text)
        self.assertIn("stop_existing", sh_text)


class OnDemandGateTests(unittest.TestCase):
    def test_gate_spawns_on_demand_and_proxies(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        worker_port = probe.getsockname()[1]
        probe.close()

        worker_state = WebAppState()
        created = []
        worker_holder = []

        def spawn_worker():
            created.append(1)
            srv = ReusableThreadingHTTPServer(("127.0.0.1", worker_port), make_handler(worker_state))
            worker_holder.append(srv)
            threading.Thread(target=srv.serve_forever, daemon=True).start()

        gate = OnDemandGate(host="127.0.0.1", port=0, worker_port=worker_port, spawn_fn=spawn_worker)
        gthread = threading.Thread(target=gate.serve_forever, daemon=True)
        gthread.start()

        try:
            self.assertEqual(len(created), 0)
            with urllib.request.urlopen(f"http://127.0.0.1:{gate.port}/manifest.json", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read().decode("utf-8"))["name"], "无水印提取")
            self.assertEqual(len(created), 1)

            # Second request reuses already running worker instead of spawning again.
            with urllib.request.urlopen(f"http://127.0.0.1:{gate.port}/api/config", timeout=5) as resp2:
                self.assertEqual(resp2.status, 200)
                self.assertIn("xhs_cookie_set", json.loads(resp2.read().decode("utf-8")))
            self.assertEqual(len(created), 1)
        finally:
            gate.close()
            for srv in worker_holder:
                srv.shutdown()
                srv.server_close()
            gthread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

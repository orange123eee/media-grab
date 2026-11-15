"""Offline regression tests for Douyin fast ttwid/parallel paths, feed fallback, and cover thumbs."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from media_grab.config import Config
from media_grab.extractors import ExtractionError
from media_grab.extractors import douyin
from media_grab.extractors.douyin import (
    _TTWID_CACHE,
    _get_anonymous_ttwid,
    _register_ttwid,
    extract_douyin,
    parse_douyin_payload,
    peek_cached_ttwid,
)
from media_grab.http_client import HttpClient, HttpResponse
from media_grab.web import JobStore, WebAppState


def _shell_router_html() -> bytes:
    payload = {
        "loaderData": {
            "video_(id)/page": {
                "ua": "Mozilla/5.0",
                "renderInSSR": True,
                "host": "www.iesdouyin.com",
            }
        }
    }
    return f"<script>window._ROUTER_DATA = {json.dumps(payload)}</script>".encode("utf-8")


def _full_router_html(aweme_id: str, title: str = "无水印测试") -> bytes:
    payload = {
        "loaderData": {
            "video_(id)/page": {
                "videoInfoRes": {
                    "item_list": [
                        {
                            "aweme_id": aweme_id,
                            "desc": title,
                            "author": {"nickname": "测试作者"},
                            "video": {
                                "cover": {"url_list": ["https://p3.douyinpic.com/cover.webp", "https://p9.douyinpic.com/cover.jpeg"]},
                                "play_addr": {
                                    "url_list": [
                                        f"https://aweme.snssdk.com/aweme/v1/playwm/?video_id=vid_{aweme_id}&ratio=720p&line=0"
                                    ]
                                },
                            },
                        }
                    ]
                }
            }
        }
    }
    return f"<script>window._ROUTER_DATA = {json.dumps(payload, ensure_ascii=False)}</script>".encode("utf-8")


class DouyinFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cfg_dir = os.environ.get("MEDIA_GRAB_CONFIG_DIR")
        os.environ["MEDIA_GRAB_CONFIG_DIR"] = self._tmp.name
        _TTWID_CACHE["value"] = ""
        _TTWID_CACHE["expires_at"] = 0.0

    def tearDown(self):
        if self._old_cfg_dir is None:
            os.environ.pop("MEDIA_GRAB_CONFIG_DIR", None)
        else:
            os.environ["MEDIA_GRAB_CONFIG_DIR"] = self._old_cfg_dir
        self._tmp.cleanup()

    def test_router_data_shell_retries_ttwid_before_feed_for_speed(self):
        target_id = "7708291218199909499"
        calls = []

        class FakeClient:
            def cookie_for_url(self, url):
                return "foo=bar"

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                if "v.douyin.com" in url:
                    return HttpResponse(
                        200,
                        f"https://www.iesdouyin.com/share/video/{target_id}/?from_ssr=1",
                        {},
                        _shell_router_html(),
                    )
                if "iesdouyin.com/share/video/" in url:
                    cookie_hdr = (kwargs.get("headers") or {}).get("Cookie", "")
                    if "ttwid=mock_tok" in cookie_hdr:
                        return HttpResponse(200, url, {}, _full_router_html(target_id, "带我远离寂寞的边缘"))
                    return HttpResponse(200, url, {}, _shell_router_html())
                raise AssertionError(f"Should not hit slow feed when ttwid succeeds: {url}")

        with patch.object(douyin, "_register_ttwid", return_value="mock_tok") as reg_mock:
            res = extract_douyin("https://v.douyin.com/JIUTdKyjptw/", FakeClient(), Config())

        reg_mock.assert_called_once()
        self.assertEqual(res.item_id, target_id)
        self.assertEqual(res.title, "带我远离寂寞的边缘")
        self.assertEqual(len(calls), 2)
        self.assertIn("ttwid=mock_tok", calls[1][1]["headers"]["Cookie"])
        self.assertIn("foo=bar", calls[1][1]["headers"]["Cookie"])
        self.assertTrue(res.assets[0].thumb_urls)

    def test_router_data_shell_fallbacks_to_feed_and_matches_exact_id(self):
        target_id = "7708291218199909499"
        calls = []

        class FakeClient:
            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                if "v.douyin.com" in url:
                    return HttpResponse(
                        200,
                        f"https://www.iesdouyin.com/share/video/{target_id}/?from_ssr=1",
                        {},
                        _shell_router_html(),
                    )
                if "/aweme/v1/feed/" in url:
                    feed = {
                        "aweme_list": [
                            {
                                "aweme_id": "9999999999999999999",
                                "desc": "wrong recommended item",
                                "author": {"nickname": "wrong"},
                                "video": {
                                    "play_addr": {"url_list": ["https://example.com/wrong.mp4"]}
                                },
                            },
                            {
                                "aweme_id": target_id,
                                "desc": "带我远离寂寞的边缘",
                                "author": {"nickname": "73"},
                                "video": {
                                    "bit_rate": [
                                        {
                                            "bit_rate": 1200000,
                                            "play_addr": {
                                                "url_list": ["https://v5-jltc.douyinvod.com/hd1200.mp4"]
                                            },
                                        }
                                    ],
                                    "play_addr": {
                                        "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v27&ratio=720p"]
                                    },
                                },
                            },
                        ]
                    }
                    return HttpResponse(200, url, {}, json.dumps(feed).encode("utf-8"))
                raise AssertionError(f"Unexpected call: {url}")

        with patch.object(douyin, "_register_ttwid", return_value=""):
            res = extract_douyin("https://v.douyin.com/JIUTdKyjptw/", FakeClient(), Config())
        self.assertEqual(res.item_id, target_id)
        self.assertEqual(res.title, "带我远离寂寞的边缘")
        self.assertEqual(res.author, "73")
        self.assertEqual(len(calls), 2)
        self.assertIn("/aweme/v1/feed/", calls[1][0])
        self.assertIn("https://v5-jltc.douyinvod.com/hd1200.mp4", res.assets[0].urls[0])

    def test_feed_mismatch_and_empty_ttwid_raises_instead_of_wrong_item(self):
        target_id = "7700000000000000001"

        class FakeClient:
            def get(self, url, **kwargs):
                if "/aweme/v1/feed/" in url:
                    unrelated = {
                        "aweme_list": [
                            {
                                "aweme_id": "8888888888888888888",
                                "desc": "unrelated item",
                                "video": {"play_addr": {"url_list": ["https://example.com/x.mp4"]}},
                            }
                        ]
                    }
                    return HttpResponse(200, url, {}, json.dumps(unrelated).encode("utf-8"))
                return HttpResponse(
                    200,
                    f"https://www.iesdouyin.com/share/video/{target_id}/",
                    {},
                    _shell_router_html(),
                )

        with patch.object(douyin, "_register_ttwid", return_value=""):
            with self.assertRaises(ExtractionError):
                extract_douyin("https://v.douyin.com/missing/", FakeClient(), Config())

    def test_ttwid_memory_and_disk_cache_reused_within_ttl(self):
        cfg = Config()
        with patch.object(douyin, "_register_ttwid", side_effect=["tok1", "tok2"]) as reg_mock:
            self.assertEqual(_get_anonymous_ttwid(cfg), "tok1")
            self.assertEqual(_get_anonymous_ttwid(cfg), "tok1")
            self.assertEqual(reg_mock.call_count, 1)

            # Clear in-memory cache and confirm disk cache restores tok1 without re-registering
            _TTWID_CACHE["value"] = ""
            _TTWID_CACHE["expires_at"] = 0.0
            self.assertEqual(peek_cached_ttwid(use_disk_cache=True), "tok1")
            self.assertEqual(_get_anonymous_ttwid(cfg), "tok1")
            self.assertEqual(reg_mock.call_count, 1)

            self.assertEqual(_get_anonymous_ttwid(cfg, force_refresh=True), "tok2")
            self.assertEqual(reg_mock.call_count, 2)

    def test_register_ttwid_extracts_cookie_or_handles_failure(self):
        class FakeHeaders:
            def get_all(self, name):
                if name == "Set-Cookie":
                    return ["other=1; Path=/", "ttwid=1%7Cabc_xyz; Path=/; HttpOnly"]
                return []

        class FakeResp:
            headers = FakeHeaders()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        cfg = Config()
        with patch("urllib.request.urlopen", return_value=FakeResp()):
            self.assertEqual(_register_ttwid(cfg), "1%7Cabc_xyz")

        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertEqual(_register_ttwid(cfg), "")

    def test_real_httpclient_fast_paths_direct_and_shortlink(self):
        cfg = Config()
        client = HttpClient(cfg)
        calls = []

        def fake_get(url, headers=None, mobile=False, follow_redirects=True):
            calls.append((url, headers, follow_redirects))
            if not follow_redirects and "v.douyin.com" in url:
                return HttpResponse(
                    302,
                    url,
                    {"location": "https://www.iesdouyin.com/share/video/7707971778865783155/?region=CN"},
                    b"",
                )
            if "v.douyin.com" in url and follow_redirects:
                self.assertIn("ttwid=fast_tok", (headers or {}).get("Cookie", ""))
                return HttpResponse(
                    200,
                    "https://www.iesdouyin.com/share/video/7707971778865783155/",
                    {},
                    _full_router_html("7707971778865783155", "单跳直达"),
                )
            if "www.iesdouyin.com/share/video/7707971778865783155/" in url:
                self.assertIn("ttwid=fast_tok", (headers or {}).get("Cookie", ""))
                return HttpResponse(200, url, {}, _full_router_html("7707971778865783155", "跳过PC页"))
            raise AssertionError(f"Unexpected request: {url}")

        with (
            patch.object(client, "get", side_effect=fake_get),
            patch.object(douyin, "_register_ttwid", return_value="fast_tok") as reg_mock,
        ):
            # 1. Cold start shortlink: 1 non-redirect lookup + concurrent ttwid + 1 iesdouyin share fetch
            r_cold = extract_douyin("https://v.douyin.com/cold/", client, cfg)
            self.assertEqual(r_cold.title, "跳过PC页")
            self.assertEqual(reg_mock.call_count, 1)
            self.assertEqual(calls[0][2], False)

            # 2. Warm start shortlink: single-hop directly with cached ttwid Cookie
            calls.clear()
            r_warm = extract_douyin("https://v.douyin.com/warm/", client, cfg)
            self.assertEqual(r_warm.title, "单跳直达")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2], True)

            # 3. Direct desktop douyin.com URL: skips 889KB desktop HTML and hits iesdouyin directly in 1 call
            calls.clear()
            r_direct = extract_douyin("https://www.douyin.com/video/7707971778865783155", client, cfg)
            self.assertEqual(r_direct.title, "跳过PC页")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][0].startswith("https://www.iesdouyin.com/share/video/7707971778865783155/"))
            self.assertEqual(reg_mock.call_count, 1)

    def test_open_media_candidates_strips_referer_on_403_retry(self):
        cfg = Config(output_dir=Path(self._tmp.name))
        client = HttpClient(cfg)
        seen_headers = []

        class GoodResp:
            status = 200
            headers = {"Content-Type": "video/mp4", "Content-Length": "4096"}

            def read(self, n=-1):
                if getattr(self, "_done", False):
                    return b""
                self._done = True
                return b"\x00\x00\x00\x1cftypisom" + b"\x00" * 64

            def close(self):
                pass

        class FakeOpener:
            def open(self, req, timeout=10):
                hdrs = {k.lower(): v for k, v in req.header_items()}
                seen_headers.append(hdrs)
                if "referer" in hdrs:
                    raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
                return GoodResp()

        with patch("urllib.request.build_opener", return_value=FakeOpener()):
            stream = client.open_media_candidates(
                ["https://cdn.example/v.mp4"],
                default_ext=".mp4",
                expected_prefix="video",
                headers={"Referer": "https://www.douyin.com/"},
                mobile=True,
            )
            stream.close()

        self.assertEqual(len(seen_headers), 2)
        self.assertIn("referer", seen_headers[0])
        self.assertNotIn("referer", seen_headers[1])

    def test_video_cover_thumb_urls_for_does_not_mix_mp4_after_images(self):
        res = parse_douyin_payload(
            json.loads(_full_router_html("7708291218199909499").decode("utf-8").split("=", 1)[1].rsplit("</script>", 1)[0]),
            "7708291218199909499",
            Config(),
        )
        store = JobStore()
        job = store.put_result(res)
        sa = job.assets[0]
        self.assertEqual(store.thumb_urls_for(sa), ["https://p3.douyinpic.com/cover.webp", "https://p9.douyinpic.com/cover.jpeg"])


if __name__ == "__main__":
    unittest.main()

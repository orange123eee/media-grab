"""Offline regression tests for Douyin SSR shell fallback, feed API, and ttwid retry."""

import json
import unittest
from unittest.mock import patch

from media_grab.config import Config
from media_grab.extractors import ExtractionError
from media_grab.extractors import douyin
from media_grab.extractors.douyin import (
    _TTWID_CACHE,
    _get_anonymous_ttwid,
    _register_ttwid,
    extract_douyin,
)
from media_grab.http_client import HttpResponse


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
                                "play_addr": {
                                    "url_list": [
                                        f"https://aweme.snssdk.com/aweme/v1/playwm/?video_id=vid_{aweme_id}&ratio=720p&line=0"
                                    ]
                                }
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
        _TTWID_CACHE["value"] = ""
        _TTWID_CACHE["expires_at"] = 0.0

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

        res = extract_douyin("https://v.douyin.com/JIUTdKyjptw/", FakeClient(), Config())
        self.assertEqual(res.item_id, target_id)
        self.assertEqual(res.title, "带我远离寂寞的边缘")
        self.assertEqual(res.author, "73")
        self.assertEqual(len(calls), 2)
        self.assertIn("/aweme/v1/feed/", calls[1][0])
        self.assertIn("https://v5-jltc.douyinvod.com/hd1200.mp4", res.assets[0].urls[0])

    def test_feed_missing_target_id_falls_back_to_ttwid_share_retry(self):
        target_id = "7137608061266464030"
        calls = []

        class FakeClient:
            def cookie_for_url(self, url):
                return "foo=bar"

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                if "v.douyin.com" in url:
                    return HttpResponse(
                        200,
                        f"https://www.iesdouyin.com/share/video/{target_id}/?region=CN",
                        {},
                        _shell_router_html(),
                    )
                if "/aweme/v1/feed/" in url:
                    return HttpResponse(
                        200,
                        url,
                        {},
                        json.dumps({"aweme_list": [{"aweme_id": "123", "video": {}}]}).encode("utf-8"),
                    )
                if "iesdouyin.com/share/video/" in url:
                    cookie_hdr = (kwargs.get("headers") or {}).get("Cookie", "")
                    if "ttwid=mock_tok" in cookie_hdr:
                        return HttpResponse(200, url, {}, _full_router_html(target_id, "老视频回归"))
                    return HttpResponse(200, url, {}, _shell_router_html())
                raise AssertionError(f"Unexpected URL: {url}")

        with patch.object(douyin, "_register_ttwid", return_value="mock_tok") as reg_mock:
            res = extract_douyin("https://v.douyin.com/oldwork/", FakeClient(), Config())

        reg_mock.assert_called_once()
        self.assertEqual(res.item_id, target_id)
        self.assertEqual(res.title, "老视频回归")
        self.assertIn("ttwid=mock_tok", calls[-1][1]["headers"]["Cookie"])
        self.assertIn("foo=bar", calls[-1][1]["headers"]["Cookie"])
        self.assertTrue(any("/play/?" in u for u in res.assets[0].urls))

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

    def test_ttwid_cache_reused_within_ttl(self):
        cfg = Config()
        with patch.object(douyin, "_register_ttwid", side_effect=["tok1", "tok2"]) as reg_mock:
            self.assertEqual(_get_anonymous_ttwid(cfg), "tok1")
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


if __name__ == "__main__":
    unittest.main()

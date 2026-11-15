"""Offline unit tests for media_grab parsing, URL extraction, cookies, and download validation."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from media_grab.cli import _build_stem, main as cli_main, run_grab, save_cookie_to_config
from media_grab.config import Config, load_config
from media_grab.extractors import ExtractResult, ExtractionError, MediaAsset, extract
from media_grab.extractors.douyin import _parse_douyin_json, extract_douyin, parse_douyin_payload
from media_grab.extractors.xiaohongshu import _parse_xhs_state, extract_xiaohongshu, parse_xhs_state
from media_grab.http_client import HttpClient, HttpError, HttpResponse
from media_grab.utils import (
    detect_platform,
    extract_balanced_json,
    extract_url,
    guess_ext_from_bytes,
    loads_relaxed_json,
    sanitize_filename,
)


class UtilsTests(unittest.TestCase):
    def test_extract_url_from_chinese_share_copy(self):
        dy_copy = "5.82 复制打开抖音，看看【测试作品】效果 https://v.douyin.com/iRNBxyz/复制此链接，打开Dou音搜索！"
        self.assertEqual(extract_url(dy_copy), "https://v.douyin.com/iRNBxyz/")

        xhs_copy = "小红书好物分享 http://xhslink.com/a/abc123XYZ，快来看看吧！"
        self.assertEqual(extract_url(xhs_copy), "http://xhslink.com/a/abc123XYZ")

    def test_extract_url_preserves_xsec_query(self):
        url = "https://www.xiaohongshu.com/explore/65abc123?xsec_token=AB123==&xsec_source=pc_feed"
        self.assertEqual(extract_url(f"标题 {url}"), url)

    def test_detect_platform(self):
        self.assertEqual(detect_platform("https://v.douyin.com/abc/"), "douyin")
        self.assertEqual(detect_platform("https://www.iesdouyin.com/share/video/123/"), "douyin")
        self.assertEqual(detect_platform("http://xhslink.com/a/xyz"), "xiaohongshu")
        self.assertEqual(detect_platform("https://xhslink.cn/o/1ZBQIcZPzoN"), "xiaohongshu")
        self.assertEqual(detect_platform("https://www.xiaohongshu.com/explore/123"), "xiaohongshu")
        self.assertEqual(detect_platform("https://x.com/user/status/1"), "x")
        self.assertIsNone(detect_platform("https://example.com/1"))

    def test_extract_balanced_and_relaxed_json(self):
        html = (
            '<script>window.__INITIAL_STATE__={"title":"bad undefined } text",'
            '"flag":!0,"off":!1,"missing":undefined,"score":NaN}</script>'
        )
        raw = extract_balanced_json(html, r"window\.__INITIAL_STATE__\s*=")
        self.assertIsNotNone(raw)
        data = loads_relaxed_json(raw)
        self.assertEqual(data["title"], "bad undefined } text")
        self.assertIs(data["flag"], True)
        self.assertIs(data["off"], False)
        self.assertIsNone(data["missing"])
        self.assertIsNone(data["score"])

    def test_guess_ext_from_bytes(self):
        self.assertEqual(guess_ext_from_bytes(b"\xff\xd8\xff\xe0test"), ".jpg")
        self.assertEqual(guess_ext_from_bytes(b"\x89PNG\r\n\x1a\nxxxx"), ".png")
        self.assertEqual(guess_ext_from_bytes(b"RIFF1234WEBPxxxx"), ".webp")
        self.assertEqual(guess_ext_from_bytes(b"\x00\x00\x00\x18ftypisom"), ".mp4")
        self.assertEqual(guess_ext_from_bytes(b"\x00\x00\x00\x18ftypqt  "), ".mov")

    def test_sanitize_filename_and_stem(self):
        self.assertEqual(sanitize_filename('a/b:c?"<>|\n\t #tag'), "a_b_c_tag")
        stem = _build_stem("douyin", "测试标题", "712345", 2, 5, "live_video")
        self.assertEqual(stem, "douyin_测试标题_712345_02_live")


class DouyinExtractorTests(unittest.TestCase):
    def test_parse_router_data_video_picks_highest_bitrate_and_strips_wm(self):
        payload = {
            "loaderData": {
                "video_(id)/page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": "7300112233",
                                "desc": "航拍风景测试",
                                "author": {"nickname": "测试作者"},
                                "video": {
                                    "play_addr": {
                                        "uri": "v0200fg10000",
                                        "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0200fg10000&ratio=720p"],
                                    },
                                    "bit_rate": [
                                        {
                                            "bit_rate": 1200000,
                                            "play_addr": {
                                                "width": 720,
                                                "height": 1280,
                                                "url_list": ["https://v3.douyinvod.com/low.mp4"],
                                            },
                                        },
                                        {
                                            "bit_rate": 4500000,
                                            "play_addr": {
                                                "width": 1080,
                                                "height": 1920,
                                                "url_list": ["https://v3.douyinvod.com/playwm/high1080.mp4?watermark=1"],
                                            },
                                        },
                                    ],
                                },
                            }
                        ]
                    }
                }
            }
        }
        html = f'<script id="_ROUTER_DATA" type="application/json">{json.dumps(payload)}</script>'
        parsed = _parse_douyin_json(html)
        res = parse_douyin_payload(parsed, "", Config())
        self.assertEqual(res.platform, "douyin")
        self.assertEqual(res.item_id, "7300112233")
        self.assertEqual(len(res.assets), 1)
        urls = res.assets[0].urls
        self.assertEqual(urls[0], "https://v3.douyinvod.com/play/high1080.mp4?watermark=0")
        self.assertIn("https://aweme.snssdk.com/aweme/v1/play/?video_id=v0200fg10000&ratio=720p", urls)
        self.assertTrue(any("improve_bitrate=1" in u for u in urls))

    def test_parse_render_data_images_and_live_photo(self):
        payload = {
            "app": {
                "videoDetail": {
                    "aweme_id": "7300998877",
                    "desc": "图集实况",
                    "images": [
                        {
                            "url_list": [
                                "https://p3.douyinpic.com/img/a.webp",
                                "https://p9.douyinpic.com/img/a.jpeg",
                            ],
                            "video": {
                                "play_addr": {
                                    "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=live1"]
                                }
                            },
                        }
                    ],
                }
            }
        }
        html = f'<script id="RENDER_DATA" type="application/json">{quote(json.dumps(payload))}</script>'
        parsed = _parse_douyin_json(html)
        res = parse_douyin_payload(parsed, "", Config(save_live_photo_video=True))
        self.assertEqual([a.kind for a in res.assets], ["image", "live_video"])
        self.assertEqual(res.assets[0].urls[0], "https://p9.douyinpic.com/img/a.jpeg")
        self.assertEqual(res.assets[0].thumb_urls[0], "https://p3.douyinpic.com/img/a.webp")
        self.assertEqual(res.assets[1].urls[0], "https://aweme.snssdk.com/aweme/v1/play/?video_id=live1")

    def test_x_platform_pending_error(self):
        cfg = Config()
        with self.assertRaises(ExtractionError):
            extract("https://x.com/elonmusk/status/123", HttpClient(cfg), cfg)

    def test_douyin_fallbacks_to_iesdouyin_share_page(self):
        payload = {
            "loaderData": {
                "video_(id)/page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": "7300112233445566778",
                                "desc": "回退成功",
                                "video": {
                                    "play_addr": {
                                        "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc"]
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
        share_html = f'<script id="_ROUTER_DATA" type="application/json">{json.dumps(payload)}</script>'.encode("utf-8")

        class FakeClient:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(url)
                if len(self.calls) == 1:
                    return HttpResponse(200, "https://www.douyin.com/video/7300112233445566778", {}, b"<html>no router data</html>")
                return HttpResponse(200, url, {}, share_html)

        fc = FakeClient()
        res = extract_douyin("https://v.douyin.com/short/", fc, Config())
        self.assertEqual(len(fc.calls), 2)
        self.assertEqual(fc.calls[1], "https://www.iesdouyin.com/share/video/7300112233445566778/")
        self.assertEqual(res.title, "回退成功")


class XiaohongshuExtractorTests(unittest.TestCase):
    def test_parse_xhs_images_watermark_free_priority_and_live_photo(self):
        state = {
            "note": {
                "noteDetailMap": {
                    "65abc123": {
                        "note": {
                            "noteId": "65abc123",
                            "type": "normal",
                            "title": "极简摄影",
                            "user": {"nickname": "作者A"},
                            "imageList": [
                                {
                                    "traceId": "1040g00830t2hgqelk4005o49b2u097vri7c1ij8",
                                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/2026/1040g00830t2hgqelk4005o49b2u097vri7c1ij8!nd_dft_wlteh_webp_3",
                                    "livePhoto": True,
                                    "stream": {
                                        "h264": [{"masterUrl": "//sns-video-bd.xhscdn.com/live.mp4", "width": 1080, "height": 1440}]
                                    },
                                }
                            ],
                        }
                    }
                }
            }
        }
        html = f"<script>window.__INITIAL_STATE__={json.dumps(state)};</script>"
        parsed = _parse_xhs_state(html)
        res = parse_xhs_state(parsed, "65abc123", Config())
        self.assertEqual([a.kind for a in res.assets], ["image", "live_video"])
        img_urls = res.assets[0].urls
        self.assertEqual(
            img_urls[0],
            "https://sns-img-hw.xhscdn.com/1040g00830t2hgqelk4005o49b2u097vri7c1ij8?imageView2/2/w/format/png",
        )
        self.assertEqual(res.assets[1].urls[0], "https://sns-video-bd.xhscdn.com/live.mp4")

        # Newer XHS schema: traceId is empty, key is in fileId and urlDefault has timestamp+md5 prefix.
        signed_state = {
            "note": {
                "noteDetailMap": {
                    "6ae88286": {
                        "note": {
                            "noteId": "6ae88286",
                            "type": "normal",
                            "title": "新版原图测试",
                            "imageList": [
                                {
                                    "traceId": "",
                                    "fileId": "1040g2sg3272o559176hg5pvasid1i1niileucv8",
                                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/202611151726/0d1593714808ab35a7e1f1ca309afc31/1040g2sg3272o559176hg5pvasid1i1niileucv8!nd_dft_wgth_jpg_3",
                                }
                            ],
                        }
                    }
                }
            }
        }
        res_signed = parse_xhs_state(signed_state, "6ae88286", Config())
        signed_urls = res_signed.assets[0].urls
        self.assertEqual(
            signed_urls[0],
            "https://sns-img-hw.xhscdn.com/1040g2sg3272o559176hg5pvasid1i1niileucv8?imageView2/2/w/format/png",
        )
        self.assertFalse(any("0d1593714808ab35a7e1f1ca309afc31" in u and "sns-img-hw" in u for u in signed_urls))
        self.assertFalse(any("sns-webpic-qc.xhscdn.com" in u and "!nd_" not in u for u in signed_urls))
        self.assertEqual(
            res_signed.assets[0].thumb_urls[0],
            "https://sns-img-hw.xhscdn.com/1040g2sg3272o559176hg5pvasid1i1niileucv8?imageView2/2/w/720/format/webp",
        )

    def test_parse_xhs_video_picks_highest_resolution_and_skips_cover_image(self):
        state = {
            "note": {
                "noteDetailMap": {
                    "65vid999": {
                        "note": {
                            "noteId": "65vid999",
                            "type": "video",
                            "title": "4K测试视频",
                            "imageList": [{"traceId": "cover_only", "urlDefault": "https://ci.xiaohongshu.com/cover.jpg"}],
                            "video": {
                                "media": {
                                    "stream": {
                                        "h264": [
                                            {"masterUrl": "https://v.xhscdn.com/720.mp4", "width": 720, "height": 1280, "videoBitrate": 1500},
                                            {"masterUrl": "https://v.xhscdn.com/1080.mp4", "width": 1080, "height": 1920, "videoBitrate": 4000},
                                        ]
                                    }
                                }
                            },
                        }
                    }
                }
            }
        }
        res = parse_xhs_state(state, "65vid999", Config())
        self.assertEqual(len(res.assets), 1)
        self.assertEqual(res.assets[0].kind, "video")
        self.assertEqual(res.assets[0].urls[0], "https://v.xhscdn.com/1080.mp4")

    def test_xhs_captcha_raises_actionable_error(self):
        class FakeClient:
            def get(self, url, **kwargs):
                return HttpResponse(200, "https://www.xiaohongshu.com/404", {}, "<html>安全限制</html>".encode("utf-8"))

        with self.assertRaises(ExtractionError) as ctx:
            extract_xiaohongshu("http://xhslink.com/a/123", FakeClient(), Config())
        self.assertIn("xhs_cookie", str(ctx.exception))


class ConfigAndDownloadTests(unittest.TestCase):
    def test_config_and_netscape_cookie_scoping(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            cookie_txt = cfg_dir / "cookies.txt"
            cookie_txt.write_text(
                "# Netscape HTTP Cookie File\n"
                ".xiaohongshu.com\tTRUE\t/\tTRUE\t0\ta1\txhs_val\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\tdy_val\n",
                encoding="utf-8",
            )
            save_cookie_to_config("douyin", "sessionid=override_dy", config_dir=cfg_dir)
            cfg = load_config(config_dir=cfg_dir)
            client = HttpClient(cfg)
            self.assertEqual(client.cookie_for_url("https://www.douyin.com/video/1"), "sessionid=override_dy")
            self.assertEqual(client.cookie_for_url("https://www.xiaohongshu.com/explore/1"), "a1=xhs_val")
            self.assertEqual(client.cookie_for_url("https://example.com/"), "")

    def test_download_candidates_falls_back_when_first_returns_html(self):
        class FakeResp:
            def __init__(self, ct: str, payload: bytes):
                self.headers = {"Content-Type": ct}
                self._bio = io.BytesIO(payload)

            def read(self, n=-1):
                return self._bio.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        html_block = FakeResp("text/html; charset=utf-8", b"<html>blocked</html>")
        good_png = FakeResp("image/png", b"\x89PNG\r\n\x1a\n" + b"A" * 2048)

        class FakeOpener:
            calls = 0

            def open(self, req, timeout=10):
                FakeOpener.calls += 1
                return html_block if FakeOpener.calls == 1 else good_png

        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(Config(output_dir=Path(tmp)))
            with patch("urllib.request.build_opener", return_value=FakeOpener()):
                saved = client.download_candidates(
                    ["https://bad.example/1", "https://good.example/1"],
                    dest_dir=Path(tmp),
                    stem="xhs_test_1",
                    default_ext=".jpg",
                    expected_prefix="image",
                )
            self.assertEqual(saved.name, "xhs_test_1.png")
            self.assertTrue(saved.is_file())
            self.assertGreater(saved.stat().st_size, 2048)

    def test_download_candidates_raises_if_all_fail(self):
        class FakeOpener:
            def open(self, req, timeout=10):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(Config(output_dir=Path(tmp)))
            with patch("urllib.request.build_opener", return_value=FakeOpener()):
                with self.assertRaises(HttpError):
                    client.download_candidates(
                        ["https://bad.example/1"],
                        dest_dir=Path(tmp),
                        stem="fail",
                        default_ext=".mp4",
                        expected_prefix="video",
                    )

    def test_run_grab_downloads_and_triggers_media_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "DCIM"
            fake_result = ExtractResult(
                platform="xiaohongshu",
                item_id="note1",
                title="风景",
                author="u",
                assets=[MediaAsset(kind="image", urls=["https://cdn.example/1.png"], default_ext=".png")],
            )
            saved_file = out_dir / "xiaohongshu_风景_note1.png"
            with (
                patch("media_grab.cli.load_config", return_value=Config(output_dir=out_dir)),
                patch("media_grab.cli.extract", return_value=fake_result),
                patch.object(HttpClient, "download_candidates", return_value=saved_file),
                patch("media_grab.cli.android.scan_media") as mock_scan,
            ):
                ok, msg = run_grab("快看 http://xhslink.com/a/xyz")
            self.assertTrue(ok)
            self.assertIn("1/1", msg)
            mock_scan.assert_called_once_with([saved_file])

    def test_cli_falls_back_to_interactive_paste_when_clipboard_empty(self):
        with (
            patch("media_grab.cli.android.get_clipboard", return_value=""),
            patch("media_grab.cli.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="http://xhslink.com/a/xyz"),
            patch("media_grab.cli.run_grab", return_value=(True, "ok")) as mock_run,
            patch("media_grab.cli.android.notify"),
        ):
            code = cli_main(["--clipboard"])
        self.assertEqual(code, 0)
        mock_run.assert_called_once_with("http://xhslink.com/a/xyz")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""CLI entrypoint shared by Termux share menu (termux-url-opener) and desktop widget."""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple

from . import android
from .config import DEFAULT_CONFIG_DIR, load_config
from .extractors import ExtractionError, extract
from .http_client import HttpClient, HttpError
from .utils import extract_url, sanitize_filename


def _build_stem(platform: str, title: str, item_id: str, index: int, total: int, kind: str) -> str:
    safe_title = sanitize_filename(title, fallback=platform, max_len=48)
    short_id = sanitize_filename(item_id, fallback="id", max_len=16)
    base = f"{platform}_{safe_title}_{short_id}"
    suffix = f"_{index:02d}" if total > 1 else ""
    if kind == "live_video":
        suffix += "_live"
    return f"{base}{suffix}"


def save_cookie_to_config(platform: str, cookie_value: str, config_dir: Path | None = None) -> Path:
    """Persist a cookie string for douyin or xhs into config.json."""
    load_config(config_dir)
    cfg_dir = config_dir or Path(os.environ.get("MEDIA_GRAB_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    cfg_file = cfg_dir / "config.json"
    data = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
    key = "douyin_cookie" if platform == "douyin" else "xhs_cookie"
    data[key] = cookie_value.strip()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg_file


def run_grab(raw_input: str) -> Tuple[bool, str]:
    """Parse share text/URL, download all media assets, refresh gallery, and return status message."""
    url = extract_url(raw_input)
    if not url:
        return False, "没有在输入或剪贴板里找到有效链接。"

    config = load_config()
    client = HttpClient(config)

    print(f"[media-grab] 识别到链接: {url}")
    try:
        result = extract(url, client, config)
    except (ExtractionError, HttpError) as e:
        return False, str(e)
    except Exception as e:
        return False, f"解析异常: {e}"

    total = len(result.assets)
    print(f"[media-grab] 平台={result.platform} | 标题={result.title} | 资源数={total}")

    saved_paths: List[Path] = []
    errors: List[str] = []

    def _download_idx(idx: int) -> Path:
        asset = result.assets[idx]
        expected = "video" if asset.kind in ("video", "live_video") else "image"
        stem = _build_stem(result.platform, result.title, result.item_id, idx + 1, total, asset.kind)
        return client.download_candidates(
            asset.urls,
            dest_dir=config.output_dir,
            stem=stem,
            default_ext=asset.default_ext,
            expected_prefix=expected,
            headers=asset.headers,
            mobile=asset.mobile,
        )

    workers = min(config.max_workers, max(1, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_idx, i): i for i in range(total)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                path = fut.result()
                saved_paths.append(path)
                print(f"  ✓ [{idx + 1}/{total}] {path.name}")
            except Exception as e:
                errors.append(f"第{idx + 1}项: {e}")
                print(f"  ✗ [{idx + 1}/{total}] {e}", file=sys.stderr)

    if saved_paths:
        android.scan_media(saved_paths)

    if not saved_paths:
        return False, f"下载全部失败: {'; '.join(errors)}"

    summary = f"已保存 {len(saved_paths)}/{total} 个文件到 {config.output_dir.name}"
    if errors:
        summary += f"（{len(errors)} 个失败）"
    return True, summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取抖音/小红书无水印最高画质视频和图片")
    parser.add_argument("input", nargs="?", help="分享文案或链接")
    parser.add_argument("--clipboard", action="store_true", help="从安卓剪贴板读取分享文案/链接")
    parser.add_argument("--no-notify", action="store_true", help="不发送 Termux 通知")
    parser.add_argument("--save-cookie", choices=["douyin", "xhs"], help="从剪贴板读取并保存指定平台的 Cookie")
    parser.add_argument("--cookie-value", help="配合 --save-cookie 直接传入 Cookie 字符串")
    args = parser.parse_args(argv)

    if args.save_cookie:
        val = (args.cookie_value or android.get_clipboard()).strip()
        if not val:
            print("[media-grab] 剪贴板为空，请先复制 Cookie 再执行。", file=sys.stderr)
            return 1
        cfg_file = save_cookie_to_config(args.save_cookie, val)
        print(f"[media-grab] 已更新 {args.save_cookie} Cookie → {cfg_file}")
        return 0

    raw = args.input or ""
    if args.clipboard or not raw.strip():
        raw = android.get_clipboard()

    if not raw.strip() and sys.stdin.isatty():
        try:
            raw = input("未读到剪贴板内容，请直接长按粘贴链接后按回车: ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""

    if not raw.strip():
        msg = "未收到链接，剪贴板也为空。请先复制抖音/小红书分享链接，或通过系统「分享 → Termux」传入。"
        print(f"[media-grab] {msg}", file=sys.stderr)
        if not args.no_notify:
            android.notify("提取失败", msg)
        return 1

    ok, msg = run_grab(raw)
    print(f"[media-grab] {msg}")
    if not args.no_notify:
        android.notify("提取完成 ✓" if ok else "提取失败", msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

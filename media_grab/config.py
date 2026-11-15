"""Configuration and cookie loading for media_grab."""

import json
import os
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Dict


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "media-grab"
DEFAULT_TERMUX_OUTPUT = Path.home() / "storage" / "shared" / "DCIM" / "MediaGrab"


@dataclass
class Config:
    """Runtime settings loaded from config.json and environment overrides."""

    output_dir: Path = field(default_factory=lambda: DEFAULT_TERMUX_OUTPUT)
    timeout: int = 25
    max_workers: int = 4
    save_live_photo_video: bool = True
    douyin_cookie: str = ""
    xhs_cookie: str = ""
    cookies_by_domain: Dict[str, str] = field(default_factory=dict)


def _default_output_dir() -> Path:
    if (Path.home() / "storage" / "shared").exists():
        return DEFAULT_TERMUX_OUTPUT
    return Path.home() / "Downloads" / "MediaGrab"


def _parse_netscape_cookies(path: Path) -> Dict[str, str]:
    """Read a Netscape cookies.txt file into domain -> 'k=v; ...' mapping."""
    if not path.is_file():
        return {}
    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return {}

    grouped: Dict[str, list] = {}
    for cookie in jar:
        dom = (cookie.domain or "").lstrip(".").lower()
        if not dom:
            continue
        grouped.setdefault(dom, []).append(f"{cookie.name}={cookie.value}")
    return {dom: "; ".join(pairs) for dom, pairs in grouped.items()}


def load_config(config_dir: Path | None = None) -> Config:
    """Load config.json (creating defaults if missing) and optional cookies.txt files."""
    cfg_dir = config_dir or Path(os.environ.get("MEDIA_GRAB_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    cfg_file = cfg_dir / "config.json"

    raw = {}
    if cfg_file.is_file():
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    else:
        try:
            cfg_dir.mkdir(parents=True, exist_ok=True)
            template = {
                "output_dir": str(_default_output_dir()),
                "timeout": 25,
                "max_workers": 4,
                "save_live_photo_video": True,
                "douyin_cookie": "",
                "xhs_cookie": "",
            }
            cfg_file.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    out_str = raw.get("output_dir") or str(_default_output_dir())
    out_dir = Path(os.path.expanduser(out_str))

    cookies_by_domain: Dict[str, str] = {}
    for name in ("cookies.txt", "douyin_cookies.txt", "xhs_cookies.txt"):
        cookies_by_domain.update(_parse_netscape_cookies(cfg_dir / name))

    return Config(
        output_dir=out_dir,
        timeout=int(raw.get("timeout", 25)),
        max_workers=max(1, min(8, int(raw.get("max_workers", 4)))),
        save_live_photo_video=bool(raw.get("save_live_photo_video", True)),
        douyin_cookie=str(raw.get("douyin_cookie", "")).strip(),
        xhs_cookie=str(raw.get("xhs_cookie", "")).strip(),
        cookies_by_domain=cookies_by_domain,
    )

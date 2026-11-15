"""Termux Android integration helpers (gracefully no-op outside Termux)."""

import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional


def has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def get_clipboard() -> str:
    """Read Android clipboard via Termux:API."""
    if not has_cmd("termux-clipboard-get"):
        return ""
    try:
        res = subprocess.run(
            ["termux-clipboard-get"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return (res.stdout or "").strip()
    except Exception:
        return ""


def scan_media(paths: Iterable[Path]) -> None:
    """Trigger Android media store scan so saved files appear immediately in Gallery."""
    if not has_cmd("termux-media-scan"):
        return
    file_args = [str(p) for p in paths if p and p.exists()]
    if not file_args:
        return
    try:
        subprocess.run(["termux-media-scan", *file_args], capture_output=True, timeout=15)
    except Exception:
        pass


def notify(title: str, content: str, notif_id: str = "media_grab") -> None:
    """Show Android notification when running in Termux."""
    if has_cmd("termux-notification"):
        try:
            subprocess.run(
                [
                    "termux-notification",
                    "--id",
                    notif_id,
                    "--title",
                    title,
                    "--content",
                    content,
                ],
                capture_output=True,
                timeout=5,
            )
            return
        except Exception:
            pass
    if has_cmd("termux-toast"):
        try:
            subprocess.run(["termux-toast", f"{title}: {content}"], capture_output=True, timeout=5)
        except Exception:
            pass


def toast(message: str) -> None:
    if has_cmd("termux-toast"):
        try:
            subprocess.run(["termux-toast", message], capture_output=True, timeout=5)
        except Exception:
            pass

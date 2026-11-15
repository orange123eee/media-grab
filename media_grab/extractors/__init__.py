"""Common extractor data models and platform router."""

from dataclasses import dataclass, field
from typing import Dict, List

from ..config import Config
from ..http_client import HttpClient
from ..utils import detect_platform


class ExtractionError(RuntimeError):
    """Raised when a share link cannot be parsed into downloadable media."""


@dataclass
class MediaAsset:
    """Single downloadable media item with fallback candidate URLs ordered best-first."""

    kind: str  # "video" | "image" | "live_video"
    urls: List[str]
    default_ext: str
    headers: Dict[str, str] = field(default_factory=dict)
    mobile: bool = False


@dataclass
class ExtractResult:
    """Parsed post/note metadata and extracted media assets."""

    platform: str
    item_id: str
    title: str
    author: str
    assets: List[MediaAsset]


def extract(url: str, client: HttpClient, config: Config) -> ExtractResult:
    """Route URL to the matching platform extractor."""
    platform = detect_platform(url)
    if platform == "douyin":
        from .douyin import extract_douyin

        return extract_douyin(url, client, config)
    if platform == "xiaohongshu":
        from .xiaohongshu import extract_xiaohongshu

        return extract_xiaohongshu(url, client, config)
    if platform == "x":
        raise ExtractionError("X (Twitter) 提取将在下一阶段接入，目前优先支持抖音和小红书。")
    raise ExtractionError(f"暂不支持该链接所属平台: {url}")

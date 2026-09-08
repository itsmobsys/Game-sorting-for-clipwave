"""ClipWave YouTube game detector: video-ID -> cached game metadata resolver."""
from __future__ import annotations

from typing import Optional

from .cache import TwoLevelCache
from .detector import YouTubeGameDetector, is_valid_video_id
from .extractor import extract_game

__all__ = [
    "YouTubeGameDetector",
    "TwoLevelCache",
    "extract_game",
    "is_valid_video_id",
    "get_game",
    "aget_game",
    "invalidate",
    "clear_cache",
]

__version__ = "1.0.0"

_default_detector: Optional[YouTubeGameDetector] = None


def _default() -> YouTubeGameDetector:
    global _default_detector
    if _default_detector is None:
        _default_detector = YouTubeGameDetector()
    return _default_detector


def get_game(video_id: str) -> Optional[str]:
    """Return the game name for ``video_id`` or ``None`` (module shortcut)."""
    return _default().get_game(video_id)


async def aget_game(video_id: str) -> Optional[str]:
    """Async variant of :func:`get_game` (module shortcut)."""
    return await _default().aget_game(video_id)


def invalidate(video_id: str) -> None:
    _default().invalidate(video_id)


def clear_cache() -> None:
    _default().clear_cache()

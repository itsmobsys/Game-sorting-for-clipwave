"""Micro-benchmark: first fetch vs. cached lookups (mocked HTTP, no network).

Run:  python -m youtube_game_detector.benchmark
"""
from __future__ import annotations

import time

from .detector import YouTubeGameDetector

PHASMO_HTML = (
    '<div class="ytVideoAttributeViewModelMetadata" role="link">'
    '<div class="ytVideoAttributeViewModelTextContainer">'
    '<h1 class="ytVideoAttributeViewModelTitle">Phasmophobia</h1>'
    '<h4 class="ytVideoAttributeViewModelSubtitle"><span>2020</span></h4>'
    "</div></div>"
)
VID = "dQw4w9WgXcQ"
N = 20000


def main() -> None:
    fetches = []

    def fetcher(video_id: str):
        fetches.append(video_id)
        time.sleep(0.005)  # simulate one ~5ms HTTP round-trip
        return PHASMO_HTML

    d = YouTubeGameDetector(fetcher=fetcher, db_path=None)

    t0 = time.perf_counter()
    assert d.get_game(VID) == "Phasmophobia"
    first_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        assert d.get_game(VID) == "Phasmophobia"
    total = time.perf_counter() - t0
    per_call_ns = total / N * 1e9

    print(f"first request (fetch+parse+store): {first_ms:.2f} ms")
    print(f"{N} cached lookups: {total * 1000:.1f} ms total, {per_call_ns:.0f} ns/call")
    print(f"HTTP fetches performed: {len(fetches)} (expected 1)")
    print(f"cache stats: {d.stats()}")
    assert len(fetches) == 1, "cached path must make ZERO HTTP requests"

    # L1 vs L2: evict L1, time one SQLite-backed read, then L1 again.
    d2 = YouTubeGameDetector(fetcher=fetcher, db_path=":memory:")
    d2.get_game(VID)
    d2.cache._l1.clear()
    t0 = time.perf_counter()
    d2.get_game(VID)
    l2_us = (time.perf_counter() - t0) * 1e6
    t0 = time.perf_counter()
    d2.get_game(VID)
    l1_us = (time.perf_counter() - t0) * 1e6
    print(f"L2 (sqlite) hit: {l2_us:.1f} us, L1 (memory) hit: {l1_us:.1f} us")
    d.close()
    d2.close()


if __name__ == "__main__":
    main()

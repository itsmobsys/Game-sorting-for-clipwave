"""Detector unit tests: mocked fetch only, never touches YouTube."""
import asyncio
import threading

from youtube_game_detector.detector import YouTubeGameDetector, is_valid_video_id

PHASMO_HTML = (
    '<div class="ytVideoAttributeViewModelMetadata" role="link">'
    '<div class="ytVideoAttributeViewModelTextContainer">'
    '<h1 class="ytVideoAttributeViewModelTitle">Phasmophobia</h1>'
    '<h4 class="ytVideoAttributeViewModelSubtitle"><span>2020</span></h4>'
    "</div></div>"
)
NO_GAME_HTML = "<html><body><title>Daily vlog</title>no game here</body></html>"

VID = "dQw4w9WgXcQ"  # 11-char fixture ID
VID2 = "eQw4w9WgXcR"
VID3 = "fQw4w9WgXcS"


def make_detector(fetcher=None, **kw):
    kw.setdefault("db_path", None)
    return YouTubeGameDetector(fetcher=fetcher, **kw)


def test_valid_and_invalid_video_ids():
    assert is_valid_video_id(VID)
    assert is_valid_video_id("AbC123_-xYz")
    for bad in ("", "short", "waytoolongvideoid123", "dQw4w9WgXc!", None, 123, "dQw4w9WgXc "):
        assert not is_valid_video_id(bad)


def test_invalid_id_makes_zero_http_requests():
    calls = []

    def fetcher(vid):
        calls.append(vid)
        return PHASMO_HTML

    d = make_detector(fetcher)
    assert d.get_game("bogus") is None
    assert d.get_game("") is None
    assert d.get_game(None) is None  # type: ignore[arg-type]
    assert calls == []


def test_extract_phasmophobia_via_detector():
    d = make_detector(lambda vid: PHASMO_HTML)
    assert d.get_game(VID) == "Phasmophobia"


def test_missing_metadata_returns_none():
    d = make_detector(lambda vid: NO_GAME_HTML)
    assert d.get_game(VID) is None


def test_cached_results_make_zero_http_requests():
    calls = []

    def fetcher(vid):
        calls.append(vid)
        return PHASMO_HTML

    d = make_detector(fetcher)
    assert d.get_game(VID) == "Phasmophobia"
    assert len(calls) == 1
    for _ in range(100):
        assert d.get_game(VID) == "Phasmophobia"
    assert len(calls) == 1  # exactly one fetch; rest is L1 memory lookup


def test_negative_result_cached_no_refetch():
    calls = []

    def fetcher(vid):
        calls.append(vid)
        return NO_GAME_HTML

    d = make_detector(fetcher, negative_ttl=1000)
    assert d.get_game(VID) is None
    for _ in range(10):
        assert d.get_game(VID) is None
    assert len(calls) == 1


def test_request_failure_returns_none_and_does_not_crash():
    d = make_detector(lambda vid: (_ for _ in ()).throw(RuntimeError("boom")))
    assert d.get_game(VID) is None
    d2 = make_detector(lambda vid: None)  # HTTP error / empty body
    assert d2.get_game(VID) is None
    d3 = make_detector(lambda vid: "<html>garbage{{{</html>")
    assert d3.get_game(VID) is None


def test_malformed_html_returns_none():
    d = make_detector(lambda vid: "<h1 class=broken")
    assert d.get_game(VID) is None


def test_duplicate_concurrent_requests_coalesced():
    gate = threading.Event()
    calls = []

    def slow_fetcher(vid):
        calls.append(vid)
        assert gate.wait(timeout=10)
        return PHASMO_HTML

    d = make_detector(slow_fetcher, max_concurrency=8)
    results = [None] * 12

    def worker(i):
        results[i] = d.get_game(VID)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    gate.set()  # release the single fetch; all waiters share it
    for t in threads:
        t.join(timeout=15)
    assert results == ["Phasmophobia"] * 12
    assert len(calls) == 1  # only ONE fetch for 12 simultaneous callers


def test_distinct_ids_each_fetch_once():
    calls = []

    def fetcher(vid):
        calls.append(vid)
        return PHASMO_HTML

    d = make_detector(fetcher, max_concurrency=4)
    out = d.get_many([VID, VID2, VID3, VID, VID2])
    assert out == {VID: "Phasmophobia", VID2: "Phasmophobia", VID3: "Phasmophobia"}
    assert sorted(calls) == sorted([VID, VID2, VID3])


def test_negative_cache_expiry_refetches():
    import time

    calls = []

    def fetcher(vid):
        calls.append(vid)
        return NO_GAME_HTML if len(calls) == 1 else PHASMO_HTML

    d = make_detector(fetcher, negative_ttl=0.05, positive_ttl=1000)
    assert d.get_game(VID) is None
    time.sleep(0.07)
    assert d.get_game(VID) == "Phasmophobia"  # transient miss, then recovered
    assert len(calls) == 2


def test_sqlite_l2_shared_between_detectors(tmp_path):
    db = str(tmp_path / "g.db")
    calls = []

    def fetcher(vid):
        calls.append(vid)
        return PHASMO_HTML

    d1 = YouTubeGameDetector(fetcher=fetcher, db_path=db)
    assert d1.get_game(VID) == "Phasmophobia"
    d1.close()
    d2 = YouTubeGameDetector(fetcher=lambda vid: (_ for _ in ()).throw(AssertionError("must not fetch")), db_path=db)
    try:
        assert d2.get_game(VID) == "Phasmophobia"  # served from L2, zero HTTP
    finally:
        d2.close()


def test_invalidate_forces_refetch():
    calls = []

    def fetcher(vid):
        calls.append(vid)
        return PHASMO_HTML

    d = make_detector(fetcher)
    assert d.get_game(VID) == "Phasmophobia"
    d.invalidate(VID)
    assert d.get_game(VID) == "Phasmophobia"
    assert len(calls) == 2


def test_async_get_game_coalesces():
    async def run():
        calls = []

        def fetcher(vid):
            calls.append(vid)
            return PHASMO_HTML

        d = YouTubeGameDetector(fetcher=fetcher)
        # Simulate overlap: start several coroutines for the same new ID.
        results = await asyncio.gather(*[d.aget_game(VID) for _ in range(8)])
        assert results == ["Phasmophobia"] * 8
        assert len(calls) == 1
        # Cached async path: zero extra fetches.
        results = await asyncio.gather(*[d.aget_game(VID) for _ in range(8)])
        assert results == ["Phasmophobia"] * 8
        assert len(calls) == 1

    asyncio.run(run())


def test_module_level_shortcut():
    import youtube_game_detector as ygd

    assert ygd.get_game("not-valid!!") is None  # no network for invalid IDs

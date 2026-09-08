"""L2 expiry-sweep tests: semantics, scheduler, concurrency, idempotency.

All network access is mocked (``fetcher=...``); YouTube is never touched.
Cleanup intervals are injected in seconds (or triggered manually via
:meth:`prune_expired`) -- no test ever waits 6 hours.
"""
import os
import sqlite3
import threading
import time

from youtube_game_detector.cache import (
    DEFAULT_CLEANUP_INTERVAL,
    DEFAULT_NEGATIVE_TTL,
    DEFAULT_POSITIVE_TTL,
    TwoLevelCache,
)
from youtube_game_detector.detector import YouTubeGameDetector

PHASMO_HTML = (
    '<div class="ytVideoAttributeViewModelMetadata" role="link">'
    '<div class="ytVideoAttributeViewModelTextContainer">'
    '<h1 class="ytVideoAttributeViewModelTitle">Phasmophobia</h1>'
    '<h4 class="ytVideoAttributeViewModelSubtitle"><span>2020</span></h4>'
    "</div></div>"
)
NO_GAME_HTML = "<html><body><title>Daily vlog</title>no game here</body></html>"

POS = "dQw4w9WgXcQ"
NEG = "eQw4w9WgXcR"
FRESH = "fQw4w9WgXcS"


def _backdate(db_path, video_id, updated_at):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE game_cache SET updated_at = ? WHERE video_id = ?",
            (updated_at, video_id),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            vid: (game, success, ts)
            for vid, game, success, ts in conn.execute(
                "SELECT video_id, game, success, updated_at FROM game_cache"
            ).fetchall()
        }
    finally:
        conn.close()


def _file_size(db_path):
    return os.path.getsize(db_path) if os.path.exists(db_path) else 0


def test_01_expired_entries_never_returned(tmp_path):
    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=0.05, negative_ttl=1000, cleanup_interval=0
    )
    try:
        c.set(POS, "Phasmophobia")
        c._l1.clear()  # bypass L1: exercise the L2 timestamp check
        time.sleep(0.07)
        assert c.get(POS) == (False, None)
    finally:
        c.close()


def test_02_valid_entries_remain_touchable(tmp_path):
    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=1000, negative_ttl=1000, cleanup_interval=0
    )
    try:
        c.set(POS, "Phasmophobia")
        c.set(NEG, None)
        assert c.prune_expired() == 0
        c._l1.clear()
        assert c.get(POS) == (True, "Phasmophobia")
        assert c.get(NEG) == (True, None)
    finally:
        c.close()


def test_03_cleanup_removes_expired_positive_entries(tmp_path):
    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=0.05, negative_ttl=1000, cleanup_interval=0
    )
    try:
        c.set(POS, "Phasmophobia")
        c.set(FRESH, "Minecraft")
        time.sleep(0.07)
        c.set(FRESH, "Minecraft")  # refresh FRESH past the cutoff
        assert c.prune_expired() == 1
        rows = _rows(db)
        assert POS not in rows
        assert rows[FRESH][0] == "Minecraft"
    finally:
        c.close()


def test_04_cleanup_removes_expired_negative_entries(tmp_path):
    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=1000, negative_ttl=0.05, cleanup_interval=0
    )
    try:
        c.set(NEG, None)
        time.sleep(0.07)
        assert c.prune_expired() == 1
        assert NEG not in _rows(db)
    finally:
        c.close()


def test_05_cleanup_does_not_remove_valid_entries(tmp_path):
    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=1000, negative_ttl=1000, cleanup_interval=0
    )
    try:
        c.set(POS, "Phasmophobia")
        c.set(NEG, None)
        size_before = _file_size(db)
        assert size_before > 0
        assert c.prune_expired() == 0
        rows = _rows(db)
        assert rows[POS][0] == "Phasmophobia"
        assert rows[NEG][0] is None
        # DB file itself untouched: not removed, not recreated (same bytes).
        assert os.path.exists(db)
        assert _file_size(db) == size_before
    finally:
        c.close()


def test_06_cleanup_runs_approximately_every_interval(tmp_path):
    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=0.05, negative_ttl=1000, cleanup_interval=0.15
    )
    try:
        assert c.cleanup_alive
        c.set(POS, "Phasmophobia")
        deadline = time.time() + 5.0
        while POS in _rows(db) and time.time() < deadline:
            time.sleep(0.05)
        assert POS not in _rows(db)  # worker swept it on schedule
        assert c.cleanups_run >= 1
        assert c.last_cleanup_at is not None
        assert c.last_cleanup_removed >= 1
    finally:
        c.close()


def test_07_detector_shutdown_stops_cleanup_worker(tmp_path):
    db = str(tmp_path / "games.db")
    d = YouTubeGameDetector(fetcher=lambda vid: PHASMO_HTML, db_path=db,
                            cleanup_interval=0.1)
    worker = d.cache._cleanup_thread
    assert worker is not None
    assert d.cache.cleanup_alive
    d.close()
    assert not d.cache.cleanup_alive
    worker.join(timeout=5.0)
    assert not worker.is_alive()  # this detector's worker stopped cleanly
    # Second close is safe and the thread stays daemon (never blocks exit).
    d.close()
    assert worker.daemon


def test_08_cleanup_does_not_cause_duplicate_http_requests(tmp_path):
    db = str(tmp_path / "games.db")
    calls = []

    def fetcher(vid):
        calls.append(vid)
        return PHASMO_HTML

    d = YouTubeGameDetector(
        fetcher=fetcher, db_path=db, positive_ttl=0.05,
        negative_ttl=1000, cleanup_interval=0.05,
    )
    try:
        assert d.get_game(POS) == "Phasmophobia"
        assert len(calls) == 1
        time.sleep(0.3)  # sweeps fire repeatedly; entry expires meanwhile
        assert d.get_game(POS) == "Phasmophobia"
        # Exactly 2 fetches: initial + post-expiry refetch. Sweeps never
        # fetch and never duplicate a request.
        assert len(calls) == 2
    finally:
        d.close()


def test_09_concurrent_access_safe_during_cleanup(tmp_path):
    db = str(tmp_path / "games.db")
    d = YouTubeGameDetector(
        fetcher=lambda vid: PHASMO_HTML, db_path=db,
        positive_ttl=0.05, negative_ttl=1000,
        cleanup_interval=0.02, max_concurrency=8,
    )
    try:
        vids = ["dQw4w9WgX%dQ" % i for i in range(10)]
        errors = []

        def worker(i):
            try:
                for _ in range(25):
                    d.get_game(vids[i % len(vids)])
                    d.get_many(vids[:4])
                    d.prune_expired()
            except Exception as exc:  # noqa: BLE001 - must surface
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not [t for t in threads if t.is_alive()]
        assert errors == []
        for v in vids:
            assert d.get_game(v) == "Phasmophobia"
    finally:
        d.close()


def test_10_database_usable_after_cleanup(tmp_path):
    db = str(tmp_path / "games.db")
    d = YouTubeGameDetector(
        fetcher=lambda vid: PHASMO_HTML, db_path=db,
        positive_ttl=0.05, negative_ttl=1000, cleanup_interval=0,
    )
    try:
        assert d.get_game(POS) == "Phasmophobia"
        _backdate(db, POS, time.time() - 1000.0)
        d.cache._l1.clear()
        assert d.get_game(POS) == "Phasmophobia"  # stale -> miss -> refetch
        assert d.prune_expired() == 0  # refetch rewrote it fresh
        d2 = YouTubeGameDetector(
            fetcher=lambda vid: (_ for _ in ()).throw(
                AssertionError("must not fetch")),
            db_path=db, positive_ttl=1000, cleanup_interval=0,
        )
        try:
            assert d2.get_game(POS) == "Phasmophobia"  # L2 intact
        finally:
            d2.close()
    finally:
        d.close()


def test_11_no_vacuum_during_normal_cleanup(tmp_path, caplog):
    # ``sqlite3.Connection`` is immutable, so observe statements at the SQL
    # trace level instead of monkeypatching ``execute``.
    import logging

    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=0.05, negative_ttl=1000, cleanup_interval=0
    )
    try:
        c.set(POS, "Phasmophobia")
        time.sleep(0.07)
        statements = []
        conn = c._ensure_conn()
        assert conn is not None
        conn.set_trace_callback(statements.append)
        try:
            with caplog.at_level(logging.DEBUG, logger="youtube_game_detector.cache"):
                assert c.prune_expired() == 1
        finally:
            conn.set_trace_callback(None)
        assert statements, "expected traced SQL statements"
        assert not [s for s in statements if "VACUUM" in s.upper()]
        assert any("DELETE FROM game_cache WHERE" in s for s in statements)
    finally:
        c.close()


def test_12_repeated_cleanup_is_idempotent(tmp_path):
    db = str(tmp_path / "games.db")
    c = TwoLevelCache(
        db_path=db, positive_ttl=0.05, negative_ttl=0.05, cleanup_interval=0
    )
    try:
        c.set(POS, "Phasmophobia")
        c.set(NEG, None)
        time.sleep(0.07)
        assert c.prune_expired() == 2
        assert c.prune_expired() == 0
        assert c.prune_expired() == 0
        rows = _rows(db)
        assert POS not in rows and NEG not in rows
        assert os.path.exists(db)  # file never removed
    finally:
        c.close()


def test_production_defaults_are_six_hours():
    assert DEFAULT_POSITIVE_TTL == 6 * 3600.0
    assert DEFAULT_NEGATIVE_TTL == 3600.0
    assert DEFAULT_CLEANUP_INTERVAL == 6 * 3600.0
    c = TwoLevelCache(db_path=":memory:")  # worker starts even here
    try:
        assert c.cleanup_interval == 6 * 3600.0
        assert c.cleanup_alive
    finally:
        c.close()
    mem_only = TwoLevelCache(db_path=None)
    try:
        assert not mem_only.cleanup_alive  # no L2 -> no worker thread
    finally:
        mem_only.close()

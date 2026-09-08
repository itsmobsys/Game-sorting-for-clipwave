"""Cache unit tests: TTLs, LRU bound, L1-avoids-L2, persistence."""
import time

from youtube_game_detector.cache import TwoLevelCache


def test_positive_cache_hit_no_expiry():
    c = TwoLevelCache(positive_ttl=1000, negative_ttl=1000)
    c.set("dQw4w9WgXcQ", "Minecraft")
    assert c.get("dQw4w9WgXcQ") == (True, "Minecraft")
    assert c.get("dQw4w9WgXcQ") == (True, "Minecraft")


def test_cache_miss_unknown_id():
    c = TwoLevelCache()
    assert c.get("dQw4w9WgXcQ") == (False, None)


def test_negative_cache_hit():
    c = TwoLevelCache(positive_ttl=1000, negative_ttl=1000)
    c.set("dQw4w9WgXcQ", None)
    assert c.get("dQw4w9WgXcQ") == (True, None)


def test_positive_expiration():
    c = TwoLevelCache(positive_ttl=0.05, negative_ttl=1000)
    c.set("dQw4w9WgXcQ", "Valorant")
    assert c.get("dQw4w9WgXcQ") == (True, "Valorant")
    time.sleep(0.07)
    assert c.get("dQw4w9WgXcQ") == (False, None)


def test_negative_expiration():
    c = TwoLevelCache(positive_ttl=1000, negative_ttl=0.05)
    c.set("dQw4w9WgXcQ", None)
    assert c.get("dQw4w9WgXcQ") == (True, None)
    time.sleep(0.07)
    assert c.get("dQw4w9WgXcQ") == (False, None)


def test_lru_bound_evicts_oldest():
    c = TwoLevelCache(max_memory_entries=2, positive_ttl=1000)
    c.set("AAAAAAAAAAA", "A")
    c.set("BBBBBBBBBBB", "B")
    c.set("CCCCCCCCCCC", "C")  # evicts A
    assert len(c) == 2
    assert c.get("AAAAAAAAAAA") == (False, None)
    assert c.get("BBBBBBBBBBB") == (True, "B")
    assert c.get("CCCCCCCCCCC") == (True, "C")


def test_l1_avoids_repeated_sqlite_reads():
    c = TwoLevelCache(db_path=":memory:", positive_ttl=1000)
    try:
        c.set("dQw4w9WgXcQ", "Minecraft")
        c._l1.clear()  # force exactly one L2 read to re-promote
        assert c.get("dQw4w9WgXcQ") == (True, "Minecraft")
        assert c.l2_reads == 1
        assert c.l2_hits == 1
        # Subsequent hits are pure L1: no further SQLite reads.
        for _ in range(50):
            assert c.get("dQw4w9WgXcQ") == (True, "Minecraft")
        assert c.l2_reads == 1
        assert c.l1_hits >= 50
    finally:
        c.close()


def test_sqlite_persistence_across_instances(tmp_path):
    db = str(tmp_path / "games.db")
    c1 = TwoLevelCache(db_path=db, positive_ttl=1000, negative_ttl=1000)
    c1.set("dQw4w9WgXcQ", "Phasmophobia")
    c1.set("eQw4w9WgXcQ", None)
    c1.close()
    c2 = TwoLevelCache(db_path=db, positive_ttl=1000, negative_ttl=1000)
    try:
        assert c2.get("dQw4w9WgXcQ") == (True, "Phasmophobia")
        assert c2.get("eQw4w9WgXcQ") == (True, None)
    finally:
        c2.close()


def test_invalidate_and_clear():
    c = TwoLevelCache(db_path=":memory:")
    try:
        c.set("dQw4w9WgXcQ", "Minecraft")
        c.invalidate("dQw4w9WgXcQ")
        assert c.get("dQw4w9WgXcQ") == (False, None)
        c.set("dQw4w9WgXcQ", "Minecraft")
        c.clear()
        assert c.get("dQw4w9WgXcQ") == (False, None)
    finally:
        c.close()

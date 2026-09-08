"""L1 in-memory LRU + optional L2 SQLite persistent cache.

Design notes (perf):
- Hot path (L1 hit) is pure in-memory: one dict lookup, no disk I/O,
  no regex, no network. ``OrderedDict.move_to_end`` keeps it LRU.
- SQLite is touched only on L1 miss, and written only on ``set``
  (i.e. after a fresh fetch), never on reads.
- Both positive results (game name) and negative results (``None``) are
  cached, with different TTLs.
- Expired L2 rows are treated as misses on read (timestamp check) and
  physically removed by a lightweight background sweeper every
  ``cleanup_interval`` (default 6h). The sweep is a single scoped
  ``DELETE`` of expired rows only: no VACUUM, no rebuild, no rewrite
  of valid entries.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS game_cache ("
    " video_id TEXT PRIMARY KEY,"
    " game TEXT,"
    " success INTEGER NOT NULL,"
    " updated_at REAL NOT NULL)"
)

DEFAULT_POSITIVE_TTL = 6 * 3600.0  # 6 hours for successful detections
DEFAULT_NEGATIVE_TTL = 3600.0  # 1 hour: failures / missing metadata may be transient
DEFAULT_MAX_MEMORY_ENTRIES = 2048
DEFAULT_CLEANUP_INTERVAL = 6 * 3600.0  # physical L2 expiry sweep every 6 hours


class _Entry:
    """Small L1 record. ``__slots__`` keeps per-entry RAM overhead minimal."""

    __slots__ = ("game", "updated_at", "success")

    def __init__(self, game: Optional[str], updated_at: float, success: bool):
        self.game = game
        self.updated_at = updated_at
        self.success = success


class TwoLevelCache:
    """Thread-safe two-level cache keyed by immutable YouTube ``video_id``.

    Args:
        max_memory_entries: bound for the L1 LRU. Oldest entries are evicted
            first, so memory cannot grow indefinitely. ``0`` disables L1.
        positive_ttl: seconds a successful detection stays valid.
        negative_ttl: seconds a ``None`` result stays valid.
        db_path: path to a SQLite file for the L2 persistent cache.
            ``None`` (default) disables L2 -- pure in-memory caching with
            zero disk I/O. ``":memory:"`` gives a non-persistent SQLite L2
            (useful for tests).
        cleanup_interval: seconds between background sweeps that physically
            delete expired L2 rows. Default is 6 hours. ``None``/``0``
            disables the background worker (sweeps can still be triggered
            manually via :meth:`prune_expired`). This is independent from
            the entry TTLs: expiry is always enforced on read first; the
            sweep only reclaims space later.
    """

    def __init__(
        self,
        max_memory_entries: int = DEFAULT_MAX_MEMORY_ENTRIES,
        positive_ttl: float = DEFAULT_POSITIVE_TTL,
        negative_ttl: float = DEFAULT_NEGATIVE_TTL,
        db_path: Optional[str] = None,
        cleanup_interval: Optional[float] = DEFAULT_CLEANUP_INTERVAL,
    ):
        self.max_memory_entries = max(0, int(max_memory_entries))
        self.positive_ttl = float(positive_ttl)
        self.negative_ttl = float(negative_ttl)
        self._db_path = db_path
        self.cleanup_interval = (
            float(cleanup_interval) if cleanup_interval else 0.0
        )
        self._l1: "OrderedDict[str, _Entry]" = OrderedDict()
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._db_disabled = False
        # Cheap observability counters (also used by tests/benchmarks).
        self.l1_hits = 0
        self.l2_reads = 0
        self.l2_hits = 0
        self.cleanups_run = 0
        self.last_cleanup_at: Optional[float] = None
        self.last_cleanup_removed = 0
        # Single long-lived daemon sweeper; created once here (never per
        # request), first sweep runs after one full interval -- no startup
        # cleanup cost. Memory-only caches need no sweeper.
        self._stop_cleanup = threading.Event()
        self._cleanup_thread: Optional[threading.Thread] = None
        if self._db_path is not None and self.cleanup_interval > 0:
            self._start_cleanup_worker()

    # ------------------------------------------------------------------ #
    # public API                                                          #
    # ------------------------------------------------------------------ #
    def get(self, video_id: str) -> Tuple[bool, Optional[str]]:
        """Return ``(found, game)``. ``found`` is False on miss/expiry."""
        now = time.time()
        with self._lock:
            entry = self._l1.get(video_id)
            if entry is not None:
                if now - entry.updated_at < self._ttl_for(entry.success):
                    self._l1.move_to_end(video_id)
                    self.l1_hits += 1
                    return True, entry.game
                del self._l1[video_id]  # expired
            if self._db_path is None or self._db_disabled:
                return False, None
            row = self._l2_read(video_id)
            if row is None:
                return False, None
            game, success, updated_at = row
            if now - updated_at < self._ttl_for(success):
                self.l2_hits += 1
                # Promote to L1 with the ORIGINAL timestamp so reads never
                # extend an entry's lifetime (and never cause a rewrite).
                self._l1_put(video_id, _Entry(game, updated_at, success))
                return True, game
            return False, None  # stale L2 row: caller refetches, then set()

    def set(self, video_id: str, game: Optional[str]) -> None:
        """Store a fresh fetch result in L1 and (if enabled) L2."""
        now = time.time()
        success = game is not None
        with self._lock:
            self._l1_put(video_id, _Entry(game, now, success))
            if self._db_path is not None and not self._db_disabled:
                self._l2_write(video_id, game, success, now)

    def invalidate(self, video_id: str) -> None:
        with self._lock:
            self._l1.pop(video_id, None)
            if self._db_path is not None and not self._db_disabled:
                try:
                    conn = self._ensure_conn()
                    if conn is not None:
                        conn.execute(
                            "DELETE FROM game_cache WHERE video_id = ?",
                            (video_id,),
                        )
                        conn.commit()
                except sqlite3.Error:
                    log.debug("cache invalidate failed", exc_info=True)

    def clear(self) -> None:
        with self._lock:
            self._l1.clear()
            if self._db_path is not None and not self._db_disabled:
                try:
                    conn = self._ensure_conn()
                    if conn is not None:
                        conn.execute("DELETE FROM game_cache")
                        conn.commit()
                except sqlite3.Error:
                    log.debug("cache clear failed", exc_info=True)

    def close(self) -> None:
        # Stop the background sweeper first so no DELETE can race the
        # connection close; the Event + timeout guarantees close() never
        # blocks shutdown for longer than necessary.
        self._stop_cleanup.set()
        thread, self._cleanup_thread = self._cleanup_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._l1)

    def __del__(self):  # best-effort: never leak a worker on GC
        try:
            self._stop_cleanup.set()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # L2 expiry sweep                                                     #
    # ------------------------------------------------------------------ #
    def prune_expired(self, now: Optional[float] = None) -> int:
        """Delete physically-expired L2 rows; return the number removed.

        Only rows whose TTL has already passed are touched -- valid rows
        are never rewritten or deleted, the database file is never
        removed/rebuilt, and no VACUUM is performed. Expiry is always
        enforced on read first (:meth:`get` treats stale rows as misses),
        so this is purely space reclamation and is safe to run at any
        time, including concurrently with ``get``/``set`` (same RLock
        and same single SQLite connection as all other L2 access).
        Returns 0 when L2 is disabled, unavailable, or has no DB yet
        (never creates a DB file just to prune it).
        """
        if self._db_path is None or self._db_disabled:
            return 0
        if now is None:
            now = time.time()
        with self._lock:
            if self._conn is None and self._db_path != ":memory:":
                # Never create a database file solely to run a sweep:
                # nothing stored yet means nothing expired.
                try:
                    if not os.path.exists(self._db_path):
                        return 0
                except (OSError, ValueError, TypeError):
                    return 0
            try:
                conn = self._ensure_conn()
                if conn is None:
                    return 0
                # One scoped DELETE: expired positives and expired negatives
                # each use their own TTL cutoff. Touches expired rows only.
                cursor = conn.execute(
                    "DELETE FROM game_cache WHERE "
                    "(success = 1 AND updated_at <= ?) OR "
                    "(success = 0 AND updated_at <= ?)",
                    (now - self.positive_ttl, now - self.negative_ttl),
                )
                conn.commit()
                removed = cursor.rowcount if cursor.rowcount >= 0 else 0
            except sqlite3.Error:
                log.debug("cache prune_expired failed", exc_info=True)
                return 0
            self.cleanups_run += 1
            self.last_cleanup_at = now
            self.last_cleanup_removed = removed
            return removed

    @property
    def cleanup_alive(self) -> bool:
        """True while the background sweep worker thread is running."""
        thread = self._cleanup_thread
        return thread is not None and thread.is_alive()

    def _start_cleanup_worker(self) -> None:
        if self._cleanup_thread is not None:
            return
        thread = threading.Thread(
            target=self._cleanup_loop,
            name="clipwave-cache-cleanup",
            daemon=True,  # never prevents interpreter exit
        )
        self._cleanup_thread = thread
        thread.start()

    def _cleanup_loop(self) -> None:
        # Event.wait(timeout) sleeps without busy-waking; the first sweep
        # runs only after one full interval (no expensive startup cleanup).
        while not self._stop_cleanup.wait(timeout=self.cleanup_interval):
            try:
                self.prune_expired()
            except Exception:
                log.debug("background cache cleanup failed", exc_info=True)
            if self._db_disabled:
                return  # persistence gone for good; retire the worker

    # ------------------------------------------------------------------ #
    # internals                                                           #
    # ------------------------------------------------------------------ #
    def _ttl_for(self, success: bool) -> float:
        return self.positive_ttl if success else self.negative_ttl

    def _l1_put(self, video_id: str, entry: _Entry) -> None:
        if self.max_memory_entries <= 0:
            return
        self._l1[video_id] = entry
        self._l1.move_to_end(video_id)
        while len(self._l1) > self.max_memory_entries:
            self._l1.popitem(last=False)

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if self._conn is not None or self._db_disabled:
            return self._conn
        try:
            conn = sqlite3.connect(
                self._db_path, check_same_thread=False, timeout=5.0
            )
            conn.execute(_SCHEMA)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=3000")
            except sqlite3.Error:
                pass  # pragmas are best-effort (e.g. ":memory:")
            conn.commit()
            self._conn = conn
            return conn
        except sqlite3.Error:
            # Never crash the caller because persistence is unavailable;
            # permanently fall back to L1-only for this instance.
            log.debug("sqlite unavailable, using memory-only cache", exc_info=True)
            self._db_disabled = True
            return None

    def _l2_read(self, video_id: str) -> Optional[Tuple[Optional[str], bool, float]]:
        self.l2_reads += 1
        try:
            conn = self._ensure_conn()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT game, success, updated_at FROM game_cache WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        except sqlite3.Error:
            log.debug("sqlite read failed", exc_info=True)
            return None
        if row is None:
            return None
        game, success, updated_at = row
        return game, bool(success), float(updated_at)

    def _l2_write(
        self, video_id: str, game: Optional[str], success: bool, now: float
    ) -> None:
        try:
            conn = self._ensure_conn()
            if conn is None:
                return
            conn.execute(
                "INSERT OR REPLACE INTO game_cache "
                "(video_id, game, success, updated_at) VALUES (?, ?, ?, ?)",
                (video_id, game, int(success), now),
            )
            conn.commit()
        except sqlite3.Error:
            log.debug("sqlite write failed", exc_info=True)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "l1_size": len(self._l1),
                "l1_hits": self.l1_hits,
                "l2_reads": self.l2_reads,
                "l2_hits": self.l2_hits,
                "cleanups_run": self.cleanups_run,
                "last_cleanup_removed": self.last_cleanup_removed,
            }

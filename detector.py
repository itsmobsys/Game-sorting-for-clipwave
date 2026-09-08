"""Sync + async YouTube watch-page -> game resolver with coalesced fetching.

Flow per ``video_id``::

    validate -> L1 memory -> L2 sqlite -> single HTTP fetch -> extract -> store

Concurrent callers for the same new ``video_id`` share one HTTP request
via a lightweight in-flight registry (threading.Event). No worker
threads/processes are created by this module; batch helpers bound
concurrency with a semaphore / ThreadPoolExecutor. The async API
delegates to the same sync path in a worker thread, so sync and async
callers share one cache and one coalescing mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional

from .cache import (
    DEFAULT_CLEANUP_INTERVAL,
    DEFAULT_MAX_MEMORY_ENTRIES,
    DEFAULT_NEGATIVE_TTL,
    DEFAULT_POSITIVE_TTL,
    TwoLevelCache,
)
from .extractor import extract_game

log = logging.getLogger(__name__)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
WATCH_URL = "https://www.youtube.com/watch?v={video_id}&hl=en&bpctr=9999999999"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 8.0
DEFAULT_MAX_CONCURRENCY = 4


def is_valid_video_id(video_id: object) -> bool:
    """Return True only for plausible 11-char YouTube video IDs.

    No network, no parsing -- pure cheap regex gate.
    """
    return isinstance(video_id, str) and _VIDEO_ID_RE.match(video_id) is not None


def _build_headers(user_agent: str) -> Dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }


class _SharedSession:
    """One reusable HTTP session, lazily created.

    Prefers ``requests.Session`` (urllib3 connection pooling) when the
    ``requests`` package is present; otherwise falls back to stdlib
    ``urllib`` so this module has zero hard dependencies.
    """

    def __init__(self, user_agent: str, timeout: float):
        self._headers = _build_headers(user_agent)
        self._timeout = timeout
        self._lock = threading.Lock()
        self._session = None  # requests.Session | None
        self._use_requests: Optional[bool] = None

    def _ensure(self) -> bool:
        """Decide backend once. Returns True if requests is usable."""
        if self._use_requests is not None:
            return self._use_requests
        with self._lock:
            if self._use_requests is not None:
                return self._use_requests
            try:
                import requests  # type: ignore

                s = requests.Session()
                s.headers.update(self._headers)
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=4, pool_maxsize=16
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                self._session = s
                self._use_requests = True
            except Exception:
                self._session = None
                self._use_requests = False
            return self._use_requests

    def get_text(self, url: str) -> Optional[str]:
        if self._ensure():
            try:
                resp = self._session.get(url, timeout=self._timeout)  # type: ignore[union-attr]
                if resp.status_code != 200:
                    return None
                # Guard RAM: never buffer absurd pages.
                ctype = resp.headers.get("Content-Type", "")
                if ctype and "html" not in ctype and "text" not in ctype:
                    return None
                resp.encoding = resp.encoding or "utf-8"
                text = resp.text
                if len(text) > 4_000_000:
                    return None
                return text
            except Exception:
                log.debug("youtube http request failed", exc_info=True)
                return None
        # stdlib fallback: single lightweight GET for the HTML only.
        try:
            import urllib.request

            req = urllib.request.Request(url, headers=self._headers, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # type: ignore[arg-type]
                if getattr(resp, "status", 200) != 200:
                    return None
                raw = resp.read(4_000_001)
                if len(raw) > 4_000_000:
                    return None
                charset = "utf-8"
                try:
                    ctype = resp.headers.get_content_charset()  # type: ignore[attr-defined]
                    if ctype:
                        charset = ctype
                except Exception:
                    pass
                return raw.decode(charset, errors="replace")
        except Exception:
            log.debug("youtube http request failed", exc_info=True)
            return None

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
                # Reset backend choice so a reused instance lazily
                # re-creates the session instead of dead-ending on None.
                self._use_requests = None


Fetcher = Callable[[str], Optional[str]]


class YouTubeGameDetector:
    """video-ID -> cached game metadata resolver.

    Args:
        max_memory_entries: bound for the L1 LRU.
        positive_ttl: seconds a detected game stays cached (default 6h).
        negative_ttl: seconds a ``None`` result stays cached.
        db_path: SQLite file for L2 persistence, or None for memory-only.
        cleanup_interval: seconds between background sweeps that delete
            physically-expired L2 rows (default 6h; ``None``/``0``
            disables the worker). Independent from the TTLs.
        timeout: per-request HTTP timeout in seconds.
        max_concurrency: cap for batch/async concurrent fetches.
        user_agent: HTTP User-Agent header.
        fetcher: optional ``(video_id) -> html|None`` override (tests /
            custom transports). When given, no HTTP session is created.
    """

    def __init__(
        self,
        max_memory_entries: int = DEFAULT_MAX_MEMORY_ENTRIES,
        positive_ttl: float = DEFAULT_POSITIVE_TTL,
        negative_ttl: float = DEFAULT_NEGATIVE_TTL,
        db_path: Optional[str] = None,
        cleanup_interval: Optional[float] = DEFAULT_CLEANUP_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        user_agent: str = DEFAULT_USER_AGENT,
        fetcher: Optional[Fetcher] = None,
    ):
        self.cache = TwoLevelCache(
            max_memory_entries=max_memory_entries,
            positive_ttl=positive_ttl,
            negative_ttl=negative_ttl,
            db_path=db_path,
            cleanup_interval=cleanup_interval,
        )
        self.timeout = float(timeout)
        self.max_concurrency = max(1, int(max_concurrency))
        self._fetcher = fetcher
        self._session = (
            None if fetcher is not None else _SharedSession(user_agent, self.timeout)
        )
        # Duplicate-request coalescing (sync): video_id -> threading.Event.
        self._inflight_lock = threading.Lock()
        self._inflight: Dict[str, threading.Event] = {}
        # Concurrency bound for fresh sync fetches.
        self._fetch_slots = threading.Semaphore(self.max_concurrency)

    # ------------------------------------------------------------------ #
    # sync public API                                                     #
    # ------------------------------------------------------------------ #
    def get_game(self, video_id: str) -> Optional[str]:
        """Return the game name for ``video_id`` or ``None``.

        Never raises on YouTube/SQLite failures; invalid IDs return
        ``None`` without any network request.
        """
        if not is_valid_video_id(video_id):
            return None
        found, game = self.cache.get(video_id)
        if found:
            return game

        owner, event = self._join_inflight(video_id)
        if not owner:
            # Coalesced: wait for the owner's single request, then serve
            # from cache (no second HTTP request, no re-parse).
            event.wait(timeout=self.timeout + 22.0)
            found, game = self.cache.get(video_id)
            return game if found else None
        try:
            game = self._fetch_and_store(video_id)
            return game
        finally:
            self._leave_inflight(video_id, event)

    def get_many(self, video_ids: Iterable[str]) -> Dict[str, Optional[str]]:
        """Resolve many IDs with bounded concurrency.

        Duplicate IDs in the input share one fetch via the same
        in-flight coalescing as :meth:`get_game`.
        """
        ids: List[str] = list(video_ids)
        if not ids:
            return {}
        if len(ids) == 1:
            return {ids[0]: self.get_game(ids[0])}
        workers = min(self.max_concurrency, len(ids))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="clipwave-game"
        ) as pool:
            games = list(pool.map(self.get_game, ids))
        return dict(zip(ids, games))

    def invalidate(self, video_id: str) -> None:
        self.cache.invalidate(video_id)

    def clear_cache(self) -> None:
        self.cache.clear()

    def prune_expired(self) -> int:
        """Manually trigger one L2 expiry sweep; returns rows removed."""
        return self.cache.prune_expired()

    def stats(self) -> Dict[str, int]:
        s = self.cache.stats()
        s["max_concurrency"] = self.max_concurrency
        return s

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
        self.cache.close()

    # ------------------------------------------------------------------ #
    # async public API                                                    #
    # ------------------------------------------------------------------ #
    async def aget_game(self, video_id: str) -> Optional[str]:
        """Async variant of :meth:`get_game`.

        Delegates to the single sync code path in a worker thread
        (``asyncio.to_thread``), so sync and async callers share the
        same cache and the same in-flight coalescing: one HTTP fetch
        per new ``video_id`` no matter how it is requested. No asyncio
        primitives are stored on the instance, so the detector stays
        safe to reuse across event loops.
        """
        if not is_valid_video_id(video_id):
            return None
        return await asyncio.to_thread(self.get_game, video_id)

    async def aget_many(self, video_ids: Iterable[str]) -> Dict[str, Optional[str]]:
        ids = list(video_ids)
        games = await asyncio.gather(*(self.aget_game(v) for v in ids))
        return dict(zip(ids, games))

    # ------------------------------------------------------------------ #
    # internals                                                           #
    # ------------------------------------------------------------------ #
    def _join_inflight(self, video_id: str) -> tuple[bool, threading.Event]:
        with self._inflight_lock:
            event = self._inflight.get(video_id)
            if event is not None:
                return False, event
            event = threading.Event()
            self._inflight[video_id] = event
            return True, event

    def _leave_inflight(self, video_id: str, event: threading.Event) -> None:
        with self._inflight_lock:
            self._inflight.pop(video_id, None)
        event.set()

    def _fetch_html(self, video_id: str) -> Optional[str]:
        if self._fetcher is not None:
            try:
                return self._fetcher(video_id)
            except Exception:
                log.debug("custom fetcher failed", exc_info=True)
                return None
        assert self._session is not None
        return self._session.get_text(WATCH_URL.format(video_id=video_id))

    def _fetch_and_store(self, video_id: str) -> Optional[str]:
        # Re-check after winning ownership: a previous fetch may have
        # populated the cache between our first check and join.
        found, game = self.cache.get(video_id)
        if found:
            return game
        # Bound simultaneous fresh fetches; coalesced waiters do NOT
        # consume a slot (they perform no I/O).
        acquired = self._fetch_slots.acquire(timeout=self.timeout + 22.0)
        if not acquired:
            return None
        try:
            html = self._fetch_html(video_id)
        finally:
            self._fetch_slots.release()
        if not html:
            game = None
        else:
            try:
                game = extract_game(html)
            except Exception:
                log.debug("extraction failed", exc_info=True)
                game = None
        # Cache both hits AND misses (misses with shorter TTL) so every
        # new video_id costs at most one HTTP request per TTL window.
        try:
            self.cache.set(video_id, game)
        except Exception:
            log.debug("cache store failed", exc_info=True)
        return game

# youtube_game_detector

Standalone ClipWave module: **YouTube `video_id` → cached game name resolver**.

No browser, no LLM, no screenshots. One lightweight HTTP GET of the watch
page per new `video_id`, then serve from cache.

## Install

Zero hard dependencies (stdlib only). If `requests` is installed it is used
for connection pooling; otherwise stdlib `urllib` is used automatically.

```bash
pip install requests  # optional but recommended (connection reuse)
```

Copy the `youtube_game_detector/` directory into your project.

## Integrate (3 lines)

```python
from youtube_game_detector import get_game

game = get_game(video_id)  # e.g. "Phasmophobia", or None

if game:
    print(game)
```

Full control (persistence, tuning):

```python
from youtube_game_detector import YouTubeGameDetector

detector = YouTubeGameDetector(
    db_path="games.db",        # L2 SQLite persistence (None = memory-only)
    max_memory_entries=2048,   # L1 LRU bound
    positive_ttl=6 * 3600,     # successful detections: 6 hours
    negative_ttl=3600,         # None results: 1 hour (transient failures)
    cleanup_interval=6 * 3600, # L2 expiry sweep: every 6 hours (None/0 = off)
    timeout=8.0,               # per-request HTTP timeout (s)
    max_concurrency=4,         # cap for batch/async fresh fetches
)
game = detector.get_game(video_id)          # sync
game = await detector.aget_game(video_id)   # async (same cache)
games = detector.get_many([id1, id2])       # bounded-concurrency batch
detector.prune_expired()                    # manual L2 expiry sweep (rows removed)
detector.invalidate(video_id)               # drop one entry
detector.clear_cache()                      # drop everything
detector.close()                            # stop cleanup worker, close HTTP + SQLite
```

## Architecture

```text
video_id → validate (11-char regex, no I/O)
        → L1 in-memory LRU (dict lookup, no disk/regex/network)
        → L2 SQLite (only on L1 miss; reads never rewrite)
        → ONE YouTube HTTP GET (reused session, timeout, realistic UA)
        → extract → store (L1 + L2) → return
```

* **Cache-first:** `video_id` is the immutable cache key. Positive hits live
  6 hours, `None`/failure results 1 hour so transient YouTube gaps heal.
* **Coalesced:** N simultaneous callers for the same new ID share one HTTP
  request (shared in-flight `threading.Event`; async delegates to the same
  sync path, so sync + async callers coalesce together).
* **Bounded:** L1 LRU cap bounds RAM; semaphore/`max_concurrency` bounds
  fresh fetches; pages over ~4MB are rejected.
* **Never crashes ClipWave:** timeouts, HTTP errors, bad HTML, YouTube
  layout changes, SQLite errors all degrade to `None` (debug-logged only).

## Cache retention + L2 expiry cleanup

```text
L1 = bounded in-memory LRU (unchanged)
L2 = persistent SQLite table `game_cache` (file never deleted/rebuilt)
positive TTL = 6 hours (success = 1 rows, `updated_at` timestamp)
negative TTL = 1 hour  (success = 0 rows, `updated_at` timestamp)
cleanup interval = 6 hours (physical sweep schedule; independent from TTLs)
```

How it works:

* **Expiry is enforced on read, before any cleanup.** `get()` compares
  `updated_at` against the matching TTL, so an expired row is a miss
  (caller refetches) even if the sweeper has not removed it yet.
* **Every 6 hours** a single daemon worker (`clipwave-cache-cleanup`,
  one thread per cache instance, `Event.wait` sleep — zero busy-wake CPU)
  runs one scoped statement touching expired rows only:

```sql
DELETE FROM game_cache
WHERE (success = 1 AND updated_at <= ?)   -- now - positive_ttl (6h)
   OR (success = 0 AND updated_at <= ?);  -- now - negative_ttl (1h)
```

* No `VACUUM`, no rebuild, no rewrite of valid rows; existing WAL +
  `synchronous=NORMAL` settings are kept. The sweep reuses the single
  shared SQLite connection under the same `RLock` as all other L2 access,
  so it is safe alongside `get_game()` / `get_many()` / `aget_game()`.
* No expensive startup cleanup: the first sweep runs after one full
  interval (and `prune_expired()` never creates a DB file just to scan it).
* `close()` signals the worker and joins it (≤5 s), so shutdown is clean;
  the thread is daemon-only and never blocks interpreter exit.
* Tests inject short intervals (`cleanup_interval=0.05…0.15`) or call
  `prune_expired()` directly — production default stays 6 hours.

## Extraction (priority order, never guesses)

1. `ytVideoAttributeViewModelTitle` element (current markup).
2. Scoped `"videoAttributeViewModel": {... "title": ...}` JSON.
3. Explicit serialized keys (`gameTitle` / `videoGame` / `gameName`).
4. `application/ld+json` blocks scanned for game keys only.

Candidates are validated: video titles, channel names, `"Gaming"`,
years, and view counts are rejected. Uncertain → `None`.

## Resource benchmark (measured, mocked network)

`python -m youtube_game_detector.benchmark_resources --quick` re-runs all of
this. Network is mocked in every section — YouTube is never contacted.
Dependencies: none new (`psutil` used only for RSS when present, with a
stdlib fallback; it is NOT a runtime dependency of the detector).

Latest smoke run with the 6h-TTL + 6h-cleanup build
(Python 3.14.2, Windows 11, 8 logical cores, 11.8 GB RAM, RSS via psutil):

```text
BASELINE (fresh child process)
RSS pre/post-import: 28.719 MB (delta +0.000; harness-specific, psutil preloaded)
Idle CPU: 0.0000 s per 1.00 s wall (effectively zero; cleanup worker sleeps in Event.wait)

L1 CACHE - 100,000 lookups (10,000 resident entries, all L1 hits)
Wall 0.243 s, CPU 0.234 s, avg CPU 96.5% of one core
Per hit: 2,428 ns wall / ~2,344 ns CPU; 411,834 lookups/s
Peak RSS 34.78 MB; HTTP 0 (asserted); SQLite reads 0 (asserted)

L2 SQLITE - 10,000 lookups (10,000 distinct IDs, L1 disabled = pure SQLite path)
Wall 0.203 s, CPU 0.203 s, avg CPU 100.1% of one core
Per hit: 20.3 us wall/CPU; 49,271 lookups/s
SQLite file 0.60 MB; peak RSS 36.33 MB

COLD LOOKUP (mocked 200 KB page, game=Phasmophobia, 1,000 distinct IDs)
Single cold 0.14 ms wall; avg 0.03 ms wall/CPU (parse alone 10.83 us)
Peak RSS 36.16 MB (+0.04 MB per 1,000 resident entries, ~45 B/entry)
HTTP 1,000 (= 1 per new ID)

CONCURRENCY (mocked latency 50 ms same-ID / 20 ms diff-ID, limit 4)
12 same-ID:  HTTP 1,  max_active 1, wall 0.06 s  OK
12 diff-ID:  HTTP 12, max_active 4, wall 0.07 s  OK
50 same-ID:  HTTP 1,  max_active 1, wall 0.07 s  OK
50 diff-ID:  HTTP 50, max_active 4, wall 0.29 s  OK
Peak RSS ~36.4-37.3 MB across rows

CACHE SIZE TEST (resident RSS delta, entries held live)
100:    +0.00 MB (0 B/entry)
1,000:  +0.04 MB (~45 B/entry)
10,000: +0.77 MB (~80 B/entry)
50,000: +11.84 MB (~248 B/entry)

20 s SOAK (404,216 ops: 400k hot L1 + 4k warm + 200 new + 4-thread bursts)
Avg RSS 37.59 MB, peak 37.59 MB
Avg CPU 5.9% of one core (1.17 s CPU / 20.0 s wall), peak tick 31.0%
L1 hit rate 98.89%, L2 hit rate 95.47% of L1 misses, HTTP 204, 0 burst errors
```

Interpretation:

* At 10,000 cached videos, expect approximately **0.8 MB additional RAM**
  (~80 B/entry). 50,000 entries jumps to ~12 MB — batch/page effects dominate
  past ~10k, so **10,000 is the recommended production L1 size**.
* A cache hit consumes approximately **2.4 us wall / 2.3 us CPU**
  (~410k lookups/s single-threaded).
* One cold lookup costs approximately **0.03 ms CPU** (parse ~11 us) plus the
  real YouTube round-trip in production (mocked to zero here); steady-state
  RSS ~45–65 B per resident entry.
* N concurrent requests for the same video cause exactly **1 HTTP request**
  (different IDs respect the `max_concurrency=4` fetch cap).
* The 6-hour cleanup worker is idle between sweeps (`Event.wait`, 0 CPU —
  see the zero idle-CPU probe) and each sweep is a single indexed `DELETE`
  of expired rows only.

Full run (1M lookups x3 + 12/50/100 concurrency + 5-min soak):

```bash
python -m youtube_game_detector.benchmark_resources
```

## Test / benchmark

```bash
uv sync                                  # create .venv + install dev deps (pytest, requests, psutil)
uv run pytest youtube_game_detector/tests -q   # 47 tests (mocked, no network)
uv run python -m youtube_game_detector.benchmark          # latency micro-benchmark (mocked)
uv run python -m youtube_game_detector.benchmark_resources --quick  # CPU/RAM smoke (~40 s, mocked)
```

Without uv, plain `pytest` / `python -m ...` also works: the detector
runtime itself is stdlib-only (`requests`/`psutil` are optional
enhancements covered by the `http`/`bench` extras and the `dev` group).

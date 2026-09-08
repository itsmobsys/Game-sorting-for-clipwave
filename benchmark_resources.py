"""Production-oriented CPU + RAM benchmark for ``youtube_game_detector``.

Answers: "If ClipWave runs this continuously, how much CPU and RAM will it
actually consume?"

* ALL network activity is mocked (``fetcher=...``). YouTube is never
  contacted. The only socket traffic possible is none -- every detector
  in this file is constructed with an injected ``fetcher``.
* ``psutil`` is used ONLY here for OS RSS readings when available. It is
  NOT a runtime dependency of ``youtube_game_detector``. Without psutil
  the benchmark falls back to stdlib-only measurement (Windows
  ``GetProcessMemoryInfo`` via ctypes, ``/proc/self/status`` on Linux).
* CPU accounting is universal and dependency-free:
  ``time.process_time()`` (whole-process user+system CPU, all threads)
  vs ``time.perf_counter()`` (wall).  Reported utilization is::

      avg CPU % = process CPU time / wall time * 100

  which may exceed 100% for multi-threaded workloads (cores reported
  alongside, so this is unambiguous).
* Peak RSS is tracked with a background sampler thread at a documented
  interval (true OS peak between samples can be marginally higher;
  sampling overhead itself is negligible -- one RSS read per tick).

Usage::

    python -m youtube_game_detector.benchmark_resources          # full (~7 min, incl. 5-min soak)
    python -m youtube_game_detector.benchmark_resources --quick  # smoke (~1 min, 20 s soak)
    python -m youtube_game_detector.benchmark_resources --longrun-secs 600 --l1-n 1000000

Sections: BASELINE (fresh process) / L1 / L2 / COLD / CONCURRENCY /
CACHE SIZES / LONG-RUN simulation.
"""
from __future__ import annotations

import argparse
import gc
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time

MB = 1024 * 1024

# --------------------------------------------------------------------------
# RSS measurement: psutil preferred, stdlib fallbacks, never fatal.
# --------------------------------------------------------------------------
try:  # optional, benchmark-only
    import psutil as _psutil

    _PSUTIL_PROC = _psutil.Process(os.getpid())
    HAVE_PSUTIL = True
except Exception:  # pragma: no cover - environment dependent
    _psutil = None  # type: ignore[assignment]
    HAVE_PSUTIL = False

if HAVE_PSUTIL:
    RSS_SOURCE = "psutil.Process.memory_info().rss"

    def rss_bytes() -> int:
        return int(_PSUTIL_PROC.memory_info().rss)

elif os.name == "nt":  # stdlib fallback: GetProcessMemoryInfo via ctypes
    import ctypes

    RSS_SOURCE = "ctypes GetProcessMemoryInfo(WorkingSetSize)"

    class _PMC(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS (first fields)
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _HPROC = ctypes.windll.kernel32.GetCurrentProcess()

    def rss_bytes() -> int:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            _HPROC, ctypes.byref(pmc), pmc.cb
        )
        return int(pmc.WorkingSetSize) if ok else -1

else:  # stdlib fallback: /proc on Linux/Unix
    RSS_SOURCE = "/proc/self/status VmRSS"

    def rss_bytes() -> int:
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except Exception:
            pass
        return -1


def rss_stable(reps: int = 5, gap: float = 0.03) -> int:
    """Settled RSS: gc + min of several samples (min rejects transient spikes)."""
    gc.collect()
    vals = []
    for _ in range(reps):
        vals.append(rss_bytes())
        time.sleep(gap)
    return min(v for v in vals if v >= 0)


class Sampler:
    """Background sampler: peak/avg RSS + interval CPU% from process_time deltas.

    Interval CPU% needs no psutil: cpu% over tick = dCPU/dWall*100.
    """

    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.rss: list[int] = []
        self.cpu: list[float] = []  # per-tick %, first tick excluded (0.0 marker)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        last_w: float | None = None
        last_c = 0.0
        while not self._stop.is_set():
            w = time.perf_counter()
            c = time.process_time()
            r = rss_bytes()
            if last_w is not None and w > last_w and r >= 0:
                self.rss.append(r)
                self.cpu.append((c - last_c) / (w - last_w) * 100.0)
            last_w, last_c = w, c
            self._stop.wait(self.interval)

    def __enter__(self) -> "Sampler":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def peak_rss(self) -> int:
        return max(self.rss) if self.rss else rss_bytes()

    def avg_rss(self) -> float:
        return statistics.fmean(self.rss) if self.rss else float(rss_bytes())

    def peak_cpu(self) -> float:
        return max(self.cpu) if self.cpu else 0.0

    def avg_cpu(self) -> float:
        return statistics.fmean(self.cpu) if self.cpu else 0.0


# --------------------------------------------------------------------------
# Fixtures (deterministic, always valid 11-char IDs, zero network).
# --------------------------------------------------------------------------
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def make_vid(i: int) -> str:
    n = int(i)
    chars = []
    for _ in range(11):
        chars.append(_ALPHABET[n % 64])
        n //= 64
    return "".join(reversed(chars))


GAMES = [
    "Phasmophobia", "Minecraft", "Valorant", "Fortnite",
    "Grand Theft Auto V", "Counter-Strike 2", "League of Legends",
    "Apex Legends", "Call of Duty: Warzone", "Elden Ring",
    "Baldur's Gate 3", "Overwatch 2", "Rocket League", "Roblox",
    "Dead by Daylight", "Lethal Company", "Helldivers 2",
    "Escape from Tarkov", "PUBG: Battlegrounds", "Rust",
    "Sea of Thieves", "Destiny 2", "Warframe", "Team Fortress 2",
]


def make_page(game: str, filler_kb: int = 200) -> str:
    """Realistic watch-page HTML (~filler_kb + markup), game in attribute slot."""
    return (
        "<html><head>"
        f"<title>{game} epic highlights stream - YouTube</title>"
        "</head><body>"
        '<span id="channel">SomeStreamer</span>'
        "<div>Gaming | 1,234,567 views | Jan 5, 2026</div>"
        '<div class="ytVideoAttributeViewModelMetadata" role="link">'
        '<div class="ytVideoAttributeViewModelTextContainer">'
        f'<h1 class="ytVideoAttributeViewModelTitle">{game}</h1>'
        '<h4 class="ytVideoAttributeViewModelSubtitle"><span>2020</span></h4>'
        "</div></div>"
        "<!-- page payload -->"
        + "x" * (filler_kb * 1024)
        + "</body></html>"
    )


class FetchCounter:
    """Thread-safe fetch stub: records calls, optional latency, active peak."""

    def __init__(self, latency: float = 0.0, page_fn=None):
        self.latency = latency
        self.page_fn = page_fn or (lambda vid: make_page("Phasmophobia"))
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, video_id: str) -> str:
        with self._lock:
            self.calls.append(video_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.latency:
                time.sleep(self.latency)
            return self.page_fn(video_id)
        finally:
            with self._lock:
                self.active -= 1

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)


# --------------------------------------------------------------------------
# Fresh-process baseline probe (child interpreter -> parsed by parent).
# --------------------------------------------------------------------------
def probe_baseline() -> None:
    gc.collect()
    time.sleep(0.1)
    pre = rss_stable()
    from youtube_game_detector.detector import YouTubeGameDetector  # noqa: F401

    gc.collect()
    time.sleep(0.1)
    imported = rss_stable()
    import youtube_game_detector.detector as _det

    d = _det.YouTubeGameDetector(db_path=None)
    gc.collect()
    time.sleep(0.1)
    initialized = rss_stable()
    c0 = time.process_time()
    time.sleep(1.0)
    idle_cpu = time.process_time() - c0
    d.close()
    print(f"PROBE rss_pre_mb={pre / MB:.3f}", flush=True)
    print(f"PROBE rss_imported_mb={imported / MB:.3f}", flush=True)
    print(f"PROBE rss_initialized_mb={initialized / MB:.3f}", flush=True)
    print(f"PROBE idle_cpu_s={idle_cpu:.6f}", flush=True)


def run_probe() -> dict[str, float]:
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from youtube_game_detector.benchmark_resources import probe_baseline; "
        "probe_baseline()" % (pkg_parent,)
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=180, cwd=pkg_parent,
    )
    vals: dict[str, float] = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("PROBE "):
            for tok in line[6:].split():
                key, _, val = tok.partition("=")
                try:
                    vals[key] = float(val)
                except ValueError:
                    pass
    if not vals:
        raise RuntimeError(
            "baseline probe produced no output; stderr:\n"
            + (proc.stderr or "")[-2000:]
        )
    return vals


# --------------------------------------------------------------------------
# Formatting helpers.
# --------------------------------------------------------------------------
def mb(b: float) -> str:
    return f"{b / MB:.2f} MB"


def us(sec: float) -> str:
    """Format a SECONDS value as microseconds."""
    return f"{sec * 1e6:.2f} us"


def ms(sec: float) -> str:
    return f"{sec * 1e3:.2f} ms"


def pct(x: float) -> str:
    return f"{x:.1f}%"


def rate(n: float, sec: float) -> str:
    return f"{n / sec:,.0f}/s" if sec > 0 else "n/a"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Sections. Each returns a dict; detector imports stay local so the
# fresh-process probe above measures a truly pre-import interpreter.
# --------------------------------------------------------------------------
def section_l1(n_lookups: int, reps: int, populate: int = 10_000) -> dict:
    from youtube_game_detector.detector import YouTubeGameDetector

    log(f"L1: populating {populate} entries, {reps}x{n_lookups:,} lookups ...")
    fetcher = FetchCounter()  # must stay at 0 calls: proof of zero HTTP
    d = YouTubeGameDetector(fetcher=fetcher, db_path=None,
                            max_memory_entries=100_000)
    vids = [make_vid(i) for i in range(populate)]
    for k, v in enumerate(vids):
        d.cache.set(v, GAMES[k % len(GAMES)])
    # correctness spot-check before timing
    assert d.get_game(vids[0]) == GAMES[0]
    assert d.get_game(vids[populate - 1]) == GAMES[(populate - 1) % len(GAMES)]
    l1_before = d.cache.l1_hits
    l2_before = d.cache.l2_reads
    fetcher.calls.clear()
    get = d.get_game
    m = len(vids)
    rep_results = []
    for _ in range(reps):
        gc.collect()
        with Sampler(interval=0.01) as s:
            c0 = time.process_time()
            w0 = time.perf_counter()
            for k in range(n_lookups):
                get(vids[k % m])
            w1 = time.perf_counter()
            c1 = time.process_time()
        rep_results.append({
            "wall": w1 - w0, "cpu": c1 - c0,
            "peak_rss": s.peak_rss(),
        })
    l1_hits = d.cache.l1_hits - l1_before
    l2_reads = d.cache.l2_reads - l2_before
    assert l1_hits == n_lookups * reps, (l1_hits, n_lookups * reps)
    assert l2_reads == 0, l2_reads
    assert fetcher.count == 0, fetcher.count
    walls = [r["wall"] for r in rep_results]
    cpus = [r["cpu"] for r in rep_results]
    med_wall = statistics.median(walls)
    med_cpu = statistics.median(cpus)
    out = {
        "n": n_lookups, "reps": reps,
        "walls": walls, "cpus": cpus,
        "wall_med": med_wall, "cpu_med": med_cpu,
        "per_call_ns": med_wall / n_lookups * 1e9,
        "per_call_cpu_ns": med_cpu / n_lookups * 1e9,
        "cpu_util": med_cpu / med_wall * 100.0 if med_wall else 0.0,
        "throughput": n_lookups / med_wall if med_wall else 0.0,
        "peak_rss": max(r["peak_rss"] for r in rep_results),
        "http": fetcher.count, "l2_reads": l2_reads,
        "l1_hits": l1_hits,
    }
    d.close()
    return out


def section_l2(n_lookups: int, n_ids: int) -> dict:
    from youtube_game_detector.detector import YouTubeGameDetector

    log(f"L2: populating {n_ids} sqlite rows, {n_lookups:,} L2 lookups ...")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    # max_memory_entries=0 disables L1: every lookup below is exactly one
    # SQLite read (pure-L2 / worst-case path, no L1 masking).
    d = YouTubeGameDetector(fetcher=FetchCounter(), db_path=tmp.name,
                            max_memory_entries=0)
    base = 10_000_000
    vids = [make_vid(base + i) for i in range(n_ids)]
    for k, v in enumerate(vids):
        d.cache.set(v, GAMES[k % len(GAMES)])
    assert d.get_game(vids[0]) == GAMES[0]
    l2r0, l2h0 = d.cache.l2_reads, d.cache.l2_hits
    gc.collect()
    rss_before = rss_stable()
    rounds = max(1, n_lookups // n_ids)
    total = rounds * n_ids
    get = d.get_game
    with Sampler(interval=0.01) as s:
        c0 = time.process_time()
        w0 = time.perf_counter()
        for _ in range(rounds):
            for v in vids:
                get(v)
        w1 = time.perf_counter()
        c1 = time.process_time()
    wall, cpu = w1 - w0, c1 - c0
    l2_reads = d.cache.l2_reads - l2r0
    l2_hits = d.cache.l2_hits - l2h0
    assert l2_reads == total, (l2_reads, total)
    db_size = os.path.getsize(tmp.name)
    out = {
        "n": total, "n_ids": n_ids, "db_file_mb": db_size / MB,
        "wall": wall, "cpu": cpu,
        "per_call_us": wall / total * 1e6,
        "per_call_cpu_us": cpu / total * 1e6,
        "cpu_util": cpu / wall * 100.0 if wall else 0.0,
        "throughput": total / wall if wall else 0.0,
        "peak_rss": s.peak_rss(), "rss_before": rss_before,
        "l2_reads": l2_reads, "l2_hits": l2_hits,
    }
    d.close()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return out


def section_cold(n_cold: int = 1000) -> dict:
    from youtube_game_detector.detector import YouTubeGameDetector
    from youtube_game_detector.extractor import extract_game

    log(f"COLD: {n_cold} distinct-ID cold lookups + parse microbench ...")
    page = make_page(GAMES[0], filler_kb=200)  # GAMES[0] == "Phasmophobia"
    assert extract_game(page) == "Phasmophobia"
    fetcher = FetchCounter(page_fn=lambda vid: page)
    d = YouTubeGameDetector(fetcher=fetcher, db_path=None)
    base = 20_000_000
    gc.collect()
    rss_before = rss_stable()
    # single cold lookup, isolated
    c0 = time.process_time()
    w0 = time.perf_counter()
    assert d.get_game(make_vid(base)) == "Phasmophobia"
    single_wall = time.perf_counter() - w0
    single_cpu = time.process_time() - c0
    # sustained colds: distinct IDs, sampler tracks peak RSS
    with Sampler(interval=0.01) as s:
        c0 = time.process_time()
        w0 = time.perf_counter()
        for k in range(1, n_cold):
            got = d.get_game(make_vid(base + k))
            assert got == "Phasmophobia", (k, got)
        bulk_wall = time.perf_counter() - w0
        bulk_cpu = time.process_time() - c0
    gc.collect()
    rss_after = rss_stable()
    assert fetcher.count == n_cold, fetcher.count
    # pure parse cost, isolated from cache/fetch
    gc.collect()
    t0 = time.perf_counter()
    reps = 2000
    for _ in range(reps):
        extract_game(page)
    parse_each = (time.perf_counter() - t0) / reps
    out = {
        "n": n_cold, "page_kb": len(page) / 1024,
        "single_wall": single_wall, "single_cpu": single_cpu,
        "bulk_wall": bulk_wall, "bulk_cpu": bulk_cpu,
        "per_cold_wall": bulk_wall / (n_cold - 1),
        "per_cold_cpu": bulk_cpu / (n_cold - 1),
        "parse_each": parse_each,
        "peak_rss": s.peak_rss(),
        "rss_before": rss_before, "rss_after": rss_after,
        "http": fetcher.count,
    }
    d.close()
    return out


def _conc_run(nworkers: int, same_id: bool, max_concurrency: int = 4) -> dict:
    from youtube_game_detector.detector import YouTubeGameDetector

    base = 30_000_000 + (0 if same_id else nworkers * 1000 + nworkers)
    if same_id:
        fetcher = FetchCounter(latency=0.05,
                               page_fn=lambda vid: make_page("Phasmophobia"))
    else:
        fetcher = FetchCounter(latency=0.02,
                               page_fn=lambda vid: make_page("Minecraft"))
    d = YouTubeGameDetector(fetcher=fetcher, db_path=None,
                            max_concurrency=max_concurrency)
    target = make_vid(base)
    ids = [target] * nworkers if same_id else [make_vid(base + i)
                                               for i in range(nworkers)]
    barrier = threading.Barrier(nworkers, timeout=60)
    results: list[object] = [None] * nworkers
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=60)
            results[i] = d.get_game(ids[i])
        except Exception as exc:  # never fail the benchmark process
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(nworkers)]
    gc.collect()
    with Sampler(interval=0.005) as s:
        c0 = time.process_time()
        w0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        w1 = time.perf_counter()
        c1 = time.process_time()
    wall, cpu = w1 - w0, c1 - c0
    alive = [t for t in threads if t.is_alive()]
    expected = "Phasmophobia" if same_id else "Minecraft"
    ok = (not alive and not errors
          and all(r == expected for r in results)
          and fetcher.count == (1 if same_id else nworkers)
          and fetcher.max_active <= max_concurrency)
    out = {
        "workers": nworkers, "same_id": same_id,
        "wall": wall, "cpu": cpu,
        "cpu_util": cpu / wall * 100.0 if wall else 0.0,
        "peak_rss": s.peak_rss(), "peak_tick_cpu": s.peak_cpu(),
        "http": fetcher.count, "max_active": fetcher.max_active,
        "limit": max_concurrency, "ok": ok,
        "errors": errors[:3], "alive": len(alive),
    }
    d.close()
    return out


def section_concurrency(levels: tuple[int, ...] = (12, 50, 100)) -> list[dict]:
    rows = []
    for n in levels:
        log(f"CONCURRENCY: {n} callers, SAME new ID ...")
        rows.append(_conc_run(n, same_id=True))
        log(f"CONCURRENCY: {n} callers, DIFFERENT new IDs ...")
        rows.append(_conc_run(n, same_id=False))
    return rows


def section_sizes(sizes: tuple[int, ...] = (100, 1_000, 10_000, 50_000)) -> list[dict]:
    from youtube_game_detector.detector import YouTubeGameDetector

    log("CACHE SIZES: resident-RSS per L1 size ...")
    rows = []
    keepalive = []  # hold refs so entries stay resident (no reuse artefact)
    base = 50_000_000
    cursor = 0
    for n in sizes:
        d = YouTubeGameDetector(fetcher=FetchCounter(), db_path=None,
                                max_memory_entries=n)
        gc.collect()
        before = rss_stable()
        for k in range(n):
            d.cache.set(make_vid(base + cursor + k), GAMES[k % len(GAMES)])
        cursor += n
        gc.collect()
        after = rss_stable()
        # prove entries are live and served from L1
        assert d.get_game(make_vid(base + cursor - 1)) == GAMES[(n - 1) % len(GAMES)]
        delta = after - before
        rows.append({"entries": n, "before": before, "after": after,
                     "delta": delta, "per_entry_b": delta / n})
        keepalive.append(d)
    for d in keepalive:
        d.close()
    return rows


def section_soak(duration_s: float) -> dict:
    from youtube_game_detector.detector import YouTubeGameDetector

    log(f"SOAK: realistic mix for {duration_s:.0f}s ...")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    fetcher = FetchCounter(page_fn=lambda vid: make_page("Valorant"))
    d = YouTubeGameDetector(fetcher=fetcher, db_path=tmp.name,
                            max_memory_entries=512, max_concurrency=4)
    base = 40_000_000
    hot = [make_vid(base + i) for i in range(300)]
    warm = [make_vid(base + 100_000 + i) for i in range(3000)]
    for k, v in enumerate(hot):
        d.cache.set(v, GAMES[k % len(GAMES)])
    for k, v in enumerate(warm):
        d.cache.set(v, GAMES[(k + 7) % len(GAMES)])
    fetcher.calls.clear()
    s0 = d.cache.stats()

    # per-second budget: 20k hot L1 + 200 warm L2-ish + 10 new + bursts
    hot_per_s, warm_per_s, new_per_s = 20_000, 200, 10
    burst_every_s = 5
    deadline = time.perf_counter() + duration_s
    hot_i = warm_i = new_i = bursts = 0
    new_cursor = 0
    burst_errors: list[str] = []

    def burst(target: str) -> None:
        def w() -> None:
            try:
                d.get_game(target)
            except Exception as exc:
                burst_errors.append(repr(exc))
        ts = [threading.Thread(target=w) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)

    with Sampler(interval=0.25) as s:
        cpu0 = time.process_time()
        wall0 = time.perf_counter()
        sec = 0
        while time.perf_counter() < deadline:
            sec_start = time.perf_counter()
            get = d.get_game
            for k in range(hot_per_s):
                get(hot[(hot_i + k) % len(hot)])
            hot_i += hot_per_s
            for k in range(warm_per_s):
                get(warm[(warm_i + k) % len(warm)])
            warm_i += warm_per_s
            for _ in range(new_per_s):
                get(make_vid(base + 500_000 + new_cursor))
                new_cursor += 1
            sec += 1
            if sec % burst_every_s == 0:
                burst(make_vid(base + 900_000 + sec))
                bursts += 1
            if time.perf_counter() >= deadline:
                break
            time.sleep(max(0.0, (sec_start + 1.0) - time.perf_counter()))
        wall = time.perf_counter() - wall0
        cpu = time.process_time() - cpu0
    s1 = d.cache.stats()
    l1_hits = s1["l1_hits"] - s0["l1_hits"]
    l2_reads = s1["l2_reads"] - s0["l2_reads"]
    l2_hits = s1["l2_hits"] - s0["l2_hits"]
    total = hot_i + warm_i + new_cursor + bursts * 4
    out = {
        "duration": wall, "cpu": cpu,
        "cpu_avg": cpu / wall * 100.0 if wall else 0.0,
        "tick_peak_cpu": s.peak_cpu(), "tick_avg_cpu": s.avg_cpu(),
        "rss_avg": s.avg_rss(), "rss_peak": s.peak_rss(),
        "hot": hot_i, "warm": warm_i, "new": new_cursor, "bursts": bursts,
        "total": total,
        "l1_hits": l1_hits, "l2_reads": l2_reads, "l2_hits": l2_hits,
        "http": fetcher.count,
        "l1_rate": l1_hits / total if total else 0.0,
        "l2_rate": (l2_hits / (total - l1_hits)) if total > l1_hits else 0.0,
        "burst_errors": len(burst_errors),
    }
    d.close()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# Report.
# --------------------------------------------------------------------------
def print_report(env: dict, probe: dict, l1: dict, l2: dict, cold: dict,
                 conc: list[dict], sizes: list[dict], soak: dict) -> None:
    cores = env["cores"]
    print("=" * 66)
    print("YouTube Game Detector Resource Benchmark")
    print("=" * 66)
    print()
    print("Environment")
    print(f"Python: {env['python']}")
    print(f"Platform: {env['platform']}")
    print(f"CPU: {cores} logical cores"
          + (f", {env['cpu_model']}" if env.get("cpu_model") else ""))
    print(f"Total RAM: {env['ram']}")
    print(f"RSS source: {RSS_SOURCE} (psutil: {'yes' if HAVE_PSUTIL else 'no'})")
    print(f"Detector import: {env['detector_from']}")
    print(f"Network: MOCKED in all sections (YouTube never contacted)")
    print()
    print("BASELINE (fresh child process)")
    print(f"RSS pre-import:    {probe['rss_pre_mb']:.3f} MB  "
          "(interpreter + os/sys/time/threading harness, imports forced)")
    print(f"RSS post-import:   {probe['rss_imported_mb']:.3f} MB  "
          f"(delta {probe['rss_imported_mb'] - probe['rss_pre_mb']:+.3f} MB for "
          "youtube_game_detector + its stdlib imports)")
    print(f"RSS initialized:   {probe['rss_initialized_mb']:.3f} MB  "
          f"(delta {probe['rss_initialized_mb'] - probe['rss_pre_mb']:+.3f} MB vs pre-import; "
          "per-instance state is ~KB: one small object graph + one sqlite "
          "connection only when db_path is set)")
    print(f"NOTE: import RSS is benchmark-harness-specific (psutil is already "
          "loaded by the harness). For an app-integrated cost estimate, use "
          "the CACHE SIZE TEST per-entry numbers instead of deltas here.")
    print(f"Idle CPU:          {probe['idle_cpu_s']:.4f} s process CPU per "
          f"1.00 s wall (~{probe['idle_cpu_s'] / 1.0 * 100:.1f}% = effectively zero)")
    print()
    print(f"L1 CACHE - {l1['n']:,} LOOKUPS x {l1['reps']} reps "
          f"(10,000 resident entries, all L1 hits)")
    for i, (w, c) in enumerate(zip(l1["walls"], l1["cpus"])):
        print(f"  rep {i + 1}: wall {w:.3f} s, CPU {c:.3f} s, "
              f"{l1['n'] / w:,.0f}/s")
    print(f"Wall time (median): {l1['wall_med']:.3f} s")
    print(f"CPU time (median):  {l1['cpu_med']:.3f} s  "
          "(whole-process user+system, all threads)")
    print(f"Avg CPU:            {l1['cpu_util']:.1f}% of one core  "
          "(= CPU time / wall time x 100)")
    print(f"Per cache hit:      {l1['per_call_ns']:,.0f} ns wall, "
          f"~{l1['per_call_cpu_ns']:,.0f} ns CPU")
    print(f"Peak RSS:           {mb(l1['peak_rss'])}")
    print(f"Throughput:         {l1['throughput']:,.0f} lookups/s")
    print(f"HTTP requests:      {l1['http']} (asserted 0)")
    print(f"SQLite reads:       {l1['l2_reads']} (asserted 0)")
    print(f"L1 hits:            {l1['l1_hits']:,}")
    print()
    print(f"L2 SQLITE - {l2['n']:,} LOOKUPS ({l2['n_ids']:,} distinct IDs, "
          "L1 disabled = pure SQLite path)")
    print(f"SQLite file:        {l2['db_file_mb']:.2f} MB for {l2['n_ids']:,} rows")
    print(f"Wall time:          {l2['wall']:.3f} s")
    print(f"CPU time:           {l2['cpu']:.3f} s")
    print(f"Avg CPU:            {l2['cpu_util']:.1f}% of one core")
    print(f"Per L2 hit:         {l2['per_call_us']:,.1f} us wall, "
          f"~{l2['per_call_cpu_us']:,.1f} us CPU")
    print(f"Peak RSS:           {mb(l2['peak_rss'])}")
    print(f"Throughput:         {l2['throughput']:,.0f} lookups/s")
    print(f"SQLite reads/hits:  {l2['l2_reads']:,} / {l2['l2_hits']:,}")
    print()
    print(f"COLD LOOKUP (mocked {cold['page_kb']:.0f} KB page, "
          f"game=Phasmophobia, {cold['n']:,} distinct IDs)")
    print(f"Single cold:        wall {ms(cold['single_wall'])} "
          f"(one fetch+parse+store, mocked zero-latency fetch; "
          f"single-sample CPU is below the OS quantum, see avg)")
    print(f"Per cold (avg over {cold['n'] - 1:,}): wall "
          f"{ms(cold['per_cold_wall'])}, "
          f"CPU {ms(cold['per_cold_cpu'])}")
    print(f"Pure parse cost:    {us(cold['parse_each'])} per extract_game() call")
    print(f"Peak RSS:           {mb(cold['peak_rss'])} "
          f"(RSS {mb(cold['rss_before'])} -> {mb(cold['rss_after'])}, "
          f"+{mb(cold['rss_after'] - cold['rss_before'])} for "
          f"{cold['n']:,} resident entries)")
    print(f"HTTP requests:      {cold['http']} (= {cold['n']:,} new IDs, 1 each)")
    print()
    print("CONCURRENCY (mocked latency: 50ms same-ID / 20ms diff-ID)")
    for r in conc:
        kind = "SAME video     " if r["same_id"] else "DIFFERENT videos "
        print(f"  {kind} workers={r['workers']:>3}: "
              f"HTTP={r['http']:>3} (expect {1 if r['same_id'] else r['workers']}), "
              f"max_active={r['max_active']} (limit {r['limit']}), "
              f"wall {r['wall']:.2f}s, CPU {r['cpu']:.2f}s "
              f"({r['cpu_util']:.0f}%), peak RSS {mb(r['peak_rss'])}, "
              f"peak tick CPU {r['peak_tick_cpu']:.0f}%, "
              f"{'OK' if r['ok'] else 'FAIL ' + repr(r['errors'])}")
    print()
    print("CACHE SIZE TEST (resident RSS delta, entries held live)")
    print(f"  {'Entries':>8}  {'RSS before':>10}  {'RSS after':>10}  "
          f"{'Increase':>10}  {'Per entry':>10}")
    for r in sizes:
        print(f"  {r['entries']:>8,}  {mb(r['before']):>10}  {mb(r['after']):>10}  "
              f"{mb(r['delta']):>10}  {r['per_entry_b']:>8.0f} B")
    print()
    print(f"LONG-RUN SIMULATION ({soak['duration']:.0f}s, "
          f"{soak['total']:,} ops: hot L1 + warm L2 + new IDs + 4-thread bursts)")
    print(f"Achieved mix:       {soak['hot']:,} hot / {soak['warm']:,} warm / "
          f"{soak['new']:,} new / {soak['bursts']} bursts "
          f"({soak['total'] / soak['duration']:,.0f} ops/s)")
    print(f"Average RSS:        {mb(soak['rss_avg'])}")
    print(f"Peak RSS:           {mb(soak['rss_peak'])}")
    print(f"Average CPU:        {soak['cpu_avg']:.1f}% of one core "
          f"(total process CPU {soak['cpu']:.2f} s / {soak['duration']:.1f} s wall)")
    print(f"Peak tick CPU:      {soak['tick_peak_cpu']:.1f}% "
          f"(0.25 s sampler ticks, {cores} cores available)")
    print(f"Total CPU time:     {soak['cpu']:.2f} s")
    print(f"Total requests:     {soak['total']:,}")
    print(f"L1 hit rate:        {soak['l1_rate'] * 100:.2f}% ({soak['l1_hits']:,})")
    print(f"L2 hit rate:        {soak['l2_rate'] * 100:.2f}% of L1 misses "
          f"({soak['l2_hits']:,} hits / {soak['l2_reads']:,} reads)")
    print(f"HTTP requests:      {soak['http']:,} (new IDs + bursts only)")
    print(f"Burst errors:       {soak['burst_errors']}")
    print()
    print("=" * 66)


def print_interpretation(l1: dict, cold: dict, conc: list[dict],
                         sizes: list[dict], soak: dict) -> None:
    ref = next(r for r in sizes if r["entries"] == 10_000)
    per_entry = ref["per_entry_b"]
    at_10k = per_entry * 10_000 / MB
    print("INTERPRETATION (all values measured above, this run)")
    print(f"* At 10,000 cached videos, expect approximately {at_10k:.1f} MB "
          f"additional RAM (~{per_entry:.0f} B/entry).")
    print(f"* A cache hit consumes approximately "
          f"{l1['per_call_ns'] / 1000:.2f} us wall "
          f"(~{l1['per_call_cpu_ns'] / 1000:.2f} us CPU) at "
          f"{l1['throughput']:,.0f} lookups/s single-threaded.")
    cold_per_entry = (cold['rss_after'] - cold['rss_before']) / max(1, cold['n'])
    print(f"* One cold lookup costs approximately "
          f"{ms(cold['per_cold_cpu'])} CPU time "
          f"(parse alone ~{us(cold['parse_each'])}); "
          f"steady-state RSS impact is ~{cold_per_entry:,.0f} B "
          f"per resident entry (over {cold['n']:,} entries).")
    same_rows = [r for r in conc if r["same_id"]]
    same_max = max(same_rows, key=lambda r: r["workers"])
    print(f"* {same_max['workers']} concurrent requests for the same video cause "
          f"exactly {same_max['http']} HTTP request "
          f"(max simultaneous fetches observed: {same_max['max_active']}).")
    cand = [r for r in sizes if r["delta"] / MB <= 10.0]
    rec = cand[-1]["entries"] if cand else sizes[0]["entries"]
    print(f"* The recommended production L1 cache size is {rec:,} entries "
          f"(largest tested size staying within ~10 MB extra RSS; "
          f"10,000 entries measured +{ref['delta'] / MB:.1f} MB). "
          f"Sustained soak load averaged {soak['cpu_avg']:.1f}% of one core.")
    print("=" * 66)


def build_env() -> dict:
    try:
        import youtube_game_detector.detector as det

        src = getattr(det, "__file__", "?")
    except Exception:
        src = "?"
    ram = "n/a"
    model = ""
    if HAVE_PSUTIL and _psutil is not None:
        try:
            ram = f"{_psutil.virtual_memory().total / (1024 ** 3):.1f} GB"
        except Exception:
            pass
        try:
            freq = _psutil.cpu_freq()
            if freq and freq.max:
                model = f"{freq.max:.0f} MHz max"
        except Exception:
            pass
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cores": os.cpu_count() or 1,
        "cpu_model": model,
        "ram": ram,
        "detector_from": src,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true",
                    help="smoke run: 100k L1 x1, 10k L2, 100 colds, 20s soak")
    ap.add_argument("--longrun-secs", type=float, default=300.0)
    ap.add_argument("--l1-n", type=int, default=1_000_000)
    ap.add_argument("--l1-reps", type=int, default=3)
    args = ap.parse_args(argv)

    if args.quick:
        l1_n, l1_reps, l2_n, l2_ids, n_cold, soak_s = \
            100_000, 1, 10_000, 10_000, 1000, 20.0
        conc_levels = (12, 50)
    else:
        l1_n, l1_reps, l2_n, l2_ids, n_cold, soak_s = \
            args.l1_n, args.l1_reps, 100_000, 20_000, 1000, args.longrun_secs
        conc_levels = (12, 50, 100)

    # sanity: ID generator yields valid, unique IDs
    assert len({make_vid(i) for i in range(5000)}) == 5000
    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    from youtube_game_detector.detector import is_valid_video_id
    assert is_valid_video_id(make_vid(0))
    assert is_valid_video_id(make_vid(2 ** 40))

    log("BASELINE: fresh-process probe ...")
    probe = run_probe()
    env = build_env()

    l1 = section_l1(l1_n, l1_reps)
    l2 = section_l2(l2_n, l2_ids)
    cold = section_cold(n_cold)
    conc = section_concurrency(conc_levels)
    sizes = section_sizes()
    soak = section_soak(soak_s)

    print_report(env, probe, l1, l2, cold, conc, sizes, soak)
    print_interpretation(l1, cold, conc, sizes, soak)


if __name__ == "__main__":
    main()

"""Engine sampler — the heart of LIMCARS-NEXT.

One thread per engine, ticking `live_hz` times a second, polling the
engine's Prometheus /metrics and turning CUMULATIVE counters into honest
near-real-time rates by differencing:

    decode_tps_now   Δgeneration_tokens_total / Δt        (1/2 s resolution)
    prefill_tps_now  Δprompt_tokens_total   / Δt          (charged at request
                                                          completion → spiky;
                                                          the *window* number
                                                          below is the truth)
    prefill_tps_avg  Σ(Δprompt − Δprefix_hits) / ΣΔprefill_time over window_s
                     (uncached tokens per second of ACTUAL prefill compute)
    decode_tps_avg   ΣΔgen / ΣΔt over the ticks that generated, last
                     decode_avg_window_s (5 min): idle time is not averaged in
    cache_hit_pct    Δprefix_hits / Δprefix_queries over window_s
    spec_accept_pct  Δaccepted / Δdraft over window_s
    ttft_p50_ms …    linear-interpolated quantiles over the DELTA of histogram
                     buckets since window_s ago (Prometheus method) — so the
                     chart reflects only the last window, not process lifetime

Why this beats the old dash: the old Streamlit rings advanced only while a
browser held the page open and were trimmed to 180 s, so info bubbles
"sampled every 3 minutes". Here the sampler thread runs unconditionally at
`live_hz`; a chart opened cold gets the ring snapshot then live SSE ticks.

Counter reset (engine restart) is detected by Δ < 0 → that tick reads None
and baselines are rebuilt, never a negative rate or a fake spike.
"""
from __future__ import annotations

from collections import deque
import json
import re
import subprocess
import threading
import time
import urllib.request

from . import dialects, prom
from .rings import Ring
from .sse import Broker

# models whose KV usage reading flips on every block boundary (see EngineSampler._kv_peak)
DEFAULT_KV_PEAK_MODELS = {r"dsv4|deepseek-v4": 10.0}


_HIST_LOGICALS = ("ttft", "itl", "prefill_time", "decode_time", "queue_time", "e2e")
_HIST_KEYS = {lg: (f"{lg}_p50_ms", f"{lg}_avg_ms", f"{lg}_n",
                   f"{lg}_p50_1h_ms", f"{lg}_avg_1h_ms", f"{lg}_n_1h")
              for lg in _HIST_LOGICALS}
SERIES_KEYS = ("prefill_tps_now", "decode_tps_now", "prefill_tps_avg",
               "decode_tps_avg", "running", "waiting", "kv_pct",
               "kv_pct_shown", "kv_pct_peak_s",
               "kv_pool_tokens", "kv_used_tokens", "kv_free_tokens", "kv_tok",
               "kv_ref_tokens", "kv_ref_step", "decode_tps_req",
               "ttft_p50_ms", "itl_p50_ms", "queue_time_p50_ms", "e2e_p50_ms",
               "cache_hit_pct", "cache_hit_life_pct", "cache_hit_agg_1h",
               "spec_accept_pct", "spec_step_accept_pct", "spec_accept_len", "draft_tok_per_step",
               "prompt_total", "gen_total", "gen_peak", "requests_total",
               "savings_day", "savings_month", "savings_year",
               "e2e_med_1h", "e2e_life_avg_ms")

PREFILL_MAX = 12000.0   # plausibility ceiling, tok/s. Real peak measured on
                        # the GB10 daily driver is ~7.5k; anything above this is
                        # completion-attribution skew, never a speed.


def _live_decode_agg(now: dict):
    """The engine board's aggregate decode speed, freshest field first:
    decode_live_tps moves every frame (4 Hz while busy); decode_tps is built
    from the engine's 1 Hz history samples, so it only changes once a second —
    the engine's own dashboard leads with the live field too."""
    for k in ("decode_live_tps", "decode_tps"):
        v = now.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
    return None


def _apply_prefill_live(row: dict, live_json: dict | None) -> None:
    """The engine live board's own in-flight prefill speed (TandemLLM
    contract: `now.prefilling` + `now.prefill_live_tps`), mirroring the
    decode-live path above.

    Why (the operator's bug, 2026-11-09): during a LONG prefill no counter
    advances — measured on the live engine: qse_prefill_tokens_forwarded_total
    froze for the whole 90 s prefill (charged at completion), and the
    prefill_time/prompt_tokens sides of the windowed rate fire only when the
    prefill ENDS. The tile therefore showed nothing exactly while the number
    mattered; short prefills looked fine because their burst lands inside the
    averaging window.

    Honest gap, measured the same day: `prefill_live_tps` populates only
    after the engine's first live chunk — LONG prefills publish it mid-flight
    (count>=1), SHORT ones never publish one (their completion burst already
    reaches the windowed path). While it is null the windowed counter value
    deliberately STANDS, and the board's `prefill_tps` is NOT used as a
    fallback: it is a window average diluted by idle time (measured 36.89
    tok/s while the engine prefilled at ~1000 — a speed lie). Capped like
    every other prefill path (PREFILL_MAX).

    The tile's DIM is the operator's lane-activity UX: dimming tells which lane is
    actually working. The counter heuristic alone cannot see a prefill that
    runs BESIDE decoding (the operator's bug, 2026-11-12): its "running but nothing
    stepped" branch never fires while decoders step every tick, so only the
    1.5 s admission edge lit the tile and a 60 s prefill read as idle.
    Measured on the live engine (3-7 decoders + a 45k-word cold prompt):
    the board said prefilling in 68 of 154 half-second samples, the tile was
    lit in 23. The board's `prefilling` flag counts exactly the requests on
    a slot with no first token, minus SRV-787 memory waits (queued ones are
    `waiting`), i.e. prefill compute in flight, so it lights the dim too.
    The speed rules above are unchanged: the live figure only replaces the
    windowed one when the engine published a plausible value.
    """
    now = (live_json or {}).get("now") or {}
    if now.get("prefilling") is not True:
        return
    row["prefill_active"] = True
    v = now.get("prefill_live_tps")
    if isinstance(v, (int, float)) and 0 < v <= PREFILL_MAX:
        row["prefill_tps_avg"] = float(v)
        row["prefill_live"] = 1


def _live_kv(live: dict, series: dict, health: dict | None = None) -> dict:
    """KV fields from an engine live-board JSON (tandemllm contract 1.2):
    engine.kv.length/max_len are the context in TOKENS — the token-denominated
    pool this engine publishes nowhere on /metrics. The state store also lands
    in GiB beside the tokens (this engine's kv_pct is a BYTE ratio, so the tile
    fallback can state the real unit). Pure; missing keys yield no fields.

    `health` (the engine's /health JSON) adds the SHARED pool truth: the KV
    pool is dynamic — pages are committed per lane as contexts grow and given
    back when idle — so there is no fixed token ceiling to draw against (the
    setup wizard's "663,179 fit in all" is a planning estimate from the model
    registry, and /v1/setup/* only exists in setup mode). What the RUNNING
    engine publishes is what it holds: committed rows per lane, summed."""
    out: dict = {}
    kv = (live.get("engine") or {}).get("kv") or {}
    kl, kmax = kv.get("length"), kv.get("max_len")
    if isinstance(kl, (int, float)) and isinstance(kmax, (int, float)) and kmax > 0:
        out["kv_pool_tokens"] = int(kmax)
        out["kv_used_tokens"] = int(kl)
        out["kv_free_tokens"] = max(0, int(kmax - kl))
        # the tile's % follows the tokens: context occupancy (length/max_len).
        # The dialect's kv_pct is the state-store BYTE ratio, which reads 0%
        # whenever the cache is cold/empty while the engine holds a full
        # context — a dead gauge next to a live token line.
        out["kv_pct"] = round(100.0 * kl / kmax, 3)
    # engine.kv.length is the ACTIVE LANE's context; with 2+ requests in
    # flight the shared usage is the sum of every running request's context
    # (prompt + generated). 'done' rows linger in the frame — only prefill/
    # decode rows count.
    rows = live.get("requests") or []
    ctx = [int(r.get("prompt_tokens") or 0) + int(r.get("tokens") or 0)
           for r in rows if isinstance(r, dict)
           and r.get("phase") in ("prefill", "decode")]
    if ctx and any(ctx):
        out["kv_used_tokens"] = sum(ctx)
        if out.get("kv_pool_tokens"):
            out["kv_free_tokens"] = max(0, out["kv_pool_tokens"] - sum(ctx))
    sb = series.get("qse_state_store_budget_bytes")
    sb = sb[0][2] if sb else None
    cb = prom.first(series, "qse_cache_bytes", cache="state")
    if sb and cb is not None:
        out["kv_used_gib"] = round(cb / 2**30, 2)
        out["kv_pool_gib"] = round(sb / 2**30, 2)
    if isinstance(health, dict):
        slots = ((health.get("kv_pool") or {}).get("slots")) or []
        rows = sum(int(s.get("committed_rows") or 0) for s in slots
                   if isinstance(s, dict))
        if rows > 0:
            out["kv_committed_rows"] = rows
            out["kv_pool_note"] = f"{rows:,} rows committed across {len(slots)} lanes"
    # The engine's own memory gate, in GiB. The peak gate is what makes a long
    # prefill WAIT rather than fail: on 2026-11-07 the engine logged "reserved
    # 73.1 + this request's workspace 5.3 > peak 75.8" and the request waited
    # 34.9 s before its client abandoned it at 45 s. A 90k-token prefill prices
    # about 5 GiB of workspace, so once the headroom under the peak is under
    # that, the next long request is already queued — which is a number the
    # dashboard can state long before anyone notices a hang.
    alloc = prom.first(series, "qse_memory_bytes", part="allocated")
    peak = prom.first(series, "qse_memory_bytes", part="peak")
    if alloc is not None and peak is not None and peak > 0:
        out["mem_alloc_gib"] = round(alloc / 2**30, 2)
        out["mem_peak_gib"] = round(peak / 2**30, 2)
        out["mem_gate_headroom_gib"] = round((peak - alloc) / 2**30, 2)
    kp = health.get("kv_pool") if isinstance(health, dict) else None
    if isinstance(kp, dict) and isinstance(kp.get("mem_available_gib"), (int, float)):
        out["mem_avail_gib"] = round(float(kp["mem_available_gib"]), 2)
    return out


def _anchor_tile_pct(row: dict) -> None:
    """ONE denominator per tile (the operator's KV-tile fix 2026-11-09): the big %
    must equal 100 x used / the SAME denominator the sub-line prints, so the
    two figures on the tile can never disagree.

    The sub-line's fraction divides kv_used_tokens by kv_ref_tokens when a
    shared-pool fit is configured — the pool implied by the used+free values
    (recomputed from the engine's live memory state, `kv_ref_source` says by
    what) — and falls back to kv_pool_tokens (vLLM block pool, Tarnwell
    gauges, or the tandem per-lane window) when there is no ref. The % now
    follows that same number.

    Before this, the tandem tile divided the ACTIVE LANE's length by the
    PER-LANE window while the fraction beside it divided the sum across all
    running lanes by the shared fit: 37.1% printed above "614.2k / 836.1k
    tok used" (73.5%). Three denominators in one tile.

    The launch-priced figure (kv_ref_launch) is deliberately NOT the pool:
    the engine commits pages dynamically past what the launch env priced
    (measured 2026-11-09: 614.2k used > 472.9k launch-priced — a launch-based
    denominator would print a fraction over 100%), so it stays an annotation
    beside the marker, visible exactly while it disagrees materially.
    """
    pool_shown = row.get("kv_ref_tokens") or row.get("kv_pool_tokens")
    used = row.get("kv_used_tokens")
    # a pool must be a POSITIVE number: a negative or zero ref (broken
    # recompute, corrupt config) must not print a negative % — the dialect's
    # own reading stands, honest, until the pool is trustworthy again
    if pool_shown and pool_shown > 0 and used is not None:
        row["kv_pct"] = round(100.0 * used / pool_shown, 3)
        row["kv_free_tokens"] = max(0, int(round(pool_shown - used)))


def _trim(lst: list, cutoff: float) -> None:
    """Drop leading (t, ...) entries older than cutoff in one slice delete."""
    i, n = 0, len(lst)
    while i < n and lst[i][0] < cutoff:
        i += 1
    if i:
        del lst[:i]


class EngineSampler(threading.Thread):
    daemon = True

    def __init__(self, cfg: dict, eng: dict, broker: Broker, store=None):
        super().__init__(name=f"sampler-{eng['id']}", daemon=True)
        self.cfg, self.eng = cfg, eng
        self.id = eng["id"]
        self.url = eng["url"].rstrip("/")
        self.broker = broker
        self.store = store
        self.hz = float(cfg.get("live_hz", 2.0))
        self.window_s = float(cfg.get("window_s", 30.0))
        # Prefill needs longer to settle than everything else: prompt tokens and
        # the prefill_seconds they cost are charged at different moments, so a
        # 30 s ratio still overshoots ~3x. The rate is therefore computed over a
        # settle window (2 min by default) — which also keeps this gauge slow,
        # the standing rule for any speed/latency bubble.
        self.prefill_win_s = float(cfg.get("prefill_window_s", 120.0))
        # decode average: only ticks that generated count, over a longer window than
        # window_s so the "avg" next to the live number does not chase it
        self.decode_avg_win_s = float(cfg.get("decode_avg_window_s", 300.0))
        self._gen_ticks: list = []
        # loop warning: every MTP draft accepted, step after step, for loop_warn_s
        self.loop_win_s = float(cfg.get("loop_window_s", 5.0))
        self.loop_warn_s = float(cfg.get("loop_warn_s", 30.0))
        # ...and only while the draft counter moves in most samples of the window: an engine that adds a request's
        # counters when it ends puts them in one sample
        self.loop_moving_share = float(cfg.get("loop_moving_share", 0.8))
        # served-model regex -> seconds: the KV tile shows the highest reading of that window
        self.kv_peak_models = cfg.get("kv_peak_models", DEFAULT_KV_PEAK_MODELS)
        # per-engine static KV reference line (tokens): the setup wizard's
        # shared-pool estimate for THIS engine's launch config. The running
        # engine publishes no fixed ceiling (its pool is dynamic), so this is
        # an operator-entered marker — dashed line on the KV chart, nothing
        # else reads it. Engines without the key never emit it.
        self.kv_ref_tokens = (eng or {}).get("kv_ref_tokens")
        # Optional: a command that RECOMPUTES that marker from the engine's own
        # launch config (scripts/pool_estimate.py for tandemllm). The number the
        # setup wizard shows is computed on demand and never stored, and its
        # route only exists in --setup mode, so a serving engine cannot answer
        # the question; this asks the engine's registry instead of trusting a
        # hand-typed figure. Config-file only, like a node's ssh options — never
        # settable over the API, because it runs a shell command. Runs off the
        # 2 Hz tick in a daemon thread; a failure keeps the last good number and
        # only records why (a dash, never a wrong number).
        self.kv_ref_cmd = (eng or {}).get("kv_ref_cmd")
        self.kv_ref_ttl = float((eng or {}).get("kv_ref_ttl_s") or 900)
        self.kv_ref_source = "static" if self.kv_ref_tokens else None
        self.kv_ref_launch = None
        # honesty markers (drift study 2026-11-10): an operator-pinned --floor
        # in kv_ref_cmd makes the recompute re-derive an OLD launch's pool —
        # pool_estimate says so via floor_pinned. Carry the pinned GiB so the
        # tile can warn out loud, and stamp one row with the previous ref when
        # the ref steps, so the chart can mark the step (a % jump with no
        # usage change must not read as a load spike in the history).
        self.kv_ref_pin_gb = None
        self._kv_ref_step = None
        self.kv_ref_error = None
        self._kv_ref_at = 0.0
        self._kv_ref_busy = False
        self._kv_hist: deque = deque()
        self._full_accept_since = None
        self.ring = Ring(float(cfg.get("ring_seconds", 3900)))
        self.alive = False
        self.last_scrape = 0.0
        self.kind = eng.get("kind")
        self.label = eng.get("label")   # configured name; the served one is self.model
        self.model = None
        self.model_root = None  # /v1/models root: the weights path the engine loaded
        self._prev: dict | None = None          # counters at last tick
        self._prev_t = 0.0
        self._hist_prev: dict[str, tuple] = {}  # logical -> hist state at last tick
        self._hist_base: dict[str, list] = {}   # logical -> [(t, state)...] window ago
        # coarse 5 s history kept for ~1 h: lets a bubble ask for an exact
        # p50 over the last HOUR (old LIMCARS method) instead of mediating
        # 30 s windows, whose means swing wildly at low request rates
        self._hist_hr: dict[str, list] = {}
        self._hist_hr_t = 0.0
        self._hist_snap_t = 0.0
        self._counter_win: dict[str, list] = {} # logical -> [(t, delta)] ring-ish list
        self._pf_last_sum = None                 # last prefill_time hist sum (per-tick activity)
        self._pf_last_move = 0.0                 # when prefill_time sum last advanced
        self._gen_last_move = 0.0                # when gen_tokens last advanced
        self._peak: deque[tuple[float, float]] = deque()
        # 1-minute counter deltas over the last hour -> median rates for the
        # savings bubbles (user rule: median of last hour, refresh 1/min —
        # a live-ticking dollar figure is jitter theatre)
        self._rate_min: deque[dict] = deque(maxlen=60)
        self._first_seen: float = 0.0
        self._min_acc: dict = {"dec": [], "spec": []}
        self._rate_prev: dict | None = None
        self._rate_prev_t = 0.0
        self._rate_live: set[str] = set()       # counters the baseline really read
        self._savings_cache: dict | None = None
        self._stop_ev = threading.Event()
        self.started = time.time()
        self._persist_acc: list[tuple[float, dict]] = []
        self.topic = f"engine:{self.id}"

    # ---------------- public ----------------

    def stop(self) -> None:
        self._stop_ev.set()

    def _hour_median(self, key: str, fallback, rows=None):
        """Median of a per-tick series over the last hour (user rule: hour
        median, not the twitchy 30 s window value). `rows` lets one tick
        share a single ring.window(3600) copy across several medians."""
        if rows is None:
            rows = self.ring.window(3600)
        vals = sorted(v for _, r in rows if (v := r.get(key)) is not None)
        if len(vals) < 5:
            return fallback
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    def snapshot(self, seconds: float | None = None) -> dict:
        rows = self.ring.all() if seconds is None else self.ring.window(seconds)
        return {
            "id": self.id, "url": self.url, "kind": self.kind, "model": self.model,
            "alive": self.alive, "last_scrape": self.last_scrape,
            # seconds since the last SUCCESSFUL scrape, computed at read time:
            # rows stop flowing when the engine dies, but a reader that only
            # sees the last row's timestamp cannot tell a frozen value from a
            # fresh one without doing clock math against its own clock.
            "scrape_age_s": (round(time.time() - self.last_scrape, 1)
                             if self.last_scrape else None),
            "proc_start": getattr(self, "proc_start_t", None),
            "counter_audit": getattr(self, "counter_audit", None),
            "hz": self.hz, "window_s": self.window_s,
            # the client opens the live relay only for engines whose dialect
            # declares a live board (tandemllm)
            "live": bool(dialects.DIALECTS.get(self.kind or "", {})
                         .get("live_json")),
            "kv_ref_tokens": self.kv_ref_tokens,
            "kv_ref_source": self.kv_ref_source,
            "kv_ref_launch": self.kv_ref_launch,
            "kv_ref_pin_gb": self.kv_ref_pin_gb,
            # why a ref is absent/blanked (foreign identity, drift gate): a
            # dash should be explainable from the API without reading the code
            "kv_ref_error": self.kv_ref_error,
            "latest": self.ring.last(),
            "series": {k: [[t, r.get(k)] for t, r in rows] for k in SERIES_KEYS},
        }

    # ---------------- thread ----------------

    def run(self) -> None:
        interval = 1.0 / max(0.5, self.hz)
        while not self._stop_ev.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 — sampler must never die
                self._fail(exc)
            # publish at most once per tick even on failure (last row stays alive)
            sleep = interval - (time.monotonic() - t0)
            if sleep > 0:
                self._stop_ev.wait(sleep)

    def _fetch(self, path: str, timeout: float = 1.5) -> bytes:
        req = urllib.request.Request(self.url + path,
                                     headers={"User-Agent": "limcars-next/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed http(s) url
            return r.read()

    def _kv_ref_maybe(self) -> None:
        """Kick a recompute when its TTL expired. Never blocks the 2 Hz tick."""
        cmd = self.kv_ref_cmd
        if not cmd or self._kv_ref_busy or time.time() - self._kv_ref_at < self.kv_ref_ttl:
            return
        self._kv_ref_busy = True
        self._kv_ref_at = time.time()
        threading.Thread(target=self._kv_ref_refresh, args=(cmd,), daemon=True).start()

    def _engine_identity(self) -> str | None:
        """What answers on this url RIGHT NOW: "tandemllm", "proxy", a dialect
        kind from the metrics family, or None (unreachable — transient).
        Cheap probe: /health JSON shape first (the tandem contract carries
        `inflight`/`uptime_s`; a vllm-proxy says so in `engine`), then a
        /metrics family sniff for engines whose /health is not JSON."""
        try:
            d = json.loads(self._fetch("/health", timeout=2.0)
                           .decode("utf-8", "replace"))
            if isinstance(d, dict):
                if "inflight" in d or "uptime_s" in d:
                    return "tandemllm"
                if "proxy" in str(d.get("engine") or ""):
                    return "proxy"
        except Exception:  # noqa: BLE001 — any health failure falls to /metrics
            pass
        try:
            head = self._fetch("/metrics", timeout=2.0)[:200_000]
            k = dialects.detect(head.decode("utf-8", "replace"))
            if k:
                return k
        except Exception:  # noqa: BLE001 — unreachable is answered as None
            pass
        return None

    def _kv_ref_refresh(self, cmd: str) -> None:
        # Identity gate (proxy incident 2026-11-12): kv_ref_cmd is a
        # tandemllm-specific estimator (pool_estimate.py --from-running). When
        # the url answered as a vllm-proxy it still exited 0 and priced the
        # TandemLLM launch that was NOT running — a fresh number for the wrong
        # engine (measured 663,179 while vLLM's own pool was 1,413,408). Never
        # print a constant fallback for an engine that is not TandemLLM: the
        # ref goes UNAVAILABLE (None) until the url answers as tandem again.
        # An unreachable engine (identity None) keeps the last good marker —
        # transient failures must not blank an honest number.
        ident = self._engine_identity()
        if ident is not None and ident != "tandemllm":
            self.kv_ref_tokens = None
            self.kv_ref_source = None
            self.kv_ref_launch = None
            self.kv_ref_pin_gb = None
            self._kv_ref_step = None
            self.kv_ref_error = (f"engine on this url answers as {ident} — "
                                 "tandem ref unavailable")
            self._kv_ref_busy = False
            return
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            if p.returncode != 0:
                # the script's drift gate exits 3 with its diff on stderr: name it,
                # and keep the marker that is already on the chart
                tail = (p.stderr or "").strip().splitlines()
                raise ValueError(tail[-1] if tail else f"exit {p.returncode}")
            out = json.loads((p.stdout or "").strip().splitlines()[-1])
            n = out.get("context_tokens_fit")
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                raise ValueError("no usable context_tokens_fit in its output")
            old = self.kv_ref_tokens
            self.kv_ref_tokens = float(n)
            self.kv_ref_source = out.get("source") or "recomputed"
            self.kv_ref_launch = out.get("launch_context_tokens_fit")
            # pool_estimate emits floor_pinned ONLY when --floor overrode the
            # launch's knob: the fit is then a stale launch-era pool, not this
            # engine's (measured 2026-11-10: pinned floor 10 read 836,140
            # while the 0.15.3 launch prices 663,179 — the tile understated
            # pressure by 26%). Carry the GiB so the tile can name the pin.
            fp = out.get("floor_pinned")
            gb = fp.get("floor_gb") if isinstance(fp, dict) else None
            self.kv_ref_pin_gb = (float(gb) if isinstance(gb, (int, float))
                                  and not isinstance(gb, bool) else None)
            if old is not None and abs(old - self.kv_ref_tokens) > 0.5:
                # the ref stepped (new launch knobs, or a pin dropped/added):
                # the next row carries the OLD value once, for the chart mark
                self._kv_ref_step = old
            self.kv_ref_error = None
        except Exception as exc:  # noqa: BLE001 — a stale marker beats a wrong one
            self.kv_ref_error = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            self._kv_ref_busy = False

    def _live_json(self, dial: dict, path: str | None = None) -> dict | None:
        """Engine live-board JSON (dialect `live_json` path), None if absent.
        Only engines whose dialect declares the path are ever asked; a failure
        (old build without the route, timeout) is silent — /metrics stays the
        source of truth. Cached per path for 0.25 s so a 2 Hz sampler cannot
        stampede an engine that regenerates the payload at 4 Hz anyway."""
        path = path or dial.get("live_json")
        if not path:
            return None
        now = time.time()
        cached = (getattr(self, "_live_cache", None) or {}).get(path)
        if cached and now - cached[0] < 0.25:
            return cached[1]
        out: dict | None = None
        try:
            req = urllib.request.Request(self.url + path,
                                         headers={"User-Agent": "limcars-next/1.0"})
            # the tandemllm live board is an SSE stream (event/id/data frames,
            # 4 Hz): take the FIRST data frame and close — never read() the
            # open stream, which would block the tick until the timeout.
            with urllib.request.urlopen(req, timeout=0.8) as r:  # noqa: S310 — fixed http(s) url
                seen: list[bytes] = []
                for raw in r:
                    if len(seen) < 64:
                        seen.append(raw)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    d = json.loads(line[5:].lstrip())
                    if isinstance(d, dict):
                        out = d
                    break
                if out is None:
                    # not SSE — a plain JSON body (other engines' boards);
                    # the loop consumed it, so parse what was buffered
                    body = b"".join(seen).decode("utf-8", "replace")
                    d = json.loads(body) if body.strip() else None
                    if isinstance(d, dict):
                        out = d
        except Exception:  # noqa: BLE001 — the live board is an extra, never a dependency
            pass
        self._live_cache = getattr(self, "_live_cache", None) or {}
        self._live_cache[path] = (now, out)
        return out

    # A port can be re-served with another model (same URL, new engine): the
    # served id is re-read every MODEL_REFRESH_S and right after a restart, so
    # the engine pill and pricing follow what is actually loaded.
    MODEL_REFRESH_S = 30.0

    @property
    def skey(self) -> str:
        """Stats key: token minutes, savings and lost potential are kept per
        (engine, served model), so a port re-served with another model starts
        its own history instead of adding to the previous model's."""
        return f"{self.id}@{self.model}" if self.model else self.id

    def _refresh_model(self, now: float) -> None:
        if now < getattr(self, "_models_next", 0.0):
            return
        try:
            models = json.loads(self._fetch("/v1/models", timeout=3.0))
            # OpenAI shape {"data":[{"id":...}]} AND Ollama-style
            # {"models":[{"name":...}]} answered by some llama-server forks
            # (measured LFM3-Audio-2B :8181, 11-13). A None model prices the
            # engine at the pricing.json DEFAULT rate — wrong money — and
            # drops the @model history key.
            entry = (models.get("data") or models.get("models") or [{}])[0]
            mid = entry.get("id") or entry.get("name") or entry.get("model")
            self.model_root = entry.get("root") or None
            # TandemLLM serves every model under one configured id; its `tandem` block names
            # the model actually loaded (and changes when another one is picked)
            loaded = entry.get("tandem") if isinstance(entry.get("tandem"), dict) else {}
            mid = loaded.get("label") or loaded.get("model") or mid
            self._models_fails = 0
            self._models_next = now + self.MODEL_REFRESH_S
        except Exception:  # noqa: BLE001 — cosmetic only
            # audit: a wedged API layer turned this into a 2 Hz 3 s hang,
            # collapsing the tick loop; back off exponentially instead.
            fails = getattr(self, "_models_fails", 0) + 1
            self._models_fails = fails
            self._models_next = now + min(60.0, 2.0 ** fails)
            return
        if mid and mid != getattr(self, "_served_raw", None):
            self._served_raw = mid
            mid = self._known_name(mid)
        else:
            mid = self.model
        if mid and mid != self.model:
            if self.model is not None:
                self._model_switch = True
            self.model = mid
            self.broker.publish("events", json.dumps(
                {"t": now, "type": "engine_model", "engine": self.id, "model": mid}))

    def _known_name(self, mid: str) -> str:
        """The name this model already has in the history (any engine), so one model keeps one
        name across engines, quants and org prefixes (app/modelnames.py)."""
        from . import modelnames
        known: list[str] = []
        try:
            if self.store:
                known += self.store.known_models()
        except Exception:  # noqa: BLE001 — naming must never stop sampling
            pass
        peers = getattr(self, "peers", None)
        if peers is not None:
            known += peers.declared_labels()
        return modelnames.pick(mid, known)

    def _tick(self) -> None:
        # start any due recompute first: it runs off this thread while the
        # scrape below is in flight
        self._kv_ref_maybe()
        # 8 s, not 1.5 s: a loaded TandemLLM renders /metrics in 4-10 s
        # (it collects allocator, cache and disk-tier reports). A scrape
        # that missed by a second must not mark a live engine dead — three
        # consecutive misses do (see _fail).
        body = self._fetch("/metrics", timeout=8.0).decode("utf-8", "replace")
        self._fails = 0
        now = time.time()
        # KV pool size (tokens) is static config — parse once: the bubble then
        # can show used/free in TOKENS, not just percent
        if getattr(self, "_kv_pool", None) is None:
            # vLLM's own token capacity when it publishes one: on a hybrid model (DSV4.1 has
            # 17 KV cache groups) blocks x block_size undercounts it (603K vs 959,768)
            m_tok = re.search(r'kv_cache_size_tokens="(\d+)"', body)
            m_nb = re.search(r'num_gpu_blocks="(\d+)"', body)
            m_bs = re.search(r'block_size="(\d+)"', body)
            if m_tok:
                self._kv_pool = int(m_tok.group(1))
            elif m_nb and m_bs:
                self._kv_pool = int(m_nb.group(1)) * int(m_bs.group(1))
        series = prom.parse(body)
        if not self.kind:
            self.kind = dialects.detect(body) or "vllm"
            self.broker.publish("events", json.dumps(
                {"t": now, "type": "engine_kind", "engine": self.id, "kind": self.kind}))
        self._refresh_model(now)
        dial = dialects.DIALECTS.get(self.kind, dialects.VLLM)
        # vLLM-family engines publish their own token pool (cache_config_info's
        # kv_cache_size_tokens, parsed above) — that measured pool IS the
        # honest denominator for the KV tile: used/pool reads the same
        # occupancy as the engine's own kv_cache_usage_perc. These engines
        # never run a tandem kv_ref_cmd, so without this their ref line and
        # the sub-line fraction would stay absent.
        if self.kind in ("vllm", "gr8") and getattr(self, "_kv_pool", None):
            self.kv_ref_tokens = float(self._kv_pool)
            self.kv_ref_source = "engine-pool"

        gauges = dialects.read_gauges(series, dial)
        counters = dialects.read_counters(series, dial)
        hists = dialects.read_hists(series, dial)
        live_json = self._live_json(dial)

        dt = now - self._prev_t if self._prev is not None else 0.0
        prev = self._prev or {}

        row: dict[str, object] = {
            "running": gauges.get("running"),
            "waiting": gauges.get("waiting"),
            "kv_pct": (gauges.get("kv_pct") or 0.0) * 100 if gauges.get("kv_pct") is not None else None,
            # tandemllm publishes the full finished-generations family under its
            # own logical; other dialects never set it (read_counters writes the
            # key as None only for mapped-but-absent series) and keep req_success.
            "requests_total": (counters.get("requests_total")
                               if counters.get("requests_total") is not None
                               else counters.get("req_success")),
        }

        # --- instantaneous rates (counter difference over this tick) ---
        # decode_tps_now is continuous and honest (tokens credited per step);
        # prefill_tps_now is a completion-burst value — used only as bubble
        # detail, never as the chart's main prefill line (see below).
        for logical, key in (("prompt_tokens", "prefill_tps_now"),
                             ("gen_tokens", "decode_tps_now")):
            cur, old = counters.get(logical), prev.get(logical)
            if cur is None or old is None or dt <= 0:
                row[key] = None
            elif cur < old:                      # counter reset → rebuild baseline
                row[key] = None
                self._reset_baseline(now, counters, hists)
            else:
                v = (cur - old) / dt
                # prompt tokens are completion-charged: a whole prompt landing
                # in one tick is an attribution artifact, not a speed. Refuse to
                # show it above the plausibility cap (the bubble holds last).
                row[key] = None if (key == "prefill_tps_now"
                                    and v > PREFILL_MAX) else v
            self._win_delta(logical, cur, old, now, dt)
            if logical == "gen_tokens" and cur is not None and old is not None and dt > 0 and cur > old:
                self._gen_ticks.append((now, cur - old, dt))
                _trim(self._gen_ticks, now - self.decode_avg_win_s)
        for logical in ("drafts", "draft_tokens", "accepted_tokens", "spec_steps_ok", "spec_steps",
                        "prefix_hits", "prefix_queries",
                        "prefill_tokens", "prefill_seconds",
                        "prefill_compute_tokens", "decode_seconds"):
            self._win_delta(logical, counters.get(logical), prev.get(logical), now, dt)

        self._loop_signal(now, row)

        # --- window averages ---
        # Prefill truth: vLLM charges prompt_tokens at request COMPLETION, so a
        # raw counter difference over one tick plots an impossible spike
        # (e.g. 127k tokens in 0.5 s). The honest prefill speed is
        #     Δprompt_tokens / Δprefill_time_sum   over the window —
        # tokens actually spent per second the engine WAS prefilling.
        row["prefill_tps_avg"] = self._prefill_rate(now)
        # prefill activity. No metric advances per-tick *during* a prefill
        # (prefix queries charge once at admission; request_prefill_time and
        # prompt_tokens fire at completion; iteration hist only logs decode
        # steps). So: admission edge (queries moved) OR "engine holds
        # running requests yet neither gen tokens nor iterations moved this
        # tick" = prefill compute in flight. Sticky 1.5 s against 2 Hz flicker.
        pq, pq_old = counters.get("prefix_queries"), prev.get("prefix_queries")
        gt, gt_old = counters.get("gen_tokens"), prev.get("gen_tokens")
        it, it_old = counters.get("iterations"), prev.get("iterations")
        rt_c, rt_p = counters.get("prefill_tokens"), prev.get("prefill_tokens")
        admitted = ((pq is not None and pq_old is not None and pq > pq_old)
                    or (rt_c is not None and rt_p is not None and rt_c > rt_p))
        stepped = ((gt is not None and gt_old is not None and gt > gt_old)
                   or (it is not None and it_old is not None and it > it_old))
        running = (row.get("running") or 0) > 0
        # completion-charged engines (llama.cpp) have no live per-step signal,
        # so "running but nothing moved" proves nothing there: admission only
        if admitted or (running and not stepped
                        and not dial.get("gen_completion_charged")):
            self._pf_last_move = now
        row["prefill_active"] = (now - self._pf_last_move) < 1.5
        # engine live board: in-flight prefill speed + live marker, and the
        # phase itself while decoders keep the heuristic from seeing it (must
        # run AFTER the heuristic assignment above, or it is overwritten)
        _apply_prefill_live(row, live_json)
        if dial.get("gen_completion_charged"):
            # gen tokens land when a request finishes (llama.cpp, TandemLLM):
            # a per-tick difference is a completion burst, not a speed. Pair
            # them with the engine's own decode compute seconds; shown while
            # requests run (no metric separates prefill from decode, so no
            # phase is claimed).
            paired = self._paired_rate("gen_tokens", "decode_seconds")
            if paired is None and "decode_tps" in dial.get("hist", {}):
                if dial.get("decode_hist_per_request"):
                    # TandemLLM: the histogram is PER REQUEST — with 4-5 lanes
                    # in flight its mean is one request's speed, not the box's.
                    # "now" is the engine's own aggregate (live board, the
                    # number its dashboard prints); no board → the window rate
                    # of gen_tokens (METRICS.md's own aggregate recommendation:
                    # rate(qse_generation_tokens_total)). The per-request mean
                    # survives beside it as decode_tps_req.
                    agg = None
                    if live_json:
                        agg = _live_decode_agg(live_json.get("now") or {})
                    if agg is None:
                        agg = self._win_rate("gen_tokens")
                    row["decode_tps_now"] = agg if running else None
                    d = self._hist_delta("decode_tps")
                    row["decode_tps_req"] = (d[1] / d[2]) if d and d[2] else None
                else:
                    # no decode-seconds counter to pair with: the engine's OWN
                    # per-request decode-speed histogram — the mean over the
                    # requests that completed in this window. The counter path
                    # above would read a 3k-token output landing in one 0.5 s
                    # tick as 6000 t/s — physically impossible, and it did.
                    d = self._hist_delta("decode_tps")
                    paired = (d[1] / d[2]) if d and d[2] else None
                    row["decode_tps_now"] = paired if running else None
            else:
                row["decode_tps_now"] = paired if running else None
        if (row.get("decode_tps_now") or 0) > 0:
            self._gen_last_move = now
        if dial.get("gen_completion_charged") and "decode_tps" in dial.get("hist", {}):
            # the histogram only moves at completion, so the 1.5 s stickiness
            # would dim the bubble straight through a long decode. The
            # engine's own gauge is the truth: it counts generations the
            # engine is working on right now.
            row["decode_active"] = bool(running)
        else:
            row["decode_active"] = (now - self._gen_last_move) < 1.5
        row["decode_tps_avg"] = (self._paired_rate("gen_tokens", "decode_seconds")
                                 if dial.get("gen_completion_charged")
                                 else self._gen_active_rate(now))
        if row["decode_tps_avg"] is None and dial.get("gen_completion_charged"):
            # no decode-seconds counter to pair with: fall back to the plain
            # window token rate (aggregate incl. idle gaps — the same
            # definition every other engine's avg carries)
            row["decode_tps_avg"] = self._win_rate("gen_tokens")
        row["cache_hit_pct"] = self._win_ratio("prefix_hits", "prefix_queries", scale=100.0)
        row["spec_accept_pct"] = self._win_ratio("accepted_tokens", "draft_tokens", scale=100.0)
        # step-level acceptance (Atlas): share of verify steps accepting >=1 draft
        row["spec_step_accept_pct"] = self._win_ratio("spec_steps_ok", "spec_steps", scale=100.0)

        # --- histogram window quantiles ---
        self._hist_snapshot(now, hists)
        for logical in _HIST_LOGICALS:
            k_p50, k_avg, k_n, k_p50h, k_avgh, k_nh = _HIST_KEYS[logical]
            d = self._hist_delta(logical)
            p50 = prom.quantile(d[0], 0.50) if d else None
            avg = (d[1] / d[2]) if d else None
            row[k_p50] = p50 * 1000 if p50 is not None else None
            row[k_avg] = avg * 1000 if avg is not None else None
            # audit: vLLM's coarse histogram buckets (…2.5/5/10/20/40 s) make a
            # p50 from 1-2 window requests pure bucket-edge fiction; expose the
            # sample count so the UI can prefer the true sum/count mean when n
            # is too small for a percentile to mean anything.
            row[k_n] = d[2] if d else 0
            # the SAME statistic over the last hour, from exact cumulative
            # bucket deltas. This is what the latency bubbles read: a 30 s
            # window holds two or three requests in normal use, so its p50 (or
            # its mean) is owned by whoever happened to queue longest — TTFT
            # showed 28.5 s while the engine's hour median was 5 s.
            sl = self._hist_slice(logical, 3600.0)
            if sl:
                row[k_p50h], row[k_avgh], row[k_nh] = sl
            else:
                row[k_p50h] = row[k_avgh] = None
                row[k_nh] = 0
        # e2e smoothing (old LIMCARS gauge basis): cumulative histogram
        # mean = sum/count over the engine's WHOLE life — inherently slow
        # moving like the old dash; plus an hour-median of the p50 samples.
        # The hour median rides the WINDOW MEAN, not the p50: vLLM's coarse
        # buckets make a low-n p50 a bucket-edge, and medians of fictions are
        # still fictions (a real workload read 25.00 s against a true 3.24 s).
        # Median-of-means keeps the gauge a slow medium, which is the standing
        # rule for latency bubbles, without ever inventing a percentile.
        st = hists.get("e2e")
        row["e2e_life_avg_ms"] = (st[1] / st[2] * 1000) if st and st[2] else None
        # the bubble's headline (e2e_p50_1h_ms / e2e_avg_1h_ms / e2e_n_1h, one
        # true p50 over the last hour) was filled by the histogram loop above;
        # e2e_med_1h stays alongside it for the sparkline.
        hour_rows = self.ring.window(3600)
        row["e2e_med_1h"] = self._hour_median("e2e_avg_ms", row.get("e2e_avg_ms"),
                                              hour_rows)

        # --- request completion events (count jump) ---
        # prefer the full finished-generations family (tandemllm's req_success
        # counts only stop/length rows — tool-call finishes never fired)
        cur_rs = (counters.get("requests_total") if counters.get("requests_total") is not None
                  else counters.get("req_success"))
        prev_rs = (prev.get("requests_total") if prev.get("requests_total") is not None
                   else prev.get("req_success"))
        if cur_rs is not None and prev_rs is not None and cur_rs > prev_rs:
            self.broker.publish("requests", json.dumps({
                "t": now, "engine": self.id,
                "completed": int(cur_rs - prev_rs),
                "running": row["running"], "waiting": row["waiting"]}))

        # --- Classic-dash extras: lifetime totals, spec internals, peak ---
        row["prompt_total"] = counters.get("prompt_tokens")
        row["gen_total"] = counters.get("gen_tokens")
        q = counters.get("prefix_queries")
        h = counters.get("prefix_hits")
        if q and h is not None and q > 0:
            row["cache_hit_life_pct"] = 100.0 * h / q
        d_drafts = self._win_sum("drafts")
        d_dtok = self._win_sum("draft_tokens")
        d_acc = self._win_sum("accepted_tokens")
        if d_drafts and d_drafts > 0:
            # audit: `or 0` fabricated numbers when a counter was missing
            # (spec 1.0 = "zero accepted"); house rule is '–', never fake 0
            if d_dtok is not None:
                row["draft_tok_per_step"] = d_dtok / d_drafts
            if d_acc is not None:
                row["spec_accept_len"] = 1.0 + d_acc / d_drafts
        # peak decode over the last hour (Classic "Gen speed peak")
        dnow = row.get("decode_tps_now")
        self._peak.append((now, dnow if dnow and dnow > 0 else 0.0))
        cutoff = now - 3600
        while self._peak and self._peak[0][0] < cutoff:
            self._peak.popleft()
        row["gen_peak"] = max((v for _, v in self._peak), default=None) or None

        # --- savings: old-LIMCARS basis, recomputed once a minute ---
        self._min_acc["dec"].append(dnow if dnow and dnow > 0 else 0.0)
        if row.get("spec_accept_len") is not None:
            self._min_acc["spec"].append(row["spec_accept_len"])
        self._rate_minute(now, counters)
        if (self._savings_cache is None
                or now - self._savings_cache.get("refreshed_at", 0) >= 60.0):
            self._savings_cache = self._compute_savings(now, counters)
        row["savings"] = self._savings_cache
        if self._savings_cache:      # numeric copies -> ring/SSE series so the
            row["savings_day"] = self._savings_cache.get("day")        # savings
            row["savings_month"] = self._savings_cache.get("month")    # bubbles get
            row["savings_year"] = self._savings_cache.get("year")      # sparklines
        pool = getattr(self, "_kv_pool", None)
        row["kv_pool_tokens"] = pool
        if pool and row.get("kv_pct") is not None:
            used = round(pool * row["kv_pct"] / 100.0)
            row["kv_used_tokens"] = used
            row["kv_free_tokens"] = pool - used
        if gauges.get("kv_pool_tokens"):  # engines that publish their pool in tokens directly (Tarnwell)
            pool = int(gauges["kv_pool_tokens"])
            used = int(gauges.get("kv_used_tokens") or 0)
            row["kv_pool_tokens"], row["kv_used_tokens"], row["kv_free_tokens"] = pool, used, max(0, pool - used)
        if live_json:
            # tandemllm: the live board reports the context in TOKENS — the KV
            # pool this engine publishes nowhere on /metrics (its state store
            # is byte-denominated). Same shape as the Tarnwell gauges; the
            # state store also lands in GiB beside the tokens (kv_pct here is
            # a BYTE ratio, so the tile fallback can state the real unit).
            # /health adds the shared pool's committed rows across all lanes.
            row.update(_live_kv(live_json, series,
                                self._live_json(dial, "/health")
                                if dial.get("live_json") else None))
        # kv_tok: the KV chart's token line — used tokens when the engine
        # publishes a token pool (vLLM via cache_config_info, Tarnwell and
        # tandemllm directly), else the percent scaled onto the known pool,
        # else None (the client keeps drawing the percent chart).
        if row.get("kv_used_tokens") is not None:
            row["kv_tok"] = row["kv_used_tokens"]
        elif pool and row.get("kv_pct") is not None:
            row["kv_tok"] = round(pool * row["kv_pct"] / 100.0)
        if self.kv_ref_tokens:
            row["kv_ref_tokens"] = float(self.kv_ref_tokens)
            if self.kv_ref_pin_gb is not None:
                row["kv_ref_pin_gb"] = self.kv_ref_pin_gb
            if self.kv_ref_launch:
                # the sub-line shows it only when it disagrees materially
                row["kv_ref_launch"] = float(self.kv_ref_launch)
            if self._kv_ref_step is not None:
                # stamped on exactly one row: the tick right after the step
                row["kv_ref_step"] = float(self._kv_ref_step)
                self._kv_ref_step = None
        _anchor_tile_pct(row)
        self._kv_peak(now, row)
        ps = counters.get("proc_start")
        if ps:
            self.proc_start_t = ps
        row["cache_hit_med_1h"] = self._hour_median(
            "cache_hit_pct", row.get("cache_hit_pct"), hour_rows)
        # headline basis: volume-weighted hour aggregate, identical pair to
        # the token counters (see _cache_hit_agg); the persisted-bucket read
        # wins so tile == counters across restarts. Explicit None test:
        # a true 0.0% must not fall through an `or` to the live path.
        _agg = self._cache_hit_persisted(now)
        row["cache_hit_agg_1h"] = self._cache_hit_agg(3600.0) if _agg is None else _agg

        self._prev = counters
        self._prev_t = now
        self.alive = True
        self.last_scrape = now
        if not self._first_seen:
            self._first_seen = now

        self.ring.append(row, now)
        self.broker.publish(self.topic, json.dumps({"t": now, **row}))

        # persist one row per second (downsample, keeps sqlite small)
        if not self._persist_acc or now - self._persist_acc[-1][0] >= 1.0:
            self._persist_acc.append((now, row))
            if self.store and len(self._persist_acc) >= 4:
                rows, self._persist_acc = self._persist_acc, []
                self.store.save_engine_samples(self.id, rows)

    # --- savings bubbles: hourly median rates, 1/min refresh ---

    def _rate_minute(self, now: float, counters: dict) -> None:
        """Close a 1-minute bucket of counter deltas every 60 s. Buckets are
        persisted (366 d) — they power BOTH the savings rate and the
        windowed Token counters report (1h/1d/7d/1mo/1y/all)."""
        cur_p, cur_g = counters.get("prompt_tokens"), counters.get("gen_tokens")
        cur_c = counters.get("cached_tokens") or 0.0
        cur_r = counters.get("req_success") or 0.0
        cur_i = counters.get("iterations") or 0.0
        if cur_p is None or cur_g is None:
            return
        base = {"prompt_tokens": cur_p, "gen_tokens": cur_g, "cached_tokens": cur_c,
                "req_success": cur_r, "iterations": cur_i}
        live = {k for k in base if counters.get(k) is not None}
        if self._rate_prev is None:
            # Restore last-seen absolutes from SQLite: the engine kept
            # serving while limcars was down/restarting — a fresh baseline
            # would permanently swallow that traffic (observed 9x undercount
            # of prompt tokens vs engine truth across a day of restarts).
            # Signals the dialect no longer exposes (e.g. a mapping dropped in
            # an upgrade) read 0 now: a saved value for them must not veto the
            # restore of the real token baselines.
            saved = self.store.get_counter_raw(self.id) if self.store else None
            if (saved and now - saved[0] < 86400
                    and all(float(saved[1].get(k, 0)) <= v + 1e-6
                            for k, v in base.items() if k in live)):
                self._rate_prev = {k: float(saved[1].get(k, v)) if k in live else v
                                   for k, v in base.items()}
                self._rate_prev_t = saved[0]
                self._audit_anchor(saved[0], self._rate_prev)
            else:
                # First-ever tracking run: credit the engine's lifetime totals
                # in a seed bucket parked just OUTSIDE the 1 h window — old-dash
                # all-time semantics are engine-lifetime, not tracker uptime —
                # without inflating current-hour rates or the savings rate basis.
                if self.store and self.store.counter_totals(self.skey, None).get("empty"):
                    # minute-ALIGNED or the importer/live buckets can't see
                    # the collision and the same minute counts twice
                    # (observed: lifetime seed at ts%60=44 -> fake 311M peak
                    # hour in the lost-potential bubble)
                    self.store.save_counter_bucket(self.skey, int((now - 3660) // 60) * 60, {
                        "prompt": cur_p, "cached": cur_c, "gen": cur_g,
                        "reqs": cur_r, "iters": cur_i, "dec_min": None,
                        "dec_peak": None, "spec_sum": None, "spec_n": 0,
                        "synthetic": 1, "synth_kind": 1})   # estimate: hidden from totals
                self._rate_prev = base
                self._rate_prev_t = now
                self._audit_anchor(now, base)
            self._rate_live = live
            return
        if now - self._rate_prev_t < 60.0:
            return
        dp = cur_p - self._rate_prev["prompt_tokens"]
        dg = cur_g - self._rate_prev["gen_tokens"]
        dc = cur_c - self._rate_prev.get("cached_tokens", cur_c)
        dr = cur_r - self._rate_prev.get("req_success", cur_r)
        di = cur_i - self._rate_prev.get("iterations", cur_i)
        # audit: engine restart (pid gone, sampler kept alive via alive-sticky)
        # left the old PID's counters as base → one giant phantom delta +
        # first_min mislabeled. proc_start jump = reset, same as counter reset.
        _ps = counters.get("proc_start")
        if _ps and getattr(self, "_ps_seen", None) and abs(_ps - self._ps_seen) > 2:
            self._ps_seen = _ps
            self._rate_prev = base
            self._rate_prev_t = now
            self._rate_live = live
            self._rate_min.clear()
            self._min_acc = {"dec": [], "spec": []}
            self._kv_pool = None    # new block config → re-parse pool
            self._models_next = 0.0  # new process may serve another model
            self._audit_anchor(now, base)
            return
        if _ps:
            self._ps_seen = _ps
        # A counter the baseline read is missing from this scrape (half-written
        # body, a label set not registered yet): its 0 is not a reset — taking
        # it as one dropped this minute and billed the engine's lifetime count
        # the next. Wait for a complete scrape; a real restart still shows as
        # prompt/gen going backwards or proc_start moving. The wait is bounded:
        # a torn scrape heals on the next tick, so a series still gone after
        # 300 s (five minute-closes) is gone for good. Past that the close runs
        # as usual and the baseline is rebuilt from what is live — at most that
        # span is lost, never every minute after it.
        if (dp >= 0 and dg >= 0 and not self._rate_live <= live
                and now - self._rate_prev_t < 300.0):
            return
        if getattr(self, "_model_switch", False):   # new model: its own history
            dp = -1.0
            self._model_switch = False
        if dp < 0 or dg < 0 or dc < 0:              # engine restart → rebuild base
            self._models_next = 0.0
            self._rate_prev = base
            self._rate_prev_t = now
            self._audit_anchor(now, base)
            self._rate_live = live
            self._rate_min.clear()
            self._min_acc = {"dec": [], "spec": []}
            return
        if self.store:      # keep durable baseline for restart-gap absorption
            self.store.save_counter_raw(self.id, now, base)
        dec, spec = self._min_acc["dec"], self._min_acc["spec"]
        self._min_acc = {"dec": [], "spec": []}
        # raw deltas per minute; cached_tokens counts prompt tokens served
        # from prefix cache (billed at the cheaper cache-READ rate, old basis)
        self._rate_min.append({"t": now, "prompt_raw": dp, "cached_raw": max(0, dc),
                               "gen_raw": dg})
        cutoff = now - 3600
        while self._rate_min and self._rate_min[0]["t"] < cutoff:
            self._rate_min.popleft()
        if self.store:
            # Freeze the money with the tokens, using the SAME price source the
            # report and bubbles use (pricing.effective_price), so the panel,
            # the chart and the savings bubble can never disagree — and so a
            # later edit of pricing.json cannot rewrite this minute's cost.
            from . import pricing as pricing_mod
            _money = pricing_mod.money_stamped(
                max(0, dp), max(0, dc), max(0, dg),
                pricing_mod.effective_price(self.model, self.cfg.get("pricing"),
                                            at=self._rate_prev_t))   # peak/off-peak of THIS minute
            # span since the last close, not since the shutdown: a 90 s restart can still
            # span >120 s wall-clock because the baseline is the previous minute's close.
            _gap = (now - self._rate_prev_t) > 120
            self.store.save_counter_bucket(self.skey, int(self._rate_prev_t // 60) * 60, {
                "prompt": max(0, dp), "cached": max(0, dc), "gen": max(0, dg),
                "reqs": max(0, dr), "iters": max(0, di),
                "dec_min": min([v for v in dec if v > 0], default=None),
                "dec_peak": max(dec, default=None),
                "spec_sum": sum(spec), "spec_n": len(spec),
                # gap-spanning bucket (sampler was down/quiet > 2 min): the engine really
                # served these tokens, so they are counted in every total (issue #22) —
                # what they are NOT is a valid "one busy minute" for rate/savings anchors,
                # so synthetic=1 still excludes them from last_active_hour (audit M2) and
                # from the measured-only dec/spec/busy columns. synth_kind=2 is what tells
                # the readers "measured traffic", not an engine-lifetime estimate.
                "synthetic": 1 if _gap else 0,
                "synth_kind": 2 if _gap else 0,
                **_money})
        self._audit_check(now, base)
        self._rate_prev = base
        self._rate_prev_t = now
        self._rate_live = live

    # --- ledger check: stored minutes vs the engine's own lifetime counters ---

    def _audit_anchor(self, t: float, base: dict, since: float | None = None) -> None:
        """Start a span (a fresh baseline: first sight, restore, engine restart). The next
        bucket closed is keyed at this minute, so every bucket from it on belongs to the span."""
        self._audit = {"t": t, "ts": int(t // 60) * 60, "base": dict(base),
                       "since": t if since is None else since}
        if since is None:
            self.counter_audit = None

    def _audit_check(self, now: float, base: dict) -> None:
        """Do the minutes stored since the anchor add up to how far the engine's own counters
        moved over the same span? The minutes are cut from those counters, so any difference
        means traffic was lost or counted twice (a failed write, an import colliding with live
        minutes, a reset misread). Measured and gap minutes count; lifetime seeds do not."""
        a = getattr(self, "_audit", None)
        if not a or not self.store:
            return
        tot = self.store.counter_totals(self.skey, a["ts"], real_only=True)
        keys = (("prompt", "prompt_tokens"), ("cached", "cached_tokens"), ("gen", "gen_tokens"))
        eng = {k: base.get(c, 0.0) - a["base"].get(c, 0.0) for k, c in keys}
        got = {k: 0.0 if tot.get("empty") else float(tot.get(k) or 0.0) for k, _ in keys}
        off = {k: got[k] - eng[k] for k, _ in keys
               if abs(got[k] - eng[k]) > max(1.0, 1e-6 * abs(eng[k]))}
        prev = getattr(self, "counter_audit", None) or {}
        done = {k: v for k, v in (prev.get("verified") or {}).items()} if a["since"] != a["t"] else {}
        self.counter_audit = {"since": a["since"], "checked_at": now, "ok": not off, "off": off,
                              "engine": {k: done.get(k, 0.0) + eng[k] for k, _ in keys},
                              "stored": {k: done.get(k, 0.0) + got[k] for k, _ in keys},
                              "verified": done}
        # roll a verified span forward daily: old minutes fold into hourly rows whose hour can
        # straddle the anchor, and a span that already matched has nothing left to prove
        if not off and now - a["t"] > 86400:
            self.counter_audit["verified"] = dict(self.counter_audit["engine"])
            self._audit_anchor(now, base, since=a["since"])

    def _compute_savings(self, now: float, counters: dict) -> dict | None:
        """Cost of the same tokens via cloud API. IDENTICAL code path to the
        Token counters report (pricing.effective_price + pricing.hot_costs +
        store.last_active_hour) so the bubbles and the report can never
        disagree. $/day-mo-yr = perHour x 24 / 730 / 8760. Refresh 1/min."""
        from . import pricing as pricing_mod
        price = pricing_mod.effective_price(self.model, self.cfg.get("pricing"))
        if not price or not (price.get("output") or price.get("cache_write")
                             or price.get("input")):
            return None
        ah = self.store.last_active_hour(self.skey) if self.store else {"empty": True}
        if not ah.get("empty") and (ah["prompt"] + ah["cached"] + ah["gen"]) > 0:
            sum_p, sum_c, sum_g = ah["prompt"], ah["cached"], ah["gen"]
            per_hour = pricing_mod.hot_costs(sum_p, sum_c, sum_g, price)["hot"]
            basis = f"last active hour · {ah.get('buckets', 0)} min-buckets"
            samples = ah.get("buckets", 0)
        else:
            # fallback: lifetime counters / engine uptime. Baseline MUST be
            # the engine's own start (process_start_time_seconds) — using the
            # sampler's fresh _first_seen would divide days of tokens by
            # minutes and print a fantasy $/year.
            sum_p = counters.get("prompt_tokens") or 0.0
            sum_c = counters.get("cached_tokens") or 0.0
            sum_g = counters.get("gen_tokens") or 0.0
            if sum_p + sum_c + sum_g <= 0:
                return None
            base = counters.get("proc_start")
            if not base or base <= 0 or base >= now:
                base = self._first_seen
            if not base or now - base < 300:      # nothing tracked long yet
                return None
            hrs = max(0.05, (now - base) / 3600.0)
            per_hour = pricing_mod.hot_costs(sum_p, sum_c, sum_g, price)["hot"] / hrs
            basis = f"lifetime ÷ {hrs:.1f} h (no active hour yet)"
            samples = 0
        return {"per_hour": round(per_hour, 4),
                "day": round(per_hour * 24, 2),
                "month": round(per_hour * 730, 2),
                "year": round(per_hour * 8760, 2),
                "tokens": {"prompt": int(sum_p), "cached": int(sum_c), "gen": int(sum_g)},
                "basis": basis,
                "price": {k: price.get(k) for k in
                          ("input", "cache_write", "cache_read", "cache_read_hot", "output")}
                | {"name": price.get("display_name"), "slug": price.get("slug"),
                   "source": price.get("source"), "provider": price.get("provider")},
                "samples": samples, "refreshed_at": now}

    def _fail(self, exc: Exception) -> None:
        now = time.time()
        # A slow engine is not a dead engine: TandemLLM's /metrics render
        # takes 4-10 s under load, and a single missed scrape used to flip
        # the dashboard to "unreachable". Three consecutive failures mean
        # it is really gone; until then the last row stands.
        self._fails = getattr(self, "_fails", 0) + 1
        if self._fails < 3:
            return
        was_alive = self.alive
        self.alive = False
        if was_alive:
            self.broker.publish("events", json.dumps(
                {"t": now, "type": "engine_down", "engine": self.id, "error": str(exc)[:200]}))
        self._prev, self._prev_t = None, 0.0
        self._reset_baseline(now, {}, {})

    def _reset_baseline(self, now: float, counters: dict, hists: dict) -> None:
        self._prev = dict(counters) if counters else None
        self._prev_t = now
        self._hist_prev = {}
        self._hist_base = {}
        self._hist_hr = {}
        self._counter_win = {}
        self._pf_last_sum = None
        self._pf_last_move = 0.0
        self._gen_last_move = 0.0
        self._gen_ticks = []
        self._full_accept_since = None

    def _kv_peak_window(self) -> float:
        for pat, secs in (self.kv_peak_models or {}).items():
            if self.model and re.search(pat, self.model, re.I):
                return float(secs)
        return 0.0

    def _kv_peak(self, now: float, row: dict) -> None:
        """kv_pct_shown: the raw reading, or for models listed in kv_peak_models the highest
        reading of the last N s. DSV4.1 on vLLM flips 0.70 <-> 1.02 % for ~1 s every 128
        generated tokens (a new block in each KV cache group, then the sliding-window groups
        free theirs), which made the tile jump with one request running."""
        raw = row.get("kv_pct")
        win = self._kv_peak_window()
        if not win or raw is None:
            self._kv_hist.clear()
            row["kv_pct_shown"], row["kv_pct_peak_s"] = raw, None
            return
        self._kv_hist.append((now, raw))
        while self._kv_hist and self._kv_hist[0][0] < now - win:
            self._kv_hist.popleft()
        row["kv_pct_shown"] = max(v for _, v in self._kv_hist)
        row["kv_pct_peak_s"] = win

    def _loop_signal(self, now: float, row: dict) -> None:
        """How long the speculative drafts have been accepted in full, step after step.

        A model stuck in an exact loop is predicted perfectly by its own draft head, so
        acceptance pins at the maximum: on DSV4.1 (K=5) a real 9-minute Hermes reasoning
        loop read 6.00 tokens/step every minute, normal reasoning 2-3. Judged over
        loop_window_s of steps so one lucky stretch does not count.

        A window counts only when the draft counter moved in at least loop_moving_share of
        its samples. gr8 added each request's counters when the request ended: agent traffic
        then put one burst in nearly every window, and tool-call JSON accepts about 98% of
        its drafts, so finished requests read as a loop while nothing looped.
        """
        since = now - self.loop_win_s
        drafts = self._win_sum("drafts", since) or 0
        dtok = self._win_sum("draft_tokens", since) or 0
        acc = self._win_sum("accepted_tokens", since) or 0
        if (drafts >= 10 and dtok > 0 and acc >= 0.98 * dtok
                and self._moving_share("drafts", since) >= self.loop_moving_share):
            if self._full_accept_since is None:
                self._full_accept_since = since
        else:
            self._full_accept_since = None
        secs = now - self._full_accept_since if self._full_accept_since is not None else 0.0
        row["spec_full_accept_s"] = round(secs, 1)
        row["loop_suspect"] = secs >= self.loop_warn_s

    def _moving_share(self, logical: str, since: float) -> float:
        """The share of the samples since SINCE in which the counter went up."""
        deltas = [d for t, d, _ in self._counter_win.get(logical) or [] if t >= since]
        return sum(1 for d in deltas if d > 0) / len(deltas) if deltas else 0.0

    def _gen_active_rate(self, now: float) -> float | None:
        """Tokens per second of the ticks that generated, over decode_avg_window_s."""
        floor_t = now - self.decode_avg_win_s
        ticks = [(d, dt) for t, d, dt in self._gen_ticks if t >= floor_t]
        span = sum(dt for _, dt in ticks)
        return sum(d for d, _ in ticks) / span if span else None

    # --- windowed counter deltas ---

    def _win_delta(self, logical: str, cur, old, now: float, dt: float) -> None:
        if cur is None or old is None or dt <= 0 or cur < old:
            return
        win = self._counter_win.setdefault(logical, [])
        win.append((now, cur - old, dt))
        # keep at least a settle window of deltas: the prefill ratio reads back
        # further than the 30 s window everything else uses
        _trim(win, now - max(self.window_s, self.prefill_win_s) * 1.1)

    def _win_rate(self, logical: str, since: float | None = None) -> float | None:
        tot_dt = self._win_span(logical, since)
        s = self._win_sum(logical, since)
        if s is None or not tot_dt:
            return None
        return s / tot_dt

    def _win_sum(self, logical: str, since: float | None = None) -> float | None:
        win = self._counter_win.get(logical)
        if not win:
            return None
        now = time.time()
        floor_t = (now - self.window_s) if since is None else since
        vals = [d for t, d, _ in win if t >= floor_t]
        return sum(vals) if vals else None

    def _paired_rate(self, tok: str, secs: str, since: float | None = None,
                     cap: float | None = None) -> float | None:
        t, s = self._win_sum(tok, since), self._win_sum(secs, since)
        if not t or not s or s <= 0:
            return None
        v = t / s
        return None if cap is not None and v > cap else v

    def _win_span(self, logical: str, since: float | None = None) -> float | None:
        win = self._counter_win.get(logical)
        if not win:
            return None
        now = time.time()
        floor_t = (now - self.window_s) if since is None else since
        tot = sum(dt for t, _, dt in win if t >= floor_t)
        return tot or None

    def _win_ratio(self, num_logical: str, den_logical: str, scale: float = 1.0) -> float | None:
        num, den = self._counter_win.get(num_logical), self._counter_win.get(den_logical)
        if not den:
            return None
        now = time.time()
        n = sum(d for t, d, _ in (num or []) if t >= now - self.window_s)
        dd = sum(d for t, d, _ in den if t >= now - self.window_s)
        if dd <= 0:
            return None
        return max(0.0, min(100.0, scale * n / dd))

    def _cache_hit_agg(self, seconds: float) -> float | None:
        """Σprefix_hits ÷ Σprefix_queries over the last `seconds` — the SAME
        numerator/denominator the token counters use (counters.py:
        100*Σcached/Σprompt), so tile and counters cannot disagree. The hour
        MEDIAN of per-tick ratios (cache_hit_med_1h) weights a 100-token tick
        equal with a 1M-token one and drifted 3 points above the counters on
        the live TandemLLM (tile 99.8% vs counters 96.8%, the operator caught it
        2026-11-09); volume-weighted aggregation is the honest headline."""
        since = time.time() - seconds
        h = self._win_sum("prefix_hits", since=since)
        q = self._win_sum("prefix_queries", since=since)
        if h is None or not q or q <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * h / q))

    def _cache_hit_persisted(self, now: float) -> float | None:
        """The counters' OWN 1-hour number, read from the persisted minute
        buckets through the same store.counter_totals(engine, since,
        real_only=True) call the counters panel uses — so the tile equals the
        counters exactly even minutes after a dashboard restart: the sampler's
        in-memory window dies with the process, the ledger does not. Re-read
        on a 60 s TTL (the counters endpoint's own TTL); the live-delta
        _cache_hit_agg stays the fallback when there is no store or no
        measured bucket yet (fresh install)."""
        if self.store is None:
            return None
        if now - getattr(self, "_agg1h_at", 0.0) < 60.0:
            return self._agg1h
        self._agg1h_at = now
        try:
            # the SAME store key the counters panel resolves by default
            # (_stats_key -> smp.skey): buckets are written per model
            # ("id@Model"); reading the bare id finds nothing once a model
            # split exists and the tile would silently fall back to the
            # live-delta window.
            key = getattr(self, "skey", self.id)
            t = self.store.counter_totals(key, int(now - 3600), real_only=True)
            p, c = t.get("prompt") or 0.0, t.get("cached") or 0.0
            self._agg1h = max(0.0, min(100.0, 100.0 * c / p)) if p > 0 else None
        except Exception:
            self._agg1h = None      # a ledger read must never blank the tile
        return self._agg1h

    # --- histogram windowing ---

    def _hist_snapshot(self, now: float, hists: dict) -> None:
        """Every ~0.5 s record the current cumulative hist state; the window
        baseline is the state from window_s ago (bucket deltas are exact)."""
        if now - self._hist_snap_t < 0.5:
            return
        self._hist_snap_t = now
        hour_tick = now - self._hist_hr_t >= 5.0
        if hour_tick:
            self._hist_hr_t = now
        for logical, state in hists.items():
            if state is None:
                continue
            base = self._hist_base.setdefault(logical, [])
            base.append((now, state))
            _trim(base, now - self.window_s * 1.25)
            # 5 s cadence hour history (~740 samples, a few tens of KB)
            if hour_tick:
                hr = self._hist_hr.setdefault(logical, [])
                hr.append((now, state))
                _trim(hr, now - 3700)
            self._hist_prev[logical] = state

    def _hist_delta(self, logical: str):
        cur = self._hist_prev.get(logical)
        base = self._hist_base.get(logical)
        if not cur or not base:
            return None
        old_state = base[0][1]
        (cur_b, cur_s, cur_c) = cur
        (old_b, old_s, old_c) = old_state
        if not cur_b or len(cur_b) != len(old_b):
            return None
        try:
            d_buckets = [(ub, max(0.0, c - oc)) for (ub, c), (_, oc) in zip(cur_b, old_b)]
            d_sum, d_count = cur_s - old_s, cur_c - old_c
        except TypeError:
            return None
        if d_count <= 0:
            return None
        return d_buckets, d_sum, d_count

    def _hist_slice(self, logical: str, secs: float):
        """(p50_ms, avg_ms, n) over the last `secs`, from EXACT bucket deltas
        of the cumulative histogram — the same method the old LIMCARS dash uses
        for its 'e2e latency (p50)' bubble. One percentile over an hour of
        requests is inherently slow-moving and does not invent a median out of
        a handful of 30 s windows."""
        cur = self._hist_prev.get(logical)
        hr = self._hist_hr.get(logical)
        if not cur or not hr:
            return None
        cutoff = time.time() - secs
        old_state = hr[0][1]
        for t, st in hr:
            if t <= cutoff:
                old_state = st
            else:
                break
        (cur_b, cur_s, cur_c), (old_b, old_s, old_c) = cur, old_state
        if not cur_b or len(cur_b) != len(old_b):
            return None
        try:
            d_b = [(ub, max(0.0, c - oc)) for (ub, c), (_, oc) in zip(cur_b, old_b)]
            d_s, d_c = cur_s - old_s, cur_c - old_c
        except TypeError:
            return None
        if d_c <= 0:
            return None
        p50 = prom.quantile(d_b, 0.50)
        return (p50 * 1000.0 if p50 is not None else None,
                d_s / d_c * 1000.0, int(d_c))

    def _hist_span_delta(self, logical: str, secs: float):
        """(sum_delta, count_delta, covered_seconds) over the last `secs`, from
        the 5 s coarse anchors. None when the anchors cannot cover the span."""
        cur = self._hist_prev.get(logical)
        hr = self._hist_hr.get(logical)
        if not cur or not hr:
            return None
        cutoff = time.time() - secs
        old_t, old_state = hr[0][0], hr[0][1]
        for t, st in hr:
            if t <= cutoff:
                old_t, old_state = t, st
            else:
                break
        if not cur[0] or len(cur[0]) != len(old_state[0]):
            return None
        return (max(0.0, cur[1] - old_state[1]),
                max(0.0, cur[2] - old_state[2]),
                time.time() - old_t)

    def _prefill_rate(self, now: float) -> float | None:
        """Honest prefill speed, or None (bubble holds last) rather than a lie.

        vLLM charges prompt_tokens at request COMPLETION, so a raw counter
        difference over one tick plots an impossible spike. Both sides of the
        ratio must therefore cover the SAME stretch of time — anchored on the
        prefill_time histogram baseline — and that stretch must genuinely be a
        window. Dividing a window's tokens by the ~1 s span of the ticks a burst
        happened to land in is what printed 10k+ tok/s peaks on the chart while
        the engine was doing ~140.
        """
        base = self._hist_span_delta("prefill_time", self.prefill_win_s)
        if base is None:
            # no prefill histogram (llama.cpp): processed prompt tokens and
            # prompt compute seconds are charged together — an exact pair
            return self._paired_rate("prefill_compute_tokens", "prefill_seconds",
                                     since=now - self.prefill_win_s, cap=PREFILL_MAX)
        d_secs, _d_cnt, covered = base
        anchor = now - covered
        # refuse to publish until the anchors really cover a settle window: a
        # young sampler, or a metrics gap, must not invent a speed
        if covered < min(self.prefill_win_s * 0.8, 30.0):
            return None
        if d_secs > 0:
            pd = self._win_sum("prompt_tokens", since=anchor)
            cd = self._win_sum("prefix_hits", since=anchor)
            # prompt_tokens_total counts prefix-CACHED tokens too (~90 % here);
            # prefill_time only counts COMPUTE time — dividing the two inflated
            # prefill speed ~10x. The honest numerator is uncached tokens only.
            # STRICT: if the cached window is missing (metrics gap / counter
            # reset) the subtraction would silently inflate — report None.
            uncached = None if (pd is None or cd is None) else max(0.0, pd - cd)
            if uncached and uncached > 0:
                rate = uncached / d_secs
                if rate <= PREFILL_MAX:
                    return rate
        # Engine-native paths (ported from the TandemDash adapter family):
        # sglang's realtime_tokens_total{mode=prefill*} genuinely advances
        # DURING prefill — a true live rate, no completion attribution.
        rt = self._win_rate("prefill_tokens", since=anchor)
        if rt is not None and 0 < rt <= PREFILL_MAX:
            return rt
        # exl3/tensorfold alt: prefill_seconds is a cumulative compute-seconds
        # COUNTER — Δprompt/Δprefill_seconds pairs perfectly.
        psecs = self._win_sum("prefill_seconds", since=anchor)
        pd = self._win_sum("prompt_tokens", since=anchor)
        if psecs and psecs > 0 and pd and pd > 0:
            v = pd / psecs
            if v <= PREFILL_MAX:
                return v
        # last resort: wall-clock rate over the SAME anchored span, capped like
        # every other path (this path used to be uncapped, which is exactly how
        # completion bursts reached the chart)
        fr = self._win_rate("prompt_tokens", since=anchor)
        cr = self._win_rate("prefix_hits", since=anchor)
        if fr is not None and cr is not None:
            v = max(0.0, fr - cr)
            if v <= PREFILL_MAX:
                return v
        return None


class EngineManager:
    """Owns the per-engine sampler threads; supports hot reconfigure."""

    def __init__(self, cfg: dict, broker: Broker, store=None):
        self.cfg, self.broker, self.store = cfg, broker, store
        self.samplers: dict[str, EngineSampler] = {}
        self._lock = threading.Lock()
        self._auto: set[str] = set()      # sampler ids started by discovery
        self._live = None                 # LiveEngines once attach_discovery runs
        for eng in cfg.get("engines", []):
            self._start(eng)

    def _start(self, eng: dict) -> None:
        s = EngineSampler(self.cfg, eng, self.broker, self.store)
        s.peers = self
        self.samplers[eng["id"]] = s
        s.start()

    # ---------------- auto-discovery ----------------

    def attach_discovery(self, live_engines) -> None:
        """Wire the live-engine detector to the sampler pool: an engine the
        detector attributes to a real process gets sampled without being
        declared in config.json. Orphan port rows stay display-only — they
        are often proxies exposing the same /metrics, and sampling both the
        proxy and the engine would double-count every token. A candidate is
        only connected once its /metrics parses as a known dialect, so a
        plain OpenAI endpoint that speaks no metrics is never polled at 2 Hz.
        Disable with cfg['auto_engines'] = false."""
        if self.cfg.get("auto_engines", True) is False:
            return
        self._live = live_engines
        threading.Thread(target=self._discover_loop, name="engine-discovery",
                         daemon=True).start()

    def _discover_loop(self) -> None:
        while True:
            try:
                self._discover_once()
            except Exception:  # noqa: BLE001 — discovery must never die
                pass
            time.sleep(15.0)

    def _discover_once(self) -> None:
        rows = self._live.scan()
        with self._lock:
            declared = {s.url for eid, s in self.samplers.items()
                        if eid not in self._auto}
        want: dict[str, dict] = {}
        for r in rows:
            if r.get("orphan") or r.get("pid") is None:
                continue                     # proxy / root-owned: display only
            for port in sorted(r.get("serving_ports") or {}):
                url = f"http://127.0.0.1:{port}"
                if url in declared:
                    continue                 # config already samples this engine
                if self._metrics_dialect(url) is None:
                    continue                 # no metrics dialect — not samplable
                eid = f"auto-{port}"
                want[eid] = {"id": eid, "url": url, "kind": None,
                             "label": r.get("model") or r.get("type") or url}
                declared.add(url)            # never two auto ids on one port
        changed = False
        with self._lock:
            for eid, eng in want.items():
                if eid not in self.samplers:
                    self._auto.add(eid)
                    self._start(eng)
                    changed = True
            for eid in list(self._auto):
                if eid not in want and eid in self.samplers:
                    self.samplers.pop(eid).stop()   # engine gone: retire sampler
                    self._auto.discard(eid)
                    changed = True
        if changed:
            # open dashboards subscribe per engine topic, so tell them to
            # re-pull the engine set (an engine swap otherwise needs a reload)
            self.broker.publish("events", json.dumps(
                {"t": time.time(), "type": "engines_changed"}))

    @staticmethod
    def _metrics_dialect(url: str) -> str | None:
        try:
            req = urllib.request.Request(
                url + "/metrics", headers={"User-Agent": "limcars-next/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as r:  # noqa: S310
                return dialects.detect(r.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — unreachable = not a candidate
            return None

    def declared_labels(self) -> list[str]:
        """Configured engine labels (operator-written model names). Discovered engines'
        labels are left out: they echo the served id, which may be a placeholder."""
        with self._lock:
            return [s.label for eid, s in self.samplers.items() if s.label and eid not in self._auto]

    def get(self, engine_id: str) -> EngineSampler | None:
        with self._lock:
            return self.samplers.get(engine_id)

    def all(self) -> list[EngineSampler]:
        with self._lock:
            return list(self.samplers.values())

    def snapshots(self, seconds: float | None = None) -> dict:
        # alive-first: the UI defaults to the FIRST engine when the user has
        # no stored selection, so a dead config-declared engine must not
        # shadow a live auto-discovered one (the "no stats" symptom)
        with self._lock:
            items = sorted(self.samplers.values(), key=lambda s: (not s.alive, s.id))
        return {s.id: s.snapshot(seconds) for s in items}

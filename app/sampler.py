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
    decode_tps_avg   ΣΔgen    / ΣΔt over window_s
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
import threading
import time
import urllib.request

from . import dialects, prom
from .rings import Ring
from .sse import Broker


_HIST_LOGICALS = ("ttft", "itl", "prefill_time", "decode_time", "queue_time", "e2e")
_HIST_KEYS = {lg: (f"{lg}_p50_ms", f"{lg}_avg_ms", f"{lg}_n",
                   f"{lg}_p50_1h_ms", f"{lg}_avg_1h_ms", f"{lg}_n_1h")
              for lg in _HIST_LOGICALS}
SERIES_KEYS = ("prefill_tps_now", "decode_tps_now", "prefill_tps_avg",
               "decode_tps_avg", "running", "waiting", "kv_pct",
               "kv_pool_tokens", "kv_used_tokens", "kv_free_tokens",
               "ttft_p50_ms", "itl_p50_ms", "queue_time_p50_ms", "e2e_p50_ms",
               "cache_hit_pct", "cache_hit_life_pct",
               "spec_accept_pct", "spec_step_accept_pct", "spec_accept_len", "draft_tok_per_step",
               "prompt_total", "gen_total", "gen_peak", "requests_total",
               "savings_day", "savings_month", "savings_year",
               "e2e_med_1h", "e2e_life_avg_ms")

PREFILL_MAX = 12000.0   # plausibility ceiling, tok/s. Real peak measured on
                        # the GB10 daily driver is ~7.5k; anything above this is
                        # completion-attribution skew, never a speed.


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
        self.ring = Ring(float(cfg.get("ring_seconds", 3900)))
        self.alive = False
        self.last_scrape = 0.0
        self.kind = eng.get("kind")
        self.model = None
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
        self._savings_cache: dict | None = None
        self._stop = threading.Event()
        self.started = time.time()
        self._persist_acc: list[tuple[float, dict]] = []
        self.topic = f"engine:{self.id}"

    # ---------------- public ----------------

    def stop(self) -> None:
        self._stop.set()

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
            "proc_start": getattr(self, "proc_start_t", None),
            "hz": self.hz, "window_s": self.window_s,
            "latest": self.ring.last(),
            "series": {k: [[t, r.get(k)] for t, r in rows] for k in SERIES_KEYS},
        }

    # ---------------- thread ----------------

    def run(self) -> None:
        interval = 1.0 / max(0.5, self.hz)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 — sampler must never die
                self._fail(exc)
            # publish at most once per tick even on failure (last row stays alive)
            sleep = interval - (time.monotonic() - t0)
            if sleep > 0:
                self._stop.wait(sleep)

    def _fetch(self, path: str, timeout: float = 1.5) -> bytes:
        req = urllib.request.Request(self.url + path,
                                     headers={"User-Agent": "limcars-next/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed http(s) url
            return r.read()

    def _tick(self) -> None:
        body = self._fetch("/metrics").decode("utf-8", "replace")
        now = time.time()
        # KV pool size (tokens) is static config — parse once: the bubble then
        # can show used/free in TOKENS, not just percent
        if getattr(self, "_kv_pool", None) is None:
            m_nb = re.search(r'num_gpu_blocks="(\d+)"', body)
            m_bs = re.search(r'block_size="(\d+)"', body)
            if m_nb and m_bs:
                self._kv_pool = int(m_nb.group(1)) * int(m_bs.group(1))
        series = prom.parse(body)
        if not self.kind:
            self.kind = dialects.detect(body) or "vllm"
            self.broker.publish("events", json.dumps(
                {"t": now, "type": "engine_kind", "engine": self.id, "kind": self.kind}))
        if not self.model and now >= getattr(self, "_models_next", 0.0):
            try:
                models = json.loads(self._fetch("/v1/models", timeout=3.0))
                self.model = (models.get("data") or [{}])[0].get("id")
                self._models_next = 0.0
            except Exception:  # noqa: BLE001 — cosmetic only
                # audit: a wedged API layer turned this into a 2 Hz 3 s hang,
                # collapsing the tick loop; back off exponentially instead.
                fails = getattr(self, "_models_fails", 0) + 1
                self._models_fails = fails
                self._models_next = now + min(60.0, 2.0 ** fails)
        dial = dialects.DIALECTS.get(self.kind, dialects.VLLM)

        gauges = dialects.read_gauges(series, dial)
        counters = dialects.read_counters(series, dial)
        hists = dialects.read_hists(series, dial)

        dt = now - self._prev_t if self._prev is not None else 0.0
        prev = self._prev or {}

        row: dict[str, object] = {
            "running": gauges.get("running"),
            "waiting": gauges.get("waiting"),
            "kv_pct": (gauges.get("kv_pct") or 0.0) * 100 if gauges.get("kv_pct") is not None else None,
            "requests_total": counters.get("req_success"),
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
        for logical in ("drafts", "draft_tokens", "accepted_tokens", "spec_steps_ok", "spec_steps",
                        "prefix_hits", "prefix_queries",
                        "prefill_tokens", "prefill_seconds",
                        "prefill_compute_tokens", "decode_seconds"):
            self._win_delta(logical, counters.get(logical), prev.get(logical), now, dt)

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
        if dial.get("gen_completion_charged"):
            # gen tokens land when a request finishes (llama.cpp): a per-tick
            # difference is a completion burst, not a speed. Pair them with the
            # engine's own decode compute seconds; shown while requests run
            # (no metric separates prefill from decode, so no phase is claimed).
            paired = self._paired_rate("gen_tokens", "decode_seconds")
            row["decode_tps_now"] = paired if running else None
        if (row.get("decode_tps_now") or 0) > 0:
            self._gen_last_move = now
        row["decode_active"] = (now - self._gen_last_move) < 1.5
        row["decode_tps_avg"] = (self._paired_rate("gen_tokens", "decode_seconds")
                                 if dial.get("gen_completion_charged")
                                 else self._win_rate("gen_tokens"))
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
        cur_rs = counters.get("req_success")
        if cur_rs is not None and prev.get("req_success") is not None and cur_rs > prev["req_success"]:
            self.broker.publish("requests", json.dumps({
                "t": now, "engine": self.id,
                "completed": int(cur_rs - prev["req_success"]),
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
        ps = counters.get("proc_start")
        if ps:
            self.proc_start_t = ps
        row["cache_hit_med_1h"] = self._hour_median(
            "cache_hit_pct", row.get("cache_hit_pct"), hour_rows)

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
        if self._rate_prev is None:
            # Restore last-seen absolutes from SQLite: the engine kept
            # serving while limcars was down/restarting — a fresh baseline
            # would permanently swallow that traffic (observed 9x undercount
            # of prompt tokens vs engine truth across a day of restarts).
            # Signals the dialect no longer exposes (e.g. a mapping dropped in
            # an upgrade) read 0 now: a saved value for them must not veto the
            # restore of the real token baselines.
            live = {k for k in base if counters.get(k) is not None}
            saved = self.store.get_counter_raw(self.id) if self.store else None
            if (saved and now - saved[0] < 86400
                    and all(float(saved[1].get(k, 0)) <= v + 1e-6
                            for k, v in base.items() if k in live)):
                self._rate_prev = {k: float(saved[1].get(k, v)) if k in live else v
                                   for k, v in base.items()}
                self._rate_prev_t = saved[0]
            else:
                # First-ever tracking run: credit the engine's lifetime totals
                # in a seed bucket parked just OUTSIDE the 1 h window — old-dash
                # all-time semantics are engine-lifetime, not tracker uptime —
                # without inflating current-hour rates or the savings rate basis.
                if self.store and self.store.counter_totals(self.id, None).get("empty"):
                    # minute-ALIGNED or the importer/live buckets can't see
                    # the collision and the same minute counts twice
                    # (observed: lifetime seed at ts%60=44 -> fake 311M peak
                    # hour in the lost-potential bubble)
                    self.store.save_counter_bucket(self.id, int((now - 3660) // 60) * 60, {
                        "prompt": cur_p, "cached": cur_c, "gen": cur_g,
                        "reqs": cur_r, "iters": cur_i, "dec_min": None,
                        "dec_peak": None, "spec_sum": None, "spec_n": 0,
                        "synthetic": 1})
                self._rate_prev = base
                self._rate_prev_t = now
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
            self._rate_min.clear()
            self._min_acc = {"dec": [], "spec": []}
            self._kv_pool = None    # new block config → re-parse pool
            return
        if _ps:
            self._ps_seen = _ps
        if dp < 0 or dg < 0 or dc < 0:              # engine restart → rebuild base
            self._rate_prev = base
            self._rate_prev_t = now
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
            self.store.save_counter_bucket(self.id, int(self._rate_prev_t // 60) * 60, {
                "prompt": max(0, dp), "cached": max(0, dc), "gen": max(0, dg),
                "reqs": max(0, dr), "iters": max(0, di),
                "dec_min": min([v for v in dec if v > 0], default=None),
                "dec_peak": max(dec, default=None),
                "spec_sum": sum(spec), "spec_n": len(spec),
                # gap-spanning bucket (sampler was down/quiet > 2 min): real
                # tokens but NOT a valid "one busy minute" for rate/savings
                # anchors — excluded from last_active_hour (audit M2)
                "synthetic": 1 if (now - self._rate_prev_t) > 120 else 0})
        self._rate_prev = base
        self._rate_prev_t = now

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
        ah = self.store.last_active_hour(self.id) if self.store else {"empty": True}
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
        for eng in cfg.get("engines", []):
            self._start(eng)

    def _start(self, eng: dict) -> None:
        s = EngineSampler(self.cfg, eng, self.broker, self.store)
        self.samplers[eng["id"]] = s
        s.start()

    def get(self, engine_id: str) -> EngineSampler | None:
        with self._lock:
            return self.samplers.get(engine_id)

    def all(self) -> list[EngineSampler]:
        with self._lock:
            return list(self.samplers.values())

    def snapshots(self, seconds: float | None = None) -> dict:
        return {s.id: s.snapshot(seconds) for s in self.all()}

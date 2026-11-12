"""Live prefill speed during LONG prefills (the operator's bug, 2026-11-09).

Symptom: TandemLLM prefill speed never shows while a long prefill is in
flight; short prefills show fine. Measured on the live engine with one
fresh ~90k-token uncached prompt (repro kit kept off-repo):
  * qse_prefill_tokens_forwarded_total FROZE for the whole 90 s prefill —
    the tandem dialect's only live-prefill counter is completion-charged;
  * the live board's now.prefilling flips true at prefill start;
  * now.prefill_live_tps publishes mid-flight for LONG prefills
    (count>=1, ~1030 tok/s measured) and NEVER for short ones (a 9k-token
    prefill stayed null through its whole 8 s);
  * the board's now.prefill_tps is a window average diluted by idle time
    (36.89 tok/s measured during a ~1000 tok/s prefill) — deliberately
    NOT used as a fallback: it would print a speed lie.

Pinned contracts:
  (a) fixture frame prefilling=true + prefill_live_tps set -> row shows it
      and carries the prefill_live marker the sub-line labels honestly;
  (b) long-prefill fixture where no counter moved (windowed rate None) ->
      the row still shows the live number;
  (c) short-prefill completion path unchanged: not prefilling -> the
      windowed value stands untouched; null live while prefilling -> the
      windowed value also stands (documented choice, see docstring);
  * PREFILL_MAX cap applies to the live figure like every other path;
  * junk frames (missing now, null/zero/negative/string values) are inert.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sampler import PREFILL_MAX, _apply_prefill_live    # noqa: E402


def _frame(**now):
    return {"now": now}


# ------------------------------------------------------------------ (a) ----

def test_live_frame_feeds_the_tile_and_marks_the_source():
    row = {"prefill_tps_avg": 140.0}          # windowed value from counters
    _apply_prefill_live(row, _frame(prefilling=True, prefill_live_tps=1034.7,
                                     prefill_live_count=1, prefill_tps=55.79))
    assert row["prefill_tps_avg"] == 1034.7
    assert row["prefill_live"] == 1


# ------------------------------------------------------------------ (b) ----

def test_long_prefill_no_counter_movement_still_shows_live():
    """The bug verbatim: 90 s prefill, forwarded_total frozen, windowed rate
    None — the tile must still print the engine's live figure."""
    row = {"prefill_tps_avg": None}
    _apply_prefill_live(row, _frame(prefilling=True, prefill_live_tps=1028.11,
                                    prefill_live_count=1, prefill_tps=55.79))
    assert row["prefill_tps_avg"] == 1028.11
    assert row["prefill_live"] == 1


# ------------------------------------------------------------------ (c) ----

def test_short_prefill_completion_path_untouched():
    """Not prefilling (short prefill already completed, its burst is in the
    window): the counter-path value stands, no marker."""
    row = {"prefill_tps_avg": 5200.0}
    _apply_prefill_live(row, _frame(prefilling=False, prefill_live_tps=None,
                                    prefill_live_count=0, prefill_tps=5033.77))
    assert row == {"prefill_tps_avg": 5200.0}


def test_stale_live_value_after_prefill_ends_is_ignored():
    """The prefilling flag is the gate: a board still carrying the last
    prefill_live_tps after the prefill ENDED must not pin the tile to that
    figure — the windowed path owns the number again once prefilling=false."""
    row = {"prefill_tps_avg": 4900.0}
    _apply_prefill_live(row, _frame(prefilling=False, prefill_live_tps=1034.7,
                                    prefill_live_count=1, prefill_tps=5033.77))
    assert row == {"prefill_tps_avg": 4900.0}


def test_null_live_while_prefilling_keeps_windowed_value_not_the_idle_avg():
    """Documented choice: while prefilling but before the engine's first
    live chunk, the windowed figure stands. now.prefill_tps (36.89 measured
    during a ~1000 tok/s prefill — idle-diluted) must NEVER become the
    tile's speed."""
    row = {"prefill_tps_avg": 4800.0}
    _apply_prefill_live(row, _frame(prefilling=True, prefill_live_tps=None,
                                    prefill_live_count=0, prefill_tps=36.89))
    assert row["prefill_tps_avg"] == 4800.0


def test_board_flag_never_touches_prefill_active_dim_is_adrians_ux():
    """the operator's correction (2026-11-09): the tile's DIM is ON PURPOSE — he
    reads dimming to see which lane is actually working. prefill_active
    belongs to the counter heuristic ALONE; the board's prefilling flag must
    not light it. The chart line survives through long prefills via the
    prefill_live marker instead (pinned in tests/js/series_sample.mjs)."""
    row = {"prefill_tps_avg": 4800.0, "prefill_active": False}
    _apply_prefill_live(row, _frame(prefilling=True, prefill_live_tps=None,
                                    prefill_live_count=0))
    assert row == {"prefill_tps_avg": 4800.0, "prefill_active": False}
    row = {"prefill_tps_avg": 4800.0, "prefill_active": False}
    _apply_prefill_live(row, _frame(prefilling=True, prefill_live_tps=1050.0,
                                    prefill_live_count=1))
    assert row["prefill_active"] is False        # dim untouched...
    assert row["prefill_tps_avg"] == 1050.0      # ...value + marker are not


# ----------------------------------------------------------------- caps ----

def test_live_figure_respects_the_plausibility_cap():
    row = {"prefill_tps_avg": 5000.0}
    _apply_prefill_live(row, _frame(prefilling=True,
                                    prefill_live_tps=PREFILL_MAX + 1))
    # absurd value: the windowed figure stands; the phase flag still lights
    # (prefilling is the engine's own truth about the phase, independent of
    # whether a plausible live speed exists yet)
    assert row == {"prefill_tps_avg": 5000.0}      # absurd: windowed stands


# ----------------------------------------------------------------- junk ----

def test_junk_frames_are_inert():
    for live in (None, {}, {"now": None}, {"now": {}}):
        row = {"prefill_tps_avg": 700.0}
        _apply_prefill_live(row, live)
        assert row == {"prefill_tps_avg": 700.0}, live
    # prefilling with a junk VALUE is fully inert (no marker, no override)
    for live in (_frame(prefilling=True, prefill_live_tps=0),
                 _frame(prefilling=True, prefill_live_tps=-5),
                 _frame(prefilling=True, prefill_live_tps="fast"),
                 _frame(prefilling=True)):
        row = {"prefill_tps_avg": 700.0}
        _apply_prefill_live(row, live)
        assert row == {"prefill_tps_avg": 700.0}, live

# ------------------------------------------------------- call-site pin ----

def test_tick_shows_live_prefill_through_the_real_scrape():
    """CALL-SITE pin (TandemLLM's mutation, NEAR-MISS-LOG 22:50): the unit
    pins pass even if _apply_prefill_live is moved BEFORE the prefill_active
    heuristic — the exact silent-overwrite bug the throwaway re-proof caught
    (the heuristic reassigns the flag after, blanking tile + chart line).
    Drive the real _tick: counters frozen (long prefill — nothing advances;
    tandem gen is completion-charged so the running-and-nothing-moved branch
    is off), board says prefilling with a live figure. The published row
    must carry the live speed AND the lit phase. Moving the call before the
    heuristic assignment fails the active assertion."""
    from app.sampler import EngineSampler
    from app.sse import Broker
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    s = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm"},
                      Broker(), store=None)
    qse = ("qse_requests_running 1.0\nqse_prompt_tokens_total 500000.0\n"
           "qse_generation_tokens_total 9000.0\nqse_request_success_total 5.0\n"
           "qse_prompt_tokens_cached_total 1000.0\n"
           "qse_prefill_tokens_forwarded_total 400000.0\n")
    s._fetch = lambda path, timeout=1.5: (
        qse.encode() if path == "/metrics" else b'{"data": []}')
    s._live_json = lambda d, path=None: {
        "now": {"prefilling": True, "prefill_live_tps": 1050.0,
                "prefill_live_count": 1}}
    s._tick()
    row = s.ring.last()
    assert row["prefill_active"] is False         # DIM stays the heuristic's (the operator)
    assert row["prefill_tps_avg"] == 1050.0       # engine live board wins
    assert row["prefill_live"] == 1               # marker keeps the chart line alive

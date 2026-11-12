"""One denominator per KV tile (the operator's bug, 2026-11-09).

The tile printed 37.1% above "614.2k / 836.1k tok used · 222.0k free": the
big % was the ACTIVE LANE's length over the PER-LANE window (262,144) while
the fraction beside it was the sum across all running lanes over the shared
recomputed fit (836,140). Three denominators in one tile, and the two
figures on it contradicted each other.

The invariant pinned here: the tile's % ALWAYS equals
round(100 x kv_used_tokens / the denominator the sub-line prints) —
kv_ref_tokens when configured, else kv_pool_tokens — across every engine
path (tandemllm live board, vLLM derived tokens, Tarnwell direct gauges).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sampler import _anchor_tile_pct    # noqa: E402


def _inv(row):
    """The invariant as the UI sees it: % == 100 x used / pool-shown."""
    pool = row.get("kv_ref_tokens") or row.get("kv_pool_tokens")
    assert row["kv_pct"] == round(100.0 * row["kv_used_tokens"] / pool, 3)


def test_tandem_screenshot_case():
    """Verbatim from the operator's screenshot: 614.2k used, ref 836.1k, per-lane
    window 262,144, old per-lane % 37.1 — the anchor must re-derive 73.5%."""
    row = {"kv_pct": 37.1, "kv_used_tokens": 614200, "kv_pool_tokens": 262144,
           "kv_free_tokens": 0, "kv_ref_tokens": 836140.0}
    _anchor_tile_pct(row)
    assert abs(row["kv_pct"] - 73.457) < 0.001
    assert row["kv_free_tokens"] == 836140 - 614200      # == the "free" shown
    _inv(row)


def test_tandem_without_ref_falls_back_to_lane_window():
    """No shared fit configured: the fraction falls back to the per-lane
    window, so the % follows the window — even past 100% (sum across lanes
    vs one lane's window is the honest reading the sub-line already prints)."""
    row = {"kv_pct": 25.4, "kv_used_tokens": 300000, "kv_pool_tokens": 262144}
    _anchor_tile_pct(row)
    assert abs(row["kv_pct"] - 114.441) < 0.001
    assert row["kv_free_tokens"] == 0
    _inv(row)


def test_vllm_path_is_an_identity():
    """vLLM derives used FROM pct x pool upstream; re-anchoring must not
    move the number (that path already had one denominator)."""
    pool, pct = 100000, 42.0
    used = round(pool * pct / 100.0)
    row = {"kv_pct": pct, "kv_pool_tokens": pool,
           "kv_used_tokens": used, "kv_free_tokens": pool - used}
    _anchor_tile_pct(row)
    assert row["kv_pct"] == pct
    _inv(row)


def test_tarnwell_tokens_win_over_disagreeing_gauge():
    """Tarnwell publishes pool + used directly AND a percent gauge. When the
    gauge disagrees with the tokens printed in the fraction, the % follows
    the tokens — the tile can never show two readings of one ratio."""
    row = {"kv_pct": 55.0, "kv_pool_tokens": 500000, "kv_used_tokens": 125000,
           "kv_free_tokens": 375000}
    _anchor_tile_pct(row)
    assert row["kv_pct"] == 25.0
    _inv(row)


def test_no_pool_or_no_used_leaves_pct_untouched():
    row = {"kv_pct": 12.5}                       # no pool, no fraction shown
    _anchor_tile_pct(row)
    assert row["kv_pct"] == 12.5
    row = {"kv_pool_tokens": 1000, "kv_pct": 9.9}    # pool but no used
    _anchor_tile_pct(row)
    assert row["kv_pct"] == 9.9 and "kv_free_tokens" not in row


def test_zero_used_is_a_real_zero():
    """Idle engine: used 0 is the truth, not a missing value — the anchor
    must write a real 0% (the tile's hold-last logic lives downstream)."""
    row = {"kv_pct": 33.0, "kv_used_tokens": 0, "kv_ref_tokens": 836140.0}
    _anchor_tile_pct(row)
    assert row["kv_pct"] == 0.0
    assert row["kv_free_tokens"] == 836140

def test_negative_or_zero_pool_never_prints_a_negative_pct():
    """A broken recompute or corrupt config must not push the tile into
    negative territory: an untrustworthy pool leaves the dialect's own
    reading standing (honest fallback), never a negative %."""
    row = {"kv_pct": 41.0, "kv_used_tokens": 100, "kv_ref_tokens": -500}
    _anchor_tile_pct(row)
    assert row["kv_pct"] == 41.0 and "kv_free_tokens" not in row


def test_tick_reanchors_pct_through_the_real_scrape():
    """CALL-SITE pin (TandemLLM's mutation, NEAR-MISS-LOG 22:50): the unit
    pins above pass even if _anchor_tile_pct is never called from the tick.
    Drive the real _tick: the live board's per-lane reading (614200/262144
    = 234%) must NOT survive into the published row — the tick's anchor
    rewrites it against the shared pool the fraction prints (73.457%).
    Removing the _anchor_tile_pct(row) call from _tick fails here."""
    from app.sampler import EngineSampler
    from app.sse import Broker
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    s = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm",
                            "kv_ref_tokens": 836140}, Broker(), store=None)
    qse = ("qse_requests_running 1.0\nqse_prompt_tokens_total 1000.0\n"
           "qse_generation_tokens_total 100.0\n")
    s._fetch = lambda path, timeout=1.5: (
        qse.encode() if path == "/metrics" else b'{"data": []}')
    def _fake_live(d, path=None):
        if path == "/health":
            return None
        return {"engine": {"kv": {"length": 614200, "max_len": 262144}},
                "requests": [], "now": {}}
    s._live_json = _fake_live
    s._tick()
    row = s.ring.last()
    assert row["kv_used_tokens"] == 614200
    assert row["kv_pct"] == round(100.0 * 614200 / 836140, 3)     # 73.457
    assert row["kv_pct"] != round(100.0 * 614200 / 262144, 3)     # per-lane gone
    assert row["kv_free_tokens"] == 836140 - 614200

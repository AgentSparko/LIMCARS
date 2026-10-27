"""The render contract: whatever the browser names, storage must keep.

This is the gate that makes "we only store what is shown" a checked property instead of a habit.
The contract is not hand-written — it is read out of the app and the client:

  * `sampler.SERIES_KEYS` is the chart series list the server publishes;
  * `hw.series_keys()` is the hardware panel's list, which includes keys (`load1`, `swap_used_gb`,
    `disk_r_mbs`) that a literal grep over the JS never finds — a grep-based "nobody reads this"
    verdict is exactly how a working panel gets deleted;
  * every identifier the client source names (web/js/*.js, web/*.html) is in the contract too,
    because cards and sparklines read fields by string.

A field outside all three is, by construction, invisible: storing it is pure cost.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from app import hw as hw_mod
from app import sampler
from app.store import Store

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
T0 = 1_800_000_000.0


def _client_identifiers() -> set[str]:
    names: set[str] = set()
    for p in sorted(list(WEB.rglob("*.js")) + list(WEB.rglob("*.html"))):
        text = p.read_text(encoding="utf-8", errors="replace")
        names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text))
    return names


CLIENT = _client_identifiers()


def _contract() -> set[str]:
    keys = set(sampler.SERIES_KEYS)
    keys.update(hw_mod.series_keys({f"gpu0_{k}": 1.0 for k in
                                    ("util", "temp_c", "power_w", "clock_mhz", "fan_speed")}))
    # dynamically-built names: histMs/histBasis concat "ttft"/"e2e" with these suffixes, so the
    # literal strings never appear in the source and a scan cannot see them
    for fam in ("ttft", "e2e"):
        for suf in ("_p50_ms", "_avg_ms", "_n", "_1h", "_p50_1h_ms", "_avg_1h_ms", "_n_1h",
                    "_med_1h", "_life_avg_ms", "_avg_1h_ms"):
            keys.add(f"{fam}{suf}")
    return keys


CONTRACT = _contract()


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "c.db"), persist_seconds=3600, ring_full_seconds=3600)


def test_the_contract_is_not_empty_or_stale():
    assert len(CONTRACT) > 30, CONTRACT
    assert {"prefill_tps_now", "kv_pct", "cpu_pct", "cpu_temp_c", "gpu_util"} <= CONTRACT
    # the client must name at least the fields the cards use, or the scan is reading nothing
    assert {"cpu_temp_c", "net_r_mbs", "net_w_mbs"} <= CLIENT, "client scan found nothing to read"


def test_every_engine_field_the_app_publishes_survives_storage(store):
    row = {k: 1.5 for k in sampler.SERIES_KEYS}
    store.save_engine_samples("e1", [(T0, row)])
    stored = json.loads(store._con.execute(
        "SELECT data FROM engine_samples WHERE t=?", (T0,)).fetchone()[0])
    lost = set(sampler.SERIES_KEYS) - set(stored)
    assert not lost, f"chart contract fields were stripped from storage: {sorted(lost)}"


def test_every_hardware_field_the_panels_chart_survives_storage(store):
    last = {k: 1.5 for k in hw_mod.series_keys({"gpu0_util": 1.0, "gpu_count": 1})}
    store.save_hw_samples([(T0, dict(last))])
    stored = json.loads(store._con.execute(
        "SELECT data FROM hw_samples WHERE t=?", (T0,)).fetchone()[0])
    lost = set(last) - set(stored)
    assert not lost, f"hardware panel series were stripped from storage: {sorted(lost)}"


def test_fields_the_client_names_are_kept(store):
    """The strongest form: anything a script or page mentions by name must still be in the row."""
    named = sorted(k for k in CLIENT if "_" in k and k[0].islower()
                   and not k.startswith(("_", "on", "x_")))
    engine_row = {k: 2.5 for k in named}
    store.save_engine_samples("e1", [(T0, dict(engine_row))])
    stored = json.loads(store._con.execute(
        "SELECT data FROM engine_samples WHERE t=?", (T0,)).fetchone()[0])
    # only fields that look like sampler metrics are relevant; the client also names config keys
    metric_like = {k for k in named if re.match(
        r"^(prefill|decode|itl|ttft|e2e|queue_time|kv|gen|draft|spec|cache_hit|cost|money|savings"
        r"|gpu|cpu|mem|disk|net|swap|load|temp|fan|energy|clock|running|waiting|requests|prompts?"
        r"|iters|preempts|queue_depth)", k)}
    assert metric_like, "the scan found no metric-like identifier to test"
    lost = {k for k in metric_like if k in engine_row and k not in stored}
    assert not lost, f"client-named fields were stripped: {sorted(lost)}"


# Fields the client names but reads exclusively off the live SSE row, never out of SQLite:
# dashboard.js reads l.savings where l = latest(), served by the in-memory Ring (sampler.py).
# Keeping the blob in storage duplicated a number that is recomputed twice a second (24.4% of
# every engine row) and no DB reader ever looked at it.
LIVE_ONLY = {"savings"}


def test_only_the_families_the_client_concatenates_are_exempt():
    """The whole latency-twin deletion rests on one fact: the client builds metric names at
    runtime for ttft and e2e ONLY (histNum/histMs/histBasis are called with those two). If someone
    later writes histNum(l, 'itl'), the drop-list becomes wrong and this is where it must fail."""
    js = (WEB / "js" / "dashboard.js").read_text(encoding="utf-8")
    used = set(re.findall(r"hist(?:Num|Ms|Basis)\(\s*l\s*,\s*'([a-z_0-9]+)'", js))
    assert used, "could not find any dynamically-read family — the scan pattern is stale"
    assert used <= {"ttft", "e2e"}, f"client now reads {sorted(used)} dynamically; _DROP_ENGINE must be re-checked"
    for fam in used:
        assert not any(k.startswith(fam) for k in Store._DROP_ENGINE), f"{fam} is read dynamically"


def test_the_deny_lists_cannot_touch_a_charted_field():
    """Structural gate: storage may only ever drop what no reader names. Both directions are
    checked, because the two deny-lists were each wrong once (engine twins that were in fact
    charted, hardware series that a JS grep could not see)."""
    assert not (Store._DROP_ENGINE & set(sampler.SERIES_KEYS)), \
        Store._DROP_ENGINE & set(sampler.SERIES_KEYS)
    charted = set(hw_mod.series_keys({"gpu0_util": 1.0, "gpu_count": 1, "vram_used_gb": 1.0}))
    assert not (Store._DROP_HW & charted), Store._DROP_HW & charted
    assert not (Store._DROP_HW & CLIENT), Store._DROP_HW & CLIENT
    assert not (Store._DROP_ENGINE & (CLIENT - LIVE_ONLY)), \
        Store._DROP_ENGINE & (CLIENT - LIVE_ONLY)
    # the live-only exemption must stay small and stay true: nothing in the store's own DB
    # readers may name it, or the exemption is hiding a stripped, read field
    assert LIVE_ONLY <= set(Store._DROP_ENGINE)


def test_storage_is_a_deny_list_and_the_gate_decides_it(store):
    """What the policy actually is: everything a reader names is kept, everything the gate proved
    unread is listed and dropped. Unknown names pass through on purpose -- being unlisted is not
    evidence of being unread, and deleting on that assumption is how the hardware panels lost their
    load/disk/net series once. So a field earns its way onto the list by failing every scan above.
    """
    store.save_engine_samples("e1", [(T0, {"prefill_tps_now": 1.0,
                                           "savings": {"day": 1.0},
                                           "some_future_metric_nobody_reads_yet": 12345.0})])
    stored = json.loads(store._con.execute(
        "SELECT data FROM engine_samples WHERE t=?", (T0,)).fetchone()[0])
    assert "savings" not in stored                      # listed: proven unread, so it is gone
    assert stored["prefill_tps_now"] == 1.0              # charted: kept
    assert "some_future_metric_nobody_reads_yet" in stored  # unlisted: kept until the gate sees it

"""KV tile honesty markers (drift study 2026-11-10, docs/wiki `498253e`).

The tile never printed a wrong number — but with `--floor 10` pinned in the
live `kv_ref_cmd` it printed a RIGHT number for the WRONG launch: 66.9 % shown
while the honest unpinned reading was 84.3 % (the pin is a 0.15.2-era knob;
the 0.15.3 launch prices floor 15). Two markers close the gap:

* ``kv_ref_pin_gb`` — pool_estimate emits ``floor_pinned`` whenever --floor
  overrode the launch knob; the sampler carries the GiB and the tile names it.
* ``kv_ref_step`` — when the ref steps (new launch knobs, a pin dropped), the
  next row carries the OLD value exactly once so the KV chart can ring the
  step: a % jump with no usage change must not read as a load spike.

The falsification pair lives in tests/js/kv_honesty.mjs (planted stale pin
MUST mark the tile; an honest row must print no badge).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.sampler import EngineSampler, SERIES_KEYS    # noqa: E402
from app.sse import Broker                            # noqa: E402

CFG = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}

# the exact shapes scripts/pool_estimate.py --json emits, measured 2026-11-10
PINNED = ("echo '{\"context_tokens_fit\": 836140, \"floor_gb\": 10.0, "
          "\"floor_pinned\": {\"floor_gb\": 10.0, \"why\": \"pinned by --floor, "
          "not the launch\"}, \"source\": \"recomputed\"}'")
UNPINNED = ("echo '{\"context_tokens_fit\": 663179, \"floor_gb\": 15.0, "
            "\"source\": \"recomputed\"}'")


def _sampler(cmd):
    return EngineSampler(CFG, {"id": "q", "url": "http://x", "kind": "tandemllm",
                               "kv_ref_cmd": cmd}, Broker(), store=None)


def _tick(s):
    """One real tick with a stubbed scrape (tandemllm dialect, one request)."""
    qse = ("qse_requests_running 1.0\nqse_prompt_tokens_total 1000.0\n"
           "qse_generation_tokens_total 100.0\n")
    s._fetch = lambda path, timeout=1.5: (
        qse.encode() if path == "/metrics" else b'{"data": []}')
    s._live_json = lambda d, path=None: (
        None if path == "/health" else
        {"engine": {"kv": {"length": 500000, "max_len": 262144}},
         "requests": [], "now": {}})
    s._tick()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell / procfs")
def test_planted_stale_pin_surfaces_on_the_engine():
    """The live incident, planted: a cmd pinned to floor 10 must leave the
    sampler carrying the pin so the tile can warn. Remove the floor_pinned
    handling from _kv_ref_refresh and this fails."""
    s = _sampler(PINNED)
    s._kv_ref_refresh(PINNED)
    assert s.kv_ref_pin_gb == 10.0
    assert s.snapshot()["kv_ref_pin_gb"] == 10.0


def test_unpinned_recompute_carries_no_pin():
    """The honest launch prints no badge: floor_pinned absent -> None."""
    s = _sampler(UNPINNED)
    s._kv_ref_refresh(UNPINNED)
    assert s.kv_ref_pin_gb is None
    assert s.snapshot()["kv_ref_pin_gb"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell / procfs")
def test_pin_clears_when_the_pin_is_dropped():
    """the operator's §5 fix live-fires this path: pinned -> unpinned recompute must
    clear the warning, not keep the stale pin (a stuck badge is decoration)."""
    s = _sampler(PINNED)
    s._kv_ref_refresh(PINNED)
    assert s.kv_ref_pin_gb == 10.0
    s._kv_ref_refresh(UNPINNED)
    assert s.kv_ref_pin_gb is None


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell / procfs")
def test_ref_step_is_stamped_exactly_once():
    """The first value is not a step; a changed value stamps ONE row with the
    OLD ref, and the row after is clean again (a persistent value would draw a
    ring every tick)."""
    s = _sampler(PINNED)
    s._kv_ref_refresh(PINNED)          # ref 836140, first read: no step
    _tick(s)
    row = s.ring.last()
    assert row["kv_ref_tokens"] == 836140.0
    assert "kv_ref_step" not in row
    assert row["kv_ref_pin_gb"] == 10.0          # pin rides every row
    s._kv_ref_refresh(UNPINNED)                  # the §5 fix fires: ref steps
    _tick(s)
    row = s.ring.last()
    assert row["kv_ref_tokens"] == 663179.0
    assert row["kv_ref_step"] == 836140.0        # the OLD value, once
    assert "kv_ref_pin_gb" not in row            # pin cleared with the pin
    _tick(s)
    assert "kv_ref_step" not in s.ring.last()    # stamped exactly once


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell / procfs")
def test_failure_keeps_last_good_and_invents_no_step():
    """A failing recompute keeps the marker AND stays silent: no step, no pin
    change (the old number stands until a good one arrives)."""
    s = _sampler(PINNED)
    s._kv_ref_refresh(PINNED)
    bad = "echo 'drift gate disagrees' >&2; exit 3"
    s._kv_ref_refresh(bad)
    assert s.kv_ref_tokens == 836140.0
    assert s.kv_ref_pin_gb == 10.0
    assert s._kv_ref_step is None
    assert s.kv_ref_error is not None


def test_step_key_is_seeded_not_only_live():
    """kv_ref_step must ride SERIES_KEYS so a page reload re-seeds past rings
    from the server ring — otherwise the mark only exists for tabs that were
    open when the step happened (the hw-chart-keys lesson)."""
    assert "kv_ref_step" in SERIES_KEYS


# ---- proxy incident 2026-11-12: identity gate on the ref consumer -----------
# The engine slot was swapped to vLLM behind a proxy on the SAME url the
# tandem kv_ref_cmd probes. The estimator exited 0 and priced the launch that
# was not running (663,179 for a TandemLLM that was down, while vLLM's own
# pool was 1,413,408). The tile must go UNAVAILABLE under a foreign identity,
# never keep or recompute a constant fallback.

PROXY_HEALTH = b'{"status": "ok", "engine": "vllm-proxy", "upstream": "127.0.0.1:8000"}'
TANDEM_HEALTH = b'{"status": "ok", "uptime_s": 991.6, "inflight": {"running": 7}}'
VLLM_BODY = ('# HELP vllm:cache_config_info Information of the LLMEngine\n'
             'vllm:cache_config_info{kv_cache_size_tokens="1413408",'
             'num_gpu_blocks="245",block_size="6624"} 1.0\n')


def _stub(s, health=PROXY_HEALTH, metrics=b'{"error": "vllm-proxy forwards /v1/* only"}'):
    if isinstance(metrics, str):
        metrics = metrics.encode()
    if isinstance(health, str):
        health = health.encode()
    s._fetch = lambda path, timeout=1.5: (metrics if path == "/metrics" else health)


def test_proxy_identity_blanks_the_ref_never_a_fallback():
    """The incident, planted: cmd would happily print 836140, but the url
    answers as a vllm-proxy — the ref MUST be None with a named reason.
    Remove the identity gate and this fails on the stale 836140."""
    s = _sampler(PINNED)
    s.kv_ref_tokens = 836140.0          # the static config marker, as live
    _stub(s)
    s._kv_ref_refresh(PINNED)
    assert s.kv_ref_tokens is None
    assert s.kv_ref_source is None
    assert s.kv_ref_error and "proxy" in s.kv_ref_error
    assert s.snapshot()["kv_ref_tokens"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell / procfs")
def test_tandem_identity_still_runs_the_cmd():
    """The gate must not blind the honest path: a tandem-shaped /health lets
    the recompute through untouched."""
    s = _sampler(PINNED)
    _stub(s, health=TANDEM_HEALTH)
    s._kv_ref_refresh(PINNED)
    assert s.kv_ref_tokens == 836140.0
    assert s.kv_ref_error is None


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell / procfs")
def test_unreachable_engine_keeps_the_last_good_ref():
    """Transient failure is not a swap: identity None (both probes raise)
    keeps the marker — blanking on every hiccup would be its own lie."""
    s = _sampler(PINNED)
    _stub(s, health=TANDEM_HEALTH)
    s._kv_ref_refresh(PINNED)
    assert s.kv_ref_tokens == 836140.0

    def raise_always(path, timeout=1.5):
        raise OSError("connection refused")
    s._fetch = raise_always
    s._kv_ref_refresh(PINNED)
    assert s.kv_ref_tokens == 836140.0


def test_direct_foreign_engine_blanks_by_metrics_family():
    """No proxy JSON to sniff (plain-text /health): the /metrics family is
    the identity witness — a vllm body under a tandem cmd still blanks."""
    s = _sampler(PINNED)
    s.kv_ref_tokens = 836140.0
    _stub(s, health=b"ok", metrics=VLLM_BODY)
    s._kv_ref_refresh(PINNED)
    assert s.kv_ref_tokens is None
    assert s.kv_ref_error and "vllm" in s.kv_ref_error


def test_vllm_engine_ref_is_its_measured_pool():
    """A vLLM-family engine (no kv_ref_cmd at all) gets the ref from its own
    cache_config_info: the tile denominator equals the engine's pool, so
    used/pool matches kv_cache_usage_perc instead of staying absent."""
    s = EngineSampler(CFG, {"id": "v", "url": "http://x", "kind": None},
                      Broker(), store=None)
    s._fetch = lambda path, timeout=1.5: (
        VLLM_BODY.encode() if path == "/metrics" else b"ok")
    s._live_json = lambda d, path=None: None
    s._refresh_model = lambda now: None
    s._tick()
    assert s.kv_ref_tokens == 1413408.0
    assert s.kv_ref_source == "engine-pool"
    assert s.ring.last()["kv_ref_tokens"] == 1413408.0


def test_client_push_list_carries_the_step_key():
    """SSE whitelist pin: a key missing from the engine push list blanks the
    chart once the page-load seed slides out of the window — the exact bug
    that bit the RAM chart. dashboard.js must push kv_ref_step."""
    js = (ROOT / "web" / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert "'kv_ref_step'" in js, "kv_ref_step missing from the SSE push list"
    assert "kvPinNote(l)" in js, "the KV tile sub-line no longer calls kvPinNote"
    assert "mark: true" in js, "the KV chart no longer declares the ref-step marker"


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_markers_falsification():
    """Planted stale-pin scenario must make the tile visibly marked, and an
    honest row must print no badge (no decorative warning)."""
    r = subprocess.run([NODE, str(ROOT / "tests" / "js" / "kv_honesty.mjs")],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, f"KV honesty falsification failed:\n{out}"
    assert "all checks passed" in out, out

# ---- second-audit follow-ups L1/L2 (TandemLLM, 2026-11-10) -----------------
# L1: argparse type=float happily accepts "nan"/"inf"; a non-finite knob that
#     reaches json.dumps emits the BARE NaN/Infinity token — invalid JSON that
#     Python's parser swallows while the browser's JSON.parse rejects it, which
#     kills the engine handler for EVERY snapshot.
# L2: a string-typed kv_ref_tokens in an engine row makes the sampler raise
#     TypeError on every refresh (caught fail-open): the step mark silently
#     dies while the tile looks healthy.

import importlib.util
import json

POOL = ROOT / "scripts" / "pool_estimate.py"


def _load_pool_estimate():
    spec = importlib.util.spec_from_file_location("pool_estimate", POOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_L1_finite_gate_rejects_nan_and_inf():
    pe = _load_pool_estimate()
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            pe._require_finite("--floor", bad)
    pe._require_finite("--floor", 10.0)      # finite passes
    pe._require_finite("fit", 663179)        # ints pass untouched


def test_L1_cli_refuses_non_finite_floor_before_anything_runs():
    # the guard must fire BEFORE the engine-dir access so a typo can never run
    # the estimate at all, and must publish nothing on stdout — ever.
    for bad in ("nan", "inf"):
        r = subprocess.run([sys.executable, str(POOL), "--engine-dir", "/nonexistent",
                            "--floor", bad, "--json"], capture_output=True, text=True)
        assert r.returncode != 0, f"--floor {bad} was accepted"
        assert "finite" in r.stderr
        assert r.stdout.strip() == ""
    # "-inf" is refused even earlier: argparse reads a leading dash as a flag
    # (never reaches the estimate either way)
    r = subprocess.run([sys.executable, str(POOL), "--engine-dir", "/nonexistent",
                        "--floor", "-inf", "--json"], capture_output=True, text=True)
    assert r.returncode != 0 and r.stdout.strip() == ""
    # a finite floor passes the gate (dies later on the missing engine dir —
    # a different error, proving the gate is specific, not blanket)
    r = subprocess.run([sys.executable, str(POOL), "--engine-dir", "/nonexistent",
                        "--floor", "10", "--json"], capture_output=True, text=True)
    assert "finite" not in r.stderr


def test_L2_string_kv_ref_tokens_coerced_or_dropped(tmp_path, monkeypatch):
    import app.config as config
    (tmp_path / "config.json").write_text(json.dumps({"engines": [
        {"id": "a", "kv_ref_tokens": "836140"},      # numeric string -> float
        {"id": "b", "kv_ref_tokens": "not-a-number"}, # garbage -> None (dash)
        {"id": "c", "kv_ref_tokens": "nan"},          # parses! finite check too
        {"id": "d", "kv_ref_tokens": "inf"},
        {"id": "e", "kv_ref_tokens": "-5"},
        {"id": "f", "kv_ref_tokens": 836140},         # numeric passes untouched
    ]}), encoding="utf-8")
    monkeypatch.setattr(config, "DATA", tmp_path)
    engines = {e["id"]: e.get("kv_ref_tokens") for e in config.load()["engines"]}
    assert engines["a"] == 836140.0 and isinstance(engines["a"], float)
    assert engines["b"] is None and engines["c"] is None
    assert engines["d"] is None and engines["e"] is None
    assert engines["f"] == 836140

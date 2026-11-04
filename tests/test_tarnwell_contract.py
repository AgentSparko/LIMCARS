"""Tarnwell dialect contract, against a body recorded from the engine's own Metrics.render().

Every mapped name must carry a sample in the capture; the tarnwell body must detect as tarnwell while every other fixture keeps
its old kind; and the prefill pair must read prefill-only passes (rows and seconds of the same step kind), never the
mixed passes that also carry decode rows.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import dialects as D  # noqa: E402
from app import prom  # noqa: E402

FIXDIR = pathlib.Path(__file__).resolve().parent / "fixtures"
FIX = FIXDIR / "tarnwell_metrics.txt"


def _names_of(spec):
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, dict):  # {"sum"|"ratio": [specs]}
        return [n for v in spec.values() for n in _names_of(v)]
    if isinstance(spec, (list, tuple)):  # [name, {label match}, alt names...]: the match dict holds labels, not names
        return [n for v in spec if not isinstance(v, dict) for n in _names_of(v)]
    return []


def _series():
    return prom.parse(FIX.read_text(encoding="utf-8"))


def test_tarnwell_body_detects_as_tarnwell():
    assert D.detect(FIX.read_text(encoding="utf-8")) == "tarnwell"


def test_other_fixtures_keep_their_kind():
    want = {"vllm_metrics.txt": "vllm", "sglang_metrics.txt": "sglang", "llamacpp_metrics.txt": "llamacpp",
            "atlas_metrics.txt": "atlas"}
    for name, kind in want.items():
        if (FIXDIR / name).exists():
            assert D.detect((FIXDIR / name).read_text(encoding="utf-8")) == kind, name


def test_every_mapped_tarnwell_metric_is_emitted():
    raw = FIX.read_text(encoding="utf-8")
    for kind in ("gauge", "counter", "hist"):
        for logical, spec in D.TARNWELL[kind].items():
            for nm in _names_of(spec):
                probe = nm + "_count" if kind == "hist" else nm
                assert ("\n" + probe + "{") in raw, (logical, nm)


def test_prefill_pair_reads_prefill_only_passes():
    c = D.read_counters(_series(), D.TARNWELL)
    assert c["prefill_compute_tokens"] == 300.0     # prefill-only pass rows, mixed-pass rows excluded
    assert abs(c["prefill_seconds"] - 0.373207) < 1e-9
    assert abs(c["decode_seconds"] - 2.060109) < 1e-9
    assert c["prompt_tokens"] == 4000.0 and c["cached_tokens"] == 0.0
    assert c["accepted_tokens"] <= c["draft_tokens"]


def test_kv_pool_in_tokens_is_read():
    g = D.read_gauges(_series(), D.DIALECTS["tarnwell"])
    assert g["kv_used_tokens"] == 1536 and g["kv_pool_tokens"] == 541903

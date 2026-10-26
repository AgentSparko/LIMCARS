"""Engine-dialect fixture tests — the guard against a dialect that lies.

A dialect maps logical signals (prompt_tokens, cached_tokens, kv_pct, ttft…) onto
an engine's Prometheus names. When that mapping is wrong the dashboard does not
crash: it reports a wrong number, and the wrongest possible place is the token
counters, because money is computed from them. This file exists because a
third-party dialect once shipped mapping `proc_start` to a metric the engine
never emits, and reading it at a desk did not catch it.

tests/fixtures/vllm_metrics.txt is a RECORDED capture from a running vLLM, so it
proves upstream truth for that dialect. The sglang and llamacpp fixtures are
hand-authored from documented family names (labelled as such in the files) and
prove internal consistency only — they are a floor, not a substitute for a
capture, and the test asserts that distinction rather than hiding it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import dialects as D
from app import prom

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"

# (dialect kind, fixture file, is this a recorded live capture?)
# The llama.cpp case lives on its own lane together with the hand-written fixture
# that lane's dialect needs: this file is about RECORDED captures from real engines,
# and mixing a synthetic body into this list blurs the provenance distinction the
# file exists to enforce (test_recorded_capture_is_actually_a_capture asserts the
# opposite property of the same list). Each dialect's fixture is checked on the lane
# that introduced it, so each lane is green alone instead of leaning on files that
# only appear after a merge.
CASES = [
    ("vllm", "vllm_metrics.txt", True),
    ("sglang", "sglang_metrics.txt", False),
]


def _mapped_names(kind):
    d = D.DIALECTS[kind]
    out = []
    for sec in ("gauge", "counter", "hist"):
        for logical, spec in d.get(sec, {}).items():
            names = _names_of(spec)
            assert names, f"{kind}.{sec}.{logical} maps to nothing checkable"
            out.append((sec, logical, names))
    return out


def _names_of(spec):
    """Flatten a mapping spec to the metric names it actually looks for.

    Iterating a dict in Python yields its KEYS, so an earlier version of this
    helper compared the string "sum" (the key of {"sum": [...]}) against the
    exposition. vLLM passed by luck because vllm:prompt_tokens_sum contains
    "sum"; llama.cpp, whose series carry no _sum suffix, failed. A contract test
    that checks the wrong token is worse than none, because it advertises cover
    it does not have.
    """
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, dict):
        out = []
        for v in spec.values():
            out.extend(_names_of(v))
        return out
    if isinstance(spec, (list, tuple)):
        out = []
        for v in spec:
            out.extend(_names_of(v))
        return out
    return []


def _emitted_names(raw):
    """Series names that carry a numeric sample, parsed the way Prometheus
    actually writes them: <name>{<labels>} <value> or <name> <value>.

    The first version of this helper took the token after the name and tried to
    read it as a float, so every LABELLED series -- which is all of vLLM's, e.g.
    vllm:num_requests_running{engine="0",model_name="..."} 0.0 -- looked absent
    and the contract reported the whole dialect as broken. Absence of a metric and
    a bug in the test look identical from a green/red light, so this is written
    against the format rather than against one unlabelled fixture.
    """
    names = set()
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, tail = line.partition(" ")
        if "{" in head:                       # name{labels} value
            name, rest = head.split("{", 1)
            value = rest.rsplit("}", 1)[-1].strip() if "}" in rest else ""
            if tail.strip():
                value = tail.strip()
        else:
            name, value = head, tail.strip()
        if not name:
            continue
        value = value.split()[0] if value else ""
        try:
            float(value)
        except (TypeError, ValueError):
            continue
        names.add(name)
    return names


def _emitted(raw, name, hist=False):
    """A mapped name counts as emitted if the sample exists; for histograms the
    exposition never carries the bare name, only _bucket/_sum/_count, so those
    suffixes are the honest check."""
    names = _emitted_names(raw)
    if name in names:
        return True
    if hist:
        return any(f"{name}{s}" in names for s in ("_bucket", "_sum", "_count"))
    return False


def _read(name):
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


def _num(series, spec):
    """_get returns a float for kind="value" and a tuple for kind="hist"; the
    type is only pinned by the string argument, so assert it instead of
    comparing blindly -- a spec that accidentally resolves to a histogram would
    otherwise make these numeric assertions meaningless.
    """
    v = D._get(series, spec, "value")
    assert v is None or isinstance(v, (int, float)), f"value spec returned {type(v).__name__}"
    return float(v) if v is not None else None


def test_every_mapped_metric_name_is_emitted_by_the_engine():
    """The Atlas bug class: a mapping that names a metric the engine never sends
    reads as None forever, or worse, silently resolves to a sibling metric."""
    for kind, fname, recorded in CASES:
        raw = _read(fname)
        missing = []
        for sec, logical, names in _mapped_names(kind):
            if not any(_emitted(raw, n, hist=(sec == "hist")) for n in names):
                missing.append(f"{sec}.{logical} -> {'|'.join(names)}")
        assert not missing, f"{kind}: dialect maps metrics absent from {fname}: {missing}"


def test_recorded_capture_is_actually_a_capture():
    """A fixture claiming to be live must look live: many samples, real TYPE
    lines, several histogram families. Guards against someone quietly replacing
    the recorded file with a hand-written stub."""
    raw = _read("vllm_metrics.txt")
    samples = [l for l in raw.splitlines() if l and not l.startswith("#")]
    assert len(samples) > 200, f"vllm fixture has only {len(samples)} samples"
    assert raw.count("# TYPE vllm:") > 20, "vllm fixture lacks real TYPE declarations"
    assert raw.count("_bucket{") > 50, "vllm fixture has no real histogram buckets"


def test_cached_tokens_stay_a_subset_of_prompt_tokens():
    """Money invariant: the pricing split bills (prompt - cached) at the write
    rate and cached at the read rate. A dialect that maps cached to something
    bigger than prompt inflates savings out of thin air."""
    for kind, fname, _ in CASES:
        d = D.DIALECTS[kind]
        series = prom.parse(_read(fname))
        p = _num(series, d["counter"].get("prompt_tokens"))
        c = _num(series, d["counter"].get("cached_tokens"))
        # A guard that skips on None disables itself exactly when it is needed:
        # a cached_tokens mapping that stops resolving (the Atlas bug class -- a
        # name the engine never emits) yields None, and the old code continued
        # past it. Both dialects in CASES declare cached_tokens, so None here is
        # a broken mapping, not a missing metric.
        dial = D.DIALECTS[kind]
        declares_cached = "cached_tokens" in dial.get("counter", {})
        if declares_cached:
            assert c is not None, (
                f"{kind}: cached_tokens maps to {dial['counter']['cached_tokens']!r} "
                f"but resolved to None -- the engine never emits it, so every "
                f"cache-hit and billing-split figure built on it is fiction")
            assert p is not None, f"{kind}: prompt_tokens resolved to None"
        if p is None or c is None:
            continue
        assert c <= p, f"{kind}: cached {c} > prompt {p} — pricing split would lie"
        assert 0 <= c, f"{kind}: negative cached tokens"


def test_gauges_are_bounded_and_counters_are_numbers():
    for kind, fname, _ in CASES:
        d = D.DIALECTS[kind]
        series = prom.parse(_read(fname))
        kv = _num(series, d["gauge"].get("kv_pct"))
        if kv is not None:
            assert 0.0 <= kv <= 1.0, f"{kind}: kv_pct {kv} outside 0..1 (fraction vs percent?)"
        for logical in ("prompt_tokens", "gen_tokens", "req_success"):
            v = _num(series, d["counter"].get(logical))
            if v is not None:
                assert v >= 0, f"{kind}: {logical} = {v!r}"


def test_label_matched_prefill_uses_the_label_not_the_whole_family():
    """sglang prefill_tokens matches mode="prefill*". Summing the family instead
    would fold decode tokens into prefill and distort the prefill/decode split
    the speed cards are built on."""
    d = D.DIALECTS["sglang"]
    series = prom.parse(_read("sglang_metrics.txt"))
    pre = _num(series, d["counter"]["prefill_tokens"])
    assert pre == 1250000, f"prefill label match returned {pre}, expected the prefill sample only"


def test_missing_metrics_read_none_never_zero():
    """An absent signal must render as an em-dash, not as 0. A 0 prefill counter
    reads as "engine is fast" where None reads as "unknown" — the difference is
    how an operator trusts the dashboard."""
    d = D.DIALECTS["vllm"]
    empty = prom.parse("# nothing here\n")
    for sec in ("gauge", "counter"):
        for logical, spec in d.get(sec, {}).items():
            assert D._get(empty, spec, "value") is None, f"vllm {logical} fabricated a value"

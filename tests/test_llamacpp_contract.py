"""llama.cpp dialect contract: every mapped metric must exist in the engine.

Self-contained on purpose. The recorded-capture lane (vLLM, SGLang) has its own
contract test and a shared helper module; this lane is not stacked on that one, so
importing from it would create a dependency that only exists after a merge -- which
is precisely the defect that let the invented metrics through: the llama.cpp map was
merged on one branch and the fixture that would have contradicted it lived on
another, so neither branch was ever red on its own.

The fixture here is HAND-AUTHORED from llama.cpp's registry in
tools/server/server-task.cpp plus README.md and tests/unit/test_metrics.py on master
(2026-10-26), because no llama.cpp server runs on this box. It is labelled as such
in the file. It is not evidence that llama.cpp emits those lines; upstream source is.
What it does prove is that this dashboard's map and its own documented expectations
agree, and it failed loudly the first time -- catching llamacpp:kv_cache_usage_ratio
and llamacpp:requests_completed, which upstream does not have.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import dialects as D          # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "llamacpp_metrics.txt"
KIND = "llamacpp"


def _names_of(spec):
    """Metric names a mapping actually looks for. Iterating a dict yields KEYS, so
    {"sum": ["a", "b"]} flattened by iteration would check the literal "sum"."""
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


def _emitted_series(raw):
    """Names carrying a numeric sample: <name>{labels} <value> or <name> <value>.
    Matching the bare name anywhere would also match a "# TYPE" comment line, so a
    deleted sample would still count as emitted."""
    out = set()
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, tail = line.partition(" ")
        name = head.split("{", 1)[0]
        value = (tail.strip() or (head.rsplit("}", 1)[-1] if "}" in head else "")).strip()
        if not name or not value:
            continue
        try:
            float(value.split()[0])
        except (TypeError, ValueError):
            continue
        out.add(name)
    return out


def _fixture():
    if not FIX.exists():
        raise AssertionError(f"missing fixture {FIX}: the lane that adds a dialect "
                             f"must ship the fixture that checks it")
    return FIX.read_text(encoding="utf-8")


def test_every_mapped_llamacpp_metric_exists_upstream():
    raw = _fixture()
    emitted = _emitted_series(raw)
    missing = []
    for sec in ("gauge", "counter", "hist"):
        for logical, spec in D.DIALECTS[KIND].get(sec, {}).items():
            names = _names_of(spec)
            assert names, f"{KIND}.{sec}.{logical} maps to nothing checkable"
            if sec == "hist":
                found = any(f"{n}{suf}" in emitted for n in names
                            for suf in ("_bucket", "_sum", "_count"))
            else:
                found = any(n in emitted for n in names)
            if not found:
                missing.append(f"{sec}.{logical} -> {'|'.join(names)}")
    assert not missing, (
        f"llama.cpp dialect maps metrics absent from the engine's registry: {missing}. "
        f"A metric the engine never emits reads None forever, or resolves to a "
        f"sibling and reports someone else's number.")


def test_map_does_not_carry_the_known_invented_names():
    """Regression lock for the two fabricated series and the reversed alias."""
    raw = str(D.DIALECTS[KIND])
    for bad in ("kv_cache_usage_ratio", "requests_completed", "predicted_tokens_total"):
        assert bad not in raw, f"{bad} is not a llama.cpp metric and must not be mapped"


def test_fixture_declares_itself_synthetic():
    """Provenance has to travel with the data. A hand-written file that reads like a
    capture gets cited as evidence of engine behaviour it never recorded."""
    head = _fixture().splitlines()[:6]
    assert any("Hand-authored" in l or "NOT recorded" in l for l in head), head

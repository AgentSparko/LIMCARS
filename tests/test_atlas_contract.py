"""Atlas dialect contract: every mapped name must exist in the engine registry.

Self-contained, because this lane is not stacked on the fixtures lane -- a shared
helper module would only exist after a merge, and that blind spot (map on one
branch, contradicting fixture on another) is what let invented metrics through.

The fixture is hand-authored from crates/spark-server/src/metrics.rs on master; no
Atlas engine runs here. So this proves the map agrees with the documented registry,
not with a live scrape -- record a real capture before calling Atlas supported.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import dialects as D          # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "atlas_metrics.txt"
KIND = "atlas"
UPSTREAM = {
    "atlas_requests_active", "atlas_requests_total", "atlas_prompt_tokens_total",
    "atlas_generation_tokens_total", "atlas_decoded_tokens_total",
    "atlas_spec_decode_verify_total", "atlas_time_to_first_token_seconds",
    "atlas_tool_calls_total", "atlas_loop_detector_verdicts_total",
    "atlas_http_bytes_in_total", "atlas_http_bytes_out_total",
}


def _names_of(spec):
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, dict):
        return [n for v in spec.values() for n in _names_of(v)]
    if isinstance(spec, (list, tuple)):
        return [n for v in spec for n in _names_of(v)]
    return []


def _emitted(raw):
    """Series carrying a numeric sample: <name>{labels} <value> or <name> <value>.
    A bare substring match would also hit the "# TYPE" comment lines."""
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


def test_every_mapped_atlas_metric_is_registered_upstream():
    if not FIX.exists():
        raise AssertionError("the lane that adds a dialect must ship its fixture")
    got = _emitted(FIX.read_text(encoding="utf-8"))
    missing = []
    for sec in ("gauge", "counter", "hist"):
        for logical, spec in D.DIALECTS[KIND].get(sec, {}).items():
            names = _names_of(spec)
            assert names, f"atlas.{sec}.{logical} maps to nothing checkable"
            ok = (any(f"{n}{suf}" in got for n in names for suf in ("_bucket", "_sum", "_count"))
                  if sec == "hist" else any(n in got for n in names))
            if not ok:
                missing.append(f"{sec}.{logical} -> {'|'.join(names)}")
    assert not missing, f"Atlas dialect maps names absent from its registry: {missing}"


def test_fixture_invents_nothing():
    got = {n.split("{")[0] for n in _emitted(FIX.read_text(encoding="utf-8"))}
    unknown = {n for n in got if not any(n == u or n.startswith(u + "_") for u in UPSTREAM)}
    assert not unknown, f"fixture carries names Atlas does not register: {unknown}"


def test_python_process_collector_name_is_not_mapped():
    assert "process_start_time_seconds" not in str(D.DIALECTS[KIND]), (
        "that name is prometheus_client's, not Atlas's")

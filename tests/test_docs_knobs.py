"""Docs and code must agree about the environment surface, in both directions.

Born from a real defect: the README's security section advertised an
`..._ALLOW_REMOTE_WRITES=1` escape hatch for the fail-closed write gate. No code ever
read it, and the claim had spread into docs/CODEMAP.md and docs/AUDIT.md. An operator
who set it, saw nothing happen, and concluded the dashboard was now reachable from the
couch would be holding a wrong model of what protects a box whose recipe runner executes
shell. Meanwhile `LIMCARS_TOKEN` -- which the code *does* read -- was documented
nowhere, so a headless box could not be gated before first use.

Both directions are therefore pinned: no documented knob without an implementation,
and no implemented knob without documentation.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "docs" / "CODEMAP.md", ROOT / "docs" / "AUDIT.md"]
EXPECTED = {"LIMCARS_HOST", "LIMCARS_PORT", "LIMCARS_DB", "LIMCARS_TOKEN"}


def _knobs_in(root, suffix="*.py"):
    names = set()
    for p in root.rglob(suffix):
        names.update(re.findall(r"LIMCARS_[A-Z_]+", p.read_text(encoding="utf-8", errors="replace")))
    return names


def test_no_documented_knob_is_unimplemented():
    implemented = _knobs_in(ROOT / "app")
    assert implemented, "no LIMCARS_* knobs in app/ -- this test is misconfigured"
    ghost = {f"{d.name}: {n}" for d in DOCS if d.exists()
             for n in re.findall(r"LIMCARS_[A-Z_]+", d.read_text(encoding="utf-8"))
             if n not in implemented}
    assert not ghost, f"docs advertise knobs no code reads: {sorted(ghost)}"


def test_no_implemented_knob_is_undocumented():
    documented = set()
    for d in DOCS:
        if d.exists():
            documented |= set(re.findall(r"LIMCARS_[A-Z_]+", d.read_text(encoding="utf-8")))
    undocumented = _knobs_in(ROOT / "app") - documented
    assert not undocumented, f"code reads env knobs that no document mentions: {sorted(undocumented)}"


def test_environment_surface_is_exactly_the_expected_four():
    """Deployment surface: a fifth knob should be a decision with a README line, not
    something discovered by grep during an incident."""
    assert _knobs_in(ROOT / "app") == EXPECTED, sorted(_knobs_in(ROOT / "app"))

"""Locate Hermes state DBs regardless of how LIMCARS was launched.

Pitfall (found in the wild): a LIMCARS server started from inside a Hermes
profile shell inherits HERMES_HOME=<home>/.hermes/profiles/<name>. Treating
that as THE hermes root hides the root install AND every sibling profile,
and mislabels the profile's own sessions as 'default'. Collect every
candidate root instead and dedupe; label a db 'default' unless it lives
under <root>/profiles/<name>/.
"""
from __future__ import annotations

import os
from pathlib import Path


def _roots() -> list[Path]:
    cands: list[Path] = []
    real = os.environ.get("HERMES_REAL_HOME")
    if real:
        cands.append(Path(real) / ".hermes")
    hh = os.environ.get("HERMES_HOME")
    if hh:
        p = Path(hh)
        # profile-scoped env: HERMES_HOME=<root>/profiles/<name>
        cands.append(p.parent.parent if p.parent.name == "profiles" else p)
    cands.append(Path.home() / ".hermes")
    out: list[Path] = []
    for r in cands:
        try:
            r = r.resolve()
        except OSError:
            continue
        if r.is_dir() and r not in out:
            out.append(r)
    return out


def hermes_state_dbs() -> list[tuple[str, Path]]:
    """[(profile_label, db_path)] across the root install and every profile."""
    seen: set[str] = set()
    out: list[tuple[str, Path]] = []
    for root in _roots():
        cands = [root / "state.db"]
        cands += sorted((root / "profiles").glob("*/state.db"))
        for db in cands:
            if not db.is_file():
                continue
            key = str(db)
            if key in seen:
                continue
            seen.add(key)
            profile = (db.parent.name if db.parent.parent.name == "profiles"
                       else "default")
            out.append((profile, db))
    return out

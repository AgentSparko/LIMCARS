"""Tokens per session, for the model currently served, active today.

Reads Hermes state.db (read-only) across the root home and every profile.
Sessions carry lifetime per-session token counters; we filter to sessions
that saw activity today and whose model matches the served one.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path


def _norm_model(m: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (m or "").lower())


def _dbs() -> list[tuple[str, Path]]:
    from .hermes_home import hermes_state_dbs
    return hermes_state_dbs()


def today_sessions(model: str | None) -> dict:
    """{model, day, sessions:[{profile,id,title,start,last,in,out,cache_r,calls}]}"""
    nm = _norm_model(model)
    lt = time.localtime()
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    rows, seen = [], set()
    for profile, db in _dbs():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error:
            continue
        c.row_factory = sqlite3.Row
        try:
            q = c.execute(
                "select id, title, model, started_at, last_activity_at,"
                " input_tokens, output_tokens, cache_read_tokens,"
                " cache_write_tokens, api_call_count"
                " from sessions where last_activity_at >= ?", (midnight,))
            for r in q:
                if nm and _norm_model(r["model"]) != nm:
                    continue
                key = (profile, r["id"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "profile": profile, "id": r["id"],
                    "title": (r["title"] or "")[:60],
                    "model": r["model"],
                    "start": r["started_at"], "last": r["last_activity_at"],
                    "in": r["input_tokens"] or 0, "out": r["output_tokens"] or 0,
                    "cache_r": r["cache_read_tokens"] or 0,
                    "cache_w": r["cache_write_tokens"] or 0,
                    "calls": r["api_call_count"] or 0,
                })
        except sqlite3.Error:
            pass
        finally:
            c.close()
    rows.sort(key=lambda s: s["last"], reverse=True)
    return {"model": model, "day": time.strftime("%Y-%m-%d"),
            "sessions": rows,
            "tot_in": sum(s["in"] for s in rows),
            "tot_out": sum(s["out"] for s in rows),
            "tot_cache_r": sum(s["cache_r"] for s in rows)}

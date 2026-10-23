"""Hermes Traces — full port of old LIMCARS /subagent-watch/* API.

Contract mirrors limcars/server.js so the proven two-pane UI
(web/subagent-watch/) works unmodified:
  GET batches                       → {generated, cache_present, batches[], summaries[]}
  GET manifest/<deleg_id>           → manifest.json verbatim
  GET transcript/<id>/task-N.log    → text, HTTP Range (bytes=-N tail polls)
  GET summary/<file>                → text
  GET main-sessions                 → live/history sessions from state.db (all profiles)
  GET main-transcript/<profile>/<sid> → {lines[], ...} formatted like subagent logs

Cache dirs: $HERMES_HOME (default ~/.hermes) cache/delegation + every
named profile's cache/delegation. Read-only, strict filename regexes.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

_ID_RE = re.compile(r"^deleg_[a-z0-9]+$", re.I)
_TASK_RE = re.compile(r"^task-\d+\.log$")
_SUM_RE = re.compile(r"^subagent-summary-\d+-\d{8}_\d{6}_\d{6}\.txt$")
_PROF_RE = re.compile(r"^[a-z0-9_-]+$", re.I)
_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

SLIVE_S = 120          # "working right now" window
SHOW_MSG_MIN = 3       # hide empty stub sessions
MAIN_SESSION_CAP = 60


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def cache_dirs() -> list[Path]:
    home = _hermes_home()
    dirs = [home / "cache" / "delegation"]
    prof = home / "profiles"
    if prof.is_dir():
        for p in sorted(prof.iterdir()):
            if p.name.startswith("."):
                continue
            dirs.append(p / "cache" / "delegation")
    return [d for d in dirs if d.is_dir()]


def find_batch_dir(bid: str) -> Path | None:
    if not _ID_RE.match(bid):
        return None
    for d in cache_dirs():
        if (d / "live" / bid).is_dir():
            return d
    return None


def _started_epoch(man: dict | None) -> float:
    if man and man.get("started"):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", str(man["started"]))
        if m:
            return time.mktime(tuple(int(x) for x in m.groups()) + (0, 0, -1))
    return 0.0


def batches() -> dict:
    dirs = cache_dirs()
    if not dirs:
        return {"generated": time.time(), "cache_present": False, "batches": [], "summaries": []}
    out, summaries = [], []
    for d in dirs:
        live = d / "live"
        if live.is_dir():
            for p in sorted(live.iterdir()):
                if not p.is_dir() or not _ID_RE.match(p.name):
                    continue
                man = None
                mf = p / "manifest.json"
                try:
                    if mf.exists():
                        man = json.loads(mf.read_text())
                except (OSError, ValueError):
                    man = None
                logs = [f.name for f in p.iterdir() if f.is_file() and _TASK_RE.match(f.name)]
                newest = max((f.stat().st_mtime for f in p.iterdir() if f.is_file()), default=0.0)
                newest = max(newest, _started_epoch(man))
                out.append({"id": p.name,
                            "started": (man or {}).get("started"),
                            "completed": (man or {}).get("completed"),
                            "model": (man or {}).get("model"),
                            "task_count": (man or {}).get("task_count", len(logs)),
                            "tasks": len(logs), "mtime": newest,
                            "has_manifest": man is not None})
        try:
            for f in sorted(d.iterdir()):
                if _SUM_RE.match(f.name):
                    summaries.append(f.name)
        except OSError:
            pass
    out.sort(key=lambda x: -(x["mtime"] or 0))
    summaries.sort()
    return {"generated": time.time(), "cache_present": True,
            "batches": out, "summaries": summaries}


def manifest(bid: str):
    d = find_batch_dir(bid)
    if not d:
        return None
    try:
        return json.loads((d / "live" / bid / "manifest.json").read_text())
    except (OSError, ValueError):
        return {}


def transcript_file(bid: str, fname: str) -> Path | None:
    if not _TASK_RE.match(fname):
        return None
    d = find_batch_dir(bid)
    if not d:
        return None
    base = (d / "live" / bid).resolve()
    f = (base / fname).resolve()
    return f if str(f).startswith(str(base)) and f.is_file() else None


def summary_file(fname: str) -> Path | None:
    if not _SUM_RE.match(fname):
        return None
    for d in cache_dirs():
        f = d / fname
        if f.is_file():
            return f
    return None


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """bytes=-N (tail), bytes=start-, bytes=start-end."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    m = re.fullmatch(r"(\d*)-(\d*)", spec)
    if not m or not any(m.groups()):
        return None
    a, b = m.groups()
    if a == "":
        n = int(b)
        if n <= 0:                     # audit: bytes=-0 produced (size, size-1)
            return None                # — an invalid range into the file reader
        return (max(0, size - n), size - 1) if size else (0, 0)
    start = int(a)
    end = min(int(b) if b else size - 1, size - 1)
    return (start, end) if start <= end else None


# ---------------- main-agent sessions (state.db across profiles) ----------------

def _state_dbs() -> list[tuple[str, Path]]:
    home = _hermes_home()
    out = []
    d = home / "state.db"
    if d.is_file():
        out.append(("default", d))
    prof = home / "profiles"
    if prof.is_dir():
        for p in sorted(prof.iterdir()):
            s = p / "state.db"
            if s.is_file():
                out.append((p.name, s))
    return out


def main_sessions() -> dict:
    now = time.time()
    out = []
    for profile, db in _state_dbs():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = c.execute("""
                SELECT s.id, s.source, s.title, s.display_name, s.model,
                       s.started_at, s.ended_at, s.last_activity_at,
                       s.last_activity_description, s.message_count,
                       MAX(m.timestamp) AS last_msg
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                WHERE s.archived = 0 AND s.hidden = 0
                GROUP BY s.id
                HAVING MAX(m.timestamp) IS NOT NULL OR s.message_count > ?
            """, (SHOW_MSG_MIN,)).fetchall()
            for (sid, source, title, disp, model, started, ended, laa,
                 ldesc, mcount, last_msg) in rows:
                last = last_msg or laa
                if (mcount or 0) < SHOW_MSG_MIN and not (last and now - last <= SLIVE_S):
                    continue
                out.append({"id": sid, "profile": profile, "source": source,
                            "title": title or None, "display": disp or None,
                            "model": model, "msg_count": mcount or 0,
                            "started_at": started, "ended_at": ended or None,
                            "last_activity": last,
                            "last_activity_s": round(now - last) if last else None,
                            "last_activity_desc": ldesc or None,
                            "live": bool(last and now - last <= SLIVE_S)})
        except sqlite3.Error:
            pass
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    out.sort(key=lambda x: -(x["last_activity"] or 0))
    return {"generated": time.time(), "sqlite": True, "sessions": out[:MAIN_SESSION_CAP]}


def _fmt_hms(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "--:--:--"


def main_transcript(profile: str, sid: str, limit: int = 200) -> dict | None:
    if not _PROF_RE.match(profile) or not _SID_RE.match(sid):
        return None
    home = _hermes_home()
    db = home / "state.db" if profile == "default" else home / "profiles" / profile / "state.db"
    if not db.is_file():
        return None
    limit = max(1, min(5000, limit))
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        srow = c.execute("SELECT started_at, model, title, last_activity_at, ended_at"
                         " FROM sessions WHERE id = ?", (sid,)).fetchone()
        if not srow:
            return None
        rows = c.execute(
            "SELECT role, content, tool_name, tool_calls, reasoning, timestamp"
            " FROM messages WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT ?",
            (sid, limit)).fetchall()
        rows.reverse()
        lines: list[str] = []
        for role, content, tool_name, tool_calls, reasoning, ts in rows:
            t = _fmt_hms(ts)
            if role == "user":
                txt = str(content or "").strip()
                if txt:
                    lines.append(f"{t} user | {txt[:4000]}")
            elif role == "assistant":
                if reasoning and str(reasoning).strip():
                    lines.append(f"{t} think | {str(reasoning)[:2000]}")
                try:
                    calls = json.loads(tool_calls) if tool_calls else []
                except ValueError:
                    calls = []
                if content and str(content).strip() and not calls:
                    lines.append(f"{t} user | {str(content)[:4000]}")
                for tc in calls or []:
                    name = tc.get("name") or (tc.get("function") or {}).get("name") or "tool"
                    args = tc.get("args", (tc.get("function") or {}).get("arguments")
                                        or tc.get("arguments") or "")
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    lines.append(f"{t} tool | -> {name}({str(args)[:400]})")
            elif role == "tool":
                ok = not re.search(r"error|fail|exception", str(content or "")[:200], re.I)
                lines.append(f"{t} result | {tool_name or 'tool'} {'ok' if ok else 'error'}"
                             f" 0.0s: {str(content or '')[:1400]}")
        return {"id": sid, "profile": profile,
                "agent": "agent" if profile == "default" else profile,
                "model": srow[1], "title": srow[2], "started": srow[0],
                "last_activity": srow[3], "lines": lines, "ts": time.time()}
    except sqlite3.Error:
        return None
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass

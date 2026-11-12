"""DSH / DS Bot session reader — read-only, never writes to the DSH dir.

Data dir: $DSH_HOME (default ~/.dsh), measured from live files 2026-11-09:
  sessions/<sanitized-cwd>/<session-id>/session.v4.jsonl[.zstd]  transcripts
      records: session{id,cwd,createdAt}, session/title, model/selection,
      user/message, assistant/message (reasoning/text/tool-call blocks +
      usage), tool/call{name,arguments,callId}, tool/result, turn/*, ...
      times are epoch MILLISECONDS.
  bot/usage.sqlite  ledger: owners(session,owner,name) folds a bot's handoff
      PARTS (separate session ids) onto one canonical session + display name
      — this table is live and is the only part we read. The calls table's
      kind='turn' rows STOPPED being written on 2026-11-08 ~19:49 on this
      box (journal: "usage could not record a call: database or disk is
      full"; marks advance but fold emits no rows) — a ds-bot bug, so turn
      tokens come from the transcripts instead, not the ledger.
  bot/state.json    bots[sid].name (display-name fallback).

Chart tokens = the LAST assistant/message usage block of the newest hot part
(input + cacheRead + cacheWrite = the real current context the model sees).
Sessions with no usage block never completed a model call, context ~ 0.
Hot/active/live come from the transcript's file mtime (it moves as the
session works). Tail reads are cached by (path, mtime_ns, size): a sampler
tick only re-reads files that actually changed.

Transcript reads are bounded: head slice for metadata (titles live at the
head), tail slice for content — zstd runs as a subprocess whose decompressed
output is capped in BYTES and whose wait is bounded by select(), so neither
a dict-heavy stream (repetitive content expands thousands-fold) nor a hung
pipe can eat the box's RAM or stall a sampler tick.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

CC_LIVE_H = 6          # hot window, same as clients.CC_LIVE_H
CC_ACTIVE_MIN = 5      # "active" legend window, same as clients.CC_ACTIVE_MIN
SLIVE_S = 120          # "live" pill window, same as swatch.SLIVE_S

TAIL_KEEP = 8 * 2**20          # keep only the last 8 MiB decompressed
USAGE_TAIL = 2 * 2**20         # tail window for the usage-block scan
DECOMPRESS_CAP = 64 * 2**20    # kill the decompress past 64 MiB out
ZSTD_TIMEOUT_S = 10
HEAD_BYTES = 64 * 2**10        # compressed head slice for metadata
HEAD_DECOMPRESS_CAP = 8 * 2**20  # stop reading the head past 8 MiB out

TRANSCRIPT_NAMES = ("session.v4.jsonl.zstd", "session.v4.jsonl")
_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_META_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
# path -> (mtime_ns, size) -> (tokens, user_messages), or None when the tail
# is unreadable/sparse; None is cached too so a corrupt file costs one
# bounded read per stamp, not one per sampler tick
_TAIL_CACHE: dict[str, tuple[tuple[int, int], tuple[int, int] | None]] = {}


def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def _zstd_bin() -> str | None:
    return shutil.which("zstd") or (
        "/usr/bin/zstd" if Path("/usr/bin/zstd").exists() else None)


# ---------------- bounded transcript reads ----------------

def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.stdout.close()
    except OSError:
        pass
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _feed_stdin(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
    except OSError:                 # zstd died early (bad frame, killed)
        pass
    try:
        pipe.close()
    except OSError:
        pass


def _bounded_chunks(proc: subprocess.Popen, timeout: float, state: dict):
    """Yield stdout chunks until EOF or until `timeout` seconds pass.
    select() bounds every wait, so a pipe that stops delivering cannot
    stall the caller past the deadline (a plain blocking read() would);
    read1() returns as soon as any data is available instead of waiting
    to fill the buffer. state["timed_out"] records which end happened."""
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not sel.select(left):
                state["timed_out"] = True
                return
            chunk = proc.stdout.read1(65536)
            if not chunk:
                return
            yield chunk


def read_jsonl_tail(path: Path, keep: int = TAIL_KEEP,
                    cap: int = DECOMPRESS_CAP,
                    timeout: float = ZSTD_TIMEOUT_S) -> bytes | None:
    """Last `keep` bytes of the (possibly zstd) transcript, or None.

    Plain files: seek straight to the tail. zstd: subprocess whose output
    is ring-buffered to `keep` bytes and killed at `cap` decompressed bytes
    or `timeout` seconds — never drained whole, never left running. The
    deadline is enforced by select(), so a hung pipe returns None at the
    deadline instead of stalling until its next chunk arrives.
    """
    try:
        if path.name.endswith(".zstd"):
            z = _zstd_bin()
            if not z:
                return None
            proc = subprocess.Popen([z, "-dc", str(path)],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            buf: deque[bytes] = deque()
            kept = total = 0
            state: dict = {}
            gen = _bounded_chunks(proc, timeout, state)
            try:
                for chunk in gen:
                    total += len(chunk)
                    buf.append(chunk)
                    kept += len(chunk)
                    while kept > keep + 65536 and len(buf) > 1:
                        kept -= len(buf.popleft())
                    if total >= cap:
                        break                     # runaway: stop at the cap
            finally:
                gen.close()
                _reap(proc)
            if state.get("timed_out"):
                return None
            data = b"".join(buf)
        else:
            size = path.stat().st_size
            with open(path, "rb") as f:
                if size > keep:
                    f.seek(size - keep)
                data = f.read()
    except OSError:
        return None
    return data[-keep:] if len(data) > keep else data


def read_jsonl_head(path: Path, nbytes: int = HEAD_BYTES,
                    cap: int = HEAD_DECOMPRESS_CAP,
                    timeout: float = 5.0) -> bytes:
    """First bytes of the decompressed stream, from a compressed HEAD SLICE
    only — metadata records sit at the head, so the chart never decompresses
    whole files. A truncated frame exits non-zero after emitting what it
    decoded; that partial output is exactly what we want. The decompressed
    output is capped at `cap` bytes: a timeout bounds time, not volume, and
    a dict-heavy slice can expand thousands-fold past what we will parse."""
    z = _zstd_bin()
    try:
        with open(path, "rb") as f:
            head = f.read(nbytes)
        if not path.name.endswith(".zstd"):
            return head
        if not z:
            return b""
        proc = subprocess.Popen([z, "-dc"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        # feed the slice from a thread: a small pipe must not deadlock a
        # writer that is simultaneously waiting on the reader side
        feeder = threading.Thread(target=_feed_stdin,
                                  args=(proc.stdin, head), daemon=True)
        feeder.start()
        out = bytearray()
        gen = _bounded_chunks(proc, timeout, {})
        try:
            for chunk in gen:
                out += chunk
                if len(out) >= cap:      # runaway expansion: stop at the cap
                    break
        finally:
            gen.close()
            _reap(proc)
            feeder.join(timeout=1.0)
        return bytes(out[:cap])
    except (OSError, subprocess.SubprocessError):
        return b""


def _records(data: bytes) -> list[dict]:
    out = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:          # partial head/tail edge line
            continue
        if isinstance(r, dict) and isinstance(r.get("type"), str):
            out.append(r)
    return out


# ---------------- session discovery + metadata ----------------

def transcript_path(d: Path) -> Path | None:
    for name in TRANSCRIPT_NAMES:
        p = d / name
        if p.is_file():
            return p
    return None


def session_dirs(root: Path) -> list[Path]:
    base = root / "sessions"
    out = []
    try:
        for cwd_dir in sorted(base.iterdir()):
            for d in sorted(cwd_dir.iterdir()):
                if d.is_dir() and transcript_path(d):
                    out.append(d)
    except OSError:
        pass
    return out


def session_meta(d: Path) -> dict:
    """{id, cwd, created_at, title, model} from the head slice, cached by
    (path, mtime_ns, size) so a 10 s sampler tick costs one stat per session."""
    p = transcript_path(d) or d
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (0, 0)
    hit = _META_CACHE.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    meta = {"id": d.name, "cwd": None, "created_at": None,
            "title": None, "model": None}
    for r in _records(read_jsonl_head(p)):
        t = r["type"]
        if t == "session":
            meta["id"] = r.get("id") or meta["id"]
            meta["cwd"] = r.get("cwd")
            cta = r.get("createdAt")
            meta["created_at"] = cta / 1000 if isinstance(cta, (int, float)) else None
        elif t == "session/title":
            meta["title"] = (r.get("data") or {}).get("title") or meta["title"]
        elif t == "model/selection":
            meta["model"] = (r.get("data") or {}).get("model") or meta["model"]
    _META_CACHE[str(p)] = (stamp, meta)
    return meta


def _tail_stats(d: Path) -> tuple[int, int] | None:
    """(tokens, user_messages) from the tail window, cached by file stamp.

    tokens = last assistant/message usage block: input + cacheRead +
    cacheWrite — the real context of the newest step. user_messages counts
    user/message records IN THE TAIL WINDOW ONLY — an approximation the UI
    labels honestly; a session longer than the window shows fewer.
    None = the tail is unreadable or sparse (read failed, or it decoded to
    nothing parsable): the card flags it instead of posing as an empty
    session. Cached by stamp like a good read.
    """
    p = transcript_path(d)
    if p is None:
        return None
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    hit = _TAIL_CACHE.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    data = read_jsonl_tail(p, keep=USAGE_TAIL)
    if data is None:
        _TAIL_CACHE[str(p)] = (stamp, None)
        return None
    nl = data.find(b"\n")
    if nl >= 0:                 # the tail may start mid-line
        data = data[nl + 1:]
    recs = _records(data)
    if not recs:
        # empty or garbage tail: no record parsed — flag, don't fake empty
        _TAIL_CACHE[str(p)] = (stamp, None)
        return None
    tokens, msgs = 0, 0
    for r in recs:
        t = r["type"]
        if t == "user/message":
            msgs += 1
        elif t == "assistant/message":
            u = (r.get("data") or {}).get("usage") or {}
            if u:
                tokens = ((u.get("inputTokens") or 0)
                          + (u.get("cacheReadTokens") or 0)
                          + (u.get("cacheWriteTokens") or 0))
    out = (tokens, msgs)
    _TAIL_CACHE[str(p)] = (stamp, out)
    return out


def _folder_name(meta: dict, sid: str) -> str:
    cwd = meta.get("cwd") or ""
    return Path(cwd).name if cwd else sid


# ---------------- ledger: owners + names only ----------------

def _ledger(root: Path) -> sqlite3.Connection | None:
    db = root / "bot" / "usage.sqlite"
    if not db.is_file():
        return None
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _owner_map(c: sqlite3.Connection | None) -> dict[str, tuple[str, str]]:
    """sid -> (canonical sid, bot name): handoff parts fold onto the owner."""
    if c is None:
        return {}
    try:
        return {r["session"]: (r["owner"], r["name"])
                for r in c.execute("SELECT session, owner, name FROM owners")}
    except sqlite3.Error:
        return {}


def _state_names(root: Path) -> dict[str, str]:
    """bots[sid].name from state.json — display-name fallback."""
    try:
        st = json.loads((root / "bot" / "state.json").read_text(encoding="utf-8"))
        return {sid: (b or {}).get("name") or ""
                for sid, b in (st.get("bots") or {}).items()}
    except (OSError, ValueError):
        return {}


def _display(root: Path, sid: str, owners: dict, state_names: dict,
             meta: dict) -> str:
    name = owners.get(sid, (None, None))[1]
    return name or state_names.get(sid) or meta.get("title") \
        or _folder_name(meta, sid)


# ---------------- per-client chart ----------------

def chart_samples(root: Path | None = None, now: float | None = None) -> list[dict]:
    """Rows shaped exactly like clients.samples_now(): {client,title,tokens,
    messages,active}. Only HOT sessions (transcript mtime within 6 h). A bot's
    handoff parts fold into one row keyed by the canonical session; the row
    carries the NEWEST part's tokens — that part holds the live context."""
    root = root or dsh_home()
    now = now if now is not None else time.time()
    c = _ledger(root)
    owners = _owner_map(c)
    if c is not None:
        try:
            c.close()
        except sqlite3.Error:
            pass
    state_names = _state_names(root)
    act_cut = now - CC_ACTIVE_MIN * 60
    by_owner: dict[str, dict] = {}
    for d in session_dirs(root):
        p = transcript_path(d)
        try:
            m = p.stat().st_mtime          # type: ignore[union-attr]
        except (OSError, AttributeError):
            continue
        if not 0 <= now - m <= CC_LIVE_H * 3600:
            continue
        u = _tail_stats(d)
        if not u or u[0] <= 0:
            continue                       # no completed model call yet
        sid = d.name
        canon, name = owners.get(sid, (sid, None))
        g = by_owner.setdefault(canon, {"mtime": -1.0, "tokens": 0,
                                        "msgs": 0, "name": None, "dir": d})
        g["name"] = g["name"] or name
        if m >= g["mtime"]:               # newest part carries the context
            g.update(mtime=m, tokens=u[0], msgs=u[1], dir=d)
    out = []
    for canon, g in by_owner.items():
        if g["name"]:
            title = g["name"]
        else:
            meta = session_meta(g["dir"])
            title = state_names.get(canon) or meta.get("title") \
                or _folder_name(meta, canon)
        out.append({"client": canon, "title": title, "tokens": g["tokens"],
                    "messages": g["msgs"], "active": g["mtime"] >= act_cut})
    return out


# ---------------- Traces browser ----------------

def trace_sessions(root: Path | None = None, now: float | None = None) -> list[dict]:
    """Session cards in swatch.main_sessions()'s shape, profile dsb|dsh.
    last_activity/live read the transcript mtime (it moves as the session
    works); msg_count is the tail-window user-message count (approximate —
    a session longer than the 2 MiB tail window shows fewer). degraded=True
    marks a transcript that could not be read or parsed (corrupt/sparse):
    the card must not pose as a real empty session."""
    root = root or dsh_home()
    now = now if now is not None else time.time()
    c = _ledger(root)
    owners = _owner_map(c)
    if c is not None:
        try:
            c.close()
        except sqlite3.Error:
            pass
    state_names = _state_names(root)
    out = []
    for d in session_dirs(root):
        sid = d.name
        meta = session_meta(d)
        p = transcript_path(d)
        try:
            last = p.stat().st_mtime       # type: ignore[union-attr]
        except (OSError, AttributeError):
            last = 0
        u = _tail_stats(d)
        out.append({"id": sid, "profile": "dsb" if sid in owners else "dsh",
                    "source": "dsh", "title": meta.get("title") or None,
                    "display": _display(root, sid, owners, state_names, meta),
                    "model": meta.get("model"),
                    "msg_count": u[1] if u else 0,
                    "degraded": u is None,
                    "started_at": meta.get("created_at"), "ended_at": None,
                    "last_activity": last,
                    "last_activity_s": round(now - last) if last else None,
                    "last_activity_desc": None,
                    "live": bool(last and now - last <= SLIVE_S)})
    return out


def _fmt_hms(ms) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ms) / 1000))
    except (TypeError, ValueError):
        return "--:--:--"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text") or ""))
    return "\n".join(parts)


def trace_transcript(root: Path | None = None, sid: str = "",
                     limit: int = 200) -> dict | None:
    """DSH session transcript in the trace UI's line vocabulary:
    "HH:MM:SS user|think|tool|result|final | body", newest `limit` lines."""
    if not _SID_RE.match(sid or ""):
        return None
    root = root or dsh_home()
    d = next((x for x in session_dirs(root) if x.name == sid), None)
    if d is None:
        return None
    p = transcript_path(d)
    meta = session_meta(d)
    c = _ledger(root)
    owners = _owner_map(c)
    if c is not None:
        try:
            c.close()
        except sqlite3.Error:
            pass
    display = _display(root, sid, owners, _state_names(root), meta)
    data = read_jsonl_tail(p)
    if data is None:
        return None
    nl = data.find(b"\n")
    if nl >= 0:
        data = data[nl + 1:]          # tail may start mid-line
    names: dict[str, str] = {}
    lines: list[str] = []
    for r in _records(data):
        t = r["type"]
        ts = _fmt_hms(r.get("time"))
        dta = r.get("data") or {}
        if t == "user/message":
            txt = _text_of(dta.get("content")).strip()
            if txt:
                lines.append(f"{ts} user | {txt[:4000]}")
        elif t == "assistant/message":
            for b in (dta.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "reasoning" and str(b.get("text") or "").strip():
                    lines.append(f"{ts} think | {str(b['text'])[:2000]}")
                elif b.get("type") == "text" and str(b.get("text") or "").strip():
                    lines.append(f"{ts} final | {str(b['text'])[:4000]}")
        elif t == "tool/call":
            cid = dta.get("callId")
            nm = dta.get("name") or "tool"
            if cid:
                names[cid] = nm
            args = dta.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args)
            lines.append(f"{ts} tool | -> {nm}({str(args)[:400]})")
        elif t == "tool/result":
            msg = dta.get("message") or {}
            cid = msg.get("toolCallId")
            nm = names.get(cid, "tool")
            body = _text_of(msg.get("content"))
            ok = not re.search(r"error|fail|exception", body[:200], re.I)
            lines.append(f"{ts} result | {nm} {'ok' if ok else 'error'}"
                         f" 0.0s: {body[:1400]}")
    limit = max(1, min(5000, int(limit)))
    try:
        last = p.stat().st_mtime if p else None    # type: ignore[union-attr]
    except OSError:
        last = None
    return {"id": sid, "profile": "dsb" if sid in owners else "dsh",
            "agent": display, "model": meta.get("model"),
            "title": meta.get("title"), "started": meta.get("created_at"),
            "last_activity": last, "lines": lines[-limit:], "ts": time.time()}
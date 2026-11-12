"""DSH/DSB session sources: per-client chart rows + Traces browser entries.

Fixtures build a fake DSH data dir under tmp_path (session dirs + usage
sqlite ledger + state.json) — never reads the live ~/.dsh. Times are
injected; the ledger stores epoch milliseconds like the real one.
"""
import sys
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from app import dsh_home

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="DSH session sources: POSIX paths and the zstd CLI")

ZSTD = shutil.which("zstd") or ("/usr/bin/zstd" if Path("/usr/bin/zstd").exists() else None)
HAS_ZSTD = ZSTD is not None

NOW = 1_800_000_000.0          # seconds
MS = 1000


def _records(sid, cwd="/home/user/projects/demo", title=None, model="qwen-x",
             turns=(), end=False):
    """A minimal but schema-faithful transcript record list."""
    t0 = int(NOW * MS)
    recs = [{"type": "session", "version": 4, "id": sid, "createdAt": t0,
             "cwd": cwd, "isSeeded": False},
            {"type": "model/selection", "seq": 1, "time": t0,
             "data": {"provider": "vllm", "model": model}}]
    seq = 2
    if title is not None:
        recs.append({"type": "session/title", "seq": seq, "time": t0,
                     "data": {"title": title}})
        seq += 1
    for i, (kind, usage) in enumerate(turns):
        if kind == "user":
            recs.append({"type": "user/message", "seq": seq, "time": t0 + i,
                         "data": {"content": [{"type": "text", "text": "hello"}]}})
        elif kind == "think":
            recs.append({"type": "assistant/message", "seq": seq, "time": t0 + i,
                         "data": {"turn": 1, "step": 1,
                                  "message": {"role": "assistant", "content": [
                                      {"type": "reasoning", "text": "pondering"}]},
                                  "usage": usage or {}}})
        elif kind == "call":
            recs.append({"type": "tool/call", "seq": seq, "time": t0 + i,
                         "data": {"callId": "c1", "name": "bash",
                                  "arguments": "{\"command\":\"ls\"}"}})
        elif kind == "result":
            recs.append({"type": "tool/result", "seq": seq, "time": t0 + i,
                         "data": {"message": {"role": "tool",
                                              "toolCallId": "c1",
                                              "content": [{"type": "text",
                                                           "text": "file1\nfile2"}]}}})
        elif kind == "final":
            recs.append({"type": "assistant/message", "seq": seq, "time": t0 + i,
                         "data": {"turn": 1, "step": 2,
                                  "message": {"role": "assistant", "content": [
                                      {"type": "text", "text": "done here"}]},
                                  "usage": usage or {}}})
        seq += 1
    if end:
        recs.append({"type": "session/end-seed", "seq": seq, "time": t0, "data": {}})
    return recs


def _write_transcript(root: Path, cwd_slug: str, sid: str, recs, zstd=True):
    d = root / "sessions" / cwd_slug / sid
    d.mkdir(parents=True)
    raw = "\n".join(json.dumps(r) for r in recs) + "\n"
    if zstd and HAS_ZSTD:
        name = "session.v4.jsonl.zstd"
        subprocess.run([ZSTD, "-q", "-c"], input=raw.encode(),
                       stdout=open(d / name, "wb"), check=True)
    else:
        name = "session.v4.jsonl"
        (d / name).write_text(raw, encoding="utf-8")
    return d / name


def _ledger(root: Path, calls, owners=()):
    """calls: (session, kind, time_s, input, cache_read, cache_write, output)"""
    db = root / "bot" / "usage.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE calls (key TEXT, session TEXT, time INTEGER, "
              "provider TEXT, model TEXT, kind TEXT, live INTEGER, input INTEGER, "
              "cache_read INTEGER, cache_write INTEGER, output INTEGER)")
    c.execute("CREATE TABLE owners (session TEXT, owner TEXT, name TEXT)")
    for i, (sid, kind, ts, inp, cr, cw, outp) in enumerate(calls):
        c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (f"k{i}", sid, int(ts * MS), "vllm", "qwen-x", kind, 1,
                   inp, cr, cw, outp))
    for sid, owner, name in owners:
        c.execute("INSERT INTO owners VALUES (?,?,?)", (sid, owner, name))
    c.commit()
    c.close()
    return db


def _state(root: Path, bots):
    (root / "bot" / "state.json").write_text(
        json.dumps({"bots": {sid: {"name": n} for sid, n in bots.items()}}),
        encoding="utf-8")


def _stamp(path: Path, ts: float):
    """Set a transcript's mtime so hot/active windows see injected time."""
    import os
    os.utime(path, (ts, ts))


# ---------------- per-client chart ----------------
# Source: transcript tail usage blocks (the ledger's turn rows stopped being
# written on this box 2026-11-08 — see app/dsh_home.py docstring); the ledger
# still provides the owners folding.

def test_chart_folds_bot_parts_into_one_owner(tmp_path):
    """Two parts of one bot conversation = ONE row, tokens from the NEWEST
    part (that part carries the live context)."""
    usage_old = {"inputTokens": 1000, "cacheReadTokens": 50_000, "outputTokens": 9}
    usage_new = {"inputTokens": 2000, "cacheReadTokens": 120_000,
                 "cacheWriteTokens": 512, "outputTokens": 20}
    p1 = _write_transcript(tmp_path, "--ws--", "session-part1",
                           _records("session-part1", turns=[("think", usage_old)]))
    p2 = _write_transcript(tmp_path, "--ws--", "session-part2",
                           _records("session-part2", turns=[("think", usage_new)]))
    _stamp(p1, NOW - 7200)
    _stamp(p2, NOW - 600)
    _ledger(tmp_path, [], owners=[("session-part1", "session-part1", "Chief"),
                                  ("session-part2", "session-part1", "Chief")])
    rows = dsh_home.chart_samples(root=tmp_path, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["client"] == "session-part1"
    assert r["title"] == "Chief"
    assert r["tokens"] == 2000 + 120_000 + 512
    assert r["active"] is False                    # newest part 10 min ago


def test_chart_active_within_five_minutes(tmp_path):
    usage = {"inputTokens": 10, "cacheReadTokens": 1000}
    p = _write_transcript(tmp_path, "--ws--", "session-x",
                          _records("session-x", turns=[("think", usage)]))
    _stamp(p, NOW - 60)
    _ledger(tmp_path, [], owners=[("session-x", "session-x", "Builder")])
    rows = dsh_home.chart_samples(root=tmp_path, now=NOW)
    assert rows and rows[0]["active"] is True


def test_chart_skips_sessions_without_usage_block(tmp_path):
    """A session that never completed a model call has context ~ 0: skip."""
    p0 = _write_transcript(tmp_path, "--ws--", "session-empty",
                           _records("session-empty"))            # no usage
    p1 = _write_transcript(tmp_path, "--ws--", "session-live",
                           _records("session-live", turns=[
                               ("think", {"inputTokens": 10, "cacheReadTokens": 1000})]))
    _stamp(p0, NOW - 60)
    _stamp(p1, NOW - 60)
    rows = dsh_home.chart_samples(root=tmp_path, now=NOW)
    assert [r["client"] for r in rows] == ["session-live"]


def test_chart_skips_cold_sessions(tmp_path):
    usage = {"inputTokens": 10, "cacheReadTokens": 1000}
    p = _write_transcript(tmp_path, "--ws--", "session-old",
                          _records("session-old", turns=[("think", usage)]))
    _stamp(p, NOW - 8 * 3600)                       # outside the 6 h window
    assert dsh_home.chart_samples(root=tmp_path, now=NOW) == []


def test_chart_plain_session_named_by_title_then_folder(tmp_path):
    usage = {"inputTokens": 1, "cacheReadTokens": 500}
    p1 = _write_transcript(tmp_path, "--home-a-notes--", "session-p1",
                           _records("session-p1", cwd="/home/user/notes",
                                    title="Trip planning", turns=[("think", usage)]))
    p2 = _write_transcript(tmp_path, "--home-a-raw--", "session-p2",
                           _records("session-p2", cwd="/home/user/raw",
                                    turns=[("think", usage)]))   # no title
    _stamp(p1, NOW - 60)
    _stamp(p2, NOW - 60)
    rows = {r["client"]: r for r in dsh_home.chart_samples(root=tmp_path, now=NOW)}
    assert rows["session-p1"]["title"] == "Trip planning"
    assert rows["session-p2"]["title"] == "raw"     # cwd folder name


def test_chart_needs_no_ledger_at_all(tmp_path):
    """Owners folding needs the ledger, but tokens do not: with no
    usage.sqlite the chart still shows hot plain sessions."""
    usage = {"inputTokens": 300, "cacheReadTokens": 9000, "outputTokens": 40}
    p = _write_transcript(tmp_path, "--ws--", "session-hot",
                          _records("session-hot", turns=[("think", usage),
                                                         ("final", usage)]))
    _stamp(p, NOW - 120)
    rows = dsh_home.chart_samples(root=tmp_path, now=NOW)
    assert len(rows) == 1
    assert rows[0]["tokens"] == 300 + 9000          # no cacheWrite key
    assert rows[0]["active"] is True


def test_chart_nothing_on_disk_is_empty(tmp_path):
    assert dsh_home.chart_samples(root=tmp_path / "nothing", now=NOW) == []


def test_chart_ignores_ledger_calls_even_if_they_revive(tmp_path):
    """The calls table is NOT a value source (its turn rows went stale on
    2026-11-08). If ds-bot's recorder revives, the chart must not pick the
    rows up, let alone double-count: tokens come from the transcript only."""
    usage = {"inputTokens": 300, "cacheReadTokens": 9000}
    p = _write_transcript(tmp_path, "--ws--", "session-hot",
                          _records("session-hot", turns=[("think", usage)]))
    _stamp(p, NOW - 60)
    _ledger(tmp_path, [("session-hot", "turn", NOW - 30, 999_999, 888_888,
                        777_777, 1)],
            owners=[("session-hot", "session-hot", "Chief")])
    rows = dsh_home.chart_samples(root=tmp_path, now=NOW)
    assert len(rows) == 1
    assert rows[0]["tokens"] == 300 + 9000          # never 999999+888888+777777
    assert rows[0]["title"] == "Chief"              # owners still folded in


def test_tail_msg_count_is_the_window_count_and_tokens_are_the_last_block(tmp_path):
    """Defined semantics, not accident: msg_count counts user messages IN the
    2 MiB tail window only (older ones fall out of the window), and tokens
    are the LAST usage block, not a sum of blocks."""
    usage_a = {"inputTokens": 100, "cacheReadTokens": 1000}
    usage_b = {"inputTokens": 700, "cacheReadTokens": 7000}
    bulk = [{"type": "system/message", "seq": 100 + i, "time": int(NOW * MS),
             "data": {"message": {"role": "system", "content": [
                 {"type": "text", "text": "z" * 50_000}]}}} for i in range(70)]
    recs = ([{"type": "user/message", "seq": 1, "time": int(NOW * MS),
              "data": {"content": [{"type": "text", "text": "old question"}]}}]
            + bulk
            + _records("session-long", turns=[("user", None),
                                              ("think", usage_a),
                                              ("think", usage_b)])[2:])
    p = _write_transcript(tmp_path, "--ws--", "session-long", recs)
    _stamp(p, NOW - 60)
    rows = dsh_home.chart_samples(root=tmp_path, now=NOW)
    assert len(rows) == 1
    assert rows[0]["tokens"] == 700 + 7000          # last block, not 800+8000
    assert rows[0]["messages"] == 1                 # the pre-bulk message fell
    # out of the window; the in-window user/message is the one that counts


# ---------------- Traces browser ----------------

def _traces_root(tmp_path):
    p1 = _write_transcript(tmp_path, "--ws--", "session-bot",
                           _records("session-bot", title="Bot chat", turns=[
                               ("user", None), ("think", None), ("call", None),
                               ("result", None), ("final", None)]))
    p2 = _write_transcript(tmp_path, "--home-a-plain--", "session-plain",
                           _records("session-plain", cwd="/home/user/plain",
                                    title="Plain work"))
    _stamp(p1, NOW - 30)
    _stamp(p2, NOW - 86400)
    _ledger(tmp_path, [], owners=[("session-bot", "session-bot", "Chief")])
    return tmp_path


def test_trace_sessions_label_dsb_and_dsh(tmp_path):
    _traces_root(tmp_path)
    rows = {r["id"]: r for r in dsh_home.trace_sessions(root=tmp_path, now=NOW)}
    bot, plain = rows["session-bot"], rows["session-plain"]
    assert bot["profile"] == "dsb" and bot["display"] == "Chief"
    assert bot["live"] is True and bot["msg_count"] == 1     # 1 user/message
    assert bot["model"] == "qwen-x"
    assert plain["profile"] == "dsh" and plain["display"] == "Plain work"
    assert plain["live"] is False


def test_trace_transcript_uses_ui_line_vocabulary(tmp_path):
    import re
    _traces_root(tmp_path)
    d = dsh_home.trace_transcript(root=tmp_path, sid="session-bot", limit=200)
    assert d is not None and d["profile"] == "dsb" and d["agent"] == "Chief"
    line_re = re.compile(r"^\d{2}:\d{2}:\d{2} (user|think|tool|result|final) \| ")
    kinds = [line_re.match(l).group(1) for l in d["lines"]]
    assert kinds == ["user", "think", "tool", "result", "final"]


def test_trace_transcript_result_names_tool_via_call_id(tmp_path):
    _traces_root(tmp_path)
    d = dsh_home.trace_transcript(root=tmp_path, sid="session-bot", limit=200)
    res = [l for l in d["lines"] if "| result" in l or "result |" in l]
    assert res and "bash" in res[0] and "ok" in res[0]
    tool = [l for l in d["lines"] if "tool |" in l]
    assert tool and "bash(" in tool[0]


def test_trace_transcript_limit_keeps_the_tail(tmp_path):
    _traces_root(tmp_path)
    d = dsh_home.trace_transcript(root=tmp_path, sid="session-bot", limit=2)
    assert len(d["lines"]) == 2
    assert "done here" in d["lines"][-1]


def test_trace_transcript_unknown_sid_is_none(tmp_path):
    _traces_root(tmp_path)
    assert dsh_home.trace_transcript(root=tmp_path, sid="session-nope") is None


# ---------------- zstd guard ----------------

@pytest.mark.skipif(not HAS_ZSTD, reason="zstd binary not installed")
def test_zstd_tail_keep_bounds_the_read(tmp_path):
    """A big transcript: only the last `keep` decompressed bytes come back,
    and the decompress process is killed, not left running."""
    sid = "session-big"
    recs = _records(sid)
    bulk = [{"type": "system/message", "seq": 100 + i, "time": int(NOW * MS),
             "data": {"message": {"role": "system", "content": [
                 {"type": "text", "text": "x" * 50_000}]}}} for i in range(400)]
    _write_transcript(tmp_path, "--ws--", sid, recs + bulk)
    path = tmp_path / "sessions" / "--ws--" / sid / (
        "session.v4.jsonl.zstd" if HAS_ZSTD else "session.v4.jsonl")
    data = dsh_home.read_jsonl_tail(path, keep=4096, cap=64 * 2**20, timeout=10)
    assert data is not None and len(data) <= 4096
    assert data.endswith(b"\n")


@pytest.mark.skipif(not HAS_ZSTD, reason="zstd binary not installed")
def test_zstd_cap_kills_a_runaway_decompress(tmp_path):
    sid = "session-huge"
    bulk = [{"type": "system/message", "seq": i, "time": int(NOW * MS),
             "data": {"message": {"role": "system", "content": [
                 {"type": "text", "text": "x" * 200_000}]}}} for i in range(600)]
    _write_transcript(tmp_path, "--ws--", sid, _records(sid) + bulk)
    path = tmp_path / "sessions" / "--ws--" / sid / "session.v4.jsonl.zstd"
    t0 = time.monotonic()
    data = dsh_home.read_jsonl_tail(path, keep=1024, cap=2 * 2**20, timeout=10)
    assert time.monotonic() - t0 < 9.5          # killed at the cap, not drained
    assert data is not None and len(data) <= 1024


def test_head_slice_reads_title_without_full_decompress(tmp_path):
    """Titles/models live at the head of the transcript; a head slice read
    must find them (the chart must not decompress whole files every tick)."""
    sid = "session-head"
    bulk = [{"type": "system/message", "seq": 100 + i, "time": int(NOW * MS),
             "data": {"message": {"role": "system", "content": [
                 {"type": "text", "text": "y" * 50_000}]}}} for i in range(200)]
    p = _write_transcript(tmp_path, "--ws--", sid,
                          _records(sid, title="Headed", turns=[]) + bulk)
    meta = dsh_home.session_meta(p.parent)
    assert meta["title"] == "Headed"
    assert meta["cwd"] == "/home/user/projects/demo"
    assert meta["model"] == "qwen-x"


# ---------------- bounded reads: volume cap + enforced deadline ----------
# Audit follow-ups 2026-11-10: a timeout bounds time, not RAM (a dict-heavy
# 64 KiB slice can expand thousands-fold), and a deadline checked only after
# a blocking read() returns cannot fire on a hung pipe.

@pytest.mark.skipif(not HAS_ZSTD, reason="zstd binary not installed")
def test_head_volume_cap_bounds_a_runaway_expansion(tmp_path, monkeypatch):
    """16 MiB of zeros compresses far below the 64 KiB head slice; the head
    read must stop consuming at HEAD_DECOMPRESS_CAP, not buffer the whole
    16 MiB. The returned length cannot pin the early stop — the `[:cap]`
    slice masks a missing break — and the child's exit code cannot either:
    a zstd drained to EOF is often still in its exit window when _reap
    polls, so it gets killed anyway (proven by mutation 2026-11-10). The
    real guard is bytes CONSUMED from the pipe: count them at the source
    and require cap + one chunk at most. Removing only the break makes
    this FAIL deterministically (consumed = 16 MiB)."""
    d = tmp_path / "sessions" / "--ws--" / "session-dict-heavy"
    d.mkdir(parents=True)
    comp = subprocess.run([ZSTD, "-q", "-c"], input=b"\x00" * (16 * 2**20),
                          stdout=subprocess.PIPE, check=True).stdout
    assert len(comp) < dsh_home.HEAD_BYTES      # whole frame inside the slice
    path = d / "session.v4.jsonl.zstd"
    path.write_bytes(comp)
    started = []
    counters = []
    real_popen = subprocess.Popen

    class Counting:
        """Pipe wrapper: counts bytes handed to the reader; delegates
        everything else (fileno for the selector, close for _reap)."""

        def __init__(self, real):
            self._real = real
            self.n = 0

        def read1(self, size=-1):
            chunk = self._real.read1(size)
            self.n += len(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(self._real, name)

    def spy(*a, **k):
        p = real_popen(*a, **k)
        started.append(p)
        counter = Counting(p.stdout)
        counters.append(counter)
        p.stdout = counter
        return p

    monkeypatch.setattr(dsh_home.subprocess, "Popen", spy)
    out = dsh_home.read_jsonl_head(path)
    assert len(out) == dsh_home.HEAD_DECOMPRESS_CAP   # capped, not 16 MiB
    # the guard's real promise: never consume more than cap + one chunk
    assert counters and counters[0].n <= dsh_home.HEAD_DECOMPRESS_CAP + 65536
    assert started and started[0].returncode is not None   # reaped, not orphaned
    assert started[0].returncode != 0                  # killed at the cap, not drained


@pytest.mark.skipif(not HAS_ZSTD, reason="zstd binary not installed")
def test_tail_volume_cap_bounds_a_runaway_expansion(tmp_path, monkeypatch):
    """The same unpinned-break class as the head pin, on the tail side
    (LIMCARS proved the mutation 2026-11-10): removing `if total >= cap:
    break` leaves the suite green because the returned length is ring-
    bounded by `keep`, masking the guard exactly as `[:cap]` did for the
    head. Pin bytes CONSUMED from the pipe: cap + one chunk at most.
    Break-only removal must FAIL deterministically (consumed = 16 MiB)."""
    comp = subprocess.run([ZSTD, "-q", "-c"], input=b"\x00" * (16 * 2**20),
                          stdout=subprocess.PIPE, check=True).stdout
    path = tmp_path / "session.v4.jsonl.zstd"
    path.write_bytes(comp)
    cap = 4 * 2**20
    started = []
    counters = []
    real_popen = subprocess.Popen

    class Counting:
        """Pipe wrapper: counts bytes handed to the reader; delegates
        everything else (fileno for the selector, close for _reap)."""

        def __init__(self, real):
            self._real = real
            self.n = 0

        def read1(self, size=-1):
            chunk = self._real.read1(size)
            self.n += len(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(self._real, name)

    def spy(*a, **k):
        p = real_popen(*a, **k)
        started.append(p)
        counter = Counting(p.stdout)
        counters.append(counter)
        p.stdout = counter
        return p

    monkeypatch.setattr(dsh_home.subprocess, "Popen", spy)
    data = dsh_home.read_jsonl_tail(path, keep=1024, cap=cap)
    assert data is not None and len(data) <= 1024 + 65536   # ring-bounded result
    # the guard's real promise: never consume more than cap + one chunk
    assert counters and counters[0].n <= cap + 65536
    assert started and started[0].returncode is not None    # reaped, not orphaned
    assert started[0].returncode != 0                       # killed at the cap


def _fake_zstd(tmp_path, body):
    fake = tmp_path / "fake-zstd.sh"
    # write_bytes: the shebang's "#" is data, not a comment — a text write
    # would be misjudged by the encoding lint's comment stripper
    fake.write_bytes(("#!/bin/sh\n" + body + "\n").encode("utf-8"))
    fake.chmod(0o755)
    return str(fake)


def test_tail_deadline_bounds_a_hung_pipe(tmp_path, monkeypatch):
    """zstd emits one chunk then hangs: the old loop blocked in read() until
    the next 64 KiB arrived; select() must return None at the deadline."""
    monkeypatch.setattr(dsh_home, "_zstd_bin", lambda: _fake_zstd(
        tmp_path, "printf '{\"type\":\"session\",\"id\":\"x\"}\\n'\nexec sleep 30"))
    path = tmp_path / "session.v4.jsonl.zstd"
    path.write_bytes(b"whatever")
    t0 = time.monotonic()
    assert dsh_home.read_jsonl_tail(path, keep=1024, timeout=0.5) is None
    assert time.monotonic() - t0 < 5            # bounded, not the sleep


def test_tail_deadline_bounds_a_silent_pipe(tmp_path, monkeypatch):
    """zstd never emits anything: same deadline path with zero chunks."""
    monkeypatch.setattr(dsh_home, "_zstd_bin",
                        lambda: _fake_zstd(tmp_path, "exec sleep 30"))
    path = tmp_path / "session.v4.jsonl.zstd"
    path.write_bytes(b"whatever")
    t0 = time.monotonic()
    assert dsh_home.read_jsonl_tail(path, keep=1024, timeout=0.5) is None
    assert time.monotonic() - t0 < 5


def test_head_deadline_bounds_a_hung_decompress(tmp_path, monkeypatch):
    monkeypatch.setattr(dsh_home, "_zstd_bin",
                        lambda: _fake_zstd(tmp_path, "exec sleep 30"))
    path = tmp_path / "session.v4.jsonl.zstd"
    path.write_bytes(b"x" * 100)
    t0 = time.monotonic()
    assert dsh_home.read_jsonl_head(path, timeout=0.5) == b""
    assert time.monotonic() - t0 < 5


# ---------------- corrupt / sparse sessions are flagged, not faked -------

def _raw_session(tmp_path, sid, name, payload):
    d = tmp_path / "sessions" / "--ws--" / sid
    d.mkdir(parents=True)
    (d / name).write_bytes(payload)
    return d


def test_corrupt_transcript_is_flagged_degraded(tmp_path):
    """A card for an unreadable transcript must not pose as a real empty
    session: degraded=True on the card, still excluded from the chart."""
    _raw_session(tmp_path, "session-corrupt", "session.v4.jsonl",
                 b"this is not jsonl at all\n" * 10)
    rows = {r["id"]: r for r in dsh_home.trace_sessions(root=tmp_path, now=NOW)}
    assert rows["session-corrupt"]["degraded"] is True
    assert rows["session-corrupt"]["msg_count"] == 0
    assert dsh_home.chart_samples(root=tmp_path, now=NOW) == []


@pytest.mark.skipif(not HAS_ZSTD, reason="zstd binary not installed")
def test_corrupt_zstd_is_flagged_degraded(tmp_path):
    """Not a zstd frame at all: decompress yields nothing -> degraded."""
    _raw_session(tmp_path, "session-badzstd", "session.v4.jsonl.zstd",
                 b"not a zstd frame\n" * 100)
    rows = {r["id"]: r for r in dsh_home.trace_sessions(root=tmp_path, now=NOW)}
    assert rows["session-badzstd"]["degraded"] is True


def test_empty_transcript_is_flagged_degraded(tmp_path):
    _raw_session(tmp_path, "session-zero", "session.v4.jsonl", b"")
    rows = {r["id"]: r for r in dsh_home.trace_sessions(root=tmp_path, now=NOW)}
    assert rows["session-zero"]["degraded"] is True


def test_real_sessions_are_not_degraded(tmp_path):
    """Polarity pin: the flag fires on garbage, not on ordinary sessions —
    including one that is legitimately empty of usage (records parse fine)."""
    _traces_root(tmp_path)
    rows = {r["id"]: r for r in dsh_home.trace_sessions(root=tmp_path, now=NOW)}
    assert rows["session-bot"]["degraded"] is False
    assert rows["session-plain"]["degraded"] is False


def test_unreadable_tail_is_cached_per_stamp(tmp_path, monkeypatch):
    """A corrupt session must not be re-read every sampler tick: the None
    result is cached by (mtime_ns, size) exactly like a good read."""
    d = _raw_session(tmp_path, "session-cached", "session.v4.jsonl",
                     b"garbage\n" * 10)
    calls = []
    real = dsh_home.read_jsonl_tail

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(dsh_home, "read_jsonl_tail", counting)
    assert dsh_home._tail_stats(d) is None
    assert dsh_home._tail_stats(d) is None
    assert len(calls) == 1


# ---------------- swatch wiring (the routes' real entry points) ----------------

def _patch_roots(monkeypatch, tmp_path):
    empty_hermes = tmp_path / "hermes-empty"
    empty_hermes.mkdir()
    monkeypatch.setattr(dsh_home, "dsh_home", lambda: tmp_path)
    from app import swatch
    monkeypatch.setattr(swatch, "_hermes_home", lambda: empty_hermes)
    return swatch


def test_swatch_main_sessions_merges_dsh_entries(monkeypatch, tmp_path):
    _traces_root(tmp_path)
    swatch = _patch_roots(monkeypatch, tmp_path)
    d = swatch.main_sessions()
    rows = {s["id"]: s for s in d["sessions"]}
    assert rows["session-bot"]["profile"] == "dsb"
    assert rows["session-bot"]["display"] == "Chief"
    assert rows["session-plain"]["profile"] == "dsh"
    # every merged entry carries the keys the trace UI reads
    for s in d["sessions"]:
        for k in ("id", "profile", "title", "model", "msg_count",
                  "degraded", "last_activity", "live"):
            assert k in s


def test_swatch_main_transcript_routes_dsb_and_dsh(monkeypatch, tmp_path):
    _traces_root(tmp_path)
    swatch = _patch_roots(monkeypatch, tmp_path)
    d = swatch.main_transcript("dsb", "session-bot")
    assert d and d["profile"] == "dsb" and d["lines"]
    d2 = swatch.main_transcript("dsh", "session-plain")
    assert d2 and d2["profile"] == "dsh"
    assert swatch.main_transcript("dsb", "session-nope") is None


def test_swatch_survives_missing_dsh_dir(monkeypatch, tmp_path):
    """No ~/.dsh at all: Hermes traces keep working, DSH adds nothing."""
    swatch = _patch_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(dsh_home, "dsh_home", lambda: tmp_path / "absent")
    d = swatch.main_sessions()
    assert d["sessions"] == []
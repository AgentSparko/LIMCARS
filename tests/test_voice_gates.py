"""Voice-lane memory gate pins (app/voice_gates.py).

Fixtures are FAKE /proc/meminfo files — the reader parses the real format
("MemAvailable:  NNN kB"), so the whole read path is exercised, not a
mocked function. The pins encode the verdict's guard semantics:
- LOAD GATE boundary is inclusive at the floor: 9.9 refuses, 10.1 allows;
  unreadable meminfo FAILS CLOSED (no measurement, no load);
- WATCHDOG needs the low state SUSTAINED: a 20 s dip must not fire,
  30 s must, and the timer resets when the line is crossed back up
  (two separate 20 s dips are not 40 s);
- hysteresis: after a trip, oscillation around the low line never
  re-fires; only recovery to the recover line rearms.
"""
import sys

import pytest

from app import voice_gates as vg


def _meminfo(tmp_path, gib):
    """A meminfo fixture in the real format; other lines present so the
    parser must find the right one."""
    p = tmp_path / "meminfo"
    kb = int(gib * 2**20)
    p.write_text("MemTotal:       127468544 kB\n"
                 "MemFree:          524288 kB\n"
                 f"MemAvailable:   {kb} kB\n"
                 "Buffers:            1024 kB\n", encoding="ascii")
    return str(p)


# ---------------------------------------------------------- load gate ----

def test_gate_refuses_below_allows_above(tmp_path):
    v = vg.check_load(10.0, _meminfo(tmp_path, 9.9))
    assert v["ok"] is False and v["mem_avail_gib"] == pytest.approx(9.9)
    assert v["reason"].startswith("refuse")
    v = vg.check_load(10.0, _meminfo(tmp_path, 10.1))
    assert v["ok"] is True and v["mem_avail_gib"] == pytest.approx(10.1)
    assert v["reason"].startswith("allow")


def test_gate_boundary_is_inclusive(tmp_path):
    assert vg.check_load(10.0, _meminfo(tmp_path, 10.0))["ok"] is True


def test_gate_fails_closed_on_unreadable(tmp_path):
    v = vg.check_load(10.0, str(tmp_path / "nope"))
    assert v["ok"] is False and v["mem_avail_gib"] is None
    assert "fail closed" in v["reason"]
    bad = tmp_path / "meminfo"
    bad.write_text("MemAvailable: garbage kB\n", encoding="ascii")
    assert vg.check_load(10.0, str(bad))["ok"] is False


def test_reader_on_real_meminfo():
    assert vg.read_memavail_gib() > 0.0


# ----------------------------------------------------------- watchdog ----

def test_watchdog_needs_sustained_low():
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0)
    fired = [wd.sample(5.9, t) for t in range(0, 21)]      # 20 s dip
    fired += [wd.sample(13.0, t) for t in range(21, 31)]   # back up at 20 s
    assert not any(fired)                                   # must NOT fire


def test_watchdog_two_short_dips_are_not_cumulative():
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0)
    fired = [wd.sample(5.9, t) for t in range(0, 21)]       # dip 1: 20 s
    fired += [wd.sample(13.0, t) for t in range(21, 41)]    # up 20 s: timer resets
    fired += [wd.sample(5.9, t) for t in range(41, 62)]     # dip 2: 20 s
    assert not any(fired)


def test_watchdog_fires_at_30s_sustained_once():
    hits = []
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0,
                     callback=hits.append)
    fired = [wd.sample(5.9, t) for t in range(0, 35)]
    assert fired[30] is True                    # exactly at the 30 s mark
    assert sum(fired) == 1                      # once per trip, not per tick
    assert len(hits) == 1 and "unload" in hits[0]


def test_watchdog_hysteresis_blocks_refire_until_recovery():
    hits = []
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0,
                     callback=hits.append)
    for t in range(0, 31):
        wd.sample(5.9, t)                       # trip at t=30
    # oscillate around the low line for a minute: never re-fires
    for t in range(31, 91):
        assert wd.sample(5.9 if t % 2 else 6.1, t) is False
    # partial recovery to 6.5 (above low, below recover): still tripped
    for t in range(91, 121):
        assert wd.sample(6.5, t) is False
    # recovery to the recover line rearms
    assert wd.sample(7.0, 121) is False
    assert wd.tripped is False
    # a fresh sustained low period can fire again
    fired = [wd.sample(5.9, t) for t in range(122, 153)]
    assert sum(fired) == 1 and len(hits) == 2


def test_watchdog_none_sample_does_not_trip():
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0)
    fired = [wd.sample(None, t) for t in range(0, 60)]
    assert not any(fired)


def test_band_bounce_after_trip_does_not_rearm_or_refire():
    # Discriminating pin (TandemLLM's audit mutation C): rearm must happen
    # ONLY at the recover line, never at the low line. Scenario: trip at
    # 5.9 -> bounce INTO THE BAND (6.5: above low, below recover) for a
    # full 30 s -> renewed SUSTAINED low 5.9 for 31 s. Correct code:
    # refires = 0 (stays tripped throughout). Rearm-at-low mutation: the
    # band bounce rearms it and the renewed low refires (refires = 1).
    hits = []
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0,
                     callback=hits.append)
    for t in range(0, 31):
        wd.sample(5.9, t)                       # trip at t=30
    assert len(hits) == 1
    for t in range(31, 61):                     # 30 s inside the band
        assert wd.sample(6.5, t) is False
    assert wd.tripped is True                   # band must NOT rearm
    fired = [wd.sample(5.9, t) for t in range(61, 92)]   # 31 s renewed low
    assert not any(fired)                       # still tripped: no refire
    assert len(hits) == 1
    assert wd.sample(7.0, 92) is False          # recover line rearms
    assert wd.tripped is False
    fired = [wd.sample(5.9, t) for t in range(93, 124)]  # fresh sustained low
    assert sum(fired) == 1 and len(hits) == 2   # now a new trip is allowed


# ---------------------------------------------------- folded audit notes ----

def test_recover_line_must_be_above_low_line():
    with pytest.raises(ValueError):
        vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=6.0)
    with pytest.raises(ValueError):
        vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=5.0)


def test_throwing_callback_does_not_kill_the_watchdog():
    def boom(reason):
        raise RuntimeError("launcher hook exploded")
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0, callback=boom)
    fired = [wd.sample(5.9, t) for t in range(0, 35)]
    assert fired[30] is True                    # trip happened...
    assert wd.tripped is True                   # ...and state is consistent
    assert wd.sample(5.9, 36) is False          # keeps running tripped
    assert wd.last_reason and "trip" in wd.last_reason


def test_watch_loop_heartbeat_while_silent(tmp_path, capsys, monkeypatch):
    # fake clock advancing 100 s per call: the 60 s heartbeat fires every
    # tick deterministically, no wall-clock racing in the pin
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(vg.time, "monotonic", lambda: next(ticks))
    p = _meminfo(tmp_path, 13.5)
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0)
    code = vg._watch_loop(wd, p, interval=0.0, heartbeat=60.0, max_ticks=2)
    assert code == 0                            # no trip: healthy silence
    out = capsys.readouterr().out
    assert out.count("ok: MemAvailable 13.5") == 2
    assert "watchdog armed" in out


# ------------------------------------------------------- config + CLI ----

def test_config_defaults_carry_verdict_numbers():
    from app.config import DEFAULTS
    assert DEFAULTS["voice_gate_load_gib"] == 10.0
    assert DEFAULTS["voice_gate_watch_low_gib"] == 6.0
    assert DEFAULTS["voice_gate_watch_secs"] == 30.0
    assert DEFAULTS["voice_gate_watch_recover_gib"] == 7.0


def test_cli_check_load_exit_codes(tmp_path, capsys):
    assert vg.main(["check-load", "--min", "10",
                    "--meminfo", _meminfo(tmp_path, 13.5)]) == 0
    assert "allow" in capsys.readouterr().out
    assert vg.main(["check-load", "--min", "10",
                    "--meminfo", _meminfo(tmp_path, 9.9)]) == 1
    assert "refuse" in capsys.readouterr().out


def test_cli_watch_trips_with_exit_3(tmp_path, capsys):
    code = vg.main(["watch", "--low", "99", "--recover", "100",
                    "--secs", "0", "--interval", "0",
                    "--meminfo", _meminfo(tmp_path, 13.5)])
    assert code == 3
    assert "trip" in capsys.readouterr().out


def test_watch_loop_prints_are_flushed(tmp_path, monkeypatch):
    # A launcher consuming `watch` through a pipe gets BLOCK-BUFFERED
    # stdout: without flush=True on the prints, the trip line and the
    # heartbeat sit in the buffer until exit and the launcher sees nothing
    # in time. capsys cannot see this (it captures at the print level), so
    # the pin swaps sys.stdout for a recorder that counts flush() calls —
    # print(..., flush=True) must flush once per printed line.
    class Recorder:
        def __init__(self):
            self.text = ""
            self.flushes = 0

        def write(self, s):
            self.text += s
            return len(s)

        def flush(self):
            self.flushes += 1

    ticks = iter(range(0, 10_000, 100))     # fake clock: 100 s per call
    monkeypatch.setattr(vg.time, "monotonic", lambda: next(ticks))
    rec = Recorder()
    monkeypatch.setattr(sys, "stdout", rec)
    wd = vg.Watchdog(low_gib=6.0, secs=30.0, recover_gib=7.0)
    code = vg._watch_loop(wd, _meminfo(tmp_path, 5.75), interval=0.0,
                          heartbeat=60.0, max_ticks=5)
    assert code == 3
    assert "ok: MemAvailable 5.75" in rec.text   # heartbeat line printed
    assert "trip" in rec.text                    # trip line printed
    assert rec.flushes >= 2                      # ... each flushed at print


def test_cli_watch_bad_thresholds_is_clean_error(capsys):
    # recover <= low is a usage error: one line on stderr, exit 1 — the
    # launcher's log must not get a raw traceback.
    code = vg.main(["watch", "--low", "9", "--recover", "8"])
    assert code == 1
    err = capsys.readouterr().err
    assert "recover" in err and "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_config_bad_thresholds_is_clean_error(monkeypatch, capsys):
    # same clean error when the bad pair comes from config, not flags
    from app import config
    monkeypatch.setattr(config, "load", lambda: {
        "voice_gate_watch_low_gib": 9.0,
        "voice_gate_watch_recover_gib": 8.0})
    code = vg.main(["watch"])
    assert code == 1
    err = capsys.readouterr().err
    assert "recover" in err and "Traceback" not in err
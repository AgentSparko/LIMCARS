"""Disk watchdog: threshold logic, reclaimable-path scan, sampler wiring.

The 2026-11-09 crash made free-space truth a safety property — these tests
lock the guard boundaries (warn <10 GiB, hard floor <5 GiB on the f_bavail
reading) and the read-only scan that names reclaimable-LOOKING paths.
"""
import os
import time

from app.diskwatch import DiskWatch, disk_alert, largest_deletable


# ---------------- thresholds ----------------

def test_boundaries():
    assert disk_alert(10.0) == "ok"          # exactly at warn is still ok
    assert disk_alert(9.99) == "warn"
    assert disk_alert(5.0) == "warn"         # exactly at crit is warn, not crit
    assert disk_alert(4.99) == "critical"
    assert disk_alert(0.0) == "critical"
    assert disk_alert(-1.0) == "critical"


def test_no_reading_never_guesses():
    assert disk_alert(None) is None          # house rule: a dash, never a wrong number


def test_custom_thresholds():
    assert disk_alert(7.0, warn=20.0, crit=6.0) == "warn"
    assert disk_alert(5.9, warn=20.0, crit=6.0) == "critical"


# ---------------- scan ----------------

def _mk(path, gib_fraction):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(int(gib_fraction * 2**30 / 1024))   # sparse, cheap


def test_scan_names_caches_not_plain_dirs(tmp_path):
    _mk(str(tmp_path / ".cache" / "big"), 5)           # known home
    _mk(str(tmp_path / "projects" / "app" / "node_modules" / "dep" / "x"), 3)
    _mk(str(tmp_path / "projects" / "keepme" / "data"), 50)   # NOT a candidate
    r = largest_deletable(str(tmp_path), top=3, budget_s=8.0)
    paths = [i["path"] for i in r["items"]]
    assert any(p.endswith(".cache") for p in paths)
    assert any("node_modules" in p for p in paths)
    assert not any("keepme" in p for p in paths)
    assert r["items"] == sorted(r["items"], key=lambda i: -i["gib"])
    assert not r["partial"]


def test_scan_top_cap_and_zero_size_skip(tmp_path):
    for n in ("cache", "tmp", "logs", "backups"):
        _mk(str(tmp_path / n / "f"), 1)
    os.makedirs(str(tmp_path / "empty_cache"))          # 0 B → never shown
    r = largest_deletable(str(tmp_path), top=3, budget_s=8.0)
    assert len(r["items"]) == 3
    assert not any(i["path"].endswith("empty_cache") for i in r["items"])


def test_scan_budget_respected(tmp_path):
    _mk(str(tmp_path / ".cache" / "f"), 1)
    r = largest_deletable(str(tmp_path), budget_s=0.0001)
    assert isinstance(r["partial"], bool)               # completes or says partial
    assert r["scanned_s"] < 2.0                         # never runs away


# ---------------- sampler wiring ----------------

def test_tick_row_carries_alert_state():
    from app.hw import HwSampler
    from app.sse import Broker
    s = HwSampler({}, Broker())
    s._tick()
    row = s.ring.last()
    assert row["disk_alert"] in ("ok", "warn", "critical")
    assert row["disk_warn_gib"] == 10.0 and row["disk_crit_gib"] == 5.0
    # on a healthy box no critical scan is kicked off
    assert "disk_alert_top" not in row


def test_critical_kicks_scan_once(tmp_path, monkeypatch):
    calls = []

    def fake(root, top=3, budget_s=8.0, max_depth=4):
        calls.append(root)
        return {"items": [{"path": root + "/.cache", "gib": 42.0}],
                "partial": False, "scanned_s": 0.1}

    monkeypatch.setattr("app.diskwatch.largest_deletable", fake)
    w = DiskWatch({"disk_warn_gib": 10, "disk_crit_gib": 5}, root=str(tmp_path))
    row = w.tick(4.0)
    assert row["disk_alert"] == "critical"
    for _ in range(100):                                # scan lands off-thread
        row = w.tick(4.0)
        if "disk_alert_top" in row:
            break
        time.sleep(0.01)
    assert row["disk_alert_top"]["items"][0]["gib"] == 42.0
    assert calls == [str(tmp_path)]                     # TTL: not re-run per tick
    assert w.tick(9.0)["disk_alert"] == "warn"          # recovery re-labels
    assert w.tick(20.0)["disk_alert"] == "ok"
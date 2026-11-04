"""Whole-drive SSD view: per-day totals survive LIMCARS restarts and reboots."""
from app import hw
from app.store import Store


def test_ssd_days_book_deltas_and_restart_from_zero_after_a_reboot(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    day = 20000 * 86400.0
    st.ssd_add(day + 10, 100 * 2**30, 10 * 2**30, "boot-a")      # first sight: nothing booked
    st.ssd_add(day + 70, 103 * 2**30, 11 * 2**30, "boot-a")      # +3 read, +1 written
    st.ssd_add(day + 86400 + 5, 104 * 2**30, 11 * 2**30, "boot-a")  # next day +1 read
    st.ssd_add(day + 86400 + 65, 2 * 2**30, 0.5 * 2**30, "boot-b")  # reboot: counters from 0
    d = {x["day"]: (x["read_gb"], x["written_gb"]) for x in st.ssd_days(30)}
    assert d == {20000: (3.0, 1.0), 20001: (3.0, 0.5)}


def test_diskstats_sectors_are_512_bytes(tmp_path, monkeypatch):
    f = tmp_path / "diskstats"
    f.write_text(" 259 0 nvme0n1 10 0 2048 0 5 0 4096 0 0 0 0\n", encoding="utf-8")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(f if p == "/proc/diskstats" else p, *a, **k))
    assert hw._ssd_bytes("nvme0n1") == (2048 * 512, 4096 * 512)
    assert hw._ssd_bytes("sda") is None

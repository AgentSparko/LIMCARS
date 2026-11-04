"""Peak/off-peak cloud prices (UTC windows) and the per-model price manager."""
import calendar
import json

from app import pricing as P

DS = {"display_name": "DeepSeek V4.1 Flash 1017", "aliases": ["dsv41"],
      "input_per_M": 0.15, "output_per_M": 0.60, "cache_write_per_M": 0.15, "cache_read_per_M": 0.003,
      "peak": {"input_per_M": 0.30, "output_per_M": 1.20, "cache_write_per_M": 0.30,
               "cache_read_per_M": 0.006, "hours_utc": [["01:00", "04:00"], ["06:00", "10:00"]],
               "offpeak_utc": [["Fri 16:00", "Sun 16:00"]]}}


def _t(s):   # "2026-11-04 02:30" UTC -> epoch
    return calendar.timegm(__import__("time").strptime(s, "%Y-%m-%d %H:%M"))


def test_windows_are_utc_and_the_weekend_exception_wins():
    pk = DS["peak"]
    assert P.in_peak(pk, _t("2026-11-04 02:30"))       # Wed 02:30 peak
    assert not P.in_peak(pk, _t("2026-11-04 04:30"))   # Wed 04:30 off-peak
    assert P.in_peak(pk, _t("2026-11-04 09:59"))
    assert not P.in_peak(pk, _t("2026-11-04 10:00"))
    assert not P.in_peak(pk, _t("2026-11-07 02:30"))   # Sat: weekend off-peak
    assert P.in_peak(pk, _t("2026-11-06 07:00"))       # Fri morning still peak
    assert not P.in_peak(pk, _t("2026-11-08 15:59"))   # Sun before 16:00 off-peak
    assert P.in_peak(pk, _t("2026-11-09 01:30"))       # Mon peak again


def test_resolve_by_alias_switches_prices_and_slug_at_peak(tmp_path, monkeypatch):
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"models": {"deepseek-v4.1-flash-1017": DS},
                             "default": {"output_per_M": 3, "input_per_M": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(P, "FILE", f)
    monkeypatch.setattr(P, "_cache", None)
    off = P.resolve("dsv41", _t("2026-11-04 12:00"))
    peak = P.resolve("dsv41", _t("2026-11-04 02:00"))
    assert (off["slug"], off["period"], off["output"], off["cache_read"]) == (
        "deepseek-v4.1-flash-1017", "offpeak", 0.60, 0.003)
    assert (peak["slug"], peak["period"], peak["output"], peak["cache_write"]) == (
        "deepseek-v4.1-flash-1017@peak", "peak", 1.20, 0.30)


def test_validate_and_save_model(tmp_path, monkeypatch):
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"models": {"x": {"output_per_M": 1, "legacy_reference": {"a": 1}}}}),
                 encoding="utf-8")
    monkeypatch.setattr(P, "FILE", f)
    assert P.validate_entry(DS) is None
    assert P.validate_entry({"output_per_M": -1})
    assert P.validate_entry({"output_per_M": 1, "peak": {"hours_utc": [["25:00", "26:00"]]}})
    assert P.validate_entry({"output_per_M": 1, "peak": {"hours_utc": []}})
    m = P.save_model("x", {"output_per_M": 2})
    assert m["x"] == {"output_per_M": 2, "legacy_reference": {"a": 1}}, "unknown keys are kept"
    m = P.save_model("deepseek", DS)
    assert m["deepseek"]["peak"]["hours_utc"][0] == ["01:00", "04:00"]
    m = P.save_model("deepseek", None)
    assert "deepseek" not in m

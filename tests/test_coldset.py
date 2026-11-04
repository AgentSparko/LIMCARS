"""SSD cold-expert set + reserve view reads the recipe's files and keeps the highest reserve peak."""
import json

from app import coldset


def _cg(tmp_path, cur, peak):
    cg = tmp_path / "reserve.slice"
    (cg / "hermes-gateway.service").mkdir(parents=True, exist_ok=True)
    (cg / "memory.current").write_text(f"{cur}\n", encoding="utf-8")
    (cg / "memory.peak").write_text(f"{peak}\n", encoding="utf-8")
    (cg / "memory.max").write_text(f"{12 * 2**30}\n", encoding="utf-8")
    (cg / "memory.high").write_text("max\n", encoding="utf-8")
    return cg


def test_nothing_on_disk_is_unavailable(tmp_path):
    cfg = {"route_heat_dir": str(tmp_path / "none"), "reserve_cgroup": str(tmp_path / "nocg")}
    assert coldset.report(cfg, tmp_path) == {"available": False, "dir": str(tmp_path / "none"),
                                             "records": [], "auto_cold": None, "reserve": None}


def test_records_last_rebuild_and_reserve(tmp_path):
    heat = tmp_path / "heat"
    heat.mkdir()
    (heat / "route-heat-m.status.json").write_text(json.dumps(
        {"time": 1000.0, "routes": 5, "boots": 2, "record_bytes": 149708, "recent_cold_share": 0.013}), encoding="utf-8")
    (heat / "auto_cold-last.json").write_text(json.dumps({"time": 900.0, "decision": "kept", "gain": 0.02}), encoding="utf-8")
    cfg = {"route_heat_dir": str(heat), "reserve_cgroup": str(_cg(tmp_path, 300, 500))}
    r = coldset.report(cfg, tmp_path, now=1060.0)
    assert r["available"]
    assert r["records"][0]["name"] == "route-heat-m" and r["records"][0]["age_s"] == 60
    assert r["auto_cold"]["decision"] == "kept" and r["auto_cold"]["age_s"] == 160
    res = r["reserve"]
    assert (res["current_bytes"], res["peak_bytes"], res["max_bytes"], res["high_bytes"]) == (300, 500, 12 * 2**30, None)
    assert res["peak_ever_bytes"] == 500 and res["tenants"] == ["hermes-gateway.service"]


def test_reserve_peak_survives_a_recreated_slice(tmp_path):
    cfg = {"route_heat_dir": str(tmp_path / "none"), "reserve_cgroup": str(_cg(tmp_path, 300, 9000))}
    coldset.report(cfg, tmp_path, now=1.0)
    _cg(tmp_path, 100, 200)            # slice recreated: its own peak restarts low
    res = coldset.report(cfg, tmp_path, now=2.0)["reserve"]
    assert res["peak_bytes"] == 200 and res["peak_ever_bytes"] == 9000 and res["peak_ever_time"] == 1.0

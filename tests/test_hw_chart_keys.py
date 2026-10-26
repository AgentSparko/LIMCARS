"""Which hardware keys are chartable is a contract with the UI."""
from app import hw


def test_gpu_count_is_not_a_chartable_series():
    last = {"gpu_count": 1, "gpu0_util": 42.0, "gpu0_temp": 55.0,
            "vram_used_gb": 90.0, "cpu_pct": 11.0}
    keys = hw.series_keys(last)
    assert "gpu_count" not in keys, f"card count is chartable: {keys}"
    assert {"gpu0_util", "gpu0_temp", "vram_used_gb"} <= set(keys), keys

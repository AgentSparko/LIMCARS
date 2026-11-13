"""Which hardware keys are chartable is a contract with the UI."""
from app import hw


def test_gpu_count_is_not_a_chartable_series():
    last = {"gpu_count": 1, "gpu0_util": 42.0, "gpu0_temp": 55.0,
            "vram_used_gb": 90.0, "cpu_pct": 11.0}
    keys = hw.series_keys(last)
    assert "gpu_count" not in keys, f"card count is chartable: {keys}"
    assert {"gpu0_util", "gpu0_temp", "vram_used_gb"} <= set(keys), keys


def test_true_free_and_page_cache_are_chartable():
    """aeeb003 samples mem_free_gb/mem_cache_gb; a sampled-but-never-charted
    key is a dead gauge — the snapshot series is built from series_keys(), so
    without this the fields reach SSE live ticks but no page-load seed."""
    keys = hw.series_keys({})
    assert {"mem_free_gb", "mem_cache_gb"} <= set(keys), keys

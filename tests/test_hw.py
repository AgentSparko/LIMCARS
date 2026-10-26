"""nvidia-smi parsing: unified (GB10), single discrete card, multi-GPU."""
from app.hw import _gpu_row, _parse_nvidia

GB10 = "0, 96, 63, 41.0, [N/A], 2463, 3003, [N/A], [N/A], NVIDIA GB10\n"
ONE = "0, 50, 60, 200.5, 450.00, 2520, 2850, 8192, 24576, NVIDIA GeForce RTX 4090\n"
TWO = ("0, 80, 70, 300.0, 350.00, 1800, 2100, 40960, 81920, NVIDIA H100, PCIe\n"
       "1, 20, 55, 100.0, 350.00, 0, 2100, 1024, 81920, NVIDIA H100, PCIe\n")


def test_unified_memory_has_no_vram_keys():
    row = _gpu_row(_parse_nvidia(GB10))
    assert row["gpu_util"] == 96 and row["gpu_clock_mhz"] == 2463
    assert row["gpu_count"] == 1 and row["gpu_power_limit_w"] is None
    assert "vram_used_gb" not in row and not any(k.startswith("gpu0_") for k in row)


def test_single_discrete_card_reports_vram():
    row = _gpu_row(_parse_nvidia(ONE))
    assert row["gpu_power_w"] == 200.5 and row["gpu_power_limit_w"] == 450.0
    assert row["vram_used_gb"] == 8.0 and row["vram_total_gb"] == 24.0
    assert not any(k.startswith("gpu0_") for k in row)


def test_multi_gpu_totals_and_per_card_keys():
    gpus = _parse_nvidia(TWO)
    assert [g["name"] for g in gpus] == ["NVIDIA H100, PCIe"] * 2   # comma in name survives
    row = _gpu_row(gpus)
    assert row["gpu_count"] == 2
    assert row["gpu_power_w"] == 400.0 and row["gpu_temp_c"] == 70 and row["gpu_util"] == 50.0
    assert row["gpu1_clock_mhz"] == 2100          # current 0 → max SM clock
    assert row["vram_used_gb"] == 41.0 and row["vram_total_gb"] == 160.0
    assert row["gpu0_mem_used_gb"] == 40.0 and row["gpu1_util"] == 20
    assert row["gpu0_mem_pct"] == 50.0 and row["gpu1_mem_pct"] == 1.2


def test_no_gpu():
    assert _parse_nvidia("") == [] and _gpu_row([]) == {}


def test_missing_memory_used_does_not_break_row():
    two = ("0, 80, 70, 300.0, 350.00, 1800, 2100, [N/A], 81920, A\n"
           "1, 20, 55, 100.0, 350.00, 1800, 2100, 1024, 81920, B\n")
    row = _gpu_row(_parse_nvidia(two))
    assert "gpu0_mem_pct" not in row and row["gpu1_mem_pct"] == 1.2


def test_partial_memory_used_skips_vram_aggregate():
    two = ("0, 80, 70, 300.0, 350.00, 1800, 2100, [N/A], 81920, A\n"
           "1, 20, 55, 100.0, 350.00, 1800, 2100, 1024, 81920, B\n")
    assert "vram_used_gb" not in _gpu_row(_parse_nvidia(two))

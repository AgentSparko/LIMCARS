"""Dialect tests: each engine family's /metrics body must be detected AND
mapped to the right logical signals. Metric names ported from probe-verified dashboard adapters
adapters; bodies here are trimmed to the fields the sampler consumes."""
import pytest

from app import dialects as D


def _parse(body):
    from app import prom
    return prom.parse(body)


VLLM_BODY = """# HELP vllm:num_requests_running Multi-process metric
vllm:num_requests_running{engine="0"} 2.0
vllm:num_requests_waiting 0.0
vllm:kv_cache_usage_perc 0.051
vllm:prompt_tokens_total 16672558.0
vllm:generation_tokens_total 3274894.0
vllm:prefix_cache_queries_total 55173888.0
vllm:prefix_cache_hits_total 53403904.0
vllm:spec_decode_num_draft_tokens_total 4000.0
vllm:spec_decode_num_accepted_tokens_total 2400.0
vllm:request_prefill_time_seconds_sum 1234.5
vllm:request_prefill_time_seconds_count 771.0
vllm:request_prefill_time_seconds_bucket{le="0.1"} 10.0
vllm:request_prefill_time_seconds_bucket{le="+Inf"} 771.0
"""

SGLANG_BODY = """sglang:num_running_reqs 1.0
sglang:num_queue_reqs 0.0
sglang:token_usage 0.12
sglang:prompt_tokens_total 1000.0
sglang:generation_tokens_total 500.0
sglang:realtime_tokens_total{mode="prefill"} 900.0
sglang:realtime_tokens_total{mode="decode"} 100.0
sglang:cached_tokens_total 40.0
sglang:time_to_first_token_seconds_count 12.0
sglang:time_to_first_token_seconds_sum 3.6
sglang:time_to_first_token_seconds_bucket{le="1.0"} 12.0
sglang:time_to_first_token_seconds_bucket{le="+Inf"} 12.0
"""

EXL3_BODY = """exl3_requests_running 1.0
exl3_requests_waiting 0.0
exl3_prompt_tokens_total 8000.0
exl3_generation_tokens_total 1200.0
exl3_requests_total 9.0
exl3_prefill_seconds_total 16.0
exl3_generate_seconds_total 40.0
exl3_accepted_draft_tokens_total 300.0
exl3_kv_used_tokens 50000.0
exl3_kv_capacity_tokens 200000.0
"""

TENSORFOLD_BODY = """tensorfold:requests_running 3.0
tensorfold:requests_waiting 1.0
tensorfold:kv_cache_usage_perc 0.42
tensorfold:prompt_tokens_total 9000.0
tensorfold:generation_tokens_total 2100.0
tensorfold:mtp_drafted_total 800.0
tensorfold:mtp_accepted_total 500.0
tensorfold:requests_total 15.0
tensorfold:request_prefill_seconds_sum 30.0
tensorfold:request_prefill_seconds_count 15.0
tensorfold:request_prefill_seconds_bucket{le="+Inf"} 15.0
"""


def test_detect_each_family():
    assert D.detect(VLLM_BODY) == "vllm"
    assert D.detect(SGLANG_BODY) == "sglang"
    assert D.detect(EXL3_BODY) == "exl3"
    assert D.detect(TENSORFOLD_BODY) == "tensorfold"
    assert D.detect("\nqwen38:prompt_tokens_total 5.0\n") == "tandemllm"
    assert D.detect("\nsomething_else 1.0\n") is None


def test_vllm_reads():
    dial = D.DIALECTS["vllm"]
    s = _parse(VLLM_BODY)
    g, c, h = D.read_gauges(s, dial), D.read_counters(s, dial), D.read_hists(s, dial)
    assert g["running"] == 2.0 and g["waiting"] == 0.0
    assert g["kv_pct"] == pytest.approx(0.051)
    assert c["prompt_tokens"] == 16672558.0
    assert c["prefix_hits"] == 53403904.0
    assert c["draft_tokens"] == 4000.0
    st = h["prefill_time"]
    assert st[1] == pytest.approx(1234.5) and st[2] == 771.0


def test_sglang_prefill_mode_label_match():
    dial = D.DIALECTS["sglang"]
    s = _parse(SGLANG_BODY)
    c = D.read_counters(s, dial)
    # trailing-* prefix match on the mode label picks ONLY prefill series
    assert c["prefill_tokens"] == 900.0
    assert c["prompt_tokens"] == 1000.0
    assert c["req_success"] == 12.0
    g = D.read_gauges(s, dial)
    assert g["running"] == 1.0 and g["kv_pct"] == pytest.approx(0.12)


def test_exl3_seconds_counters():
    dial = D.DIALECTS["exl3"]
    s = _parse(EXL3_BODY)
    c = D.read_counters(s, dial)
    assert c["prefill_seconds"] == 16.0 and c["decode_seconds"] == 40.0
    assert c["prompt_tokens"] == 8000.0 and c["req_success"] == 9.0
    assert c["accepted_tokens"] == 300.0


def test_tensorfold_alt_names_and_hist():
    dial = D.DIALECTS["tensorfold"]
    s = _parse(TENSORFOLD_BODY)
    g, c, h = D.read_gauges(s, dial), D.read_counters(s, dial), D.read_hists(s, dial)
    assert g["running"] == 3.0 and g["kv_pct"] == pytest.approx(0.42)
    assert c["draft_tokens"] == 800.0 and c["accepted_tokens"] == 500.0
    st = h["prefill_time"]
    assert st[1] == pytest.approx(30.0) and st[2] == 15.0


def test_missing_signal_reads_none_not_zero():
    dial = D.DIALECTS["vllm"]
    s = _parse("vllm:num_requests_running 1.0\n")
    g, c = D.read_gauges(s, dial), D.read_counters(s, dial)
    assert g["running"] == 1.0
    assert g["kv_pct"] is None
    assert c["prompt_tokens"] is None
    assert D.read_hists(s, dial)["prefill_time"] is None


def test_tandemllm_dual_namespace_alt():
    dial = D.DIALECTS["tandemllm"]
    s = _parse("qwen38:prompt_tokens_total 42.0\nqwen38:requests_inflight 1.0\n")
    c = D.read_counters(s, dial)
    g = D.read_gauges(s, dial)
    assert c["prompt_tokens"] == 42.0   # qwen38 alt resolves
    assert g["running"] == 1.0

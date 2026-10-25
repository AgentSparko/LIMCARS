"""Dialect tests: each engine family's /metrics body must be detected AND
mapped to the right logical signals. Metric names ported from TandemDash-Fork
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


def test_vllm_cached_tokens_prefers_the_authoritative_counter():
    """The money split and every token total assume cached is a SUBSET of the
    prompt tokens, so it must come from vLLM's own cached-prompt counter when
    the build exposes it — prefix_cache_hits_total is block-rounded and is only
    a fallback for older builds."""
    dial = D.DIALECTS["vllm"]
    with_cached = VLLM_BODY + "vllm:prompt_tokens_cached_total 12000000.0\n"
    c = D.read_counters(_parse(with_cached), dial)
    cached, prompt = c["cached_tokens"], c["prompt_tokens"]
    assert cached is not None and prompt is not None
    assert cached == 12000000.0                             # NOT the hits figure
    assert cached <= prompt                                 # subset invariant
    assert c["prefix_hits"] == 53403904.0                   # still its own signal
    # older build without that counter: fall back to the prefix-cache hits
    c2 = D.read_counters(_parse(VLLM_BODY), dial)
    assert c2["cached_tokens"] == 53403904.0


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


def test_vllm_e2e_is_the_whole_request_not_the_running_phase():
    """vllm:request_inference_time_seconds is 'time spent in RUNNING phase' and
    excludes queueing. A bubble labelled end-to-end that reads it under-reads by
    exactly whatever requests spent waiting."""
    body = (
        'vllm:e2e_request_latency_seconds_sum{model_name="m"} 40.0\n'
        'vllm:e2e_request_latency_seconds_count{model_name="m"} 10.0\n'
        'vllm:e2e_request_latency_seconds_bucket{model_name="m",le="5"} 10.0\n'
        'vllm:e2e_request_latency_seconds_bucket{model_name="m",le="+Inf"} 10.0\n'
        'vllm:request_inference_time_seconds_sum{model_name="m"} 10.0\n'
        'vllm:request_inference_time_seconds_count{model_name="m"} 10.0\n'
        'vllm:request_inference_time_seconds_bucket{model_name="m",le="5"} 10.0\n'
        'vllm:request_inference_time_seconds_bucket{model_name="m",le="+Inf"} 10.0\n'
    )
    h = D.read_hists(_parse(body), D.DIALECTS["vllm"])
    assert h["e2e"][1] == pytest.approx(40.0), \
        "e2e must be the whole request, not the RUNNING phase"
    assert h["e2e"][2] == 10.0


QSE_BODY = """qse_requests_running{model_name="m"} 2.0
qse_requests_waiting 0.0
qse_prompt_tokens_total 9000.0
qse_generation_tokens_total 2100.0
qse_request_success_total 14.0
qse_prompt_tokens_cached_total 3000.0
qse_prefill_tokens_forwarded_total 6000.0
qse_spec_decode_num_draft_tokens_total 800.0
qse_spec_decode_num_accepted_tokens_total 500.0
qse_e2e_request_latency_seconds_sum{model_name="m"} 40.0
qse_e2e_request_latency_seconds_count{model_name="m"} 10.0
qse_e2e_request_latency_seconds_bucket{model_name="m",le="5"} 10.0
qse_e2e_request_latency_seconds_bucket{model_name="m",le="+Inf"} 10.0
"""


def test_tandemllm_qse_family_detected_and_mapped():
    assert D.detect("\n" + QSE_BODY) == "tandemllm"
    dial = D.DIALECTS["tandemllm"]
    s = _parse(QSE_BODY)
    g, c, h = D.read_gauges(s, dial), D.read_counters(s, dial), D.read_hists(s, dial)
    assert g["running"] == 2.0 and g["waiting"] == 0.0
    assert c["prompt_tokens"] == 9000.0 and c["gen_tokens"] == 2100.0
    assert c["req_success"] == 14.0
    # prefix_hits must be RESTORED TOKENS. qse_cache_hits_total counts lookups;
    # subtracting a lookup count from a token count inflates prefill speed.
    assert c["prefix_hits"] == 3000.0
    assert c["prefill_tokens"] == 6000.0
    assert c["draft_tokens"] == 800.0 and c["accepted_tokens"] == 500.0
    st = h["e2e"]
    assert st[1] == pytest.approx(40.0) and st[2] == 10.0
    # no single-counter counterpart for prefix_queries -> honest None, never 0%
    assert c.get("prefix_queries") is None

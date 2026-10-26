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
    # upstream METRICS.md: hit rate = qse_prompt_tokens_cached_total / qse_prompt_tokens_total
    assert c["prefix_queries"] == 9000.0
    assert c["cached_tokens"] == 3000.0          # a subset of prompt_tokens
    assert c.get("drafts") is None and c.get("proc_start") is None


def test_tandemllm_contract_010_fallbacks_and_new_signals():
    body = QSE_BODY.replace("qse_prompt_tokens_cached_total 3000.0\n",
                            "qse_prefill_tokens_reused_total 2500.0\n")
    body += "qse_spec_decode_num_drafts_total 200.0\nqse_engine_start_time_seconds 1790000000\n"
    c = D.read_counters(_parse(body), D.DIALECTS["tandemllm"])
    assert c["cached_tokens"] == 2500.0 and c["prefix_hits"] == 2500.0
    assert c["drafts"] == 200.0 and c["proc_start"] == 1790000000


def test_tandemllm_old_namespaces_never_fake_a_hit_rate():
    c = D.read_counters(_parse("qwen38:prompt_tokens_total 42.0\n"), D.DIALECTS["tandemllm"])
    assert c["prefix_queries"] is None and c["prefix_hits"] is None


# verbatim shape of llama-server --metrics (tools/server/server-task.cpp)
LLAMACPP_BODY = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed, excluding cached tokens
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 6000
llamacpp:prompt_tokens_cached_total 4000
llamacpp:prompt_seconds_total 3
llamacpp:tokens_predicted_total 1500
llamacpp:tokens_predicted_seconds_total 30
llamacpp:n_decode_total 1800
llamacpp:n_tokens_max 9000
llamacpp:spec_decode_num_draft_tokens_total 800
llamacpp:spec_decode_num_accepted_tokens_total 600
llamacpp:spec_decode_num_drafts_total 200
llamacpp:prompt_tokens_seconds 2000
llamacpp:predicted_tokens_seconds 50
llamacpp:requests_processing 2
llamacpp:requests_deferred 1
llamacpp:n_busy_slots_per_decode 1.5
llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 180
"""


def test_llamacpp_upstream_names_mapped():
    assert D.detect(LLAMACPP_BODY) == "llamacpp"
    dial = D.DIALECTS["llamacpp"]
    s = _parse(LLAMACPP_BODY)
    g, c = D.read_gauges(s, dial), D.read_counters(s, dial)
    assert g["running"] == 2 and g["waiting"] == 1
    # no KV occupancy metric upstream, so the key is simply absent and the UI
    # shows a dash; .get() because absence is the honest answer here
    assert g.get("kv_pct") is None
    assert c["prompt_tokens"] == 10000          # processed + cached: cached is a subset
    assert c["cached_tokens"] == 4000 and c["prefix_hits"] == 4000 and c["prefix_queries"] == 10000
    assert c["prefill_compute_tokens"] == 6000 and c["prefill_seconds"] == 3
    assert c["gen_tokens"] == 1500 and c["decode_seconds"] == 30 and c.get("iterations") is None
    assert c["draft_tokens"] == 800 and c["accepted_tokens"] == 600 and c["drafts"] == 200
    assert c.get("req_success") is None         # upstream has no request counter
    assert all(v is None for v in D.read_hists(s, dial).values())


def test_invented_llamacpp_names_stay_unmapped():
    """This test used to assert the OPPOSITE -- that llamacpp:kv_cache_usage_ratio
    read as 0.25 and llamacpp:predicted_tokens_total read as 9, under an "older
    builds" excuse upstream does not support. Neither name exists in llama.cpp
    (tools/server/server-task.cpp, README.md, tests/unit/test_metrics.py on master,
    checked 2026-10-26): the first is vLLM's kv_cache_usage_perc wearing a llama.cpp
    prefix, the second is the real tokens_predicted_total spelled backwards.

    Kept as a test rather than deleted, because the failure mode is quiet: if
    someone re-adds either mapping, a Prometheus-shaped line WILL populate the gauge
    and the dashboard will display a KV utilisation figure no engine ever produced.
    So we feed exactly those lines and require them to be ignored, while the real
    names still resolve.
    """
    body = ("llamacpp:prompt_tokens_total 70\n"
            "llamacpp:kv_cache_usage_ratio 0.25\n"        # not a llama.cpp metric
            "llamacpp:predicted_tokens_total 9\n"         # reversed; not either
            "llamacpp:tokens_predicted_total 12\n"        # the real one
            "llamacpp:requests_completed 40\n")           # zero hits upstream
    dial = D.DIALECTS["llamacpp"]
    s = _parse(body)
    c, g = D.read_counters(s, dial), D.read_gauges(s, dial)
    assert c["prompt_tokens"] == 70 and c["cached_tokens"] is None
    assert c["gen_tokens"] == 12, f"real name must win: {c['gen_tokens']}"
    assert g.get("kv_pct") is None, "invented KV ratio must not surface"
    assert c.get("req_success") is None, "invented request counter must not surface"


def test_existing_dialects_untouched_by_sum_form():
    for body, kind in ((VLLM_BODY, "vllm"), (SGLANG_BODY, "sglang"), (EXL3_BODY, "exl3"),
                       (TENSORFOLD_BODY, "tensorfold"), (ATLAS_BODY, "atlas")):
        assert D.detect(body) == kind


ATLAS_BODY = """# HELP atlas_requests_total Total requests processed
# TYPE atlas_requests_total counter
atlas_requests_total 40
atlas_requests_active 3
atlas_prompt_tokens_total 12000
atlas_generation_tokens_total 3000
atlas_time_to_first_token_seconds_bucket{model="m",le="0.5"} 30
atlas_time_to_first_token_seconds_bucket{model="m",le="+Inf"} 40
atlas_time_to_first_token_seconds_sum{model="m"} 16
atlas_time_to_first_token_seconds_count{model="m"} 40
atlas_spec_decode_verify_total{k="2",outcome="accept"} 60
atlas_spec_decode_verify_total{k="2",outcome="reject"} 40
atlas_spec_decode_verify_total{k="mtp",outcome="accept_all"} 20
atlas_spec_decode_verify_total{k="mtp",outcome="accept_partial"} 10
atlas_spec_decode_verify_total{k="mtp",outcome="reject"} 70
"""


def test_atlas_detected_without_reclassifying_others():
    assert D.detect(ATLAS_BODY) == "atlas"
    assert D.detect(VLLM_BODY) == "vllm" and D.detect(SGLANG_BODY) == "sglang"
    assert D.detect(EXL3_BODY) == "exl3" and D.detect(TENSORFOLD_BODY) == "tensorfold"
    assert D.detect(VLLM_BODY + "atlas_requests_total 1\n") == "vllm"   # earlier family wins


def test_atlas_reads_and_step_level_spec_acceptance():
    from app import prom
    dial = D.DIALECTS["atlas"]
    s = prom.parse(ATLAS_BODY)
    g, c, h = D.read_gauges(s, dial), D.read_counters(s, dial), D.read_hists(s, dial)
    assert g["running"] == 3 and g.get("waiting") is None and g.get("kv_pct") is None
    assert c["prompt_tokens"] == 12000 and c["gen_tokens"] == 3000 and c["req_success"] == 40
    assert c["spec_steps_ok"] == 90 and c["spec_steps"] == 200          # 45 % of verify steps
    assert c.get("accepted_tokens") is None and c.get("draft_tokens") is None   # not tokens
    assert c.get("cached_tokens") is None and c.get("drafts") is None
    assert h["ttft"] is not None and h.get("e2e") is None

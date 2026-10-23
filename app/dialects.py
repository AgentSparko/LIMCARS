"""Engine dialects: one logical schema, many Prometheus-flavoured engines.

Logical signal names consumed by the sampler and the UI:

    running            in-flight requests                gauge
    waiting            queued requests                   gauge
    kv_pct             KV/prefix pool utilisation 0-100  gauge
    prompt_tokens      cumulative prompt tokens          counter
    gen_tokens         cumulative generated tokens       counter
    prefix_queries     cumulative prefix-cache queries   counter
    prefix_hits        cumulative prefix-cache hits      counter
    ttft               time_to_first_token histogram     histogram (s)
    prefill_time       per-request prefill hist          histogram (s)
    decode_time        per-request decode hist           histogram (s)
    itl                inter-token latency hist          histogram (s)
    queue_time         per-request queue hist            histogram (s)
    req_success        finished requests                 counter
    draft/accepted     speculative-decode tokens         counter
    e2e                end-to-end request hist           histogram (s)

Adding an engine = add a dict here. A missing signal reads None ("–") in
the UI, never a wrong number.
"""
from __future__ import annotations

from . import prom

# Each dialect maps logical -> (metric-name-template, optional label filter)
# "{p}" is replaced by the engine's metric prefix (e.g. "vllm", "sglang").

VLLM = {
    "kind": "vllm",
    "prefix": "vllm:",
    "gauge": {
        "running": "vllm:num_requests_running",
        "waiting": "vllm:num_requests_waiting",
        "kv_pct": "vllm:kv_cache_usage_perc",
    },
    "counter": {
        "prompt_tokens": "vllm:prompt_tokens_total",
        "gen_tokens": "vllm:generation_tokens_total",
        "cached_tokens": "vllm:prefix_cache_hits_total",
        "proc_start": "process_start_time_seconds",
        "iterations": "vllm:iteration_tokens_total_count",
        "prefix_queries": "vllm:prefix_cache_queries_total",
        "prefix_hits": "vllm:prefix_cache_hits_total",
        "req_success": "vllm:request_success_total",
        "draft_tokens": "vllm:spec_decode_num_draft_tokens_total",
        "accepted_tokens": "vllm:spec_decode_num_accepted_tokens_total",
        "drafts": "vllm:spec_decode_num_drafts_total",
    },
    "hist": {
        "ttft": "vllm:time_to_first_token_seconds",
        "prefill_time": "vllm:request_prefill_time_seconds",
        "decode_time": "vllm:request_decode_time_seconds",
        "itl": "vllm:inter_token_latency_seconds",
        "queue_time": "vllm:request_queue_time_seconds",
        "e2e": "vllm:request_inference_time_seconds",
    },
}

SGLANG = {
    "kind": "sglang",
    "prefix": "sglang:",
    "gauge": {
        "running": "sglang:num_running_reqs",
        "waiting": "sglang:num_queue_reqs",
        "kv_pct": "sglang:token_usage",
        "cache_hit": "sglang:cache_hit_rate",
    },
    "counter": {
        "prompt_tokens": "sglang:prompt_tokens_total",
        "gen_tokens": "sglang:generation_tokens_total",
        "req_success": "sglang:num_requests_total",
    },
    "hist": {
        "ttft": "sglang:time_to_first_token_seconds",
        "itl": "sglang:inter_token_latency_seconds",
        "e2e": "sglang:e2e_request_latency_seconds",
    },
}

LLAMACPP = {
    "kind": "llamacpp",
    "prefix": "llamacpp:",
    "gauge": {
        "running": "llamacpp:requests_processing",
        "waiting": "llamacpp:requests_deferred",
    },
    "counter": {
        "prompt_tokens": "llamacpp:prompt_tokens_total",
        "gen_tokens": "llamacpp:predicted_tokens_total",
        "req_success": "llamacpp:requests_completed",
    },
    "hist": {},
}

TANDEMLLM = {  # TandemLLM-family engines (exl3 backend)
    "kind": "tandemllm",
    "prefix": "qwen38:",
    "gauge": {
        "running": "qwen38:requests_inflight",
    },
    "counter": {
        "prompt_tokens": "qwen38:prompt_tokens_total",
        "gen_tokens": "qwen38:completion_tokens_total",
        "req_success": "qwen38:requests_total",
    },
    "hist": {},
}

DIALECTS = {d["kind"]: d for d in (VLLM, SGLANG, LLAMACPP, TANDEMLLM)}


def detect(body: str) -> str | None:
    """Identify the engine kind from a raw /metrics body."""
    probe = body[:200_000]
    if "\nvllm:" in probe or probe.startswith("# HELP vllm:"):
        return "vllm"
    if "\nsglang:" in probe:
        return "sglang"
    if "\nllamacpp:" in probe:
        return "llamacpp"
    if "\nqwen38:" in probe or "\ntandemllm:" in probe:
        return "tandemllm"
    return None


def read_gauges(series: dict, dial: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for logical, metric in dial["gauge"].items():
        out[logical] = prom.total(series, metric)
    return out


def read_counters(series: dict, dial: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for logical, metric in dial["counter"].items():
        out[logical] = prom.total(series, metric)
    return out


def read_hists(series: dict, dial: dict) -> dict[str, tuple | None]:
    out: dict[str, tuple | None] = {}
    for logical, metric in dial["hist"].items():
        out[logical] = prom.histogram(series, metric)
    return out

"""Engine dialects: one logical schema, many Prometheus-flavoured engines.

Logical signal names consumed by the sampler and the UI:

    running            in-flight requests                gauge
    waiting            queued requests                   gauge
    kv_pct             KV/prefix pool utilisation 0-1    gauge
    prompt_tokens      cumulative prompt tokens          counter
    gen_tokens         cumulative generated tokens       counter
    prefill_tokens     tokens actively prefilled         counter (label-matched)
    prefill_seconds    cumulative prefill compute sec    counter
    decode_seconds     cumulative decode compute sec     counter
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

Map value forms (per logical):
    "metric_name"
    ["metric_name", "alt_name"]                 first non-null wins
    ["metric_name", {"mode": "prefill*"}]       label match; trailing * = prefix

Metric-name sources ported from TandemDash-Fork proxy/adapters/ (vllm, sglang,
exl3, tensorfold), whose family names were verified against each upstream's
metrics_collector. A missing signal reads None ("–") in the UI, never a wrong
number.
"""
from __future__ import annotations

from . import prom


def _get(series: dict, spec, kind: str):
    """Resolve one logical entry against a parsed exposition.

    kind: 'value' -> prom.total-style sum (counters/gauges),
          'hist'  -> prom.histogram tuple.
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        name, match, alts = spec, None, []
    else:
        name = spec[0]
        match = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else None
        alts = [x for x in spec[1:] if isinstance(x, str)]
    for nm in [name] + alts:
        if kind == "hist":
            h = prom.histogram(series, nm)
            if h is not None:
                return h
        elif match is None:
            v = prom.total(series, nm)
            if v is not None:
                return v
        else:
            # label-aware sum; trailing * in a match value = prefix match
            acc, seen = 0.0, False
            for _, labels, val in series.get(nm, ()):
                if all(_lab_ok(labels.get(k), v) for k, v in match.items()):
                    acc += val
                    seen = True
            if seen:
                return acc
    return None


def _lab_ok(actual, want) -> bool:
    if actual is None:
        return False
    if want.endswith("*"):
        return actual.startswith(want[:-1])
    return actual == want


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
        # cached_tokens is the prompt-token share served from cache. It MUST
        # stay a subset of prompt_tokens: the pricing split bills
        # (prompt - cached) at the write rate and cached at the read rate, and
        # every token TOTAL on the dash is prompt + gen. vLLM's
        # prompt_tokens_cached_total is the authoritative figure;
        # prefix_cache_hits_total is the fallback for older builds (it is
        # block-rounded, so it can sit a hair above the true cached share).
        "cached_tokens": ["vllm:prompt_tokens_cached_total",
                          "vllm:prefix_cache_hits_total"],
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
        # True end-to-end: vllm:e2e_request_latency_seconds covers the whole
        # request. request_inference_time_seconds is the RUNNING phase only and
        # silently drops queue time, so it under-reads whenever requests wait.
        "e2e": "vllm:e2e_request_latency_seconds",
    },
}

# sglang has no request_prefill_time hist and NO official prefill_throughput
# gauge; prefill speed comes from realtime_tokens_total{mode=prefill*} (which
# genuinely advances DURING prefill — the one engine with a true live signal)
# or Δprompt/Δprefill compute. Completion counting goes through the ttft
# histogram _count (verified vs sglang srt/observability/metrics_collector).
SGLANG = {
    "kind": "sglang",
    "prefix": "sglang:",
    "gauge": {
        "running": "sglang:num_running_reqs",
        "waiting": "sglang:num_queue_reqs",
        "kv_pct": "sglang:token_usage",
    },
    "counter": {
        "prompt_tokens": "sglang:prompt_tokens_total",
        "gen_tokens": "sglang:generation_tokens_total",
        "prefill_tokens": ["sglang:realtime_tokens_total", {"mode": "prefill*"}],
        "cached_tokens": "sglang:cached_tokens_total",
        "req_success": "sglang:time_to_first_token_seconds_count",
    },
    "hist": {
        "ttft": "sglang:time_to_first_token_seconds",
        "itl": "sglang:inter_token_latency_seconds",
        "e2e": "sglang:e2e_request_latency_seconds",
        "queue_time": "sglang:queue_time_seconds",
    },
}

# llama.cpp server metrics
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

# TandemLLM family: the engine serves its own dashboard contract; metrics are
# a small official set. qwen38: is the early-access namespace, tandemllm: the
# public one — both map identically.
# TandemLLM's native engine. Three name generations exist in the wild and all
# three must keep working, so every logical entry lists them in order (first
# non-null wins): `tandemllm:*` (the current upstream family), `qwen38:*` (the
# older family), and `qse_*` — the family the engine on this box actually
# emits, verified against its own `# HELP` text on 2026-10-24.
#
# ONE TRAP, recorded because it silently lies: the sampler's prefill truth is
#     (Δprompt_tokens − Δprefix_hits) / Δprefill_time
# and it needs prefix_hits in TOKENS (vLLM's prefix_cache_hits_total is a token
# count). `qse_cache_hits_total` counts LOOKUPS, not tokens — wiring it here
# would subtract a lookup count from a token count and inflate prefill speed.
# The token-count equivalent is `qse_prompt_tokens_cached_total` ("prompt
# tokens restored from the state store"), so THAT is prefix_hits.
# Consequence: `prefix_queries` has no single-counter counterpart (the engine
# splits lookups into hits+misses), so cache_hit_pct reads None ("–") rather
# than a fabricated ratio — the module's standing rule.
_TANDEM_MAP = {
    "gauge": {
        "running": ["tandemllm:requests_inflight", "qwen38:requests_inflight",
                    "qse_requests_running"],
        "waiting": ["tandemllm:requests_waiting", "qwen38:requests_waiting",
                    "qse_requests_waiting"],
    },
    "counter": {
        "prompt_tokens": ["tandemllm:prompt_tokens_total", "qwen38:prompt_tokens_total",
                          "qse_prompt_tokens_total"],
        "gen_tokens": ["tandemllm:completion_tokens_total", "qwen38:completion_tokens_total",
                       "qse_generation_tokens_total"],
        "req_success": ["tandemllm:requests_total", "qwen38:requests_total",
                        "qse_request_success_total"],
        # tokens genuinely run through the layers (excludes cache-restored), so
        # this is the sampler's engine-native live-prefill path
        "prefill_tokens": ["qse_prefill_tokens_forwarded_total"],
        # TOKENS restored from cache — see the trap note above
        "prefix_hits": ["qse_prompt_tokens_cached_total"],
        "draft_tokens": ["qse_spec_decode_num_draft_tokens_total"],
        "accepted_tokens": ["qse_spec_decode_num_accepted_tokens_total"],
    },
    "hist": {
        "ttft": ["qse_time_to_first_token_seconds"],
        "itl": ["qse_time_per_output_token_seconds"],
        "e2e": ["qse_e2e_request_latency_seconds"],
        "queue_time": ["qse_request_queue_seconds"],
        "prefill_time": ["qse_request_prefill_seconds"],
    },
}
TANDEMLLM = {"kind": "tandemllm", "prefix": "tandemllm:", **_TANDEM_MAP}

# exl3 (exllamav3 worker metrics). Unique win: prefill_seconds and
# generate_seconds are COUNTERS — prefill speed = Δprompt/Δprefill_seconds
# pairs perfectly (both completion-charged counters, no histogram needed).
# decTps gauge is ABSENT when idle by design; kv_used/kv_capacity give pool
# occupancy only when a worker reports them.
EXL3 = {
    "kind": "exl3",
    "prefix": "exl3_",
    "gauge": {
        "running": "exl3_requests_running",
        "waiting": "exl3_requests_waiting",
    },
    "counter": {
        "prompt_tokens": "exl3_prompt_tokens_total",
        "gen_tokens": "exl3_generation_tokens_total",
        "req_success": "exl3_requests_total",
        "prefill_seconds": "exl3_prefill_seconds_total",
        "decode_seconds": "exl3_generate_seconds_total",
        "accepted_tokens": "exl3_accepted_draft_tokens_total",
    },
    "hist": {},
}

# TensorFold: dual-name families (mtp_* canonical, spec_decode_* alt mirrors).
TENSORFOLD = {
    "kind": "tensorfold",
    "prefix": "tensorfold:",
    "gauge": {
        "running": ["tensorfold:requests_running", "tensorfold:num_requests_running"],
        "waiting": ["tensorfold:requests_waiting", "tensorfold:num_requests_waiting"],
        "kv_pct": "tensorfold:kv_cache_usage_perc",
    },
    "counter": {
        "prompt_tokens": "tensorfold:prompt_tokens_total",
        "gen_tokens": ["tensorfold:generation_tokens_total"],
        "draft_tokens": ["tensorfold:mtp_drafted_total",
                         "tensorfold:spec_decode_num_draft_tokens_total"],
        "accepted_tokens": ["tensorfold:mtp_accepted_total",
                            "tensorfold:spec_decode_num_accepted_tokens_total"],
        "req_success": "tensorfold:requests_total",
    },
    "hist": {
        "ttft": "tensorfold:time_to_first_token_seconds",
        "e2e": "tensorfold:e2e_request_latency_seconds",
        "itl": "tensorfold:request_time_per_output_token_seconds",
        "prefill_time": ["tensorfold:request_prefill_seconds",
                         "tensorfold:request_prefill_time_seconds"],
        "decode_time": ["tensorfold:request_decode_seconds",
                        "tensorfold:request_decode_time_seconds"],
    },
}

DIALECTS = {d["kind"]: d for d in (VLLM, SGLANG, LLAMACPP, TANDEMLLM, EXL3, TENSORFOLD)}


def detect(body: str) -> str | None:
    """Identify the engine kind from a raw /metrics body."""
    probe = body[:200_000]
    if "\nvllm:" in probe or probe.startswith("# HELP vllm:"):
        return "vllm"
    if "\nsglang:" in probe:
        return "sglang"
    if "\nllamacpp:" in probe:
        return "llamacpp"
    if "\nexl3_" in probe or "\nexl3:" in probe:
        return "exl3"
    if "\ntensorfold:" in probe:
        return "tensorfold"
    if "\nqwen38:" in probe or "\ntandemllm:" in probe:
        return "tandemllm"
    # The native engine's current family (verified 2026-10-24 against its own
    # # HELP text). Deliberately checked LAST and only on two distinctive
    # family names: a body that matches any family above still resolves to it
    # exactly as before, so adding this cannot reclassify another engine.
    if "\nqse_requests_running" in probe or "\nqse_generation_tokens_total" in probe:
        return "tandemllm"
    return None


def read_gauges(series: dict, dial: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for logical, spec in dial.get("gauge", {}).items():
        out[logical] = _get(series, spec, "value")
    return out


def read_counters(series: dict, dial: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for logical, spec in dial.get("counter", {}).items():
        out[logical] = _get(series, spec, "value")
    return out


def read_hists(series: dict, dial: dict) -> dict[str, tuple | None]:
    out: dict[str, tuple | None] = {}
    for logical, spec in dial.get("hist", {}).items():
        out[logical] = _get(series, spec, "hist")
    return out

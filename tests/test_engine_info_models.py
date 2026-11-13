"""engine_info.info() must learn the served model from BOTH /v1/models shapes.

Measured 2026-11-13: a llama-server fork (LFM3-Audio-2B on :8181) answers
/v1/models Ollama-style {"models":[{"name":...}]} while vLLM/TandemLLM and
standard llama.cpp answer OpenAI-style {"data":[{"id":...}]}. A None model is
not cosmetic: pricing.resolve(None) falls through to the pricing.json DEFAULT
rates, so the engine's minutes get silently priced as whatever the fallback
models — wrong money on the ledger.
"""
import io
import json
import urllib.request

from app import engine_info

LCPP_METRICS = (
    "# HELP llamacpp:prompt_tokens_total Prompt tokens processed\n"
    "llamacpp:prompt_tokens_total 47376\n"
    "llamacpp:prompt_tokens_cached_total 320\n"
    "llamacpp:tokens_predicted_total 2933\n"
)


def _fake_open(responses):
    def fake(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        for frag, payload in responses.items():
            if url.endswith(frag):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return io.BytesIO(body)
        raise OSError(f"unexpected url {url}")
    return fake


def _info(responses):
    orig = urllib.request.urlopen
    urllib.request.urlopen = _fake_open(responses)
    try:
        return engine_info.info("http://127.0.0.1:8181")
    finally:
        urllib.request.urlopen = orig


def test_ollama_style_models_shape():
    out = _info({
        "/metrics": LCPP_METRICS,
        "/v1/models": {"models": [{"name": "LFM3-Audio-2B", "model": "LFM3-Audio-2B"}]},
    })
    assert out["healthy"] and out["model"] == "LFM3-Audio-2B"


def test_openai_style_data_shape_still_works():
    out = _info({
        "/metrics": LCPP_METRICS,
        "/v1/models": {"object": "list", "data": [{"id": "Qwen/Qwen3.8-Flash-Next"}]},
    })
    assert out["healthy"] and out["model"] == "Qwen/Qwen3.8-Flash-Next"


def test_model_key_fallback_and_empty_shapes():
    out = _info({
        "/metrics": LCPP_METRICS,
        "/v1/models": {"models": [{"model": "only-model-key"}]},
    })
    assert out["model"] == "only-model-key"
    out = _info({"/metrics": LCPP_METRICS, "/v1/models": {"models": []}})
    assert out["healthy"] and out["model"] is None   # fail-safe, never a guess


def test_models_endpoint_failure_is_silent():
    out = _info({"/metrics": LCPP_METRICS})   # no /v1/models fixture -> OSError
    assert out["healthy"] and out["model"] is None


# ---------------------------------------------------------------------------
# The REAL consumer: sampler._refresh_model sets the snapshot model and the
# counter-bucket `id@model` history key. It must accept both shapes too.

def test_sampler_refresh_model_ollama_shape():
    from app.sampler import EngineSampler
    from app.sse import Broker
    s = EngineSampler({"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900},
                      {"id": "lfm3-audio", "url": "http://x", "kind": "llamacpp"},
                      Broker(), store=None)
    s._fetch = lambda path, timeout=1.5: json.dumps(
        {"models": [{"name": "LFM3-Audio-2B", "model": "LFM3-Audio-2B"}]}).encode()
    s._refresh_model(1000.0)
    assert s.model == "LFM3-Audio-2B"


def test_sampler_refresh_model_openai_shape_still_works():
    from app.sampler import EngineSampler
    from app.sse import Broker
    s = EngineSampler({"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900},
                      {"id": "e", "url": "http://x", "kind": "vllm"},
                      Broker(), store=None)
    s._fetch = lambda path, timeout=1.5: json.dumps(
        {"object": "list", "data": [{"id": "Qwen/Qwen3.8-Flash-Next"}]}).encode()
    s._refresh_model(1000.0)
    assert s.model == "Qwen/Qwen3.8-Flash-Next"
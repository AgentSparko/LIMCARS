"""Unit tests for the pure parts: parser, dialect detect, diff math, SSE."""
import time

from app import prom
from app import dialects
from app.sse import Broker


VLLM_BODY = """# HELP vllm:num_requests_running Multi-process status
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3.8-Flash-Next"} 2.0
vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen3.8-Flash-Next"} 1.0
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen/Qwen3.8-Flash-Next"} 0.25
vllm:prompt_tokens_total{engine="0",model_name="Qwen/Qwen3.8-Flash-Next"} 1000.0
vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen3.8-Flash-Next"} 500.0
vllm:prefix_cache_queries_total{engine="0",model_name="m"} 100.0
vllm:prefix_cache_hits_total{engine="0",model_name="m"} 75.0
vllm:time_to_first_token_seconds_bucket{le="0.1"} 1.0
vllm:time_to_first_token_seconds_bucket{le="0.5"} 3.0
vllm:time_to_first_token_seconds_bucket{le="+Inf"} 4.0
vllm:time_to_first_token_seconds_sum 2.0
vllm:time_to_first_token_seconds_count 4.0
broken metric nan-ish
"""


def test_parse_and_queries():
    s = prom.parse(VLLM_BODY)
    assert prom.first(s, "vllm:num_requests_running") == 2.0
    assert prom.total(s, "vllm:prefix_cache_hits_total") == 75.0
    h = prom.histogram(s, "vllm:time_to_first_token_seconds")
    assert h is not None
    buckets, sum_, count = h
    assert buckets[-1][0] == float("inf") and buckets[-1][1] == 4.0
    assert count == 4.0


def test_quantile():
    buckets = [(0.1, 1.0), (0.5, 3.0), (float("inf"), 4.0)]
    p50 = prom.quantile(buckets, 0.5)
    assert p50 is not None and 0.1 < p50 <= 0.5  # between 2nd and 3rd bucket-ish
    assert prom.quantile([], 0.5) is None
    # q=0 lands in the first bucket (c==prev_c branch → its upper bound)
    assert prom.quantile([(0.1, 0.0), (0.2, 10.0), (float("inf"), 10.0)], 0.0) == 0.1


def test_detect():
    assert dialects.detect(VLLM_BODY) == "vllm"
    assert dialects.detect("# x\nsglang:num_running_reqs 3") == "sglang"
    assert dialects.detect("# x\nllamacpp:requests_processing 1") == "llamacpp"
    assert dialects.detect("nothing here") is None


def test_labels_with_commas_and_quotes():
    s = prom.parse('m{a="x,y",b="say \\"hi\\""} 5')
    _, labels, v = s["m"][0]
    assert labels["a"] == "x,y" and labels["b"] == 'say "hi"' and v == 5.0


def test_sse_broker_replay_and_drop():
    from app.sse import Topic
    import queue as _q
    t = Topic("x", replay=3)
    for i in range(5):
        t.publish(f"d{i}")
    q, backlog = t.subscribe(since=2)  # seqs 1..5; ring maxlen 3 → 3,4,5 replay
    assert [d for _, d in backlog] == ["d2", "d3", "d4"]
    t.publish("live")
    assert q.get_nowait() == (6, "live")
    # slow consumer: subscriber queue is full → oldest unread dropped,
    # the newest event ALWAYS lands
    q.put_nowait(("x0", "pad1"))
    q.put_nowait(("x1", "pad2"))
    # shrink the queue to force "full" without 2000 publishes
    q.maxsize = 2
    sid = t.publish("fresh")
    vals = []
    while not q.empty():
        vals.append(q.get_nowait())
    assert ("x1", "pad2") in vals          # one pad survived…
    assert vals[-1] == (sid, "fresh")      # …and the newest always ships

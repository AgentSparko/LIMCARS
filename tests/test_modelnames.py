"""One model under different names keeps one history (engines, quants, org prefixes)."""
import json
import time

from app import modelnames as M
from app.sampler import EngineSampler
from app.sse import Broker


def test_same_model_across_engines_quants_and_orgs():
    assert M.same_model("qwen3.8-flash-next", "Qwen3.8-Flash-Next")
    assert M.same_model("qwen3.8-flash-next", "Qwen/Qwen3.8-Flash-Next-FP4-Local")
    assert M.same_model("turboderp/Qwen3.8-Flash-Next-exl3-3.05bpw", "Qwen3.8-Flash-Next")
    assert M.same_model("qwen3.8-27b", "Qwen/Qwen3.8-27B-FP8")


def test_different_models_and_versions_do_not_match():
    assert not M.same_model("qwen3.8-flash-next", "qwen3.8-27b-tandem")
    assert not M.same_model("qwen3.8-flash-next", "qwen3.9-flash-next")
    assert not M.same_model("qwen3.8-flash-next", "dsv41")
    assert not M.same_model("", "dsv41")


def test_pick_prefers_the_recorded_spelling():
    known = ["dsv41", "qwen3.8-27b-tandem", "Qwen3.8-Flash-Next"]
    assert M.pick("qwen3.8-flash-next", known) == "Qwen3.8-Flash-Next"
    assert M.pick("brand-new-model-x", known) == "brand-new-model-x"


def _mk(served, store=None):
    s = EngineSampler({"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900},
                      {"id": "auto-8020", "url": "http://x", "kind": "vllm"}, Broker(), store=store)
    s._fetch = lambda path, timeout=1.5: json.dumps({"data": [served]}).encode()
    return s


def test_tandemllm_is_recorded_under_the_model_it_actually_runs(tmp_path):
    from app.store import Store
    st = Store(str(tmp_path / "s.db"))
    b = {"prompt": 1, "cached": 0, "gen": 1, "reqs": 1, "iters": 1, "dec_min": None,
         "dec_peak": None, "spec_sum": 0, "spec_n": 0}
    st.save_counter_bucket("flashnext@Qwen3.8-Flash-Next", int(time.time()) // 60 * 60, b)
    s = _mk({"id": "qwen3.8-27b-tandem",
             "tandem": {"model": "qwen3.8-flash-next", "label": "Qwen3.8-Flash-Next-EXL3", "loaded": True}}, st)
    s._refresh_model(1000.0)
    assert s.model == "Qwen3.8-Flash-Next" and s.skey == "auto-8020@Qwen3.8-Flash-Next"


def test_plain_engines_keep_their_served_id():
    s = _mk({"id": "dsv41"})
    s._refresh_model(1000.0)
    assert s.model == "dsv41"


def test_savings_chart_merges_one_model_served_by_two_engines(tmp_path):
    from app.counters import daily_savings
    from app.store import Store
    st = Store(str(tmp_path / "s.db"))
    now = int(time.time()) // 60 * 60
    b = {"prompt": 1000, "cached": 0, "gen": 100, "reqs": 1, "iters": 1, "dec_min": None,
         "dec_peak": None, "spec_sum": 0, "spec_n": 0}
    st.save_counter_bucket("flashnext", now, b)
    st.save_counter_bucket("auto-8020@Qwen3.8-Flash-Next", now, b)
    price = {"input": 1.0, "output": 1.0, "cache_read": 0.1}
    engines = [{"id": "flashnext", "model": "Qwen3.8-Flash-Next", "group": "model:fn", "group_label": "Qwen3.8-Flash-Next"},
               {"id": "auto-8020@Qwen3.8-Flash-Next", "model": "Qwen3.8-Flash-Next", "group": "model:fn",
                "group_label": "Qwen3.8-Flash-Next"}]
    r = daily_savings(st, engines, price, 7)
    assert r["models"] == {"model:fn": "Qwen3.8-Flash-Next"}
    day = r["days"][-1]["engines"]
    assert list(day) == ["model:fn"] and day["model:fn"]["tokens"] == 2200

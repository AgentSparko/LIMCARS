"""Server config panel — old LIMCARS /v27/dash/info parity, vLLM edition.

Reads what the engine actually publishes: cache_config_info labels (KV pool
size, block size, gpu blocks), served model id, version endpoint, and the
speculative counters (draft tokens / drafts ratio ⇒ effective K). Nothing is
guessed; anything the engine doesn't expose comes back None → UI shows '–'.
"""
from __future__ import annotations

import json
import re
import urllib.request


def _labels(text: str, metric: str) -> dict:
    m = re.search(r"^" + re.escape(metric) + r"\{([^}]*)\}", text, re.M)
    if not m:
        return {}
    return dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))


def _val(text: str, metric: str) -> float | None:
    m = re.search(r"^" + re.escape(metric) + r"(?:\{[^}]*\})?\s+([\d.eE+-]+)", text, re.M)
    return float(m.group(1)) if m else None


def _launch_cmd(port: int | None) -> str | None:
    """Full launch 'recipe' = the serving process command line, read from
    /proc (same-user). Match: mentions vllm AND the engine's --port."""
    import glob
    best = None
    for cl in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            raw = open(cl, "rb").read()
        except OSError:
            continue
        if b"vllm" not in raw:
            continue
        parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
        if not parts or "vllm" not in " ".join(parts[:3]):
            continue
        joined = " ".join(parts)
        if port and f"--port {port}" not in joined and f"--port={port}" not in joined:
            continue
        best = joined
        if port:
            break
    return best


def info(url: str) -> dict:
    out: dict = {"healthy": False, "url": url}
    try:
        text = urllib.request.urlopen(url + "/metrics", timeout=5).read().decode()
        labels = _labels(text, "vllm:cache_config_info")
        drafts = _val(text, "vllm:spec_decode_num_drafts_total") or 0
        dtok = _val(text, "vllm:spec_decode_num_draft_tokens_total") or 0
        atok = _val(text, "vllm:spec_decode_num_accepted_tokens_total") or 0
        model = None
        try:
            md = json.loads(urllib.request.urlopen(url + "/v1/models", timeout=5).read())
            model = (md.get("data") or [{}])[0].get("id")
        except Exception:  # noqa: BLE001
            pass
        ver = None
        try:
            body = urllib.request.urlopen(url + "/version", timeout=3).read().decode()
            try:
                ver = json.loads(body).get("version", body[:40])
            except ValueError:
                ver = body[:40]
        except Exception:  # noqa: BLE001
            pass
        out.update({
            "healthy": True, "model": model, "version": ver,
            "launch": _launch_cmd(int(url.rsplit(":", 1)[-1].rstrip("/")) if url.rsplit(":", 1)[-1].rstrip("/").isdigit() else None),
            "config": {k: v for k, v in labels.items() if k not in ("model_name", "created")},
            "ctx": int(float(labels.get("max_model_len", 0))) or None,
            "pool_tokens": int(float(labels["kv_cache_size_tokens"])) if labels.get("kv_cache_size_tokens") else None,
            "block_size": int(float(labels["block_size"])) if labels.get("block_size") else None,
            "num_gpu_blocks": int(float(labels["num_gpu_blocks"])) if labels.get("num_gpu_blocks") else None,
            "kv_dtype": labels.get("cache_dtype"),
            "spec": "on (DFlash)" if drafts else "off",
            "draft_k": round(dtok / drafts, 1) if drafts else None,
            "accept_rate": round(atok / dtok * 100, 1) if dtok else None,
        })
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    return out

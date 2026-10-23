"""Engine probe — the Classic dash "⚡ Ping engine" button.

Fires ONE streamed chat completion at the engine and measures what the
engine truly does, not what counters average over half a minute:

    ttft_ms            request send → first content token
    pp_tps             prompt-processing speed (prompt tokens / prefill time)
    tg_tps             generation speed (completion tokens / decode window)
    prompt_tokens      real prompt size (usage from the stream, or estimate)
    completion_tokens  as above

The prompt is ~1.3k tokens of repetitive-but-unique filler so prefix cache
does not eat it (each request gets a fresh nonce prefix).
"""
from __future__ import annotations

import json
import time
import urllib.request


def probe(url: str, model: str | None, max_tokens: int = 220,
          prompt_words: int = 1300, timeout: float = 90.0) -> dict:
    nonce = f"{int(time.time())}-{int(time.time() * 1000) % 9973}-"
    prompt = nonce + " ".join(f"ocean{time.time_ns() % 99991}{i}"
                              for i in range(prompt_words))
    body = json.dumps({
        "model": model or "default",
        "messages": [{"role": "user", "content": prompt
                      + " Reply with OK, then describe a coral reef in about 80 words."}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "limcars-next-probe/1.0"})
    t0 = time.perf_counter()
    t_first = t_last = None
    usage = {}
    content_chars = 0
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                # count EVERY decode delta — this vLLM streams thinking under
                # delta.reasoning (NOT reasoning_content), and thinking-only
                # answers would otherwise look like "no content received"
                if d.get("content") or d.get("reasoning") or d.get("reasoning_content"):
                    now = time.perf_counter()
                    if t_first is None:
                        t_first = now
                    t_last = now
                    content_chars += 1
    if t_first is None:
        return {"ok": False, "error": "no content tokens received"}
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens") or content_chars or None
    ttft = (t_first - t0) * 1000
    decode_window = (t_last - t_first) if t_last and t_last > t_first else None
    return {
        "ok": True,
        "at": time.time(),
        "model": model,
        "ttft_ms": round(ttft, 1),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "pp_tps": round(pt / (ttft / 1000), 1) if pt and ttft > 0 else None,
        # single-token answers have no measurable window — report rate over
        # the whole decode stretch rather than returning nothing
        "tg_tps": (round((ct - 1) / decode_window, 1) if ct and decode_window
                   else (round(ct / max(0.001, (t_last or t_first) - t0), 1) if ct else None)),
    }

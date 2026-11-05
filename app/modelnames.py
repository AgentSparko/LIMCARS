"""One model served under different names: by another engine, quant, org prefix or build tag.

LIMCARS keeps token history per (engine, served model). When a model's name is new, the
sampler matches it against the names already in the history, so "Qwen/Qwen3.8-Flash-Next-FP4"
on one engine and "qwen3.8-flash-next" on another count as the same model. Version digits
stay significant: "qwen3.8" never matches "qwen3.9"."""
from __future__ import annotations

import re

_VARIANT = re.compile(r"nvfp4|mxfp4|fp4|fp8|int4|int8|w4a16|w8a8|awq|gptq|gguf|rexl3|exl3|exl2|"
                      r"bf16|fp16|\d+(?:p\d+)?bpw|tandem|instruct|local|hybrid\d*")


def core(name: str | None) -> str:
    """The model's identity: last path part, lowercase, alphanumerics only, variant tags removed."""
    n = re.sub(r"[^a-z0-9.]", "", (name or "").split("/")[-1].lower())
    n = re.sub(r"(\d)\.(\d)", r"\1p\2", n)        # 3.05bpw -> 3p05bpw, so the tag strips whole
    return _VARIANT.sub("", n)


def _lcs(a: str, b: str) -> int:
    best, prev = 0, [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def overlap(a: str | None, b: str | None) -> int:
    """Length of the shared name part when a and b name the same model, else 0."""
    x, y = core(a), core(b)
    if not x or not y:
        return 0
    if x == y:
        return len(x) + 1000                       # exact identity outranks any partial match
    n = _lcs(x, y)
    if n >= 8 and n >= 0.75 * min(len(x), len(y)) and n >= 0.6 * max(len(x), len(y)):
        return n
    return 0


def same_model(a: str | None, b: str | None) -> bool:
    return overlap(a, b) > 0


def pick(name: str | None, known) -> str | None:
    """`name`, or the already-recorded name of the same model (the closest match)."""
    if not name:
        return name
    best, score = name, 0
    for k in known:
        s = overlap(name, k) if k else 0
        if s > score:
            best, score = k, s
    return best

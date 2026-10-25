"""Cloud-API price resolution — ported from limcars server.js pricing logic.

Source of truth: data/pricing.json (USD per 1M tokens, OpenRouter-derived).
The served model id (e.g. 'Qwen/Qwen3.8-Flash-Next') is matched to a pricing
slug by alphanumeric normalization, trying the model id, its last path
segment, and each entry's openrouter_id. No match -> 'default' fallback
block; 'n/a' if even that is missing.

Basis (identical to the old Classic bubbles): uncached prefill is billed at
the cache-WRITE rate, cached tokens at the cache-READ rate, generation at the
OUTPUT rate ("hot tier" — we keep the KV cache warm, the API charges a
cheaper read for it).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
FILE = DATA / "pricing.json"

_cache: dict | None = None
_cache_mtime: float = 0.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load() -> dict:
    """Reload if the file changed on disk (editing pricing.json needs no restart)."""
    global _cache, _cache_mtime
    try:
        mt = FILE.stat().st_mtime
    except OSError:
        return {}
    if _cache is None or mt != _cache_mtime:
        # audit: a half-written/invalid pricing.json used to raise through
        # every $ endpoint AND the sampler's blanket except (fake engine_down
        # + baseline wipe). Keep serving the last good copy instead.
        try:
            _cache = json.loads(FILE.read_text())
            _cache_mtime = mt
        except (OSError, ValueError):
            return _cache if _cache is not None else {}
    return _cache


def resolve(model: str | None) -> dict | None:
    """Return {display_name, provider, source, input, output, cache_write,
    cache_read, slug} in USD per 1M tokens, or None if nothing matches."""
    data = load()
    if not data:
        return None
    models = data.get("models") or {}
    keys = [_norm(model or ""), _norm((model or "").split("/")[-1])]
    entry = None
    slug_hit = "default"
    for k in keys:
        if not k:
            continue
        for slug, e in models.items():
            if _norm(slug) == k or _norm(e.get("openrouter_id", "")) == k:
                entry, slug_hit = e, slug
                break
        if entry:
            break
    if not entry:
        # token-level contains match, e.g. 'qwen38flash' inside 'qwen38flashnext'
        for slug, e in models.items():
            ks = _norm(slug)
            for k in keys:
                if k and (k in ks or ks in k):
                    entry, slug_hit = e, slug
                    break
            if entry:
                break
    if not entry:
        d = data.get("default")
        if not d:
            return None
        entry, slug_hit = d, "default"
    cw = entry.get("cache_write_per_M")
    cr = entry.get("cache_read_per_M")
    return {
        "slug": slug_hit,
        "display_name": entry.get("display_name", slug_hit),
        "provider": entry.get("provider", ""),
        "source": entry.get("source_url", ""),
        "input": entry.get("input_per_M"),
        "output": entry.get("output_per_M", 0) or 0,
        # hot-tier basis falls back to input price when write/read are unknown
        "cache_write": cw if cw is not None else entry.get("input_per_M", 0),
        "cache_read": cr if cr is not None else 0.0,
        "fetched_at": data.get("fetched_at"),
    }


def effective_price(model: str | None, override: dict | None = None) -> dict | None:
    """Single price source of truth: manual override (from /api/config) wins,
    else data/pricing.json resolution. Savings bubbles AND token counters
    MUST both use this so their numbers agree."""
    if override and (override.get("output") or override.get("cache_write")
                     or override.get("input")):
        p = dict(override)
        p.setdefault("slug", "manual")
        p.setdefault("display_name", p.get("label") or "manual override")
        return p
    return resolve(model)


def hot_costs(prompt: float | None, cached: float | None, gen: float | None,
              price: dict) -> dict:
    """Canonical cloud-API cost trio (old LIMCARS basis):
    uncached@cache-WRITE + cached@cache-READ(_HOT) + gen@OUTPUT."""
    _g = lambda k: (lambda v: 0.0 if v is None else float(v))(price.get(k))
    p_in, p_out = _g("input"), _g("output")
    p_r = _g("cache_read")
    cw = price.get("cache_write")
    p_w = float(cw) if cw is not None else (p_in or 0.0)   # 0 is a real price
    crh = price.get("cache_read_hot")
    p_rhot = float(crh) if crh is not None else p_r
    # the dialect contract is that `cached` is a SUBSET of `prompt` (vLLM's
    # prompt_tokens_cached_total / SGLang's cached_tokens_total are shares of
    # the prompt, not extra tokens). Clamp it: an engine whose cached counter is
    # block-rounded (prefix_cache_hits_total on older vLLM builds) could
    # otherwise bill cache-read tokens that were never prompted, on top of the
    # write rate for the whole prompt.
    prompt = max(0.0, float(prompt or 0.0))
    cached = max(0.0, min(float(cached or 0.0), prompt))
    gen = max(0.0, float(gen or 0.0))
    uncached = prompt - cached
    return {
        "hot": uncached / 1e6 * p_w + cached / 1e6 * p_rhot + gen / 1e6 * p_out,
        "base": uncached / 1e6 * p_w + cached / 1e6 * p_r + gen / 1e6 * p_out,
        "nocache": prompt / 1e6 * p_in + gen / 1e6 * p_out,
    }

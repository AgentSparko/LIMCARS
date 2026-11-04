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
import time
from pathlib import Path

from . import config as _config

DATA = _config.data_dir()
FILE = DATA / "pricing.json"

_cache: dict | None = None
_cache_mtime: float = 0.0


_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PRICE_KEYS = ("input_per_M", "output_per_M", "cache_write_per_M", "cache_read_per_M")


def _hm(s: str) -> int:
    h, m = str(s).strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60 and h * 60 + m <= 1440):
        raise ValueError(s)
    return h * 60 + m


def _week_min(s: str) -> int:
    d, t = str(s).strip().split()
    return _DAYS.index(d[:3].lower()) * 1440 + _hm(t)


def _within(x: int, lo: int, hi: int) -> bool:
    return lo <= x < hi if lo <= hi else (x >= lo or x < hi)   # wraps midnight / Sunday


def in_peak(peak: dict | None, at: float) -> bool:
    """Peak pricing applies at ``at`` (epoch s)? All windows are UTC. ``hours_utc`` are
    daily peak windows [["01:00","04:00"], ...]; ``offpeak_utc`` are weekly exceptions
    that are off-peak whatever the hour [["Fri 16:00","Sun 16:00"]]."""
    if not peak:
        return False
    tm = time.gmtime(at)
    wm = tm.tm_wday * 1440 + tm.tm_hour * 60 + tm.tm_min
    for a, b in peak.get("offpeak_utc") or []:
        if _within(wm, _week_min(a), _week_min(b)):
            return False
    return any(_within(wm % 1440, _hm(a), _hm(b)) for a, b in peak.get("hours_utc") or [])


def validate_entry(e) -> str | None:
    """None if ``e`` is a storable model price entry, else the reason."""
    if not isinstance(e, dict):
        return "entry must be an object"
    for k in ("display_name", "provider", "source_url", "openrouter_id"):
        if k in e and e[k] is not None and not isinstance(e[k], str):
            return f"{k} must be a string"
    al = e.get("aliases")
    if al is not None and (not isinstance(al, list) or not all(isinstance(x, str) for x in al)):
        return "aliases must be a list of strings"

    def prices(d, where):
        for k in PRICE_KEYS:
            v = d.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                return f"{where}{k} must be a number >= 0"
        return None
    err = prices(e, "")
    if err:
        return err
    if not any(isinstance(e.get(k), (int, float)) and e.get(k) > 0
               for k in ("output_per_M", "cache_write_per_M", "input_per_M")):
        return "need an input, cache-write or output price above 0"
    pk = e.get("peak")
    if pk is not None:
        if not isinstance(pk, dict):
            return "peak must be an object"
        err = prices(pk, "peak.")
        if err:
            return err
        try:
            for a, b in pk.get("hours_utc") or []:
                _hm(a), _hm(b)
            for a, b in pk.get("offpeak_utc") or []:
                _week_min(a), _week_min(b)
        except (ValueError, TypeError):
            return "peak hours must be [\"HH:MM\",\"HH:MM\"] and off-peak windows [\"Fri 16:00\",\"Sun 16:00\"] (UTC)"
        if not pk.get("hours_utc"):
            return "peak needs at least one hours_utc window"
    return None


def save_model(slug: str, entry: dict | None) -> dict:
    """Add, replace (keys merged onto the existing entry) or delete (entry None) one model
    in pricing.json, written atomically. Returns the new models map."""
    global _cache, _cache_mtime
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    models = data.setdefault("models", {})
    if entry is None:
        models.pop(slug, None)
    else:
        cur = dict(models.get(slug) or {})
        cur.update(entry)
        if entry.get("peak") is None:
            cur.pop("peak", None)
        models[slug] = cur
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(FILE)
    _cache, _cache_mtime = None, 0.0
    return models


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
            _cache = json.loads(FILE.read_text(encoding="utf-8"))
            _cache_mtime = mt
        except (OSError, ValueError):
            return _cache if _cache is not None else {}
    return _cache


def resolve(model: str | None, at: float | None = None) -> dict | None:
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
            if (_norm(slug) == k or _norm(e.get("openrouter_id", "")) == k
                    or any(_norm(x) == k for x in e.get("aliases") or [])):
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
    period = None
    name = entry.get("display_name", slug_hit)
    if entry.get("peak"):
        period = "offpeak"
        try:
            hot = in_peak(entry["peak"], time.time() if at is None else at)
        except (ValueError, TypeError):
            hot = False
        if hot:
            period = "peak"
            entry = {**entry, **{k: v for k, v in entry["peak"].items()
                                 if k in PRICE_KEYS and v is not None}}
            slug_hit, name = f"{slug_hit}@peak", f"{name} (peak)"
    cw = entry.get("cache_write_per_M")
    cr = entry.get("cache_read_per_M")
    return {
        "slug": slug_hit,
        "period": period,
        "display_name": name,
        "provider": entry.get("provider", ""),
        "source": entry.get("source_url", ""),
        "input": entry.get("input_per_M"),
        "output": entry.get("output_per_M", 0) or 0,
        # hot-tier basis falls back to input price when write/read are unknown
        "cache_write": cw if cw is not None else entry.get("input_per_M", 0),
        "cache_read": cr if cr is not None else 0.0,
        "fetched_at": data.get("fetched_at"),
    }


def effective_price(model: str | None, override: dict | None = None,
                    at: float | None = None) -> dict | None:
    """Single price source of truth: manual override (from /api/config) wins,
    else data/pricing.json resolution. Savings bubbles AND token counters
    MUST both use this so their numbers agree."""
    if override and (override.get("output") or override.get("cache_write")
                     or override.get("input")):
        p = dict(override)
        p.setdefault("slug", "manual")
        p.setdefault("display_name", p.get("label") or "manual override")
        return p
    return resolve(model) if at is None else resolve(model, at)


def money_stamped(prompt: float | None, cached: float | None, gen: float | None,
                  price: dict | None, *, src: str = "live") -> dict:
    """Costs PLUS the price identity that produced them, for persistence.

    Cloud prices move under us: pricing.json is refreshed by the fetcher and a user can edit
    it by hand, and until now every $ figure was recomputed from whatever the rate happened to
    be at read time. That means last March's savings silently changed when a provider cut a
    price. A bucket is a historical fact, so the money is frozen here, together with the rates
    used, so any figure can be audited ("$12.34 at slug qwen38flash, input 0.25, read 0.03")
    instead of being a function of today's file.

    Returns the three canonical costs plus provenance. Nothing here guesses: when no price is
    available the caller must store NULL (unknown), not 0.0 — a fabricated zero would look
    exactly like a genuinely free hour.
    """
    p = price or {}
    if not p:
        return {}
    c = hot_costs(prompt, cached, gen, p)
    return {
        "cost_hot": round(c["hot"], 6),
        "cost_base": round(c["base"], 6),
        "cost_nocache": round(c["nocache"], 6),
        "price_slug": p.get("slug"),
        "price_in": p.get("input"),
        "price_out": p.get("output"),
        "price_cw": p.get("cache_write"),
        "price_cr": p.get("cache_read"),
        "price_crhot": p.get("cache_read_hot"),
        "priced_at": time.time(),
        "price_src": src,
    }


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

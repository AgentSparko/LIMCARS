"""Tiny Prometheus text-format parser.

Single pass over the /metrics body, regex-free hot loop, returns:

    samples  {(name, label_key_tuple_or_None): (value, labels_dict)}  -> last wins
    families gathered lazily by callers through helper queries.

Design notes (why not prometheus_client): we poll up to 2 Hz from a
sampler thread; this parse costs ~1 ms for a vLLM body (~2 MB worst case)
and allocates once per line. No dependency, no registry, no global state.
"""
from __future__ import annotations

Line = tuple[str, dict[str, str], float]  # (metric_name, labels, value)


def parse(text: str) -> dict[str, list[Line]]:
    """Parse a Prometheus exposition into {metric_name: [(name, labels, value), ...]}.

    Comments (#) and blank lines are skipped. Values that fail to float()
    are dropped (a half-written metric must never crash the sampler).
    """
    out: dict[str, list[Line]] = {}
    for raw in text.split("\n"):
        if not raw or raw[0] == "#":
            continue
        # split value: last whitespace-separated token
        sp = raw.rfind(" ")
        if sp < 0:
            continue
        val_s = raw[sp + 1:]
        head = raw[:sp]
        try:
            val = float(val_s)
        except ValueError:
            continue
        if val != val or val in (float("inf"), float("-inf")):
            # audit: NaN/Inf in an engine metric would poison json.dumps
            # (allow_nan=False) and 500 the whole snapshot route
            continue
        # split name / labels
        lb = head.find("{")
        if lb < 0:
            name, labels = head, {}
        else:
            name = head[:lb]
            labels = _parse_labels(head[lb + 1:head.rfind("}")])
        bucket = out.get(name)
        if bucket is None:
            out[name] = bucket = []
        bucket.append((name, labels, val))
    return out


def _parse_labels(s: str) -> dict[str, str]:
    """labels='a="1",b="x,y"' -> {'a':'1','b':'x,y'}  (handles escaped quotes)."""
    d: dict[str, str] = {}
    i, n = 0, len(s)
    while i < n:
        eq = s.find("=", i)
        if eq < 0:
            break
        key = s[i:eq].strip()
        if s[eq + 1:eq + 2] != '"':
            # unquoted value (valid proto text but rare) — read to next comma
            end = s.find(",", eq)
            if end < 0:
                end = n
            d[key] = s[eq + 1:end].strip()
            i = end + 1
            continue
        j = eq + 2
        buf: list[str] = []
        while j < n:
            c = s[j]
            if c == "\\":
                buf.append(s[j + 1: j + 2])
                j += 2
                continue
            if c == '"':
                break
            buf.append(c)
            j += 1
        d[key] = "".join(buf)
        i = s.find(",", j) + 1
        if i == 0:
            break
    return d


def first(series: dict[str, list[Line]], name: str, **match: str) -> float | None:
    """Value of the first series of `name` whose labels contain all `match` items."""
    for _, labels, val in series.get(name, ()):
        if all(labels.get(k) == v for k, v in match.items()):
            return val
    return None


def total(series: dict[str, list[Line]], name: str, **match: str) -> float | None:
    """Sum over all series of `name` whose labels contain all `match` items."""
    acc, seen = 0.0, False
    for _, labels, val in series.get(name, ()):
        if all(labels.get(k) == v for k, v in match.items()):
            acc += val
            seen = True
    return acc if seen else None


def histogram(series: dict[str, list[Line]], base: str) -> tuple[list[tuple[float, float]], float, float] | None:
    """Return (sorted [(le, cumulative_count)], sum, count) for histogram `base`.

    Buckets are converted to cumulative counts keyed by upper bound (+Inf last).
    """
    # audit: multi-series labels (multi-process vLLM, per-model exl3) —
    # take the first line only and the buckets are silently wrong. Aggregate:
    # sum buckets per le across label sets, sum _sum/_count across all lines.
    by_le: dict[float, float] = {}
    for _, labels, val in series.get(base + "_bucket", ()):
        le = labels.get("le")
        if le is None:
            continue
        try:
            ub = float("inf") if le == "+Inf" else float(le)
        except ValueError:
            continue
        by_le[ub] = by_le.get(ub, 0.0) + val
    buckets = sorted(by_le.items(), key=lambda p: p[0])
    ssum = total(series, base + "_sum")
    scount = total(series, base + "_count")
    if not buckets and ssum is None and scount is None:
        return None
    return buckets, ssum or 0.0, scount or 0.0


def quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """Linear-interpolated quantile over cumulative buckets (Prometheus method)."""
    if not buckets:
        return None
    total_c = buckets[-1][1]
    if total_c <= 0:
        return None
    target = q * total_c
    prev_ub, prev_c = 0.0, 0.0
    for ub, c in buckets:
        if c >= target:
            if ub == float("inf"):
                return prev_ub
            if c == prev_c:
                return ub
            frac = (target - prev_c) / (c - prev_c)
            return prev_ub + frac * (ub - prev_ub)
        prev_ub, prev_c = (ub, c) if ub != float("inf") else (prev_ub, c)
    return prev_ub

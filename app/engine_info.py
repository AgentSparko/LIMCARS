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


def _val_labeled(text: str, metric: str, **match) -> float | None:
    """One labelled sample: _val_labeled(text, 'qse_cache_bytes', cache='state')."""
    for m in re.finditer(r"^" + re.escape(metric) + r"\{([^}]*)\}\s+([\d.eE+-]+)", text, re.M):
        labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if all(labels.get(k) == v for k, v in match.items()):
            return float(m.group(2))
    return None


def _launch_cmd(port: int | None) -> str | None:
    """Full launch 'recipe' = the serving process command line, read from
    /proc (same-user). Match: mentions vllm AND the engine's --port."""
    import glob

    from . import procs
    best = None
    if not procs.LINUX_PROC:
        # sorted(): psutil's pid order is arbitrary, and the port-less path kept
        # overwriting `best`, so "the launch recipe" was last-seen-wins.
        for pid in sorted(procs.pids()):
            parts = procs.argv(pid)
            if not parts or "vllm" not in " ".join(parts[:3]):
                continue
            joined = " ".join(parts)
            if port and f"--port {port}" not in joined and f"--port={port}" not in joined:
                continue
            best = joined
            break                      # first match in pid order, not last-seen
        return best
    for cl in sorted(glob.glob("/proc/[0-9]*/cmdline"),
                     key=lambda p: int(p.split("/")[2])):   # pid order, not glob order
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
        break                          # first match in pid order, not last-seen
    return best


def _launch_cmd_any(port: int | None) -> str | None:
    """The serving process's command line for a port, engine-agnostic.

    The vLLM-only matcher above looks for the string 'vllm' in the argv;
    TandemLLM's argv says 'server/app.py', so the panel showed no launch
    recipe for it. Here the live-engine detector (which already maps
    ports to processes) is the source of truth, and /proc is the fallback.
    """
    if port:
        try:
            from .live_engines import LiveEngines
            for row in LiveEngines().scan():
                if port in (row.get("ports") or []) and row.get("cmd"):
                    return row["cmd"]
        except Exception:  # noqa: BLE001 — the fallback below still works
            pass
    try:
        import glob
        from . import procs
        if procs.LINUX_PROC:
            for cl in sorted(glob.glob("/proc/[0-9]*/cmdline"),
                             key=lambda p: int(p.split("/")[2])):
                try:
                    raw = open(cl, "rb").read()
                except OSError:
                    continue
                parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
                if not parts:
                    continue
                joined = " ".join(parts)
                if port and not (f"--port {port}" in joined or f"--port={port}" in joined):
                    continue
                if port or "server/app.py" in joined:
                    return joined
    except Exception:  # noqa: BLE001
        pass
    return None


def _tandem_info(url: str, text: str) -> dict:
    """TandemLLM's own contract (0xBakeer/TandemLLM server/metrics.py).

    It publishes no vllm:cache_config_info and no /version route, so the
    vLLM-only path returned an empty panel for this engine. Everything
    here is read from the engine's own metrics, nothing guessed.
    """
    bi = _labels(text, "qse_build_info")
    ei = _labels(text, "qse_engine_info")
    drafts = _val(text, "qse_spec_decode_num_drafts_total") or 0
    dtok = _val(text, "qse_spec_decode_num_draft_tokens_total") or 0
    atok = _val(text, "qse_spec_decode_num_accepted_tokens_total") or 0
    budget = _val(text, "qse_state_store_budget_bytes")
    used = _val_labeled(text, "qse_cache_bytes", cache="state")
    model = None
    try:
        md = json.loads(urllib.request.urlopen(url + "/v1/models", timeout=5).read())
        model = (md.get("data") or [{}])[0].get("id")
    except Exception:  # noqa: BLE001
        pass
    port = url.rsplit(":", 1)[-1].rstrip("/")
    port = int(port) if port.isdigit() else None
    version = bi.get("version")
    if bi.get("git_sha"):
        version = f"{version} · {bi['git_sha'][:7]}" if version else bi["git_sha"][:7]
    rows = [
        ["Context window", (f"{int(ei['max_len']):,} tok" if ei.get("max_len", "").isdigit() else ei.get("max_len")) or "–"],
        ["Drafter", ei.get("drafter") or "–"],
        ["Accept rate", (f"{atok / dtok * 100:.1f} %" if dtok else "–")],
        ["State store", (f"{used / 2**30:.2f} / {budget / 2**30:.2f} GiB" if used and budget else "–")],
        ["Caches", ei.get("caches") or "–"],
    ]
    cfg = {k: v for k, v in ei.items() if k != "model"}
    cfg.update({"build": bi.get("version", ""), "git_sha": bi.get("git_sha", ""),
                "code_sha256": (bi.get("code_sha256") or "")[:16],
                "flags_sha256": (bi.get("flags_sha256") or "")[:16]})
    return {
        "healthy": True, "kind": "tandemllm", "model": model or ei.get("model"),
        "version": version, "launch": _launch_cmd_any(port),
        "config": cfg, "rows_extra": rows,
        "ctx": int(ei["max_len"]) if ei.get("max_len", "").isdigit() else None,
        "pool_tokens": None, "block_size": None, "num_gpu_blocks": None,
        "kv_dtype": None,
        "spec": f"on ({ei.get('drafter') or 'mtp'})" if dtok else "off",
        "draft_k": round(dtok / drafts, 1) if drafts else None,
        "accept_rate": round(atok / dtok * 100, 1) if dtok else None,
    }


def info(url: str) -> dict:
    out: dict = {"healthy": False, "url": url}
    try:
        text = urllib.request.urlopen(url + "/metrics", timeout=8).read().decode()
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    from . import dialects
    kind = dialects.detect(text)
    if kind == "tandemllm":
        try:
            return _tandem_info(url, text)
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)[:200]
            return out
    try:
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

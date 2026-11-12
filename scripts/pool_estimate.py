#!/usr/bin/env python3
"""Recompute a TandemLLM launch's shared context-pool estimate (tokens), offline.

Why this exists: the setup wizard's "tokens fit in all" figure is computed on
demand by the engine's `server/setup/estimate.py` and is NEVER stored — and its
route (`POST /v1/setup/estimate`) exists only in `--setup` mode, so a SERVING
engine cannot answer the question at all. LIMCARS shows that figure as the KV
chart's dashed reference line; this script lets the line follow the launch
config instead of being typed by hand.

The math here is LIMCARS' own port of the estimator's documented formula (the
engine's docstring spells it out; this file re-implements it on purpose, so a
change in the engine shows up as a mismatch rather than silently passing). The
engine's REGISTRY is used only as data — read through the engine's own loader,
because the registry is YAML and LIMCARS stays stdlib-only.

Drift gate: every case in the engine's `models/estimate-cases.json` is run
through this port first. Any mismatch prints the diff and exits 3 with NO
usable number on stdout — LIMCARS keeps its last good value rather than a
wrong one (house rule: a dash, never a wrong number).

Usage (any python works — the script hands itself to the engine's own venv
python, which is the one holding the registry's deps):

    python3 scripts/pool_estimate.py --from-running --port 8020 --json

The install folder is AUTO-DETECTED from the running engine: the process
owning --port is read through /proc (its argv[0] and cwd), and the install
root plus the interpreter it was actually started with come from there — so
any user's location (~/TandemLLM, /opt/whatever) resolves without being
configured. --engine-dir is the fallback for when no engine runs on the port
(offline estimate); when an engine does run, its live location wins and a
stale --engine-dir is reported on stderr, not obeyed.

    --from-running   take profile/window/floor from the live launch instead of
                     the registry's recommended defaults (see running_selection)
    --slots/--window/--floor/--kv-pool   override one knob
    --no-self-check  skip the cases gate (development only)

Prints one JSON object: {"context_tokens_fit": int, "slots": …, "window": …,
"floor_gb": …, "profile": …, "kv_bytes_per_token": …, "budget_gb": …,
"room_gb": …, "source": "recomputed", "self_check": {"cases": n, "ok": true}}
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

GIB = float(1 << 30)


# --------------------------------------------------------------- the formula
# Ported from the engine's documented estimate (server/setup/estimate.py):
#   budget = device total - floor
#   parts  = os + weights + registry parts + table + per-window + side slots + tools
#   kv     = slots x min(window, typical_fill) x bytes a token   (pool on)
#          = slots x window x bytes a token                      (pool off)
#   room   = budget - every part but the KV
#   ctx    = floor(room x GiB / bytes a token), capped at max_window x slots
def kv_bytes_per_token(m: dict, comps: dict) -> int:
    for c in m["components"]:
        if c.get("kv_bytes_per_token") and comps.get(c["id"]):
            return int(c["kv_bytes_per_token"])
    return int(m["parallelism"]["kv_bytes_per_token"])


def launch_components(m: dict, env: dict) -> dict:
    """The KV-priced components (e.g. kv_fp8) the launch env switched on or off:
    {id: bool} for every component with kv_bytes_per_token whose knobs ALL read
    their 'on' (or all their 'off') value. A component the env does not name keeps
    the registry default, so an e4m3 cache is priced at its own bytes a token."""
    out: dict = {}
    for c in m["components"]:
        knobs = c.get("knobs") or {}
        if not c.get("kv_bytes_per_token") or not knobs:
            continue
        vals = [env.get(k) for k in knobs]
        if any(v is None for v in vals):
            continue
        for state in ("on", "off"):
            if all(isinstance(spec, dict) and str(spec.get(state)) == str(v).strip().strip("'\"")
                   for spec, v in zip(knobs.values(), vals)):
                out[c["id"]] = state == "on"
    return out


def default_selection(m: dict, registry) -> dict:
    """The recommended selection: registry defaults, with the engine's two
    component rules (a `with_parent` tool rides its parent; a pack whose entry
    says images do not work keeps the vision tower off)."""
    p = m["parallelism"]
    comps = {c["id"]: c["default"] for c in m["components"]}
    for c in m["components"]:
        if c.get("with_parent") and all(comps[d] for d in c["depends_on"]):
            comps[c["id"]] = True
    if registry.profile(m, registry.recommended_profile(m)["id"]).get("vision") is False:
        for c in m["components"]:
            if "VISION" in c["knobs"]:
                comps[c["id"]] = False
    return {"profile": registry.recommended_profile(m)["id"], "components": comps,
            "slots": p["slots"]["default"], "window": p["window"]["default"],
            "kv_pool": p["kv_pool"]["default"], "floor_gb": float(p["floor_gb"]["default"]),
            "device": m["devices"][0]["id"]}


def estimate(m: dict, sel: dict, registry, side_const: float | None = None) -> dict:
    """`side_const` overrides the registry's per-slot constant with what the
    launch actually set (QSE_SIDE_SLOT_CONST_GB): the wizard's figure and the
    engine's own admission can disagree, and the line should say which it is."""
    p = m["parallelism"]
    prof = registry.profile(m, sel["profile"])
    dev = next(d for d in m["devices"] if d["id"] == sel["device"])
    kvb = kv_bytes_per_token(m, sel["components"])
    fill = int(p["typical_fill_tokens"])
    slots, window = int(sel["slots"]), int(sel["window"])
    parts = [{"id": "os", "gb": float(dev["reserved_gib"])},
             {"id": "weights", "gb": float(prof["weights_gib"])}]
    parts += [{"id": x["id"], "gb": float(x["gib"])} for x in m["memory"]["parts"]]
    fixed = sum(x["gb"] for x in parts)
    table = prof.get("table")
    if table:
        gb = float(table["gib"] if table["policy"] == "warm" else table["cache_gib"])
        parts.append({"id": "table", "gb": gb})
        fixed += gb
    for part in m["memory"].get("per_window_parts") or []:
        gb = float(part["bytes_per_token"]) * window / GIB
        if part.get("max_gib") is not None:
            gb = min(gb, float(part["max_gib"]))
        parts.append({"id": part["id"], "gb": gb})
        fixed += gb
    if slots > 1:
        gb = (slots - 1) * float(p["side_slot_const_gib"] if side_const is None else side_const)
        parts.append({"id": "side_slots", "gb": gb})
        fixed += gb
    for c in m["components"]:
        if not sel["components"].get(c["id"]):
            continue
        gb = (prof.get("components_gib") or {}).get(
            c["id"], prof["drafters_gib"] if c["id"] == "drafters" else c["memory_gib"])
        if gb is None or float(gb) <= 0 and c["memory_max_gib"] is None:
            continue
        parts.append({"id": c["id"], "gb": float(gb)})
        fixed += float(gb)
    kv_worst = slots * window * kvb / GIB
    kv = (slots * min(window, fill) if sel["kv_pool"] else slots * window) * kvb / GIB
    budget = float(dev["total_gib"]) - float(sel["floor_gb"])
    room = budget - fixed
    ctx = max(0, math.floor(room * GIB / kvb))
    mw = prof.get("max_window", 0)
    if mw is None:
        ctx = 0
    elif mw:
        ctx = min(ctx, int(mw) * slots)
    return {"context_tokens_fit": ctx, "slots": slots, "window": window,
            "floor_gb": float(sel["floor_gb"]), "profile": sel["profile"],
            "kv_pool": bool(sel["kv_pool"]), "kv_bytes_per_token": kvb,
            "fixed_gb": round(fixed, 2), "kv_gb": round(kv, 2),
            "budget_gb": round(budget, 2), "room_gb": round(room, 2),
            "parts": [{"id": x["id"], "gb": round(x["gb"], 2)} for x in parts],
            "fits": round(fixed + kv, 2) <= round(budget, 2)}


def merge_components(m: dict, base: dict, given: dict) -> dict:
    """The engine's `check` merges the given components OVER the defaults, then
    re-applies the `with_parent` rule to the ones the selection did not name.
    Replacing the dict wholesale loses every tool the case did not mention."""
    comps = dict(base)
    for cid, on in (given or {}).items():
        comps[cid] = on
    for c in m["components"]:
        if c.get("with_parent") and c["id"] not in (given or {}) \
                and all(comps.get(d) for d in c["depends_on"]):
            comps[c["id"]] = True
    return comps


def self_check(m_by_id: dict, registry, cases_path: str) -> dict:
    """Run every pinned case through this port. Returns {ok, cases, bad:[…]}."""
    out = {"ok": True, "cases": 0, "bad": []}
    if not os.path.exists(cases_path):
        out["ok"] = False
        out["bad"].append({"case": "-", "reason": f"no cases file at {cases_path}"})
        return out
    doc = json.load(open(cases_path, encoding="utf-8"))
    for case in doc.get("cases", []):
        if "expect" not in case:
            continue        # an error case: it pins `check`'s validation, not the math
        sel_model = case["selection"].get("model")
        m = m_by_id.get(sel_model)
        out["cases"] += 1
        if m is None:
            out["bad"].append({"case": case["name"], "reason": f"model {sel_model} not loaded"})
            out["ok"] = False
            continue
        sel = dict(default_selection(m, registry), **case["selection"])
        if "components" in case["selection"]:
            sel["components"] = merge_components(m, default_selection(m, registry)["components"],
                                                 case["selection"]["components"])
        try:
            got = estimate(m, sel, registry)["context_tokens_fit"]
        except Exception as exc:  # noqa: BLE001 — a drift report, not a crash
            out["bad"].append({"case": case["name"], "reason": f"{type(exc).__name__}: {exc}"})
            out["ok"] = False
            continue
        want = case["expect"]["context_tokens_fit"]
        if got != want:
            out["bad"].append({"case": case["name"], "want": want, "got": got})
            out["ok"] = False
    return out


# ------------------------------------------------------- the live launch's knobs
def engine_pid(port: int) -> int | None:
    for pid in _pids_on_port(port):
        return pid
    return None


# ------------------------------------------- where the RUNNING engine lives
# The install path must never be hardcoded: other users install TandemLLM
# wherever they like, and an upgrade moves the folder under our feet (0.15.2
# -> 0.15.3 did exactly that on this box). The running process is the only
# authority — its argv[0] is the interpreter the launch really used and its
# cwd sits inside the install — so both are read through /proc and validated
# against the install's own marker file. Nothing here assumes a location.
def _is_engine_install(d: str) -> bool:
    return bool(d) and os.path.isfile(os.path.join(d, "src", "server", "app.py"))


def install_from_proc(argv0: str, cwd: str) -> tuple[str, str | None] | None:
    """(install_dir, python_exe) from the engine process's own argv[0] and cwd,
    or None when neither identifies a TandemLLM tree. The engine runs as
    <install>/venv/bin/python server/app.py with cwd in the install's run dir
    (usually <install>/src), so the install is cwd, cwd's parent, or an
    ancestor of argv[0] — whichever carries src/server/app.py. The interpreter
    is argv[0] itself (the venv the launch really used); <install>/venv/bin/
    python only as a stand-in when argv[0] is unusable."""
    cands: list[str] = []
    for d in (cwd, os.path.dirname(cwd)):
        if _is_engine_install(d) and os.path.realpath(d) not in cands:
            cands.append(os.path.realpath(d))
    if argv0.startswith(os.sep):
        parts = argv0.split(os.sep)
        for i in range(len(parts) - 1, 0, -1):
            cand = os.sep.join(parts[:i])
            if _is_engine_install(cand):
                if os.path.realpath(cand) not in cands:
                    cands.append(os.path.realpath(cand))
                break
    if not cands:
        return None
    install = cands[0]
    py = argv0 if argv0.startswith(os.sep) and os.access(argv0, os.X_OK) else None
    if py is None:
        venv_py = os.path.join(install, "venv", "bin", "python")
        py = venv_py if os.access(venv_py, os.X_OK) else None
    return install, py


def detect_running_install(port: int) -> tuple[str, str | None] | None:
    """Resolve the install of the TandemLLM serving this port, from /proc."""
    pid = engine_pid(port)
    if pid is None:
        return None
    try:
        argv0 = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0", 1)[0] \
            .decode(errors="replace")
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
    return install_from_proc(argv0, cwd)


def _reexec_argv(argv: list[str], engine_dir: str) -> list[str]:
    """Same argv with --engine-dir pinned to the resolved install (inserted if
    the caller omitted it). Split out so the rewrite is testable without exec."""
    args: list[str] = []
    skip = False
    seen = False
    for tok in argv:
        if skip:
            skip = False
            continue
        if tok == "--engine-dir":
            args += ["--engine-dir", engine_dir]
            skip = True
            seen = True
            continue
        if tok.startswith("--engine-dir="):
            args.append(f"--engine-dir={engine_dir}")
            seen = True
            continue
        args.append(tok)
    if not seen:
        args += ["--engine-dir", engine_dir]
    return args


def _reexec_with(py: str, engine_dir: str, argv: list[str]) -> None:
    """Hand the run to the engine's own interpreter: the registry's loader needs
    its deps (yaml), and LIMCARS' python is stdlib-only on purpose. One-way
    trip — POOL_ESTIMATE_REEXEC stops any loop, and the guard is internal to
    this script, not a LIMCARS_* deployment knob. --engine-dir is pinned to the
    resolved install so the re-executed run cannot re-detect differently."""
    env = dict(os.environ, POOL_ESTIMATE_REEXEC="1")
    os.execve(py, [py, os.path.abspath(__file__)] + _reexec_argv(argv, engine_dir), env)


def proc_env(pid: int) -> dict:
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read().decode(errors="replace")
    except OSError:
        return {}
    out = {}
    for pair in raw.split("\0"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k] = v
    return out


def running_selection(engine_dir: str, port: int | None) -> dict:
    """What the RUNNING engine was launched with, as far as it is discoverable:
    the profile from run/logs/engine-status.json, the window from the serving
    process's --max-len, and the memory knobs from that process's OWN environ —
    the process env is the truth of the launch, serve.env is only what the
    launcher intended (they differ: serve.env's ceiling is 70, the launch is 66).
    Slots are not on the command line (the engine takes its default), so they
    stay at the registry default. Missing piece -> that knob is left unset and
    the registry default stands; nothing is invented."""
    found: dict = {}
    st = os.path.join(engine_dir, "run", "logs", "engine-status.json")
    if os.path.exists(st):
        try:
            prof = json.load(open(st, encoding="utf-8")).get("profile")
            if isinstance(prof, str):
                found["profile"] = prof
        except (OSError, ValueError):
            pass
    pid = engine_pid(port) if port else None
    env = proc_env(pid) if pid else {}
    if not env and os.path.exists(os.path.join(engine_dir, "run", "ops", "serve.env")):
        # fall back to the launcher's file, read the same way (KEY=VALUE lines)
        try:
            env = dict(l.split("=", 1) for l in
                       open(os.path.join(engine_dir, "run", "ops", "serve.env"), encoding="utf-8",
                            errors="replace").read().splitlines()
                       if "=" in l and not l.lstrip().startswith("#"))
        except OSError:
            env = {}
    if env.get("QSE_PARALLEL_FLOOR_GB"):
        try:
            found["floor_gb"] = float(env["QSE_PARALLEL_FLOOR_GB"])
        except ValueError:
            pass
    if env.get("QSE_SIDE_SLOT_CONST_GB"):
        try:
            found["side_slot_const_gib"] = float(env["QSE_SIDE_SLOT_CONST_GB"])
        except ValueError:
            pass
    # knobs a component reads (QWEN38_FN_KV_FP8 ...): main() maps them to components
    found["env"] = {k: v for k, v in env.items() if k.startswith(("QWEN38_", "QSE_"))}
    if pid:
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
            m = re.search(r"--max-len[= ]+(\d+)", cmd.replace("\0", " "))
            if m:
                found["window"] = int(m.group(1))
        except OSError:
            pass
    return found


def _pids_on_port(port: int) -> list[int]:
    """PIDs whose cmdline looks like the engine's server on this port. Cheap and
    dependency-free: /proc/*/cmdline, no ss/lsof parse. The cmdline is NUL-
    separated — normalized to spaces before matching, or `--port\\08020` never
    matches the `--port 8020` shape the pattern expects (this silently missed
    every real launch until the install-path autodetector needed it)."""
    out = []
    pat = re.compile(rf"server/app\.py.*--port[= ]+{port}\b")
    if not os.path.isdir("/proc"):
        return out
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmd = open(f"/proc/{entry}/cmdline", "rb").read().decode(errors="replace")
        except OSError:
            continue
        if pat.search(cmd.replace("\0", " ")):
            out.append(int(entry))
    return out


def _require_finite(name, x):
    """json.dumps writes the BARE tokens NaN/Infinity for non-finite floats —
    invalid JSON that Python's parser swallows while the browser's JSON.parse
    rejects it: one poisoned snapshot kills the engine handler for EVERY
    subsequent read (second-audit L1, 2026-11-10). Nothing non-finite may
    reach the output, whatever path a knob took to get here (argparse's
    type=float happily accepts "nan"/"inf"; env-parsed launch knobs too)."""
    if isinstance(x, float) and not math.isfinite(x):
        raise ValueError(f"{name} is not a finite number (got {x!r})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="recompute a TandemLLM shared context-pool estimate")
    ap.add_argument("--engine-dir", default=None,
                    help="install folder; the fallback when no engine runs on --port "
                         "(the running engine's own location wins over this)")
    ap.add_argument("--model", default=None, help="registry model id (default: the running one)")
    ap.add_argument("--port", type=int, default=None, help="engine port, for --from-running")
    ap.add_argument("--from-running", action="store_true")
    ap.add_argument("--slots", type=int)
    ap.add_argument("--window", type=int)
    ap.add_argument("--floor", type=float)
    ap.add_argument("--kv-pool", choices=("0", "1"))
    ap.add_argument("--no-self-check", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    # L1: a pinned floor is the one knob an operator types by hand — refuse a
    # non-finite one here, before anything runs, so it can never ride into
    # floor_pinned.floor_gb and out through json.dumps.
    try:
        for k, v in (("--floor", a.floor), ("--slots", a.slots), ("--window", a.window)):
            if v is not None:
                _require_finite(k, v)
    except ValueError as exc:
        print(f"pool_estimate: {exc}", file=sys.stderr)
        return 2

    # Resolve the install: the RUNNING engine is the authority (any location);
    # --engine-dir only stands in when nothing serves the port. Detection runs
    # even when --engine-dir was passed: a folder that moved (an upgrade) must
    # not silently keep the estimate on the old tree.
    engine_dir, running_py = a.engine_dir, None
    if a.from_running and a.port:
        det = detect_running_install(a.port)
        if det:
            engine_dir, running_py = det
            if a.engine_dir and os.path.realpath(a.engine_dir) != engine_dir:
                print(f"pool_estimate: --engine-dir {a.engine_dir} is not where the engine on "
                      f":{a.port} runs from ({engine_dir}); using the running engine's install",
                      file=sys.stderr)
        elif not a.engine_dir:
            print(f"pool_estimate: no TandemLLM process on port {a.port} to detect an install "
                  f"from, and no --engine-dir to fall back to", file=sys.stderr)
            return 2
    if not engine_dir:
        print("pool_estimate: pass --engine-dir, or --from-running --port <p> with the engine up",
              file=sys.stderr)
        return 2

    # The registry's loader needs the engine's venv deps; LIMCARS' python has
    # none. Re-exec into the engine's own interpreter once (the guard env and
    # the pinned --engine-dir make the second pass deterministic).
    if not os.environ.get("POOL_ESTIMATE_REEXEC"):
        target = running_py or os.path.join(engine_dir, "venv", "bin", "python")
        if os.access(target, os.X_OK) and \
                os.path.abspath(target) != os.path.abspath(sys.executable):
            _reexec_with(target, engine_dir, sys.argv[1:])

    src = os.path.join(engine_dir, "src")
    if not os.path.isdir(src):
        print(f"pool_estimate: no engine source at {src}", file=sys.stderr)
        return 2
    sys.path.insert(0, src)
    try:
        from server.setup import registry                      # noqa: S404 — the engine's loader
        from server.setup import device as device_mod
    except Exception as exc:  # noqa: BLE001
        print(f"pool_estimate: cannot import the engine's registry ({exc}); run it with the "
              f"engine's venv python", file=sys.stderr)
        return 2

    models, _ = registry.load_all()
    if not models:
        print("pool_estimate: the registry loaded no models", file=sys.stderr)
        return 2

    mid = a.model
    run = running_selection(engine_dir, a.port) if a.from_running else {}
    if mid is None:
        # the model that OWNS the running profile. The served id is a legacy
        # label (qwen3.8-27b-tandem serves a flash-next pack), so matching it
        # against registry ids picks the wrong model — the profile does not.
        mid = next(iter(models))
        prof = run.get("profile")
        if prof:
            owners = [k for k in models if any(x["id"] == prof for x in registry.get(k)["profiles"])]
            if len(owners) == 1:
                mid = owners[0]
            elif owners:
                print(f"pool_estimate: profile {prof} is offered by {owners}; pass --model",
                      file=sys.stderr)
                return 2

    m = device_mod.windowed(registry.get(mid))
    sel = default_selection(m, registry)
    side_const = None
    if a.from_running:
        for k, v in run.items():
            if k == "side_slot_const_gib":
                side_const = v
                continue
            if k == "env":
                continue
            if k == "profile" and registry.profile(m, v) is None:
                continue                      # a profile this model does not know
            sel[k] = v
    launch_comps = launch_components(m, run.get("env") or {}) if a.from_running else {}
    sel["components"] = dict(sel["components"], **launch_comps)
    for k, v in (("slots", a.slots), ("window", a.window), ("floor_gb", a.floor)):
        if v is not None:
            sel[k] = v
    # an operator-pinned floor is not the launch's: say so in the output, so the
    # marker never quietly claims to be what the engine was started with
    if a.floor is not None:
        res_note = {"floor_gb": float(a.floor), "why": "pinned by --floor, not the launch"}
    else:
        res_note = None
    if a.kv_pool is not None:
        sel["kv_pool"] = a.kv_pool == "1"

    res = estimate(m, sel, registry)          # the marker: the wizard's own arithmetic
    res["model"] = mid
    res["source"] = "recomputed"
    if launch_comps:
        res["launch_components"] = launch_comps
    if res_note:
        res["floor_pinned"] = res_note
    if side_const is not None:
        # what the launch's env would price it at, beside the marker. Kept as
        # information only: on the live box the engine committed 425,984 rows
        # with 22.8 GiB still available while its whole RSS was 10.7 GiB, so the
        # env's 1.5 GiB per side slot is not hard memory this build pays — the
        # registry's 0.0 is the closer truth, and the wizard's figure is what a
        # person can go and verify in the setup screen.
        # L1 again: this value reaches the dashboard as kv_ref_launch unvalidated
        # downstream, so it passes the same finite gate before it is emitted.
        try:
            launch_fit = estimate(m, sel, registry,
                                  side_const=side_const)["context_tokens_fit"]
            _require_finite("launch_context_tokens_fit", launch_fit)
        except ValueError as exc:
            print(f"pool_estimate: {exc}; refusing to publish a poisoned figure",
                  file=sys.stderr)
            return 3
        res["launch_context_tokens_fit"] = launch_fit
        res["env_overrides"] = {"side_slot_const_gib": {"registry":
                                                        float(m["parallelism"]["side_slot_const_gib"]),
                                                        "launch": side_const}}
    if not a.no_self_check:
        chk = self_check({k: device_mod.windowed(registry.get(k)) for k in models},
                         registry, os.path.join(src, "models", "estimate-cases.json"))
        res["self_check"] = chk
        if not chk["ok"]:
            print(json.dumps(res, indent=2), file=sys.stderr)
            print(f"pool_estimate: this port disagrees with {len(chk['bad'])} of "
                  f"{chk['cases']} pinned cases — the engine's formula moved; publishing "
                  f"nothing", file=sys.stderr)
            return 3
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
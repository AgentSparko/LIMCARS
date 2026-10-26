"""Configuration: defaults < data/config.json < LIMCARS_* env overrides."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"

DEFAULTS = {
    "port": 8951,
    # Loopback by default: a dashboard whose recipes are shell lines run by
    # subprocess must not be reachable by the whole LAN unless the operator
    # asks. Expose via data/config.json "host" or LIMCARS_HOST=0.0.0.0.
    "host": "127.0.0.1",
    "live_hz": 2.0,          # engine sampler ticks per second (1-4)
    "hw_hz": 1.0,            # hardware sampler ticks per second
    "ring_seconds": 3900,    # in-memory ring depth (65 min — supports the 1 h window)
    # 1 s-resolution ring window. This is the whole database: ~1.7 KB per tick
    # row per engine, so 7 d was roughly 1 GB/engine of data the UI never
    # renders -- charts above 1 h come from the hourly counter_buckets, which
    # is retained 366 d and is three orders of magnitude smaller. Editable in
    # Settings (5 min .. 7 d); 48 h keeps two days of forensic detail.
    "persist_seconds": 172800,
    "window_s": 30.0,        # window for p50 / avg derived stats
    "pricing": None,         # None -> resolve model against data/pricing.json
    "engines": [
        {"id": "flashnext", "url": "http://127.0.0.1:8000", "kind": None,
         "label": "Qwen3.8-Flash-Next"},
    ],
    "services": [  # systemd units the admin page may start/stop
        {"id": "flashnext", "unit": "docker", "label": "Flash-Next engine (docker)",
         "start": "docker start rl-flashnext-nvidia-vr8vr8",
         "stop": "docker stop rl-flashnext-nvidia-vr8vr8",
         "status": "docker ps --filter name=rl-flashnext-nvidia-vr8vr8 --format {{.Status}}"},
    ],
    "db_path": str(DATA / "limcars.db"),
    "auth_token": None,      # when set, /api/* requires X-Limcars-Token
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    f = DATA / "config.json"
    if f.exists():
        try:
            cfg.update(json.loads(f.read_text()))
        except Exception as exc:  # noqa: BLE001 — bad config must not brick boot
            print(f"[config] ignoring {f}: {exc}")
    if v := os.environ.get("LIMCARS_PORT"):
        cfg["port"] = int(v)
    if v := os.environ.get("LIMCARS_HOST"):
        cfg["host"] = v
    if v := os.environ.get("LIMCARS_DB"):
        cfg["db_path"] = v
    if v := os.environ.get("LIMCARS_TOKEN"):
        cfg["auth_token"] = v
    DATA.mkdir(exist_ok=True)
    return cfg

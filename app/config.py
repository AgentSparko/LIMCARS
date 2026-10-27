"""Configuration: defaults < data/config.json < LIMCARS_* env overrides."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Where all runtime state lives: config.json, auth.json, recipes.json,
    pricing.json, the ssh control dir, and the default database.

    Defaults to <repo>/data. LIMCARS_DATA relocates the whole set in one knob,
    because every state path resolves through here. That knob exists for a
    reason: the security suite boots the real app with cwd=<repo>, and with one
    shared data dir its POSTs landed in the same data/auth.json and
    data/config.json the running dashboard uses — one test's /api/auth/set
    created a real password on the production instance. A test that mutates the
    operator's state is not a test.
    """
    v = os.environ.get("LIMCARS_DATA")
    return Path(v).expanduser() if v else ROOT / "data"


DATA = data_dir()
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
    # Settings (5 min .. 7 d).
    #
    # One hour, not 48. Measured against every ring reader, nothing older than an hour can be
    # rendered (largest chart window 3600 s) EXCEPT the two bubble sparklines, which draw ~200
    # points of a derived stat -- and those points now live in their own table (spark_stats,
    # kept spark_hours long) at the stride they are displayed at. Raw samples past the hour were
    # ~1000x more data than the dashboard can show: 48 h of rings was 508 MB, this is ~10 MB.
    "persist_seconds": 3600,
    # ladder retention (app/tiers.py): raw minute buckets are kept this long, the hourly tier
    # covers history_years, daily totals are kept forever. Minutes cost ~14 KB/day, so a long
    # minute window is affordable — it is the 1 s rings that are 242 MB/day.
    "minute_bucket_days": 30,
    "history_years": 1,
    # ring resolution: 1 s samples are kept this long, then collapsed to one row per minute
    # until persist_seconds. The charts' largest window is 3600 s and the sparklines stride at
    # 60/120 s, so a second-resolution row older than this cannot be rendered by anything.
    "ring_full_seconds": 3600,
    # the rings are a one-hour buffer now: past it the charts cannot render a sample, and the
    # only thing still displayed is the bubble sparklines' own stats (spark_stats, kept this long)
    "spark_hours": 26,
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
            cfg.update(json.loads(f.read_text(encoding="utf-8")))
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

# LIMCARS-NEXT

Live observability for this box's inference engines — rebuilt from zero,
inspired by the old LIMCARS, pattern-copied from TandemLLM's live dashboard.

## Why this exists
The old LIMCARS grew to a 7,600-line `server.js` plus a Streamlit dash whose
rings only advanced while a browser held the page open and were trimmed to
180 s — so prefill/decode bubbles effectively "sampled every 3 minutes" and
the charts did not reflect reality. TandemLLM proved the right pattern: a
sampler thread ticking 1–4 Hz while requests run, streaming over resumable
SSE, with lightweight SVG charts.

## What it does
- **2 Hz engine sampler** per engine (`app/sampler.py`): polls the engine's
  Prometheus `/metrics` and turns cumulative counters into honest rates —
  `Δgen_tokens/Δt` for decode tok/s, `Δprompt_tokens/Δt` for prefill,
  windowed (30 s) averages, prefix-cache hit %, spec-decode accept %, and
  **p50s computed from histogram-bucket deltas over the window** (Prometheus
  method), so a chart point is the last half-second, not the last boot.
- **1 Hz hardware sampler** (`app/hw.py`): CPU, load, memory, swap, disk/net
  throughput (counter diffs), GB10 GPU util/temp/power via one `nvidia-smi`
  per tick (GB10 `memory.used` is N/A — unified memory shows in MemAvail,
  we do not fake it), package temps from hwmon.
- **Always-on rings** (in-memory, 15 min) + 1 s sqlite downsample (1 h) —
  history accrues with zero browsers attached.
- **Resumable SSE** (`/api/events`): per-topic sequence ids, `Last-Event-ID`
  replay, keepalive pings; slow tabs drop frames, the sampler never blocks.
- **Engine dialects** (`app/dialects.py`): vLLM, SGLang, llama.cpp,
  TandemLLM/qwen38 — one logical schema; a missing metric renders “–”.
- **Admin**: whitelisted service start/stop cards on the hub.

## Stack discipline
Python **stdlib only** (the single third-party import is `psutil`). No npm,
no pip requirements, no build step, no Streamlit, no ECharts. Frontend is
vanilla ES modules + hand-rolled SVG charts (`web/js/charts.js`) following
the TandemLLM house rule: **orange is now**.

## Run
```bash
./scripts/start.sh                      # foreground, port 8951
LIMCARS_PORT=8990 ./scripts/start.sh    # override port
# systemd:
sudo cp scripts/limcars-next.service /etc/systemd/system/
sudo systemctl enable --now limcars-next
```

## Config
`data/config.json` (see `app/config.py` DEFAULTS): engines list, sampler
rates (`live_hz` 1–4, `hw_hz`), ring/persist depth, window, services,
optional `auth_token` (then `/api/*` needs `X-Limcars-Token`).
Env overrides: `LIMCARS_PORT`, `LIMCARS_HOST`, `LIMCARS_DB`, `LIMCARS_TOKEN`.

## API
| route | what |
|---|---|
| `GET /api/health` | ok/version/uptime |
| `GET /api/snapshot` | engines + hw latest + full rings + services |
| `GET /api/history?engine=&seconds=` | sqlite downsample |
| `GET /api/events?topics=engine:x,hw,requests,events` | SSE (resumable) |
| `POST /api/services/<id>/start|stop` | whitelisted actions |

## Tests
`python3 -m pytest tests/ -q` (parser, dialect detect, diff math, SSE broker,
quantile math — all pure, no engine needed).

## External prerequisites (documented, not vendored)
- The monitored engine (vLLM container on :8000 by default).
- `nvidia-smi` for GPU telemetry (absent → GPU rows read “–”).
- `psutil` (`pip install psutil`).

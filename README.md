# LIMCARS

**Lim** → **L**ocal **In**ference **M**onitor · **C**ost **A**nalyzer · **R**ecipe **S**erver

Real-time observability for self-hosted LLM inference engines — built for
single-box GPU rigs (DGX Spark / GB10 friendly, works on anything). One
Python process, **zero dependencies** beyond `psutil`: a 2 Hz engine
sampler, an SSE-pushed live dashboard, honest cost analytics, a recipe
launcher, and live Hermes agent-trace browsing.

```
python3 -m app          # → http://localhost:8951
```

![dashboard](docs/screenshot.png) <!-- optional; add later -->

## Why

Existing engine dashboards either poll lazily, lose values the moment
inference stops, or quietly drift from the engine's own counters. LIMCARS
is built around three rules:

1. **The engine's `/metrics` is the only source of truth.** All token
   totals are counter-differenced from Prometheus exposition — the 1-minute
   ledger reconciles to the engine absolutes to 0.00 %, across restarts and
   dashboard downtime.
2. **Never lie with stale zero.** Latency bubbles hold the last real value
   (dimmed) because "0 ms" when idle is a lie; speed bubbles dim the instant
   the engine stops reporting activity.
3. **Conventions, not guesses.** Prefill speed excludes prefix-cache hits,
   savings use the last *active* hour at market pricing, sparklines are
   real histories, and chart axes follow physical maxima.

## Features

**Live dashboard** (`/`)
- Bubble strip: gen speed, prefill speed, KV cache (used/free in *tokens*),
  running requests, TTFT, e2e latency (hour median + lifetime mean),
  speculative acceptance (+ draft tok/step), prefix-cache hits (hour median,
  lifetime sparkline), peak gen speed.
- Hardware sidebar: CPU/GPU util, temps, power, clocks, unified RAM free,
  SSD free, net ↓↑ — each with sparkline charts, AUTO Y (data-fit) vs
  fixed (physical-max) axes, hover readouts.
- Server-backed panel settings (shared by every browser that opens the dash),
  3/15/60-min chart windows, per-session token bars, per-client context
  chart, engine probe (real streamed TTFT / prefill / decode speed).

**Token counters & cost**
- Per-model prompt/cached/generation ledger in 1-minute buckets, 366-day
  retention; cache-hit %, "tokens saved by cache", projected $/day and
  $/year from the last active hour.
- Market pricing resolution (`data/pricing.json`, OpenRouter-style) with a
  manual override editor.

**Recipe server**
- Save engine launch commands as emoji-named recipes; Start/Stop from the
  sidebar; per-recipe detached logs under `data/recipe-logs/`.
- Live engine detection: scans GPU compute apps (>1 GiB), listening ports
  (OpenAI-API fingerprinting) and process groups — Stop button per engine.

**Hermes Traces** (when [Hermes Agent](https://hermes-agent.nousresearch.com)
sessions exist on the box)
- Two-pane live browser of agent/subagent delegations: manifests, tailing
  transcripts (HTTP Range), summaries, main sessions, export.

**Security**
- One shared password guards recipe add/edit and trace data, with an
  escalating per-IP lockout (3 wrong → 60 s → 5 min → 30 min → 2 h) and
  constant-time hashed comparison. Start/Stop and the dashboard stay open
  for convenience; localhost is the admin domain.

## Supported engines

| Engine | Status |
|---|---|
| vLLM (v1) | full — counters, histograms, KV cache config, spec-decode internals |
| SGLang | full — incl. `realtime_tokens_total{mode=prefill}` true live prefill rate |
| exl3 | full — counters + `prefill/generate_seconds` exact pairing |
| TensorFold | full — dual-name families, spec-decode (mtp/spec_decode), histograms |
| llama.cpp | basic — running/deferred, prompt/predicted tokens |
| TandemLLM-family | basic — inflight gauge + token/request counters |

Engine kind is auto-detected per scrape from the `/metrics` exposition — no
configuration needed to switch or mix engines. Missing metrics degrade to `–`,
never to fake zeros.

## Requirements

- Python ≥ 3.10 (`/usr/bin/python3` fine) — stdlib + `psutil` only
- Any POSIX host with `/proc` (Linux). No npm, no CDN, no telemetry.

## Configuration

Environment (or `data/config.json`):

| Variable | Default | Meaning |
|---|---|---|
| `LIMCARS_HOST` | `0.0.0.0` | bind address (`127.0.0.1` for local-only) |
| `LIMCARS_PORT` | `8951` | dashboard port |
| `LIMCARS_DB` | `data/limcars.db` | SQLite history path |
| `LIMCARS_PYTHON` | `python3` | interpreter used by `scripts/start.sh` |

Engines are auto-detected from listening OpenAI-API ports; pin explicitly via
`data/config.json` → `"engines": [{"name": "...", "url": "http://127.0.0.1:8000", "model": "..."}]`.

## systemd

```
sudo cp scripts/limcars.service /etc/systemd/system/
# edit User= and WorkingDirectory= to your install path
sudo systemctl enable --now limcars
```

## Tests

```
python3 -m pytest tests/ -q
```

## Data & privacy

Everything runs on your box. `data/` (SQLite history, settings, recipes,
password hash, logs) is gitignored and never leaves the machine. No outbound
requests are made except to the inference engines you point it at.

## License

MIT (see `LICENSE`)

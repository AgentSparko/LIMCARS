# LIMCARS

**L**ocal **I**nference **M**onitor · **C**ost **A**nalyzer · **R**ecipe **S**erver

Real-time observability for self-hosted LLM inference engines — built for
single-box GPU rigs (DGX Spark / GB10 friendly, works on anything). One
Python process, **zero dependencies** beyond `psutil`: a 2 Hz engine
sampler, an SSE-pushed live dashboard, honest cost analytics, a recipe
launcher, and live Hermes agent-trace browsing.

```
python3 -m app          # → http://localhost:8951
```

![LIMCARS — live bubbles, hardware sidebar and projected savings](docs/screenshots/1.jpg)

| Live charts: decode/prefill speed, KV pool, spec accept, per-session & per-client context |
|---|
| ![charts](docs/screenshots/2.jpg) |

| Stats by model & request completions | Token counters ledger & money saved |
|---|---|
| ![stats](docs/screenshots/3.jpg) | ![counters](docs/screenshots/4.jpg) |

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
  lifetime sparkline), peak gen speed. Prefill/gen bubbles light only while
  the engine is genuinely in that phase (server-side sticky activity flags,
  not raw 2 Hz flicker).
- Hardware sidebar: CPU/GPU util, temps, power, clocks, unified RAM free,
  SSD free, net ↓↑ MB/s — each with sparkline charts, AUTO Y (data-fit) vs
  fixed (physical-max) axes, hover readouts.
- Server-backed panel settings (shared by every browser that opens the
  dash), 3/15/60-min chart windows, per-session token bars, per-client
  context chart, engine probe (real streamed TTFT / prefill / decode speed
  against a live completion).
- Everything pushes over **SSE** — cold paint from `/api/snapshot`, then
  live deltas; no polling loops.

**Token counters & cost**
- Per-model prompt/cached/generation ledger in 1-minute buckets, 366-day
  retention; cache-hit %, "tokens saved by cache", projected $/day and
  $/year from the last active hour.
- Daily money-saved chart (week / month / 3-month / year switch): one color
  per model, hover for per-model cost + write/read/decode token split,
  all-model totals; built from real persisted usage only (synthetic seed
  buckets excluded).
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
  transcripts (HTTP Range tail-polls), summaries, main sessions, export.
- Opens as a pane swap (the hardware sidebar stays visible; Esc or the
  orange toggle returns) or standalone via *open in new tab ↗* — both carry
  the session password automatically. A standalone page opened without
  credentials prompts with a masked dialog instead of sitting empty.

## Supported engines

| Engine | Status |
|---|---|
| vLLM (v1) | full — counters, histograms, KV cache config, spec-decode internals |
| SGLang | full — incl. `realtime_tokens_total{mode=prefill}` true live prefill rate |
| exl3 | full — counters + `prefill/generate_seconds` exact pairing |
| TensorFold | full — dual-name families, spec-decode (mtp/spec_decode), histograms |
| llama.cpp | basic — running/deferred, prompt/predicted tokens |
| TandemLLM-family | basic — inflight gauge + token/request counters |

Engine kind is **auto-detected per scrape** from the `/metrics` exposition —
no configuration needed to switch or mix engines; each family then reads
through its own dialect (`app/dialects.py`). Missing metrics degrade to `–`,
never to fake zeros.

**How prefill speed is measured honestly** — engines credit prompt tokens at
request *completion*, so a naive per-tick delta plots impossible spikes
(127k tokens in 0.5 s). LIMCARS uses the best exact pairing each family
offers:

| Family | Prefill speed source |
|---|---|
| SGLang | `realtime_tokens_total{mode=prefill*}` — genuine live rate |
| vLLM | Δuncached-prompt ÷ Δ`request_prefill_time_seconds` (same window) |
| exl3 / TensorFold | Δprompt ÷ Δ`prefill_seconds` counter pairing |
| others | Δprompt-window rate (plausibility-capped) |

Prefix-cached tokens are excluded from the numerator throughout — a 90 %-hit
prompt bills ~10× less prefill compute than raw counters suggest.

## HTTP API

| Route | Method | Notes |
|---|---|---|
| `/` | GET | dashboard (SSE-pushed) |
| `/api/health` | GET | `{ok, version, uptime_s}` |
| `/api/snapshot` | GET | all engines + hw latest rows + rings (cold paint) |
| `/api/history?engine=&seconds=` | GET | SQLite downsampled history |
| `/api/events` | GET | SSE stream: `engine:*`, `hw`, `requests`, `events` |
| `/api/counters` | GET | token ledger windows (1h/1d/7d/1mo/1y/all) |
| `/api/savings-history?days=7\|30\|92\|365` | GET | per-UTC-day per-model $ + tokens (daily chart) |
| `/api/engine-info` | GET | engine fingerprint + KV cache config |
| `/api/session-tokens` · `/api/clients` | GET | per-session / per-client token charts |
| `/api/settings` | GET/POST | shared panel settings |
| `/api/config` | GET | non-secret config view |
| `/api/recipes` (+`/<id>/(update|delete|start|stop)`) | POST | recipe server; add/edit/delete need password, start/stop open |
| `/api/engines-live` · `/api/engines-live/<pid>/stop` | GET/POST | live engine detector |
| `/api/probe` | POST | streamed latency/throughput probe (real completion against the engine) |
| `/api/services/<id>/start\|stop` | POST | whitelisted launchers |
| `/api/auth/status` · `/api/auth/set` | GET/POST | password gate |
| `/subagent-watch/*` | GET | Hermes Traces UI + data (data endpoints need password) |

## Security model

- One shared password (set from the sidebar 🔒 Security panel) guards
  recipe add/edit/delete and Hermes Trace **data**; static shells, the
  dashboard, Start/Stop, probe and settings stay open for convenience.
- Escalating per-IP lockout: 3 wrong → 60 s, 6 → 5 min, 9 → 30 min,
  12 → 2 h; a success resets the ladder. Lockout check + verify + ladder
  bump happen in one atomic step (no check-then-act race). Password stored
  as salted **scrypt** (`s2:` format; legacy SHA-256 entries keep verifying
  until reset), compared in constant time (`data/auth.json`).
- Accepted via `X-Limcars-Pw` header or `?pw=` query (iframe / new-tab flow).
- Localhost is the admin domain (exempt from the gate). Bind
  `LIMCARS_HOST=127.0.0.1` to keep it single-user.
- The recipe server executes stored commands — the password gate exists
  precisely because that is a privileged surface on a shared LAN. Treat the
  password as the admin boundary and restrict LAN access accordingly.

## Architecture

```
app/
  server.py     stdlib ThreadingHTTPServer: routes, SSE, statics (no-store)
  sampler.py    one 2 Hz thread per engine: counter windows, sticky phase
                flags, histogram quantiles, savings, event publish
  dialects.py   per-engine metric maps (name | alt-name | label-match forms)
                + fingerprint auto-detect → one logical signal schema
  prom.py       Prometheus exposition parser (labels, histogram buckets)
  store.py      SQLite (WAL): 1-min buckets, raw-counter baselines
                (downtime-exact), settings, pricing, auth persistence
  rings.py      in-memory ring buffers served in /api/snapshot
  sse.py        broker: topics engine:*/hw/requests/events
  hw.py         psutil: CPU/GPU/temp/power/RAM/SSD/net
  counters.py   token ledger + savings windows · pricing.py market $ lookup
  probe.py      real streamed completion probe (TTFT/prefill/decode)
  recipes.py    recipe store + detached start/stop
  live_engines.py  GPU/port/process engine detector
  swatch.py     Hermes delegation cache discovery (trace data)
  auth.py       AuthGate: hashed pw + per-IP lockout ladder
web/
  dashboard.html · css/dashboard.css · js/dashboard.js  (cache-busted, no-store)
  subagent-watch/   vendored traces UI — fetch wrapper attaches the session
                    password to every data call; prompts when credentials missing
tests/          pytest: dialects (synthetic per-family bodies), auth lockout,
                counter conservation, store windows
```

Single process, threads for samplers, `ThreadingHTTPServer` for I/O. State
that must survive restarts lives in SQLite; in-memory windows rebuild and
reconcile against persisted raw-counter baselines, so downtime never
swallows tokens.

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

`scripts/boot-test.sh` boots the app on a scratch port and asserts
health + dashboard respond before you install it anywhere.

## Tests

```
python3 -m pytest tests/ -q
```

## Data & privacy

Everything runs on your box. `data/` (SQLite history, settings, recipes,
password hash, logs) is gitignored and never leaves the machine. No outbound
requests are made except to the inference engines you point it at — pricing
is resolved from the local `data/pricing.json`.

## License

MIT (see `LICENSE`)

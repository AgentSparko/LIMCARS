# 🚗 LIMCARS

**L**ocal **I**nference **M**onitor · **C**ost **A**nalyzer · **R**ecipe **S**erver

Your **DGX Spark**, finally with a dashboard that tells the truth. Built and
tuned on a GB10 Spark, at home on any Linux inference rig: LIMCARS watches
your self-hosted LLM engines 2× per second and paints a live dashboard —
speeds, KV cache, thermals, hardware, token ledgers and real **money saved vs
cloud pricing** — plus a one-click recipe launcher for the engines themselves.

⚡ One Python process · 🧩 zero dependencies beyond `psutil` · 📡 no cloud, no telemetry, no accounts

> 🧪 **Honest status:** LIMCARS is developed and **tested on a single NVIDIA
> DGX Spark** (GB10, unified memory). It is designed to run anywhere Python 3
> does, but many features (the non-vLLM engine dialects, Hermes Traces, the
> recipe server under unusual setups) and any **hardware other than the Spark
> need further field testing** — if something misbehaves on your rig, open an
> issue, don't suffer in silence.

```bash
python3 -m app          # → http://localhost:8951
```

![LIMCARS — live bubbles, projected savings and the lost-potential alert](docs/screenshots/1.jpg)

![LIMCARS dashboard views](docs/screenshots/collage.jpg)

## ✨ What you get

### 📊 Live dashboard
- **Bubble strip** of everything that matters: decode & prefill tok/s, KV cache
  in tokens, running requests, TTFT, end-to-end latency, speculative
  acceptance, prefix-cache hits, peak gen speed — pushed live over SSE, no
  polling loops.
- **Hardware sidebar**: CPU/GPU utilization, temperatures, power, clocks,
  unified RAM, SSD and network ↓↑ — each with its own sparkline, smart axes
  and hover readouts.

> ⚠️ **Hardware scope**: the hardware tiles are designed for a **single
> DGX Spark** (GB10 unified memory, local `nvidia-smi`/hwmon). Everything
> *inference-related* — engines, token ledgers, money saved, probes, session
> stats — works perfectly across Spark clusters and multi-GPU rigs (point it
> at any number of engine URLs). The hardware sidebar, though, always
> reflects only the one machine LIMCARS itself runs on. The RAM tile is
> system memory (psutil) — on a Spark that is genuinely the pool your LLMs
> eat, on a normal PC it's plain system RAM. **VRAM is not shown**: a Spark
> has no discrete VRAM (`nvidia-smi` reports `N/A` by design), and a VRAM
> tile for discrete-GPU cards is not yet implemented — until then the
> engine's own KV-cache tiles are the best proxy for what the model actually
> reserved on the GPU.
- **Honest lights**: prefill/decode bubbles glow only while the engine is
  genuinely in that phase; idle latency holds its last real value instead of
  lying with a fat zero.
- 🎯 **Engine probe**: run a real streamed completion and see true TTFT,
  prefill and decode speed — measured, not estimated.

### 💰 Cost & tokens
- Per-model token ledger (write / cache-read / decode) in 1-minute buckets,
  kept for a year, reconciled to the engine's own counters to 0.00 %.
- Projected **$/day and $/year saved vs the equivalent cloud API**, computed
  from your last active hour at market prices.
- 🏦 **Daily money-saved chart** with week / month / 3-month / year switch —
  one color per model, hover for per-model cost and the full write/read/decode
  token split, totals across all models.

![Charts — decode speed, KV pool, spec accept, per-session tokens](docs/screenshots/2.jpg)

### 🍳 Recipe server
- Save each engine launch command as an emoji-named recipe and
  **Start / Stop from the sidebar** — per-recipe detached logs included.
- 🛰️ **Live engine detection** scans GPU compute apps, listening ports and
  process groups, so stray engines show up with their own Stop button.

### 🕵️ Hermes Traces (bonus)
If [Hermes Agent](https://hermes-agent.nousresearch.com) sessions live on your
box, LIMCARS doubles as a two-pane live browser of agent & subagent
transcripts — tailing logs, summaries, session stats, export.

![Token counters ledger & daily money saved vs cloud pricing](docs/screenshots/3.jpg)

## 🔌 Works with your engine

Engine kind is **auto-detected** — mix and match engines with zero
configuration. Missing metrics degrade to `–`, never to fake zeros.

| Engine | Support |
|---|---|
| 🟣 vLLM (v1) | full — counters, histograms, KV cache, spec decode |
| 🔶 SGLang | full — true live prefill rate |
| 🧊 exl3 | full — exact prefill/generate pairing |
| 🧮 TensorFold | full — dual metric families, spec decode |
| 🦙 llama.cpp | basic — running requests + token counters |
| 🔁 TandemLLM-family | **work in progress** — inflight + token counters |

> **TandemLLM detection is still work in progress.** It is recognised only when
> the engine advertises the `qse_*` metric names; a stock build that reuses plain
> vLLM metric names is auto-detected as vLLM, which keeps the throughput panels
> working but leaves the latency bubbles empty until its metric surface settles
> upstream. Everything else degrades to `–`, never to fake numbers.

Prefill speed is measured **honestly**: prefix-cache hits are excluded and each
engine family is read through the most exact counter pairing it offers — so you
never see the phantom "100k tokens in half a second" spikes other dashboards
happily plot. 🧮

![Server config — engine launch parameters, KV pool, spec decoding](docs/screenshots/4.jpg)

## 🚀 Quick start

```bash
git clone https://github.com/AgentSparko/LIMCARS.git
cd LIMCARS
python3 -m pytest tests/ -q     # 255 tests, stdlib pytest, no GPU/network/engine needed
python3 -m app                  # open http://localhost:8951 🎉
```

**Requirements:** Python ≥ 3.10 and `pip install psutil` — that's it. Linux,
any NVIDIA/AMD/CPU box. No npm, no CDN, no database server.

Engines are discovered automatically from listening OpenAI-API ports; to pin
one explicitly, edit `data/config.json`:

```json
{ "engines": [ { "name": "MyModel", "url": "http://127.0.0.1:8000", "model": "org/model" } ] }
```

Handy environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `LIMCARS_HOST` | `127.0.0.1` | bind address -- local-only by default; set `0.0.0.0` to reach the dashboard from your LAN |
| `LIMCARS_PORT` | `8951` | dashboard port |
| `LIMCARS_DB` | `data/limcars.db` | history database path |
| `LIMCARS_TOKEN` | unset | shared token required by the read/trace endpoints (`app/config.py`); the password in the header is the usual way to get the same protection -- this exists so a headless box can be gated before anyone opens the UI |
| `LIMCARS_DATA` | `<repo>/data` | directory holding every runtime state file -- `config.json`, `auth.json`, `recipes.json`, `pricing.json`, the ssh control dir and the default database. Relocate it in one knob (a read-only or shared-state install, or a test suite that must not touch the live dashboard's files) |

## 🔁 Autorun — survive reboots, forget about it

### systemd (recommended)

```bash
sudo cp scripts/limcars.service /etc/systemd/system/
sudo nano /etc/systemd/system/limcars.service   # set User= and WorkingDirectory= to your clone path
sudo systemctl enable --now limcars
```

Starts at boot ✅ restarts itself on failure ✅ plays nice under load (`Nice=5`) ✅
Watch it with `systemctl status limcars` or `journalctl -u limcars -f`.

### cron @reboot (no systemd?)

```bash
crontab -e
# add one line:
@reboot cd /path/to/LIMCARS && /usr/bin/python3 -m app >> data/server.log 2>&1
```

Want a dry run before installing anything system-wide?
`bash scripts/boot-test.sh` boots LIMCARS on a scratch port and asserts the
health endpoint and dashboard answer. 🔍

## 🧬 Had another dashboard before? Bring your history

Don't start from zero — LIMCARS can absorb the measured usage your previous
dashboard recorded (token totals per minute), so the Lost-potential bubble 💡
and daily money-saved chart 📈 have real history from day one.

**Just tell your AI agent:**

> "I used <old dashboard name> on this machine before LIMCARS. Read
> `AGENTS.md` in the LIMCARS repo and migrate my history from it."

The agent gets a complete contract there: the exact
`counter_buckets` schema, the field-mapping traps (cached vs prompt is the
one everyone trips on), the no-double-count and measured-data-only rules,
and a verify-before-claiming loop. `scripts/import_legacy_history.py` ships
as a worked reference — an event-table-style source with per-request rows
(prefill + decode events, cached tokens tracked separately) — and it's also
how this repo's own history got populated. Every source dashboard
needs its own mapping pass — but that is precisely the kind of work a coding
agent does well. 🤖

## 🔐 Security

- 🔒 One shared password (set from the sidebar) guards recipe add/edit/delete
  and Hermes trace data. Start/Stop, probe and the dashboard stay open for
  convenience; localhost is exempt.
- 🧱 Escalating per-IP lockout — 3 wrong tries → 1 min, 6 → 5 min, 9 → 30 min,
  12 → 2 h. Passwords are stored as salted **scrypt** hashes and compared in
  constant time.
- 🔒 **The dashboard binds to `127.0.0.1` by default.** The recipe server
  executes stored commands as the user running LIMCARS, so a dashboard reachable
  from your LAN without a password is a remote code execution path for anyone on
  your network -- the guest on the wifi, the smart fridge. Binding loopback first
  means the safe thing is what you get if you change nothing.
- 🔒 **Writes fail closed.** With no password configured, any *non-local* write is
  refused with HTTP 428 and a message saying what to do. Local requests
  (loopback, including a browser on the same machine) keep working with no setup,
  because the usual case is you, on the box, configuring your own rig.
  There is deliberately **no environment variable that turns this off** -- an
  unauthenticated remote write here is remote code execution, so recipes stop working
  from other machines until you set a password. The supported way to drive the
  dashboard from your phone is: set a password (padlock in the header), then bind
  `LIMCARS_HOST=0.0.0.0` in `data/config.json`.
- 🔒 **The control plane needs the password too**, from anywhere but loopback:
  starting or stopping a service, stopping an engine process, and editing the
  cloud price table. The last one matters because those prices are the baseline
  every "money saved" figure is measured against -- leaving them open lets
  anything on the LAN rewrite your savings numbers.


## 🕊️ Data & privacy

Everything stays on your box. `data/` (history, settings, recipes, password
hash, logs) is gitignored and never leaves the machine. LIMCARS makes no
outbound requests except to the inference engines you point it at. 🏠

## 💾 Storage, retention and self-tidy

**What is kept, and for how long.**

| data | resolution | retention | default |
|---|---|---|---|
| engine samples | per tick (`live_hz`, 2.0/s) | `persist_seconds` | 48 h |
| hardware samples | per tick (`hw_hz`, 1.0/s) | `persist_seconds` | 48 h |
| counter buckets (the money history) | per busy minute, per engine | 366 d | — |

Pruning runs on every write, so the 1-second rings stay a ring and never a year of them. The
charts cap at one hour and the money math reads the buckets, so nothing is lost from the
live views — the rings are recent drill-down detail.

**The database tidies itself.** SQLite never returns deleted pages to the filesystem: after a
prune they sit on the freelist and the file keeps its high-water mark forever. A live install
measured 818 MB with 322 MB (39%) of it dead pages. So the app reclaims it on its own — once
a few seconds after boot, which is the moment right after an update, and then daily:

| knob (in `data/config.json`) | default | meaning |
|---|---|---|
| `auto_compact` | `true` | master switch for the self-tidy thread |
| `compact_delay_s` | `45` | grace period after boot before the first pass |
| `compact_interval_s` | `86400` | how often it re-checks |
| `compact_reclaim_ratio` | `0.10` | only rewrite the file if the freelist is at least this share of it |
| `compact_min_bytes` | `20971520` | ignore small databases — nothing to win |
| `compact_disk_headroom` | `1.2` | refuse to start unless free disk ≥ this × the file |

`GET /api/storage` shows what it decided (`runs`, `last_vacuum`, `freed_bytes_total`, a plain
`note` explaining any refusal) plus file size, freelist pages and row counts. `?tables=1`
adds per-table bytes; it walks every page, so leave it off the polling path.

**Prefer a cron/systemd timer?** The same engine is a script, so the nightly job and a
one-shot migration can never drift apart:

```bash
python3 scripts/limcars_tidy.py --check              # dry run: measures, writes nothing
python3 scripts/limcars_tidy.py --apply              # backup -> gate -> VACUUM -> verify
python3 scripts/limcars_tidy.py --install-timer      # daily user timer (04:30, randomised)
```

It keeps a `*.pretidy-<stamp>` backup, refuses to run against an open database unless
`--allow-live`, refuses when the disk lacks headroom, and **aborts and restores** if any
headline figure moves: a reclaim that changed a token total is a bug, not a saving. Under
`--allow-live` it aborts but never copies the backup over a database the server has open
(the server would keep writing to the deleted file); the backup is kept and it says so.

## 📆 Six years of history, in about 48 MB

The whole database used to be 1-second rings, and the money table was deleted at 366 days —
the expensive data was the data nobody renders, and the cheap data you actually want for six
years was the one being thrown away. Now the data is a **ladder**, and each rung keeps only
what the dashboard reads from it:

| Rung | Granularity | Kept | Measured cost |
|---|---|---|---|
| `engine_samples` / `hw_samples` | 1 s | `persist_seconds` (default 48 h) | ~242 MB/day — charts never zoom past 1 h, so nothing older needs it |
| `counter_buckets` | 1 min | `minute_bucket_days` (default **30 d**) | ~14 KB/day |
| `counter_hour` | 1 h | `history_years` (default **1 y**, set it to 6) | ~12 KB/day |
| `counter_day` | 1 day | **forever** | ~40 B/day |

Six years at that ladder measured **≈48 MB**, against 818 MB for two days of the old layout.

Change the horizon in `data/config.json` and restart:

```json
{ "history_years": 6, "minute_bucket_days": 30 }
```

**A rung is never deleted until the rung that replaces it has been proven equal.** Before any
trim, the minute rows are folded into hours, then each hour is checked row-for-row — tokens,
requests, iterations, money as earned, and its row count — against the aggregate that claims to
summarise it. If a single hour disagrees, the prune is *blocked* and the minutes stay, with the
reason reported. Averages are never averaged: peaks travel as `MAX`, floors as `MIN`, and a ratio
like speculative-accept rate travels as its numerator and denominator and is re-derived on read,
because averaging percentages is how dashboards quietly go wrong. Occupancy stays measured too —
an aggregated hour carries how many of its minutes actually produced a token.

Verified against a copy of a live 44-day database: 519 minute rows (69.5 M prompt tokens) folded
away, and the dashboard's own reader returned `prompt 3,054,645,010 · cached 2,720,126,615 ·
gen 27,960,289 · reqs 28,965 · $1.296333` **identical before and after**, now served from three
rungs instead of one. Check it yourself at `/api/storage` → `ladder`, or migrate an old install
with:

```bash
python3 scripts/limcars_tidy.py --tier --history-years 6
```

In plain words: the dashboard keeps a 1-second memory for two days, a minute-by-minute memory
for a month, an hourly memory for as long as you ask (six years costs about 48 MB), and a daily
memory forever — and it is not allowed to forget anything until it has proven it can remember it
another way.

## 🔬 One hour of samples, and the stats the dashboard actually shows

The two ring tables (`engine_samples`, `hw_samples`) were 94% of the database — 540 MB for 48
hours. The fix came from measuring two things rather than guessing: **what is in a row**, and
**which field any reader names**.

*What is in a row.* An engine row carried 65 fields. A copy of the projected-savings bubble
(`savings`) was 24.4% of the bytes and no DB reader ever looked at it — the bubbles read it off
the live SSE row. 29 latency twins (`itl_*`, `queue_time_*`, `prefill_time_*`, `decode_time_*`)
added 34.8% with no consumer at all.

*What can be rendered.* The charts' largest window is 3600 s, sparklines stride at 60/120 s, and
the browser reads exactly two things out of SQLite rings: `t` and `cache_hit_pct` (the
prefix-cache bubble's 7-day lifetime sparkline). Everything else it gets from memory. So:

| tier | kept | why |
|---|---|---|
| ring samples (1 s) | **1 hour** | the largest window any chart can plot |
| `spark_stats` | 6 h @ 60 s (`savings_month`), 24 h @ 120 s (`savings_year`), 8 d @ 60 s (`cache_hit_pct`) | the three series displayed past the hour — ~200 points each |
| minute buckets | 30 d | money, duty hours |
| hourly / daily totals | 6 y / forever | see next section |

**Measured: 540 MB → 10.3 MB.** The whole 7-day lifetime sparkline costs 11 KB; it used to cost
~500 MB of 1-second samples. Tunables in `data/config.json`: `persist_seconds` (ring horizon), `ring_full_seconds` (how long a
sample keeps second detail), `spark_hours` (how long the sparkline stats survive — keep it ≥ 8 days
or a visible curve ages out early; `/api/storage` reports `rings.spark_stats.short`).

Storage is a **deny-list, decided by a gate**. `tests/test_render_contract.py` derives the render
contract from the app itself — `sampler.SERIES_KEYS`, `hw.series_keys()` (whose `_BASE_KEYS`
charts `load1`, `swap_used_gb`, `disk_r_mbs`, names a JS grep never finds), the dynamically
concatenated `ttft`/`e2e` families, and every identifier the client source mentions — and asserts
the deny-lists never touch any of them. An unlisted field is kept, because "my grep didn't find
it" is not evidence of being unread: that is precisely how the hardware panels once lost their
load and disk series.

Two scripts make any future storage change provable rather than plausible:

```
python3 scripts/limcars_contract_snapshot.py before.json      # before the change
python3 scripts/limcars_contract_diff.py before.json after.json
```

The diff re-fetches every endpoint the browser calls and asserts: no contract field systematically
disappeared, physical constants identical per minute, lifetime counters monotonic in time, no
minute of history lost, sparkline points unchanged, and the money never smaller. It derives its
contract from the same app-side source, so it cannot drift from what is rendered.

## 💵 Money is recorded as it was earned

Cloud prices move — the price fetcher refreshes `data/pricing.json` and you can edit it by
hand. Every bucket therefore stores its cost (`cost_hot`, `cost_base`, `cost_nocache`) at the
moment it is written, together with the rates that produced it (`price_slug`, `price_in`,
`price_out`, `price_cw`, `price_cr`, `price_crhot`, `priced_at`, `price_src`). The stamp is
**write-once**: re-saving a minute after a restart, a gap backfill or an import refreshes the
tokens but can never re-price money that was already earned, so last March's savings stay what
they were when March happened.

Readers report their basis rather than quietly mixing two:

- `"money_basis": "as_written"` / `"priced": "as_written"` — every row in the range was priced
  when it occurred.
- `"mixed"` — some rows predate stamping; those are re-priced at today's rate and the figure
  says so.
- `"current"` — the whole range predates stamping (e.g. an imported history), so the money is
  today's rate, not history.

Buckets whose cost was never known stay `NULL`, never `0.0`: unknown money must not render as
a free hour. Rates used for *projections* (the capacity claim, the per-hour rate anchor) are
deliberately today's prices — that is what "what would this cost now" means — while every
historical total is money as earned.

## 🤝 Contributing

Fork → branch → `python3 -m pytest tests/ -q` before opening a PR — dialects,
auth lockout and counter conservation all have tests. Bug reports with an
engine name + `/metrics` snippet are extra welcome. 🙏

## 📜 License

MIT — see [LICENSE](LICENSE).

# LIMCARS → LIMCARS-NEXT feature parity

Source of truth: `.codemap/` of `~/projects/limcars` (regenerated 2026-10-22,
76 files / 29.5k LOC) + the 70 route inventory of `server.js`. Statuses:
✅ ported · 🟡 partial · ⬜ not ported.

## Dash core (Classic UI, hub/dash.html + server.js /v27/*)
| old route | new | status |
|---|---|---|
| GET /v27/dash/bubbles (info bubbles) | `/api/snapshot` + SSE `engine:*` | ✅ |
| GET /v27/dash/counters | row `prompt_total/gen_total/requests_total` | ✅ |
| GET /v27/dash/model-stats (+/:model) | Stats-by-model table + click-through detail | ✅ |
| GET /v27/dash/history | `/api/history?engine=&seconds=` (sqlite 1s) | ✅ |
| GET /v27/dash/probe | POST `/api/probe` (streamed, real TTFT/PP/TG) | ✅ |
| GET+POST /v27/dash/settings (group toggles) | ⚙ bar, localStorage | ✅ |
| GET /v27/hw (hw rings) | `/api/snapshot` hw + SSE `hw` | ✅ |
| GET /v27/bus (metrics-bus availability) | dialect detect + `–` for missing | 🟡 no explicit availability page |
| GET /v27/dash/engines | `/api/snapshot` engines | ✅ |
| window 3m/15m/1h + AUTO Y + refresh | toolbar buttons; AUTO Y fits min–max | ✅ |
| ZOOM slider (B/C) | window selector covers the need | ⬜ cosmetic, skipped |
| charts: decode/prefill/KV/running/spec-len/cumulative-tokens | all six | ✅ |
| sidebar tiles CPU/GPU util/CPU temp/GPU temp/power/clock + 3 side charts | all | ✅ |
| savings day/month/year | **median 1-min rates over last hour, refresh 1/min, prompt net of prefix-cache** | ✅ |
| per-client context (chc chart) | SGLang-only; note shown for vLLM | ⬜ port when SGLang is an engine |
| speculative internals note (DFlash K8 etc.) | spec bubbles ported from vLLM counters | ✅ |

## Engines / services
| old | new | status |
|---|---|---|
| engine_panel.py + /v27/engines/local (start/stop) | services cards (whitelisted argv) | 🟡 flashnext only; add units to config |
| engine_backend.py + engine_db.py (registry, attribution) | config `engines[]` list + dialects | 🟡 no sqlite registry yet |
| sglang /v1/loads proxy (`/v27/dash/loads`) | – | ⬜ needs SGLang |

## Pricing
| old | new | status |
|---|---|---|
| pricing_updater.py (fetches live API price lists) + /v27/pricing | config `pricing` + edit-UI via POST /api/config | 🟡 manual prices, no auto-fetch |

## Recipes launcher (recipes_panel.py + ~20 /v27/recipes/* routes)
Launchable catalog, scan publishers, import repo, GitHub token admin,
launch/stop runs, run-history+notes, preflight, host-fit, recommended,
hermes register/slots (hermes_slots.py, hermes_model_sync.py).
Status: ⬜ NOT ported — user decision 2026-10-22: "do not port recipes launcher subsystem for now".

## Token counters report (dash_events parity) — ported 2026-10-22
1-min counter_buckets in sqlite (366 d), tabs 1h/1d/7d/1mo/1y/all,
prefill-uncached / cached / gen / hit% / avg-per-req / decode iters /
min+peak tok/s / spec avg, hot-cache + base-cache + no-API-cache $
scenarios, $/hr + projections from most recent active hour, partial-window
flags. Server config panel ported too (/api/engine-info: vllm
cache_config_info labels, /version, DFlash K + accept rate).

## Hermes / agents
| old | new | status |
|---|---|---|
| subagent_watch (live Hermes sessions + delegation transcripts) | 🐝 Hermes Traces: batches across ALL profile caches, manifests, transcript tails, summaries | ✅ |
| ring_feed.py keep-alive concept | sampler runs always-on | ✅ (by design) |

## Hub site (index.html, sites.json manifest, tiles)
| old | new | status |
|---|---|---|
| hub index with service cards + manifest | hub page with engine/service/system cards | 🟡 no sites.json manifest yet |
| /v27/tiles map png, navtool, sailing sites | links only | ⬜ low value for LIMCARS-NEXT |

## Priority queue (proposed)
1. **Recipes** (decide scope) → biggest gap.
2. **Subagent-watch** port (self-contained JS, reads Hermes cache dir).
3. Engine registry sqlite + multi-engine sidebar (parity with engine_panel).
4. Pricing auto-refresh (port pricing_updater) if live prices wanted.
5. Per-client context + loads — only when an SGLang engine is configured.

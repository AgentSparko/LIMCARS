# .codemap — LIMCARS (public repo, live on :8951)

Repo: this checkout = the dashboard (`python3 -m app`, default `127.0.0.1:8951`)
(systemd user unit `limcars.service`, `python3 -m app`, `LIMCARS_PORT=8951`).
Remote `origin` = https://github.com/AgentSparko/LIMCARS (branch `main`).

Regen after big edits: `python3 .codemap/regen-codemap.py .` (idempotent, ~instant).
`jq` is at /usr/bin/jq.

## Layer map (query symbols.json for line numbers before opening source)

| Layer | Files | Notes |
|---|---|---|
| Entry | `app/__main__.py` → `app/server.py` | ThreadingHTTPServer + tiny router; route blocks in `do_POST` and `_api_get` are near-identical — anchor patches on method-unique context. |
| Sampler | `app/sampler.py` (1.4k loc) | One thread per engine at `live_hz` (2 Hz). Rates are COUNTER DIFFERENCES; completion-charged metrics lie live. TandemLLM `kv_ref_tokens` (the 836140 shared-pool estimate) set at L191/L325. |
| Dialects | `app/dialects.py` | vllm/sglang/llamacpp/tandemllm/exl3/tensorfold/atlas/tarnwell (+gr8 WIP in working tree). `detect(body)` auto-detects per scrape. |
| Discovery | `app/live_engines.py` | Engine detector (GPU holders + argv classification + /metrics probe); auto ids `auto-<port>`. |
| HW | `app/hw.py` (350 loc) | 1 Hz system+GPU row. **DISK metric path: `_tick()` L293 `shutil.disk_usage("/")`** → `disk_used_gb`/`disk_total_gb`; CPython's `free = total-used = f_bfree·frsize` INCLUDES ext4 reserved blocks. UI "SSD free" tile (`web/js/dashboard.js:105`) computes total−used → over-reports by reserved (~36.8 GiB measured 2026-11-09). True user-writable free = `statvfs.f_bavail`. `maintenance.py:169` VACUUM headroom check has the same f_bfree bug. |
| Storage | `app/store.py` (1.1k loc) | sqlite WAL cold path (`engine_samples`, `hw_samples`, `counter_buckets` 366 d); `app/rings.py` in-memory hot rings (15 min). DB path = live server's cwd → `data/limcars.db`. |
| SSE | `app/sse.py` | Topic/Broker, per-topic seq ids, Last-Event-ID replay, `MAX_SUBS=64`, full queue DROPS. `/api/events?topics=engine:<id>,hw,requests,events`. |
| Money | `app/counters.py`, `app/pricing.py`, `app/tiers.py` | `effective_price()` + `hot_costs()` + `last_active_hour()` are the ONE shared path for every money figure; `counter_buckets.synthetic` excluded from anchors; retention ladder minutes→hours→days in tiers.py. |
| Frontend | `web/js/dashboard.js` (2.4k loc), `web/js/charts.js` | ES modules, hand-rolled SVG. SSE handlers must push EVERY numeric key (no whitelist). `window.__lc` debug hook. |
| Ops | `app/recipes.py`, `app/services.py`, `app/maintenance.py`, `scripts/limcars_tidy.py` | Recipe start/stop (KillMode=process keeps engines alive across dashboard restarts). |
| Pool math | `scripts/pool_estimate.py` | Offline recompute of a TandemLLM launch's shared context-pool estimate (tokens) — the 836K figure's formula lives here. |

## jq recipes
```
jq '.symbols["app/sampler.py"]' .codemap/symbols.json      # fns/classes + line numbers
jq '.imports["app/server.py"]'  .codemap/module-graph.json # what it imports
jq '."app/hw.py"'               .codemap/reverse-imports.json  # who imports it
```

## Hard constraints for agents
- Static review only against the map; never restart `limcars.service` or touch :8020 to "verify".
- Working tree may hold someone's WIP (`app/dialects.py`, `app/live_engines.py` gr8 detection) — never stage files you did not edit.
- Commits: AgentSparko identity + noreply email; LOCAL only, never push.
- Tests: `/usr/bin/python3 -m pytest tests/ -q`. JS: `node --input-type=module --check < web/js/dashboard.js`.
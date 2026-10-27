# LIMCARS — Code Map

Derived from the tree, not carried forward from a previous version — a map copied forward carries
that version's drift. Measured now: `ls app/*.py | wc -l` → **27 modules**, `wc -l app/*.py` →
**7,139 lines**, **29 test files / 247 tests**, `find web -type f` → 10.
If this file and the code disagree, the code is right and this file is a bug — and
`tests/test_docs_knobs.py` exists to make that painful rather than silent.

Canonical instance (this deployment): `limcars.service` (user-mode systemd) → the repo checkout
(default `~/projects/LIMCARS`), port **8951**, binds `0.0.0.0`, owns `data/limcars.db`. Users update by `git pull` + restart, so
**anything that must survive an update has to do so in code** (`Store.__init__` migrations +
`scripts/limcars_upgrade_rehearsal.py` proves it does).

Entry point: `python3 -m app` (`app/__main__.py` → `serve(load())`).
**`app/server.py` has no `__main__` guard** — `python3 -m app.server` exits silently without
serving, which cost a debugging cycle during the live security proofs.
Stdlib only (`http.server`, `sqlite3`, `urllib`, `subprocess`, `threading`) plus `psutil`: no
framework, no build step. Frontend is hand-written ES in `web/js/`.

## 1. Shape of the thing

```
 engines (Prometheus / OpenAI endpoints)        host (psutil, nvidia-smi, /sys/class/thermal, ssh)
   │ sampler.py 1–2 Hz                            │ hw.py 1 Hz      │ nodes.py
   ▼                                              ▼                 ▼
 in-memory Ring (rings.py — wall-clock trimmed, NOT count trimmed)   ← what charts + cards read
   │ SSE (sse.py) + /api/snapshot
   ▼
 store.py (SQLite, WAL)
   engine_samples / hw_samples   1 s rows, kept persist_seconds (1 h)
   spark_stats                   the ~200 points each sparkline draws, kept per-series reach
   counter_buckets               1 row / engine-minute, minute_bucket_days (30 d)
   counter_hour / counter_day    tiers.py ladder — history_years (6 y) / forever
   ▼
 counters.py  ← the ONLY money math
   ▼
 server.py  stdlib ThreadingHTTPServer + tiny router (re.fullmatch)
```

### The route split that decides every storage question

| Route | Backing source |
|---|---|
| `/api/snapshot` | **memory only** (`app.engines.snapshots`, `app.hw.snapshot`, nodes, services) — hero cards, charts, hw panels |
| `/api/events` | memory (SSE broker) |
| `/api/clients` | memory (`clients.window`) |
| `/api/counters` | **SQLite** — `counters.report(store, …)`: buckets + ladder + `series_tail` sparks |
| `/api/savings-history` | **SQLite** — `counters.daily_savings(store, …)` (60 s TTL cache) |
| `/api/lost-potential` | **SQLite** — `counters.lost_potential(store, …)` (60 s TTL cache) |
| `/api/history` | **SQLite** raw ring rows (`store.history`) — the only DB ring reader the browser uses |
| `/api/storage` | `store.storage_stats()` + `store.ladder_counts()` + `store.ring_stats()` |
| `/api/engine-info`, `/api/probe` | engine HTTP, not the DB |
| `/api/session-tokens` | Hermes state DBs (`hermes_home`); read-trace gated |
| `/api/health`, `/api/auth/status` | trivial |

**The fact that keeps mattering:** the browser reads exactly two fields out of the SQLite rings —
`t` and `cache_hit_pct` (`fetchCacheLife()` in `web/js/dashboard.js`, asking up to **7 days**).
Everything else visible comes from memory or from the counter tables. Storage decisions are made
against *that* list — measured, never grepped.

## 2. Modules

| Module | Owns | Load-bearing detail |
|---|---|---|
| `sampler.py` (815) | Polls engines, builds 1 s samples, drives the ring | `except Exception as exc: self._fail(exc)` sits **inside** the `while not self._stop.is_set()` loop, so one bad scrape is recorded, not fatal |
| `server.py` (946) | Routing, auth gates, config writes, SSE | `_picked_sampler(qs)` must resolve the browser-selected `?engine=`; using `samplers[0]` makes money endpoints report the wrong engine (a lane regressed this; `tests/test_engine_selection.py` locks it) |
| `store.py` (895) | SQLite (WAL), retention, ring/stats storage | `_PRUNEABLE` frozenset of `(table, column)` — never widen to caller strings; `set_persist_window()` + `compact()` apply retention without restart; `_lean()` is a **deny-list** (unlisted = kept); `ring_stats()` cached 30 s because `/api/storage` polls a full-table `SUM(LENGTH())` |
| `counters.py` (566) | **The money math** | capacity = best demonstrated hour × 8760; two named utilisation bases, never blended; measured vs synthetic decided by ONE SQL predicate shared with the fold |
| `tiers.py` (478) | Long-horizon ladder (hourly + daily) | `hour` is **epoch seconds**, `day` is an **epoch-day index** — comparing one against the other's unit silently sums zero, and it did |
| `live_engines.py` (443) | Finds engines wherever they were started | containers + `/proc` + port probing; discovery, never authority over what an engine is serving |
| `dialects.py` (420) | Per-engine metric maps | every name must exist in the engine — see Dialects |
| `maintenance.py` (234) | Self-tidy: fold, thin, compact | the pre/post-VACUUM invariant **fails closed**: an unreadable baseline means no compaction (an empty result once passed the check vacuously); two cadences on one thread — thin every `thin_interval_s`, compact on `compact_interval_s` or when a thin is big |
| `hw.py` (244) | Host + GPU sampling, in-process rings | `series_keys()` = `_BASE_KEYS` + auto-charted `gpu*/vram*`; `_BASE_KEYS` (`load1`, `load5`, `swap_used_gb`, `disk_r_mbs`, `net_*`) are **charted but appear nowhere in the JS** — a grep calls them dead and once deleted working panel series |
| `nodes.py` (271) | Agentless remote GPU over ssh | options splice **before** `--`, so `sanitise_ssh_opts()` is an execution gate, not a style rule |
| `recipes.py` (202) | User-saved launch commands | `shell=True` is deliberate (recipes *are* shell lines) — this is why remote writes fail closed |
| `swatch.py` (285) | Hermes Traces port | reads Hermes state; private content, gated like traces |
| `auth.py` (154) | password → scrypt, per-IP lockout | loopback exempt from the write gate |
| `prom.py` (153) | Prometheus text parser | histogram families exist only suffixed (`_bucket`/`_sum`/`_count`) |
| `pricing.py` (175) | Cloud price table, tier math | drives every `$` figure; remote-write gated; `display_name`/`provider` are operator strings; unknown price ⇒ UNKNOWN, never `$0.00` |
| `procs.py`, `engine_info.py`, `clients.py`, `sse.py`, `rings.py`, `session_tokens.py`, `hermes_home.py`, `services.py`, `probe.py`, `config.py` | process helpers, server info, per-client rings, pub/sub, settings | `config.py`: defaults < `data/config.json` < `LIMCARS_*` env |

## 3. Storage

`data/limcars.db` (SQLite, WAL). Measured on the live install: **540 MB → 10.3 MB** total file;
on a synthetic 1 GB old-user database: **1,017 MB → 37 MB** with money identical.

| Table | Grain | Retention | Measured |
|---|---|---|---|
| `engine_samples` (t, engine, data) | **1 s** | `persist_seconds` = **1 h** | ~3.6 k rows ≈ 4 MB; row 65 → 42 fields after the strip, 2,175 B → 1,213 B |
| `hw_samples` (t, data) | **1 s** | same | ~3.6 k rows ≈ 1.4 MB; no browser reader at all |
| `spark_stats` (engine, key, t, v) | display stride | per series: 26 h / 26 h / **8 d**, capped by `spark_hours` | ~14 KB for everything the sparklines draw |
| `counter_buckets` (engine, ts, …, cost_*, price_*) | **1 min** | `minute_bucket_days` = 30 d | ~14 KB/day; the money source; `ts` epoch seconds, PK (engine, ts) |
| `counter_hour` | **1 h** | `history_years` = 6 y | folded from minutes by `tiers.rollup_hours`; measured/synthetic kept in separate columns |
| `counter_day` | **1 day** | forever | derived from hours by `tiers.rollup_days`; `day` is an epoch-day index |
| `counter_raw` | last absolute counters | never | 1 row; why engine restarts don't swallow tokens |
| `events` | event log | 30 d | `pricing_changed` audit; empty on a healthy idle box |

Six years of history ≈ 48 MB. The rings were always the giant, not the summaries
(`freelist_count` was 0 — deletion never returned pages, which is why `compact()` exists and why a
big thin must *earn* a VACUUM).

## 4. The retention ladder

| rung | reach | replaced by | guard |
|---|---|---|---|
| rings @ 1 s | `ring_full_seconds` (1 h) | `spark_stats` + minute buckets | `thin_rings()` before/after minute-set equality, else rollback |
| rings @ 1/min | `persist_seconds` | — | same proof |
| `spark_stats` | each series' own reach (`SPARK_SERIES`), capped by `spark_hours` | nothing — it is the summary | one source for stride + reach; `/api/storage → rings.spark_stats.short` |
| `counter_buckets` | `minute_bucket_days` | `counter_hour` | `buckets_are_redundant()` per-hour equality; folds monotonic |
| `counter_hour` | `history_years` | `counter_day` | same |
| `counter_day` | forever | — | — |

`horizon_ts()` / `hour_horizon()` are **hour-aligned**; every reader must partition at the same seam
or an hour is counted twice or never. `Store._ensure_ladder()` folds before any reader may trust an
empty rung (`test_a_reader_never_trusts_an_unbuilt_ladder`).

## 5. Storage rules — each one is a bug that already happened

1. **Measure before designing**: per-field byte share, then every reader (server route, JS, JS-built
   key names). Designing tiers before that produced both bloat and deleted features.
2. **Strip at the storage boundary only.** `save_*_samples` receives the same dict the SSE broker
   hands browsers; `latest()` reads the in-memory `Ring.last()`, never SQLite. A strip leaking into
   the live row blanks the savings cards (`l.savings`, `Object.entries(l)`). `_lean()` copies.
3. **Deny-list, not allow-list.** Unlisted ≠ unread. `tests/test_render_contract.py` derives the
   render contract from `sampler.SERIES_KEYS` + `hw.series_keys()` + the dynamic `ttft`/`e2e`
   families + every identifier in `web/`, and fails if a deny-list touches any of it.
4. **Dynamic names beat grep.** The client builds `l[k + '_p50_1h_ms']` for `ttft`/`e2e` only, and a
   test pins that so the drop-list cannot silently become wrong.
5. **Thin behind a proof.** Resolution may collapse; coverage may not.
6. **A big thin earns a VACUUM** (`Maintenance.run()` re-runs the tidy when `thinned > 5000`) — boot
   otherwise compacts before the delete that mattered.
7. **The VACUUM guard fails closed** — `_invariants()` bounded-acquires the lock and returns `{}`
   when blind; `run_once()` then refuses.
8. **Harvest before you prune**: `backfill_spark_stats()` runs in `Store.__init__`, watermark-driven
   so a 1 GB ring is not re-scanned at every boot.
9. **`spark_hours` must cover the deepest reach** or a visible sparkline silently ages out (this was
   a real defect: config said 26 h against an 8-day series).
10. **Money is recorded as earned** — `cost_*`/`price_*` frozen per bucket, write-once; NULL means
    "not priced then", never `$0.00`; days are labelled `as_written` / `mixed` / `current`.
11. **Money columns hold numbers, enforced at both ends.** SQLite does not police a REAL column, so
    a formatted `"1,200"` is stored as TEXT and nothing complains — then `SUM()` reads its leading
    digits and understates that minute a thousandfold with nothing on screen looking wrong, and
    `p + g` in `counters.py` raises. `_num()` is the one coercion function; `save_counter_bucket`
    applies it on the way in and `repair_numeric_columns()` clears what older builds left, at boot,
    through the same `_num` (a SQL `CAST` would have "repaired" `"1,200"` into `1.0`).
12. **A query string may not choose the scan size.** `_window_seconds()` clamps `?seconds=` to
    retention + slack for both ring readers; unclamped it was `WHERE t >= 1970` under the store lock
    the sampler writes through. Parsing lives in one helper because `/api/snapshot` had the guard and
    `/api/history` grew the hole while the merge was added to it.

## 6. Mutating routes and their gate (default config, no password)

| Route | Loopback | Remote |
|---|---|---|
| `/api/recipes` POST/update/delete | allowed | **428** fail-closed + "set a password" |
| `/api/config` POST (`pricing`, `window_s`, `live_hz`, `hw_hz`, `persist_seconds`) | allowed, value-validated | pricing **428**; numeric keys allowed |
| `/api/settings` POST | allowed | allowed (owner's canary decision — deliberately ungated on LAN; do not "fix") |
| `/api/services/<id>/start\|stop` | allowed | **428** |
| `/api/engines-live/<pid>/stop` | allowed | **428** — killing a 26 GB KV-warm engine is an outage |
| `/api/session-tokens`, trace reads | allowed | **428** (trace password) |
| `/api/auth/set` | allowed | proves current password |

Reads (`snapshot`, `counters`, `lost-potential`, `engines-live`, `engine-info`, `clients`,
`savings-history`, `storage`, `health`) are open by design — the dash is meant to be watched from a
phone on the LAN. The knobs are exactly `LIMCARS_HOST`, `LIMCARS_PORT`, `LIMCARS_DB`,
`LIMCARS_TOKEN`, `LIMCARS_DATA` and `data/config.json`; there is **no** "allow remote writes"
override — setting a password is the supported path for LAN administration. (`tests/test_docs_knobs.py`
fails whenever docs promise a knob the code does not have.)

**`LIMCARS_DATA` relocates all state** (config, auth, recipes, pricing, db). Tests must set it:
with one shared data dir, a test's `POST /api/auth/set` created a real password on the production
dashboard. A test that mutates the operator's state is not a test.

## 7. Money — these are contracts, not comments

- Cloud comparison totals are **prompt + generated**. Cached is a *subset* of prompt, reported as
  detail, never added.
- **Lost potential = capacity − registered saved.** Capacity projects the best demonstrated hour to
  a year (× 8760); a capability claim, never scaled by a working day.
- Duty cycle is **measured, never assumed**: no 8-hour day, no "awake hours", no single-user
  assumption. An aggregated hour carries its measured `busy`-minute count so duty hours stay
  measured at any history depth.
- **Two explicitly named bases**, and no unsuffixed field may switch between them:
  `hours_basis_measured` → `pct_usage_watched`, `hours_basis_availability` (24 h × recording days)
  → `pct_usage_availability`. `loss_period_cost` is measured; `loss_availability_cost` is
  availability. They may legitimately disagree.
- `usage %` is **not** `100 × saved / capacity` (`test_usage_percent_is_not_saved_over_capacity`).
- Honesty gates on the lost-potential bubble: engine alive, ≥60 distinct recorded minutes, best hour
  ≥15 active minutes — a burst is not a demonstrated rate.

## 8. Dialects — the rule that keeps money honest

`detect()` sniffs the exposition prefix; the map turns engine metric names into the logical set
(`prompt_tokens`, `gen_tokens`, `cached_tokens`, `kv_pct`, `ttft`…).

1. **A mapped name must exist in the engine.** `tests/test_dialect_fixtures.py` checks recorded
   captures (vLLM recorded live from `:8000`, SGLang recorded); `tests/test_llamacpp_contract.py`
   checks the hand-authored llama.cpp fixture. Invented names read `None` forever or resolve to a
   sibling and report someone else's number — three were removed. Absence is honest: UI shows "–".
2. **Provenance travels with the data.** Recorded fixtures carry source URL + date; synthetic ones
   say HAND-AUTHORED. Never cite a synthetic fixture as evidence of engine behaviour.

Adding an engine means adding a dialect and **nothing else**. Never special-case an engine in
`counters.py`, `sampler.py`, or the frontend.

## 9. Frontend

`web/js/dashboard.js` is the dashboard (cards, bubbles, charts, recipe manager, pricing editor,
password dialog); `charts.js` the plotting. `escH` (`dashboard.js:383`) is the only HTML-escaping
path for foreign text — engine model names, Hermes session titles, LAN-submitted recipe names,
pricing labels — with **40 call sites**. Contract pinned by `tests/test_web_escapes.py`: the class
covers `& < > " ' \`, the entity map has an entry per class member, foreign fields reach sinks
escaped (`textContent` and `title="…"`).

The client also owns half of the storage contract: which fields exist on disk is decided by what the
client can name — including `histNum`/`histMs`/`histBasis` (`dashboard.js:104-124`), which builds
`ttft`/`e2e` names at runtime.

## 10. Verification surface

| Guard | File | Catches |
|---|---|---|
| 255 tests / 31 files | `tests/` | everything below is also a test |
| ring storage | `test_ring_storage.py` (12) | unread field stored, caller row mutated, thin losing a minute, row byte budget, a text metric costing the engine its samples, `ring_stats` caching |
| sparkline stats | `test_spark_stats.py` (11) | stats not surviving the prune, the 7-day sparkline amputated, watermark skipping points, stride/keys drifting from the consumer |
| render contract | `test_render_contract.py` (7) | deny-list touching a charted field, dynamic-family drift |
| money types | `test_money_types.py` (4) | text money vanishing from `SUM()` / understating it 1000×, `p + g` 500ing the money endpoint, NULL-as-0.0, a repair that runs twice |
| ladder | `test_tiers.py` (20) | unit confusion, a fold shrinking history, a reader trusting an unbuilt rung |
| money | `test_priced_history.py` (8), `test_lost_potential.py` (17) | prices rewriting history, capacity/duty semantics |
| self-tidy | `test_self_tidy.py` (13) | VACUUM losing rows, guard failing open, probe wedging on the lock |
| engine selection | `test_engine_selection.py` | the wrong engine's money shown |
| security | `test_security_http.py` (16), `test_nodes_ssh_opts.py` (31), `test_auth.py` | unauthenticated writes, ssh option injection, lockout, a query string choosing the scan size |
| portability | `test_portability.py`, `test_portability_linux.py` | Windows path and `/proc` path, and that they agree |
| docs/code drift | `test_docs_knobs.py` | a documented knob the code does not implement |
| explicit encodings | `test_explicit_encodings.py` (2) | a text open that leaves the encoding to the platform — the bug that turned the Windows lane red |
| **upgrade rehearsal** | `scripts/limcars_upgrade_rehearsal.py` | a real old database, old code as the baseline, deep diff of every payload the dashboard renders, plus the file actually shrinking |
| contract diff | `scripts/limcars_contract_snapshot.py` + `limcars_contract_diff.py` | a storage change moving a visible reading; the contract is derived from the app so it cannot drift from what is rendered |
| tidy / ladder CLIs | `scripts/limcars_tidy.py` (`--tier`), `scripts/ladder_live_proof.py` | operator-driven migration with a money gate |
| **gate proof** | `scripts/limcars_gate_proof.py` | a gate that exists but is never called by its route, and the one that matters most: it connects with the **source address bound to the LAN IP**, because the server exempts loopback and a 127.0.0.1 test proves nothing |

Rehearsal/diff semantics worth remembering: point **timestamps** in strided series are grid-anchored
to `now` and legitimately shift between reads; fields **added** are features, fields **removed** are
regressions; lifetime counters are compared for **monotonicity**, not equality; series coverage is
judged by **gap analysis**, not by identical sample sets; `/api/history` downsamples to ~800 rows so
the sample representing a minute is not stable.

## 11. Sharp edges

- `Store` has one non-reentrant `threading.Lock`. Anything called from inside a locked region must
  not take it again (`_capture_spark_stats` documents "caller must hold"); anything acquiring it from
  another thread needs a bounded wait (`_invariants`, `invariant_timeout_s`).
- Boot thin on a 1 GB ring holds that lock ~5 s; the sampler buffers, SSE and charts are memory-fed,
  so it is a delay, not a loss.
- `store.compact()` closes and reopens the connection to drain the WAL while the server keeps
  serving — a reopen bug shows up as "writes silently stopped working".
- An empty `events` table is not evidence a cron ran; check `systemctl --user list-timers` too.
- `histMs`/`histBasis` returning "idle · no requests yet" is a real state, not a broken widget.

## 12. Branch state

`main` (published) + local commits on `feat/priced-history-and-self-tidy` covering: priced history as
earned, self-tidy inside the app, the tiered ladder, the fold-before-trust guard, the bubble refresh
fixes, the ring/stats storage redesign, and the audit fixes. `scripts/limcars_upgrade_rehearsal.py`
and `scripts/limcars_contract_*.py` are the publication gate for all of it.

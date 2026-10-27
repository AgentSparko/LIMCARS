# LIMCARS — Code Map

Regenerated for publication against the final 13-lane stack (30 commits above
`main`). Measured: `ls app/*.py | wc -l` → **25 modules**, `wc -l app/*.py` →
**5,619 lines**, `ls tests/test_*.py | wc -l` → **16 files**, 114 test functions /
**139 collected**, `find web -type f` → 10.

Entry point: `python3 -m app` (`app/__main__.py` → `ThreadingHTTPServer((cfg["host"], cfg["port"]))`).
**`app/server.py` has no `__main__` guard** — `python3 -m app.server` exits silently
without serving, which cost a debugging cycle during the live security proofs.
Stdlib only (`http.server`, `sqlite3`, `urllib`, `subprocess`, `threading`) plus
`psutil`: no web framework, no build step. Frontend is hand-written ES modules
injected by `app/swatch.py`.

## Layout and responsibility

| Module | Owns | Load-bearing detail |
|---|---|---|
| `sampler.py` | Polls engines, builds 1 s samples, drives the ring | `except Exception as exc: self._fail(exc)` sits **inside** the `while not self._stop.is_set()` loop, so one bad scrape is recorded, not fatal |
| `server.py` | Routing, auth gates, config writes, SSE | `_picked_sampler(qs)` must resolve the browser-selected `?engine=`; using `samplers[0]` makes money endpoints report the wrong engine (a lane regressed this; `tests/test_engine_selection.py` locks it) |
| `counters.py` | **The money math** | capacity = best demonstrated hour × 8760; two named utilisation bases |
| `store.py` | SQLite (WAL), retention | `_PRUNEABLE` frozenset of `(table, column)` — never widen to caller strings; `set_persist_window()` + `compact()` apply retention without restart |
| `store.py` rings | what a sample costs on disk | `_DROP_ENGINE` / `_DROP_HW` strip unread fields **at the storage boundary only** — the SSE row keeps all 65/26 fields, and `latest()` reads the in-memory `Ring.last()`, never SQLite, so a strip that leaked into the live row blanks the savings cards; `thin_rings()` keeps 1 s inside `ring_full_seconds` and one sample per minute beyond, behind a before/after minute-set equality proof that rolls the delete back |
| ring field consumers | what may be stored | chart series = `sampler.SERIES_KEYS` (29) + `hw.py` `gpu{i}_{k}`; dynamic = `histMs`/`histBasis` concat `ttft`/`e2e` with `_p50_ms/_avg_ms/_n/_1h` (a literal grep finds no consumer — keep them); cards = in-memory `latest()`; money = `counter_buckets`/`counter_hour`/`counter_day`, **never the rings**. Pinned by `tests/test_ring_storage.py` |
| `recipes.py` | User-authored launch recipes | `shell=True` is deliberate (recipes *are* shell lines) — this is why remote writes fail closed |
| `nodes.py` | Remote host GPU over ssh | options splice **before** `--`, so `sanitise_ssh_opts()` is an execution gate, not a style rule |
| `dialects.py` | Per-engine metric maps | every name must exist in the engine — see Dialects |
| `prom.py` | Prometheus text parsing | histogram families exist only suffixed (`_bucket`/`_sum`/`_count`) |
| `pricing.py` | Cloud price table, tier math | drives every `$` figure; remote-write gated; `display_name`/`provider` are operator strings |
| `live_engines.py`, `hw.py` | discovery, stop, host sample | fixed argv, `shlex.split` |
| `auth.py` | password → scrypt, per-IP lockout | loopback exempt from the write gate |
| `swatch.py`, `broker.py`, `sse.py`, `ring.py`, `bus.py`, `clients.py`, `engine_info.py`, `hermes_home.py`, `session_tokens.py`, `services.py`, `config.py` | templating, pub/sub, transport, settings | `config.py` defaults `host=127.0.0.1`, `persist_seconds=172800` |

## Request flow

```
browser ─HTTP─> server.py ── _authorized() ──> route regex (re.fullmatch) ──> _json()
                  │                              SSE (/api/stream) bypasses _json
                  ├─ counters.py   money math
                  ├─ store.py      rings + hourly counter_buckets + events
                  └─ auth.py       gate decisions

sampler.py / hw.py / nodes.py  ──poll engines, /proc, ssh──> broker topics ──> sse.py
```

## Mutating routes and their gate (default config, no password)

| Route | Loopback | Remote |
|---|---|---|
| `/api/recipes` POST/update/delete | allowed | **428** fail-closed + "set a password" message |
| `/api/config` POST (`pricing`, `window_s`, `live_hz`, `hw_hz`, `persist_seconds`) | allowed, value-validated | pricing **428**; numeric keys allowed |
| `/api/services/<id>/start\|stop` | allowed | **428** |
| `/api/engines-live/<pid>/stop` | allowed | **428** — killing a 26 GB KV-warm engine is an outage |
| `/api/session-tokens`, trace reads | allowed | **428** (trace password) |
| `/api/auth/set` | allowed | proves current password |

Reads (`snapshot`, `counters`, `lost-potential`, `engines-live`, `engine-info`,
`clients`, `savings-history`, `health`) are open by design — the dash is meant to be
watched from a phone on the LAN. The knobs are exactly `LIMCARS_HOST`,
`LIMCARS_PORT`, `LIMCARS_DB`, `LIMCARS_TOKEN`, `LIMCARS_DATA` and `data/config.json`; there is **no**
"allow remote writes" override -- setting a password is the supported path for LAN
administration. (Earlier drafts of these docs promised such a variable; it was never
implemented, and `tests/test_docs_knobs.py` now fails whenever docs and code drift
apart like that.)

## Storage

`data/limcars.db` (SQLite, WAL):

| Table | Grain | Retention | Measured |
|---|---|---|---|
| `engine_samples` | **1 s** | `persist_seconds` default **172800 (48 h)** | 191,649 rows / 3.2 d at the old 7 d default, avg blob 1,694 B |
| `counter_hour` | **1 h** | `history_years` default **1 y** | folded from minutes by `tiers.rollup_hours`; `hour` is **epoch seconds**; measured/synthetic kept in separate columns |
| `counter_day` | **1 day** | **forever** | derived from hours by `tiers.rollup_days`; `day` is an **epoch-day index** — the two units are not interchangeable |
| `hw_samples` | **1 s** | same | 215,215 rows, avg blob 550 B |
| `counter_buckets` | **1 min** | `minute_bucket_days` default **30 d** (then folded into `counter_hour`) | ~14 KB/day; the money source — `ts` is epoch seconds, PRIMARY KEY (engine, ts) |
| `counter_raw` | last absolute counters | never | 1 row; why restarts don't swallow tokens |
| `events` | event log | 30 d | includes `pricing_changed` audit |

The 1-second rings were the 585 MB, not the summaries (`freelist_count` was 0 —
deletion never returned pages). `compact()` vacuums only when freeable pages
justify it; storage tests measure footprint as DB **plus WAL**.

## Money — these are contracts, not comments

- Cloud comparison totals are **prompt + generated**. Cached is a *subset* of prompt,
  reported as detail, never added.
- **Lost potential = capacity − registered saved.** Capacity projects the best
  demonstrated hour to a year (× 8760); a capability claim, never scaled by a working
  day.
- Duty cycle is **measured, never assumed**: no 8-hour day, no "awake hours", no
  single-user assumption.
- **Two explicitly named bases**, and no unsuffixed field may switch between them:
  `hours_basis_measured` → `pct_usage_watched`, `hours_basis_availability`
  (24 h × recording days) → `pct_usage_availability`. `loss_period_cost` is measured;
  `loss_availability_cost` is availability. They may legitimately disagree.
- `usage %` is **not** `100 × saved / capacity`
  (`test_usage_percent_is_not_saved_over_capacity`).

Honesty gates on the lost-potential bubble: engine alive, ≥60 distinct recorded
minutes, best hour ≥15 active minutes — a burst is not a demonstrated rate.

## Dialects — the rule that keeps money honest

`detect()` sniffs the exposition prefix; the map turns engine metric names into the
logical set (`prompt_tokens`, `gen_tokens`, `cached_tokens`, `kv_pct`, `ttft`…).

1. **A mapped name must exist in the engine.** `tests/test_dialect_fixtures.py`
   checks recorded captures (vLLM recorded live from `:8000`, SGLang recorded);
   `tests/test_llamacpp_contract.py` checks the hand-authored llama.cpp fixture.
   Invented names read `None` forever or resolve to a sibling and report someone
   else's number — three were removed (`llamacpp:kv_cache_usage_ratio`,
   `llamacpp:requests_completed`, a reversed `predicted_tokens_total` alias, plus an
   Atlas MTP-acceptance metric). Absence is honest: the UI shows "–".
2. **Provenance travels with the data.** Recorded fixtures carry source URL + date;
   synthetic ones say HAND-AUTHORED in their first lines. Never cite a synthetic
   fixture as evidence of engine behaviour — verify against upstream source.

Adding an engine means adding a dialect and **nothing else**. Never special-case an
engine in `counters.py`, `sampler.py`, or the frontend: engines nobody has heard of
must still render honest numbers.

## Frontend

`web/js/dashboard.js` is the whole dashboard (cards, bubbles, charts, recipe
manager, pricing editor, password dialog). `escH` is the only HTML-escaping path for
foreign text — engine model names, Hermes session titles, LAN-submitted recipe
names, pricing labels — with **32 call sites**. Contract pinned by
`tests/test_web_escapes.py`: class covers `& < > " ' \`, the entity map has an entry
per class member, foreign fields reach sinks escaped. Pricing labels were checked at
both sinks: `textContent` (`:176`) and `title="…"` via `escH` (`:1538`).

## Test inventory

| File | Guards |
|---|---|
| `test_lost_potential.py` | capacity/saved/loss arithmetic, two-basis separation, usage-% identity |
| `test_dialects.py`, `test_dialect_fixtures.py`, `test_llamacpp_contract.py` | name mapping vs engines; invented-name locks |
| `test_engine_selection.py` | `?engine=` honoured by money endpoints |
| `test_web_escapes.py` | `escH` class/map + sinks |
| `test_storage_growth.py` | ring window, `compact()`, WAL-inclusive footprint |
| `test_portability.py`, `test_portability_linux.py` | Windows path **and** `/proc` path, and that they agree |
| `test_nodes_ssh_opts.py` | ssh allowlist; no exec once options refused |
| `test_auth.py`, `test_core.py`, `test_counters.py`, `test_sampler.py`, `test_hw.py`, `test_savings_history.py` | gates, counters, poll loop, hardware, history |

## Branch stack (merge order)

`main` (baseline + escaping, storage, money, dialect tests) ← `pr/01` fail-closed
writes · `pr/02` control-plane auth · `pr/03` pricing gate · `pr/04` ring retention ·
`pr/05` prune allowlist · `pr/07` session-token gate · `pr/06` recorded dialect
fixtures · `pr/08` dashboard UI pass · `pr/09` UX/pricing/theme · `pr/10` hardware
charts · `pr/11` Atlas dialect · `pr/12` llama.cpp dialect · `pr/13` Windows
portability. `pr/08`–`pr/13` are stacked; the rest branch from `main`. Two conflicts
against the security lanes, both additive in `app/server.py` (pricing audit line +
live persist-window setter; fork's label-aware pricing validation + `persist_seconds`
numeric key) — resolutions keep both sides.

# AGENTS.md — instructions for AI agents working with LIMCARS

You are an AI assistant helping the user install, operate, or extend LIMCARS
(Local Inference Monitor · Cost Analyzer · Recipe Server) on their machine.

## What LIMCARS is

A dependency-free Python (stdlib + psutil) dashboard for local LLM inference
servers. Entry point: `python3 -m app` (NOT `python3 -m app.server`).
Server: `http://127.0.0.1:8951` locally — **it binds `0.0.0.0:8951` by
default (config `host` override), so it is LAN-visible**: password gates
cover recipe add/edit/delete and Hermes trace data only. Treat the network
as hostile when migrating data. Backend lives in `app/`, frontend in
`web/`, tests in `tests/` (`/usr/bin/python3 -m pytest tests/ -q`).

## Your most useful job: migrate the user's dashboard history

If the user previously tracked their engine with another dashboard
(an older LIMCARS build, a custom Streamlit panel, a Prometheus/Grafana
setup, a stream-tail logger…), do NOT let them start LIMCARS with an empty
history. The "Lost potential" bubble, the daily money-saved chart and the
counters page all read one table — import the measured past into it.

### The target schema (SQLite, default `data/limcars.db`)

```sql
CREATE TABLE counter_buckets(          -- one row per minute bucket
  engine TEXT, ts INTEGER,             -- UTC, minute-aligned SECONDS (not ms)
  prompt REAL,                         -- TOTAL input tokens, cache hits included
  cached REAL,                         -- cache-hit tokens (a SUBSET of prompt)
  gen REAL,                            -- generated tokens
  reqs REAL, iters REAL,               -- requests / decode iterations (may be 0)
  dec_min REAL, dec_peak REAL,         -- decode tok/s min/peak that minute (NULL ok)
  spec_sum REAL, spec_n INTEGER,       -- speculative acceptance (NULL/0 ok)
  synthetic INTEGER DEFAULT 0,         -- 1 ONLY for estimated seed/gap data
  PRIMARY KEY(engine, ts));
```

(The `synthetic` column is added by a migration in `app/store.py` on
databases created before it existed — pre-creating the table without it is
fine, LIMCARS will ALTER it in.)

`engine` must equal an engine id from `data/config.json` (the id LIMCARS
scrapes), not the old dashboard's name.

### Rules you must follow

1. **Import measured data only.** Never fabricate or "round up" numbers to
   make charts look good — the user checks against what their old dashboard
   showed them, and honest math is the product.
2. **Map cached correctly.** The #1 migration bug: if the source stores
   `prompt` and `cached` as disjoint fields, target `prompt = prompt + cached`;
   if the source's prompt already includes cached, copy as-is. Verify with a
   same-minute cross-check against a period both dashboards recorded, like
   the bundled reference importer does (`new_tok + cached_tok` style event
   tables ⇒ target `prompt = new + cached`, `gen` from decode rows).
2a. **One model often hides behind many engine rows.** The source's engine
   table usually has several rows for the SAME model recorded under
   different names over time: quant or hardware variant suffixes
   (`-hybrid48`, `-nvidia`, `-fp8`, build tags), missing/extra org prefix
   (`Org/Model` vs `model`), case and punctuation drift. Normalize names
   before matching — lowercase, strip everything outside `[a-z0-9]`, strip
   repeated leading org/model-family prefixes — then include every matching
   engine id and SUM their minutes together. If a variant's identity is
   ambiguous (e.g. a different model family with a similar name), ask the
   user before importing it; missing an era of their history is worse than
   one clarifying question.
3. **Minute-align ts** (`ts // 60 * 60`, UTC seconds) — non-aligned rows
   fragment the hourly sliding windows and the money chart.
4. **Never double-count.** Skip `(engine, ts)` minutes LIMCARS already
   sampled live. If both dashboards watched the same window, one of them
   owns each minute.
5. **`synthetic=1` only for estimates** (gap fills, extrapolated totals).
   Real recorded usage is `0`. Synthetic rows are excluded from rate anchors
   and the daily chart by design.
6. **Retention:** rows older than 366 days are pruned on write — do not
   fight it, filter your import instead.
7. **Importing alongside the live sampler is safe with the bundled
   importer** — it writes under `BEGIN IMMEDIATE` with
   `ON CONFLICT(engine,ts) DO NOTHING`, so it can never clobber a minute
   the 2 Hz sampler already recorded (INSERT OR REPLACE would). If you write
   your own importer, use the same pattern or stop `python3 -m app` first.
   The bundled importer also validates `--engine` against
   `data/config.json` and refuses unknown ids, and `--dry-run` never
   creates/modifies the target database.
8. **Read-only on the source.** Open the old database `mode=ro`. Never
   modify someone's legacy install.
9. **Validate after import:** restart the server, then
   `curl -s localhost:8951/api/lost-potential` and `/api/savings-history?days=30`
   must show sane, source-matching numbers (`peak_hour_tokens`, day totals
   within a few % of the old dashboard's own charts). State the cross-check
   numbers to the user; don't just claim success.
10. **Price honesty:** all `$` figures are computed live from
    `data/pricing.json` at import time — import tokens, never dollars.

### Reference implementation

`scripts/import_legacy_history.py` is the bundled example that migrates an
event-table style history (`dash_events`-like schema) into
`counter_buckets`. Use it as the template: read-only source, model-alias
engine matching incl. variant-suffix stripping (rule 2a), minute
aggregation, existing-minute skip, `--dry-run` first, and a full receipt
that prints EVERY source engine with MATCH/no-match — never accept an
import whose receipt silently dropped an era the user remembers using. For other
source dashboards, adapt the field mapping, keep every rule above, and keep
the dry-run/verify loop. If the source keeps its own daily rollup table,
DO NOT import it — daily totals cannot feed the hourly best-window math;
rebuild minutes from the finest-grain table the source has.

### What NOT to import

Only tokens are imported; every `$` and every projection (best hour, pace
projections, "lost potential") is DERIVED by LIMCARS at render time from
`data/pricing.json`. Never port dollar figures — they go stale the moment
pricing changes, and mixing them into token data breaks the honesty rules
the dashboard's lost-potential bubble depends on (measured vs projected).

## Other agent-facing notes

- Settings are plain JSON (`data/settings.json`); theme + palette are
  per-browser cookies (`limcars_ui`), not server settings.
- After any frontend change bump the cache stamp in `web/dashboard.html`
  (`?v=YYYYMMDDx`) and validate the module with
  `node --input-type=module --check < web/js/dashboard.js`.
- UI verification must use computed styles (`getComputedStyle`) and real
  input events — the `.hidden` attribute lies when CSS sets `display`.
- Honesty invariants any agent edit must preserve: `counter_buckets.ts` is
  minute-aligned; `synthetic=1` rows never feed a rate anchor or a $ figure
  (`counter_totals(..., real_only=True)`); every displayed percentile
  carries its sample count and p50 is only shown at n>=20 (engine histogram
  buckets are coarse — a 2-request window p50 is bucket-edge fiction, the
  sum/count mean is the truth); measured best-day/month/year only for
  periods actually watched, pace figures say "at this rate".
- `tests/` must stay green: `/usr/bin/python3 -m pytest tests/ -q`.
- Run it at boot (no user login): install `scripts/limcars-next@.service`
  (templated, `User=%i`, repo-relative launcher) as a SYSTEM unit and
  `systemctl enable --now limcars-next@<user>.service`. Only one process may
  hold port 8951 — stop any hand-started `python3 -m app` first.
- Do not commit `data/` — it holds the user's history and credentials.

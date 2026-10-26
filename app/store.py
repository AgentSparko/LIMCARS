"""sqlite persistence (WAL) for 1 s downsampled history.

Rings are the hot path (in-memory, 15 min); sqlite is the cold path so a
dashboard opened after an engine restart or a weekend still draws real
history instead of an empty chart. Pruned to persist_seconds on write.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time


# Plausibility caps for history reads. Samples written before the prefill
# attribution fix can hold 10k+ tok/s fictions; the sampler refuses to publish
# those today, so the charts must not resurrect them from old rows either.
SERIES_MAX = {"prefill_tps_avg": 12000.0, "prefill_tps_now": 12000.0}


class Store:
    def __init__(self, path: str, persist_seconds: float = 3600):
        self.path = path
        self.persist_s = persist_seconds
        self._lock = threading.Lock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript("""
            CREATE TABLE IF NOT EXISTS engine_samples(
              id INTEGER PRIMARY KEY, t REAL, engine TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS ix_es ON engine_samples(engine, t);
            CREATE TABLE IF NOT EXISTS hw_samples(
              id INTEGER PRIMARY KEY, t REAL, data TEXT);
            CREATE INDEX IF NOT EXISTS ix_hw ON hw_samples(t);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, t REAL, kind TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS counter_buckets(
              engine TEXT, ts INTEGER, prompt REAL, cached REAL, gen REAL,
              reqs REAL, iters REAL, dec_min REAL, dec_peak REAL,
              spec_sum REAL, spec_n INTEGER,
              PRIMARY KEY(engine, ts));
            CREATE INDEX IF NOT EXISTS ix_cb_ts ON counter_buckets(ts);
            CREATE TABLE IF NOT EXISTS counter_raw(
              engine TEXT PRIMARY KEY, t REAL, raw TEXT);
        """)
        # audit M1/M2: seed & gap-backfill buckets are flagged synthetic so
        # the active-hour anchor never treats an engine-lifetime dump as
        # "one busy minute" (fresh installs printed $616k/yr savings).
        # Old rows default 0 — harmless: they predate the seed logic.
        try:
            self._con.execute(
                "ALTER TABLE counter_buckets ADD COLUMN synthetic INTEGER DEFAULT 0")
        except Exception:  # duplicate column on existing DBs
            pass
        # Money as it was EARNED. Cloud prices move (the fetcher refreshes
        # pricing.json, a user edits it), and every $ figure used to be
        # recomputed at read time from whatever the rate was right then — so
        # historical savings silently rewrote themselves. A bucket is a fact,
        # so its cost is frozen here alongside the rates that produced it.
        # NULL means "priced under the old code / no price known then", which
        # readers must NOT report as $0.00.
        for col, typ in (("cost_hot", "REAL"), ("cost_base", "REAL"),
                         ("cost_nocache", "REAL"), ("price_slug", "TEXT"),
                         ("price_in", "REAL"), ("price_out", "REAL"),
                         ("price_cw", "REAL"), ("price_cr", "REAL"),
                         ("price_crhot", "REAL"), ("priced_at", "REAL"),
                         ("price_src", "TEXT")):
            try:
                self._con.execute(
                    f"ALTER TABLE counter_buckets ADD COLUMN {col} {typ}")
            except Exception:  # duplicate column on existing DBs
                pass
        self._con.commit()

    def save_engine_samples(self, engine: str, rows: list[tuple[float, dict]]) -> None:
        with self._lock:
            self._con.executemany(
                "INSERT INTO engine_samples(t, engine, data) VALUES (?,?,?)",
                [(t, engine, json.dumps(r)) for t, r in rows])
            self._con.commit()
        self._prune("engine_samples", "t")

    def save_hw_samples(self, rows: list[tuple[float, dict]]) -> None:
        with self._lock:
            self._con.executemany(
                "INSERT INTO hw_samples(t, data) VALUES (?,?)",
                [(t, json.dumps(r)) for t, r in rows])
            self._con.commit()
        self._prune("hw_samples", "t")

    def save_event(self, kind: str, data: dict) -> None:
        with self._lock:
            self._con.execute("INSERT INTO events(t, kind, data) VALUES (?,?,?)",
                              (time.time(), kind, json.dumps(data)))
            # audit: events was the one table with no retention (flap forever)
            self._con.execute("DELETE FROM events WHERE t < ?",
                              (time.time() - 30 * 86400,))
            self._con.commit()

    # the only (table, column) pairs this class will ever delete on
    _PRUNEABLE = frozenset({("engine_samples", "t"), ("hw_samples", "t")})

    def _prune(self, table: str, col: str) -> None:
        # The statement is built by f-string because SQLite cannot bind
        # identifiers. That is only safe while both names are literals, so pin
        # them: a future caller that threads a request value through here must
        # fail loudly here rather than become injection at runtime.
        if (table, col) not in self._PRUNEABLE:
            raise ValueError(f"refusing to prune {table}.{col}")
        cutoff = time.time() - self.persist_s
        with self._lock:
            self._con.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))  # noqa: S608 — pinned above
            self._con.commit()

    def save_counter_bucket(self, engine: str, ts: int, b: dict) -> None:
        ts = int(ts) // 60 * 60      # defensive: minute buckets must align,
                                     # unaligned rows fragment hourly windows
        synth = 1 if b.get("synthetic") else 0
        with self._lock:
            if synth:
                # A synthetic seed/gap bucket must never clobber a real minute.
                # INSERT OR REPLACE used to overwrite the row and minute_rows()
                # filters synthetic out, so every limcars restart silently
                # deleted the minute of real traffic the seed landed in
                # (found while gating the lost-potential alert on an honest
                # hour of history). A real minute is worth more than a seed.
                cur = self._con.execute(
                    "SELECT COALESCE(synthetic,0) FROM counter_buckets"
                    " WHERE engine=? AND ts=?", (engine, ts)).fetchone()
                if cur is not None and cur[0] == 0:
                    return           # keep the real bucket, drop the seed
            self._con.execute(
                """
                INSERT INTO counter_buckets
                  (engine,ts,prompt,cached,gen,reqs,iters,dec_min,dec_peak,
                   spec_sum,spec_n,synthetic,
                   cost_hot,cost_base,cost_nocache,price_slug,price_in,price_out,
                   price_cw,price_cr,price_crhot,priced_at,price_src)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(engine,ts) DO UPDATE SET
                  prompt=excluded.prompt, cached=excluded.cached, gen=excluded.gen,
                  reqs=excluded.reqs, iters=excluded.iters,
                  dec_min=excluded.dec_min, dec_peak=excluded.dec_peak,
                  spec_sum=excluded.spec_sum, spec_n=excluded.spec_n,
                  synthetic=excluded.synthetic,
                  -- money is write-once: the first stamp is the historical fact.
                  -- Re-saving a minute (restart, gap backfill, importer) must never
                  -- re-price what was already earned, so an existing value always wins.
                  cost_hot=COALESCE(counter_buckets.cost_hot, excluded.cost_hot),
                  cost_base=COALESCE(counter_buckets.cost_base, excluded.cost_base),
                  cost_nocache=COALESCE(counter_buckets.cost_nocache, excluded.cost_nocache),
                  price_slug=COALESCE(counter_buckets.price_slug, excluded.price_slug),
                  price_in=COALESCE(counter_buckets.price_in, excluded.price_in),
                  price_out=COALESCE(counter_buckets.price_out, excluded.price_out),
                  price_cw=COALESCE(counter_buckets.price_cw, excluded.price_cw),
                  price_cr=COALESCE(counter_buckets.price_cr, excluded.price_cr),
                  price_crhot=COALESCE(counter_buckets.price_crhot, excluded.price_crhot),
                  priced_at=COALESCE(counter_buckets.priced_at, excluded.priced_at),
                  price_src=COALESCE(counter_buckets.price_src, excluded.price_src)
                """,
                (engine, ts, b["prompt"], b["cached"], b["gen"], b["reqs"],
                 b["iters"], b["dec_min"], b["dec_peak"], b["spec_sum"],
                 b["spec_n"], synth,
                 b.get("cost_hot"), b.get("cost_base"), b.get("cost_nocache"),
                 b.get("price_slug"), b.get("price_in"), b.get("price_out"),
                 b.get("price_cw"), b.get("price_cr"), b.get("price_crhot"),
                 b.get("priced_at"), b.get("price_src")))
            self._con.commit()
        # 366-day retention, old-LIMCARS parity
        with self._lock:
            self._con.execute("DELETE FROM counter_buckets WHERE ts < ?",
                              (int(time.time()) - 366 * 86400,))
            self._con.commit()

    def set_persist_window(self, seconds: float) -> float:
        """Change the 1 s ring window at runtime and prune immediately, so a
        shrink takes effect without a restart (a restart would still leave the
        old rows until the next write). Clamped to the Settings range.
        """
        self.persist_s = min(604800.0, max(300.0, float(seconds)))
        self._prune("engine_samples", "t")
        self._prune("hw_samples", "t")
        return self.persist_s

    def storage_stats(self, tables: bool = False) -> dict:
        """File-level truth for the operator: how big the database is and how much
        of it is freelist pages that a compact would hand back.

        `tables` runs dbstat, which walks every page — expensive on a large file,
        so it is opt-in (?tables=1) and never on the dashboard's polling path.
        """
        with self._lock:
            pages = self._con.execute("PRAGMA page_count").fetchone()[0]
            free = self._con.execute("PRAGMA freelist_count").fetchone()[0]
            ps = self._con.execute("PRAGMA page_size").fetchone()[0]
            counts = {}
            for t in ("engine_samples", "hw_samples", "counter_buckets", "events"):
                try:
                    counts[t] = self._con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    counts[t] = None
            tops = {}
            if tables:
                try:
                    tops = {r[0]: r[1] for r in self._con.execute(
                        "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
                        " ORDER BY 2 DESC LIMIT 10")}
                except sqlite3.Error:
                    tops = {}
        try:
            wal = os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0
        except OSError:
            wal = 0
        return {"path": self.path, "pages": pages, "free_pages": free,
                "page_size": ps, "bytes": pages * ps, "dead_bytes": free * ps,
                "wal_bytes": wal, "rows": counts, "tables_bytes": tops}

    def compact(self, reclaim_ratio: float = 0.10) -> dict:
        """Return pages to the filesystem after pruning.

        DELETE does not shrink the file: freed pages sit on the freelist and
        the DB stays at its high-water mark forever, which is why a 3-day
        history file measured 615 MB with every page live. VACUUM is a full
        rewrite (I/O proportional to live data), so it only runs when the
        freelist actually holds something worth taking back.
        """
        with self._lock:
            pages = self._con.execute("PRAGMA page_count").fetchone()[0]
            free = self._con.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = self._con.execute("PRAGMA page_size").fetchone()[0]
            out = {"pages": pages, "free_pages": free, "page_size": page_size,
                   "bytes_before": (pages * page_size) if pages else 0,
                   "vacuumed": False}
            if not pages or free < pages * reclaim_ratio:
                return out
            self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._con.execute("VACUUM")
            after = self._con.execute("PRAGMA page_count").fetchone()[0]
            out["vacuumed"] = True
            out["pages"] = after
            out["bytes_after"] = after * page_size
            self._drain_wal(out)
            return out

    def _drain_wal(self, out: dict) -> None:
        """Push the VACUUM's pages from the WAL back into the main file.

        In WAL mode VACUUM rewrites the whole database *through* the WAL, so
        immediately afterwards the main file is still its old size and a fresh
        copy of the new pages sits in -wal: measured on a real 818 MB database,
        VACUUM left a 499 MB WAL and the file looked like it had GROWN by that
        much. A checkpoint issued from the connection that just did the VACUUM
        can return without raising and still leave those pages in place, so this
        checkpoints from fresh connections until the WAL is actually small —
        otherwise every auto-compaction looks like a no-op to the operator.
        """
        try:
            self._con.close()
        except Exception:  # noqa: BLE001 — worst case we simply retry below
            pass
        wal = 0
        for _ in range(40):
            try:
                con = sqlite3.connect(self.path, timeout=120)
                try:
                    con.isolation_level = None
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    con.close()
            except sqlite3.Error:
                break
            try:
                wal = os.path.getsize(self.path + "-wal") if os.path.exists(
                    self.path + "-wal") else 0
            except OSError:
                wal = 0
            if wal <= 64 * 1024:
                break
            time.sleep(0.25)
        out["wal_bytes"] = wal
        try:
            self._con = sqlite3.connect(self.path, check_same_thread=False)
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:                 # pragma: no cover — pathological
            out["reopen_error"] = str(exc)

    def save_counter_raw(self, engine: str, t: float, raw: dict) -> None:
        """Last-seen ABSOLUTE engine counters. Survives limcars restarts so
        the next bucket absorbs the downtime delta (engine keeps serving
        while we are down) — without this, every restart permanently swallows
        a few minutes of tokens into a fresh baseline."""
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO counter_raw(engine, t, raw) VALUES (?,?,?)",
                (engine, t, json.dumps(raw)))
            self._con.commit()

    def get_counter_raw(self, engine: str) -> tuple[float, dict] | None:
        with self._lock:
            r = self._con.execute(
                "SELECT t, raw FROM counter_raw WHERE engine=?", (engine,)).fetchone()
        return (r[0], json.loads(r[1])) if r else None

    def counter_totals(self, engine: str, since_ts: int | None,
                       real_only: bool = False) -> dict:
        """real_only=True excludes synthetic=1 seed/gap buckets — REQUIRED for
        any rate anchor (audit: the lifetime÷tracked fallback once turned a
        seed row into a fantasy $/yr). Panel totals keep them: the seed is
        honest engine-lifetime history, not measured traffic."""
        q = ("SELECT COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0),"
             " COALESCE(SUM(gen),0), COALESCE(SUM(reqs),0), COALESCE(SUM(iters),0),"
             " MIN(dec_min), MAX(dec_peak), COALESCE(SUM(spec_sum),0),"
             " COALESCE(SUM(spec_n),0), MIN(ts), MAX(ts),"
             # money earned at the time, and how much of the range lacks it.
             # Splitting the unpriced rows out (rather than treating NULL as 0)
             # is what lets the reader say "this total is part as-earned, part
             # re-priced today" instead of silently mixing the two.
             " COALESCE(SUM(CASE WHEN cost_hot IS NOT NULL THEN cost_hot END),0),"
             " COALESCE(SUM(CASE WHEN cost_base IS NOT NULL THEN cost_base END),0),"
             " COALESCE(SUM(CASE WHEN cost_nocache IS NOT NULL THEN cost_nocache END),0),"
             " COALESCE(SUM(CASE WHEN cost_hot IS NOT NULL THEN 1 ELSE 0 END),0),"
             " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN 1 ELSE 0 END),0),"
             " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN prompt END),0),"
             " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN cached END),0),"
             " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN gen END),0)"
             " FROM counter_buckets"
             " WHERE engine=?" + (" AND ts>=?" if since_ts is not None else "")
             + (" AND COALESCE(synthetic,0)=0" if real_only else ""))
        params = [engine] + ([since_ts] if since_ts is not None else [])
        with self._lock:
            r = self._con.execute(q, params).fetchone()
        if not r or r[9] is None:
            return {"empty": True}
        (prompt, cached, gen, reqs, iters, dmin, dpeak, ssum, sn, first, last,
         chot, cbase, cnocache, n_priced, n_legacy, lp, lc, lg) = r
        return {"empty": False, "prompt": prompt, "cached": cached, "gen": gen,
                "reqs": reqs, "iters": iters,
                "dec_min": dmin, "dec_peak": dpeak,
                "spec": (ssum / sn) if sn else None,
                "first_ts": first, "last_ts": last,
                "cost_hot": chot, "cost_base": cbase, "cost_nocache": cnocache,
                "money_rows": n_priced, "money_legacy_rows": n_legacy,
                "legacy_prompt": lp, "legacy_cached": lc, "legacy_gen": lg}

    def counter_bucket_rows(self, engine: str, seconds: float) -> list[tuple]:
        """(ts, prompt, cached, gen) raw minute-buckets for sparklines."""
        with self._lock:
            return self._con.execute(
                "SELECT ts,prompt,cached,gen FROM counter_buckets"
                " WHERE engine=? AND ts>=? AND COALESCE(synthetic,0)=0"
                " ORDER BY ts",
                (engine, int(time.time() - seconds))).fetchall()

    def minute_rows(self, engine: str) -> list[tuple]:
        """(ts, prompt, cached, gen) for every non-synthetic minute bucket of
        one engine, oldest first. Feeds the lost-potential best-hour scan."""
        with self._lock:
            return self._con.execute(
                "SELECT ts, prompt, cached, gen FROM counter_buckets"
                " WHERE engine=? AND COALESCE(synthetic,0)=0 ORDER BY ts",
                (engine,)).fetchall()

    def daily_bucket_totals(self, engine: str, since_ts: int) -> list[tuple]:
        """(utc_day, prompt, cached, gen, cost_hot, cost_base, cost_nocache,
        priced_rows, unpriced_rows, unpriced_prompt, unpriced_cached, unpriced_gen)
        calendar-day sums for the daily
        money-saved chart. SYNTHETIC buckets (seed/gap) are EXCLUDED: the
        lifetime seed bucket parked at "today" would otherwise render as one
        engine-lifetime-sized bar dominating the chart (audit F7), and gap
        backfill covers downtime, not the measured day. The chart therefore
        means "what was measured live", consistent with every other rate view.

        Money comes back split into what was frozen at the time and what is
        still unpriced, so the caller never has to present a re-priced figure
        as if it were historical.
        """
        with self._lock:
            return self._con.execute(
                "SELECT ts/86400, COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0),"
                " COALESCE(SUM(gen),0),"
                " COALESCE(SUM(CASE WHEN cost_hot IS NOT NULL THEN cost_hot END),0),"
                " COALESCE(SUM(CASE WHEN cost_base IS NOT NULL THEN cost_base END),0),"
                " COALESCE(SUM(CASE WHEN cost_nocache IS NOT NULL THEN cost_nocache END),0),"
                " COALESCE(SUM(CASE WHEN cost_hot IS NOT NULL THEN 1 ELSE 0 END),0),"
                " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN 1 ELSE 0 END),0),"
                " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN prompt END),0),"
                " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN cached END),0),"
                " COALESCE(SUM(CASE WHEN cost_hot IS NULL THEN gen END),0)"
                " FROM counter_buckets"
                " WHERE engine=? AND ts>=? AND COALESCE(synthetic,0)=0"
                " GROUP BY ts/86400 ORDER BY ts/86400",
                (engine, int(since_ts))).fetchall()

    def series_tail(self, engine: str, key: str, seconds: float,
                    stride_s: int = 60) -> list[tuple]:
        """(t, value) samples of ONE json field, SQL-side strided (~1 per
        stride_s seconds) and json_extract'ed server-side. Sparklines need
        ~200 points; fetching all 2 Hz rows + json.loads under the store
        lock per poll was needlessly heavy (audit F4)."""
        cutoff = time.time() - seconds
        cap = SERIES_MAX.get(key)
        with self._lock:
            cur = self._con.execute(
                "SELECT t, json_extract(data, ?) FROM engine_samples"
                " WHERE engine=? AND t>=? AND CAST(t AS INTEGER) % ? = 0"
                " ORDER BY t", (f"$.{key}", engine, cutoff, int(stride_s)))
            out = [(t, v) for t, v in cur.fetchall()
                   # pre-fix samples carry completion-attribution fictions
                   # (10k+ tok/s prefill); never plot what the sampler itself
                   # would refuse to publish today
                   if v is not None and not (cap is not None
                                             and isinstance(v, (int, float))
                                             and v > cap)]
        return out

    def last_active_hour(self, engine: str) -> dict:
        """Totals of the most recent 60-min span that actually has REAL
        buckets (anchor = newest non-synthetic bucket, window =
        anchor-3600..anchor). Canonical rate basis for ALL cost projections
        (old-dash rule). Synthetic seed/gap buckets are excluded from both
        the anchor and the totals (audit M1: fresh install seeded the whole
        engine lifetime into one bucket -> $/year fantasy figures)."""
        with self._lock:
            r = self._con.execute(
                "SELECT MAX(ts) FROM counter_buckets WHERE engine=?"
                " AND COALESCE(synthetic,0)=0", (engine,)).fetchone()
            if not r or r[0] is None:
                return {"empty": True}
            anchor = r[0]
            r2 = self._con.execute(
                "SELECT COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0),"
                " COALESCE(SUM(gen),0), COUNT(*) FROM counter_buckets"
                " WHERE engine=? AND ts>=? AND COALESCE(synthetic,0)=0",
                (engine, anchor - 3600)).fetchone()
        prompt, cached, gen, n = r2
        # NOTE: this is an activity test, not a token total — cached is summed
        # in deliberately, so an engine whose cached counter is NOT a subset of
        # prompt still counts as active. Displayed totals are always p + g.
        if not n or (prompt + cached + gen) <= 0:
            return {"empty": True}
        return {"empty": False, "prompt": prompt, "cached": cached, "gen": gen,
                "buckets": n}

    def history(self, table: str, engine: str | None, seconds: float) -> list[dict]:
        cutoff = time.time() - seconds
        with self._lock:
            if engine:
                cur = self._con.execute(
                    "SELECT t, data FROM engine_samples WHERE engine=? AND t>=? ORDER BY t",
                    (engine, cutoff))
            else:
                cur = self._con.execute(
                    "SELECT t, data FROM hw_samples WHERE t>=? ORDER BY t", (cutoff,))
            rows = cur.fetchall()
        out = [{"t": t, **json.loads(d)} for t, d in rows]
        if len(out) > 800:                       # stride downsample for charts
            step = -(-len(out) // 800)
            out = out[::step]
        return out

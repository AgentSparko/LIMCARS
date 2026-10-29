// LIMCARS-NEXT chart engine — hand-rolled SVG, zero dependencies.
// Philosophy borrowed from TandemLLM's dashboard: lightweight vector
// charts, hover to inspect, and the house rule ORANGE IS NOW — the newest
// point is always the accent color; series colors stay quiet.
//
// All charts share one global rAF ticker and redraw only when marked dirty
// (a tick of new data or a resize), so 2 Hz data costs ~nothing.

export const COLORS = {
  cobalt: '#4f8cff', teal: '#2dd4bf', violet: '#a78bfa',
  rose: '#fb7185', yellow: '#facc15', green: '#34d399', now: '#ff8c1a',
  muted: '#7f8ca3', grid: '#1c2432',
};

const scol = v => typeof v === 'function' ? v() : v;  // palette-reactive:
                                                       // series may carry a
                                                       // getter resolved at draw
export function fmt(v, d = 1) {
  if (v === null || v === undefined || !Number.isFinite(v)) return '–';
  if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString('en-US');
  return v.toFixed(d).replace(/\.0+$/, m => (Math.abs(v) >= 10 ? m : ''));
}

// ---------------- series store ----------------

export class SeriesStore {
  constructor(retention = 900) { this.map = new Map(); this.retention = retention; }
  push(name, t, v) {
    let a = this.map.get(name);
    if (!a) this.map.set(name, a = []);
    if (a.length && a[a.length - 1][0] > t) return; // out-of-order guard
    a.push([t, v === undefined ? null : v]);
    const cutoff = t - this.retention;
    let drop = 0;
    while (drop < a.length && a[drop][0] < cutoff) drop++;
    if (drop) a.splice(0, drop);
  }
  seed(name, rows) { this.map.set(name, rows.map(r => Array.isArray(r) ? [r[0], r[1]] : null).filter(Boolean)); }
  at(name) { return this.map.get(name) || []; }
}

// ---------------- line chart ----------------

let _uid = 0;
const _charts = new Set();
let _raf = 0;

function _tick() {
  const now = performance.now();
  for (const c of _charts) if (c._dirty || now - c._lastDraw > 500) c.draw();
  _raf = requestAnimationFrame(_tick);
}
if (typeof document !== 'undefined') _tick();

const tip = (typeof document !== 'undefined') ? (() => {
  const d = document.createElement('div'); d.className = 'chart-tip';
  document.body.appendChild(d); return d;
})() : null;

// Which instantaneous rate fields are only meaningful while the engine is actually
// doing the thing they measure. The card layer already dims these on the STICKY
// server-side activity flag ("instantaneous 2 Hz per-tick values flicker bright/dark
// mid-stream"), but the series underneath was fed the raw 2 Hz value — so between
// decode bursts the sparkline plunged to 0 (and back) while the number above it was
// held: the "dancing charts" report, worst on the top row (Gen speed / Prefill / KV).
// A rate that cannot be measured is not a measured zero, so it feeds a gap.
export const RATE_WHILE_ACTIVE = {
  decode_tps_now: 'decode_active', decode_tps_avg: 'decode_active',
  prefill_tps_now: 'prefill_active', prefill_tps_avg: 'prefill_active',
};
// Fields that report a false 0 while idle (vLLM: kv_cache_usage_perc = 0 while the
// prefix-cached blocks are still resident). The number holds the last real reading;
// the curve must hold it too, or the two visibly contradict each other.
export const HOLD_LAST_NONZERO = new Set(['kv_pct']);

/**
 * The value to append for one engine field on one frame — pure, so it can be tested
 * without a DOM (tests/js/series_sample.mjs).
 * @param {string} field   the field name, e.g. 'kv_pct'
 * @param {*} raw          the value the engine reported this frame
 * @param {object} [frame] the whole frame, for the sticky activity flags
 * @param {object} [held]  caller-owned per-engine scratch pad for hold-last values
 * @returns {number|null}  what to store; null means "not measurable", not zero
 */
export function seriesSample(field, raw, frame, held) {
  if (HOLD_LAST_NONZERO.has(field)) {
    if (typeof raw === 'number' && raw > 0) { (held || (held = {}))[field] = raw; return raw; }
    if (raw === 0 && held && typeof held[field] === 'number') return held[field];
    return raw;                     // never seen a real reading yet: 0 is all we know
  }
  const flag = RATE_WHILE_ACTIVE[field];
  if (flag && frame && frame[flag] === false) return null;
  return raw;
}

/**
 * Smooth the false zeros in ALREADY-RECORDED history for display.
 *
 * An idle engine reports kv_cache_usage_perc = 0 while the prefix-cached blocks are
 * still resident (its own tooltip says so), so old samples dip to 0% on every idle
 * stretch. Live samples are handled by seriesSample; this covers what the ring
 * already holds, so the chart reads true end to end instead of only from now on.
 *
 * Display-only on purpose: the stored rows are never rewritten. A genuine
 * "the cache really emptied" moment is indistinguishable from the idle false zero
 * in that data, and inventing history is worse than showing a dip — so anything
 * without a previous positive reading is left exactly as it was.
 *
 * @param {Array<[number, number|null]>} rows  [timestamp, value] pairs, oldest first
 * @returns {Array<[number, number|null]>}     new array; only interior zeros change
 */
export function fillIdleZeros(rows) {
  if (!Array.isArray(rows)) return rows;
  let last = null;
  return rows.map(r => {
    const v = r && r[1];
    if (typeof v === 'number' && v > 0) { last = v; return r; }
    if (v === 0 && typeof last === 'number') return [r[0], last];
    return r;                        // leading zeros and nulls are left alone
  });
}

/**
 * Recorded rate zeros are gaps, not measured zeros.
 *
 * seriesSample turns a rate into null while the sticky activity flag says
 * nothing is running — but the ring stores the RAW sampler rows, so a page
 * load seeds the curve with those zeros anyway and the bubble plunges again
 * (#28 fixed the live layer only). Ring rows carry no flags, and inventing
 * history is worse than showing a gap, so every recorded 0 on a rate field
 * becomes "not measurable" — we only ever subtract displayed data.
 */
export function rateZerosToGaps(rows) {
  if (!Array.isArray(rows)) return rows;
  return rows.map(r => (r && r[1] === 0 ? [r[0], null] : r));
}

// Fields whose 0 means "rate not measurable" (same set seriesSample gates).
export const RATE_FIELDS = new Set(Object.keys(RATE_WHILE_ACTIVE));

export class LineChart {
  /**
   * @param {HTMLElement} host
   * @param {{series:{key,label,color,fill?:boolean,dash?:boolean}[],
   *          store:SeriesStore, window?:number, h?:number,
   *          yFmt?:(v:number)=>string, yMinZero?:boolean}} opts
   */
  constructor(host, opts) {
    this.host = host; this.opts = opts;
    this.store = opts.store;
    this.win = opts.window || 300;
    this.h = opts.h || 180;
    this.yFmt = opts.yFmt || (v => fmt(v, 0));
    this.yFixed = opts.yFixed || null;   // e.g. [0,100] for percentages
    this.yCeil = opts.yCeil || null;      // physical max: OFF view = 0..yCeil
    this.autoY = true; this._frozenMax = null;
    this.id = 'lc' + (++_uid);
    this.hoverX = null; this._dirty = true; this._lastDraw = 0;
    this._svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    this._svg.style.height = this.h + 'px';
    host.appendChild(this._svg);
    this._svg.addEventListener('mousemove', e => {
      const r = this._svg.getBoundingClientRect();
      this.hoverX = e.clientX - r.left; this._ttX = e.clientX; this._ttY = e.clientY;
      this._dirty = true;
    });
    this._svg.addEventListener('mouseleave', () => { this.hoverX = null; this._dirty = true; tip.style.display = 'none'; });
    _charts.add(this);
    this._ro = new ResizeObserver(() => { this._dirty = true; });
    this._ro.observe(host);
  }
  destroy() {   // detached cards must stop being redrawn and observed
    _charts.delete(this); this._ro.disconnect(); tip.style.display = 'none';
  }
  setWindow(sec) { this.win = sec; this._dirty = true; }
  setAutoY(on) {
    this.autoY = on;
    this._dirty = true;
  }
  mark() { this._dirty = true; }

  _visible() {
    const now = Date.now() / 1000;
    // nulls are KEPT as gap markers (draw() breaks the polyline on them);
    // filtering them here is what let the old renderer bridge gaps with
    // straight connectors.
    return this.opts.series.map(s => {
      return this.store.at(s.key).filter(([t, v]) => t >= now - this.win && (v === null || Number.isFinite(v)));
    });
  }

  draw() {
    const W = this._svg.clientWidth || 300, H = this.h;
    const PL = 46, PR = 10, PT = 8, PB = 18;
    const data = this._visible();
    let lo, hi;
    if (this.yFixed) {
      lo = this.yFixed[0]; hi = this.yFixed[1];
    } else if (!this.autoY) {
      // OFF = full view anchored at zero up to the PHYSICAL maximum when
      // known (GPU 110 W, temp 100 °C, RAM 128 GB, percentages 100) — the
      // reading's true amplitude against the real-world range. No ceiling:
      // 0..observed max. ON fits both ends of the data (zoom).
      let mx = 0;
      for (const pts of data) for (const [, v] of pts) { if (v > mx) mx = v; }
      if (!Number.isFinite(mx) || mx <= 0) mx = 1;
      lo = 0;
      hi = this.yCeil || mx * 1.08;
    } else {
      // AUTO Y: fit BOTH ends to the visible data — a 60–70 °C series gets a
      // 58–72 scale, not a 0–70 one that draws a flat line at the top.
      // Null gap markers are skipped: `null < mn` coerces to 0 and would
      // silently pin the floor to zero.
      let mn = Infinity, mx = 0;
      for (const pts of data) for (const [, v] of pts) { if (v === null) continue; if (v < mn) mn = v; if (v > mx) mx = v; }
      if (!Number.isFinite(mn)) { mn = 0; mx = 1; }
      if (mx <= mn) mx = mn + (Math.abs(mn) * 0.1 || 1);
      const pad = (mx - mn) * 0.10 || Math.abs(mx) * 0.05 || 1;
      // ON = always fit to the visible data range (never pinned to zero, or
      // the toggle would render identical to the zero-based OFF view)
      hi = mx + pad;
      lo = Math.max(0, mn - pad);
      if (hi - lo < 1e-9) hi = lo + 1;
      this._scale = [lo, hi];
    }
    const max = hi, min = lo;
    const now = Date.now() / 1000;
    const x = t => PL + (t - (now - this.win)) / this.win * (W - PL - PR);
    const y = v => H - PB - ((v - min) / (max - min)) * (H - PT - PB);

    let out = '';
    // gridlines + y labels
    for (let i = 0; i <= 3; i++) {
      const gv = min + (max - min) * i / 3, gy = y(gv);
      out += `<line x1="${PL}" x2="${W - PR}" y1="${gy}" y2="${gy}" stroke="${COLORS.grid}" stroke-width="1"/>`;
      out += `<text x="${PL - 6}" y="${gy + 3.5}" text-anchor="end" font-size="10" fill="${COLORS.muted}" font-family="var(--mono)">${this.yFmt(gv)}</text>`;
    }
    // x time labels — seconds-ago for short windows, HH:MM for long (classic style)
    const steps = Math.max(2, Math.floor((W - PL - PR) / 90));
    for (let i = 0; i <= steps; i++) {
      const tt = now - this.win + this.win * i / steps;
      const label = this.win >= 900
        ? new Date(tt * 1000).toTimeString().slice(0, 5)
        : `-${Math.round(now - tt)}s`;
      out += `<text x="${x(tt)}" y="${H - 5}" text-anchor="middle" font-size="9.5" fill="${COLORS.muted}">${label}</text>`;
    }
    // series
    let hoverRows = null;
    this.opts.series.forEach((s, si) => {
      const pts = data[si];
      if (!pts.length) return;
      // Break the polyline on null gap markers — one path per finite run, so
      // an unmeasurable stretch renders BLANK instead of as a straight
      // connector between the samples on either side.
      const segs = [];
      let cur = null;
      for (const [t, v] of pts) {
        if (v === null) { cur = null; continue; }
        if (!cur) { cur = []; segs.push(cur); }
        cur.push([t, v]);
      }
      if (!segs.length) return;
      let d = '', area = '';
      const base = y(Math.max(min, 0));
      for (const seg of segs) {
        d += seg.map(([t, v], k) => `${k ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join('');
        if (s.fill) area += `M${x(seg[0][0]).toFixed(1)},${base.toFixed(1)}`
          + seg.map(([t, v]) => `L${x(t).toFixed(1)},${y(v).toFixed(1)}`).join('')
          + `L${x(seg[seg.length - 1][0]).toFixed(1)},${base.toFixed(1)}Z`;
      }
      if (s.fill) {
        out += `<path d="${area}" fill="${scol(s.color)}" opacity="0.10"/>`;
      }
      out += `<path d="${d}" fill="none" stroke="${scol(s.color)}" stroke-width="1.6" ${s.dash ? 'stroke-dasharray="4 3"' : ''} stroke-linejoin="round"/>`;
      let last = null;
      for (let i = pts.length - 1; i >= 0; i--) if (pts[i][1] !== null) { last = pts[i]; break; }
      if (last) out += `<circle cx="${x(last[0])}" cy="${y(last[1])}" r="3" fill="${COLORS.now}"/>`;
    });
    // hover crosshair
    if (this.hoverX != null && this.hoverX > PL && this.hoverX < W - PR) {
      out += `<line x1="${this.hoverX}" x2="${this.hoverX}" y1="${PT}" y2="${H - PB}" stroke="#3a4a66" stroke-dasharray="3 3"/>`;
      hoverRows = this._hoverValues(this.hoverX, W, now, x);
    }
    this._svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    this._svg.innerHTML = out;
    this._dirty = false; this._lastDraw = performance.now();
    if (hoverRows && hoverRows.length) {
      tip.style.display = 'block';
      tip.style.left = Math.min(window.innerWidth - 220, this._ttX + 14) + 'px';
      tip.style.top = (this._ttY + 12) + 'px';
      tip.innerHTML = hoverRows;
    }
  }

  _hoverValues(px, W, now, x) {
    const inv = now - this.win + (px - 46) / (W - 56) * this.win;
    let tLabel = `-${Math.round(now - inv)}s`;
    const rows = this.opts.series.map(s => {
      const pts = this.store.at(s.key);
      let best = null, bd = 1e9;
      for (const [t, v] of pts) {
        const d = Math.abs(t - inv);
        if (d < bd) { bd = d; best = v; }
      }
      if (best === null || bd > 3) return null;
      return `<span style="color:${scol(s.color)}">●</span> ${s.label}: <b>${(this.opts.yFmt || fmt)(best)}</b>`;
    }).filter(Boolean);
    if (!rows.length) { tip.style.display = 'none'; return null; }
    return `<div class="t">${tLabel}</div>` + rows.join('<br>');
  }
}

// ---------------- sparkline (bubbles) ----------------

export function spark(svgEl, values, color = COLORS.cobalt, opts = {}) {
  color = typeof color === 'function' ? color() : color;
  const W = svgEl.clientWidth || 150, H = 30, n = values.length;
  svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  if (!n) { svgEl.innerHTML = ''; return; }
  let max = 0, min = Infinity;
  for (const v of values) if (Number.isFinite(v)) { if (v > max) max = v; if (v < min) min = v; }
  if (!Number.isFinite(min)) min = 0;
  if (max <= min) max = min + 1;
  const y = v => H - 3 - ((v - min) / (max - min)) * (H - 6);
  const x = i => 1 + i * (W - 2) / Math.max(1, n - 1);
  // Draw at most ~1 point per 1.5 px. Beyond that the screen cannot resolve the
  // series at all, and because x() divides by n, EVERY appended sample shifts all
  // the others a sub-pixel to the left — a bubble that has been on screen a while
  // (the lifetime and savings ones, which no caller thins) crawled/danced on every
  // poll until a reload reset the array. Index is preserved, so the shape, the
  // span and the hover readout all stay true to the full series.
  const CAP = Math.max(16, Math.floor(W / 1.5));
  let idxs = [];
  for (let i = 0; i < n; i++) if (Number.isFinite(values[i])) idxs.push(i);
  if (idxs.length > CAP) {
    const stride = Math.ceil(idxs.length / CAP);
    const kept = idxs.filter((_, k) => k % stride === 0);
    if (kept[kept.length - 1] !== idxs[idxs.length - 1]) kept.push(idxs[idxs.length - 1]);
    idxs = kept;
  }
  // A null is an unmeasurable stretch (the #28 gap policy), so the polyline
  // must BREAK there. The old builder filtered non-finite values and drew the
  // kept samples as ONE path — which stitched a straight line across every
  // gap, connecting distant real samples through invisible terrain (owner
  // screenshot: prefill curve "long perfectly straight diagonals").
  // Consecutive kept indices separated only by THINNING skips (finite values)
  // do not break; only true non-finite terrain does.
  const segs = [];
  let cur = null, prev = -2;
  for (const i of idxs) {
    let gap = false;
    for (let j = prev + 1; j < i; j++) if (!Number.isFinite(values[j])) { gap = true; break; }
    if (!cur || gap) { cur = []; segs.push(cur); }
    cur.push(i); prev = i;
  }
  let d = '', fillD = '';
  for (const seg of segs) {
    d += seg.map((i, k) => (k ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(values[i]).toFixed(1)).join('');
    fillD += `M${x(seg[0])},${H}`
      + seg.map(i => 'L' + x(i).toFixed(1) + ',' + y(values[i]).toFixed(1)).join('')
      + `L${x(seg[seg.length - 1])},${H}Z`;
  }
  const lastIdx = idxs.length ? idxs[idxs.length - 1] : -1;
  let out = '';
  if (d) {
    out += `<path d="${fillD}" fill="${color}" opacity="0.10"/>`;
    out += `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.4"/>`;
    const lv = values[lastIdx];
    if (Number.isFinite(lv)) out += `<circle cx="${x(lastIdx)}" cy="${y(lv)}" r="2.4" fill="${COLORS.now}"/>`;
  }
  svgEl.innerHTML = out;
  // hover readout: nearest sample -> shared tooltip + marker dot (values,
  // optional times and formatter ride along on the element for the listener)
  svgEl._spark = { values, min, max, n, W, color, x, y, idxs, fmt: opts.fmt || (v => fmt(v, 1)), times: opts.times };
  if (!svgEl._sparkBound && typeof document !== 'undefined') {
    svgEl._sparkBound = true;
    svgEl.style.cursor = 'crosshair';
    const kill = () => { if (svgEl._mk) { svgEl._mk.remove(); svgEl._mk = null; } tip.style.display = 'none'; };
    svgEl.addEventListener('mousemove', e => {
      const s = svgEl._spark; if (!s || !s.n) return;
      const r = svgEl.getBoundingClientRect();
      const px = (e.clientX - r.left) * (s.W / (r.width || s.W));
      const i0 = Math.max(0, Math.min(s.n - 1, Math.round((px - 1) * Math.max(1, s.n - 1) / (s.W - 2))));
      // series may contain null gaps, and long series are thinned to the pixel
      // budget: snap to the nearest sample that is actually DRAWN, so the marker
      // dot and the readout can never disagree with what the eye sees
      const cand = (s.idxs && s.idxs.length) ? s.idxs
        : (() => { const a = []; for (let k = 0; k < s.n; k++) if (Number.isFinite(s.values[k])) a.push(k); return a; })();
      if (!cand.length) { kill(); return; }
      let i = cand[0], best = Infinity;
      for (const k of cand) { const dd = Math.abs(k - i0); if (dd < best) { best = dd; i = k; } }
      const v = s.values[i];
      if (!Number.isFinite(v)) { kill(); return; }
      let label = s.fmt(v);
      if (s.times && s.times[i] != null) {
        const d = Date.now() / 1000 - s.times[i];
        label += ' · ' + (d < 90 ? Math.max(0, Math.round(d)) + 's' : Math.round(d / 60) + 'm') + ' ago';
      }
      tip.textContent = label;
      tip.style.display = 'block';
      tip.style.left = Math.min(window.innerWidth - 220, e.clientX + 14) + 'px';
      tip.style.top = (e.clientY + 12) + 'px';
      if (!svgEl._mk) {
        svgEl._mk = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        svgEl._mk.setAttribute('r', '2.6'); svgEl._mk.setAttribute('fill', scol(s.color));
      }
      svgEl.appendChild(svgEl._mk);   // spark() rewrites innerHTML per tick — keep it attached
      svgEl._mk.setAttribute('cx', s.x(i)); svgEl._mk.setAttribute('cy', s.y(v));
    });
    svgEl.addEventListener('mouseleave', kill);
  }
}

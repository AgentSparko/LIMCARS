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
    new ResizeObserver(() => { this._dirty = true; }).observe(host);
  }
  setWindow(sec) { this.win = sec; this._dirty = true; }
  setAutoY(on) {
    this.autoY = on;
    this._dirty = true;
  }
  mark() { this._dirty = true; }

  _visible() {
    const now = Date.now() / 1000;
    return this.opts.series.map(s => {
      return this.store.at(s.key).filter(([t, v]) => t >= now - this.win && v !== null && Number.isFinite(v));
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
      let mn = Infinity, mx = 0;
      for (const pts of data) for (const [, v] of pts) { if (v < mn) mn = v; if (v > mx) mx = v; }
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
      const d = pts.map(([t, v], i) => `${i ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join('');
      if (s.fill) {
        const base = y(Math.max(min, 0));
        const area = d + `L${x(pts[pts.length - 1][0]).toFixed(1)},${base}L${x(pts[0][0]).toFixed(1)},${base}Z`;
        out += `<path d="${area}" fill="${scol(s.color)}" opacity="0.10"/>`;
      }
      out += `<path d="${d}" fill="none" stroke="${scol(s.color)}" stroke-width="1.6" ${s.dash ? 'stroke-dasharray="4 3"' : ''} stroke-linejoin="round"/>`;
      const last = pts[pts.length - 1];
      out += `<circle cx="${x(last[0])}" cy="${y(last[1])}" r="3" fill="${COLORS.now}"/>`;
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
  let d = '', lastIdx = -1;
  values.forEach((v, i) => {
    if (!Number.isFinite(v)) return;
    d += (d ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1);
    lastIdx = i;
  });
  let out = '';
  if (d) {
    out += `<path d="${d}L${x(lastIdx)},${H}L${x(0)},${H}Z" fill="${color}" opacity="0.10"/>`;
    out += `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.4"/>`;
    const lv = values[lastIdx];
    if (Number.isFinite(lv)) out += `<circle cx="${x(lastIdx)}" cy="${y(lv)}" r="2.4" fill="${COLORS.now}"/>`;
  }
  svgEl.innerHTML = out;
  // hover readout: nearest sample -> shared tooltip + marker dot (values,
  // optional times and formatter ride along on the element for the listener)
  svgEl._spark = { values, min, max, n, W, color, x, y, fmt: opts.fmt || (v => fmt(v, 1)), times: opts.times };
  if (!svgEl._sparkBound && typeof document !== 'undefined') {
    svgEl._sparkBound = true;
    svgEl.style.cursor = 'crosshair';
    const kill = () => { if (svgEl._mk) { svgEl._mk.remove(); svgEl._mk = null; } tip.style.display = 'none'; };
    svgEl.addEventListener('mousemove', e => {
      const s = svgEl._spark; if (!s || !s.n) return;
      const r = svgEl.getBoundingClientRect();
      const px = (e.clientX - r.left) * (s.W / (r.width || s.W));
      const i0 = Math.max(0, Math.min(s.n - 1, Math.round((px - 1) * Math.max(1, s.n - 1) / (s.W - 2))));
      // series may contain null gaps — snap to the nearest finite sample
      let i = i0;
      if (!Number.isFinite(s.values[i])) {
        let l = i0 - 1, r = i0 + 1;
        while (l >= 0 && !Number.isFinite(s.values[l])) l--;
        while (r < s.n && !Number.isFinite(s.values[r])) r++;
        const dl = l >= 0 ? i0 - l : Infinity, dr = r < s.n ? r - i0 : Infinity;
        i = dl <= dr ? l : r;
      }
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

// LIMCARS Classic — full rebuild of the old hub/dash.html canvas dash
// (the one Streamlit labelled "🎛️ Modern") on the new 2 Hz backend.
// Same reading order: sidebar system tiles + side charts, hero cards,
// latency/live/spec/savings bubble rows, six-chart grid, model table.

import { SeriesStore, LineChart, spark, fmt, COLORS } from './charts.js?v=20261023h';

const state = {
  store: new SeriesStore(4000),
  engines: {}, hw: {}, pricing: null,
  windowSec: 900, charts: [], reqFeed: [],
};
window.LC_CLASSIC = state;

// ---------------- definitions ----------------

const num = (v, d = 0) => fmt(v, d);

const HERO = [];

const BUBS = {
  'bub-main': [
    { label: 'Gen speed', series: 'decode_tps_now', color: COLORS.green,
      dimWhen: 'decode_active',
      sparkFmt: v => v.toFixed(1) + ' tok/s',
      main: l => tps(l.decode_tps_now), sub: l => `tok/s now · avg ${tps(l.decode_tps_avg)}` },
    { label: 'Prefill speed', series: 'prefill_tps_avg', color: COLORS.teal, dimZero: true,
      dimWhen: 'prefill_active',
      sparkFmt: v => v.toFixed(1) + ' tok/s',
      main: l => tps(l.prefill_tps_avg), sub: l => `uncached tok / prefill-sec · burst ${tps(l.prefill_tps_now)}` },
    { label: 'KV cache used', hot: true, series: 'kv_pct', color: COLORS.yellow, holdZero: true,
      sparkFmt: v => v.toFixed(1) + ' %',
      main: l => pct(l.kv_pct),
      sub: l => l.kv_pool_tokens
        ? `${(l.kv_used_tokens ?? 0).toLocaleString()} / ${l.kv_pool_tokens.toLocaleString()} tok used · ${(l.kv_free_tokens ?? 0).toLocaleString()} free`
        : '% of token pool' },
    { label: 'Running reqs', series: 'running', color: COLORS.green,
      sparkFmt: v => String(Math.round(v)),
      main: l => num(l.running), sub: l => `${num(l.waiting)} waiting` },
    { label: 'TTFT (p50)', series: 'ttft_p50_ms', color: COLORS.violet, hist: true,
      sparkFmt: v => Math.round(v) + ' ms',
      main: l => ms(l.ttft_p50_ms), sub: () => 'time to first token · 30 s window' },
    { label: 'e2e latency (p50)', series: 'e2e_p50_ms', color: COLORS.violet, hist: true,
      sparkFmt: v => (v / 1000).toFixed(2) + ' s',
      main: l => sec(l.e2e_med_1h != null ? l.e2e_med_1h : l.e2e_p50_ms),
      sub: l => `median · last hour · now ${num(l.e2e_p50_ms)} ms · lifetime ${num(l.e2e_life_avg_ms)} ms` },
    { label: 'Spec acceptance', series: 'spec_accept_pct', color: COLORS.violet,
      sparkFmt: v => v.toFixed(1) + ' %',
      main: l => pct(l.spec_accept_pct),
      sub: l => `accept len ${l.spec_accept_len != null ? l.spec_accept_len.toFixed(2) : '–'} tok · `
              + `${l.draft_tok_per_step != null ? l.draft_tok_per_step.toFixed(1) : '–'} draft tok/step · DFlash` },
    { label: 'Prefix cache hit', series: 'cache_hit_pct', life: true, color: COLORS.teal,
      sparkFmt: v => v.toFixed(1) + ' %',
      main: l => pct(l.cache_hit_med_1h != null ? l.cache_hit_med_1h : l.cache_hit_pct),
      sub: l => `median · last hour · now ${pct(l.cache_hit_pct)} · lifetime ${pct(l.cache_hit_life_pct)}` },
    { label: 'Gen speed peak', series: 'gen_peak', color: COLORS.cobalt,
      sparkFmt: v => v.toFixed(1) + ' tok/s',
      main: l => tps(l.gen_peak), sub: () => 'tok/s · last hour' },
  ],
  'bub-savings': [
    { label: 'Projected save · day', hot: true, series: 'savings_day', savings: 'day', color: COLORS.green,
      sparkFmt: v => '$' + v.toFixed(2) },
    { label: 'Projected save · month', hot: true, series: 'savings_month', savings: 'month', color: COLORS.green,
      sparkFmt: v => '$' + v.toFixed(2) },
    { label: 'Projected save · year', hot: true, series: 'savings_year', savings: 'year', color: COLORS.green,
      sparkFmt: v => '$' + v.toFixed(0) },
  ],
};

const SIDE_TILES = [
  ['CPU', h => [num(h.cpu_pct, 0) + '%']],
  ['GPU util', h => [num(h.gpu_util, 0) + '%']],
  ['CPU temp', h => [h.cpu_temp_c != null ? h.cpu_temp_c.toFixed(0) + ' °C' : '–']],
  ['GPU temp', h => [h.gpu_temp_c != null ? h.gpu_temp_c.toFixed(0) + ' °C' : '–']],
  ['GPU power', h => [h.gpu_power_w != null ? h.gpu_power_w.toFixed(1) + ' W' : '–']],
  ['GPU clock', h => [h.gpu_clock_mhz != null ? num(h.gpu_clock_mhz) + ' MHz' : '–']],
  ['Unified RAM', h => [h.mem_total_gb != null ? (h.mem_total_gb - h.mem_used_gb).toFixed(1) + ' GB free' : '–']],
  ['SSD free', h => [h.disk_total_gb != null ? (h.disk_total_gb - h.disk_used_gb).toFixed(0) + ' GB free' : '–']],
  ['Net traffic', h => [h.net_r_mbs != null ? `↓${h.net_r_mbs.toFixed(2)} MB/s ↑${(h.net_w_mbs ?? 0).toFixed(2)} MB/s` : '–']],
];

const pct = v => v != null ? v.toFixed(1) + '%' : '–';
const ms = v => v != null ? Math.round(v) + ' ms' : '–';
const sec = v => v != null ? (v / 1000).toFixed(2) + ' s' : '–';
const tps = v => v != null ? Math.round(v).toLocaleString('en-US') + ' tok/s' : '–';

function savingsFor(l, secs) {
  const p = state.pricing;
  if (!p || !l) return '–';
  const pre = l.prefill_tps_avg || 0, dec = l.decode_tps_avg || 0;
  if (!pre && !dec) return '–';
  const perSec = (pre * p.input_per_mtok + dec * p.output_per_mtok) / 1e6;
  return '$' + (perSec * secs).toLocaleString('en-US', { maximumFractionDigits: 0 });
}

// ---------------- build ----------------

const eng = () => Object.keys(state.engines)[0];
const latest = () => (state.engines[eng()] || {}).latest || {};
const hwLatest = () => state.hw.latest || {};

const cardRefs = [];

function buildCards() {
  for (const [id, defs] of Object.entries(BUBS)) {
    const host = document.querySelector('#' + id);
    host.innerHTML = '';
    for (const d of defs) {
      const el = document.createElement('div');
      el.className = 'bubble' + (d.hot ? ' hot' : '');
      el.innerHTML = `<div class="label">${d.label}</div>
        <div class="value"><span class="v">–</span></div><div class="sub"></div>
        ${d.series ? '<svg class="spark"></svg>' : ''}`;
      host.appendChild(el);
      cardRefs.push({ el, d, v: el.querySelector('.v'), sub: el.querySelector('.sub'), sp: el.querySelector('.spark') });
    }
  }
  const hero = document.querySelector('#hero');
  hero.innerHTML = '';
  for (const d of HERO) {
    const el = document.createElement('div');
    el.className = 'bubble' + (d.hot ? ' hot' : '');
    el.innerHTML = `<div class="label">${d.label}</div>
      <div class="value"><span class="v">–</span></div><div class="sub"></div>
      <svg class="spark"></svg>`;
    hero.appendChild(el);
    cardRefs.push({ el, d, hero: true, v: el.querySelector('.v'), sub: el.querySelector('.sub'), sp: el.querySelector('.spark') });
  }
  document.querySelector('#side-tiles').innerHTML = SIDE_TILES.map(([l]) =>
    `<div class="st"><div class="l">${l}</div><div class="v">–</div></div>`).join('');
  const p = state.pricing;
  document.querySelector('#pricing-note').textContent = p
    ? `· vs ${p.display_name || p.slug || 'API'} @ $${p.cache_write} w / $${p.cache_read} read / $${p.output} out per Mtok` +
      (p.provider ? ` (${p.provider})` : '') + (p.fetched_at ? ` · prices ${String(p.fetched_at).slice(0, 10)}` : '')
    : '· no pricing entry for this model';
}

function buildCharts() {
  const st = state.store, e = eng();
  const grid = document.querySelector('#charts-grid');
  grid.innerHTML = '';
  state.charts = [];
  const mk = (host, title, series, opts = {}) => {
    const card = document.createElement('div');
    card.className = 'chart-card';
    card.innerHTML = `<div class="head"><span class="title">${title}</span>
      <span class="cur"></span><span class="legend" style="margin-left:auto"></span>
      <button class="pill tiny autoy on" style="margin-left:8px">AUTO Y</button></div><div class="ch"></div>`;
    grid.appendChild(card);
    const c = new LineChart(card.querySelector('.ch'), { store: st, window: state.windowSec, h: 200, series, ...opts });
    const btn = card.querySelector('.autoy');
    if (opts.yFixed) { btn.style.display = 'none'; }
    else btn.addEventListener('click', () => {
      const on = c.autoY ? false : true;
      c.setAutoY(on); btn.classList.toggle('on', on);
    });
    const leg = card.querySelector('.legend');
    leg.innerHTML = series.map(s => `<span><i style="background:${s.color}"></i>${s.label}</span>`).join('');
    card._chart = c; card._cur = card.querySelector('.cur'); card._series = series;
    state.charts.push(card);
    return c;
  };
  mk(0, `Decode speed · last <span class="w">15 min</span>`, [
    { key: `${e}.decode_tps_now`, label: 'tok/s', color: COLORS.cobalt, fill: true }], { curFmt: v => v != null ? Math.round(v) + ' tok/s' : '' });
  mk(0, 'Prefill speed · last <span class="w">15 min</span>', [
    { key: `${e}.prefill_tps_avg`, label: 'Δtok/Δprefill-sec', color: COLORS.teal, fill: true }]);
  mk(0, 'KV pool (0–100%) · last <span class="w">15 min</span>', [
    { key: `${e}.kv_pct`, label: '%', color: COLORS.yellow, fill: true }], { yCeil: 100, yFmt: v => v.toFixed(0) });
  mk(0, 'Speculative accept length · last <span class="w">15 min</span>', [
    { key: `${e}.spec_accept_len`, label: 'tok accepted/step', color: COLORS.violet, fill: true }]);

  // Tokens per session (today) — replaces the old cumulative-tokens chart,
  // which was just two flat lines. Source: Hermes state.db per-session totals.
  const sessCard = document.createElement('div');
  sessCard.className = 'chart-card';
  sessCard.innerHTML = `<div class="head"><span class="title">Tokens per session · today ·
      <span class="mono" id="st-model">…</span></span><span class="cur" id="st-tot"></span></div>
    <div id="sess-tok" class="small" style="min-height:120px"><span class="muted">loading…</span></div>`;
  grid.appendChild(sessCard);
  renderSessionTokens();

  // Per-client context (old LIMCARS parity): one line per hot Hermes session,
  // est. resident context tokens from state.db, 30 min window.
  const ccCard = document.createElement('div');
  ccCard.className = 'chart-card';
  ccCard.id = 'cc-card';
  ccCard.innerHTML = `<div class="head"><span class="title">Per-client context · last 30 min</span>
      <span class="cur" id="cc-max"></span>
      <span class="legend" id="cc-legend" style="margin-left:auto"></span>
      <button class="pill tiny autoy on" id="cc-autoy" style="margin-left:8px">AUTO Y</button></div>
    <div id="cc-box" style="min-height:150px"><span class="muted small">collecting…</span></div>`;
  grid.appendChild(ccCard);
  document.querySelector('#cc-autoy').addEventListener('click', e => {
    ccAutoY = !ccAutoY; e.target.classList.toggle('on', ccAutoY); renderClientChart();
  });
  renderClientChart();

  const side = (hostSel, series, fmtFn, chartOpts = {}) => {
    const host = document.querySelector(hostSel);
    // user rule: hw side charts always show last 3 min — more relevant than
    // following the global window button
    const c = new LineChart(host, { store: st, window: 180, h: 110, series, ...chartOpts });
    const card = host.closest('.side-chart');
    card._fixedWin = true;
    if (chartOpts.autoY === false) {   // user choice: power & RAM default to the
      c.setAutoY(false);               // 0..physical-max view
      card.querySelector('.autoy').classList.remove('on');
    }
    card.querySelector('.autoy').addEventListener('click', () => {
      const on = c.autoY ? false : true; c.setAutoY(on);
      card.querySelector('.autoy').classList.toggle('on', on);
    });
    card._chart = c; card._series = series; card._cur = card.querySelector('.cur'); card._fmt = fmtFn;
    state.charts.push(card);
  };
  // yCeil = physical maximum: with AUTO Y OFF the view is 0..ceiling (real
  // amplitude vs the hardware range); ON zooms to the data extremes.
  side('#sch-power', [{ key: 'hw.gpu_power_w', label: 'W', color: COLORS.cobalt, fill: true }], v => v != null ? v.toFixed(1) + ' W' : '', { yCeil: 110, autoY: false });
  side('#sch-temp', [{ key: 'hw.cpu_temp_c', label: 'CPU °C', color: COLORS.rose },
                     { key: 'hw.gpu_temp_c', label: 'GPU °C', color: COLORS.cobalt }], v => v != null ? v.toFixed(1) + '°' : '', { yCeil: 100 });
  side('#sch-clock', [{ key: 'hw.gpu_clock_mhz', label: 'MHz', color: COLORS.teal, fill: true }], v => v != null ? Math.round(v) + ' MHz' : '', { yCeil: 3003 });
  side('#sch-ram', [{ key: 'hw.mem_used_gb', label: 'GB used', color: COLORS.rose, fill: true }],
    v => v != null ? v.toFixed(1) + ' GB' : '', { yCeil: 128, autoY: false });
}

const escH = s => String(s || '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const CC_COLORS = ['rgb(148,217,168)','rgb(232,193,141)','rgb(74,127,168)','rgb(208,92,74)',
  'rgb(167,139,250)','rgb(94,190,182)','rgb(217,140,180)','rgb(180,200,90)',
  'rgb(230,160,110)','rgb(130,150,220)','rgb(120,210,140)','rgb(200,170,120)'];
const ccColor = c => { let h = 0; for (let i = 0; i < c.length; i++) h = (h * 31 + c.charCodeAt(i)) >>> 0; return CC_COLORS[h % CC_COLORS.length]; };

let ccAutoY = true;   // panel AUTO Y state (matches reference dash_stream.py chart)
async function renderClientChart() {
  const box = document.querySelector('#cc-box');
  if (!box) return;
  let d;
  try { d = await (await fetch('/api/clients?window_s=1800')).json(); }
  catch (e) { return; }
  const legend = document.querySelector('#cc-legend');
  const cur = document.querySelector('#cc-max');
  if (!d.series || !d.series.length) {
    box.innerHTML = '<span class="muted small">collecting… (10 s samples, hot Hermes sessions only)</span>';
    if (legend) legend.innerHTML = '';
    if (cur) cur.textContent = '';
    return;
  }
  const WIN = 1800, H = 170, PL = 46, PR = 12, PT = 8, PB = 16;
  const now = Date.now() / 1000, t0 = now - WIN;
  const W = box.clientWidth || 640;
  const byC = {};
  for (const p of d.series) (byC[p.client] = byC[p.client] || []).push(p);
  const actSet = new Set((d.clients || []).filter(c => c.active).map(c => c.client));
  const vals = d.series.map(p => p.tokens).filter(Number.isFinite);
  const dataMax = Math.max(...vals, 1), dataMin = Math.min(...vals);
  let lo, hi;
  if (ccAutoY) {                                  // zoom: data extremes fill the panel
    const pad = Math.max(1, (dataMax - dataMin) * 0.1);
    lo = Math.max(0, dataMin - pad); hi = dataMax + pad;
  } else { lo = 0; hi = dataMax * 1.08; }
  if (hi <= lo) hi = lo + 1;
  const X = ts => PL + (ts - t0) / WIN * (W - PL - PR);
  const Y = v => PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB);
  let svg = `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
  for (const g of [0, 0.25, 0.5, 0.75, 1]) {      // y gridlines + k-formatted labels
    const v = lo + (hi - lo) * g, y = Y(v);
    svg += `<line x1="${PL}" x2="${W - PR}" y1="${y}" y2="${y}" stroke="#16213a" stroke-width="1"/>`
        +  `<text x="${PL - 5}" y="${y + 3}" text-anchor="end" font-size="9" fill="#5b6c85">${fmtK(Math.round(v))}</text>`;
  }
  for (let i = 0; i <= 4; i++) {                   // clock x labels, spread & edge-clamped
    const t = t0 + WIN * i / 4;
    const lbl = new Date(t * 1000).toTimeString().slice(0, 5);
    const anc = i === 0 ? 'start' : i === 4 ? 'end' : 'middle';
    svg += `<text x="${X(t)}" y="${H - 4}" text-anchor="${anc}" font-size="9" fill="#5b6c85">${lbl}</text>`;
  }
  for (const [c, pts] of Object.entries(byC)) {
    const col = ccColor(c);
    const dd = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.ts).toFixed(1)},${Y(p.tokens).toFixed(1)}`).join('');
    const last = pts[pts.length - 1];
    const lbl = (d.clients.find(x => x.client === c) || {}).title || c.slice(-8);
    svg += `<path d="${dd}" fill="none" stroke="${col}" stroke-width="1.6" opacity="${actSet.has(c) ? 1 : 0.4}"><title>${escH(lbl)} · ${fmtK(last.tokens)} tok</title></path>`;
    if (actSet.has(c)) svg += `<circle cx="${X(last.ts).toFixed(1)}" cy="${Y(last.tokens).toFixed(1)}" r="2.5" fill="${col}"/>`;
  }
  svg += `</svg>`;
  box.innerHTML = svg;
  if (cur) {                                       // current total across clients (old dash top-right)
    const tot = Object.values(byC).reduce((a, pts) => a + (pts[pts.length - 1]?.tokens || 0), 0);
    cur.textContent = `${fmtK(tot)} tok`;
  }
  if (legend) legend.innerHTML = (d.clients || []).slice(0, 12).map(c =>
    `<span><i style="background:${ccColor(c.client)}"></i>${escH(c.title)}${c.active ? ' <b style="color:var(--orange)">●</b>' : ''}</span>`).join('');
}

async function renderSessionTokens() {
  const el = document.querySelector('#sess-tok');
  if (!el) return;
  let d;
  try { d = await (await fetch('/api/session-tokens')).json(); }
  catch (e) { return; }
  const mm = document.querySelector('#st-model');
  if (mm) mm.textContent = d.model || '–';
  const tt = document.querySelector('#st-tot');
  if (tt) tt.textContent = `${fmtK(d.tot_in)} in · ${fmtK(d.tot_out)} out`;
  const rows = (d.sessions || []).slice(0, 14);
  if (!rows.length) { el.innerHTML = '<span class="muted">no sessions for this model today</span>'; return; }
  const max = Math.max(...rows.map(s => s.in + s.out), 1);
  el.innerHTML = rows.map(s => {
    const t = new Date(s.start * 1000).toTimeString().slice(0, 5);
    const wi = (s.in / max * 100).toFixed(1), wo = (s.out / max * 100).toFixed(1);
    const hit = s.cache_r ? ' <span class="muted">· ' + fmtK(s.cache_r) + ' cached</span>' : '';
    return `<div class="st-row" title="${escH(s.id)} · ${s.calls} API calls · last seen ${new Date(s.last * 1000).toLocaleTimeString()}">
      <span class="st-lbl mono" title="${escH(s.title)}">${t} ${escH(s.profile !== 'default' ? s.profile + ' · ' : '')}${escH(s.title || s.id.slice(0, 18))}</span>
      <span class="st-bar"><i style="width:${wi}%;background:${COLORS.cobalt}"></i><i style="width:${wo}%;background:${COLORS.orange || '#ff8c3a'}"></i></span>
      <span class="st-val mono">${fmtK(s.in)} in / ${fmtK(s.out)} out${hit}</span></div>`;
  }).join('');
}

// ---------------- refresh loop ----------------

window.__lc = state;  // debug/testing hook

function refresh() {
  state._held = state._held || {};
  for (const ref of cardRefs) {
    const l = latest();
    if (ref.hero) { ref.v.textContent = ref.d.main(l); ref.sub.textContent = ref.d.sub(l); }
    else if (ref.d.savings) {
      const sv = l.savings;
      ref.v.textContent = sv && sv[ref.d.savings] != null
        ? '$' + sv[ref.d.savings].toLocaleString('en-US', { maximumFractionDigits: sv[ref.d.savings] < 100 ? 2 : 0 })
        : '–';
      ref.sub.textContent = sv && sv.tokens
        ? `${sv.basis} · ${(sv.tokens.prompt / 1000).toFixed(1)}k in + ${(sv.tokens.cached / 1000).toFixed(1)}k cached + ${(sv.tokens.gen / 1000).toFixed(1)}k gen · ${sv.refreshed_at ? new Date(sv.refreshed_at * 1000).toTimeString().slice(0, 8) : '–'}`
        : 'collecting first minute…';
    }
    else { ref.v.textContent = ref.d.main(l); ref.sub.textContent = ref.d.sub ? ref.d.sub(l) : ''; }
    const hk = (ref.hero ? 'H:' : 'B:') + (ref.d.label || ref.d.savings || '');
    // hold-last for metrics that fake-zero when idle (KV cache used): vLLM
    // reports kv_cache_usage_perc = 0 while nothing runs, but the prefix-cached
    // blocks ARE still there — keep the last non-zero reading visible.
    if (ref.d.holdZero) {
      state._heldNz = state._heldNz || {};
      const raw = l ? l[ref.d.series] : null;
      if (raw != null && raw > 0) state._heldNz[hk] = { v: ref.v.textContent, s: ref.sub.textContent };
      else if (raw === 0 && state._heldNz[hk]) {
        ref.v.textContent = state._heldNz[hk].v;
        ref.sub.textContent = state._heldNz[hk].s + ' · held, cache resident';
      }
    }
    // hold-last: when a value blanks out (traffic stopped / window empty),
    // keep showing the last real reading, dimmed — never a bare dash
    const blank = ref.v.textContent === '–';
    if (!blank) state._held[hk] = { v: ref.v.textContent, s: ref.sub.textContent };
    else if (state._held[hk]) {
      ref.v.textContent = state._held[hk].v;
      ref.sub.textContent = state._held[hk].s;
    }
    ref.el.classList.toggle('stale',
      (blank && !!state._held[hk] && !ref.d.hist) ||
      // dim ONLY on the sticky server-side activity flags (1 s hold) —
      // instantaneous 2 Hz per-tick values flicker bright/dark mid-stream
      (!!ref.d.dimZero && l[ref.d.series] === 0) ||
      (!!ref.d.dimWhen && l[ref.d.dimWhen] === false));
    if (ref.sp && ref.d.series) {
      if (ref.d.life) {
        spark(ref.sp, (state._cacheHitLife || []).map(r => r[1]), ref.d.color,
              { times: (state._cacheHitLife || []).map(r => r[0]), fmt: ref.d.sparkFmt });
      } else if (ref.d.savings && state._savSparks && state._savSparks[ref.d.savings]) {
        // rolling 60-min hot-cost $/hr from persisted minute-buckets — the
        // three bubbles get 6 h / 3 d / 7 d windows, so lines truly vary
        const rows = state._savSparks[ref.d.savings];
        spark(ref.sp, rows.map(r => r[1]), ref.d.color,
              { times: rows.map(r => r[0]), fmt: v => '$' + v.toFixed(2) + '/hr' });
      } else {
        const now = Date.now() / 1000;
        let rows = state.store.at(`${eng()}.${ref.d.series}`).filter(([t]) => t >= now - 3600);
        const step = Math.max(1, Math.ceil(rows.length / 200));
        rows = rows.filter((_, i) => i % step === 0 || i === rows.length - 1);
        spark(ref.sp, rows.map(r => r[1]), ref.d.color,
              { times: rows.map(r => r[0]), fmt: ref.d.sparkFmt });
      }
    }
  }
  // side tiles
  const h = hwLatest();
  document.querySelectorAll('#side-tiles .st').forEach((el, i) => {
    el.querySelector('.v').textContent = SIDE_TILES[i][1](h)[0];
  });
  // chart current values — hold last reading, dimmed, when stream blanks
  for (const card of state.charts) {
    if (!card._cur) continue;
    const s = card._series[0];
    const rows = state.store.at(s.key);
    const last = rows.length ? rows[rows.length - 1][1] : null;
    const fmtFn = card._fmt || (card._chart.opts.curFmt || (v => v != null ? fmt(v, 1) : ''));
    if (last != null) card._lastCur = fmtFn(last);
    card._cur.textContent = last != null ? fmtFn(last) : (card._lastCur || '');
    card._cur.classList.toggle('stale', last == null && !!card._lastCur);
  }
  // engine pill
  const e = state.engines[eng()] || {};
  const meta = e.meta || {};
  document.querySelector('#engine-dot').className = 'dot ' + (meta.alive ? 'up' : 'down');
  document.querySelector('#engine-name').textContent = `${meta.model || meta.kind || '–'} @ ${meta.url || ''}`;
  renderModelTable();
}

function renderModelTable(force) {
  const nowMs = Date.now();
  if (!force && state._mtblAt && nowMs - state._mtblAt < 4000 && state._mtblExp === state.expanded) return;
  state._mtblAt = nowMs; state._mtblExp = state.expanded;
  const t = document.querySelector('#mtbl');
  const rows = Object.values(state.engines).map(({ meta = {} }) => {
    const l = meta.latest || latest();
    const open = state.expanded === meta.id;
    const detail = open ? `<tr><td colspan="10"><div class="mono small" style="line-height:1.8;color:var(--muted)">` +
      Object.entries(l).filter(([k]) => k !== 'savings').map(([k, v]) =>
        `${k} = <b style="color:var(--text)">${typeof v === 'number' ? fmt(v, 2) : v}</b>`).join(' · ') +
      ` · savings = <b style="color:var(--text)">${l.savings ? '$' + (l.savings.day || 0).toFixed(2) + '/day' : '–'}</b></div></td></tr>` : '';
    return `<tr data-model="${meta.id}" style="cursor:pointer"><td>${meta.model || meta.id}</td><td>${meta.kind || '–'}</td>
      <td>${fmt(l.running, 0)}</td><td>${fmt(l.waiting, 0)}</td>
      <td>${l.kv_pct != null ? l.kv_pct.toFixed(0) + '%' : '–'}</td>
      <td>${l.spec_accept_len != null ? l.spec_accept_len.toFixed(2) : '–'}</td>
      <td>${l.ttft_p50_ms != null ? Math.round(l.ttft_p50_ms) : '–'}</td>
      <td>${l.e2e_p50_ms != null ? (l.e2e_p50_ms / 1000).toFixed(2) : '–'}</td>
      <td>${(state.store.at(`${meta.id}.decode_tps_now`) || []).length}</td>
      <td>${meta.last_scrape ? new Date(meta.last_scrape * 1000).toTimeString().slice(0, 8) : '–'}</td></tr>` + detail;
  });
  t.innerHTML = `<tr><th>Model</th><th>Backend</th><th>Run</th><th>Wait</th><th>KV %</th>
    <th>Spec accept</th><th>TTFT p50</th><th>e2e p50 s</th><th>Samples</th><th>Seen</th></tr>` +
    (rows.join('') || '<tr><td colspan="10" class="muted">no engines</td></tr>');
  t.querySelectorAll('tr[data-model]').forEach(tr => tr.addEventListener('click', () => {
    state.expanded = state.expanded === tr.dataset.model ? null : tr.dataset.model;
    renderModelTable();
  }));
}

function renderFeed() {
  const f = document.querySelector('#req-feed');
  f.innerHTML = state.reqFeed.map(r => {
    const hh = new Date(r.t * 1000).toTimeString().slice(0, 8);
    return `<div class="row"><span class="t">${hh}</span><span>+${r.completed} completed</span>
      <span class="muted">running ${r.running ?? '–'} · waiting ${r.waiting ?? '–'}</span></div>`;
  }).join('');
}

// ---------------- snapshot + SSE ----------------

async function seed(reseedWindow = true) {
  const r = await fetch(`/api/snapshot?seconds=${state.windowSec + 30}`);
  const snap = await r.json();
  state.engines = {};
  for (const [id, e] of Object.entries(snap.engines || {})) {
    state.engines[id] = { meta: e, latest: e.latest || {} };
    for (const [k, rows] of Object.entries(e.series || {})) state.store.seed(`${id}.${k}`, rows);
    // e2e/queue p50 series aren't in snapshot series list — seed from ring rows missing: skip, SSE fills in
  }
  state.hw = snap.hw || {};
  for (const [k, rows] of Object.entries(snap.hw?.series || {})) state.store.seed(`hw.${k}`, rows);
  state.pricing = snap.pricing;
  if (reseedWindow) {
    buildCards();
    if (!state.charts.length) buildCharts();
    else state.charts.forEach(c => c._chart && !c._fixedWin && c._chart.setWindow(state.windowSec));
  } else {
    buildCards();
  }
}

const lastId = {};
async function connect() {
  const topics = ['hw', 'requests', 'events', ...Object.keys(state.engines).map(id => `engine:${id}`)].join(',');
  try {
    const res = await fetch(`/api/events?topics=${encodeURIComponent(topics)}`,
                            { headers: { Accept: 'text/event-stream' } });
    document.querySelector('#conn-dot').className = 'dot up';
    document.querySelector('#conn-txt').textContent = 'live 2 Hz';
    const reader = res.body.getReader(); const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        let ev = '', data = '', id = null;
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) ev = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trimStart();
          else if (line.startsWith('id:')) id = line.slice(3).trim();
        }
        if (!data) continue;
        if (id) lastId[ev] = id;
        let o; try { o = JSON.parse(data); } catch { continue; }
        if (ev === 'hw') {
          state.hw.latest = o;
          for (const k of ['cpu_pct','cpu_temp_c','gpu_util','gpu_temp_c','gpu_power_w','gpu_clock_mhz'])
            state.store.push(`hw.${k}`, o.t, o[k]);
        } else if (ev.startsWith('engine:')) {
          const id_ = ev.slice(7);
          const e = state.engines[id_]; if (!e) continue;
          e.latest = o; if (e.meta) e.meta.latest = o;
          for (const k of ['prefill_tps_now','decode_tps_now','prefill_tps_avg','decode_tps_avg',
                           'running','waiting','kv_pct','ttft_p50_ms','itl_p50_ms','queue_time_p50_ms',
                           'e2e_p50_ms','cache_hit_pct','spec_accept_pct','spec_accept_len',
                           'draft_tok_per_step','gen_peak','prompt_total','gen_total'])
            state.store.push(`${id_}.${k}`, o.t, o[k]);
        } else if (ev === 'requests') {
          state.reqFeed.unshift(o); if (state.reqFeed.length > 50) state.reqFeed.pop();
          renderFeed();
        }
      }
    }
  } catch { /* fall through */ }
  document.querySelector('#conn-dot').className = 'dot down';
  document.querySelector('#conn-txt').textContent = 'reconnecting…';
  setTimeout(connect, 2000);
}

// ---------------- token counters report (old LIMCARS parity) ----------------

const CNT_TABS = [['1h','1 hour'],['1d','1 day'],['7d','7 days'],['1mo','1 month'],['1y','1 year'],['all','All time']];
let cntLabel = 'all';
const fmtK = v => v == null ? '–' : v >= 1e6 ? (v/1e6).toFixed(2)+'M' : v >= 1e3 ? (v/1e3).toFixed(1)+'k' : String(Math.round(v));
const usd = v => v == null || !isFinite(v) ? '–' : '$' + (v < 1 ? v.toFixed(4) : v < 100 ? v.toFixed(2) : v.toLocaleString('en-US',{maximumFractionDigits:2}));

async function renderCounters() {
  const tabs = document.querySelector('#cnt-tabs');
  tabs.innerHTML = CNT_TABS.map(([l, t]) =>
    `<button class="pill tiny${l===cntLabel?' on':''}" data-l="${l}">${t}</button>`).join('');
  tabs.querySelectorAll('[data-l]').forEach(b => b.onclick = () => { cntLabel = b.dataset.l; renderCounters(); });
  const d = await (await fetch('/api/counters?window=' + cntLabel)).json();
  state._savSparks = d.sparks || state._savSparks || {};
  const tbl = document.querySelector('#cnt-tbl');
  if (d.empty) { tbl.innerHTML = '<tr><td class="muted">no counter data yet</td></tr>'; return; }
  const w = d.totals, a = d.all_time, p = d.price || {};
  const pf = v => v == null ? 'n/a' : '$' + (+v).toFixed(v < 1 ? 4 : 2).replace(/\.?0+$/,'') + '/M';
  const rows = [
    ['Prefill tokens (uncached)', fmtK(w.uncached) + ' tok'],
    ['Cache hit (prefill saved)', fmtK(w.cached) + ' tok' + (w.cache_hit != null ? ` · <b style="color:var(--teal)">${w.cache_hit}%</b> hit rate` : '')],
    ['Generation tokens', fmtK(w.gen) + ' tok'],
    ['Requests prefilled · avg uncached', fmtK(w.reqs) + (w.per_batch ? ` · ${fmtK(w.per_batch)} tok/req` : '')],
    ['Decode iterations / min speed', fmtK(w.iters) + ' / ' + (w.dec_min != null ? w.dec_min.toFixed(1) + ' tok/s' : '–')],
    ['Gen speed peak (batch)', w.dec_peak != null ? w.dec_peak.toFixed(1) + ' tok/s' : '–'],
    ['Spec accept (avg)', w.spec != null ? w.spec.toFixed(2) : '–'],
    ['All-time prefill / gen', fmtK(a.uncached) + ' / ' + fmtK(a.gen) + ' tok'],
  ];
  rows.push([`<span class="muted">API · cache write · ${fmtK(w.uncached)} tok @ ${pf(p.cache_write)}</span>`, usd(w.uncached/1e6*p.cache_write)]);
  rows.push([`<span class="muted">API · cache read · ${fmtK(w.cached)} tok @ ${pf(p.cache_read_hot)}</span>`, usd(w.cached/1e6*p.cache_read_hot)]);
  rows.push([`<span class="muted">API · output · ${fmtK(w.gen)} tok @ ${pf(p.output)}</span>`, usd(w.gen/1e6*p.output)]);
  rows.push([`<b>Money saved by local (hot cache)</b>`,
    `<b style="color:var(--teal)">${usd(w.cost_hot)}</b> <span class="muted">/ ${usd(a.cost_hot)} all-time</span>` +
    `<br><span class="muted">${usd(d.per_hour)}/hr · ${usd(d.per_day)}/day @ this rate</span>`]);
  rows.push([`<span class="muted">If cache reads used base rate (${pf(p.cache_read)})</span>`, usd(w.cost_base) + ` <span class="muted">/ ${usd(a.cost_base)} all</span>`]);
  rows.push([`<span class="muted">If context not cached at all (input ${pf(p.input)})</span>`, usd(w.cost_nocache) + ` <span class="muted">/ ${usd(a.cost_nocache)} all</span>`]);
  for (const [label, v] of (d.project || []))
    rows.push([`<span class="muted">Projected · ${label} @ this rate</span>`, usd(v)]);
  tbl.innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td class="mono">${v}</td></tr>`).join('');
  document.querySelector('#cnt-range').textContent = d.tracking_since
    ? `· tracking since ${new Date(d.tracking_since*1000).toLocaleDateString('en-GB',{day:'2-digit',month:'short'})} (366 days kept)` : '';
  document.querySelector('#cnt-note').textContent =
    `Counters computed live from the engine's /metrics counters, bucketed per minute (always-on, no browser needed). ` +
    `Prefill = uncached tokens processed; cache hit = tokens served from the KV/radix cache instead of recomputed. ` +
    `Money saved = what the same tokens would cost via a cloud API at ${p.name || 'pricing.json'} rates. ` +
    `$/hr rate derives from the most recent active hour.` +
    (d.extrapolated ? ` ⚠ Partial window: tracking started less than ${CNT_TABS.find(t=>t[0]===cntLabel)?.[1]} ago — showing real totals since tracking began.` : '');
}

// ---------------- Hermes traces (swaps the main pane; sidebar stays) ----------------

const tracesPane = document.querySelector('#traces-pane');
const dashPane = document.querySelector('#dash-pane');
const tracesFrame = document.querySelector('#traces-frame');
const btnTraces = document.querySelector('#btn-traces');
async function openTraces() {
  // password gate: forward the recipe password to the iframe so the vendored
  // UI can authenticate its transcript fetches (?pw= accepted server-side)
  if (!tracesFrame.src.includes('subagent-watch')) {
    let src = '/subagent-watch/';
    try {
      const st = await (await fetch('/api/auth/status')).json();
      if (st.enabled) {
        if (!state.pw) {
          const v = await pwDialog('🔒 Hermes Traces are password-protected',
                                   [{ label: 'Password' }]);
          const p = v && v[0];
          if (p) { state.pw = p; sessionStorage.setItem('lc-pw', p); }
        }
        if (state.pw) src += '?pw=' + encodeURIComponent(state.pw);
      }
    } catch (e) { /* gate check failed: open anyway, server decides */ }
    tracesFrame.src = src;
  }
  dashPane.style.display = 'none';
  tracesPane.style.display = 'block';
  btnTraces.textContent = '← Back to dashboard';
  btnTraces.classList.add('on');
}
function backToDash() {
  tracesPane.style.display = 'none';
  dashPane.style.display = '';
  btnTraces.textContent = '🐝 Hermes Traces';
  btnTraces.classList.remove('on');
}
btnTraces.addEventListener('click', () => {
  tracesPane.style.display === 'block' ? backToDash() : openTraces();
});
document.querySelector('#btn-traces-back').addEventListener('click', backToDash);
window.addEventListener('message', e => {
  if (e.data === 'sw-close' && tracesPane.style.display === 'block') backToDash();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && tracesPane.style.display === 'block') backToDash();
});

// ---------------- panel settings (server-side, shared — old dash_settings parity) ----------------

const SET_OPTS = {
  bubbles_main: 'Bubbles · main strip',
  bubbles_savings: 'Bubbles · cost-savings',
  charts_infer: 'Charts · infer (KV/PP/gen)',
  chart_per_client: 'Chart · per-client context',
  chart_per_model: 'Table · stats by model',
  requests_feed: 'Table · recent requests',
};
function applySettings(s) {
  sideSettings = s || {};
  const hide = (id, off, sel) => {
    const el = document.querySelector(id); if (!el) return;
    const tgt = sel ? el.closest(sel) : el; if (!tgt) return;
    tgt.style.display = off ? 'none' : '';
  };
  hide('#bub-main', sideSettings.bubbles_main === false, '.bub-group');
  hide('#bub-savings', sideSettings.bubbles_savings === false, '.bub-group');
  hide('#charts-grid', sideSettings.charts_infer === false);
  hide('#mtbl', sideSettings.chart_per_model === false, '.chart-card');
  hide('#cc-card', sideSettings.chart_per_client === false);
  hide('#req-feed', sideSettings.requests_feed === false, '.chart-card');
  const rsel = document.querySelector('#refresh-sel');
  if (rsel && document.activeElement !== rsel) rsel.value = String(sideSettings.refresh_s ?? 1);
  const box = document.querySelector('#side-settings');
  if (box && !box._built) {
    box._built = true;
    box.innerHTML = Object.entries(SET_OPTS).map(([k, l]) =>
      `<label class="side-ck"><input type="checkbox" data-set="${k}"> <span>${l}</span></label>`).join('');
    box.querySelectorAll('input[data-set]').forEach(inp => inp.addEventListener('change', () =>
      postSettings({ [inp.dataset.set]: inp.checked })));
  }
  if (box) box.querySelectorAll('input[data-set]').forEach(inp =>
    inp.checked = sideSettings[inp.dataset.set] !== false);
  renderScope();
}
async function postSettings(patch) {
  try {
    const r = await fetch('/api/settings', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
    const d = await r.json(); if (d.settings) applySettings(d.settings);
  } catch (e) { /* offline */ }
}
async function pollSettings() {
  try { applySettings(await (await fetch('/api/settings')).json()); } catch (e) {}
}
pollSettings(); setInterval(pollSettings, 20000);
document.querySelector('#refresh-sel').addEventListener('change', e =>
  postSettings({ refresh_s: parseFloat(e.target.value) }));
let sideSettings = {};
function renderScope() {
  const sc = document.querySelector('#side-scope');
  const cur = sideSettings.stats_scope === 'active' ? 'active' : 'all';
  sc.innerHTML = `<button class="pill tiny${cur === 'active' ? ' on' : ''}" data-scope="active">this engine</button>` +
    `<button class="pill tiny${cur === 'all' ? ' on' : ''}" data-scope="all">all engines</button>`;
  sc.querySelectorAll('[data-scope]').forEach(b => b.addEventListener('click', () =>
    postSettings({ stats_scope: b.dataset.scope })));
}

// ---------------- server config ----------------

async function renderInfo() {
  const d = await (await fetch('/api/engine-info')).json();
  const t = document.querySelector('#info-tbl');
  if (!d.healthy) { t.innerHTML = `<tr><td class="muted">${d.error || 'offline'}</td></tr>`; return; }
  const rows = [['Model', d.model], ['Version', d.version],
    ['Context window', d.ctx ? d.ctx.toLocaleString() + ' tok' : '–'],
    ['KV pool', d.pool_tokens ? d.pool_tokens.toLocaleString() + ' tok' + (d.num_gpu_blocks ? ` · ${d.num_gpu_blocks.toLocaleString()} blocks × ${d.block_size}` : '') : '–'],
    ['KV dtype', d.kv_dtype], ['Speculative', d.spec + ' · K=' + (d.draft_k ?? '–')],
    ['Accept rate', d.accept_rate != null ? d.accept_rate + '%' : '–'],
  ].map(([k, v]) => `<tr><td class="muted">${k}</td><td class="mono">${v ?? '–'}</td></tr>`).join('');
  const cfg = Object.entries(d.config || {}).map(([k, v]) =>
    `<tr><td class="muted">${escH(k)}</td><td class="mono">${escH(String(v))}</td></tr>`).join('');
  const launch = d.launch
    ? `<div class="side-lbl" style="margin-top:10px">launch recipe (process cmdline)</div>
       <pre class="mono small" style="white-space:pre-wrap;word-break:break-all;max-height:150px;overflow:auto;background:#0b1322;padding:8px;border-radius:6px">${escH(d.launch)}</pre>
       <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
         <button class="pill" id="rec-qs">＋ Add Quickstart</button>
         <button class="pill" id="rec-edit-cur">✎ Edit</button>
         <button class="pill" id="rec-new">＋ New</button>
       </div>`
    : '';
  t.innerHTML = rows
    + `<tr><td colspan="2" style="padding:6px 0 0"><div class="side-lbl">full engine config (vllm:cache_config_info)</div>
       <table class="data" style="width:100%">${cfg}</table>${launch}</td></tr>`;
  const saveRec = (name, cmd) => authFetch('/api/recipes', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, cmd }) }).then(r => r.json());
  const bqs = document.querySelector('#rec-qs');
  if (bqs) bqs.onclick = () => {
    if (!d.launch) return alert('no running launch recipe detected');
    recipeDialog({ title: '＋ Add Quickstart — current running recipe',
      name: '🚀 ' + (d.model || 'engine').split('/').pop(), cmd: d.launch, onSave: saveRec });
  };
  const bec = document.querySelector('#rec-edit-cur');
  if (bec) bec.onclick = () => {
    if (!d.launch) return alert('no running launch recipe detected');
    recipeDialog({ title: '✎ Edit current recipe → save to sidebar',
      name: '🔧 ' + (d.model || 'engine').split('/').pop(), cmd: d.launch, onSave: saveRec });
  };
  const bnw = document.querySelector('#rec-new');
  if (bnw) bnw.onclick = () => recipeDialog({ title: '＋ New recipe', onSave: saveRec });
}

// lifetime sparkline for the prefix-cache bubble: from engine start (sqlite,
// stride-downsampled), refreshed each minute
async function fetchCacheLife() {
  try {
    const e = state.engines[eng()] || {};
    const secs = e.proc_start ? Math.min(604800, Date.now() / 1000 - e.proc_start + 120) : 604800;
    const d = await (await fetch(`/api/history?engine=${encodeURIComponent(eng())}&seconds=${secs}`)).json();
    state._cacheHitLife = (d.rows || []).map(r => [r.t, r.cache_hit_pct]).filter(r => r[1] != null);
  } catch (err) { /* offline */ }
}

// boot extras
renderCounters(); setInterval(renderCounters, 30000);
renderInfo(); setInterval(renderInfo, 60000);
setInterval(renderSessionTokens, 15000);
setInterval(renderClientChart, 10000);
setInterval(fetchCacheLife, 60000);

document.querySelectorAll('[data-win]').forEach(btn => btn.addEventListener('click', async () => {
  state.windowSec = +btn.dataset.win;
  document.querySelectorAll('[data-win]').forEach(b => b.classList.toggle('on', b === btn));
  const label = { 180: '3 min', 900: '15 min', 3600: '1 h' }[state.windowSec];
  document.querySelector('#win-label').textContent = 'last ' + label;
  document.querySelectorAll('#charts-grid .w').forEach(w => w.textContent = label);
  await seed(false);
}));

document.querySelector('#btn-refresh').addEventListener('click', () => seed(false));

// pricing editor — manual override of the pricing.json resolution (null = clear)
document.querySelector('#btn-pricing').addEventListener('click', () => {
  const ed = document.querySelector('#pricing-edit');
  const p = state.pricing || {};
  document.querySelector('#pr-name').value = p.display_name || '';
  document.querySelector('#pr-w').value = p.cache_write ?? '';
  document.querySelector('#pr-r').value = p.cache_read ?? '';
  document.querySelector('#pr-out').value = p.output ?? '';
  ed.style.display = ed.style.display === 'none' ? 'block' : 'none';
});
document.querySelector('#pr-save').addEventListener('click', async () => {
  const out = parseFloat(document.querySelector('#pr-out').value);
  const w = parseFloat(document.querySelector('#pr-w').value);
  if (!out && !w) return alert('need at least output or write $/Mtok (leave blank to clear override)');
  const body = { pricing: document.querySelector('#pr-out').value === '' && document.querySelector('#pr-w').value === ''
    ? null
    : { display_name: document.querySelector('#pr-name').value || 'manual override',
        provider: 'manual override',
        cache_write: w || 0, cache_read: parseFloat(document.querySelector('#pr-r').value) || 0,
        output: out || 0 } };
  const r = await (await fetch('/api/config', { method: 'POST', body: JSON.stringify(body) })).json();
  if (r.ok) { state.pricing = r.config.pricing; await seed(false);
    document.querySelector('#pricing-edit').style.display = 'none'; }
  else alert('save failed: ' + (r.error || '?'));
});

document.querySelector('#btn-probe').addEventListener('click', async () => {
  const out = document.querySelector('#probe-out');
  out.innerHTML = '⚡ probing… <span class="muted">streaming a ~1.3k-token request</span>';
  try {
    const r = await (await fetch('/api/probe', { method: 'POST' })).json();
    out.innerHTML = r.ok
      ? `⚡ probe: <b>TTFT ${Math.round(r.ttft_ms)} ms</b> · PP <b>${r.pp_tps != null ? Math.round(r.pp_tps).toLocaleString('en-US') : '–'} tok/s</b> · TG <b>${r.tg_tps != null ? Math.round(r.tg_tps) : '–'} tok/s</b><br><span class="muted">prompt ${r.prompt_tokens} tok · completion ${r.completion_tokens} tok · ${new Date(r.at * 1000).toTimeString().slice(0, 8)}</span>`
      : `⚡ probe failed: ${r.error}`;
  } catch (e) { out.innerHTML = `⚡ probe error: ${e}`; }
});

// ---------------- boot ----------------

(async () => {
  await seed();
  fetchCacheLife();  // after seed so engine proc_start is known
  connect();
  (function loop() {
    refresh();
    setTimeout(loop, Math.max(0.5, sideSettings.refresh_s ?? 1) * 1000);
  })();
})();

/* ============ Recipe server (sidebar + dialogs) ============ */

const RECIPE_EMOJIS = ['🚀','🔥','⚡','🐢','🧪','🌊','🦥','🤖','🧠','💎','🎯','🛠️',
  '🏎️','🐎','🦄','🧊','🌶️','🥶','🐌','⭐','🔋','🧯','🪶','🏋️','🧵','📦','🧩',
  '🎛️','🐝','🦾','🧭','🛰️','🎚️','🧲'];

let _recOv = null;
function closeRecModal() { if (_recOv) { _recOv.remove(); _recOv = null; } }
function recOverlay(inner) {
  closeRecModal();
  const ov = document.createElement('div');
  ov.className = 'rec-overlay';
  ov.innerHTML = `<div class="rec-modal">${inner}</div>`;
  ov.addEventListener('mousedown', e => { if (e.target === ov) closeRecModal(); });
  document.body.appendChild(ov);
  _recOv = ov;
  return ov;
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeRecModal(); });

function recipeDialog({ title, name = '', cmd = '', onSave }) {
  const ov = recOverlay(`
    <div class="rec-h">${escH(title)}</div>
    <div class="side-lbl">name <span class="muted">— add an emoji anchor up front so it's easy to spot</span></div>
    <div style="display:flex;gap:6px;align-items:center">
      <input id="rec-name" class="inp" style="flex:1" placeholder="🚀 Flash Next 128k" value="${escH(name)}">
      <button class="pill" id="rec-emoji-btn" type="button" title="pick an emoji">😀</button>
    </div>
    <div id="rec-emoji" class="rec-emoji" style="display:none">
      ${RECIPE_EMOJIS.map(x => `<button type="button" class="rec-e">${x}</button>`).join('')}</div>
    <div class="side-lbl" style="margin-top:10px">launch command (recipe)</div>
    <textarea id="rec-cmd" class="inp mono" rows="9" spellcheck="false"
      placeholder="vllm serve … --port 8000">${escH(cmd)}</textarea>
    <div class="rec-msg muted small" id="rec-msg"></div>
    <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
      <button class="pill" id="rec-cancel">Cancel</button>
      <button class="btn" id="rec-save">💾 Save to sidebar</button>
    </div>`);
  const nameEl = ov.querySelector('#rec-name');
  ov.querySelector('#rec-emoji-btn').onclick = () => {
    const g = ov.querySelector('#rec-emoji');
    g.style.display = g.style.display === 'none' ? 'flex' : 'none';
  };
  ov.querySelectorAll('.rec-e').forEach(b => b.onclick = () => {
    // replace any existing leading emoji anchor, then prepend the picked one
    const bare = nameEl.value.replace(/^[^\w\u00C0-\uFFFF]*[\p{Extended_Pictographic}\p{Emoji_Presentation}\uFE0F\u200D]+/u, '').trimStart();
    nameEl.value = b.textContent + ' ' + bare;
    nameEl.focus();
  });
  ov.querySelector('#rec-cancel').onclick = closeRecModal;
  ov.querySelector('#rec-save').onclick = async () => {
    const msg = ov.querySelector('#rec-msg');
    msg.textContent = 'saving…';
    const res = await onSave(nameEl.value, ov.querySelector('#rec-cmd').value);
    if (!res || res.ok === false) { msg.textContent = '⚠ ' + ((res && res.error) || 'failed'); return; }
    closeRecModal();
    loadRecipes();
  };
  nameEl.focus();
}

async function loadRecipes() {
  const host = document.querySelector('#recipe-list');
  if (!host) return;
  let rs = [];
  try { rs = (await (await fetch('/api/recipes')).json()).recipes || []; }
  catch { host.innerHTML = '<span class="muted small">recipes unavailable</span>'; return; }
  if (!rs.length) {
    host.innerHTML = '<span class="muted small">none saved yet — add one from the buttons under the launch recipe in Server config</span>';
    return;
  }
  host.innerHTML = rs.map(r => `
    <div class="rec-row" data-id="${r.id}">
      <div class="rec-top"><span class="rec-dot${r.running ? ' on' : ''}"></span>
        <span class="rec-name">${escH(r.name)}</span></div>
      <div class="rec-sub muted small">${r.running
        ? `pid ${r.pid} · up ${new Date(r.started_at * 1000).toLocaleTimeString()}`
        : 'stopped'}</div>
      <div class="rec-btns">
        ${r.running
          ? '<button class="btn tiny rec-stop">■ Stop</button>'
          : '<button class="btn tiny rec-start">▶ Start</button>'}
      </div>
    </div>`).join('');
  host.querySelectorAll('.rec-start').forEach(b =>
    b.onclick = () => recAction(b.closest('.rec-row').dataset.id, 'start', b));
  host.querySelectorAll('.rec-stop').forEach(b =>
    b.onclick = () => recAction(b.closest('.rec-row').dataset.id, 'stop', b));
}

async function recAction(id, act, btn) {
  if (btn) { btn.disabled = true; btn.textContent = act === 'start' ? 'starting…' : 'stopping…'; }
  const res = await fetch(`/api/recipes/${id}/${act}`, { method: 'POST' })
    .then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  if (res.ok === false) {
    if (btn) { btn.disabled = false; btn.textContent = act === 'start' ? '▶ Start' : '■ Stop'; }
    alert('⚠ ' + (res.error || act + ' failed'));
  }
  loadRecipes();
}

document.querySelector('#btn-recipes-manage').onclick = async () => {
  let rs = [];
  try { rs = (await (await fetch('/api/recipes')).json()).recipes || []; } catch { /* ignore */ }
  const ov = recOverlay(`
    <div class="rec-h">⚙️ Manage recipes</div>
    <div id="rec-manage-list">${rs.length ? rs.map(r => `
      <div class="rec-mrow" data-id="${r.id}" data-name="${escH(r.name)}" data-cmd="${escH(r.cmd)}">
        <span class="rec-mname">${escH(r.name)}</span>
        <span class="muted small mono">${r.running ? '● running pid ' + r.pid : 'stopped'}</span>
        <span style="flex:1"></span>
        <button class="pill rec-medit">✎ Edit</button>
        <button class="pill rec-mdel">🗑 Delete</button>
      </div>`).join('') : '<span class="muted small">No recipes yet — create one with ＋ New.</span>'}</div>
    <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
      <button class="pill" id="rec-mp-new">＋ New recipe</button>
      <button class="btn" id="rec-mp-close">Close</button>
    </div>`);
  const post = (p, body) => authFetch(p, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}) }).then(r => r.json());
  ov.querySelector('#rec-mp-close').onclick = closeRecModal;
  ov.querySelector('#rec-mp-new').onclick = () =>
    recipeDialog({ title: '＋ New recipe', onSave: (n, c) => post('/api/recipes', { name: n, cmd: c }) });
  ov.querySelectorAll('.rec-medit').forEach(b => b.onclick = () => {
    const row = b.closest('.rec-mrow');
    recipeDialog({ title: '✎ Edit recipe', name: row.dataset.name, cmd: row.dataset.cmd,
      onSave: (n, c) => post(`/api/recipes/${row.dataset.id}/update`, { name: n, cmd: c }) });
  });
  ov.querySelectorAll('.rec-mdel').forEach(b => b.onclick = async () => {
    const row = b.closest('.rec-mrow');
    const res = await post(`/api/recipes/${row.dataset.id}/delete`);
    if (res.ok === false) { alert('⚠ ' + (res.error || 'delete failed')); return; }
    row.remove();
    loadRecipes();
  });
};

loadRecipes();
setInterval(loadRecipes, 5000);

/* ============ Live engines (detected on the box) ============ */

const ENG_ICONS = { 'vLLM': '🔴', 'SGLang': '🐍', 'llama.cpp': '🦙',
  'TandemLLM': '🔁', 'TGI': '🌀', 'GPU process': '🎮' };

async function loadEngines() {
  const host = document.querySelector('#engine-list');
  if (!host) return;
  let es = [];
  try { es = (await (await fetch('/api/engines-live')).json()).engines || []; }
  catch { host.innerHTML = '<span class="muted small">scan unavailable</span>'; return; }
  if (!es.length) { host.innerHTML = '<span class="muted small">no engine / >1 GB GPU process found</span>'; return; }
  host.innerHTML = es.map(e => {
    const svc = Object.entries(e.serving_ports || {});
    const ports = (e.ports || []).length ? e.ports.map(p => ':' + p).join(' ') : 'no port';
    const gpu = e.gpu_mib != null ? (e.gpu_mib >= 1024
      ? (e.gpu_mib / 1024).toFixed(1) + ' GB GPU' : e.gpu_mib + ' MB GPU') : 'no GPU';
    const m = e.model ? e.model.split('/').pop() : '';
    const who = e.orphan ? 'port only · root/container'
      : 'pid ' + e.pid + ((e.pids_extra || []).length ? ` +${e.pids_extra.length} proc` : '');
    return `<div class="rec-row eng-row" data-pid="${e.pid ?? ''}">
      <div class="rec-top"><span class="rec-dot${svc.length ? ' on' : ''}"></span>
        <span class="rec-name">${ENG_ICONS[e.type] || '⚙️'} ${escH(e.type)}${m ? ' · ' + escH(m) : ''}</span></div>
      <div class="rec-sub muted small">${ports} · ${gpu} · ${who}${svc.length ? ' · serving ' + svc.map(([p]) => ':' + p).join(' ') : ''}</div>
      <div class="rec-btns"><button class="btn tiny eng-stop"${e.orphan ? ' disabled title="run outside dashboard reach — stop it where it was started"' : ''}>■ Stop</button></div>
    </div>`;
  }).join('');
  host.querySelectorAll('.eng-stop').forEach(b => b.onclick = async () => {
    const row = b.closest('.eng-row');
    const pid = row.dataset.pid;
    const label = row.querySelector('.rec-name').textContent;
    if (!confirm(`Stop ${label} (pid ${pid})?\nThis SIGTERMs → SIGKILLs its whole process group.`)) return;
    b.disabled = true; b.textContent = 'stopping…';
    const res = await fetch(`/api/engines-live/${pid}/stop`, { method: 'POST' })
      .then(r => r.json()).catch(err => ({ ok: false, error: String(err) }));
    if (res.ok === false) { b.disabled = false; b.textContent = '■ Stop'; alert('⚠ ' + (res.error || 'stop failed')); }
    loadEngines();
  });
}
loadEngines();
setInterval(loadEngines, 7000);

// ---------- recipe password gate ----------
// One shared password for recipe add/edit/delete only — Start/Stop and the
// rest of the dash stay open for convenience. Server-side escalating lockout
// (3 wrong -> 60 s, next 3 -> 5 min, -> 30 min, -> 2 h). Localhost exempt.
// Password kept in sessionStorage (per browser tab session).
state.pw = sessionStorage.getItem('lc-pw') || '';

// Masked password dialog (native prompt() shows the text — bad for shoulder
// surfing). Resolves to array of field values, or null on cancel/backdrop.
function pwDialog(title, fields) {
  return new Promise(resolve => {
    const ov = document.createElement('div');
    ov.className = 'rec-overlay';
    ov.innerHTML = `<div class="rec-modal"><div class="rec-title">${title}</div>`
      + fields.map((f, i) =>
          `<div class="rec-row" style="margin:8px 0"><label style="display:block;margin-bottom:4px">${f.label}</label>`
          + `<input type="password" id="pwd-${i}" autocomplete="new-password" autocapitalize="none" `
          + `style="width:100%;font-size:14px" placeholder="••••••••"></div>`).join('')
      + `<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">`
      + `<label style="margin-right:auto;align-self:center;font-size:12px"><input type="checkbox" id="pwd-show"> show</label>`
      + `<button class="pill" id="pwd-cancel">Cancel</button><button class="btn" id="pwd-ok">OK</button></div></div>`;
    document.body.appendChild(ov);
    const inputs = [...ov.querySelectorAll('input[type="password"], input[type="text"]')];
    const done = v => { ov.remove(); resolve(v); };
    ov.querySelector('#pwd-show').onchange = e =>
      inputs.forEach(i => { i.type = e.target.checked ? 'text' : 'password'; });
    ov.querySelector('#pwd-ok').onclick = () => done(inputs.map(i => i.value));
    ov.querySelector('#pwd-cancel').onclick = () => done(null);
    ov.addEventListener('keydown', e => {
      if (e.key === 'Enter') done(inputs.map(i => i.value));
      if (e.key === 'Escape') done(null);
    });
    ov.addEventListener('click', e => { if (e.target === ov) done(null); });
    inputs[0]?.focus();
  });
}

async function authFetch(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {});
  if (state.pw) opts.headers['X-Limcars-Pw'] = state.pw;
  let r = await fetch(url, opts);
  if (r.status === 401) {
    const v = await pwDialog('🔒 Editing recipes is password-protected',
                             [{ label: 'Password' }]);
    const p = v && v[0];
    if (p) {
      state.pw = p; sessionStorage.setItem('lc-pw', p);
      opts.headers['X-Limcars-Pw'] = p;
      r = await fetch(url, opts);
    }
  }
  if (r.status === 423) {
    let s = 60; try { s = Math.ceil((await r.clone().json()).retry_after || 60); } catch (e) {}
    alert(`⏳ Too many wrong passwords — locked for ~${s} s. Try again after that.`);
  }
  return r;
}

async function renderAuth() {
  const el = document.querySelector('#auth-status');
  const btn = document.querySelector('#btn-auth');
  if (!el || !btn) return;
  try {
    const d = await (await fetch('/api/auth/status')).json();
    if (!d.enabled) {
      el.innerHTML = '<span style="color:#e8a33d">⚠ no password — anyone on the LAN can add/edit recipes</span>';
      btn.textContent = 'Set password';
    } else {
      el.innerHTML = `<span style="color:#5dd97c">✓ password protection on</span>`
        + (d.locked_for_you > 0 ? ` · ⏳ locked ${Math.ceil(d.locked_for_you)} s` : '');
      btn.textContent = 'Change password';
    }
  } catch (e) { el.textContent = 'auth status unavailable'; }
}
renderAuth();

document.querySelector('#btn-auth').addEventListener('click', async () => {
  const d = await (await fetch('/api/auth/status')).json();
  const v = await pwDialog(d.enabled ? 'Change recipe password' : 'Set recipe password',
    d.enabled ? [{ label: 'Current password' }, { label: 'New password (empty = remove protection)' },
                 { label: 'Repeat new password' }]
              : [{ label: 'New password — LAN needs it to add/edit recipes (this box and Start/Stop stay free)' },
                 { label: 'Repeat new password' }]);
  if (!v) return;
  const cur = d.enabled ? v[0] : null;
  let nw = (d.enabled ? v[1] : v[0]) || '';
  if (!nw.trim()) nw = '';
  const rep = d.enabled ? v[2] : v[1];
  if (nw && rep !== nw) return alert('Passwords do not match.');
  const r = await fetch('/api/auth/set', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current: cur, new: nw }) }).then(r => r.json())
    .catch(e => ({ ok: false, error: String(e) }));
  if (r.ok) { state.pw = nw || ''; sessionStorage.setItem('lc-pw', state.pw); renderAuth(); }
  else alert(r.error === 'locked' ? `⏳ Locked — retry in ${Math.ceil(r.retry_after || 60)} s`
        : 'Failed: ' + (r.error || '?'));
});

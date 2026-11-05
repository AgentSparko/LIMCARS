// LIMCARS-NEXT Classic — full rebuild of the old hub/dash.html canvas dash
// (the one Streamlit labelled "🎛️ Modern") on the new 2 Hz backend.
// Same reading order: sidebar system tiles + side charts, hero cards,
// latency/live/spec/savings bubble rows, six-chart grid, model table.

import { SeriesStore, LineChart, spark, fmt, COLORS, seriesSample, fillIdleZeros, rateZerosToGaps, RATE_FIELDS, HOLD_LAST_NONZERO } from './charts.js?v=20261104h';

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
    { label: 'Gen speed', series: 'decode_tps_now', tip: 'Generated tokens per second right now, plus the average over the last 5 min of decoding (idle time left out). Dims while the engine is not decoding. Turns red with \'possible loop\' when every speculative draft has been accepted for 30 s straight: the signature of a model stuck repeating itself.', color: () => COLORS.green,
      dimWhen: 'decode_active',
      sparkFmt: v => v.toFixed(1) + ' tok/s',
      alertWhen: 'loop_suspect',
      main: l => tps(l.decode_tps_now),
      sub: l => l.loop_suspect
        ? `⚠ possible loop · every draft accepted for ${Math.round(l.spec_full_accept_s)} s`
        : `tok/s now · avg ${tps(l.decode_tps_avg)}` },
    { label: 'Prefill speed', series: 'prefill_tps_avg', tip: 'Uncached prompt tokens per second of actual prefill time. Cache hits are excluded and attribution is capped, so prompt tokens charged at completion never read as a fake prefill burst.', color: () => COLORS.teal, dimZero: true,
      dimWhen: 'prefill_active',
      sparkFmt: v => v.toFixed(1) + ' tok/s',
      main: l => tps(l.prefill_tps_avg), sub: l => `uncached tok / prefill-sec · burst ${tps(l.prefill_tps_now)}` },
    { label: 'KV cache used', hot: true, tip: "Share of the engine's KV token pool in use. Idle vLLM reports 0 while prefix-cached blocks stay resident, so the last non-zero reading is held. For models listed in kv_peak_models (DeepSeek V4.1, whose reading jumps for ~1 s every 128 tokens) it is the highest reading of the last few seconds, and says so.", series: 'kv_pct_shown', color: () => COLORS.yellow, holdZero: true,
      sparkFmt: v => v.toFixed(1) + ' %',
      main: l => pct(l.kv_pct_shown ?? l.kv_pct),
      sub: l => l.kv_pct_peak_s
        ? `highest of last ${Math.round(l.kv_pct_peak_s)} s · now ${pct(l.kv_pct)} · ${fmtK(l.kv_pool_tokens ?? 0)} tok pool`
        : l.kv_pool_tokens
        ? `${fmtK(l.kv_used_tokens ?? 0)} / ${fmtK(l.kv_pool_tokens)} tok used · ${fmtK(l.kv_free_tokens ?? 0)} free`
        : '% of token pool' },
    { label: 'Running reqs', series: 'running', tip: "Requests being decoded now; 'waiting' are queued for a slot.", color: () => COLORS.green,
      sparkFmt: v => String(Math.round(v)),
      main: l => num(l.running), sub: l => `${num(l.waiting)} waiting` },
    { label: 'TTFT', series: 'ttft_p50_ms', tip: 'Time to first token over the last hour. A p50 is shown only with n ≥ 20 requests: engine histogram buckets are coarse, so below that the true mean (sum ÷ count) is shown. n is always displayed.', color: () => COLORS.violet, hist: true,
      sparkFmt: v => Math.round(v) + ' ms',
      main: l => histMs(l, 'ttft'), sub: l => 'time to first token · ' + histBasis(l, 'ttft') },
    { label: 'e2e latency', series: 'e2e_p50_ms', tip: 'Request in → last token out, queue included. Same rule as TTFT: p50 only at n ≥ 20, otherwise the true mean, with n shown.', color: () => COLORS.violet, hist: true,
      sparkFmt: v => (v / 1000).toFixed(2) + ' s',
      main: l => sec(e2eHourNum(l) != null ? e2eHourNum(l) : histNum(l, 'e2e')),
      sub: l => `${e2eHourBasis(l)} · now ${sec(histNum(l, 'e2e'))} · lifetime ${sec(l.e2e_life_avg_ms)}` },
    { label: 'Spec acceptance', series: 'spec_accept_pct', seriesAlt: 'spec_step_accept_pct', tip: 'Share of speculative draft tokens the target model accepted, mean accepted length per step and draft tokens proposed per step.', color: () => COLORS.violet,
      sparkFmt: v => v.toFixed(1) + ' %',
      main: l => pct(l.spec_accept_pct ?? l.spec_step_accept_pct),
      sub: l => l.spec_accept_pct == null && l.spec_step_accept_pct != null
        ? 'of verify steps accepted ≥1 draft · engine reports steps, not tokens'
        : `accept len ${l.spec_accept_len != null ? l.spec_accept_len.toFixed(2) : '–'} tok · `
              + `${l.draft_tok_per_step != null ? l.draft_tok_per_step.toFixed(1) : '–'} draft tok/step · DFlash` },
    { label: 'Prefix cache hit', series: 'cache_hit_pct', tip: 'Share of prompt tokens served from the prefix cache — median over the last hour. Cached tokens are a subset of prompt tokens, never added on top.', life: true, color: () => COLORS.teal,
      sparkFmt: v => v.toFixed(1) + ' %',
      main: l => pct(l.cache_hit_med_1h != null ? l.cache_hit_med_1h : l.cache_hit_pct),
      sub: l => `median · last hour · now ${pct(l.cache_hit_pct)} · lifetime ${pct(l.cache_hit_life_pct)}` },
    { label: 'Gen speed peak', series: 'gen_peak', tip: 'Highest decode speed seen in the last hour.', color: () => COLORS.cobalt,
      sparkFmt: v => v.toFixed(1) + ' tok/s',
      main: l => tps(l.gen_peak), sub: () => 'tok/s · last hour' },
  ],
  'bub-savings': [
    { label: 'Projected save · day', hot: true, series: 'savings_day', savings: 'day', tip: 'What the same tokens would cost at the cloud API price shown above — a projection at this rate from the last active hour of real minute buckets. Cached tokens are priced at the cache-read rate; estimated (synthetic) minutes never feed a $ figure.', color: () => COLORS.green,
      sparkFmt: v => '$' + v.toFixed(2) },
    { label: 'Projected save · month', hot: true, series: 'savings_month', savings: 'month', tip: 'What the same tokens would cost at the cloud API price shown above — a projection at this rate from the last active hour of real minute buckets. Cached tokens are priced at the cache-read rate; estimated (synthetic) minutes never feed a $ figure.', color: () => COLORS.green,
      sparkFmt: v => '$' + v.toFixed(2) },
    { label: 'Projected save · year', hot: true, series: 'savings_year', savings: 'year', tip: 'What the same tokens would cost at the cloud API price shown above — a projection at this rate from the last active hour of real minute buckets. Cached tokens are priced at the cache-read rate; estimated (synthetic) minutes never feed a $ figure.', color: () => COLORS.green,
      sparkFmt: v => '$' + v.toFixed(0) },
  ],
};

const mbs = v => v >= 100 ? v.toFixed(0) : v.toFixed(1);   // keeps the SSD duo on one line
const SIDE_TILES = [
  ['CPU', h => [num(h.cpu_pct, 0) + '%']],
  ['GPU util', h => [num(h.gpu_util, 0) + '%']],
  ['CPU temp', h => [h.cpu_temp_c != null ? h.cpu_temp_c.toFixed(0) + ' °C' : '–']],
  ['GPU temp', h => [h.gpu_temp_c != null ? h.gpu_temp_c.toFixed(0) + ' °C' : '–']],
  ['GPU power', h => [h.gpu_power_w != null ? h.gpu_power_w.toFixed(1) + ' W' : '–']],
  ['GPU clock', h => [h.gpu_clock_mhz != null ? num(h.gpu_clock_mhz) + ' MHz' : '–']],
  [h => h.vram_total_gb ? 'System RAM' : 'Unified RAM',
   h => [h.mem_total_gb != null ? (h.mem_total_gb - h.mem_used_gb).toFixed(1) + ' GB free' : '–']],
  ['SSD free', h => [h.disk_total_gb != null ? (h.disk_total_gb - h.disk_used_gb).toFixed(0) + ' GB free' : '–']],
  // discrete GPUs only (hidden on unified-memory boxes like the DGX Spark)
  ['VRAM', h => [h.vram_total_gb ? (h.vram_total_gb - h.vram_used_gb).toFixed(1) + ' GB free'
                   + ` of ${Math.round(h.vram_total_gb)}` : '–'], { wide: true, vram: true }],
  // duo/wide: down and up shown side by side in a full-row tile (user ask:
  // not stacked — one after another, double width)
  ['Net traffic', h => h.net_r_mbs != null
      ? [`↓ ${h.net_r_mbs.toFixed(2)} MB/s`, `↑ ${(h.net_w_mbs ?? 0).toFixed(2)} MB/s`]
      : ['–', '–'],
    { duo: true, wide: true }],
  ['SSD I/O', h => h.ssd_r_mbs != null
      ? [`R ${mbs(h.ssd_r_mbs)} MB/s`, `W ${mbs(h.ssd_w_mbs ?? 0)} MB/s`]
      : ['–', '–'],
    { duo: true, wide: true, id: 'st-ssd-io' }],
];

const _fin = v => v != null && Number.isFinite(v);
const pct = v => _fin(v) ? v.toFixed(1) + '%' : '–';
const ms = v => _fin(v) ? Math.round(v) + ' ms' : '–';
const sec = v => _fin(v) ? (v / 1000).toFixed(2) + ' s' : '–';
const tps = v => _fin(v) ? Math.round(v).toLocaleString('en-US') + ' tok/s' : '–';
// Histogram bubbles: p50 only from a population; with few samples the coarse
// engine buckets produce round-edge fiction, so show the true window mean.
// histNum is the numeric form so a bubble can render in its own unit (ms or s)
// while every latency bubble shares ONE n-gate and one basis rule.
const histNum = (l, k) => {
  // Latency bubbles read the LAST HOUR, not the 30 s window: a 30 s window
  // holds two or three requests in normal use, so its percentile — or its
  // mean — belongs to whoever queued longest. TTFT read 28.5 s there while the
  // engine's own hour median was 5 s. Falls back to the window only when the
  // hour has no samples at all.
  const nh = l[k + '_n_1h'] || 0;
  if (nh) return nh >= 20 ? l[k + '_p50_1h_ms']
         : (l[k + '_avg_1h_ms'] != null ? l[k + '_avg_1h_ms'] : l[k + '_p50_1h_ms']);
  const n = l[k + '_n'] || 0;
  return n >= 20 ? l[k + '_p50_ms']
                 : (l[k + '_avg_ms'] != null ? l[k + '_avg_ms'] : l[k + '_p50_ms']);
};
const histMs = (l, k) => ms(histNum(l, k));
const histBasis = (l, k) => {
  const nh = l[k + '_n_1h'] || 0;
  if (nh) return nh >= 20 ? `p50 · last hour · n=${nh}`
       : `mean · last hour · n=${nh} (too few reqs for p50)`;
  const n = l[k + '_n'] || 0;
  return n >= 20 ? `p50 · 30 s window · n=${n}`
       : n ? `true mean · 30 s · n=${n} (too few reqs for p50)`
       : 'idle · no requests yet';
};
// e2e kept its own names for the sub-line wording; both now share the one
// hour-based rule above so no bubble can drift onto a different basis.
const e2eHourNum = l => histNum(l, 'e2e');
const e2eHourBasis = l => histBasis(l, 'e2e');

// ---------------- build ----------------

// selected engine (header switcher; ?engine= > cookie > first configured)
const eng = () => {
  const ids = Object.keys(state.engines);
  const up = id => { const e = state.engines[id]; return !!(e && (e.alive || (e.meta && e.meta.alive))); };
  const sel = state.sel && state.engines[state.sel] ? state.sel : null;
  // A remembered selection that went dead must not hide a live engine —
  // after an engine swap the stored pref pointed at the old (now dead) one
  // and the whole page read "no stats". An EXPLICIT click on a dead engine
  // is honoured: the user wants its last state, not a silent redirect.
  if (sel && (up(sel) || state.selExplicit)) return sel;
  return ids.find(up) || sel || ids[0];
};
const latest = () => (state.engines[eng()] || {}).latest || {};
// token counters for a model this engine served earlier (?model=; '-' = minutes stored
// before stats were split per model); unset = the model it serves now
state.statModel = {};
const statQ = () => state.statModel[eng()] ? '&model=' + encodeURIComponent(state.statModel[eng()]) : '';
// sidebar hardware source: this box, or a cluster node picked in the sidebar
const hwNode = () => (state.nodeSel && state.nodes && state.nodes[state.nodeSel]) || null;
const hwPrefix = () => hwNode() ? `hw@${state.nodeSel}` : 'hw';
const hwSrc = () => hwNode() || state.hw || {};
const hwLatest = () => hwSrc().latest || {};

const cardRefs = [];

function buildCards() {
  cardRefs.length = 0;  // audit: rebuilds without this leaked detached refs
                        // and the 1 Hz refresh loop grew unbounded on outages
  for (const [id, defs] of Object.entries(BUBS)) {
    const host = document.querySelector('#' + id);
    host.innerHTML = '';
    for (const d of defs) {
      const el = document.createElement('div');
      el.className = 'bubble' + (d.hot ? ' hot' : '');
      el.innerHTML = `<div class="label">${d.label}</div>
        <div class="value"><span class="v">–</span></div><div class="sub"></div>
        ${d.series ? '<svg class="spark"></svg>' : ''}`;
      if (d.tip) el.title = d.tip;
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
  document.querySelector('#side-tiles').innerHTML = SIDE_TILES.map(([l, , opt]) =>
    `<div class="st${opt?.wide ? ' wide' : ''}${opt?.duo ? ' duo' : ''}"${opt?.id ? ` id="${opt.id}"` : ''}${opt?.vram && !hwLatest().vram_total_gb ? ' hidden' : ''}>` +
    `<div class="l">${typeof l === 'function' ? l(hwLatest()) : l}</div><div class="v">–</div></div>`).join('');
  const p = state.pricing;
  document.querySelector('#pricing-note').textContent = p
    ? `· vs ${p.display_name || p.slug || 'API'} @ $${p.cache_write} w / $${p.cache_read} read / $${p.output} out per Mtok` +
      (p.provider ? ` (${p.provider})` : '')
    : '· no pricing entry for this model';
}

function sideChart(host, series, fmtFn, chartOpts = {}) {
  // user rule: hw side charts always show last 3 min — more relevant than
  // following the global window button
  const c = new LineChart(host, { store: state.store, window: 180, h: 110, series, ...chartOpts });
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
  return c;
}

// Discrete-GPU boxes: RAM and VRAM get separate charts, and 2+ cards get one
// chart each. Unified-memory boxes (DGX Spark) keep the original sidebar.
let gpuSig = null;
function ensureGpuCards() {
  const h = hwLatest();
  const n = h.gpu_count || 0, vram = !!h.vram_total_gb;
  const names = (hwSrc().gpus || []).map(g => g.name).join('|');
  const hp = hwPrefix(), sig = `${hp}:${n}:${vram}:${names}`;   // names: titles fill in once known
  const ramCard = document.querySelector('#sch-ram')?.closest('.side-chart');
  if (sig === gpuSig || !ramCard) return;
  gpuSig = sig;
  document.querySelectorAll('.gpu-dyn').forEach(c => {
    c._chart && c._chart.destroy();
    state.charts = state.charts.filter(x => x !== c); c.remove(); });
  let after = ramCard;
  const add = (id, title, series, fmtFn, opts) => {
    const card = document.createElement('div');
    card.className = 'chart-card side-chart gpu-dyn';
    card.innerHTML = `<div class="head"><span class="title"></span><span class="cur"></span>
      <button class="pill tiny autoy on">AUTO Y</button></div><div id="${id}"></div>`;
    card.querySelector('.title').textContent = title;
    after.after(card); after = card;
    sideChart(card.querySelector('#' + id), series, fmtFn, opts);
  };
  if (vram) add('sch-vram', 'VRAM (GB) · last 3 min',
    [{ key: `${hp}.vram_used_gb`, label: 'GB used', color: () => COLORS.violet, fill: true }],
    v => v != null ? v.toFixed(1) + ' GB' : '', { yCeil: h.vram_total_gb, autoY: false });
  if (n > 1) {
    const gpus = hwSrc().gpus || [];
    for (let i = 0; i < n; i++) {
      const series = [{ key: `${hp}.gpu${i}_util`, label: 'util %', color: () => COLORS.cobalt, fill: true }];
      if (vram) series.push({ key: `${hp}.gpu${i}_mem_pct`, label: 'VRAM %', color: () => COLORS.violet });
      add(`sch-gpu${i}`, `GPU ${i}${gpus[i] ? ' · ' + gpus[i].name : ''} · util${vram ? ' + VRAM' : ''} % · last 3 min`,
          series, v => v != null ? Math.round(v) + '%' : '', { yCeil: 100 });
    }
  }
}
// Titles and physical ceilings follow the detected hardware; on a Spark
// (no VRAM, no power limit reported) they stay exactly as before.
function adaptHwCharts(h) {
  const hp = hwPrefix(), from = state._hwPrefix || 'hw';
  if (hp !== from) {                 // node picker changed: retarget the fixed side charts
    for (const card of state.charts) {
      if (!card._fixedWin || !card._series) continue;
      for (const s of card._series) if (s.key.startsWith(from + '.')) s.key = hp + s.key.slice(from.length);
      card._lastCur = null;          // never show the previous source's held reading
      card._chart && card._chart.mark();
    }
    state._hwPrefix = hp;
  }
  ensureGpuCards();
  const discrete = !!h.vram_total_gb, many = (h.gpu_count || 0) > 1;
  const set = (sel, title, ceil) => {
    const card = document.querySelector(sel)?.closest('.side-chart');
    if (!card) return;
    const t = card.querySelector('.title');
    if (t.textContent !== title) t.textContent = title;
    if (card._chart && ceil && card._chart.yCeil !== ceil) { card._chart.yCeil = ceil; card._chart.mark(); }
  };
  set('#sch-ram', `${discrete ? 'System' : 'Unified'} RAM (GB) · last 3 min`,
      discrete && h.mem_total_gb ? Math.ceil(h.mem_total_gb) : 128);
  set('#sch-power', `GPU power${many ? ' · all cards' : ''} · last 3 min`,
      h.gpu_power_limit_w ? Math.ceil(h.gpu_power_limit_w) : 110);
  set('#sch-temp', `Temperature (CPU + GPU${many ? ' max' : ''}) · last 3 min`, 100);
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
    leg.innerHTML = series.map(s => `<span><i style="background:${typeof s.color==='function'?s.color():s.color}"></i>${s.label}</span>`).join('');
    card._chart = c; card._cur = card.querySelector('.cur'); card._series = series;
    state.charts.push(card);
    return c;
  };
  mk(0, `Decode speed · last <span class="w">15 min</span>`, [
    { key: `${e}.decode_tps_now`, label: 'tok/s', color: () => COLORS.cobalt, fill: true }], { curFmt: v => v != null ? Math.round(v) + ' tok/s' : '' });
  mk(0, 'Prefill speed · last <span class="w">15 min</span>', [
    { key: `${e}.prefill_tps_avg`, label: 'Δtok/Δprefill-sec', color: () => COLORS.teal, fill: true }]);
  mk(0, 'KV pool (0–100%) · last <span class="w">15 min</span>', [
    { key: `${e}.kv_pct`, label: '%', color: () => COLORS.yellow, fill: true }], { yCeil: 100, yFmt: v => v.toFixed(0) });
  mk(0, 'Speculative accept length · last <span class="w">15 min</span>', [
    { key: `${e}.spec_accept_len`, label: 'tok accepted/step', color: () => COLORS.violet, fill: true }]);

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

  const side = (sel, ...args) => sideChart(document.querySelector(sel), ...args);
  // yCeil = physical maximum: with AUTO Y OFF the view is 0..ceiling (real
  // amplitude vs the hardware range); ON zooms to the data extremes.
  side('#sch-power', [{ key: 'hw.gpu_power_w', label: 'W', color: () => COLORS.cobalt, fill: true }], v => v != null ? v.toFixed(1) + ' W' : '', { yCeil: 110, autoY: false });
  side('#sch-temp', [{ key: 'hw.cpu_temp_c', label: 'CPU °C', color: () => COLORS.rose },
                     { key: 'hw.gpu_temp_c', label: 'GPU °C', color: () => COLORS.cobalt }], v => v != null ? v.toFixed(1) + '°' : '', { yCeil: 100 });
  side('#sch-clock', [{ key: 'hw.gpu_clock_mhz', label: 'MHz', color: () => COLORS.teal, fill: true }], v => v != null ? Math.round(v) + ' MHz' : '', { yCeil: 3003 });
  side('#sch-ram', [{ key: 'hw.mem_used_gb', label: 'GB used', color: () => COLORS.rose, fill: true }],
    v => v != null ? v.toFixed(1) + ' GB' : '', { yCeil: 128, autoY: false });
  // SSD: an hour, not 3 min — expert offload and downloads come in long runs
  side('#sch-ssd', [{ key: 'hw.ssd_r_mbs', label: 'read', color: () => COLORS.teal, fill: true },
                    { key: 'hw.ssd_w_mbs', label: 'write', color: () => COLORS.rose }],
    v => v != null ? v.toFixed(0) + ' MB/s' : '', { window: 3600 });
  gpuSig = null;
  ensureGpuCards();
}

// ---------------- toasts + in-page confirm (no blocking alert/confirm) ----------------

function toast(msg, kind = 'info') {
  let host = document.getElementById('toasts');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toasts'; host.className = 'toasts';
    host.setAttribute('role', 'status'); host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.textContent = msg;
  t.title = 'click to dismiss';
  t.addEventListener('click', () => t.remove());
  host.appendChild(t);
  setTimeout(() => t.remove(), kind === 'err' ? 8000 : 4500);
}

function confirmDialog(msg, okLabel = 'OK') {
  return new Promise(resolve => {
    const prev = document.activeElement;
    const ov = document.createElement('div');
    ov.className = 'rec-overlay';
    ov.innerHTML = `<div class="rec-modal confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="cf-msg">
      <div class="confirm-msg" id="cf-msg"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        <button class="pill" data-a="0">Cancel</button><button class="btn confirm-ok" data-a="1"></button></div></div>`;
    ov.querySelector('.confirm-msg').textContent = msg;
    ov.querySelector('.confirm-ok').textContent = okLabel;
    const done = v => { ov.remove(); prev && prev.focus && prev.focus(); resolve(v); };
    ov.addEventListener('click', e => {
      if (e.target === ov) return done(false);
      const b = e.target.closest('button[data-a]');
      if (b) done(b.dataset.a === '1');
    });
    ov.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); done(false); } });
    document.body.appendChild(ov);
    ov.querySelector('button[data-a="0"]').focus();   // destructive actions: safe default
  });
}

// Single pass over all six HTML specials; the ampersand must be in the class or
// its map entry is dead code. Pinned in tests/test_web_escapes.py.
const escH = s => String(s || '').replace(/[&<>"'\\]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '\\': '&#92;' }[c]));

// Client-context category colors are THEME-REACTIVE: stored as base hues,
// rotated with the accent palette (applyTheme) so NO chart is left behind;
// category separation (relative hues) is preserved.
const CC_BASE = ['#94d9a8', '#e8c18d', '#4a7fa8', '#d05c4a', '#a78bfa',
                 '#5ebeba', '#d98cb4', '#b4c85a', '#e6a06e', '#8296dc',
                 '#78d28c', '#c8aa78'];
const CC_COLORS = CC_BASE.slice();
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
  try { d = await (await fetch('/api/session-tokens?engine=' + encodeURIComponent(eng()))).json(); }
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
      <span class="st-bar"><i style="width:${wi}%;background:${COLORS.cobalt}"></i><i style="width:${wo}%;background:${COLORS.now}"></i></span>
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
        ? `${sv.basis} · ${fmtK(sv.tokens.prompt)} in (${fmtK(sv.tokens.cached)} from cache) + ${fmtK(sv.tokens.gen)} gen`
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
      else if (raw === 0) {
        if (state._heldNz[hk]) {
          ref.v.textContent = state._heldNz[hk].v;
          ref.sub.textContent = state._heldNz[hk].s + ' · held, cache resident';
        } else {
          // Nothing non-zero seen since the reload, but the seeded series carries the
          // last real reading. Use it, so the number and the sparkline hold the same
          // value — one layer holding while the other shows the engine's false 0 is
          // exactly what made the top row look like it was dancing.
          const hv = (state.engines[eng()] || {})._held?.[ref.d.series];
          if (typeof hv === 'number') {
            const lf = { ...(l || {}), [ref.d.series]: hv };
            ref.v.textContent = ref.d.main(lf);
            ref.sub.textContent = (ref.d.sub ? ref.d.sub(lf) : '') + ' · held, cache resident';
          }
        }
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
    if (ref.d.alertWhen) ref.el.classList.toggle('alert', l[ref.d.alertWhen] === true);
    if (ref.sp && ref.d.series) {
      if (ref.d.life) {
        spark(ref.sp, (state._cacheHitLife || []).map(r => r[1]), ref.d.color,
              { times: (state._cacheHitLife || []).map(r => r[0]), fmt: ref.d.sparkFmt });
      } else if (ref.d.savings && state._savSparks && state._savSparks[ref.d.savings]) {
        // rolling 60-min hot-cost $/hr from persisted minute-buckets — the
        // three bubbles get 6 h / 3 d / 7 d windows, so lines truly vary
        const rows = state._savSparks[ref.d.savings];
        // day = rolling $/hr history; month/year = the bubble's own monthly/
        // yearly projection (audit F1: one shared '/hr' formatter labelled a
        // $43k yearly projection "$43000/hr" — 8760x wrong)
        const sfmt = ref.d.savings === 'day'
          ? (v => '$' + v.toFixed(2) + '/hr')
          : (v => '$' + v.toLocaleString('en-US', {maximumFractionDigits: 0}));
        spark(ref.sp, rows.map(r => r[1]), ref.d.color,
              { times: rows.map(r => r[0]), fmt: sfmt });
      } else {
        const now = Date.now() / 1000;
        const hour = k => state.store.at(`${eng()}.${k}`).filter(([t]) => t >= now - 3600);
        const has = rs => rs.some(([, v]) => v != null);
        let rows = hour(ref.d.series);
        // engines without the token-level series (Atlas) chart the step-level
        // one — chosen by which series holds history, so idle gaps keep it
        if (ref.d.seriesAlt && !has(rows)) {
          const alt = hour(ref.d.seriesAlt);
          if (has(alt)) rows = alt;
        }
        const step = Math.max(1, Math.ceil(rows.length / 200));
        rows = rows.filter((_, i) => i % step === 0 || i === rows.length - 1);
        spark(ref.sp, rows.map(r => r[1]), ref.d.color,
              { times: rows.map(r => r[0]), fmt: ref.d.sparkFmt });
      }
    }
  }
  // side tiles
  renderNodePicker();
  const h = hwLatest();
  document.querySelectorAll('#side-tiles .st').forEach((el, i) => {
    const vals = SIDE_TILES[i][1](h);
    const [lbl, , opt] = SIDE_TILES[i];
    if (typeof lbl === 'function') el.querySelector('.l').textContent = lbl(h);
    if (opt?.vram) el.hidden = !h.vram_total_gb;
    const vEl = el.querySelector('.v');
    if (SIDE_TILES[i][2]?.duo) {
      vEl.innerHTML = vals.map(s => `<span>${s}</span>`).join('');
    } else {
      vEl.textContent = vals[0];
    }
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
  const sm = state.statModel[eng()];
  document.querySelector('#engine-name').textContent = `${meta.model || meta.kind || '–'} @ ${meta.url || ''}` +
    (sm ? ` · stats: ${sm === '-' ? 'before model split' : String(sm).split('/').pop()}` : '');
  document.querySelector('#engine-caret').hidden = false;
  updateTabStatus(meta);
  renderStaleBanner(meta);
  adaptHwCharts(hwLatest());
  renderModelTable();
}

// Selected engine as this tab sees it: 'down' = stream dropped or engine
// unreachable, 'stale' = no fresh row for STALE_AFTER_S (sampler ticks at 2 Hz).
// state.connected stays undefined until the first stream attempt settles.
const STALE_AFTER_S = 10;
function engineHealth() {
  const e = state.engines[eng()] || {};
  const age = e.rx ? (Date.now() - e.rx) / 1000 : null;
  if (state.connected === false) return { st: 'down', why: 'stream', age };
  if (e.meta && e.meta.alive === false) return { st: 'down', why: 'engine', age };
  if (state.connected && (age == null || age > STALE_AFTER_S)) return { st: 'stale', why: 'quiet', age };
  return { st: 'up', age };
}

// Tab title carries live decode speed; the favicon gains a status dot only
// when something is wrong, so a background tab still tells the truth.
const favLink = document.querySelector('link[rel="icon"][type="image/svg+xml"]');
const favHref = favLink ? favLink.getAttribute('href') : null;
let favSvg = null, favState = 'up';
if (favHref) fetch(favHref).then(r => r.text()).then(t => { favSvg = t; }).catch(() => {});
function updateTabStatus(meta) {
  const { st } = engineHealth();
  const l = latest();
  const model = String(meta.model || meta.id || '').split('/').pop();
  const speed = st === 'up' && _fin(l.decode_tps_now) && l.decode_tps_now > 0
    ? Math.round(l.decode_tps_now).toLocaleString('en-US') + ' tok/s · ' : '';
  const title = (st === 'up' ? '' : st === 'stale' ? '⏸ ' : '⚠ ') + speed +
    (model ? model + ' — ' : '') + 'LIMCARS';
  if (document.title !== title) document.title = title;
  if (!favLink || !favSvg || st === favState) return;
  favState = st;
  if (st === 'up') { favLink.setAttribute('href', favHref); return; }
  const col = st === 'stale' ? '#facc15' : '#fb7185';
  const svg = favSvg.replace('</svg>',
    `<circle cx="50" cy="14" r="12" fill="${col}" stroke="#0b0e13" stroke-width="3"/></svg>`);
  favLink.setAttribute('href', 'data:image/svg+xml,' + encodeURIComponent(svg));
}

const fmtAge = s => s == null ? 'an unknown time' : s < 90 ? Math.round(s) + ' s'
  : s < 5400 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h';
function renderStaleBanner(meta) {
  const b = document.querySelector('#stale-banner');
  if (!b) return;
  const { st, why, age } = engineHealth();
  const stale = st !== 'up';
  document.querySelector('#dash-pane').classList.toggle('is-stale', stale);
  b.hidden = !stale;
  if (!stale) return;
  const name = String(meta.model || meta.id || 'engine').split('/').pop();
  b.textContent = why === 'stream'
    ? `⚠ Live stream disconnected — reconnecting. Values below are from ${fmtAge(age)} ago.`
    : why === 'engine'
      ? `⚠ ${name} is unreachable — showing its last values from ${fmtAge(age)} ago.`
      : `⏸ No update from ${name} for ${fmtAge(age)} — values below may be stale.`;
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
        `${k} = <b style="color:var(--text)">${typeof v === 'number' ? fmt(v, 2) : escH(String(v))}</b>`).join(' · ') +
      ` · savings = <b style="color:var(--text)">${l.savings ? '$' + (l.savings.day || 0).toFixed(2) + '/day ' + escH('(' + (l.savings.basis || 'at this rate') + ')') : '–'}</b></div></td></tr>` : '';
    return `<tr data-model="${escH(meta.id)}" style="cursor:pointer"><td>${escH(meta.model || meta.id)}</td><td>${escH(meta.kind || '–')}</td>
      <td>${fmt(l.running, 0)}</td><td>${fmt(l.waiting, 0)}</td>
      <td>${l.kv_pct != null ? l.kv_pct.toFixed(0) + '%' : '–'}</td>
      <td>${l.spec_accept_len != null ? l.spec_accept_len.toFixed(2) : '–'}</td>
      <td>${(() => { const v = (l.ttft_n >= 20 || l.ttft_avg_ms == null) ? l.ttft_p50_ms : l.ttft_avg_ms;
                     return v != null ? Math.round(v) + (l.ttft_n < 20 ? ' *' : '') : '–'; })()}</td>
      <td>${(() => { const v = histNum(l, 'e2e');
                     return v != null ? (v / 1000).toFixed(2) + ((l.e2e_n || 0) < 20 ? ' *' : '') : '–'; })()}</td>
      <td>${(state.store.at(`${meta.id}.decode_tps_now`) || []).length}</td>
      <td>${meta.last_scrape ? new Date(meta.last_scrape * 1000).toTimeString().slice(0, 8) : '–'}</td></tr>` + detail;
  });
  t.innerHTML = `<tr><th>Model</th><th>Backend</th><th>Run</th><th>Wait</th><th>KV %</th>
    <th>Spec accept</th><th title="p50 over the 30 s window; * = true mean, too few requests in the window for a p50 to mean anything">TTFT ms</th><th title="request in to last token out, queue included; * = true mean, too few requests for a p50">e2e s</th><th>Samples</th><th>Seen</th></tr>` +
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
    return `<div class="row"><span class="t">${hh}</span><span>+${escH(String(r.completed ?? '–'))} completed</span>
      <span class="muted">running ${escH(String(r.running ?? '–'))} · waiting ${escH(String(r.waiting ?? '–'))}</span></div>`;
  }).join('');
}

// ---------------- snapshot + SSE ----------------

async function seed(reseedWindow = true) {
  const sel = state.sel ? `&engine=${encodeURIComponent(state.sel)}` : '';
  const r = await fetch(`/api/snapshot?seconds=${state.windowSec + 30}${sel}`);
  const snap = await r.json();
  state.engines = {};
  for (const [id, e] of Object.entries(snap.engines || {})) {
    const eng = { meta: e, latest: e.latest || {},
      rx: e.last_scrape ? Date.now() - Math.max(0, (snap.ts || 0) - e.last_scrape) * 1000 : 0 };
    state.engines[id] = eng;
    for (const [key, raw] of Object.entries(e.series || {})) {
      // Smooth the recorded idle zeros for display only (see fillIdleZeros) — the
      // ring keeps what the engine said, the chart stops showing a false 0%.
      // Rate fields get the #28 treatment on the seeded side too: a recorded 0
      // is an unmeasurable rate, so it seeds as a gap, never as a plunging point.
      const rows = HOLD_LAST_NONZERO.has(key) ? fillIdleZeros(raw)
        : RATE_FIELDS.has(key) ? rateZerosToGaps(raw) : raw;
      state.store.seed(`${id}.${key}`, rows);
      // Prime the hold pad from history. Without this a reload forgets the last real
      // reading, and an idle engine's DOCUMENTED false zero (kv_cache_usage_perc = 0
      // while prefix-cached blocks are still resident) would then print and draw as
      // "cache empty" for the whole session — the two layers agreeing only by being
      // both wrong. The truth is already in the rows we just seeded, so borrow it.
      if (HOLD_LAST_NONZERO.has(key)) {
        for (let i = rows.length - 1; i >= 0; i--) {
          const v = rows[i] && rows[i][1];
          if (typeof v === 'number' && v > 0) { (eng._held || (eng._held = {}))[key] = v; break; }
        }
      }
    }
    // e2e/queue p50 series aren't in snapshot series list — seed from ring rows missing: skip, SSE fills in
  }
  state.hw = snap.hw || {};
  for (const [k, rows] of Object.entries(snap.hw?.series || {})) state.store.seed(`hw.${k}`, rows);
  seedNodes(snap);
  state.lastHwSeed = Date.now();
  state.pricing = snap.pricing;
  if (reseedWindow) {
    buildCards();
    if (!state.charts.length) buildCharts();
    else state.charts.forEach(c => c._chart && !c._fixedWin && c._chart.setWindow(state.windowSec));
  } else {
    buildCards();
  }
}

// Self-heal: hw series always exist server-side (ring history), so re-seeding
// costs one cheap GET and NEVER restarts a chart from 0 — it backfills the
// window the SSE stream missed (whitelist gaps, server restarts, zombie
// sockets where reader.read() hangs forever without erroring).
function seedNodes(snap) {
  state.nodes = state.nodes || {};
  for (const [id, n] of Object.entries(snap.nodes || {})) {
    state.nodes[id] = Object.assign(state.nodes[id] || {}, n);
    for (const [k, rows] of Object.entries(n.series || {})) state.store.seed(`hw@${id}.${k}`, rows);
  }
}
async function hwResync() {
  try {
    const snap = await (await fetch('/api/snapshot?seconds=210')).json();
    if (snap && snap.hw && snap.hw.series)
      for (const [k, rows] of Object.entries(snap.hw.series)) state.store.seed(`hw.${k}`, rows);
    if (snap && snap.hw && snap.hw.gpus) state.hw.gpus = snap.hw.gpus;
    if (snap) seedNodes(snap);
    state.lastHwSeed = Date.now();
  } catch { /* server briefly down — the 30 s watchdog retries */ }
}
setInterval(() => {
  if (document.hidden) return;
  const now = Date.now();
  if (state.lastStreamMsg && now - state.lastStreamMsg > 90000) {
    // zombie SSE stream (no events at all for 90 s though hw ticks every
    // second): abort the fetch so the normal reconnect path fires
    try { state.sseAbort && state.sseAbort.abort(); } catch { /* noop */ }
    hwResync();
  } else if (now - (state.lastHwSeed || 0) > 150000) {
    hwResync();   // silent stall of just the hw topic: refill from rings
  }
}, 30000);

const lastId = {};
async function connect() {
  const topics = ['hw', 'requests', 'events', ...Object.keys(state.engines).map(id => `engine:${id}`),
                  ...Object.keys(state.nodes || {}).map(id => `hw:${id}`)].join(',');
  // self-heal on (re)connect: hw series come back from the SERVER rings, so a
  // reconnect after a server restart or a dropped socket never starts charts
  // from 0
  hwResync();
  const ac = new AbortController();
  state.sseAbort = ac;
  state.lastStreamMsg = Date.now();
  try {
    const res = await fetch(`/api/events?topics=${encodeURIComponent(topics)}`,
                            { headers: { Accept: 'text/event-stream' }, signal: ac.signal });
    if (!res.ok || !res.body) throw new Error('sse http ' + res.status);
    state.sseBackoff = 1000;   // a stream opened: forget the backoff ladder
    // audit: pill used to glow 'live 2 Hz' on response headers alone
    document.querySelector('#conn-dot').className = 'dot up';
    document.querySelector('#conn-txt').textContent = 'live 2 Hz';
    state.connected = true;
    const reader = res.body.getReader(); const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      state.lastStreamMsg = Date.now();
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
          // push EVERY numeric hw key — the old 6-key whitelist omitted
          // mem_used_gb, so the RAM chart only ever had its page-load seed
          // and went blank once the 3-min window slid past it (user-caught:
          // 'again the RAM chart does not SHOW the chart')
          for (const [k, v] of Object.entries(o))
            if (k !== 't' && typeof v === 'number') state.store.push(`hw.${k}`, o.t, v);
        } else if (ev.startsWith('hw:')) {
          const nid = ev.slice(3), n = state.nodes && state.nodes[nid];
          if (!n) continue;
          n.latest = o; n.alive = true; n.error = null;
          for (const [k, v] of Object.entries(o))
            if (k !== 't' && typeof v === 'number') state.store.push(`hw@${nid}.${k}`, o.t, v);
        } else if (ev.startsWith('engine:')) {
          const id_ = ev.slice(7);
          const e = state.engines[id_]; if (!e) continue;
          e.latest = o; e.rx = Date.now();
          if (e.meta) { e.meta.latest = o; e.meta.alive = true; }
          for (const k of ['prefill_tps_now','decode_tps_now','prefill_tps_avg','decode_tps_avg',
                           'running','waiting','kv_pct','kv_pct_shown','ttft_p50_ms','itl_p50_ms','queue_time_p50_ms',
                           'e2e_p50_ms','cache_hit_pct','spec_accept_pct','spec_step_accept_pct','spec_accept_len',
                           'draft_tok_per_step','gen_peak','prompt_total','gen_total'])
            // seriesSample keeps the curve honest about what the number above it says:
            // rate fields go null (not 0) while the sticky activity flag says nothing is
            // happening, and kv_pct holds its last real reading instead of plunging on
            // the idle 0 the engine reports. Feeding raw 2 Hz values made the top row of
            // bubbles dance at the poll rate while every printed value stayed put.
            state.store.push(`${id_}.${k}`, o.t, seriesSample(k, o[k], o, (e._held || (e._held = {}))));
        } else if (ev === 'events' && o.type === 'node_down') {
          const n = state.nodes && state.nodes[o.node];
          if (n) { n.alive = false; n.error = o.error; }
        } else if (ev === 'events' && o.type === 'engine_down') {
          const e = state.engines[o.engine];
          if (e && e.meta) e.meta.alive = false;
        } else if (ev === 'requests') {
          state.reqFeed.unshift(o); if (state.reqFeed.length > 50) state.reqFeed.pop();
          renderFeed();
        }
      }
    }
  } catch { /* fall through */ }
  state.connected = false;
  document.querySelector('#conn-dot').className = 'dot down';
  document.querySelector('#conn-txt').textContent = 'reconnecting…';
  // self-heal: re-pull snapshot rings so engine latest/bubbles never sit
  // stale after an outage (audit finding: lastId was dead state — reconnect
  // replayed nothing, so lost events stayed lost until a manual refresh)
  seed(false).catch(() => {});
  // Exponential backoff, capped at 30 s (was a fixed 2 s retry: with no engine
  // connected the stream was rejected every 2 s around the clock, which read as
  // the page refreshing itself nonstop). Reset once a stream actually opens.
  state.sseBackoff = Math.min((state.sseBackoff || 1000) * 2, 30000);
  setTimeout(connect, state.sseBackoff);
}

// ---------------- token counters report (old LIMCARS parity) ----------------

const CNT_TABS = [['1h','1 hour'],['1d','1 day'],['7d','7 days'],['1mo','1 month'],['1y','1 year'],['all','All time']];
let cntLabel = 'all';
const fmtK = v => v == null ? '–' : v >= 1e9 ? (v/1e9).toFixed(2)+'B' : v >= 1e6 ? (v/1e6).toFixed(2)+'M' : v >= 1e3 ? (v/1e3).toFixed(1)+'k' : String(Math.round(v));
const usd = v => v == null || !isFinite(v) ? '–' : '$' + (v < 1 ? v.toFixed(4) : v < 100 ? v.toFixed(2) : v.toLocaleString('en-US',{maximumFractionDigits:2}));

async function renderCounters() {
  const tabs = document.querySelector('#cnt-tabs');
  tabs.innerHTML = CNT_TABS.map(([l, t]) =>
    `<button class="pill tiny${l===cntLabel?' on':''}" data-l="${l}">${t}</button>`).join('');
  tabs.querySelectorAll('[data-l]').forEach(b => b.onclick = () => { cntLabel = b.dataset.l; renderCounters(); });
  let d;
  try { d = await (await fetch('/api/counters?window=' + cntLabel + '&engine=' + encodeURIComponent(eng()) + statQ())).json(); }
  catch (e) { return; }  // audit: uncaught rejection every 30 s during outages
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
  const au = d.audit;
  if (au) {
    const since = new Date(au.since * 1000).toLocaleString('en-GB', {day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'});
    const off = Object.entries(au.off || {});
    rows.push(['Ledger check vs engine totals', au.ok
      ? `<span style="color:var(--teal)">✓ matches</span> <span class="muted">· ${fmtK(au.engine.prompt)} prompt / ${fmtK(au.engine.gen)} gen since ${since}</span>`
      : `<b style="color:var(--yellow)">⚠ ${off.map(([k, v]) => `${k} ${v > 0 ? '+' : ''}${fmtK(v)}`).join(' · ')} tok</b>` +
        ` <span class="muted">stored vs engine since ${since}</span>`]);
  }
  rows.push([`<span class="muted">API · cache write · ${fmtK(w.uncached)} tok @ ${pf(p.cache_write)}</span>`, usd(w.uncached/1e6*p.cache_write)]);
  rows.push([`<span class="muted">API · cache read · ${fmtK(w.cached)} tok @ ${pf(p.cache_read_hot)}</span>`, usd(w.cached/1e6*p.cache_read_hot)]);
  rows.push([`<span class="muted">API · output · ${fmtK(w.gen)} tok @ ${pf(p.output)}</span>`, usd(w.gen/1e6*p.output)]);
  rows.push([`<b>Money saved by local (hot cache)</b>`,
    `<b style="color:var(--teal)">${usd(w.cost_hot)}</b> <span class="muted">/ ${usd(a.cost_hot)} all-time</span>` +
    `<br><span class="muted">${usd(d.per_hour)}/hr · ${usd(d.per_day)}/day @ this rate</span>`]);
  rows.push([`<span class="muted">If cache reads used base rate (${pf(p.cache_read)})</span>`, usd(w.cost_base) + ` <span class="muted">/ ${usd(a.cost_base)} all</span>`]);
  rows.push([`<span class="muted">If context not cached at all (input ${pf(p.input)})</span>`, usd(w.cost_nocache) + ` <span class="muted">/ ${usd(a.cost_nocache)} all</span>`]);
  for (const [label, v] of (d.project || []))
    rows.push([`<span class="muted">Projected · ${escH(label)} @ this rate</span>`, usd(v)]);
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
  // UI can authenticate its transcript fetches (?pw= accepted server-side).
  // Rebuilt whenever state.pw changes (audit finding: a one-shot latch froze
  // an unauthenticated or stale-pw URL into both frame and new-tab pill)
  const pwNow = state.pw || '';
  if (tracesFrame.dataset.built === '1' && tracesFrame.dataset.pwUsed === pwNow) {
    // already authenticated with the current pw — just show it
  } else {
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
    // the new-tab pill must carry the SAME authenticated URL — without ?pw=
    // the standalone page loads but every trace fetch 401s (user-caught:
    // 'goes to a page that is not populated with any traces')
    const nt = document.querySelector('#traces-newtab');
    if (nt) nt.href = src;
    tracesFrame.dataset.built = '1';
    tracesFrame.dataset.pwUsed = state.pw || '';
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
  if (e.origin !== location.origin) return;  // audit: trusted any origin
  if (e.data === 'sw-close' && tracesPane.style.display === 'block') backToDash();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && tracesPane.style.display === 'block') backToDash();
});

// ---------------- panel settings (server-side, shared — old dash_settings parity) ----------------

const SET_OPTS = {
  bubbles_main: 'Bubbles · main strip',
  bubbles_savings: 'Bubbles · cost-savings',
  bubbles_potential: 'Bubbles · lost potential',
  charts_infer: 'Charts · infer (KV/PP/gen)',
  chart_per_client: 'Chart · per-client context',
  chart_per_model: 'Table · stats by model',
  chart_savings_daily: 'Chart · daily money saved',
  requests_feed: 'Table · recent requests',
  ssd_io_tile: 'Sidebar · SSD I/O tile',
  ssd_chart: 'Sidebar · SSD read/write chart',
  ssd_usage: 'Sidebar · SSD usage box',
  cold_usage: 'Sidebar · SSD cold experts & reserve box',
};
function applySettings(s) {
  sideSettings = s || {};
  applyTheme();
  const hide = (id, off, sel) => {
    const el = document.querySelector(id); if (!el) return;
    const tgt = sel ? el.closest(sel) : el; if (!tgt) return;
    tgt.style.display = off ? 'none' : '';
  };
  hide('#bub-main', sideSettings.bubbles_main === false, '.bub-group');
  hide('#bub-savings', sideSettings.bubbles_savings === false, '.bub-group');
  hide('#bub-potential', sideSettings.bubbles_potential === false, '.bub-group');
  hide('#charts-grid', sideSettings.charts_infer === false);
  hide('#mtbl', sideSettings.chart_per_model === false, '.chart-card');
  hide('#sav-card', sideSettings.chart_savings_daily === false);
  hide('#cc-card', sideSettings.chart_per_client === false);
  hide('#req-feed', sideSettings.requests_feed === false, '.chart-card');
  hide('#sch-ssd', sideSettings.ssd_chart === false, '.side-chart');
  hide('#ssd-usage', sideSettings.ssd_usage === false);
  hide('#st-ssd-io', sideSettings.ssd_io_tile === false);
  renderColdset();
  const rsel = document.querySelector('#refresh-sel');
  if (rsel && document.activeElement !== rsel) rsel.value = String(sideSettings.refresh_s ?? 0.5);
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
  let d;
  try { d = await (await fetch(`/api/engine-info?engine=${encodeURIComponent(eng())}`)).json(); }
  catch (e) { return; }  // audit: kept showing pre-outage config as current
  const t = document.querySelector('#info-tbl');
  if (!d.healthy) { t.innerHTML = `<tr><td class="muted">${escH(String(d.error || 'offline'))}</td></tr>`; return; }
  const rows = [['Model', d.model], ['Version', d.version],
    ['Context window', d.ctx ? fmtK(d.ctx) + ' tok' : '–'],
    ['KV pool', d.pool_tokens ? fmtK(d.pool_tokens) + ' tok' + (d.num_gpu_blocks ? ` · ${d.num_gpu_blocks.toLocaleString()} blocks × ${d.block_size}` : '') : '–'],
    ['KV dtype', d.kv_dtype], ['Speculative', d.spec + ' · K=' + (d.draft_k ?? '–')],
    ['Accept rate', d.accept_rate != null ? d.accept_rate + '%' : '–'],
  ];
  // engines with their own contract (TandemLLM) bring their own rows
  for (const [k, v] of (d.rows_extra || [])) {
    if (!rows.some(r => r[0] === k)) rows.push([k, v]);
  }
  const html = rows.map(([k, v]) => `<tr><td class="muted">${k}</td><td class="mono">${escH(String(v ?? '–'))}</td></tr>`).join('');
  const cfg = Object.entries(d.config || {}).map(([k, v]) =>
    `<tr><td class="muted">${escH(k)}</td><td class="mono">${escH(String(v))}</td></tr>`).join('');
  const launch = d.launch
    ? `<div class="side-lbl" style="margin-top:10px">launch recipe (${escH(d.launch_source || 'process cmdline')})</div>
       <pre class="mono small" style="white-space:pre-wrap;word-break:break-all;max-height:150px;overflow:auto;background:#0b1322;padding:8px;border-radius:6px">${escH(d.launch)}</pre>
       <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
         <button class="pill" id="rec-qs">＋ Add Quickstart</button>
         <button class="pill" id="rec-edit-cur">✎ Edit</button>
         <button class="pill" id="rec-new">＋ New</button>
       </div>`
    : '';
  t.innerHTML = html
    + `<tr><td colspan="2" style="padding:6px 0 0"><div class="side-lbl">full engine config (vllm:cache_config_info)</div>
       <table class="data" style="width:100%">${cfg}</table>${launch}</td></tr>`;
  const saveRec = (name, cmd) => authFetch('/api/recipes', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, cmd }) }).then(r => r.json());
  const bqs = document.querySelector('#rec-qs');
  if (bqs) bqs.onclick = () => {
    if (!d.launch) return toast('No running launch recipe detected', 'err');
    recipeDialog({ title: '＋ Add Quickstart — current running recipe',
      name: '🚀 ' + (d.model || 'engine').split('/').pop(), cmd: d.launch, onSave: saveRec });
  };
  const bec = document.querySelector('#rec-edit-cur');
  if (bec) bec.onclick = () => {
    if (!d.launch) return toast('No running launch recipe detected', 'err');
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

// ---------------- daily money saved · real usage (user feature) ----------------
// Per-model per-day hot-tier $ from persisted minute-buckets. 1w/1m/3m/1y
// switch; bars up to 3 months, lines beyond; hover = per-model cost, total
// tokens and write (cache-write prompt) / read (cache-hit) / decode split.
const SAV_BASE = ['#4fa3ff', '#ffb454', '#5dd39e', '#e06c9f',
                  '#b48cff', '#ffd75d', '#5ad1d1', '#ff8a65'];
const SAV_COLORS = SAV_BASE.slice();      // rotated by applyTheme (reactive)
let savDays = 30;      // default timeframe: 1 month (user request)

function _savMoney(v) {
  return '$' + (v >= 100 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(4));
}
function _savDate(day) {
  // UTC getters: day indices are UTC days; local getters would render the
  // previous day in any timezone west of UTC (audit F3)
  const d = new Date(day * 86400000);
  return String(d.getUTCMonth() + 1).padStart(2, '0') + '-' + String(d.getUTCDate()).padStart(2, '0');
}

async function renderSavingsChart() {
  const host = document.querySelector('#sav-chart');
  if (!host || host.closest('[style*="display: none"]')) return;
  let d;
  try {
    const res = await fetch('/api/savings-history?days=' + savDays + '&engine=' + encodeURIComponent(eng()));
    d = await res.json();
    if (!d.ok) throw new Error(d.error || 'bad response');
  } catch (e) {
    host.innerHTML = '<span class="muted small">savings history unreachable</span>';
    return;
  }
  const ids = Object.keys(d.models || {});
  if (!d.days.length) {
    host.innerHTML = '<span class="muted small">no usage in this window</span>';
    const st0 = document.querySelector('#sav-stats');
    if (st0) st0.innerHTML = 'total saved <b>$0.00</b>';
    return;
  }
  const color = {};
  ids.forEach((id, i) => color[id] = SAV_COLORS[i % SAV_COLORS.length]);
  const nowDay = Math.floor(Date.now() / 86400000);
  const n = d.days_n;
  const start = nowDay - n + 1;
  const byDay = new Map(d.days.map(x => [x.day, x.engines]));
  const W = Math.max(300, host.clientWidth || 900), H = 190;
  const padL = 58, padR = 12, padT = 10, padB = 22;
  const iw = W - padL - padR, ih = H - padT - padB;
  let maxV = 0;
  for (const x of d.days) for (const e of Object.values(x.engines)) maxV = Math.max(maxV, e.cost);
  maxV = maxV || 1;
  const xOf = day => padL + ((day - start) / Math.max(1, n - 1)) * iw;
  const yOf = v => padT + ih - (v / maxV) * ih;
  let svg = `<svg width="${W}" height="${H}" style="display:block">`;
  for (let i = 0; i <= 4; i++) {
    const v = maxV * i / 4, y = yOf(v);
    svg += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--line)"/>`;
    svg += `<text x="${padL - 6}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--muted)">${_savMoney(v)}</text>`;
  }
  // x labels: start, middle, today
  for (const day of [start, Math.round((start + nowDay) / 2), nowDay])
    svg += `<text x="${xOf(day)}" y="${H - 6}" text-anchor="middle" font-size="9" fill="var(--muted)">${_savDate(day)}</text>`;
  const bars = n <= 92;
  if (bars) {
    const bw = Math.max(2, Math.min(12, (iw / n) * 0.72));
    for (const x of d.days) {
      const cx = xOf(x.day);
      Object.entries(x.engines).forEach(([id, e], j) => {
        const bx = cx - (bw * ids.length) / 2 + j * bw;
        const by = yOf(e.cost);
        svg += `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${(bw - 1).toFixed(1)}" height="${Math.max(1, padT + ih - by).toFixed(1)}" fill="${color[id]}" rx="1"/>`;
      });
    }
  } else {
    for (const id of ids) {
      const pts = d.days.filter(x => x.engines[id])
        .map(x => `${xOf(x.day).toFixed(1)},${yOf(x.engines[id].cost).toFixed(1)}`);
      if (pts.length > 1)
        svg += `<polyline points="${pts.join(' ')}" fill="none" stroke="${color[id]}" stroke-width="1.6"/>`;
    }
  }
  // hover crosshair + tooltip
  svg += `<line id="sav-cross" x1="0" y1="${padT}" x2="0" y2="${padT + ih}" stroke="var(--accent)" stroke-width="1" opacity="0"/>`;
  svg += `</svg><div id="sav-tip" style="position:absolute;top:6px;left:0;display:none;pointer-events:none;
          background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 9px;
          font-size:11px;z-index:5;box-shadow:0 4px 14px rgba(0,0,0,.4)"></div>`;
  host.innerHTML = svg;
  const tip = host.querySelector('#sav-tip');
  const cross = host.querySelector('#sav-cross');
  // byDay maps day -> engines map (see Map(d.days.map(x => [x.day, x.engines])))
  const dayTot = engines => Object.values(engines).reduce((a, e) => a + e.cost, 0);
  // on* property assignment: render runs every 60 s on a persistent host —
  // addEventListener would stack a new handler (and stale closure) per render
  host.onmousemove = ev => {
    const r = host.getBoundingClientRect();
    const px = ev.clientX - r.left;
    const day = Math.round(start + ((px - padL) / iw) * (n - 1));
    const rec = byDay.get(day);
    if (day < start || day > nowDay) { tip.style.display = 'none'; cross.setAttribute('opacity', 0); return; }
    const cx = xOf(day);
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('opacity', .6);
    let html = `<b>${new Date(day * 86400000).toISOString().slice(0, 10)}</b>`;
    if (!rec || !Object.keys(rec).length) {
      html += '<br><span class="muted">no usage</span>';
    } else {
      for (const [id, e] of Object.entries(rec)) {
        html += `<br><span style="color:${color[id]}">●</span> ${escH(d.models[id] || id)}
          <b>${_savMoney(e.cost)}</b> · ${fmtK(e.tokens)} tok
          <br><span class="muted" style="margin-left:14px">w ${fmtK(Math.max(0, (e.prompt || 0) - (e.cached || 0)))} · r ${fmtK(e.cached)} · d ${fmtK(e.gen)}</span>`;
      }
      html += `<br><b>${_savMoney(dayTot(rec))}</b> <span class="muted">total</span>`;
    }
    tip.innerHTML = html;
    tip.style.display = 'block';
    tip.style.left = Math.min(px + 14, W - 220) + 'px';
  };
  host.onmouseleave = () => { tip.style.display = 'none'; cross.setAttribute('opacity', 0); };
  // stats + legend (total saved with ALL models in total — user spec)
  const st = document.querySelector('#sav-stats');
  if (st) {
    const leg = ids.map(id => `<span style="color:${color[id]}">●</span> ${escH(d.models[id] || id)}`).join('  ');
    // measured occupancy: hours a day the box actually produced tokens in
    // this window (from the minute rows), averaged over days that had traffic
    const occ = (d.avg_active_hours_per_day == null) ? ''
      : ` · avg <b>${d.avg_active_hours_per_day}</b> h/day active`;
    st.innerHTML = `total saved <b style="color:var(--green,#5dd39e)">${_savMoney(d.totals.cost)}</b>`
      + ` · ${fmtK(d.totals.tokens)} tokens`
      // cached is a subset of the prompt tokens, so it is reported as "of
      // which" and never added into the total (that addition was the 1.9x)
      + (d.totals.cached ? ` <span class="muted">(of which ${fmtK(d.totals.cached)} from cache)</span>` : '')
      + ` · ${d.days.length} active day${d.days.length === 1 ? '' : 's'}`
      + occ
      + (ids.length ? ` &nbsp;${leg}` : '');
  }
}
document.querySelectorAll('#sav-seg [data-days]').forEach(b =>
  b.addEventListener('click', () => {
    savDays = +b.dataset.days;
    document.querySelectorAll('#sav-seg [data-days]').forEach(x => x.classList.toggle('on', x === b));
    renderSavingsChart();
  }));

// boot extras
// ---------------- theme system (top bar) ----------------
// 24 preset base colors; each drives --accent + its nuances (borders, chart
// "now" dot, pill highlights) the same way house orange does by default.
// Persisted server-side (settings.theme / settings.palette) so every tab on
// the LAN shares one look. Day/night flips surface vars in style.css.
const PALETTES = {
  orange: '#ff8c1a', amber: '#f59e0b', gold: '#eab308', sand: '#d6b98c',
  bronze: '#b08d57', tomato: '#f97316', coral: '#fb7185', rose: '#f43f5e',
  red: '#ef4444', pink: '#ec4899', fuchsia: '#d946ef', purple: '#a855f7',
  violet: '#8b5cf6', lavender: '#c4b5fd', indigo: '#6366f1', blue: '#3b82f6',
  sky: '#0ea5e9', cyan: '#06b6d4', teal: '#14b8a6', mint: '#4ade80',
  green: '#22c55e', emerald: '#10b981', lime: '#84cc16', slate: '#94a3b8',
};
function hexRgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}
// Per-browser preference in a cookie (not server settings): each person
// browsing the dash from their own browser keeps their own theme, and the
// click applies instantly with no server round-trip that an SSE settings
// update could race back and revert.
const UI_COOKIE = 'limcars_ui';
function readUiPrefs() {
  try {
    const m = document.cookie.match(new RegExp('(?:^|; )' + UI_COOKIE + '=([^;]+)'));
    const v = m ? JSON.parse(decodeURIComponent(m[1])) : {};
    // audit: a valid-JSON non-object cookie (e.g. 'null') threw at module
    // top level and blanked the whole dashboard until manual cookie clear
    return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
  } catch (e) { return {}; }
}
function writeUiPrefs(patch) {
  const p = Object.assign(readUiPrefs(), patch);
  document.cookie = UI_COOKIE + '=' +
    encodeURIComponent(JSON.stringify(p)) +
    ';path=/;max-age=31536000;samesite=lax';
  return p;
}
let uiPrefs = readUiPrefs();
// Family hues: every chart/bubble accent is the house palette ROTATED to
// the chosen base — relationships between roles (decode vs prefill vs
// latency hues) stay exactly as in the default orange theme (user spec:
// "nuances of the base color the same way as in the default theme now").
const FAMILY_BASE = { cobalt: '#4f8cff', teal: '#2dd4bf', violet: '#a78bfa',
                      rose: '#fb7185', yellow: '#facc15', green: '#34d399' };
function hexToHsl(hex) {
  const n = parseInt(hex.slice(1), 16);
  const r = ((n >> 16) & 255) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
  if (mx === mn) return [0, 0, l];
  const d = mx - mn, s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
  let h = mx === r ? (g - b) / d + (g < b ? 6 : 0)
      : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h * 60, s, l];
}
function hslToHex(h, s, l) {
  const f = n => {
    const k = (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, Math.min(9 - k, 1)))));
  };
  return '#' + [f(0), f(8), f(4)].map(v => v.toString(16).padStart(2, '0')).join('');
}
function rotateCategorical(arr, base, delta, aroundHue) {
  for (let i = 0; i < base.length; i++) {
    if (delta === null) { arr[i] = base[i]; continue; }   // house exact
    const [, s, l] = hexToHsl(base[i]);
    // categories fan out evenly across ±90 deg of the chosen base hue
    const spread = (arr.length > 1 ? (i / (arr.length - 1)) * 180 - 90 : 0);
    arr[i] = hslToHex(((aroundHue || 0) + spread + 720) % 360, s, l);
  }
}
function applyTheme() {
  const mode = uiPrefs.theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = mode;
  const base = PALETTES[uiPrefs.palette] || PALETTES.orange;
  const rs = document.documentElement.style;
  rs.setProperty('--accent', base);
  rs.setProperty('--accent-weak', hexRgba(base, .12));
  rs.setProperty('--accent-border', hexRgba(base, .45));
  // Chart lines follow the CHOSEN color (user audit of the old full-wheel
  // rotation: "the change is not to the color I selected but to a totally
  // different color"). Each role keeps its identity through a fixed
  // analogous offset from the base hue + its own saturation/lightness, so
  // every line is a nuance of the palette color while multi-line charts
  // (CPU vs GPU temp) stay clearly distinct.
  const baseH = hexToHsl(base)[0];
  const warm = base === PALETTES.orange;   // house theme: keep EXACT hexes,
  const ROLE_OFFSET = { cobalt: 0, teal: 24, violet: -24, rose: 50,
                        yellow: -50, green: 14 };
  for (const [key, hex0] of Object.entries(FAMILY_BASE)) {
    const [, s, l] = hexToHsl(hex0);       // role's own s/l identity
    const hex = warm ? hex0 : hslToHex((baseH + ROLE_OFFSET[key] + 720) % 360, s, l);
    COLORS[key] = hex;
    rs.setProperty('--' + key, hex);       // CSS consumers (links, tiles)
  }
  COLORS.now = base;
  COLORS.muted = mode === 'light' ? '#5b6a84' : '#7f8ca3';
  COLORS.grid = mode === 'light' ? '#e2e7f0' : '#1c2432';
  // categorical charts follow the chosen color too: spread the categories
  // symmetrically around the base hue (±90 deg) instead of full rotation,
  // so they read as one family of the user's color
  rotateCategorical(CC_COLORS, CC_BASE, warm ? null : true, baseH);
  rotateCategorical(SAV_COLORS, SAV_BASE, warm ? null : true, baseH);
  // Page tint: the SECOND palette (user's original idea) — tints the whole
  // page (background/panels/grid lines) with a desaturated shade of the
  // chosen color. 'default'/unset keeps the exact house navy.
  const bgKey = uiPrefs.bg;
  if (bgKey && bgKey !== 'default' && PALETTES[bgKey]) {
    const n = parseInt(PALETTES[bgKey].slice(1), 16);
    const cr = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    // mix the swatch itself into the house surfaces: grey swatch -> grey
    // page, green swatch -> green page. Weight = how present the color is.
    const mix = (base, w) => '#' + base.map((c0, i) =>
      Math.round(c0 * (1 - w) + cr[i] * w).toString(16).padStart(2, '0')).join('');
    if (mode === 'light') {
      rs.setProperty('--bg', mix([242, 245, 250], .18));
      rs.setProperty('--panel', mix([255, 255, 255], .10));
      rs.setProperty('--panel-2', mix([233, 237, 245], .15));
      const line = mix([215, 221, 233], .38);
      rs.setProperty('--line', line); COLORS.grid = line;
    } else {
      // weights ordered so panels stay LIGHTER than the page (card
      // separation) while still reading as the chosen color
      rs.setProperty('--bg', mix([11, 14, 19], .20));
      rs.setProperty('--panel', mix([18, 23, 34], .30));
      rs.setProperty('--panel-2', mix([15, 20, 29], .25));
      const line = mix([31, 39, 53], .45);
      rs.setProperty('--line', line); COLORS.grid = line;
    }
  } else {
    for (const v of ['--bg', '--panel', '--panel-2', '--line']) rs.removeProperty(v);
  }
  const tb = document.getElementById('btn-theme');
  if (tb) tb.textContent = mode === 'light' ? '☀️' : '🌙';
  // legends are built once at chart-build time — repaint them now
  for (const card of (state.charts || [])) {
    const leg = card.querySelector && card.querySelector('.legend');
    if (leg && card._series)
      leg.innerHTML = card._series.map(s =>
        `<span><i style="background:${typeof s.color === 'function' ? s.color() : s.color}"></i>${s.label}</span>`).join('');
    if (card._chart && card._chart.mark) card._chart.mark();
  }
  // audit: daily money chart + session bars were interval-only, so a palette
  // change left them on OLD colors for up to 60 s — repaint them now
  try { renderSavingsChart(); renderSessionTokens(); } catch (e) { /* pre-boot */ }
}
function wireThemeUi() {
  const tb = document.getElementById('btn-theme');
  if (!tb) return;
  tb.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    uiPrefs = writeUiPrefs({ theme: next });
    applyTheme();
  });
  // two popovers built from the same swatch list: accent palette and page
  // tint (the tint one carries an extra 'default' swatch = house navy)
  const pops = [
    { btn: document.getElementById('btn-palette'), pop: document.getElementById('palette-pop'),
      key: 'palette', attr: 'data-pal', withDefault: false },
    { btn: document.getElementById('btn-bg'), pop: document.getElementById('bg-pop'),
      key: 'bg', attr: 'data-bg', withDefault: true },
  ];
  const closeAll = () => pops.forEach(p => {
    if (p.pop) p.pop.hidden = true;
    if (p.btn) p.btn.setAttribute('aria-expanded', 'false');
  });
  for (const P of pops) {
    if (!P.btn || !P.pop) continue;
    const openPop = () => {
      const sel = uiPrefs[P.key] || (P.withDefault ? 'default' : 'orange');
      const sw = (P.withDefault
        ? `<button ${P.attr}="default" title="house default" aria-label="house default"` +
          ` style="background:linear-gradient(135deg,#0b0e13,#121722)"` +
          ` aria-pressed="${sel === 'default'}"${sel === 'default' ? ' class="sel"' : ''}></button>`
        : '') + Object.entries(PALETTES).map(([name, hex]) =>
        `<button ${P.attr}="${name}" title="${name}" aria-label="${name}" style="background:${hex}"` +
        ` aria-pressed="${sel === name}"${sel === name ? ' class="sel"' : ''}></button>`).join('');
      P.pop.innerHTML = sw;
      const r = P.btn.getBoundingClientRect();
      P.pop.style.top = (r.bottom + 6) + 'px';
      P.pop.style.left = Math.max(8, Math.min(r.left, innerWidth - 210)) + 'px';
      closeAll();
      P.pop.hidden = false;
      P.btn.setAttribute('aria-expanded', 'true');
    };
    P.btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (P.pop.hidden) {
        openPop();
        if (e.detail === 0) (P.pop.querySelector('.sel') || P.pop.querySelector('button'))?.focus();   // opened from keyboard
      } else closeAll();
    });
    P.pop.addEventListener('click', (e) => {
      e.stopPropagation();
      const b = e.target.closest(`button[${P.attr}]`);
      if (!b) return;
      uiPrefs = writeUiPrefs({ [P.key]: b.getAttribute(P.attr) });
      applyTheme();
      openPop();           // refresh the selection ring
      P.pop.querySelector('.sel')?.focus();
    });
    // keyboard: arrows walk the 6-column swatch grid, Esc returns to the button
    P.pop.addEventListener('keydown', (e) => {
      const items = [...P.pop.querySelectorAll('button')];
      const i = items.indexOf(document.activeElement);
      const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 6, ArrowUp: -6 }[e.key];
      if (step && i >= 0) {
        e.preventDefault();
        items[Math.max(0, Math.min(items.length - 1, i + step))].focus();
      } else if (e.key === 'Escape') { e.stopPropagation(); closeAll(); P.btn.focus(); }
    });
  }
  document.addEventListener('click', closeAll);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAll(); });
}
applyTheme();
wireThemeUi();
(function wireSideSections() {
  document.querySelectorAll('.side-toggle').forEach(b => {
    const sec = document.getElementById('sec-' + b.dataset.sec);
    if (!sec) return;
    const set = c => { sec.classList.toggle('collapsed', c); b.setAttribute('aria-expanded', String(!c)); };
    set(!!(uiPrefs.collapsed || {})[b.dataset.sec]);
    b.addEventListener('click', () => {
      const c = !sec.classList.contains('collapsed');
      set(c);
      uiPrefs = writeUiPrefs({ collapsed: { ...(uiPrefs.collapsed || {}), [b.dataset.sec]: c } });
    });
  });
})();
(function wireHeaderTools() {
  const btn = document.getElementById('btn-tools'), box = document.getElementById('hdr-tools');
  if (!btn || !box) return;
  const set = open => { box.classList.toggle('open', open); btn.setAttribute('aria-expanded', String(open)); };
  btn.addEventListener('click', e => { e.stopPropagation(); set(!box.classList.contains('open')); });
  document.addEventListener('click', e => { if (!box.contains(e.target)) set(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') set(false); });
})();
window.__setTheme = (p, b) => {   // test hook: recolor without page reload
  const patch = {};
  if (p !== undefined && p !== null) patch.palette = p;
  if (b !== undefined) patch.bg = b;
  uiPrefs = writeUiPrefs(patch);
  applyTheme();
};
window.__themeDbg = { COLORS, CC_COLORS, SAV_COLORS, CC_BASE, SAV_BASE,
                      PALETTES, uiPrefsGet: () => uiPrefs };

// ---------------- lost potential bubble ----------------
// One wide strip under the cost-savings row: what this model demonstrated
// it can do in its best day/month/year vs what the recorded history
// actually used. Numbers come from /api/lost-potential (60 s server cache).
const fmtTokShort = n => {
  if (n == null || !isFinite(n)) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
};
const fmtUsd0 = n => n == null || !isFinite(n) ? null
  : n < 10 ? '$' + n.toFixed(2)                      // cents matter at the hour
  : '$' + Math.round(n).toLocaleString('en-US');      // scale up, drop cents
// Whole-drive SSD view (/api/ssd): GB moved in the last hour, today, 7 days and per
// day, plus the NVMe temperature against the drive's own warning/critical limits.
const fmtGB = v => v == null ? '–' : v >= 1000 ? (v / 1000).toFixed(2) + ' TB' : v.toFixed(v < 10 ? 2 : 1) + ' GB';
async function renderSsd() {
  const host = document.querySelector('#ssd-usage .ssd-body');
  if (!host) return;
  let d;
  try { d = await (await fetch('/api/ssd')).json(); } catch (e) { return; }
  if (!d || !d.disk) { host.textContent = 'no SSD found'; return; }
  const days = d.days || [];
  const today = Math.floor(Date.now() / 86400000);
  const td = days.find(x => x.day === today) || { read_gb: 0, written_gb: 0 };
  const wk = days.filter(x => x.day > today - 7).reduce((a, x) =>
    ({ r: a.r + x.read_gb, w: a.w + x.written_gb }), { r: 0, w: 0 });
  const t = d.temp_c, warn = d.temp_max_c, crit = d.temp_crit_c;
  const tcol = t == null ? '' : crit && t >= crit ? 'var(--rose)' : warn && t >= warn - 5 ? 'var(--amber, #f59e0b)' : '';
  const row = (l, r, w) => `<tr><td>${l}</td><td style="text-align:right">${fmtGB(r)}</td><td style="text-align:right">${fmtGB(w)}</td></tr>`;
  const n = Math.min(14, days.length), shown = days.slice(-n);
  const mx = Math.max(1, ...shown.map(x => Math.max(x.read_gb, x.written_gb)));
  const bw = 100 / Math.max(1, n);
  const bars = n ? `<svg viewBox="0 0 100 34" preserveAspectRatio="none" style="width:100%;height:46px;margin-top:6px">` +
    shown.map((x, i) => {
      const hr = 30 * x.read_gb / mx, hw = 30 * x.written_gb / mx;
      return `<rect x="${i * bw + bw * 0.1}" y="${32 - hr}" width="${bw * 0.38}" height="${hr}" fill="${COLORS.teal}"><title>${new Date(x.day * 86400000).toISOString().slice(0, 10)} read ${fmtGB(x.read_gb)}</title></rect>` +
             `<rect x="${i * bw + bw * 0.52}" y="${32 - hw}" width="${bw * 0.38}" height="${hw}" fill="${COLORS.rose}"><title>${new Date(x.day * 86400000).toISOString().slice(0, 10)} written ${fmtGB(x.written_gb)}</title></rect>`;
    }).join('') + `</svg><div class="muted" style="font-size:10px">per day, last ${n} day${n === 1 ? '' : 's'} recorded (read teal, written rose)</div>` : '';
  host.innerHTML = `<div>${escH(d.disk)} · <span style="color:${tcol || 'var(--text)'};font-size:16px;font-weight:700">${t != null ? t.toFixed(0) + ' °C' : '–'}</span>` +
    (warn ? ` <span class="muted">(warn ${warn.toFixed(0)} · crit ${(crit || 0).toFixed(0)} °C)</span>` : '') + `</div>` +
    `<table class="mono" style="width:100%;margin-top:4px"><tr><th></th><th style="text-align:right">read</th><th style="text-align:right">written</th></tr>` +
    row(d.hour.span_s >= 3500 ? 'last hour' : `last ${Math.round(d.hour.span_s / 60)} min`, d.hour.read_gb, d.hour.written_gb) +
    row('today', td.read_gb, td.written_gb) + row('7 days', wk.r, wk.w) +
    (d.since_boot ? row('since boot', d.since_boot.read_gb, d.since_boot.written_gb) : '') + `</table>` + bars;
}

// SSD cold-expert set + memory reserve (/api/coldset): what the serving recipe's permanent
// route record and boot-time cold-set rebuild left on disk, and the reserve cgroup's use.
const fmtGiB = b => b == null ? '–' : (b / 2 ** 30).toFixed(b < 10 * 2 ** 30 ? 2 : 1) + ' GiB';
const fmtAgo = s => s == null ? '' : s < 120 ? `${s} s ago` : s < 7200 ? `${Math.round(s / 60)} min ago`
  : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
async function renderColdset() {
  const card = document.getElementById('cold-usage');
  const host = card && card.querySelector('.cold-body');
  if (!host) return;
  let d;
  try { d = await (await fetch('/api/coldset')).json(); } catch (e) { return; }
  card.style.display = d && d.available && sideSettings.cold_usage !== false ? '' : 'none';
  if (!d || !d.available) return;
  const row = (l, v, t) => `<tr${t ? ` title="${escH(t)}"` : ''}><td>${l}</td><td style="text-align:right">${v}</td></tr>`;
  let h = '<table class="mono" style="width:100%">';
  for (const r of d.records || []) {
    const sh = r.recent_cold_share;
    h += `<tr><th colspan="2" style="text-align:left">${escH(r.name.replace(/^route-heat-/, ''))}${r.paused ? ' · <span style="color:var(--amber, #f59e0b)">paused</span>' : ''}</th></tr>` +
      row('routes to SSD', sh == null ? '–' : `<b style="font-size:14px">${(100 * sh).toFixed(2)} %</b>`,
          `share of expert routes that went to SSD experts over the last ${r.recent_routes ?? 0} routes`) +
      row('route record', `${((r.routes || 0) / 1e6).toFixed(1)} M · ${r.boots} boots`,
          `${fmtGiB(r.record_bytes)} on disk (fixed size); saved ${fmtAgo(r.age_s)}`) +
      row('record size', r.record_bytes == null ? '–' : (r.record_bytes / 1024).toFixed(0) + ' KiB');
  }
  const a = d.auto_cold;
  if (a) {
    const what = { rebuilt: 'rebuilt', kept: 'kept', 'no-disk-space': 'kept (no disk space)', 'no-record': 'kept (no record yet)' }[a.decision] || a.decision;
    h += row('cold set at boot', `${what} · ${fmtAgo(a.age_s)}`,
      a.gain != null ? `cold experts touched per step ${a.touched_now} now vs ${a.touched_new} re-picked (${(100 * a.gain).toFixed(1)} %, threshold ${(100 * a.min_gain).toFixed(0)} %); ${a.gib} GiB on SSD` : '');
  }
  const r = d.reserve;
  if (r) {
    const pct = r.max_bytes ? Math.round(100 * r.current_bytes / r.max_bytes) : null;
    h += `<tr><th colspan="2" style="text-align:left">reserve (${escH(r.cgroup)})</th></tr>` +
      row('in use', `${(r.current_bytes / 2 ** 30).toFixed(2)}${r.max_bytes ? ` / ${fmtGiB(r.max_bytes)}` : ' GiB'}${pct != null ? ` (${pct} %)` : ''}`,
          (r.tenants || []).join(', ')) +
      row('highest seen', fmtGiB(r.peak_ever_bytes),
          r.watched_since ? `watched since ${new Date(r.watched_since * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC` : '');
  }
  host.innerHTML = h + '</table>';
}

async function renderLostPotential() {
  const host = document.getElementById('bub-potential');
  if (!host) return;
  const grp = host.closest('.bub-group');
  if (sideSettings.bubbles_potential === false) { if (grp) grp.style.display = 'none'; return; }
  let d = null;
  try { d = await (await fetch('/api/lost-potential?engine=' + encodeURIComponent(eng()))).json(); } catch (e) { return; }
  if (!d || !d.ok) { if (grp) grp.style.display = 'none'; host.innerHTML = ''; return; }
  if (grp) grp.style.display = '';
  host.title = 'Measured best hour/day/month/year only cover periods LIMCARS actually watched; ' +
    'month/year figures are "at this rate" projections, not measurements. Prices come from data/pricing.json.';
  const pct = d.pct_usage == null ? '—' : d.pct_usage + '%';
  // User-approved wording: lead with the yearly LOSS (best-hour ceiling x
  // 8760 minus the year at recorded pace), then demonstrated peak rate,
  // then CAPACITY projections (best hour scaled to month/year — the old
  // dash's "600M tokens a day" basis), then what was actually used.
  // title says the alert; body = one sentence per row (user layout request)
  // title = warning + the money hook (user: eye-catching, bigger $ figure);
  // the rest are plain sentences, one per row
  const sent = [];
  let titleHtml = '⚠️ Attention! Lost potential detected.';
  if (d.loss_year_cost != null && d.loss_year_cost > 0) {
    titleHtml += ' — Your token usage rate makes you leave on the table ' +
      '<span class="pot-loss">' + fmtUsd0(d.loss_year_cost) + ' USD</span> a year!';
  }
  sent.push(escH(d.model) + ' was recorded doing up to ' +
    fmtTokShort(d.peak_hour_tokens) + ' tokens an hour but historically you ' +
    'use barely ' + pct + ' of that across the ' + d.recorded_days +
    ' recorded days.');
  sent.push('This model is projected to do ' +
    fmtTokShort(d.proj_month_tokens) + ' tokens/month or ' +
    fmtTokShort(d.proj_year_tokens) + '/year.  \u2014  Over the ' +
    d.recorded_days + ' recorded days you did just ' +
    fmtTokShort(d.total_tokens) + ' tokens.');
  if (d.best_hour_cost != null) {
    // measured only for windows we actually watched; anything longer is a
    // pace-based projection, labeled as such (user rule: never claim a
    // "best month" the dashboard has not seen a month of)
    const parts = ['best hour ' + fmtUsd0(d.best_hour_cost)];
    if (d.best_day_cost != null) parts.push('best day ' + fmtUsd0(d.best_day_cost));
    else if (d.proj_day_cost != null) parts.push('at this rate ~' + fmtUsd0(d.proj_day_cost) + '/day');
    if (d.best_month_cost != null) parts.push('best month ' + fmtUsd0(d.best_month_cost) + ' (measured)');
    else if (d.proj_month_cost != null) parts.push('at this rate ~' + fmtUsd0(d.proj_month_cost) + '/month');
    if (d.best_year_cost != null) parts.push('best year ' + fmtUsd0(d.best_year_cost) + ' (measured)');
    else if (d.proj_year_cost != null) parts.push('at this rate ~' + fmtUsd0(d.proj_year_cost) + '/year');
    sent.push('Historic savings registered: ' + parts.join(', ') +
      ' — so put this baby to work!');
  }
  const msg = sent.join('<br>');
  const pr = state.pricing;
  const title = 'avg recorded hour ' + fmtTokShort(d.avg_hour_tokens) +
    ' tokens · total ' + fmtTokShort(d.total_tokens) + ' over ' +
    d.recorded_days + ' days' +
    (pr && pr.fetched_at ? ' \u00b7 all $ figures priced live at ' +
      (pr.provider || pr.display_name || 'API') + ' rates (' +
      String(pr.fetched_at).slice(0, 10) + '); change pricing.json and ' +
      'every $ updates' : '') +
    ' \u00b7 estimated/seed rows are excluded' +
    // the headline number, spelled out so it can be checked by hand:
    // lost potential = capacity (best recorded hour x 8760 h) - registered saved
    (d.capacity_year_cost != null ? ' \u00b7 on the table = capacity ' +
      fmtUsd0(d.capacity_year_cost) + '/yr (best recorded hour \u00d7 8760 h) \u2212 ' +
      'money the dash registered as saved ' + fmtUsd0(d.saved_year_cost) + '/yr' : '') +
    (d.duty_hours_observed_per_day != null ? ' \u00b7 ' +
      d.duty_hours_observed_per_day + ' active h/day measured' : '');
  host.innerHTML = '<div class="bubble potential" title="' + escH(title) +
    '"><div class="label">' + titleHtml + '</div><div class="value pot-text">' +
    msg + '</div></div>';
}
renderSavingsChart(); setInterval(renderSavingsChart, 60000);
renderLostPotential(); setInterval(renderLostPotential, 60000);
renderSsd(); setInterval(renderSsd, 60000);
setInterval(renderColdset, 60000);
// debug handle (module scope is otherwise unreachable from the console)
window.__pot = {render: renderLostPotential, ss: () => sideSettings};
renderCounters(); setInterval(renderCounters, 30000);
renderInfo(); setInterval(renderInfo, 60000);
setInterval(renderSessionTokens, 15000);
setInterval(renderClientChart, 10000);
setInterval(fetchCacheLife, 60000);

document.querySelectorAll('[data-win]').forEach(btn => btn.addEventListener('click', async () => {
  state.windowSec = +btn.dataset.win;
  document.querySelectorAll('[data-win]').forEach(b => {
    b.classList.toggle('on', b === btn); b.setAttribute('aria-pressed', String(b === btn)); });
  const label = { 180: '3 min', 900: '15 min', 3600: '1 h' }[state.windowSec];
  document.querySelector('#win-label').textContent = 'last ' + label;
  document.querySelectorAll('#charts-grid .w').forEach(w => w.textContent = label);
  await seed(false);
}));

document.querySelector('#btn-refresh').addEventListener('click', () => seed(false));

// ---------------- price manager: every model LIMCARS has recorded, editable (data/pricing.json) ----------------

let pmData = null;
const pmF = v => v == null || v === '' ? '–' : '$' + (+v).toString();
async function pmLoad() {
  try { pmData = await (await fetch('/api/pricing-models')).json(); } catch (e) { return; }
  const models = pmData.models || {};
  const used = {};
  (pmData.seen || []).forEach(s => { if (s.slug) (used[s.slug] = used[s.slug] || []).push(s.model); });
  const rows = Object.entries(models).map(([slug, e]) => {
    const pk = e.peak;
    const peak = pk ? `peak ${pmF(pk.input_per_M)}/${pmF(pk.output_per_M)} · ${(pk.hours_utc || []).map(w => w.join('–')).join(', ')} UTC` +
      ((pk.offpeak_utc || []).length ? ` · off-peak ${(pk.offpeak_utc).map(w => w.join(' – ')).join(', ')}` : '') : '';
    return `<tr><td>${escH(e.display_name || slug)}<div class="muted">${escH(slug)}${(used[slug] || []).length ? ' · serves ' + escH(used[slug].join(', ')) : ''}</div></td>` +
      `<td>${pmF(e.input_per_M)}</td><td>${pmF(e.cache_write_per_M)}</td><td>${pmF(e.cache_read_per_M)}</td><td>${pmF(e.output_per_M)}</td>` +
      `<td class="muted">${escH(peak)}</td><td><button class="pill tiny" data-pm-edit="${escH(slug)}">edit</button></td></tr>`;
  });
  const unpriced = (pmData.seen || []).filter(s => !s.slug || s.slug === 'default').map(s =>
    `<tr><td>${escH(s.model)}<div class="muted">no price of its own · using the fallback rates</div></td><td colspan="5"></td>` +
    `<td><button class="pill tiny" data-pm-new="${escH(s.model)}">add price</button></td></tr>`);
  document.querySelector('#pm-tbl').innerHTML =
    `<tr><th>model</th><th>input</th><th>cache write</th><th>cache read</th><th>output</th><th>peak</th><th></th></tr>` +
    unpriced.join('') + rows.join('') +
    `<tr><td colspan="7"><button class="pill tiny" data-pm-new="">+ add a model</button></td></tr>`;
}
const pmWin = (s, week) => String(s || '').split(',').map(x => x.trim()).filter(Boolean).map(x => x.split(/\s*[-–]\s*/));
function pmOpen(slug, alias) {
  const e = (pmData.models || {})[slug] || {};
  const pk = e.peak || null;
  const set = (id, v) => { document.querySelector(id).value = v == null ? '' : v; };
  set('#pm-slug', slug || (alias ? alias.toLowerCase().replace(/[^a-z0-9._/-]+/g, '-') : ''));
  document.querySelector('#pm-slug').readOnly = !!slug;
  set('#pm-name', e.display_name || alias || '');
  set('#pm-alias', (e.aliases || (alias ? [alias] : [])).join(', '));
  set('#pm-in', e.input_per_M); set('#pm-w', e.cache_write_per_M); set('#pm-r', e.cache_read_per_M); set('#pm-out', e.output_per_M);
  document.querySelector('#pm-peak-on').checked = !!pk;
  document.querySelector('#pm-peak').style.display = pk ? 'block' : 'none';
  set('#pm-pin', pk?.input_per_M); set('#pm-pw', pk?.cache_write_per_M); set('#pm-pr', pk?.cache_read_per_M); set('#pm-pout', pk?.output_per_M);
  set('#pm-hours', (pk?.hours_utc || []).map(w => w.join('-')).join(', '));
  set('#pm-off', (pk?.offpeak_utc || []).map(w => w.join('-')).join(', '));
  document.querySelector('#pm-del').hidden = !slug;
  document.querySelector('#pm-msg').textContent = '';
  document.querySelector('#pm-form').style.display = 'block';
}
async function pmSave(del) {
  const v = id => document.querySelector(id).value.trim();
  const n = id => v(id) === '' ? null : +v(id);
  const slug = v('#pm-slug');
  const entry = del ? null : {
    display_name: v('#pm-name') || slug,
    aliases: v('#pm-alias').split(',').map(x => x.trim()).filter(Boolean),
    input_per_M: n('#pm-in'), cache_write_per_M: n('#pm-w'), cache_read_per_M: n('#pm-r'), output_per_M: n('#pm-out'),
    peak: document.querySelector('#pm-peak-on').checked ? {
      input_per_M: n('#pm-pin'), cache_write_per_M: n('#pm-pw'), cache_read_per_M: n('#pm-pr'), output_per_M: n('#pm-pout'),
      hours_utc: pmWin(v('#pm-hours')), offpeak_utc: pmWin(v('#pm-off')) } : null };
  let r;
  try { r = await (await authFetch('/api/pricing-models', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slug, entry }) },
    '🔒 Editing prices is password-protected')).json(); }
  catch (e) { r = { ok: false, error: String(e) }; }
  const msg = document.querySelector('#pm-msg');
  if (!r.ok) { msg.textContent = 'Save failed: ' + (r.error || '?'); msg.className = 'small pr-err'; return; }
  document.querySelector('#pm-form').style.display = 'none';
  toast(del ? 'Price removed ✓' : 'Price saved ✓ (new minutes use it; recorded minutes keep their price)', 'ok');
  await pmLoad(); seed(false).catch(() => {});
}
document.querySelector('#btn-price-mgr').addEventListener('click', () => {
  const p = document.querySelector('#price-mgr');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
  if (p.style.display === 'block') pmLoad();
});
document.querySelector('#pm-tbl').addEventListener('click', e => {
  const ed = e.target.closest('[data-pm-edit]'), nw = e.target.closest('[data-pm-new]');
  if (ed) pmOpen(ed.dataset.pmEdit); else if (nw) pmOpen('', nw.dataset.pmNew);
});
document.querySelector('#pm-peak-on').addEventListener('change', e => {
  document.querySelector('#pm-peak').style.display = e.target.checked ? 'block' : 'none'; });
document.querySelector('#pm-save').addEventListener('click', () => pmSave(false));
document.querySelector('#pm-del').addEventListener('click', () => pmSave(true));
document.querySelector('#pm-cancel').addEventListener('click', () => { document.querySelector('#pm-form').style.display = 'none'; });

document.querySelector('#btn-probe').addEventListener('click', async () => {
  const out = document.querySelector('#probe-out');
  out.innerHTML = '⚡ probing… <span class="muted">streaming a ~1.3k-token request</span>';
  try {
    const r = await (await fetch('/api/probe', { method: 'POST' })).json();
    out.innerHTML = r.ok
      ? `⚡ probe: <b>TTFT ${Math.round(r.ttft_ms)} ms</b> · PP <b>${r.pp_tps != null ? Math.round(r.pp_tps).toLocaleString('en-US') : '–'} tok/s</b> · TG <b>${r.tg_tps != null ? Math.round(r.tg_tps) : '–'} tok/s</b><br><span class="muted">prompt ${r.prompt_tokens} tok · completion ${r.completion_tokens} tok · ${new Date(r.at * 1000).toTimeString().slice(0, 8)}</span>`
      : `⚡ probe failed: ${escH(String(r.error))}`;
  } catch (e) { out.innerHTML = `⚡ probe error: ${escH(String(e))}`; }
});

// ---------------- engine switcher ----------------

function selectEngine(id) {
  const prev = eng();
  if (!state.engines[id] || id === prev) return;
  state.sel = id;
  state.selExplicit = true;   // clicked, not remembered — see eng()
  uiPrefs = writeUiPrefs({ engine: id });
  const u = new URL(location.href);
  u.searchParams.set('engine', id);
  history.replaceState(null, '', u);
  for (const card of state.charts) {
    if (card._fixedWin || !card._series) continue;
    for (const s of card._series)
      if (s.key.startsWith(prev + '.')) s.key = id + s.key.slice(prev.length);
    card._chart && card._chart.mark();
  }
  state._held = {}; state._heldNz = {}; state._cacheHitLife = [];
  fetchCacheLife();
  seed(false).catch(() => {});
  refresh();
  renderModelTable(true);
}

function wireEngineSwitcher() {
  const btn = document.querySelector('#btn-engine');
  const pop = document.querySelector('#engine-pop');
  const close = () => { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  const open = () => {
    const cur = eng();
    pop.innerHTML = Object.entries(state.engines).map(([id, { meta = {} }]) =>
      `<button role="option" data-eng="${escH(id)}" aria-selected="${id === cur}">` +
      `<span class="dot ${meta.alive ? 'up' : 'down'}"></span>` +
      `<span class="mono">${escH(meta.model || id)}</span>` +
      `<span class="eng-url">${escH(meta.url || '')}</span></button>`).join('');
    const r = btn.getBoundingClientRect();
    pop.style.top = (r.bottom + 6) + 'px';
    pop.style.left = Math.max(8, Math.min(r.left, innerWidth - 280)) + 'px';
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    fetch('/api/stat-models?engine=' + encodeURIComponent(cur)).then(r => r.json()).then(d => {
      const past = (d.models || []).filter(m => m.model !== d.current);
      if (pop.hidden || !past.length) return;
      const pick = state.statModel[cur];
      const day = t => t ? new Date(t * 1000).toISOString().slice(0, 10) : '?';
      pop.insertAdjacentHTML('beforeend', `<div class="eng-sec muted small">Token stats for</div>` +
        [{ model: '', key: '' }, ...past].map(m => {
          const v = m.key === '' ? '' : (m.model || '-');
          const name = m.key === '' ? (d.current || 'current model') + ' (now)'
            : m.model ? m.model : 'before model split';
          return `<button role="option" data-stat="${escH(v)}" aria-selected="${(pick || '') === v}">` +
            `<span class="mono">${escH(name)}</span>` +
            (m.key ? `<span class="eng-url">${day(m.first)} → ${day(m.last)}</span>` : '') + `</button>`;
        }).join(''));
    }).catch(() => {});
    (pop.querySelector('[aria-selected="true"]') || pop.querySelector('button'))?.focus();
  };
  btn.addEventListener('click', e => {
    e.stopPropagation();
    pop.hidden ? open() : close();
  });
  pop.addEventListener('click', e => {
    e.stopPropagation();
    const st = e.target.closest('button[data-stat]');
    if (st) {
      close(); btn.focus();
      if (st.dataset.stat) state.statModel[eng()] = st.dataset.stat; else delete state.statModel[eng()];
      renderCounters(); refresh();
      return;
    }
    const b = e.target.closest('button[data-eng]');
    if (!b) return;
    close(); btn.focus();
    selectEngine(b.dataset.eng);
  });
  pop.addEventListener('keydown', e => {
    const items = [...pop.querySelectorAll('button')];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
    } else if (e.key === 'Escape') { close(); btn.focus(); }
  });
  document.addEventListener('click', close);
}
wireEngineSwitcher();

// ---------------- cluster node picker (agentless SSH nodes, config `nodes`) ----------------

function renderNodePicker() {
  const host = document.querySelector('#node-pick'), err = document.querySelector('#node-err');
  if (!host) return;
  const ids = Object.keys(state.nodes || {});
  host.hidden = !ids.length;
  const sel = hwNode() ? state.nodeSel : null;
  const sig = sel + '|' + ids.map(id => id + (state.nodes[id].alive ? '+' : '-')).join(',');
  if (ids.length && host._sig !== sig) {
    host._sig = sig;
    host.innerHTML = [[null, 'this box', true], ...ids.map(id => [id, id, state.nodes[id].alive])]
      .map(([id, label, up]) =>
        `<button class="pill tiny${id === sel ? ' on' : ''}" data-node="${escH(id ?? '')}" aria-pressed="${id === sel}">` +
        `<span class="dot ${up ? 'up' : 'down'}"></span>${escH(label)}</button>`).join('');
  }
  const n = hwNode();
  err.hidden = !(n && !n.alive);
  if (n && !n.alive) err.textContent = `⚠ ${sel} unreachable over SSH — ${n.error || 'no response yet'}`;
}
document.querySelector('#node-pick').addEventListener('click', e => {
  const b = e.target.closest('button[data-node]');
  if (!b) return;
  state.nodeSel = b.dataset.node || null;
  uiPrefs = writeUiPrefs({ node: state.nodeSel });
  refresh();
});

// ---------------- boot ----------------

(async () => {
  state.sel = new URLSearchParams(location.search).get('engine') || uiPrefs.engine || null;
  state.nodeSel = uiPrefs.node || null;
  try {
    await seed();
  } catch (e) {
    // audit: a server-restart race at tab-load aborted boot permanently
    // ('loading…' forever — no SSE, no refresh loop). Reload and try again.
    setTimeout(() => location.reload(), 3000);
    return;
  }
  fetchCacheLife();  // after seed so engine proc_start is known
  connect();
  (function loop() {
    refresh();
    setTimeout(loop, Math.max(0.5, sideSettings.refresh_s ?? 0.5) * 1000);
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
        : r.last_error ? `<span class="rec-err" title="${escH(r.last_error)}">⚠ ${escH(r.last_error)}</span>`
        : r.last_note ? escH(r.last_note) : 'stopped'}</div>
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
    toast('⚠ ' + (res.error || act + ' failed'), 'err');
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
    if (res.ok === false) { toast('⚠ ' + (res.error || 'delete failed'), 'err'); return; }
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
    if (!(await confirmDialog(`Stop ${label} (pid ${pid})?\nThis SIGTERMs → SIGKILLs its whole process group.\nA containerised engine is stopped with \u2018docker stop\u2019 so it can release the GPU cleanly.`, '■ Stop'))) return;
    b.disabled = true; b.textContent = 'stopping…';
    const res = await authFetch(`/api/engines-live/${pid}/stop`, { method: 'POST' },
                                '🔒 Stopping an engine is password-protected')
      .then(r => r.json()).catch(err => ({ ok: false, error: String(err) }));
    if (res.ok === false) { b.disabled = false; b.textContent = '■ Stop'; toast('⚠ ' + (res.error || 'stop failed'), 'err'); }
    else if (res.stopped_container) toast('■ stopped ' + res.stopped_container + ' (docker stop)');
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

async function authFetch(url, opts = {}, label = '🔒 This action is password-protected') {
  opts.headers = Object.assign({}, opts.headers || {});
  if (state.pw) opts.headers['X-Limcars-Pw'] = state.pw;
  let r = await fetch(url, opts);
  if (r.status === 401) {
    const v = await pwDialog(label, [{ label: 'Password' }]);
    const p = v && v[0];
    if (p) {
      state.pw = p; sessionStorage.setItem('lc-pw', p);
      opts.headers['X-Limcars-Pw'] = p;
      r = await fetch(url, opts);
    }
  }
  if (r.status === 423) {
    let s = 60; try { s = Math.ceil((await r.clone().json()).retry_after || 60); } catch (e) {}
    toast(`⏳ Too many wrong passwords — locked for ~${s} s. Try again after that.`, 'err');
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
  if (nw && rep !== nw) return toast('Passwords do not match.', 'err');
  const r = await fetch('/api/auth/set', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current: cur, new: nw }) }).then(r => r.json())
    .catch(e => ({ ok: false, error: String(e) }));
  if (r.ok) {
    state.pw = nw || ''; sessionStorage.setItem('lc-pw', state.pw); renderAuth();
    toast(nw ? 'Password saved ✓' : 'Password protection removed', 'ok');
  }
  else toast(r.error === 'locked' ? `⏳ Locked — retry in ${Math.ceil(r.retry_after || 60)} s`
        : 'Failed: ' + (r.error || '?'), 'err');
});

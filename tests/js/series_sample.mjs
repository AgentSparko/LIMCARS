// Regression: the bubble sparklines must not contradict the number above them.
//
// Owner's report (video): "the small charts ... dance, the ones on the first line the most,
// like at 1 fps". Measured live, the printed values were STATIC while the curves moved:
// the engine reports kv_pct = 0 and decode_tps_now = 0 between bursts, the card layer holds
// the last real reading for the number ("14.8% · held, cache resident") but the SERIES was
// fed the raw 2 Hz value — so the last point fell to 0 and back at the poll rate, and since
// spark()'s min is pinned at 0 that swing is the full 24 px.
//
// Run: node tests/js/series_sample.mjs   (also driven by tests/test_web_charts.py)

import { seriesSample, RATE_WHILE_ACTIVE, HOLD_LAST_NONZERO, spark } from '../../web/js/charts.js';

let fails = 0;
const eq = (label, got, want) => {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { fails++; console.log(`FAIL  ${label}\n        got  ${g}\n        want ${w}`); }
  else console.log(`ok    ${label}  ->  ${g}`);
};

// --- 1. kv_pct: the false idle zero must not drag the curve down ---------------
const held = {};
eq('kv_pct records a real reading', seriesSample('kv_pct', 14.7541, { kv_pct: 14.7541 }, held), 14.7541);
eq('kv_pct holds it when the engine reports its idle 0',
   seriesSample('kv_pct', 0, { kv_pct: 0 }, held), 14.7541);
eq('...and still holds on the next idle frame',
   seriesSample('kv_pct', 0, { kv_pct: 0 }, held), 14.7541);
eq('a fresh reading replaces the held one',
   seriesSample('kv_pct', 22.5, { kv_pct: 22.5 }, held), 22.5);

// --- 2. no reading, no invention ------------------------------------------------
eq('kv_pct with no history keeps the honest 0',
   seriesSample('kv_pct', 0, {}, {}), 0);

// --- 3. rate fields: not measurable is a gap, not a measured zero ---------------
eq('decode_tps_now while not decoding -> null',
   seriesSample('decode_tps_now', 0, { decode_active: false }, {}), null);
eq('decode_tps_now while decoding -> the value',
   seriesSample('decode_tps_now', 72.2, { decode_active: true }, {}), 72.2);
eq('decode_tps_avg follows the same sticky flag',
   seriesSample('decode_tps_avg', 0, { decode_active: false }, {}), null);
eq('prefill fields key off prefill_active, not decode_active',
   seriesSample('prefill_tps_avg', 0, { prefill_active: false, decode_active: true }, {}), null);
eq('prefill passes through while prefilling',
   seriesSample('prefill_tps_avg', 1950.4, { prefill_active: true }, {}), 1950.4);
// the flag must say false: a missing flag means "engine does not report it", and
// inventing gaps for an engine that never sends the flag would blank the chart
eq('a frame without the flag is left alone (older/other engines)',
   seriesSample('decode_tps_now', 0, {}, {}), 0);
eq('undefined flag (engine never sends it) is left alone',
   seriesSample('decode_tps_now', 5.5, { decode_active: undefined }, {}), 5.5);

// --- 4. nothing else is touched -------------------------------------------------
for (const f of ['running', 'waiting', 'ttft_p50_ms', 'e2e_p50_ms', 'gen_peak', 'spec_accept_len'])
  eq(`${f} passes through, zeros included`, seriesSample(f, 0, { decode_active: false }, {}), 0);
eq('genuine measured 0 on a hold field is never held before a real reading',
   seriesSample('running', 0, {}, { running: 7 }), 0);   // 'running' is not a hold field
eq('a real zero is not swallowed by the policy tables',
   HOLD_LAST_NONZERO.has('running'), false);

// --- 5. held values are per engine, never shared --------------------------------
const e1 = {}, e2 = {};
seriesSample('kv_pct', 11.0, {}, e1);
eq('engine 2 has no idea what engine 1 saw', seriesSample('kv_pct', 0, {}, e2), 0);
eq('engine 1 still holds its own', seriesSample('kv_pct', 0, {}, e1), 11.0);
eq('policy tables stay small and explicit',
   [Object.keys(RATE_WHILE_ACTIVE).sort(), [...HOLD_LAST_NONZERO].sort()],
   [['decode_tps_avg', 'decode_tps_now', 'prefill_tps_avg', 'prefill_tps_now'], ['kv_pct']]);

// --- 6. the renderer: a gap must not become a plunge ----------------------------
// spark() draws only finite samples, so a null leaves the shape (and the y range)
// exactly as it was instead of dropping a spike to the floor.
const stub = { clientWidth: 150, _attrs: {}, setAttribute(k, v) { this._attrs[k] = v; } };
spark(stub, [14.75, 14.75, 14.75, 14.75]);
const clean = stub._spark;
spark(stub, [14.75, 14.75, 14.75, null]);
const gapped = stub._spark;
eq('null is skipped, not drawn at the floor', gapped.idxs, [0, 1, 2]);
eq('so the y range does not move', [gapped.min, gapped.max], [clean.min, clean.max]);
eq('and the curve does not change shape', gapped.n, 4);
// what the OLD code did with the raw idle zero, for the record:
spark(stub, [14.75, 14.75, 14.75, 0]);
eq('a raw 0 WOULD have stretched the scale (this was the dance)', [stub._spark.min, stub._spark.max], [0, 14.75]);

// --- 6b. gaps render BLANK, never as straight connectors ------------------------
// The gap policy (#28/#33) made idle stretches null; the renderer still
// filtered them and drew the kept samples as ONE path, stitching long
// straight diagonals across the invisible terrain (owner screenshot:
// "straight lines that look horrible"). Each finite run must be its own
// subpath: stroke M + fill M per run.
spark(stub, [10, 20, 30]);
eq('a solid series = one polyline (1 stroke M + 1 fill M)', (stub.innerHTML.match(/M/g) || []).length, 2);
spark(stub, [10, null, null, 20]);
eq('a gapped series = one polyline PER run (2+2)', (stub.innerHTML.match(/M/g) || []).length, 4);
spark(stub, [10, null, 20, null, 30]);
eq('three runs -> three breaks', (stub.innerHTML.match(/M/g) || []).length, 6);
// CAP thinning skips indices too — it must NOT read as a gap:
spark(stub, Array.from({ length: 300 }, (_, i) => i));
eq('thinning is not a gap', (stub.innerHTML.match(/M/g) || []).length, 2);

// --- 7. smoothing the RECORDED idle zeros (display only, never invents) ---------
import { fillIdleZeros } from '../../web/js/charts.js';

const T = 1_000_000;
const src = [[T, 14.75], [T + 1, 0], [T + 2, 0], [T + 3, 22], [T + 4, 0]];
const nz = fillIdleZeros(src);
eq('interior idle zeros take the last real reading', nz.map(r => r[1]), [14.75, 14.75, 14.75, 22, 22]);
eq('timestamps are never disturbed', nz.map(r => r[0]), [T, T + 1, T + 2, T + 3, T + 4]);
eq('the input rows are NOT mutated — the ring keeps what the engine said',
   src.map(r => r[1]), [14.75, 0, 0, 22, 0]);
eq('and it returns a new array, not the same object', nz === src, false);
eq('leading zeros are left alone — nothing to borrow from',
   fillIdleZeros([[T, 0], [T + 1, 0], [T + 2, 5]]).map(r => r[1]), [0, 0, 5]);
eq('nulls are gaps, not zeros: untouched',
   fillIdleZeros([[T, 9], [T + 1, null], [T + 2, 0]]).map(r => r[1]), [9, null, 9]);
eq('a series of all zeros is reported as it was recorded',
   fillIdleZeros([[T, 0], [T + 1, 0]]).map(r => r[1]), [0, 0]);
eq('garbage in is passed through, not crashed on', fillIdleZeros(undefined), undefined);
// The accepted blind spot, written down as a test so nobody "fixes" it silently:
// a genuine "the cache really emptied" looks identical to an idle false zero in
// recorded data, so it gets smoothed too. Display-only; the stored row is intact.
eq('known trade-off: a real empty-cache moment is smoothed as well',
   fillIdleZeros([[T, 30], [T + 1, 0]]).map(r => r[1]), [30, 30]);

// ---- rateZerosToGaps: the SEEDED side of the #28 policy ----------------
// The live push gates rates with the frame's sticky flags; ring rows carry no
// flags, so a seeded 0 on a rate field is "not measurable" and must become a
// gap. Otherwise page-load replays the exact square wave #28 removed.
import { rateZerosToGaps, RATE_FIELDS } from '../../web/js/charts.js';
eq('recorded rate 0 seeds as a gap, not a plunge',
   rateZerosToGaps([[T, 40.2], [T + 1, 0], [T + 2, 0], [T + 3, 38.1]]).map(r => r[1]),
   [40.2, null, null, 38.1]);
eq('a genuine mid-burst stall also reads as a gap on reload (accepted: subtract, never invent)',
   rateZerosToGaps([[T, 60], [T + 1, 0]]).map(r => r[1]), [60, null]);
eq('nulls already recorded stay null',
   rateZerosToGaps([[T, null], [T + 1, 0]]).map(r => r[1]), [null, null]);
eq('timestamps preserved',
   rateZerosToGaps([[T, 0]])[0][0], T);
eq('garbage in is passed through, not crashed on', rateZerosToGaps(undefined), undefined);
eq('RATE_FIELDS matches exactly what seriesSample gates',
   [...RATE_FIELDS].sort().join(','),
   'decode_tps_avg,decode_tps_now,prefill_tps_avg,prefill_tps_now');

console.log(fails ? `\n${fails} check(s) FAILED` : '\nall checks passed');
process.exit(fails ? 1 : 0);

// Regression test for the "info-bubble charts dance" bug (2026-10-28).
//
// spark() used to emit one path point per sample. The lifetime and savings bubbles pass their
// raw arrays straight in, so the polyline grew for as long as the page stayed open (~1 sample
// every 6 s; measured 191 and 323 points into a 271 px box). Because x(i) divides by n, every
// appended sample shifted ALL the others a sub-pixel left, and the min/max re-fit moved every y
// with it: the curve crawled until a reload reset the array.
//
// Run: node tests/js/spark_cap.mjs   (exit 0 = pass)
import { spark } from '../../web/js/charts.js';

// ---- the smallest DOM that charts.js will load against -------------------
const mkEl = (tag = 'div') => ({
  tag, style: {}, attrs: {}, children: [], _html: '',
  setAttribute(k, v) { this.attrs[k] = String(v); },
  getAttribute(k) { return this.attrs[k] ?? null; },
  addEventListener() {}, appendChild(c) { this.children.push(c); },
  querySelector() { return null; },
  getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: 30 }; },
  get innerHTML() { return this._html; },
  set innerHTML(v) { this._html = v; },
  classList: { add() {}, remove() {}, toggle() {} },
});
globalThis.document = {
  createElement: (t) => mkEl(t),
  createElementNS: (_ns, t) => mkEl(t),
  addEventListener() {},
  body: mkEl('body'),
};
globalThis.window = { addEventListener() {}, innerWidth: 1600 };

const mkSvg = (clientWidth) => Object.assign(mkEl('svg'), { clientWidth });
const segs = (svg) => {
  // the stroked path is the second <path> in the emitted markup
  const m = [...svg.innerHTML.matchAll(/<path d="([^"]+)"/g)];
  if (!m.length) return [];
  return m[m.length - 1][1].split(/[ML]/).filter(Boolean);
};
const circleCx = (svg) => {
  const m = svg.innerHTML.match(/<circle cx="([-\d.]+)"/);
  return m ? parseFloat(m[1]) : null;
};

let failed = 0;
const check = (name, cond, extra = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed++;
};

const W = 271, CAP = Math.floor(W / 1.5);          // 180 points in a 271 px bubble

// ---- 1. a long series is thinned to the pixel budget ----------------------
const long = Array.from({ length: 5000 }, (_, i) => 50 + 10 * Math.sin(i / 40));
const s1 = mkSvg(W);
spark(s1, long, '#123456');
const drawn = segs(s1).length;
check('long series is capped to the pixel budget', drawn > 0 && drawn <= CAP + 1,
      `drew ${drawn} points, cap ${CAP}, input 5000`);
check('_spark.idxs matches what was drawn', s1._spark.idxs.length === drawn,
      `idxs=${s1._spark.idxs.length} drawn=${drawn}`);

// ---- 2. the LAST sample is always drawn, and the span is preserved -------
check('the newest sample survives the thinning',
      s1._spark.idxs[s1._spark.idxs.length - 1] === long.length - 1,
      `last idx=${s1._spark.idxs[s1._spark.idxs.length - 1]}`);
check('the series still spans the whole width (x uses the ORIGINAL index)',
      circleCx(s1) !== null && Math.abs(circleCx(s1) - (W - 1)) < 0.6,
      `last dot cx=${circleCx(s1)}, expected ~${W - 1}`);

// ---- 3. the crawl is gone: n changing must not move the drawn points -----
// Before the fix, feeding one more sample shifted every coordinate. With the cap in
// place, a series that keeps growing keeps roughly the same number of drawn points.
const s2 = mkSvg(W);
spark(s2, Array.from({ length: 300 }, (_, i) => i % 97));
const before = segs(s2).length;
spark(s2, Array.from({ length: 4000 }, (_, i) => i % 97));
const after = segs(s2).length;
check('adding 3700 samples does not grow the polyline', Math.abs(after - before) < 40,
      `${before} -> ${after} points`);

// ---- 4. null gaps stay out of the path ----------------------------------
const gapped = Array.from({ length: 900 }, (_, i) => (i % 7 === 0 ? null : i));
const s3 = mkSvg(W);
spark(s3, gapped, '#abcdef');
check('null gaps never reach the markup', !/NaN|undefined/.test(s3.innerHTML),
      `path points=${segs(s3).length}`);
check('a hovered index is always one that was drawn',
      s3._spark.idxs.every(i => Number.isFinite(gapped[i])),
      `${s3._spark.idxs.length} drawn indices`);

// ---- 5. short series are untouched ---------------------------------------
const s4 = mkSvg(W);
spark(s4, [1, 2, 3, 4, 5], '#000');
check('a short series is drawn whole', segs(s4).length === 5, `drew ${segs(s4).length}`);
const s5 = mkSvg(0);                        // hidden bubble: clientWidth 0 -> fallback
spark(s5, Array.from({ length: 2000 }, (_, i) => i));
check('a hidden bubble (clientWidth 0) still renders and stays capped',
      segs(s5).length > 0 && segs(s5).length <= Math.floor(150 / 1.5) + 1,
      `drew ${segs(s5).length} of 2000 on the 150px fallback`);

console.log(failed ? `\n${failed} check(s) FAILED` : '\nall checks passed');
process.exit(failed ? 1 : 0);

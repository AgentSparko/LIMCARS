// Falsification for the KV tile honesty markers (drift study 2026-11-10).
//
// The live tile showed 66.9% KV while the honest unpinned reading was 84.3%:
// kv_ref_cmd pinned `--floor 10` (a 0.15.2-era knob), so every healthy
// recompute re-derived the OLD launch's pool. The tile printed a right number
// for the wrong launch — invisible unless the tile says so. These checks fire
// the planted stale-pin scenario and demand a visible mark, and demand that
// an honest row prints no warning (no decorative badge).
//
// Run: node tests/js/kv_honesty.mjs   (also driven by tests/test_kv_tile_honesty.py)

import { kvPinNote, stepMarks } from '../../web/js/charts.js';

let fails = 0;
const ok = (label, cond, got) => {
  if (!cond) { fails++; console.log(`FAIL  ${label}  got ${JSON.stringify(got)}`); }
  else console.log(`ok    ${label}`);
};

// --- planted stale-pin: the tile MUST be visibly marked ----------------------
const note = kvPinNote({ kv_ref_tokens: 836140, kv_ref_pin_gb: 10 });
ok('planted stale pin marks the tile', note.includes('pinned') && note.includes('10'), note);
ok('the mark names it as not the launch', note.includes('not the launch'), note);
ok('the mark carries the warning glyph', note.includes('⚠'), note);

// --- honest row: no pin, no mark ----------------------------------------------
ok('unpinned row prints no warning', kvPinNote({ kv_ref_tokens: 663179 }) === '',
   kvPinNote({ kv_ref_tokens: 663179 }));
ok('missing row prints nothing', kvPinNote(undefined) === '');
ok('null pin prints nothing', kvPinNote({ kv_ref_pin_gb: null }) === '');
// 0 GiB is falsy but IS a pin — the marker must not skip it
ok('pin at 0 GiB still marks', kvPinNote({ kv_ref_pin_gb: 0 }).includes('pinned'));

// --- step rings on the pool line ----------------------------------------------
const x = t => t * 2, y = v => v / 1000;
const marks = stepMarks([[1, null], [2, 836140], [3, null]], x, y, '#fb7185');
ok('planted step draws exactly one ring', (marks.match(/<circle/g) || []).length === 1, marks);
ok('ring sits at the step point', marks.includes('cx="4.0"') && marks.includes('cy="836.1"'), marks);
ok('no steps, no rings', stepMarks([[1, null], [2, null]], x, y, '#f00') === '');
ok('empty series draws nothing', stepMarks([], x, y, '#f00') === '');

console.log(fails ? `${fails} FAILURES` : 'all checks passed');
process.exit(fails ? 1 : 0);
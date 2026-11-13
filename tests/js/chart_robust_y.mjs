// A single short burst must not set the scale of the decode/prefill charts.
import test from 'node:test';
import assert from 'node:assert/strict';
import { robustTop } from '../../web/js/charts.js';

const series = (vals) => [vals.map((v, i) => [i, v])];

test('one spike is clipped to the 98th percentile', () => {
  const vals = Array.from({ length: 200 }, (_, i) => 120 + (i % 30));
  vals[100] = 1200;
  const top = robustTop(series(vals), 1200);
  assert.ok(top < 200 && top > 149, `top ${top}`);
});

test('steady data keeps its true max', () => {
  const vals = Array.from({ length: 200 }, (_, i) => 120 + (i % 30));
  assert.equal(robustTop(series(vals), 149), 149);
});

test('short windows and gaps fall back to the max', () => {
  assert.equal(robustTop(series([1, 2, 900]), 900), 900);
  const vals = Array.from({ length: 50 }, () => null);
  assert.equal(robustTop(series(vals), 5), 5);
});

// Falsification for the engine-table "Seen" cell (scrape-age feature).
// The cell used to print a wall-clock time — which reads identically whether
// the engine answered 2 s ago or froze an hour ago while rows kept flowing
// from the ring. seenLabel must render an AGE, tier it honestly, and print
// '–' (never '0 s ago') for an engine that never scraped.
//
// Run: node tests/js/scrape_age.mjs   (also driven by tests/test_scrape_age.py)

import { seenLabel } from '../../web/js/charts.js';

let fails = 0;
const ok = (label, cond, got) => {
  if (!cond) { fails++; console.log(`FAIL  ${label}  got ${JSON.stringify(got)}`); }
  else console.log(`ok    ${label}`);
};

ok('never scraped prints a dash, never a fake age', seenLabel(null) === '–', seenLabel(null));
ok('undefined prints a dash', seenLabel(undefined) === '–', seenLabel(undefined));
ok('NaN prints a dash', seenLabel(NaN) === '–', seenLabel(NaN));
ok('fresh scrape reads seconds', seenLabel(2.4) === '2 s ago', seenLabel(2.4));
ok('sub-second never goes negative', seenLabel(-5) === '0 s ago', seenLabel(-5));
ok('minutes tier at 90 s', seenLabel(120) === '2 min ago', seenLabel(120));
ok('hours tier past 90 min', seenLabel(7200) === '2.0 h ago', seenLabel(7200));
ok('boundary 89 s stays seconds', seenLabel(89) === '89 s ago', seenLabel(89));
ok('boundary 90 s tiers to minutes', seenLabel(90) === '2 min ago', seenLabel(90));
ok('boundary 5400 s tiers to hours', seenLabel(5400) === '1.5 h ago', seenLabel(5400));

if (fails) { console.log(`${fails} FAILURES`); process.exit(1); }
console.log('ALL PASS');
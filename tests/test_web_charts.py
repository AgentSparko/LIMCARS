"""The sparkline in an info bubble must not grow a point per poll.

Symptom the owner reported: the small charts in the bubbles "start dancing sometimes and it's
fixed by a page refresh". Measured on the live dashboard: the series handed to spark() grew by
one sample every ~6 s (191 points, and 323 on the prefix-cache lifetime bubble, in a 271 px
box), and because spark's x() divides by n, every appended sample shifted all the others a
sub-pixel left while the min/max re-fit moved every y — a crawl that a reload only hides,
because the reload rebuilds the array from the bounded server window.

node is preinstalled on the CI runners and on the box; where it is genuinely absent the test
skips rather than failing an unrelated install.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "js" / "spark_cap.mjs"
SCRIPT2 = ROOT / "tests" / "js" / "series_sample.mjs"
NODE = shutil.which("node")
HAS_NODE = NODE is not None
node_cmd = [NODE or "node", str(SCRIPT)]


def test_the_replay_script_exists():
    assert SCRIPT.exists(), "the JS regression script went missing"


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_sparkline_is_capped_to_the_pixel_budget():
    r = subprocess.run(node_cmd, capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, f"spark() cap regression failed:\n{out}"
    assert "all checks passed" in out, out


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_dashboard_imports_the_charts_bundle_it_needs():
    """The mechanism that gets a charts.js fix onto an operator's screen has to exist.

    (Whether the tag was actually bumped when charts.js changed is a review question, not
    something a test can assert without going stale the next time it is edited.)
    """
    js = (ROOT / "web" / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert "charts.js?v=" in js, "the charts import lost its cache-bust tag"
    tag = js.split("charts.js?v=", 1)[1].split("'", 1)[0]
    assert tag and tag != "undefined", f"empty cache-bust tag: {tag!r}"


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_series_feed_does_not_contradict_the_printed_number():
    """The 'dancing charts' fix lives in a pure function, so it is testable headlessly.

    Regression from the owner's video: bubbles whose number is held (kv_pct) or dimmed
    (rate fields while idle) were still fed the raw 2 Hz value, so the last point fell to
    0 at the poll rate — a full-height swing, since spark()'s floor is 0.
    """
    r = subprocess.run([NODE or "node", str(SCRIPT2)], capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, f"seriesSample regression failed:\n{out}"
    assert "all checks passed" in out, out


def test_the_feed_actually_routes_engine_fields_through_the_policy():
    """Mechanism, not intent: if the push site reverts to `o[k]`, the dance is back.

    A unit test on the pure function cannot notice the caller stop calling it.
    """
    js = (ROOT / "web" / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert "seriesSample(" in js, "the SSE feed no longer applies the series policy"
    assert "state.store.push(`${id_}.${k}`, o.t, o[k])" not in js, \
        "the raw 2 Hz value is being pushed again — that is the dancing-charts bug"

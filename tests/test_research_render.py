"""Research rendering options (2026-10-30, the xrsec-dp DP-pilot page): three optional per-figure fields a results
document may set, with the old behaviour when absent.
  decimals      bars/dot-bars value labels: an AUC difference of 0.00004 showed as "0.000" (always 3 decimals)
  points_label  dot-bars legend: "dots: test users" was hard-coded, wrong for per-seed points
  baseline      dot-bars: bars always grew from 0, so an AUC panel on a [0.45, 0.6] domain drew bars from 0.45 and
                a 0.535 bar looked like a large effect beside 0.495; with baseline 0.5 they grow from chance
Behavioural (node imports research.js and runs the helpers) plus a static check that the drawing code uses them."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "server" / "avalon_monitor" / "static"
SRC = (STATIC / "research.js").read_text()
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

JS = """
import { decOf, fvd, pointsLabel, baseOf } from %(research)s;
console.log(JSON.stringify({
  dec: [decOf({decimals: 5}), decOf({}), decOf({decimals: 9}), decOf({decimals: 2.5}), decOf({decimals: "5"})],
  fmt: [fvd(0.00004, 5), fvd(0.00004, null), fvd(0.5349, 2), fvd(null, 4), fvd(12.3456, null)],
  pts: [pointsLabel({points_label: "seeds (1@AVALON, 2@Miami)"}), pointsLabel({}), pointsLabel({points_label: "  "})],
  base: [baseOf({baseline: 0.5}), baseOf({}), baseOf({baseline: "0.5"})],
}));
"""


def _run():
    out = subprocess.run(["node", "--input-type=module", "-e", JS % {"research": json.dumps((STATIC / "research.js").as_uri())}],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@needs_node
def test_decimals_option():
    """Research options: decimals 0-6 (an integer) sets the precision; absent or invalid keeps 3."""
    r = _run()
    assert r["dec"] == [5, None, None, None, None]
    assert r["fmt"] == ["0.00004", "0.000", "0.53", "—", "12.35"]


@needs_node
def test_points_label_option():
    """Research options: the dots legend names what the dots are; absent or blank keeps "test users"."""
    assert _run()["pts"] == ["dots: seeds (1@AVALON, 2@Miami)", "dots: test users", "dots: test users"]


@needs_node
def test_baseline_option():
    """Research options: dot-bars grow from the baseline (a number only), else from 0."""
    assert _run()["base"] == [0.5, 0, 0]


def test_drawing_code_uses_the_options():
    """Research options (static, source-text): bars and dot-bars format through fvd(decOf(fig)), dot-bars grow from
    baseOf(fig) and label their dots with pointsLabel(fig); nothing hard-codes "dots: test users" or a 0 baseline."""
    assert SRC.count("decOf(fig)") >= 2
    assert "pointsLabel(fig)" in SRC and '"dots: test users"' not in SRC
    assert "const base = baseOf(fig)" in SRC and "Math.min(dom[1], base)" in SRC
    assert not re.search(r"Math\.min\(dom\[1\], 0\)\)\);", SRC)

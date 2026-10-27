"""Every text file opened in this repo must say what encoding it is.

Found the hard way: a test that read `web/js/dashboard.js` with a bare `read_text()` passed on
Linux (UTF-8 locale) and failed on the Windows CI runner with
`UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f` — Python falls back to the machine
locale there, and the dashboard's JS contains em dashes.

The fix is one keyword, so the class of bug is worth a gate rather than a review habit: this walks
the source files that ship and asserts none of them leaves text encoding to the platform. Binary
opens (`"rb"`/`"wb"`) and `os.open` are not text and are ignored; a deliberate exception can say
so with `# noqa: encoding`.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN = ("app", "scripts", "tests")

# (?<![A-Za-z0-9_.]) keeps urlopen( and os.open( out of the match
OPEN_RE = re.compile(r"(?<![A-Za-z0-9_.])open\(")
TEXT_RE = re.compile(r"\.read_text\(\)|\.write_text\(")
MODE_RE = re.compile(r"""[,(]\s*['"]([rwbax+]+)['"]""")


def _logical_lines(text):
    """Join a call that spans several physical lines into one string to judge.

    Matching line by line was itself a bug: `write_text(\n    payload, encoding="utf-8")` is
    correct code and a line-wise scan flagged it. Yields (line_number, joined_code_without_comments).
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start, depth, parts = i + 1, 0, []
        while i < len(lines):
            raw = lines[i]
            code = raw.split("#", 1)[0] if raw.lstrip().startswith("#") is False else ""
            if "noqa:" in raw and "noqa: encoding" in raw:
                code = ""                                    # deliberate exception, stated inline
            parts.append(code)
            depth += code.count("(") - code.count(")")
            i += 1
            if depth <= 0:
                break
        yield start, " ".join(parts).strip()


def offenders():
    out = []
    for folder in SCAN:
        for path in sorted((ROOT / folder).glob("*.py")):
            rel = path.relative_to(ROOT)
            for no, code in _logical_lines(path.read_text(encoding="utf-8")):
                if not code:
                    continue
                mode = MODE_RE.search(code)
                if mode and "b" in mode.group(1):
                    continue                                 # binary: no encoding involved
                if TEXT_RE.search(code) and "encoding" not in code:
                    out.append(f"{rel}:{no}: {code[:110]}")
                elif OPEN_RE.search(code) and "encoding" not in code:
                    out.append(f"{rel}:{no}: {code[:110]}")
    return out


def test_no_text_io_leaves_encoding_to_the_platform():
    bad = offenders()
    assert not bad, (
        "text file I/O without an explicit encoding — fine on a UTF-8 box, broken where Python "
        "falls back to the locale codec (Windows CI hit exactly this reading dashboard.js):\n  "
        + "\n  ".join(bad))


def test_the_gate_itself_detects_the_bug_it_exists_to_catch():
    """A guard that finds nothing even when handed the bug is worse than no guard."""
    sample = 'js = pathlib.Path("dashboard.js").read_text()\n'   # noqa: encoding — the bug, kept
    hits = [ln for ln in sample.splitlines()
            if TEXT_RE.search(ln.split("#", 1)[0]) and "encoding" not in ln]
    assert hits, "the pattern stopped matching the very bug this file was written for"
    good = 'js = pathlib.Path("dashboard.js").read_text(encoding="utf-8")\n'
    assert not [ln for ln in good.splitlines()
                if TEXT_RE.search(ln.split("#", 1)[0]) and "encoding" not in ln]

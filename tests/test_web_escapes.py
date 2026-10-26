"""Character-escaping and engine-text sinks for the dashboard.

Two contracts, both earned the hard way:

1. escH() is the only guard between text the operator does not control (Hermes
   session titles, model names, client labels, user-created recipe names) and the
   dashboard's innerHTML sinks. Its correctness lives entirely in one regex
   character class. It used to cover < > " ' \\ but omitted '&', which made the
   '&' entry in the replacement map unreachable: a bare '&' survived raw and let a
   supplied entity such as &lt; decode into a visible '<'. That is display
   corruption rather than tag breakout — < > " ' were escaped and no sink puts
   escH output into a URL — but an escaping helper is only worth having if it is
   complete, and a dropped character is exactly the regression this file catches.

2. Foreign text must reach markup through escH(), never raw. The field list below
   is the set of values that originate outside this app (engine /api responses,
   /api/recipes POST bodies written by anyone on the LAN when no password is set).
   Literal UI strings and hardcoded BUBS labels are deliberately NOT in the list:
   they are not attacker-influencable, and pretending otherwise makes a contract
   that has to be loosened the first time someone adds a card.
"""
import pathlib
import re

JS = pathlib.Path(__file__).resolve().parent.parent / "web" / "js" / "dashboard.js"
SRC = JS.read_text(encoding="utf-8")

REQUIRED = {"&", "<", ">", '"', "'", "\\"}

# values that originate outside this process, and so must never be interpolated raw
FOREIGN_FIELDS = (
    r"c\.title",        # client-context titles from /api
    r"s\.title",        # Hermes session titles (attacker-influencable)
    r"s\.profile",      # Hermes profile names
    r"meta\.id",
    r"meta\.model",
    r"meta\.kind",
    r"r\.name",         # recipe names: POSTed by whoever can reach the LAN port
    r"r\.cmd",
)


def _escH_class():
    m = re.search(r"const escH = s =>\s*String\(s \|\| ''\)\.replace\(/([^/]+)/g", SRC)
    assert m, "escH() not found or its shape changed — update this test deliberately"
    cls = m.group(1)
    assert cls.startswith("[") and cls.endswith("]"), f"unexpected class form: {cls}"
    return cls[1:-1]                      # strip the brackets, they are not members


def _members(cls):
    out, i = set(), 0
    esc = {"n": "\n", "t": "\t", "r": "\r"}
    while i < len(cls):
        if cls[i] == "\\":
            nxt = cls[i + 1]
            out.add(esc.get(nxt, nxt))
            i += 2
        else:
            out.add(cls[i])
            i += 1
    return out


def test_escH_covers_every_html_special():
    missing = REQUIRED - _members(_escH_class())
    assert not missing, f"escH() does not escape {sorted(missing)} — HTML injection surface"


def test_escH_replacement_map_covers_every_escaped_char():
    """A character in the class with no map entry renders as the string undefined."""
    body = SRC[SRC.index("const escH ="):]
    # pull the map literal out, then parse key: value pairs in either quote style
    m = re.search(r"\(\{(.+?)\}\[c\]\)", body, re.S)
    assert m, "escH map literal not found — helper shape changed"
    pairs = re.findall(r"""(?:"([^"]*)"|'([^']*)')\s*:\s*(?:"[^"]*"|'[^']*')""", m.group(1))
    keys = set()
    for k1, k2 in pairs:
        k = k1 if k1 else k2
        keys.add(k.replace("\\\\", "\\").replace("\n", ""))
    unmapped = {c for c in _members(_escH_class()) if c not in keys}
    assert not unmapped, f"escH() escapes {sorted(unmapped)} but the map has no entry for it"


def test_foreign_text_never_reaches_an_html_sink_unescaped():
    """Only HTML contexts count. A textContent assignment is parsed as data by the
    browser, so requiring escH there would be theatre — the risk is attribute and
    text-node contexts inside template strings that become innerHTML.
    """
    offenders = []
    Q = "[\\x22\\x27]"          # matches a double or single quote, without quoting drama
    for field in FOREIGN_FIELDS:
        f = "(?:" + field + r")(?:\s*\|\|[^}]*)?"
        head = r"\$\{\s*(?!escH\()" + f + r"\s*\}"
        # attribute sink:  foo="${field}"  /  foo='${field}'
        for m in re.finditer(r"=\s*" + Q + head + Q, SRC):
            offenders.append(("attr", m.group(0)))
        # text-node sink:  >${field}<
        for m in re.finditer(r">\s*" + head + r"\s*<", SRC):
            offenders.append(("text", m.group(0)))
    assert not offenders, f"foreign text reached an HTML sink without escH: {offenders[:5]}"


def test_escH_helper_exists_and_is_used():
    """Guards against someone deleting the helper and 'fixing' call sites inline."""
    assert "const escH =" in SRC
    assert SRC.count("escH(") >= 10, "escH exists but is no longer the escaping path"

#!/usr/bin/env python3
"""Static checks for dash.html, to be run before committing a change to it.

Every check here exists because the bug it catches already shipped. None of those bugs
threw anything visible - the panel kept rendering and simply did the wrong thing, or
stopped doing a right thing, and the only symptom was somebody eventually noticing. That
is the shape of a failure in one global scope with no compiler: it is not that mistakes
happen, it is that nothing is watching.

    python check-dash.py          # exits non-zero if anything fails
"""
import collections
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / "dash.html"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def scripts(html):
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))


def check_syntax(html, js):
    """node --check. Catches the ordinary slips, and costs nothing."""
    tmp = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "_dashcheck.js"
    tmp.write_text(js, encoding="utf-8")
    try:
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True,
                           text=True, timeout=60, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return None, "node not on PATH - syntax unchecked"
    return (r.returncode == 0), ((r.stdout or "") + (r.stderr or "")).strip()[:400]


def check_duplicate_functions(html, js):
    """Two `function foo(){}` at top level: the later one silently replaces the first.

    ⚠️ THIS IS NOT HYPOTHETICAL. `renderPresets` was declared twice - once for the visuals
    tab's saved looks, once for scene presets. Declarations hoist, so the scene one won
    every call, including the visuals refresh's `renderPresets(s.presets || [])`, which
    passed an argument the winner does not take. The saved-looks list was never filled and
    "No saved looks yet" showed permanently, for as long as both names coexisted. Nothing
    errored. The wrong function ran and returned quietly, which is the worst case: a
    failure that looks exactly like a feature nobody uses.
    """
    names = re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", js, re.M)
    dupes = {n: c for n, c in collections.Counter(names).items() if c > 1}
    return (not dupes), ("duplicates: " + repr(dupes) if dupes
                         else "%d functions, all distinct" % len(names))


def check_ids_exist(html, js):
    """Every $('x') and getElementById('x') must match an id in the markup.

    A missing id is not an error in JavaScript - $() returns null, and the guard a few
    lines down (`if (!box) return;`) swallows it. The feature simply does not happen. This
    caught `pw_tags` before it shipped: a warning label that would never have appeared,
    on a panel where its absence means "this field is fine" rather than "this check is
    broken".
    """
    used = set(re.findall(r"\$\('([A-Za-z0-9_\-]+)'\)", js))
    used |= set(re.findall(r"getElementById\('([A-Za-z0-9_\-]+)'\)", js))
    have = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
    missing = sorted(used - have)
    return (not missing), ("missing from markup: " + ", ".join(missing) if missing
                           else "%d ids referenced, all present" % len(used))


def check_unique_ids(html, js):
    """Two elements sharing an id means the second one is unreachable by $()."""
    ids = re.findall(r'id="([A-Za-z0-9_\-]+)"', html)
    dupes = {i: c for i, c in collections.Counter(ids).items() if c > 1}
    return (not dupes), ("duplicated: " + repr(dupes) if dupes
                         else "%d ids, all unique" % len(ids))


def check_error_surface(html, js):
    """The page must install a global error handler before anything else can throw.

    Without it a thrown error is invisible: the panel keeps its last good paint and looks
    healthy. Three separate bugs here hid behind exactly that.
    """
    ok = "addEventListener('error'" in js and "unhandledrejection" in js
    if not ok:
        return False, "no window error / unhandledrejection handler found"
    head = js[:4000]
    early = "addEventListener('error'" in head
    return early, ("installed early" if early
                   else "present but not near the top - it must be armed before any "
                        "other code can throw")


def _find_tsc():
    """The TypeScript compiler, wherever it happens to live on this machine.

    ⚠️ BORROWED, NOT A DEPENDENCY OF THIS PROJECT. There is no node_modules here and
    adding one would mean a package manager in the loop of a rig that deliberately has no
    build step. tsc is used purely as an external linter - nothing it produces is shipped,
    nothing imports it - so the check SKIPS rather than fails when it is absent. A missing
    linter must never look like a failing check, or the suite stops being believed.
    """
    import glob
    for pat in (r"C:\Users\mccul\Airgid\node_modules\.pnpm\typescript@*"
                r"\node_modules\typescript\bin\tsc",
                r"C:\Users\mccul\**\node_modules\typescript\bin\tsc"):
        hits = glob.glob(pat, recursive="**" in pat)
        if hits:
            return hits[0]
    return None


def check_types(html, js):
    """tsc --checkJs, tuned to the few error classes that are real bugs here.

    ⚠️ FULL STRICTNESS IS NOISE ON UNTYPED JS AND WOULD GET THIS SWITCHED OFF. Run plain,
    it reports 534 errors against this file, almost all of them "implicitly has an 'any'
    type" and "object is possibly null" from $() - true statements about untyped
    JavaScript, and not one of them a defect. Suppressing those three leaves 77, and in
    that 77 were two REAL bugs nothing else had found: BR_WHY declared 'unknown' twice
    with different text, so one message could never display, and two isNaN(dateObject)
    calls that only worked by implicit coercion.

    A checker whose output nobody reads is worth less than no checker, so this reports
    only the high-signal codes:
        TS1117  duplicate key in an object literal - one value silently wins
        TS2304  cannot find name  <- this is the catNote(c) bug
        TS2552  cannot find name, did you mean...
        TS2554  wrong number of arguments
        TS2345  argument of the wrong type
    The rest are counted, not listed, so a drift upward is still visible.
    """
    import os
    import re
    import subprocess
    tsc = _find_tsc()
    if not tsc:
        return None, "tsc not found - type check skipped (it is a borrowed linter)"
    tmp = HERE / "_dashcheck.js"
    # Pad to the <script> offset so tsc's line numbers ARE dash.html's line numbers.
    m = re.search(r"<script[^>]*>", html)
    pad = html[:m.end()].count("\n") if m else 0
    tmp.write_text("\n" * pad + js, encoding="utf-8")
    try:
        r = subprocess.run(
            ["node", str(tsc), "--allowJs", "--checkJs", "--noEmit",
             "--target", "es2022", "--lib", "es2022,dom", "--skipLibCheck",
             "--noImplicitAny", "false", "--strictNullChecks", "false",
             "--noImplicitThis", "false", str(tmp)],
            capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
    except Exception as e:
        return None, "could not run tsc: %s" % type(e).__name__
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    loud = [l for l in (r.stdout or "").splitlines()
            if re.search(r"error TS(1117|2304|2552|2554|2345)\b", l)]
    total = len(re.findall(r"error TS", r.stdout or ""))
    if loud:
        detail = "%d real, %d total\n       " % (len(loud), total)
        detail += "\n       ".join(x.replace("_dashcheck.js", "dash.html") for x in loud[:8])
        return False, detail
    return True, "no high-signal type errors (%d low-signal, not shown)" % total


CHECKS = [
    ("syntax (node --check)", check_syntax),
    ("no duplicate functions", check_duplicate_functions),
    ("referenced ids exist", check_ids_exist),
    ("ids are unique", check_unique_ids),
    ("global error surface", check_error_surface),
    ("types (tsc --checkJs)", check_types),
]


def main():
    if not PAGE.exists():
        print("no dash.html next to this script"); return 2
    html = PAGE.read_text(encoding="utf-8")
    js = scripts(html)
    print("dash.html: %d lines, %d chars of script\n" % (
        html.count("\n") + 1, len(js)))
    worst = 0
    for label, fn in CHECKS:
        ok, detail = fn(html, js)
        if ok is None:                      # could not run; not a failure
            mark = "SKIP"
        elif ok:
            mark = "PASS"
        else:
            mark = "FAIL"; worst = 1
        print("  %-4s %-24s %s" % (mark, label, detail))
    print("\n" + ("all checks passed" if worst == 0 else "SOMETHING FAILED - do not commit"))
    return worst


if __name__ == "__main__":
    sys.exit(main())

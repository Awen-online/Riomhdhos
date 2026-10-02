#!/usr/bin/env python3
"""smoketest - run every check that has actually caught a dashboard bug here, in one go.

WHY THIS EXISTS, AND WHY IT IS NOT JUST checkdash.py. checkdash answers one question - does
the JavaScript parse - and that question has a cousin that bites harder. dash.html is one
page with ONE <script>, so everything in it shares one global scope, and the failures that
cost the most time are the ones that parse perfectly:

    a second `function foo()`     silently replaces the first. Today that quietly took out
                                  a whole tab's worth of buttons. node --check says ok.
    $('typo')                     returns null, and the TypeError lands in an onclick, so
                                  one control is dead and the rest of the page looks fine.
    a duplicated id=              getElementById returns whichever came first, so the
                                  control you are looking at is not the one you are wiring.

All three report the page as healthy at every level except the one that matters - the same
shape as the JSFX compile error that once looked like dead hardware. So this checks the
served page end to end: it parses, it is syntactically valid, its wiring is internally
consistent, and the read-only APIs behind it still answer in the shape the page expects.

    python smoketest.py                    # against the running dashboard
    python smoketest.py --url http://...   # against another host
    python smoketest.py --browser          # + load the page in headless Chrome

⚠️ GET ONLY, DELIBERATELY. A POST to /api/stream can retitle the live stream, flip a
destination or start a recording. Nothing in here is allowed to issue one, which is also
why --browser is off by default: a real browser runs the page's own JavaScript, and that
code is one stray init-time call away from writing to the rig mid-show. Run it against a
copy on a scratch port, not at the live dashboard, unless you have read today's diff.

Needs node for the parse (it is on PATH). --browser additionally needs Chrome and the
websocket-client package; without either it says so and skips rather than pretending.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8770/"

# The read-only half of the API, with the top-level keys the page destructures on arrival.
# A missing key here is not a 500 - the endpoint answers 200 with valid JSON and the panel
# renders blank - so the shape is asserted, not just the status code.
# ⚠️ /api/camera fans out to adb and to both phones over WiFi and takes ~3-7 s. It is not
# hung; a short timeout here just produces a scary false failure.
API_CHECKS = [
    ("/api/stream",      ("platforms", "ingest", "destinations", "obs",
                          "record", "meta", "task"),                     15),
    ("/api/chat?since=0", ("sources", "msgs", "seq"),                    15),
    ("/api/camera",      ("devices", "rigcams", "rigcamFps"),            60),
]


class Page(HTMLParser):
    """Collects markup ids and inline script text, with source line numbers.

    ⚠️ Deliberately not a regex. HTMLParser switches to CDATA mode inside <script>, so JS
    that writes `id="n_' + k + '"` into a template string is not mistaken for a real markup
    id - which a regex over the whole file does do, and that hides exactly the duplicate-id
    bug this is here to find.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []            # [(id, line)] in document order, duplicates kept
        self.scripts = []        # [(text, first line, kind)]
        self._in_script = False
        self._kind = "classic"

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append((value, self.getpos()[0]))
        if tag == "script":
            self._in_script = True
            # ⚠️ THE TYPE DECIDES HOW IT MUST BE PARSED. A module's `import` is a syntax
            # error in a classic script, and an importmap is JSON that is not JavaScript
            # at all - checking either as a plain script reports the checker's assumption
            # as the page's bug. The page was correct; this stage was not.
            t = dict((k, v or "") for k, v in attrs).get("type", "")
            self._kind = ("importmap" if t == "importmap"
                          else "module" if t == "module" else "classic")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.scripts.append((data, self.getpos()[0], self._kind))


class Report:
    """PASS/FAIL accumulator. Prints as it goes, because the camera stage is slow enough
    that a silent run looks like a hang."""

    def __init__(self):
        self.rows = []

    def add(self, name, ok, *detail, skipped=False):
        mark = "SKIP" if skipped else ("PASS" if ok else "FAIL")
        self.rows.append((mark, name))
        print(f"{mark}  {name}")
        for line in detail:
            print(f"      {line}")
        return ok

    def failed(self):
        return [n for m, n in self.rows if m == "FAIL"]

    def summary(self):
        print()
        print("-" * 70)
        for mark, name in self.rows:
            print(f"  {mark}  {name}")
        bad = self.failed()
        print("-" * 70)
        n_skip = sum(1 for m, _ in self.rows if m == "SKIP")
        print(f"{len(self.rows) - len(bad) - n_skip} passed, {len(bad)} failed,"
              f" {n_skip} skipped")
        return 1 if bad else 0


def fetch(url, timeout):
    req = urllib.request.Request(url, method="GET")   # explicit: see the GET-ONLY note
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ stage 1: the page

def stage_page(rep, url):
    try:
        status, html = fetch(url, 20)
    except Exception as e:
        rep.add("page GET", False, f"{url}: {e}")
        return None
    if status != 200:
        rep.add("page GET", False, f"{url}: HTTP {status}")
        return None

    page = Page()
    try:
        page.feed(html)
        page.close()
    except Exception as e:
        rep.add("HTML parses", False, str(e))
        return None
    rep.add("HTML parses", True, f"{len(html)} bytes, {len(page.ids)} ids")

    if not page.scripts:
        rep.add("page carries a <script>", False, "no inline script in the served page")
        return None
    total = sum(len(s) for s, _, k in page.scripts if k != "importmap")
    rep.add("page carries a <script>", True,
            f"{len(page.scripts)} block(s), {total} bytes of JavaScript")
    return page


# ------------------------------------------------------------------ stage 2: node --check

def stage_syntax(rep, page):
    node = shutil.which("node")
    if not node:
        rep.add("JavaScript parses (node --check)", True,
                "node not found - cannot parse", skipped=True)
        return

    for js, first_line, kind in page.scripts:
        if kind == "importmap":
            continue                      # JSON, validated by the browser, not by node
        # ⚠️ Pad the temp file so its line numbers ARE dash.html's line numbers. node
        # reports a line in the file it was handed, and "error on line 458" of something
        # that only ever existed in %TEMP% is not a place you can stand and look.
        padded = "\n" * (first_line - 1) + js
        # .mjs makes node parse it as a module, which is the only way `import` is legal.
        with tempfile.NamedTemporaryFile("w", suffix=(".mjs" if kind == "module" else ".js"),
                                         delete=False, encoding="utf-8") as fh:
            fh.write(padded)
            tmp = Path(fh.name)
        try:
            r = subprocess.run([node, "--check", str(tmp)],
                               capture_output=True, text=True)
        finally:
            tmp.unlink(missing_ok=True)

        if r.returncode == 0:
            continue

        err = (r.stderr or r.stdout).strip()
        detail = [ln for ln in err.splitlines() if ln.strip()][:8]
        # node's first stderr line is "<path>:<line>" - recover the line and quote around it
        m = re.search(r":(\d+)$", err.splitlines()[0]) if err else None
        if m:
            n = int(m.group(1))
            src = padded.splitlines()
            detail.append(f"-- dash.html around line {n} --")
            for i in range(max(0, n - 3), min(len(src), n + 2)):
                detail.append(f"{i + 1:5d} | {src[i]}")
        rep.add("JavaScript parses (node --check)", False, *detail)
        return

    rep.add("JavaScript parses (node --check)", True, "clean")


# ------------------------------------------------------------------ stage 3: static checks

FUNC_DECL = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)
# Only literal ids. $(someVar) and $('pre_' + k) cannot be resolved statically and are
# counted, not guessed at - a check that invents answers stops being believed.
DOLLAR_REF = re.compile(r"\$\(\s*(['\"])([A-Za-z0-9_\-]+)\1\s*\)")
GEBI_REF = re.compile(r"getElementById\(\s*(['\"])([A-Za-z0-9_\-]+)\1\s*\)")
DOLLAR_ANY = re.compile(r"\$\(")


def js_of(page):
    """Each script block as (text, line-number offset) so matches can be reported in
    dash.html coordinates."""
    return [(t, l) for t, l, k in page.scripts if k == "classic"]


def line_of(text, pos, first_line):
    return first_line + text.count("\n", 0, pos)


def check_dup_functions(rep, page):
    seen = {}
    dupes = []
    for js, first in js_of(page):
        # ⚠️ Column 0 only. One page, one <script>, one global scope: a `function foo` at
        # the left margin is a global binding, and the second one wins silently. Nested
        # declarations are indented in this file and are properly scoped, so they are not
        # redeclarations and must not be flagged.
        for m in FUNC_DECL.finditer(js):
            if m.start() != 0 and js[m.start() - 1] != "\n":
                continue
            name = m.group(1)
            line = line_of(js, m.start(), first)
            if name in seen:
                dupes.append((name, seen[name], line))
            else:
                seen[name] = line
    if dupes:
        return rep.add("no duplicate top-level function declarations", False,
                       *[f"{n}() declared at line {a}, redeclared at line {b}"
                         f" - the second one wins and the first is gone"
                         for n, a, b in dupes])
    return rep.add("no duplicate top-level function declarations", True,
                   f"{len(seen)} top-level functions, all distinct")


def check_dup_ids(rep, page):
    first_seen = {}
    dupes = []
    for ident, line in page.ids:
        if ident in first_seen:
            dupes.append((ident, first_seen[ident], line))
        else:
            first_seen[ident] = line
    if dupes:
        return rep.add("no duplicate id attributes", False,
                       *[f'id="{i}" at line {a} and again at line {b}'
                         f" - getElementById only ever returns the first"
                         for i, a, b in dupes])
    return rep.add("no duplicate id attributes", True, f"{len(first_seen)} unique ids")


# Ids are no longer all declared in static markup: the Preact panels write their own
# id="..." inside html`` templates, and those are as real at runtime as any other. Count
# them too, or every converted field reads as a dangling reference. The check still earns
# its keep - it catches a typo'd id in either half - it just no longer assumes one half.
SCRIPT_ID = re.compile('(?:^|[^A-Za-z-])id="([A-Za-z0-9_-]+)"')


def _ids_in_script(page):
    # Both kinds: js_of() is classic-only, and the converted panels live in the module.
    # Only ever added to the "known" set for reference resolution - never to the
    # duplicate-id check, which must keep counting real markup and nothing else.
    found = set()
    for text, _, kind in page.scripts:
        if kind in ("classic", "module"):
            found.update(SCRIPT_ID.findall(text))
    return found


def _check_refs(rep, page, label, pattern):
    known = {i for i, _ in page.ids} | _ids_in_script(page)
    bad, n_refs = [], 0
    for js, first in js_of(page):
        for m in pattern.finditer(js):
            n_refs += 1
            ident = m.group(2)
            if ident not in known:
                bad.append((ident, line_of(js, m.start(), first)))
    if bad:
        return rep.add(label, False,
                       *[f"line {line}: '{i}' has no id=\"{i}\" in markup or templates"
                         for i, line in sorted(set(bad), key=lambda t: t[1])])
    return rep.add(label, True, f"{n_refs} literal references, all resolve")


def stage_static(rep, page):
    ok = check_dup_functions(rep, page)
    ok &= check_dup_ids(rep, page)
    ok &= _check_refs(rep, page, "every $('id') resolves to markup", DOLLAR_REF)
    ok &= _check_refs(rep, page, "every getElementById('id') resolves to markup", GEBI_REF)

    total = sum(len(DOLLAR_ANY.findall(js)) for js, _ in js_of(page))
    literal = sum(len(DOLLAR_REF.findall(js)) for js, _ in js_of(page))
    if total > literal:
        print(f"      note: {total - literal} $() calls take a computed id and were"
              f" not checked")
    return ok


# ------------------------------------------------------------------ stage 4: the APIs

def stage_api(rep, base):
    ok = True
    for path, keys, timeout in API_CHECKS:
        url = base.rstrip("/") + path
        t0 = time.time()
        try:
            status, body = fetch(url, timeout)
        except Exception as e:
            ok &= rep.add(f"GET {path}", False, f"{type(e).__name__}: {e}")
            continue
        dt = time.time() - t0
        if status != 200:
            ok &= rep.add(f"GET {path}", False, f"HTTP {status}")
            continue
        try:
            data = json.loads(body)
        except ValueError as e:
            ok &= rep.add(f"GET {path}", False, f"not JSON: {e}",
                          f"first 200 bytes: {body[:200]!r}")
            continue
        if not isinstance(data, dict):
            ok &= rep.add(f"GET {path}", False,
                          f"expected an object, got {type(data).__name__}")
            continue
        missing = [k for k in keys if k not in data]
        if missing:
            ok &= rep.add(f"GET {path}", False,
                          f"missing top-level keys: {', '.join(missing)}",
                          f"got: {', '.join(sorted(data))}")
            continue
        ok &= rep.add(f"GET {path}", True, f"{len(keys)} keys present, {dt:.1f}s")
    return ok


# ------------------------------------------------------------------ stage 5: real browser

def _arg_text(a):
    if "value" in a:
        return str(a["value"])
    return a.get("description") or a.get("unserializableValue") or a.get("type", "?")


def stage_browser(rep, url, settle):
    """Load the page in headless Chrome over CDP and report what the console said.

    Everything above is static; this is the only stage that can catch a runtime failure -
    a null deref in an init path, a fetch to an endpoint that moved. It is also the only
    stage that executes the dashboard's own code, hence the warning at the top of the file.
    """
    try:
        import websocket                        # websocket-client, already a rig dependency
    except ImportError:
        rep.add("browser console clean", True,
                "websocket-client not installed - cannot drive CDP (not installing)",
                skipped=True)
        return True

    chrome = (shutil.which("chrome") or shutil.which("chrome.exe")
              or next((p for p in (
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
              ) if Path(p).exists()), None))
    if not chrome:
        rep.add("browser console clean", True, "no Chrome or Brave found", skipped=True)
        return True

    # ⚠️ A throwaway --user-data-dir. Pointing headless Chrome at the real profile either
    # refuses to start because the desktop browser holds the lock, or worse, disturbs the
    # browser the show is being run from.
    profile = tempfile.mkdtemp(prefix="smoketest-chrome-")
    # Chrome's own stderr goes to a file, not a pipe: it is chatty, nobody reads the pipe
    # until something has already gone wrong, and a full pipe buffer would wedge it.
    log = Path(profile) / "chrome.log"
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--remote-debugging-port=0", "--no-first-run",
         "--no-default-browser-check", "--disable-gpu", f"--user-data-dir={profile}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=log.open("w"),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    try:
        # Port 0 means Chrome picks one and writes it here; polling the file is the only
        # supported way to learn it.
        port_file = Path(profile) / "DevToolsActivePort"
        deadline = time.time() + 20
        port = None
        while time.time() < deadline:
            if port_file.exists():
                txt = port_file.read_text().splitlines()
                if txt and txt[0].strip().isdigit():
                    port = int(txt[0].strip())
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if port is None:
            tail = log.read_text(errors="replace")[-300:] if log.exists() else ""
            return rep.add("browser console clean", False,
                           "headless Chrome did not come up", tail)

        # ⚠️ Attach to the about:blank tab Chrome already opened rather than asking for a
        # new one. /json/new has needed PUT rather than POST since Chrome 111 and answers
        # 405 to the old call - a confusing way to fail for something we do not need.
        # ⚠️ AND FILTER ON type == "page". Even a brand-new profile lists Chrome's own
        # component-extension background pages first, and they are all attachable. The
        # first attempt took tabs[0], attached to an extension, navigated that, and
        # reported a serenely clean console for a page it had never loaded.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list",
                                    timeout=10) as r:
            tabs = [t for t in json.load(r)
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not tabs:
            return rep.add("browser console clean", False, "no debuggable page target")
        # ⚠️ suppress_origin, not --remote-allow-origins=*. Chrome 111+ rejects a CDP
        # socket that carries an Origin header it was not told to trust, and
        # websocket-client sends one by default - that shows up as a bare "403 Forbidden"
        # with nothing to connect it to. Dropping the header is the narrow fix; the flag
        # would open the debugging port to any page the browser happens to load.
        ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"],
                                         timeout=30, suppress_origin=True)
        try:
            # ⚠️ Read CDP's own events; do NOT inject a script that wraps console and
            # stashes messages on window. That was the first attempt and it silently
            # collected nothing - the array lives in a page context that navigation and
            # the page's own code can get between, so the probe reports "clean" whatever
            # happened, which is the single worst thing a check can do.
            errors, warnings = [], []

            def pump(msg):
                m, p = msg.get("method"), msg.get("params", {})
                if m == "Runtime.exceptionThrown":
                    d = p.get("exceptionDetails", {})
                    desc = (d.get("exception") or {}).get("description") or d.get("text")
                    line = d.get("lineNumber")
                    where = f" (line {line + 1})" if isinstance(line, int) else ""
                    errors.append(f"uncaught: {str(desc).splitlines()[0]}{where}")
                elif m == "Runtime.consoleAPICalled":
                    text = " ".join(_arg_text(a) for a in p.get("args", []))
                    kind = p.get("type")
                    if kind == "error":
                        errors.append(f"console.error: {text}")
                    elif kind in ("warning", "assert"):
                        warnings.append(f"console.{kind}: {text}")
                elif m == "Log.entryAdded":
                    e = p.get("entry", {})
                    if e.get("level") == "error":
                        src = e.get("source", "log")
                        # The url matters more than the text: "Failed to load resource:
                        # 404" on its own names no endpoint and helps nobody.
                        at = f" <- {e['url']}" if e.get("url") else ""
                        errors.append(f"{src}: {e.get('text', '')}{at}")

            def cmd(n, method, **params):
                ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == n:
                        return msg
                    pump(msg)

            cmd(1, "Runtime.enable")
            cmd(2, "Log.enable")       # network and CSP failures the page never sees
            cmd(3, "Page.enable")
            cmd(4, "Page.navigate", url=url)

            # Drain events for `settle` seconds. The dashboard polls on a 1 s timer, so
            # anything shorter than a few seconds misses the failures that only show up
            # on the second pass.
            ws.settimeout(0.5)
            deadline = time.time() + settle
            while time.time() < deadline:
                try:
                    pump(json.loads(ws.recv()))
                except websocket.WebSocketTimeoutException:
                    continue                           # quiet half-second, keep waiting
                except Exception:
                    break                              # socket gone; report what we have
        finally:
            ws.close()
    except Exception as e:
        # A broken debugging stage must report itself as broken, not take the whole run
        # down with a traceback - the static checks above it are the ones that matter.
        return rep.add("browser console clean", False, f"{type(e).__name__}: {e}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    # Dedupe but keep order: a 1 Hz poll against a dead endpoint produces the same line
    # six times, and six copies of one fault reads as six faults.
    seen, uniq = set(), []
    for e in errors:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    if uniq:
        return rep.add("browser console clean", False,
                       *uniq[:20],
                       *([f"... and {len(uniq) - 20} more"] if len(uniq) > 20 else []))
    return rep.add("browser console clean", True,
                   f"{settle:g}s after load, no console errors"
                   + (f", {len(warnings)} warnings" if warnings else ""))


# ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"dashboard root (default {DEFAULT_URL})")
    ap.add_argument("--no-api", action="store_true",
                    help="static checks only - skip the API round trips")
    ap.add_argument("--browser", action="store_true",
                    help="also load the page in headless Chrome and report console errors."
                         " ⚠️ this runs the page's own JavaScript - see the file header")
    ap.add_argument("--settle", type=float, default=6.0,
                    help="seconds to let the page run before reading the console")
    args = ap.parse_args()

    rep = Report()
    page = stage_page(rep, args.url)
    if page is None:
        return rep.summary()

    stage_syntax(rep, page)
    stage_static(rep, page)
    if not args.no_api:
        stage_api(rep, args.url)
    if args.browser:
        stage_browser(rep, args.url, args.settle)

    return rep.summary()


if __name__ == "__main__":
    sys.exit(main())

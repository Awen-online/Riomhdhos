#!/usr/bin/env python3
"""checkdash - parse the dashboard's JavaScript before it reaches a phone.

WHY THIS EXISTS. dash.html is one page with one <script> block, so a single syntax error -
a duplicated `const`, a stray brace - kills EVERYTHING on it. And it fails in the worst
possible way: the server still returns 200, the HTML still renders, the layout still looks
right, and nothing works. From the outside that reads as "the dashboard is down" or "the
network is broken", which is where the looking starts, and it is nowhere near the fault.

That is the same shape as the JSFX compile error that once looked like dead hardware: the
thing reports itself as healthy at every level except the one that matters.

    python checkdash.py            # check the file on disk
    python checkdash.py --served   # check what the running dashboard actually serves

Needs node for the parse. Without it this says so and exits 0 rather than pretending.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def scripts_of(html):
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--served", action="store_true",
                    help="fetch from the running dashboard instead of reading the file")
    ap.add_argument("--url", default="http://127.0.0.1:8770/")
    args = ap.parse_args()

    if args.served:
        with urllib.request.urlopen(args.url, timeout=10) as r:
            html = r.read().decode("utf-8", "replace")
        where = args.url
    else:
        path = HERE / "dash.html"
        html = path.read_text(encoding="utf-8")
        where = str(path)

    js = scripts_of(html)
    if not js.strip():
        print(f"no <script> found in {where}")
        return 1

    node = shutil.which("node")
    if not node:
        print("node not found - cannot parse; install node or check in a browser console")
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        tmp = fh.name
    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"ok  {where}: {len(js)} bytes of JavaScript, parses clean")
        return 0
    # node reports a line number within the extracted script; give the surrounding lines,
    # because "line 458 of something you never wrote to disk" is not a useful place to stand
    print(f"BAD {where}:")
    print((r.stderr or r.stdout).strip()[:1200])
    m = re.search(r":(\d+)\s*$", (r.stderr or "").splitlines()[0] if r.stderr else "")
    if m:
        n = int(m.group(1))
        lines = js.splitlines()
        for i in range(max(0, n - 3), min(len(lines), n + 2)):
            print(f"  {i+1:5d} | {lines[i]}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
tuner - live filter control from the phone, for when you are at the instrument and not
at the keyboard.

WHY A PROXY AND NOT A PURE BROWSER PAGE: obs-websocket needs a SHA256 challenge handshake
with the server password. Doing that in the page would mean shipping the password to
every device that loads it. This keeps it server-side - the phone talks plain HTTP to
this process, and only this process ever sees the credential.

⚠️ WHAT IS SAFE TO CHANGE LIVE, established by testing rather than assumption:

    blur params (strength/focus/depth)   SAFE - 6 rapid changes, no fault
    enable / disable                     SAFE - 4 toggles, no fault
    remove + create a filter             CRASHES OBS - observed, access violation in
                                         obs_source_skip_video_filter
    model_select                         same allocation path as create; assume unsafe

So this exposes parameters and toggles ONLY. It deliberately cannot add, remove, or
re-model a filter - the operations that take the render thread down. If you need those,
do them before a show, from OBS itself.

    python tuner.py                 # then open http://<this-machine>:8770/ on the phone
    python tuner.py --port 8770 --source Webcam
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import obsctl  # noqa: E402  - reuses the credential handling and the logger pinning

_lock = threading.Lock()
_cl = None
SOURCE = "Webcam"

# Only these keys may be written. An allowlist rather than a filter, because the unsafe
# operations here do not merely misconfigure - they take OBS down mid-show.
ALLOWED = {"blur_background": (0, 20), "blur_focus_point": (0.0, 1.0),
           "blur_focus_depth": (0.0, 1.0), "temporal_smooth_factor": (0.0, 1.0)}


def client():
    """One shared connection, reconnected on demand. A dropped websocket must not need a
    restart of this process - it is meant to be left running through a show."""
    global _cl
    with _lock:
        try:
            _cl.get_version()
        except Exception:
            _cl = obsctl.connect(timeout=10)
        return _cl


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Filter tuner</title>
<style>
  :root{--bg:#0b0d10;--panel:#14181d;--line:#232a32;--fg:#e8edf2;--dim:#7d8894;
        --accent:#e0a458;--ok:#5fb87a;color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.4 ui-sans-serif,system-ui,-apple-system,sans-serif;
       padding:16px;padding-bottom:max(16px,env(safe-area-inset-bottom))}
  h1{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);
     margin:0 0 14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
        padding:14px 16px;margin-bottom:12px}
  .row{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
  .k{color:var(--dim);font-size:13px}
  .v{font-family:ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums}
  /* Big targets: this is used standing up, one-handed, in a dim room. */
  input[type=range]{width:100%;height:38px;accent-color:var(--accent);margin:0}
  button{width:100%;padding:15px;font-size:16px;border-radius:10px;border:1px solid var(--line);
         background:#1b2129;color:var(--fg);font-weight:600}
  button.on{background:var(--ok);border-color:var(--ok);color:#06210f}
  .note{color:var(--dim);font-size:12px;line-height:1.5;margin:10px 2px 0}
  .err{color:#e0645f;font-size:12px;margin-top:8px;min-height:1em}
</style>

<h1>Focal blur &middot; <span id="src"></span></h1>

<div class="card">
  <button id="toggle">…</button>
</div>

<div class="card" id="controls"></div>
<div class="err" id="err"></div>
<p class="note">Parameters and on/off only. Adding, removing or re-modelling a filter
crashes OBS's render thread, so those are deliberately not here — do them before a show.</p>

<script>
const FIELDS = [
  ["blur_background",       "Strength",   0,   20,  1,    v=>v],
  ["blur_focus_point",      "Focus point",0,    1,  0.01, v=>v.toFixed(2)],
  ["blur_focus_depth",      "Sharp band", 0,    1,  0.01, v=>v.toFixed(2)],
  ["temporal_smooth_factor","Smoothing",  0,    1,  0.01, v=>v.toFixed(2)],
];
let timer = null, pending = {};

function el(t, c, txt){ const e=document.createElement(t); if(c)e.className=c;
                        if(txt!==undefined)e.textContent=txt; return e; }

function build(state){
  document.getElementById('src').textContent = state.source;
  const box = document.getElementById('controls'); box.innerHTML='';
  for (const [key,label,min,max,step,fmt] of FIELDS){
    const row = el('div','row');
    row.append(el('span','k',label));
    const val = el('span','v', fmt(state.settings[key] ?? 0)); val.id = 'v_'+key;
    row.append(val); box.append(row);
    const r = el('input'); r.type='range'; r.min=min; r.max=max; r.step=step;
    r.value = state.settings[key] ?? 0;
    // Debounced: a slider drag fires continuously, and 60 websocket round-trips a second
    // buys nothing a viewer can see while giving the render thread more to contend with.
    r.addEventListener('input', () => {
      val.textContent = fmt(parseFloat(r.value));
      pending[key] = parseFloat(r.value);
      clearTimeout(timer); timer = setTimeout(flush, 120);
    });
    box.append(r);
  }
  const b = document.getElementById('toggle');
  b.textContent = state.enabled ? 'BLUR ON' : 'BLUR OFF';
  b.classList.toggle('on', state.enabled);
}

async function flush(){
  const body = pending; pending = {};
  try{
    const r = await fetch('/api/set', {method:'POST',headers:{'Content-Type':'application/json'},
                                       body:JSON.stringify(body)});
    if(!r.ok) throw new Error(await r.text());
    document.getElementById('err').textContent='';
  }catch(e){ document.getElementById('err').textContent = e.message; }
}

document.getElementById('toggle').addEventListener('click', async () => {
  try{
    const r = await fetch('/api/toggle',{method:'POST'});
    build(await r.json());
  }catch(e){ document.getElementById('err').textContent = e.message; }
});

(async function init(){
  try{ build(await (await fetch('/api/state')).json()); }
  catch(e){ document.getElementById('err').textContent = 'cannot reach OBS: '+e.message; }
})();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # the default logger writes a line per request; useless noise here

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _state(self):
        cl = client()
        fl = cl.get_source_filter_list(SOURCE).filters
        f = next((x for x in fl if x["filterKind"] == "background_removal"), None)
        if not f:
            return {"source": SOURCE, "error": "no background_removal filter on this source",
                    "settings": {}, "enabled": False}
        s = cl.get_source_filter(SOURCE, f["filterName"]).filter_settings
        return {"source": SOURCE, "filter": f["filterName"],
                "enabled": f["filterEnabled"], "settings": s}

    def do_GET(self):
        try:
            if self.path.startswith("/api/state"):
                self._send(200, json.dumps(self._state()))
            elif self.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            else:
                self._send(404, "{}")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        try:
            st = self._state()
            if "filter" not in st:
                self._send(409, json.dumps(st)); return
            cl = client()
            if self.path.startswith("/api/toggle"):
                cl.set_source_filter_enabled(SOURCE, st["filter"], not st["enabled"])
                self._send(200, json.dumps(self._state())); return

            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            out = {}
            for k, v in body.items():
                if k not in ALLOWED:      # allowlist: the unsafe keys take OBS down
                    continue
                lo, hi = ALLOWED[k]
                out[k] = max(lo, min(hi, float(v)))
                if k == "blur_background":
                    out[k] = int(out[k])
            if out:
                cl.set_source_filter_settings(SOURCE, st["filter"], out, True)
            self._send(200, json.dumps({"applied": out}))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))


def main():
    global SOURCE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--source", default="Webcam")
    args = ap.parse_args()
    SOURCE = args.source

    client()   # fail loudly now rather than on the phone's first tap
    # ASCII only in console output. Windows consoles default to cp1252, and a single
    # emoji in a print() raises UnicodeEncodeError - which killed this server at startup
    # AFTER it had already bound and reported its address, so it looked like it was
    # running and simply refused every connection.
    print(f"tuner on port {args.port}   (source: {SOURCE})")
    for label, ip in _addresses():
        print(f"  {label:<10} http://{ip}:{args.port}/")
    print("If the phone cannot reach it, the Ethernet adapter is on the Public firewall")
    print("profile and needs an inbound rule for this port.")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


def _addresses():
    """Every address the phone might use, LAN first.

    NOT socket.gethostbyname(gethostname()) - with a VPN up that returns the TUNNEL
    address (10.2.0.2 here), which no phone on the LAN can reach. Printing one confident
    wrong URL is worse than printing several and letting the reader choose.
    """
    import socket
    out, seen = [], set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 9))   # no packet is sent; this only picks a route
        ip = s.getsockname()[0]; s.close()
        if ip not in seen:
            out.append(("lan", ip)); seen.add(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in seen:
                out.append(("also", ip)); seen.add(ip)
    except Exception:
        pass
    return out or [("local", "127.0.0.1")]


if __name__ == "__main__":
    main()

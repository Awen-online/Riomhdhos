#!/usr/bin/env python3
"""
ollama_obs - local LLM to an on-screen OBS text source.

The GPU half of this is settled: Ollama 0.32.13 selects ROCm on the RX 9060 XT
(gfx1200) and runs 100% on the GPU at ~53-70 tok/s. The CPU fallback is 4.8 tok/s, so
the placement is worth 11x and is worth CHECKING rather than assuming - see the traps
below, both of which produce a working-but-wrong setup that looks fine.

  python ollama_obs.py --demo
  python ollama_obs.py --ask "what tuning is DADGAD?"
  python ollama_obs.py --check          # is it on the GPU right now?

⚠️ TRAP 1 - A SINGLE CPU REQUEST POISONS THE RESIDENT MODEL.
Asking for `num_gpu: 0` once (to benchmark, say) loads a CPU-placed copy, and every
later request without options reuses THAT resident instance. `ollama ps` flips to
"100% CPU" and stays there until the model unloads. It is not sticky config, it is a
resident instance - `ollama stop <model>` clears it. This cost a confusing five minutes
here; on a stream it would look like the GPU had silently stopped working.

⚠️ TRAP 2 - THE MODEL UNLOADS AFTER 5 MINUTES IDLE.
Default OLLAMA_KEEP_ALIVE is 5m. A warm request answers in ~320 ms; the first request
after an unload takes ~16 SECONDS while 5.3 GB is read back into VRAM. Chat during a
set is bursty by nature, so the default guarantees the worst latency exactly when
someone is watching. KEEP_ALIVE below pins it resident for the whole show.

⚠️ Windows GPU performance counters do NOT see ROCm allocations - they reported 2.07 GB
with the model resident and 2.09 GB without. `ollama ps` is the number to trust; the
Task Manager style counters are measuring only the DirectX heap.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# obsctl already solves connecting to OBS (it reads OBS's own generated password out of
# %APPDATA%). Import it rather than restating that logic - one place to fix if the
# credential path ever moves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obsctl  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
MODEL = "llama3.1:8b"

# -1 pins the model in VRAM indefinitely. See TRAP 2 - the alternative is a 16 s stall
# on the first message after a quiet patch.
KEEP_ALIVE = -1

# Short answers on purpose. This lands on a video overlay that someone reads while music
# is playing, not in a chat window they can scroll. Two sentences is about four lines at
# a legible size, and anything longer either overflows or has to be shrunk to unreadable.
SYSTEM = (
    "You are answering questions in the live chat of a solo Irish folk music stream. "
    "The performer plays guitar and uilleann pipes and tells stories. "
    "Answer in at most two short sentences. Be warm and plain-spoken. "
    "No markdown, no lists, no emoji - your answer is rendered as plain text on screen."
)


def _post(path, payload, timeout=300):
    req = urllib.request.Request(
        OLLAMA + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(path, timeout=10):
    with urllib.request.urlopen(OLLAMA + path, timeout=timeout) as r:
        return json.loads(r.read())


def ask(prompt, model=MODEL, system=SYSTEM, on_token=None):
    """Send a prompt, return (text, stats).

    Streams by default so a caller can paint tokens as they arrive - on an overlay that
    reads as the answer being written rather than appearing whole, which looks alive and
    covers the generation time instead of showing a blank box during it.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": on_token is not None,
        "keep_alive": KEEP_ALIVE,
    }

    if on_token is None:
        d = _post("/api/generate", payload)
        return d.get("response", "").strip(), _stats(d)

    req = urllib.request.Request(
        OLLAMA + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    parts, final = [], {}
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("response"):
                parts.append(d["response"])
                on_token("".join(parts))
            if d.get("done"):
                final = d
    return "".join(parts).strip(), _stats(final)


def _stats(d):
    ns = 1e9
    ev, ed = d.get("eval_count", 0), d.get("eval_duration", 0)
    return {
        "tokens": ev,
        "tok_per_sec": (ev / (ed / ns)) if ed else 0.0,
        "ttft_ms": (d.get("load_duration", 0) + d.get("prompt_eval_duration", 0)) / 1e6,
        "total_s": d.get("total_duration", 0) / ns,
    }


def placement(model=MODEL):
    """Where is the model actually running? Returns (label, size_bytes) or (None, 0).

    This is the check that matters. A CPU-placed model answers correctly and slowly,
    which is the failure mode that survives testing and shows up during a show.
    """
    try:
        for m in _get("/api/ps").get("models", []):
            if m.get("name", "").startswith(model.split(":")[0]):
                total = m.get("size", 0)
                vram = m.get("size_vram", 0)
                if total and vram >= total * 0.99:
                    label = "100% GPU"
                elif vram == 0:
                    label = "100% CPU"
                else:
                    label = f"{100 * vram / total:.0f}% GPU"
                return label, total
    except urllib.error.URLError:
        pass
    return None, 0


# --------------------------------------------------------------------- OBS side

# text_gdiplus_v3 is the current Windows text source; the older names are kept as
# fallbacks because a scene collection made on an older OBS keeps whatever kind it was
# created with, and this has to drive sources it did not create.
TEXT_KINDS = ("text_gdiplus_v3", "text_gdiplus_v2", "text_ft2_source_v2", "text_gdiplus")


def push(cl, source, text):
    """Write text into an existing OBS text source."""
    cl.set_input_settings(source, {"text": text}, True)


def ensure_text_source(cl, scene, source):
    """Create the text source only if it is missing. Deliberately does not restyle an
    existing one - if it is already on screen, its font and position are someone's
    deliberate choice and not this script's to overwrite."""
    if source in [i["inputName"] for i in cl.get_input_list().inputs]:
        return False
    # get_input_kind_list takes a required `unversioned` flag in obsws-python 1.8 and
    # returns bare strings, not dicts - both differ from what the protocol docs suggest.
    kinds = set(cl.get_input_kind_list(False).input_kinds)
    kind = next((k for k in TEXT_KINDS if k in kinds), None)
    if kind is None:
        sys.exit("no text source kind available in this OBS build")
    if scene not in [s["sceneName"] for s in cl.get_scene_list().scenes]:
        cl.create_scene(scene)
    cl.create_input(scene, source, kind, {
        "text": "",
        "font": {"face": "Segoe UI", "size": 42, "style": "Regular"},
        # ⚠️ WITHOUT extents + extents_wrap A TEXT SOURCE NEVER WRAPS. It renders one
        # ever-lengthening line straight off the side of the canvas, which looks like
        # the text is missing rather than like it overflowed. The bounding box is what
        # turns a string into a paragraph.
        "extents": True,
        "extents_cx": 1500,
        "extents_cy": 400,
        "extents_wrap": True,
        # An outline, because this lands over live video whose brightness is not known
        # in advance. White text alone disappears against a bright frame.
        "outline": True,
        "outline_size": 3,
        "outline_color": 0xFF000000,
    }, True)
    return True


# --------------------------------------------------------------------- commands

def cmd_check(_a):
    try:
        tags = _get("/api/tags")
    except urllib.error.URLError as e:
        sys.exit(f"Ollama is not answering on {OLLAMA}: {e}")
    print("models:", ", ".join(m["name"] for m in tags.get("models", [])) or "(none)")
    label, size = placement()
    if label is None:
        print("placement: model not resident - send one request to load it")
    else:
        print(f"placement: {label}   resident size {size / 1e9:.1f} GB")


def cmd_ask(a):
    text, st = ask(a.ask)
    print(text)
    print(f"\n[{st['tokens']} tok, {st['tok_per_sec']:.1f} tok/s, "
          f"ttft {st['ttft_ms']:.0f} ms]  placement: {placement()[0]}")


def cmd_demo(a):
    """Prove the whole path: prompt -> GPU -> OBS overlay."""
    cl = obsctl.connect()
    made = ensure_text_source(cl, a.scene, a.source)
    print(f"{'created' if made else 'using existing'} text source '{a.source}'"
          f"{' in scene ' + a.scene if made else ''}")

    question = a.ask or "What is DADGAD tuning, and why do folk guitarists like it?"
    push(cl, a.source, "…")
    print(f"\nQ: {question}\n")

    # Repaint on every token so the overlay writes the answer out live. Throttled,
    # because obs-websocket calls at ~70 tok/s would be 70 round-trips a second for a
    # visual difference nobody can perceive.
    last = [0.0]

    def on_token(sofar):
        now = time.time()
        if now - last[0] > 0.1:
            push(cl, a.source, sofar)
            last[0] = now

    text, st = ask(question, on_token=on_token)
    push(cl, a.source, text)          # final repaint, so no throttled tail is lost

    print(f"A: {text}\n")
    print(f"[{st['tokens']} tok, {st['tok_per_sec']:.1f} tok/s, "
          f"ttft {st['ttft_ms']:.0f} ms, total {st['total_s']:.1f} s]")
    print(f"placement: {placement()[0]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="prompt -> GPU -> OBS overlay")
    ap.add_argument("--check", action="store_true", help="is the model on the GPU?")
    ap.add_argument("--ask", metavar="TEXT", help="ask a question")
    # ⚠️ These must differ. Scenes and inputs share ONE namespace in OBS - a scene IS a
    # source - so naming both "CHAT DEMO" makes CreateInput fail with 601 "a source
    # already exists by that input name", pointing at a scene you created a line earlier.
    ap.add_argument("--scene", default="CHAT DEMO")
    ap.add_argument("--source", default="CHAT TEXT")
    a = ap.parse_args()

    if a.check:
        cmd_check(a)
    elif a.demo:
        cmd_demo(a)
    elif a.ask:
        cmd_ask(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

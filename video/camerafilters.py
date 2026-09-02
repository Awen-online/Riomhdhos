#!/usr/bin/env python3
"""
camerafilters - version the OBS camera filter settings, and put them back.

⚠️ WHY THIS EXISTS. Filter settings live inside OBS's scene collection, which is not in
this repository and not backed up with it. The background-removal tuning below took five
rounds of measurement to arrive at, and every bit of it would vanish with a corrupted
scene collection, a fresh OBS profile, or a move to another machine. Everything else the
rig depends on is version-controlled; this was the exception.

    python camerafilters.py --dump          # OBS -> camerafilters.json
    python camerafilters.py --apply         # camerafilters.json -> OBS
    python camerafilters.py --diff          # what differs, changing nothing

--dump is the authoring path: tune by hand in the OBS filter dialog while looking at the
picture, then dump. Hand-editing the JSON works too but the dialog gives you a preview.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import obsctl  # noqa: E402

STORE = HERE / "camerafilters.json"
SOURCES = ["Pixel 8", "Pixel 6 (vcam)"]


def dump(cl):
    out = {}
    for src in SOURCES:
        try:
            fl = cl.get_source_filter_list(src).filters
        except Exception as e:
            print(f"  {src}: {type(e).__name__} - skipped")
            continue
        out[src] = [{
            "name": f["filterName"],
            "kind": f["filterKind"],
            "index": f["filterIndex"],
            # ⚠️ ENABLED IS DELIBERATELY NOT STORED. Which filters are ON is a per-scene
            # decision owned by scenepresets.json, and re-applying a stored enabled-state
            # here would silently fight it. This file owns SETTINGS only.
            "settings": cl.get_source_filter(src, f["filterName"]).filter_settings,
        } for f in fl]
    return out


def apply(cl, data):
    steps = []
    for src, filters in data.items():
        for f in filters:
            try:
                existing = {x["filterName"] for x in cl.get_source_filter_list(src).filters}
            except Exception as e:
                steps.append(f"{src}: unreachable ({type(e).__name__})")
                break
            if f["name"] not in existing:
                cl.create_source_filter(src, f["name"], f["kind"], f["settings"])
                # A filter that did not exist is created DISABLED: this file does not own
                # enabled-state, and switching something on unannounced during a show is
                # the last thing it should do.
                cl.set_source_filter_enabled(src, f["name"], False)
                steps.append(f"{src}: created '{f['name']}' (disabled)")
            else:
                cl.set_source_filter_settings(src, f["name"], f["settings"], True)
                steps.append(f"{src}: '{f['name']}' settings restored")
    return steps


def diff(cl, data):
    live = dump(cl)
    n = 0
    for src, filters in data.items():
        got = {f["name"]: f["settings"] for f in live.get(src, [])}
        for f in filters:
            if f["name"] not in got:
                print(f"  {src}: '{f['name']}' MISSING in OBS")
                n += 1
                continue
            for k, v in f["settings"].items():
                if got[f["name"]].get(k) != v:
                    print(f"  {src}/{f['name']}: {k} = {got[f['name']].get(k)!r} "
                          f"(stored {v!r})")
                    n += 1
    print("  identical" if n == 0 else f"  {n} difference(s)")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dump", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--diff", action="store_true")
    args = ap.parse_args()

    cl = obsctl.connect(timeout=8)

    if args.dump:
        data = dump(cl)
        tmp = STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(STORE)
        total = sum(len(v) for v in data.values())
        print(f"  wrote {STORE.name}: {total} filter(s) across {len(data)} source(s)")
        return 0

    if not STORE.exists():
        sys.exit(f"{STORE.name} not found - run --dump first")
    data = json.loads(STORE.read_text(encoding="utf-8"))

    if args.diff:
        return 1 if diff(cl, data) else 0

    for s in apply(cl, data):
        print("  " + s)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())

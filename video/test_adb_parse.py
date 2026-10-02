#!/usr/bin/env python3
"""Pin how `adb devices -l` output is classified into USB versus wireless.

WHY THIS FILE EXISTS: that classification was wrong twice in one day, both times
silently, and both times the symptom was the opposite of the truth.

  1. The first version decided "cabled" meant "the serial has no colon", because
     wireless debugging keys a device as host:port. True, but not sufficient - once a
     phone is paired, adb's own mDNS auto-connect attaches it AGAIN under a service name
     that has no colon anywhere. So a phone on WiFi was taken for a cabled one, and the
     bridge set up an adb forward and tunnelled 16 Mbps of video over the very WiFi the
     USB path exists to avoid, while the log read "transport: USB".

  2. The fix tested for adb's own `usb:1-4.3` column instead - the property itself rather
     than a proxy for it. The platform-tools on this machine does not emit that column: a
     cabled Pixel 8 prints only `product: model: device: transport_id:`. So every device
     classified as wireless and the USB path went dead. It failed safe, which is exactly
     why nothing broke and nothing said so; plugging the cable in simply did nothing.

Both bugs are one function and a handful of strings. Neither was caught, because the only
way to exercise this code was to physically plug in a cable - so it got tested once, by
hand, on the day it was written. The fixtures below are real captures from this rig.

    python test_adb_parse.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vcambridge import parse_adb_devices                      # noqa: E402


# Verbatim from this machine: a cabled Pixel 8, a wireless Pixel 6, and the SAME Pixel 6
# again under adb's mDNS name. Note the cabled line carries no `usb:` column - that
# absence is the whole of bug 2 and the reason this fixture is copied rather than typed.
REAL = """List of devices attached
38021FDJH004KS         device product:shiba model:Pixel_8 device:shiba transport_id:4062
192.168.1.168:43141    device product:shiba model:Pixel_8 device:shiba transport_id:8
192.168.1.50:38533     device product:oriole model:Pixel_6 device:oriole transport_id:171
adb-1A061FDF600KVG-gdxOWk._adb-tls-connect._tcp device product:oriole model:Pixel_6 device:oriole transport_id:170
emulator-5562          offline transport_id:4068
"""

# An adb build that DOES emit the usb: column. Both signals must work.
WITH_USB_COLUMN = """List of devices attached
38021FDJH004KS         device usb:1-4.3 product:shiba model:Pixel_8 device:shiba
"""

STATES = """List of devices attached
38021FDJH004KS         unauthorized
192.168.1.50:38533     offline
1A061FDF600KVG         device
"""

EMPTY = "List of devices attached\n\n"

FAILS = []


def check(label, got, want):
    if got == want:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s\n          got  %r\n          want %r" % (label, got, want))
        FAILS.append(label)


def main():
    rows = parse_adb_devices(REAL)
    by = {r[0]: r for r in rows}

    check("cabled phone is USB, despite no usb: column  [bug 2]",
          by["38021FDJH004KS"][2], True)
    check("host:port is not USB",
          by["192.168.1.168:43141"][2], False)
    check("adb's mDNS service name is not USB  [bug 1]",
          by["adb-1A061FDF600KVG-gdxOWk._adb-tls-connect._tcp"][2], False)
    check("emulators are skipped entirely",
          [r for r in rows if r[0].startswith("emulator-")], [])
    check("the header line is not parsed as a device",
          [r for r in rows if r[0] == "List"], [])
    check("one row per adb entry, duplicates included",
          len(rows), 4)

    # Deduping the same handset reached two ways is find_usb_serial's job, not this
    # function's - it must report what adb said, so the caller can see the duplicate.
    check("the same phone twice is reported twice",
          len([r for r in rows if "1A061FDF600KVG" in r[0] or r[0] == "192.168.1.50:38533"]), 2)

    check("an explicit usb: column is honoured",
          parse_adb_devices(WITH_USB_COLUMN)[0][2], True)

    st = {r[0]: r[1] for r in parse_adb_devices(STATES)}
    check("unauthorized state is preserved, not dropped", st.get("38021FDJH004KS"), "unauthorized")
    check("offline state is preserved, not dropped", st.get("192.168.1.50:38533"), "offline")
    check("a bare hardware serial with no detail columns is USB",
          parse_adb_devices(STATES)[2][2], True)

    check("no devices attached yields no rows", parse_adb_devices(EMPTY), [])
    check("empty output does not raise", parse_adb_devices(""), [])

    print()
    if FAILS:
        print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

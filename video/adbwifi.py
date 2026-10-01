#!/usr/bin/env python3
"""Keep the rig's phones on adb over WiFi, addressed by SERIAL rather than by IP.

WHY THIS EXISTS, and why it is not a list of IP addresses:

adb's TLS pairing is a key exchange. The phone stores this host's public key and this host
stores the phone's, and that record already survives IP changes, reboots, port rotation
and the WiFi dropping. Pairing is NOT the fragile part and never was.

The fragile part is the ADDRESS. `adb connect` wants `host:port`, and both halves move:
DHCP can hand the phone a different address, and the wireless-debugging port is chosen
afresh every time the toggle is flipped - 38533 and 43141 today, something else tomorrow.
Anything that writes those numbers down is broken the first time either changes, and the
failure looks exactly like a lost pairing, which sends you to re-pair something that was
never unpaired.

⚠️ MAC ADDRESS IS THE WRONG KEY, though it is the obvious one to reach for. You cannot
`adb connect` to a MAC - you would resolve it to an IP through ARP and be back where you
started - and Android randomises its MAC per SSID by default, so it is not even reliably
stable. The device SERIAL is stable, is what adb already identifies devices by, and is
broadcast on the LAN: Android advertises `_adb-tls-connect._tcp` with a service name of
`adb-<SERIAL>-<tag>` pointing at its CURRENT address. So the serial resolves to wherever
the phone is right now, which is precisely the lookup that was missing.

⚠️ ALLOWLISTED BY SERIAL, PASSED IN, NOT HARDCODED. Two reasons. The rig goes to venues,
and on a strange LAN another Android with wireless debugging on will advertise the same
service - attempting every discovery would mean reaching for strangers' handsets. And
this file lives in a PUBLIC repository, so the serials belong in the scheduled task's
arguments next to the phone URLs, the same way vcambridge already takes them.

The serials themselves are deliberately not written down here - see the arguments of the
"Riomhdhos adb wifi" scheduled task, or run with --list to read them off the air.

Safe to run on a timer: connecting an already-connected device is a no-op, and a phone
that is off simply is not advertising.

    python adbwifi.py --serial <PIXEL6-SERIAL> --serial <PIXEL8-SERIAL>
    python adbwifi.py --list        # just show what is on the air
"""
import argparse
import datetime
import re
import subprocess
import sys

ADB = r"C:\Users\mccul\Android\Sdk\platform-tools\adb.exe"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# `adb-<SERIAL>-<tag>  _adb-tls-connect._tcp  <ip>:<port>`
SERVICE = re.compile(
    r"^(?P<name>adb-(?P<serial>[^-\s]+)-\S*)\s+"
    r"(?P<type>_adb-tls-\S+)\s+"
    r"(?P<host>[0-9.]+):(?P<port>\d+)\s*$")


def adb(*args, timeout=30):
    """Run adb. Returns (returncode, combined output). Never raises."""
    try:
        p = subprocess.run([ADB, *args], capture_output=True, text=True,
                           timeout=timeout, creationflags=NO_WINDOW)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)


def is_connect(d):
    """True for the connectable service.

    ⚠️ THE TYPE IS `_adb-tls-connect._tcp`, so it ENDS IN "._tcp", not in "connect" - an
    endswith("connect") test is false for every record and labels a perfectly connectable
    phone as sitting on the pairing screen. Caught only because the first run said both
    phones needed a pairing code while one of them was already connected.
    """
    return d["type"].startswith("_adb-tls-connect")


def discovered():
    """[{serial, host, port, type}] currently advertising on the LAN.

    Two service types exist and they are NOT interchangeable: `_adb-tls-connect` is a
    phone that will accept a connection from a host it has already paired with, and
    `_adb-tls-pairing` is the short-lived one the "Pair device with pairing code" dialog
    puts up. Only the first is connectable; seeing only the second means the phone is
    sitting on the pairing screen waiting for a code.
    """
    rc, out = adb("mdns", "services", timeout=25)
    found = []
    for line in out.splitlines():
        m = SERVICE.match(line.strip())
        if m:
            found.append({"serial": m.group("serial"), "host": m.group("host"),
                          "port": int(m.group("port")), "type": m.group("type")})
    return found


def connected():
    """{serial_or_address: state} for everything adb currently holds."""
    rc, out = adb("devices", timeout=20)
    seen = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            seen[parts[0]] = parts[1]
    return seen


def serial_of(address, cache={}):
    """Hardware serial behind a `host:port` adb address, or "" if it cannot be read.

    Needed because a phone attached over WiFi is keyed in `adb devices` by its address,
    not its serial - so "is this phone already connected?" cannot be answered by looking
    for the serial in that list, which is the obvious implementation and the wrong one.
    It would reconnect a phone on every pass, and each reconnect drops the existing
    session - taking the dashboard's battery and thermal readings down with it.
    """
    if address in cache:
        return cache[address]
    rc, out = adb("-s", address, "shell", "getprop", "ro.serialno", timeout=15)
    cache[address] = out.strip() if rc == 0 else ""
    return cache[address]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", action="append", default=[], metavar="SERIAL",
                    help="hardware serial to keep connected; repeatable")
    ap.add_argument("--list", action="store_true",
                    help="show what is advertising and exit, changing nothing")
    ap.add_argument("--log", metavar="FILE",
                    help="append output here as well as to stdout")
    args = ap.parse_args()

    # ⚠️ RUNS UNDER pythonw, WHICH HAS NO STDOUT AT ALL. The first version of the
    # scheduled task redirected with `cmd /c ... >> log` and produced an empty file every
    # time, because pythonw discards the stream before any redirect can see it - the task
    # looked like it was failing silently when it was in fact working silently. Same
    # approach vcambridge takes, for the same reason.
    if args.log:
        try:
            fh = open(args.log, "a", encoding="utf-8", buffering=1)

            class _Tee:
                def __init__(self, a, b): self.a, self.b = a, b

                def write(self, t):
                    for st in (self.a, self.b):
                        try:
                            if st: st.write(t)
                        except Exception:
                            pass

                def flush(self):
                    for st in (self.a, self.b):
                        try:
                            if st: st.flush()
                        except Exception:
                            pass

            sys.stdout = _Tee(sys.stdout, fh)
            print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass

    adb("start-server", timeout=30)
    air = discovered()

    if args.list or not args.serial:
        if not air:
            print("nothing advertising adb over WiFi")
            print("  on the phone: Developer options > Wireless debugging > on")
        for d in air:
            kind = "connectable" if is_connect(d) else "PAIRING SCREEN"
            print("  %-18s %s:%-6d %s" % (d["serial"], d["host"], d["port"], kind))
        if not args.serial:
            print("\nno --serial given, so nothing was connected")
        return 0

    have = connected()
    # Map the addresses adb already holds back to hardware serials, so an existing
    # session is recognised and left strictly alone.
    live = set()
    for addr, state in have.items():
        if state != "device":
            continue
        # ⚠️ THREE SHAPES, NOT TWO. A cabled phone is keyed by its serial, a connected one
        # by "host:port", and - once paired - adb's own mDNS auto-connect adds it AGAIN
        # under "adb-<SERIAL>-<tag>._adb-tls-connect._tcp". Miss that third shape and this
        # decides the phone is absent and connects it a second time, which is exactly how
        # the Pixel 6 ended up listed twice on the dashboard. The serial is right there in
        # the name, so read it rather than paying for a getprop.
        m = re.match(r"^adb-([^-]+)-", addr)
        if m:
            live.add(m.group(1))
        elif ":" in addr:
            s = serial_of(addr)
            if s:
                live.add(s)
        else:
            live.add(addr)              # plain USB: the key IS the serial

    # ⚠️ EXIT 0 UNLESS adb ITSELF IS BROKEN, and that is deliberate. A phone that is off,
    # off this network, or not yet paired is an ordinary resting state, not a fault in this
    # tool - and a task that reports failure every fifteen minutes for a normal condition
    # trains you to ignore its result, so the one time it means something you will not look.
    # The detail goes in the log and on the dashboard, which is where you would look anyway.
    rc_final = 0
    for want in args.serial:
        if want in live:
            print("  %-18s already connected" % want)
            continue
        hit = next((d for d in air if d["serial"] == want and is_connect(d)), None)
        if not hit:
            pairing = any(d["serial"] == want and not is_connect(d) for d in air)
            if pairing:
                print("  %-18s on the PAIRING screen - needs `adb pair <host>:<port> "
                      "<code>` with the code it is showing" % want)
            else:
                print("  %-18s not advertising (phone off, off this LAN, or wireless "
                      "debugging disabled)" % want)
            continue
        code, out = adb("connect", "%s:%d" % (hit["host"], hit["port"]), timeout=30)
        ok = "connected to" in out.lower()
        print("  %-18s %s:%-6d %s" % (want, hit["host"], hit["port"],
                                      "connected" if ok else "FAILED - " + out))
        if not ok:
            # The port accepting TCP while adb refuses is the pairing signature, not a
            # network fault, and it is worth saying so - the two look identical from here
            # and only one of them is fixed by checking the WiFi.
            print("  %-18s   if the port is open but this keeps failing, the pairing "
                  "record is gone: re-pair with a code from the handset" % "")
    return rc_final


if __name__ == "__main__":
    sys.exit(main())

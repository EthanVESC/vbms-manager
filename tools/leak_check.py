"""Fail a release build if the staged files contain personal data.

    python3 -I tools/leak_check.py <staged-dir>

Looks for fixed patterns (home paths, the LAN IPs the boards were tested on)
and for the real values in this computer's VBMS Manager data: board MACs,
nicknames, WiFi SSIDs/keys and BLE names/PINs from ~/.vbms_manager*/boards.json
and the config backups. Those values are only held in memory and never printed.
"""

import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zlib

# VESC defaults and other values that appear in every board config, plus
# trivial PINs (one is the pairing-code placeholder in the guide).
GENERIC = {"vesc bms", "vesc wifi", "vesc6wifi", "veschub.vedder.se", "express1", "vbms32", "00:00:00:00:00:00",
           "000000", "111111", "123456", "654321", "012345"}

FIXED = [
    # (/Users/Barney is an example path in CPython's own source)
    (re.compile(rb"/Users/(?!runner/|Shared/|Barney\b)[A-Za-z0-9._-]+"), "home folder path"),
]

# Personal patterns (your name, test LAN IPs) stay out of the source: one regex
# per line in ~/.vbms_manager-leak-patterns.txt
try:
    with open(os.path.expanduser("~/.vbms_manager-leak-patterns.txt"), "rb") as _f:
        FIXED += [(re.compile(l.strip()), "personal pattern") for l in _f if l.strip()]
except OSError:
    pass


def secrets():
    """Personal values from local VBMS Manager data, as (bytes, kind)."""
    home = os.path.expanduser("~")
    found = {}

    def add(v, kind):
        v = (v or "").strip()
        if len(v) >= 4 and v.lower() not in GENERIC:
            found[v] = kind

    for d in glob.glob(os.path.join(home, ".vbms_manager*")):
        try:
            with open(os.path.join(d, "boards.json")) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        for mac, prof in (data.get("boards") or {}).items():
            add(mac, "board MAC")
            add(mac.replace(":", ""), "board MAC")
            if isinstance(prof, dict):
                add(prof.get("nickname"), "board nickname")
                add(prof.get("last_host"), "board address")
        for h in data.get("hosts") or []:
            add(h.get("host") if isinstance(h, dict) else h, "board address")
        for x in glob.glob(os.path.join(d, "backups", "**", "*.xml"), recursive=True):
            try:
                root = ET.parse(x).getroot()
            except (OSError, ET.ParseError):
                continue
            for tag in ("wifi_sta_ssid", "wifi_sta_key", "wifi_ap_ssid", "wifi_ap_key", "ble_name", "ble_pin",
                        "tcp_hub_pass", "tcp_hub_id"):
                for el in root.iter(tag):
                    add(el.text, tag)
            for name in re.findall(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", os.path.basename(x)):
                add(name, "board MAC")
    return [(v.encode(), k) for v, k in found.items()]


def main(top):
    checks = [(re.compile(re.escape(v), re.I), k) for v, k in secrets()]
    print("leak check: %d values from local data, %d fixed patterns" % (len(checks), len(FIXED)))
    hits = 0
    for dirpath, dirnames, files in os.walk(top):
        for name in files + [d for d in dirnames]:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, top)
            blobs = [("name", rel.encode())]
            if name in files and not os.path.islink(p):
                with open(p, "rb") as f:
                    blobs.append(("content", f.read()))
                if name.endswith(".z"):  # Qt qCompress (4-byte length + zlib), as in the test fixtures
                    try:
                        blobs.append(("decompressed content", zlib.decompress(blobs[-1][1][4:])))
                    except zlib.error:
                        pass
            for where, blob in blobs:
                for rx, kind in FIXED + checks:
                    if rx.search(blob):
                        hits += 1
                        print("  LEAK: %s in %s of %s" % (kind, where, rel))
    if hits:
        sys.exit("leak check FAILED (%d hits)" % hits)
    print("leak check: clean")


if __name__ == "__main__":
    main(sys.argv[1])

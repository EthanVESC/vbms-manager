"""Capture a read-only snapshot of a board for tests and bug reports.

Usage:  python3 tools/capture.py 192.168.1.50 tests/fixtures/myboard
Writes: config.xml.z (as sent by the board), config.bin, bms_values.bin, fw.json
Only read commands are sent. Secrets in the config blob (WiFi keys, SSIDs, hub
password, BLE pin) are overwritten with same-length placeholders so every field
offset stays identical. The MAC is replaced too. Lisp code is not captured
unless --lisp is given (it is the board owner's code).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from vesc.client import VescClient  # noqa: E402
from vesc.confparams import T_INT, T_STRING  # noqa: E402

SCRUB = {"wifi_sta_ssid", "wifi_sta_key", "wifi_ap_ssid", "wifi_ap_key", "tcp_hub_pass", "ble_pin"}


def scrub_blob(cfg):
    out = bytearray(cfg.blob)
    for name in SCRUB:
        if name not in cfg.spans:
            continue
        a, b = cfg.spans[name]
        fd = cfg.schema.fields[name]
        if fd.type == T_STRING:
            out[a:b - 1] = b"x" * (b - 1 - a)
        elif fd.type == T_INT:
            out[a:b] = bytes(b - a)
    return bytes(out)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    host, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    c = VescClient(host)
    fw = c.connect()
    fw["mac"] = "00:00:00:00:00:00"
    fw["uuid"] = "0" * 24
    from vesc.client import COMM_GET_CUSTOM_CONFIG_XML
    xml_z = c._read_chunked(COMM_GET_CUSTOM_CONFIG_XML, b"\x00", 10)
    from vesc.confparams import ConfigSchema
    schema = ConfigSchema.from_compressed(xml_z)
    cfg = schema.decode(c.get_config_blob())
    vals = c.get_bms_values_raw()
    lisp = c.read_lisp_code() if "--lisp" in sys.argv else None
    c.close()
    with open(os.path.join(outdir, "config.xml.z"), "wb") as f:
        f.write(xml_z)
    with open(os.path.join(outdir, "config.bin"), "wb") as f:
        f.write(scrub_blob(cfg))
    with open(os.path.join(outdir, "bms_values.bin"), "wb") as f:
        f.write(vals)
    with open(os.path.join(outdir, "fw.json"), "w") as f:
        json.dump(fw, f, indent=2)
    if lisp is not None:
        with open(os.path.join(outdir, "script.lisp"), "wb") as f:
            f.write(lisp)
    print("captured %s -> %s (roundtrip %s)" % (host, outdir, cfg.roundtrip_ok()))


if __name__ == "__main__":
    sys.exit(main())

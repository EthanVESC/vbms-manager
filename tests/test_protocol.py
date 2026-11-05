import math
import os
import random
import struct
import unittest
import xml.etree.ElementTree as ET

from tests import mock_bms  # noqa: F401  (sets sys.path)
from vesc import packet
from vesc.bms_values import parse_bms_values
from vesc.buffer import float32_auto_decode, float32_auto_encode
from vesc.confparams import SECRET_FIELDS, ConfigError, ConfigSchema

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    d = os.path.join(FIX, name)
    xml_z = open(os.path.join(d, "config.xml.z"), "rb").read()
    blob = open(os.path.join(d, "config.bin"), "rb").read()
    vals = open(os.path.join(d, "bms_values.bin"), "rb").read()
    ref = ET.parse(os.path.join(d, "vesc_tool_export.xml")).getroot()
    return ConfigSchema.from_compressed(xml_z), blob, vals, ref


class TestPacket(unittest.TestCase):
    def test_crc_vector(self):
        self.assertEqual(packet.crc16(b"123456789"), 0x31C3)  # CRC16-XMODEM check value

    def test_roundtrip_sizes(self):
        for n in (1, 255, 256, 1000, 70000):
            pl = bytes(random.getrandbits(8) for _ in range(n))
            out = packet.Parser(max_len=1 << 20).feed(packet.encode(pl))
            self.assertEqual(out, [pl])

    def test_resync_and_split(self):
        p = packet.Parser()
        a, b = packet.encode(b"\x00hello"), packet.encode(b"\x60" * 300)
        stream = b"\xff\x02garbage" + a + b
        got = []
        for i in range(0, len(stream), 7):
            got += p.feed(stream[i:i + 7])
        self.assertEqual(got, [b"\x00hello", b"\x60" * 300])

    def test_bad_crc_dropped(self):
        f = bytearray(packet.encode(b"\x00abc"))
        f[-2] ^= 0xFF
        p = packet.Parser()
        self.assertEqual(p.feed(bytes(f)), [])
        self.assertEqual(p.feed(packet.encode(b"\x00ok")), [b"\x00ok"])


class TestFloat32Auto(unittest.TestCase):
    def test_matches_ieee_for_normals(self):
        for v in (4.21, 4.11, 3.975, 4.075, 3.87, 3.97, 0.1, -12.5, 1e-5, 1234.5678, 3.1, 4.2):
            self.assertEqual(float32_auto_encode(v), struct.pack(">f", v), v)

    def test_roundtrip(self):
        for v in (0.0, 4.21, -3.3, 1e-30, 6e20):
            f32 = struct.unpack(">f", struct.pack(">f", v))[0]
            self.assertEqual(float32_auto_decode(float32_auto_encode(v)), f32)

    def test_subnormal_to_zero(self):
        self.assertEqual(float32_auto_encode(1e-39), bytes(4))

    def test_rejects_nan(self):
        with self.assertRaises(ValueError):
            float32_auto_encode(math.nan)


class TestConfig(unittest.TestCase):
    def test_fixtures_match_vesc_tool_exports(self):
        # The reference XMLs were saved by VESC Tool, independently of this code.
        for name in ("vbms32_18s", "vbms32_32s"):
            schema, blob, _, ref = load(name)
            cfg = schema.decode(blob)
            self.assertEqual(len(schema.order), 54)
            for fname, val in cfg.values.items():
                if fname in SECRET_FIELDS or fname in ("wifi_sta_ssid", "wifi_ap_ssid"):
                    continue
                r = ref.findtext(fname)
                self.assertIsNotNone(r, fname)
                if isinstance(val, float):
                    self.assertAlmostEqual(val, float(r), delta=1e-6 * max(1, abs(float(r))), msg="%s %s" % (name, fname))
                elif isinstance(val, int):
                    self.assertEqual(val, int(float(r)), "%s %s" % (name, fname))
                else:
                    self.assertEqual(val, r or "", "%s %s" % (name, fname))

    def test_known_values(self):
        cfg = load("vbms32_18s")[0].decode(load("vbms32_18s")[1])
        v = cfg.values
        self.assertEqual((v["cells_ic1"], v["cells_ic2"], v["controller_id"]), (14, 4, 10))
        self.assertAlmostEqual(v["vc_charge_start"], 3.87, places=5)
        self.assertAlmostEqual(v["vc_charge_end"], 3.97, places=5)
        cfg = load("vbms32_32s")[0].decode(load("vbms32_32s")[1])
        self.assertEqual((cfg.values["cells_ic1"], cfg.values["cells_ic2"]), (16, 16))

    def test_roundtrip_exact(self):
        for name in ("vbms32_18s", "vbms32_32s"):
            schema, blob, _, _ = load(name)
            self.assertTrue(schema.decode(blob).roundtrip_ok())

    def test_patch_changes_only_two_fields(self):
        schema, blob, _, _ = load("vbms32_32s")
        cfg = schema.decode(blob)
        new = cfg.patch_float32_auto({"vc_charge_start": 4.11, "vc_charge_end": 4.21})
        self.assertEqual(len(new), len(blob))
        diff = [i for i in range(len(blob)) if blob[i] != new[i]]
        allowed = set(range(*cfg.spans["vc_charge_start"])) | set(range(*cfg.spans["vc_charge_end"]))
        self.assertTrue(set(diff) <= allowed)
        after = schema.decode(new)
        for n in schema.order:
            if n not in ("vc_charge_start", "vc_charge_end"):
                self.assertEqual(after.values[n], cfg.values[n], n)
        self.assertAlmostEqual(after.values["vc_charge_end"], 4.21, places=5)
        self.assertAlmostEqual(after.values["vc_charge_start"], 4.11, places=5)

    def test_patch_refuses_other_encodings(self):
        schema, blob, _, _ = load("vbms32_18s")
        cfg = schema.decode(blob)
        for bad in ("t_charge_max", "cells_ic1", "ble_name", "nope"):
            with self.assertRaises(ConfigError):
                cfg.patch_float32_auto({bad: 1.0})

    def test_truncated_blob_rejected(self):
        schema, blob, _, _ = load("vbms32_18s")
        with self.assertRaises(ValueError):
            schema.decode(blob[:-3])
        with self.assertRaises(ConfigError):
            schema.decode(blob + b"\x00")

    def test_xml_export_matches_vesc_tool(self):
        for name in ("vbms32_18s", "vbms32_32s"):
            schema, blob, _, _ = load(name)
            ours = ET.fromstring(schema.decode(blob).to_vesc_tool_xml())
            ref = ET.parse(os.path.join(FIX, name, "vesc_tool_export.xml")).getroot()
            for el in ref:
                if el.tag in ("wifi_sta_ssid", "wifi_ap_ssid"):
                    continue  # scrubbed differently in the fixture
                self.assertEqual(ours.findtext(el.tag), el.text or "", "%s %s" % (name, el.tag))

    def test_xml_export_order_matches_vesc_tool(self):
        # The fixture exports are VESC Tool's own files with secrets scrubbed; field order is unchanged.
        for name in ("vbms32_18s", "vbms32_32s"):
            with open(os.path.join(FIX, name, "vesc_tool_export.xml"), encoding="utf-8") as f:
                ref = f.read()
            order = [l.strip().split(">")[0][1:] for l in ref.splitlines()[4:-1]]
            schema, blob, _, _ = load(name)
            self.assertEqual(schema.xml_order, order)

    def test_export_redacts_secrets(self):
        schema, blob, _, _ = load("vbms32_18s")
        x = ET.fromstring(schema.decode(blob).to_vesc_tool_xml())
        self.assertEqual(x.findtext("wifi_sta_key"), "")
        self.assertEqual(x.findtext("ble_pin"), "0")


class TestBmsValues(unittest.TestCase):
    def test_parse(self):
        for name, n, can in (("vbms32_18s", 18, 10), ("vbms32_32s", 32, 11)):
            v = parse_bms_values(load(name)[2])
            self.assertEqual(len(v["cells"]), n)
            self.assertEqual(len(v["balancing"]), n)
            self.assertEqual(v["can_id"], can)
            self.assertTrue(all(2.5 < c < 4.3 for c in v["cells"]))
            self.assertAlmostEqual(v["v_tot"], sum(v["cells"]), delta=0.05 * n)
            self.assertEqual(v["data_version"], 1)
            self.assertIn("IC", [t["name"] for t in v["temps"]])


if __name__ == "__main__":
    unittest.main()

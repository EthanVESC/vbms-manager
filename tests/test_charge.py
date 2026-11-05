import os
import struct
import unittest

from tests import mock_bms
import cells
import charge_limit as cl
import script_check

DEV = {"vc_charge_min": 2.0, "vc_balance_min": 3.5}


def generic():
    data = cells.load()
    c = data["by_id"]["reliance_rs50"]
    return data["curves"][c["curve"]]["points"], c["max_end"]


def f32(x):
    return struct.unpack(">f", struct.pack(">f", x))[0]


class TestChargeLimit(unittest.TestCase):
    def test_every_step(self):
        pts, me = generic()
        prev = 0
        for p in range(60, 101):
            r = cl.compute(p, pts, me, DEV)
            self.assertAlmostEqual(r["end"] - r["start"], 0.100, places=9)
            self.assertLessEqual(r["end"], me)
            self.assertLessEqual(r["end"], cl.ABS_MAX_END)
            self.assertGreater(r["end"], prev)  # strictly increasing
            prev = r["end"]
            # Gap still OK after float32 storage on the BMS
            self.assertTrue(cl.gap_ok(f32(r["start"]), f32(r["end"])))

    def test_100_is_421_411(self):
        pts, me = generic()
        r = cl.compute(100, pts, me, DEV)
        self.assertEqual((r["end"], r["start"]), (4.21, 4.11))

    def test_rejects_bad_pct(self):
        pts, me = generic()
        for bad in (59, 101, 80.5, "80", True, None, -1):
            with self.assertRaises(cl.LimitError, msg=repr(bad)):
                cl.compute(bad, pts, me, DEV)

    def test_hard_limits(self):
        with self.assertRaises(cl.LimitError):
            cl.check(4.16, 4.26, 4.30, DEV)            # above absolute
        with self.assertRaises(cl.LimitError):
            cl.check(4.12, 4.22, 4.21, DEV)            # above profile max
        with self.assertRaises(cl.LimitError):
            cl.check(3.99, 4.2, 4.21, DEV)             # gap 0.21
        with self.assertRaises(cl.LimitError):
            cl.check(4.16, 4.2, 4.21, DEV)             # gap 0.04
        with self.assertRaises(cl.LimitError):
            cl.check(3.75, 3.85, 4.21, {"vc_charge_min": 3.8, "vc_balance_min": 0})
        cl.check(3.75, 3.85, 4.21, {"vc_charge_min": 2.0, "vc_balance_min": 3.8})  # Balance Min above start: allowed
        with self.assertRaises(cl.LimitError):
            cl.check(3.4, 3.5, 4.21, DEV)              # end below 3.6
        cl.check(4.11, 4.21, 4.21, DEV)                # the 100% pair is fine
        cl.check(4.0, 4.2, 4.21, DEV)                  # a custom gap inside 0.050-0.200 V is allowed
        cl.check(4.15, 4.2, 4.21, DEV)
        self.assertFalse(cl.gap_ok(4.0, 4.2))
        self.assertTrue(cl.gap_valid(4.0, 4.2))

    def test_balance_note(self):
        self.assertIsNone(cl.balance_note(4.11, 4.21, 3.5))
        self.assertIsNone(cl.balance_note(4.11, 4.21, 4.11))
        self.assertIn("near the top", cl.balance_note(3.9, 4.0, 3.95))
        self.assertIn("won't balance", cl.balance_note(3.9, 4.0, 4.1))

    def test_curve_must_stay_below_max(self):
        pts, _ = generic()
        with self.assertRaises(cl.LimitError):          # curve passes 4.10 before 100%
            cl.compute(99, pts, 4.10, DEV)

    def test_bad_curves(self):
        for curve in ([[60, 3.9], [100, 3.8]],                 # decreasing
                      [[70, 3.9], [100, 4.2]],                 # doesn't cover 60
                      [[60, 3.9], [90, 4.1]],                  # doesn't cover 100
                      [[60, 3.9], [60, 4.0], [100, 4.2]],      # duplicate pct
                      [[60, 2.0], [100, 4.2]],                 # absurd voltage
                      [[60, "x"], [100, 4.2]], "nope", []):
            with self.assertRaises(cl.LimitError, msg=repr(curve)):
                cl.validate_curve(curve, 4.21)

    def test_pct_for_end(self):
        pts, me = generic()
        self.assertEqual(cl.pct_for_end(4.21, pts, me), 100.0)
        for p in (60, 73, 87, 95):
            self.assertAlmostEqual(cl.pct_for_end(cl.compute(p, pts, me, DEV)["end"], pts, me), p, delta=0.6)

    def test_pct_for_end_float32(self):
        # The board stores the 60% end as float32 and reads back e.g. 3.8229999: still 60%, not "below the curve".
        pts, me = cells.resolve({"cell_id": "reliance_rs50"})[1:3]
        end = cl.compute(60, pts, me, DEV)["end"]
        self.assertNotEqual(f32(end), end)
        self.assertEqual(cl.pct_for_end(f32(end), pts, me), 60.0)

    def test_profiles_valid(self):
        data = cells.load()
        for c in data["cells"]:
            self.assertIn(c["curve"], data["curves"])
            cl.validate_curve(data["curves"][c["curve"]]["points"], c["max_end"])
            self.assertLessEqual(c["max_end"], 4.21)

    def test_bundled_curves_are_full_range_and_sourced(self):
        for cid, c in cells.load()["curves"].items():
            pts = c["points"]
            self.assertEqual([p for p, _ in pts], list(range(0, 101, 5)), cid)
            self.assertEqual(pts[0][1], 2.80, cid)    # 0% = 2.8 V cut-off
            self.assertEqual(pts[-1][1], 4.20, cid)   # 100% = rated charge voltage
            if c["verified"]:
                self.assertRegex(c["source"], r"https?://", cid)


class TestScriptCheck(unittest.TestCase):
    def test_stock_like(self):
        r = script_check.analyze(mock_bms.STOCK_LIKE_SCRIPT + b"\x00\x00")
        self.assertTrue(r["verified"], r["problems"])
        self.assertTrue(r["wake_path_uses_end"])
        self.assertFalse(r["unexpected"])

    def test_extra_reference_blocks(self):
        code = mock_bms.STOCK_LIKE_SCRIPT + b"(setq x (bms-get-param 'vc_charge_end))\n"
        r = script_check.analyze(code)
        self.assertFalse(r["verified"])
        self.assertEqual(len(r["unexpected"]), 1)

    def test_swapped_hysteresis_blocks(self):
        code = mock_bms.STOCK_LIKE_SCRIPT.replace(b"'vc_charge_end)\n                    (bms-get-param 'vc_charge_start)",
                                                  b"'vc_charge_start)\n                    (bms-get-param 'vc_charge_end)")
        self.assertFalse(script_check.analyze(code)["verified"])

    def test_comment_mentions_ignored(self):
        code = mock_bms.STOCK_LIKE_SCRIPT + b"; vc_charge_end is the cutoff\n"
        self.assertTrue(script_check.analyze(code)["verified"])

    def test_empty(self):
        self.assertFalse(script_check.analyze(b"")["verified"])

    def test_real_scripts_if_present(self):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "private")
        found = 0
        for b in (sorted(os.listdir(d)) if os.path.isdir(d) else []):
            p = os.path.join(d, b, "script.lisp")
            if os.path.exists(p):
                found += 1
                r = script_check.analyze(open(p, "rb").read())
                self.assertTrue(r["verified"], (b, r["problems"], r["unexpected"]))
                self.assertTrue(r["wake_path_uses_end"])
        if not found:
            self.skipTest("private board scripts not present")


if __name__ == "__main__":
    unittest.main()

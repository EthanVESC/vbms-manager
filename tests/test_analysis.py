"""Analysis over seeded history: daily totals, drift flags, self-discharge across sleep gaps, capacity."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAC = "02:00:00:00:00:09"


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls._old = os.environ.get("VBMS_MANAGER_HOME")
        os.environ["VBMS_MANAGER_HOME"] = cls.home
        # Seeded: cell 7 drifts low, cell 12 leaks faster while the BMS sleeps (02:00-07:00 gaps).
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "seed_history.py"), MAC, "--days", "20",
                        "--cells", "16", "--interval", "60"], check=True, capture_output=True, env=os.environ.copy())
        import analysis
        cls.r = analysis.report(MAC, None, 15.0, days=90)

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("VBMS_MANAGER_HOME", None)
        else:
            os.environ["VBMS_MANAGER_HOME"] = cls._old
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_daily_totals(self):
        d = self.r["daily"]
        self.assertEqual(len(d), 20)
        day = d[0]
        self.assertAlmostEqual(day["ah_out"], 10.0, delta=0.1)        # 1 h at 10 A
        self.assertAlmostEqual(day["wh_in"], day["wh_in_bms"], delta=1)
        self.assertGreater(day["coverage"], 0.7)
        self.assertEqual(day["charges"], 1)

    def test_drift_flags_seeded_cells(self):
        flags = {(f["cell"], f["kind"]) for f in self.r["drift"]["flags"]}
        self.assertIn((7, "sagging"), flags)
        self.assertIn((12, "sagging"), flags)
        self.assertEqual(self.r["drift"]["weakest"], 12)
        self.assertNotIn((1, "sagging"), flags)

    def test_self_discharge_across_gaps(self):
        sd = self.r["self_discharge"]
        self.assertTrue(sd["available"])
        self.assertTrue(sd["events"])
        self.assertTrue(all(e["hours"] >= 6 for e in sd["events"]))
        self.assertEqual([f["cell"] for f in sd["flags"]], [12])

    def test_capacity_and_health(self):
        cap = self.r["capacity"]
        self.assertAlmostEqual(cap["estimate_ah"], 14.2, delta=0.3)
        self.assertEqual(cap["nominal_ah"], 15.0)
        h = self.r["health"]
        self.assertEqual(h["charges_logged"], 20)
        self.assertGreater(h["rest_spread_change"], 5)


if __name__ == "__main__":
    unittest.main()

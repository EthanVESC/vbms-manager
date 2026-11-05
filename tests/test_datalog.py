"""Recording: settings, bucketing, levels, gzip rollover, retention, queries and CSV."""

import gzip
import json
import os
import shutil
import tempfile
import time
import unittest

MAC = "02:00:00:00:00:01"


def row(t, i=1.0, cells=(3.9, 3.95), chg=False):
    v = sum(cells)
    return {"t": t, "v": v, "i": i, "p": round(v * i, 1), "soc": 0.5, "cmin": min(cells), "cmax": max(cells),
            "d": round((max(cells) - min(cells)) * 1000, 1), "tmax": 25.0, "vc": 0.0, "chg": chg, "bal": False,
            "c": list(cells), "tp": [25.0, 24.0], "tpn": ["IC", "Cell sensor 1"], "bm": 0, "ah": 0, "wh": 0,
            "hum": 40, "soh": 1.0, "ahc": 10.0, "whc": 800.0, "ahd": 9.0, "whd": 700.0}


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old = os.environ.get("VBMS_MANAGER_HOME")
        os.environ["VBMS_MANAGER_HOME"] = self.home
        global datalog
        import datalog

    def tearDown(self):
        if self._old is None:
            os.environ.pop("VBMS_MANAGER_HOME", None)
        else:
            os.environ["VBMS_MANAGER_HOME"] = self._old
        shutil.rmtree(self.home, ignore_errors=True)

    def rec(self, **st):
        cfg = dict(datalog.DEFAULTS, **st)
        return datalog.Recorder(MAC, lambda: cfg)

    def rows(self, day=None):
        files = datalog.day_files(MAC)
        out = []
        for d in sorted(files):
            if day is None or d == day:
                out += list(datalog.iter_rows(files[d]))
        return out


class Settings(Base):
    def test_defaults_and_validation(self):
        self.assertEqual(datalog.settings_from({}), datalog.DEFAULTS)
        s = datalog.clean_settings({"interval_s": "30", "level": "summary", "retention_days": 0, "enabled": 0})
        self.assertEqual(s, {"enabled": False, "interval_s": 30, "level": "summary", "retention_days": 0})
        for bad in ({"interval_s": 7}, {"level": "verbose"}, {"retention_days": 12}, {"interval_s": "x"}):
            with self.assertRaises(ValueError):
                datalog.clean_settings(bad)
        self.assertEqual(datalog.settings_from({"logging": {"interval_s": 7}}), datalog.DEFAULTS)


class Recording(Base):
    def test_buckets_are_aligned_and_combined(self):
        r = self.rec(interval_s=10)
        t0 = 1_700_000_000  # multiple of 10
        for k in range(25):
            r.add(row(t0 + k, i=float(k), cells=(3.9, 3.9 + k / 1000)))
        r.flush()
        rows = self.rows()
        self.assertEqual([x[1]["n"] for x in rows], [10, 10, 5])
        first = rows[0][1]
        self.assertAlmostEqual(first["i"], 4.5)          # average
        self.assertEqual(first["cmax"], 3.909)           # extreme kept
        self.assertEqual(rows[0][0]["interval_s"], 10)

    def test_levels_choose_fields(self):
        t0 = 1_700_000_000
        for level, has_cells in (("summary", False), ("detailed", True)):
            shutil.rmtree(datalog.board_dir(MAC), ignore_errors=True)
            r = self.rec(interval_s=1, level=level)
            r.add(row(t0))
            r.add(row(t0 + 1))
            r.flush()
            hdr, first = self.rows()[0]
            self.assertEqual("c" in first, has_cells, level)
            self.assertEqual(hdr["level"], level)

    def test_disabled_writes_nothing(self):
        r = self.rec(enabled=False)
        r.add(row(1_700_000_000))
        r.flush()
        self.assertEqual(datalog.day_files(MAC), {})

    def test_interval_change_writes_new_header(self):
        cfg = dict(datalog.DEFAULTS, interval_s=10)
        r = datalog.Recorder(MAC, lambda: cfg)
        t0 = 1_700_000_000
        for k in range(10):
            r.add(row(t0 + k))
        cfg["interval_s"] = 1
        for k in range(10, 13):
            r.add(row(t0 + k))
        r.flush()
        hdrs = {h["interval_s"] for h, _ in self.rows()}
        self.assertEqual(hdrs, {10, 1})


class Files(Base):
    def _write_day(self, day_start, n=5):
        r = self.rec(interval_s=1)
        for k in range(n):
            r.add(row(day_start + 3600 + k))
        r.flush()

    def test_compress_and_prune(self):
        today0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        for back in (40, 2, 0):
            self._write_day(today0 - back * 86400)
        datalog.compress_old(MAC)
        files = datalog.day_files(MAC)
        gz = sorted(d for d, p in files.items() if p.endswith(".gz"))
        self.assertEqual(len(gz), 2)
        self.assertEqual(len(self.rows(gz[0])), 5)       # still readable after gzip
        with gzip.open(files[gz[0]], "rt") as f:
            self.assertEqual(json.loads(f.readline())["hdr"], 1)
        self.assertEqual(datalog.prune(MAC, 0), 0)       # forever keeps all
        datalog.prune(MAC, 30)
        self.assertEqual(len(datalog.day_files(MAC)), 2)

    def test_query_downsamples_and_merges_live(self):
        now = time.time()
        t0 = int(now - 3000)
        r = self.rec(interval_s=1)
        for k in range(1200):
            r.add(row(t0 + k, i=2.0))
        r.flush()
        live = [row(t0 + 2000 + k, i=-3.0) for k in range(60)]
        q = datalog.query(MAC, t0, now, "current", max_points=100, live=live, now=now)
        pts = q["series"][0]["pts"]
        self.assertLessEqual(len(pts), 101)
        self.assertEqual(pts[0][1], 2.0)
        self.assertEqual(pts[-1][1], -3.0)
        # The hole between file rows and live rows is wider than the gap threshold.
        self.assertTrue(any(b[0] - a[0] > q["gap"] for a, b in zip(pts, pts[1:])))
        with self.assertRaises(ValueError):
            datalog.query(MAC, t0, now, "nope")

    def test_cells_metric_and_csv(self):
        now = time.time()
        t0 = int(now - 600)
        r = self.rec(interval_s=1)
        for k in range(30):
            r.add(row(t0 + k))
        r.flush()
        q = datalog.query(MAC, t0, now, "cells", cell="all", now=now)
        self.assertEqual([s["name"] for s in q["series"]], ["cell 1", "cell 2"])
        text = "".join(datalog.csv_rows(MAC, t0, now))
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 31)
        self.assertIn("cell2_V", lines[0])


if __name__ == "__main__":
    unittest.main()

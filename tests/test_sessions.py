"""Charge sessions: stop reasons, Ah/Wh integration, joining late, capacity from rest voltages, ETA."""

import unittest

import sessions

CURVE = [(0, 3.0), (100, 4.2)]  # linear: 1 % per 12 mV
CTX = {"end": 4.10, "start": 4.00, "soc_pts": CURVE}


def s(t, i=0.0, chg=False, cell=3.6, vc=0.0, n=4, ahc=100.0, ahd=50.0):
    v = cell * n
    return {"t": float(t), "v": v, "i": i, "p": v * i, "soc": 0.5, "cmin": cell, "cmax": cell, "d": 0.0,
            "tmax": 25.0, "vc": vc, "chg": chg, "bal": False, "c": [cell] * n, "tp": [25.0], "tpn": ["Cell sensor 1"],
            "ahc": ahc, "whc": ahc * v, "ahd": ahd, "whd": ahd * v}


class Tracker(unittest.TestCase):
    def setUp(self):
        self.closed, self.updates = [], []
        self.tr = sessions.ChargeTracker(self.closed.append, self.updates.append)

    def feed(self, samples, ctx=CTX):
        for x in samples:
            self.tr.update(x, ctx)

    def charge(self, t0, secs, i=5.0, cell=3.8, **kw):
        return [s(t0 + k, i=i, chg=True, cell=cell, vc=20.0, **kw) for k in range(secs)]

    def test_integrates_and_stops_unplugged(self):
        self.feed([s(0)] + [s(1)])
        self.feed(self.charge(2, 3601))
        self.feed([s(3603, vc=0.0)])
        r = self.closed[0]
        self.assertEqual(r["reason"], "unplugged")
        self.assertAlmostEqual(r["ah"], 5.0, places=2)
        self.assertAlmostEqual(r["wh"], 5.0 * 3.8 * 4, delta=1)
        self.assertFalse(r["joined_late"])

    def test_reached_limit(self):
        self.feed([s(0)])
        self.feed(self.charge(1, 60, cell=4.095))
        self.feed([s(61, vc=20.0, cell=4.095)])
        self.assertEqual(self.closed[0]["reason"], "limit")

    def test_paused_fault_and_temperature(self):
        for ctx, want in (({"paused": True}, "paused"), ({"fault": True}, "fault"), ({"t_max": 20}, "temperature")):
            self.setUp()
            self.feed([s(0)])
            self.feed(self.charge(1, 30))
            self.tr.update(s(31, vc=20.0), dict(CTX, **ctx))
            self.assertEqual(self.closed[0]["reason"], want, want)

    def test_joined_late_and_offline(self):
        self.feed(self.charge(0, 30))
        self.tr.disconnected(40)
        r = self.closed[0]
        self.assertTrue(r["joined_late"])
        self.assertEqual(r["reason"], "offline")

    def test_gap_not_integrated(self):
        self.feed([s(0)])
        self.feed(self.charge(1, 10) + self.charge(100, 10))
        self.feed([s(110)])
        r = self.closed[0]
        self.assertEqual(r["gaps"], 1)
        self.assertLess(r["ah"], 5.0 * 30 / 3600)

    def test_capacity_from_rest_voltages(self):
        rest0, rest1 = 3.6, 3.84          # 50 % -> 70 % on the linear curve
        self.feed([s(k * 10, cell=rest0) for k in range(130)])          # 21+ min rest
        t = 1300
        self.feed(self.charge(t, 1440, i=5.0, ahc=100.0))               # 2 Ah
        t += 1440
        self.feed([s(t + k * 10, cell=rest1, ahc=102.0) for k in range(200)])  # 33 min rest after
        r = self.closed[0]
        self.assertAlmostEqual(r["rest_before"]["soc"], 50.0, places=1)
        self.assertEqual(len(self.updates), 1)
        self.assertAlmostEqual(self.updates[0]["capacity_ah"], 10.0, delta=0.3)

    def test_live_and_eta(self):
        self.feed([s(0)])
        pts = [s(1 + k, i=5.0, chg=True, cell=4.0 + k * 0.00005, vc=20.0) for k in range(600)]
        self.feed(pts)
        live = self.tr.live()
        self.assertGreater(live["ah"], 0.8)
        # 3 mV/min and ~70 mV left: about 23 minutes.
        self.assertAlmostEqual(live["eta"], (4.10 - pts[-1]["cmax"]) / 0.00005, delta=60)
        self.assertIsNone(sessions.eta([(0, 4.0, 5)] * 40, 4.1, 60))   # too early

    def test_soc_curve_bounds(self):
        self.assertIsNone(sessions.soc_from_curve(2.9, CURVE))
        self.assertAlmostEqual(sessions.soc_from_curve(3.6, CURVE), 50.0)


if __name__ == "__main__":
    unittest.main()

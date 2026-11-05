"""Safety notices (injected clock) and the low-SOC rest -> check-in cycle against the mock BMS."""

import time
import unittest

from tests import mock_bms
from tests.test_write import Base, wait

import safety  # noqa: E402
import storage  # noqa: E402
from vesc.bms_values import parse_bms_values  # noqa: E402

DAY = 86400


class FakeBoards:
    def __init__(self):
        self.p, self.s = {}, {}

    def get(self, mac):
        return dict(self.p)

    def update(self, mac, **kw):
        for k, v in kw.items():
            if v is None:
                self.p.pop(k, None)
            else:
                self.p[k] = v
        return dict(self.p)

    def settings(self):
        return dict(self.s)


class FakeBoard:
    def __init__(self):
        self.mac, self.state, self.values, self.notices = "02:00", "online", None, []
        self.boards, self.events, self.rest = FakeBoards(), [], None

    def profile(self):
        return self.boards.get(self.mac)

    def resting(self):
        return self.rest

    def _event(self, kind, text, **kw):
        self.events.append((kind, text, kw))


def vals(cell, n=18, soc=0.5, charging=False, v_charge=0.0):
    return {"v_tot": cell * n, "soc": soc, "cell_min": cell, "cell_max": cell, "cells": [cell] * n,
            "is_charging": charging, "v_charge": v_charge}


class TestNotices(unittest.TestCase):
    def keys(self, b, t):
        return {x["key"] for x in safety.evaluate(b, now=t)}

    def test_below_nominal_after_days(self):
        b = FakeBoard()
        t0 = time.time()
        b.values = vals(3.55, soc=0.3)
        self.assertEqual(self.keys(b, t0), set())
        # the board goes to sleep: last-known values carry on
        b.boards.update(b.mac, last_seen=safety.seen_record(b.values, t0))
        b.state, b.values = "resting", None
        self.assertEqual(self.keys(b, t0 + 2 * DAY), set())
        self.assertIn("below_nominal", self.keys(b, t0 + 3 * DAY + 1))
        self.assertEqual([e[0] for e in b.events], ["notice"])          # logged once
        self.keys(b, t0 + 3 * DAY + 60)
        self.assertEqual(len(b.events), 1)
        # a charge clears it
        b.state, b.values = "online", vals(3.9, soc=0.6, charging=True, v_charge=75)
        self.assertEqual(self.keys(b, t0 + 4 * DAY), set())
        self.assertNotIn("below_nominal", b.profile().get("safety_state", {}))

    def test_low_cell_and_full(self):
        b = FakeBoard()
        t0 = time.time()
        b.values = vals(3.1, soc=0.03)
        self.assertIn("low_cell", self.keys(b, t0))
        b.values = vals(4.15, soc=0.95)
        self.assertEqual(self.keys(b, t0), set())
        self.assertIn("stored_full", self.keys(b, t0 + 7 * DAY + 1))
        b.boards.update(b.mac, storage={"on": True})                   # storage mode silences it
        self.assertEqual(self.keys(b, t0 + 8 * DAY), set())

    def test_switches(self):
        b = FakeBoard()
        b.boards.s["safety"] = {"low_cell_on": False}
        b.values = vals(3.1, soc=0.03)
        self.assertEqual(self.keys(b, time.time()), set())

    def test_clean(self):
        st = safety.clean({"low_soc_pct": 15, "low_soc_on": False}, dict(safety.DEFAULTS))
        self.assertEqual((st["low_soc_pct"], st["low_soc_on"]), (15, False))
        with self.assertRaises(ValueError):
            safety.clean({"low_soc_pct": 90}, dict(safety.DEFAULTS))


def set_values(mock, **kw):
    d = parse_bms_values(mock.values)
    d.update(kw)
    mock.values = mock_bms.encode_values(d)


class TestRestCycle(Base):
    fixture = "vbms32_18s"

    def test_low_soc_rest_checkin_and_charger(self):
        set_values(self.mock, soc=0.10, v_charge=0.0, status="")
        self.assertTrue(wait(lambda: self.b.values["soc"] < 0.11))
        t = time.time()
        self.b._check_rest(self.b.values, t)
        self.assertIsNone(self.b.resting())
        self.b._check_rest(self.b.values, t + 2 * 3600 + 1)
        self.assertEqual(self.b.resting()["reason"], "low_soc")
        self.assertTrue(wait(lambda: self.b.state == "resting"), self.b.state)
        self.assertEqual(self.app.store.get(self.b.mac)["rest"]["reason"], "low_soc")   # survives a restart
        self.assertIn("rested_low", {x["key"] for x in safety.evaluate(self.b)})
        # check-in while still low: disconnects again
        n = self.b.connect_count
        self.b._set_rest(dict(self.b.resting(), next=time.time()))
        self.b._wake.set()
        self.assertTrue(wait(lambda: any(e["kind"] == "rest_check" for e in self.b.events)))
        self.assertTrue(wait(lambda: self.b.state == "resting"))
        self.assertEqual(self.b.connect_count, n + 1)
        self.assertIsNotNone(self.b.resting())
        # the charger is plugged in: the next check-in stays connected
        set_values(self.mock, soc=0.10, v_charge=70.0, status="CHARGING")
        self.b._set_rest(dict(self.b.resting(), next=time.time()))
        self.b._wake.set()
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b.resting() is None), self.b.state)
        self.assertNotIn("rest", self.app.store.get(self.b.mac))

    def test_reconnect_ends_rest(self):
        self.b.rest("low_soc", 3600, "test")
        self.assertTrue(wait(lambda: self.b.state == "resting"))
        self.app.pause({"host": self.mock.host, "pause": False})
        self.assertTrue(wait(lambda: self.b.state == "online"))
        self.assertIsNone(self.b.resting())


class TestStorageSchedule(Base):
    fixture = "vbms32_18s"

    def test_scheduler_waits_while_storage_on(self):
        self.setup_profile()
        prop, info = storage.plan(self.b, True, register=False)
        import safe_write
        safe_write.apply(self.b, prop)
        storage.finish(self.b, info, self.app)
        self.app.store.update(self.b.mac, schedule={"enabled": True, "items": [{"x": 1}]})
        writes = len(self.mock.writes)
        self.app.scheduler.tick(self.b)        # returns before looking at the (bogus) schedule
        self.assertEqual(len(self.mock.writes), writes)

    def test_off_requested_while_away(self):
        self.setup_profile()
        before = {k: round(self.b.config.values[k], 3) for k in storage.SAVED}
        storage.apply(self.b, True, app=self.app)
        self.b.rest("storage", 3600, "test")
        self.assertTrue(wait(lambda: self.b.state == "resting"))
        storage.request_off_later(self.b)       # also ends the rest
        self.assertTrue(wait(lambda: self.b.state == "online"))
        self.app.scheduler.housekeeping(self.b)
        self.assertFalse(storage.is_on(self.b.profile()))
        self.assertEqual(before, {k: round(self.b.config.values[k], 3) for k in storage.SAVED})


if __name__ == "__main__":
    unittest.main()

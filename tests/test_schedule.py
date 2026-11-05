"""Charge schedules and timers: validation, per-channel resolution, and the scheduler against the mock BMS."""

import datetime as dt
import time
import unittest

from tests.test_write import Base, wait

import schedule  # noqa: E402
import server  # noqa: E402

D = dt.datetime.fromisoformat
PTS = [[60, 3.86], [80, 4.02], [100, 4.21]]
DEV = {"vc_charge_min": 2.0, "vc_balance_min": 3.5}

WEEKNIGHT = {"name": "Weeknight", "repeat": "weekly", "days": [0, 1, 2, 3, 4], "start": "22:00", "end": "06:00",
             "action": "charge_off"}
WEEKEND = {"name": "Weekend", "repeat": "weekly", "days": [5, 6], "start": "00:00", "end": "24:00",
           "action": "limit", "pct": 100}


def sched(*items):
    s = schedule.clean({"items": [dict(r, id="r%d" % i) for i, r in enumerate(items)]})
    return schedule.attach_volts(s, PTS, 4.21, DEV)


def val(s, ch, t, timers=None):
    d = schedule.desired(s, timers, ch, D(t))
    return None if d is None else d["value"]


class TestItems(unittest.TestCase):
    def test_validation(self):
        bad = [dict(WEEKNIGHT, days=[]), dict(WEEKNIGHT, start="25:00"), dict(WEEKNIGHT, end="22:00"),
               dict(WEEKEND, pct=50), dict(WEEKEND, pct=101), dict(WEEKEND, action="x"),
               {"name": "x", "repeat": "once", "date": "nope", "start": "01:00", "end": "02:00",
                "action": "charge_off"},
               dict(WEEKNIGHT, start="24:00"), dict(WEEKNIGHT, kind="nope")]
        for r in bad:
            with self.assertRaises(schedule.ScheduleError, msg=str(r)):
                schedule.clean({"items": [r]})
        ev = schedule.clean({"items": [{"repeat": "weekly", "days": [0], "kind": "event", "start": "17:00",
                                        "action": "limit", "pct": 90, "end": "ignored"}]})["items"][0]
        self.assertNotIn("end", ev)

    def test_overnight_and_all_day(self):
        s = sched(WEEKNIGHT, WEEKEND)
        # 2026-10-30 is a Friday
        self.assertEqual(val(s, "charge", "2026-10-30 21:59"), "revert")  # Thursday night's window ended
        self.assertEqual(val(s, "charge", "2026-10-30 23:00"), 0)
        self.assertEqual(val(s, "charge", "2026-10-31 05:59"), 0)       # Fri night runs into Sat
        self.assertEqual(val(s, "charge", "2026-10-31 06:00"), "revert")
        self.assertEqual(val(s, "limit", "2026-10-31 06:00")["volts"]["end"], 4.21)
        self.assertEqual(schedule.desired(s, None, "limit", D("2026-11-01 23:59:59"))["source"]["name"], "Weekend")
        self.assertEqual(val(s, "limit", "2026-11-02 00:00"), "revert")
        self.assertIsNone(val(s, "balance", "2026-11-02 00:00"))         # nothing touches balancing

    def test_channels_are_independent(self):
        s = sched(WEEKEND, dict(WEEKNIGHT, days=[5]))
        self.assertEqual(val(s, "charge", "2026-10-31 23:00"), 0)
        self.assertEqual(val(s, "limit", "2026-10-31 23:00")["pct"], 100)

    def test_latest_start_wins_then_hands_back(self):
        trip = {"name": "Trip", "repeat": "once", "date": "2026-10-31", "start": "10:00", "end": "12:00",
                "action": "limit", "pct": 95}
        s = sched(WEEKEND, trip)
        self.assertEqual(val(s, "limit", "2026-10-31 09:00")["pct"], 100)
        self.assertEqual(val(s, "limit", "2026-10-31 10:30")["pct"], 95)
        d = schedule.desired(s, None, "limit", D("2026-10-31 12:30"))   # trip ended, weekend still running
        self.assertEqual((d["value"]["pct"], d["kind"]), (100, "start"))
        self.assertTrue(d["key"].startswith("start:r0@2026-10-31"))

    def test_event_holds_until_next_boundary(self):
        five = {"name": "Five", "repeat": "weekly", "days": [0, 1, 2, 3, 4, 5, 6], "kind": "event",
                "start": "17:00", "action": "limit", "pct": 90}
        s = sched(five)
        self.assertEqual(val(s, "limit", "2026-10-31 16:59")["pct"], 90)   # yesterday's event still holds
        self.assertEqual(val(s, "limit", "2026-10-31 23:00")["pct"], 90)
        self.assertEqual(schedule.desired(s, None, "limit", D("2026-10-31 17:00"))["key"],
                         "event:r0@2026-10-31T17:00:00")

    def test_revert_off_keeps_the_limit(self):
        keep = dict(WEEKEND, revert=False)
        s = sched(keep)
        self.assertEqual(val(s, "limit", "2026-11-02 01:00")["pct"], 100)

    def test_timer_beats_window_until_next_boundary(self):
        s = sched(WEEKNIGHT)
        t = {"id": "t1", "label": "start charging", "actions": {"charge": 1},
             "at": D("2026-10-30 23:30").timestamp()}
        self.assertEqual(val(s, "charge", "2026-10-30 23:29", [t]), 0)
        self.assertEqual(val(s, "charge", "2026-10-30 23:30", [t]), 1)
        self.assertEqual(val(s, "charge", "2026-10-31 05:00", [t]), 1)
        self.assertEqual(val(s, "charge", "2026-10-31 06:00", [t]), "revert")   # schedule takes over again
        self.assertEqual(val(s, "charge", "2026-11-02 22:00", [t]), 0)

    def test_timer_without_schedule(self):
        t = {"id": "t1", "label": "x", "actions": {"limit": {"pct": 90, "volts": {"start": 4.0, "end": 4.1}}},
             "at": D("2026-10-30 12:00").timestamp()}
        self.assertIsNone(val(None, "limit", "2026-10-30 11:00", [t]))
        self.assertEqual(val(None, "limit", "2026-10-30 12:00", [t])["pct"], 90)

    def test_disabled_item_and_schedule_ignored(self):
        self.assertIsNone(val(sched(dict(WEEKNIGHT, enabled=False)), "charge", "2026-10-30 23:00"))
        self.assertIsNone(val(dict(sched(WEEKNIGHT), enabled=False), "charge", "2026-10-30 23:00"))

    def test_next_change_skips_same_window_midnight(self):
        s = sched(WEEKEND)
        self.assertEqual(schedule.next_change(s, None, D("2026-10-31 12:00")), D("2026-11-02 00:00"))

    def test_upgrade_v1_rules(self):
        s = schedule.clean({"default_pct": 80, "rules": [dict(WEEKNIGHT, mode="block", action=None),
                                                          dict(WEEKEND, mode="limit", action=None)]})
        self.assertEqual([(r["action"], r["kind"]) for r in s["items"]], [("charge_off", "window"),
                                                                          ("limit", "window")])
        self.assertNotIn("default_pct", s)

    def test_timer_validation(self):
        now = 1_000_000.0
        with self.assertRaises(schedule.ScheduleError):
            schedule.clean_timer({"in_s": 600}, now)                       # does nothing
        with self.assertRaises(schedule.ScheduleError):
            schedule.clean_timer({"in_s": 8 * 86400, "charge": "on"}, now)
        tm = schedule.clean_timer({"in_s": 4 * 3600, "charge": "on", "pct": 90}, now)
        self.assertEqual((tm["at"], tm["actions"]), (now + 14400, {"charge": 1, "limit": {"pct": 90}}))
        self.assertEqual(tm["label"], "start charging, to 90%")


class Clock:
    def __init__(self, t):
        self.t = D(t)

    def __call__(self):
        return self.t


TRIP = {"name": "Trip", "repeat": "once", "date": "2026-10-31", "start": "13:00", "end": "15:00",
        "action": "limit", "pct": 100}
TOPUP = {"name": "Top up", "repeat": "once", "date": "2026-10-31", "start": "15:00", "end": "16:00",
         "action": "limit", "pct": 90}
QUIET = {"name": "Quiet", "repeat": "once", "date": "2026-10-31", "start": "13:00", "end": "15:00",
         "action": "charge_off"}
NOBAL = {"name": "No balancing", "repeat": "once", "date": "2026-10-31", "start": "13:00", "end": "15:00",
         "action": "balance_off"}


class TestScheduler(Base):
    """Windows save the board's values first and give them back afterwards, through disconnects."""
    fixture = "vbms32_18s"

    def setUp(self):
        super().setUp()
        self.app.scheduler.stop()
        self.setup_profile()
        prop = self.app.prepare({"host": self.mock.host, "pct": 70})   # what "you" set by hand
        self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.mine = self.end()
        self.clock = Clock("2026-10-31 12:00")   # Saturday
        self.sch = schedule.Scheduler(self.app, clock=self.clock)
        self.app.scheduler.clock = self.clock
        self.base = len(self.mock.writes)

    def use(self, *items):
        p = self.app.schedule_prepare({"host": self.mock.host, "schedule": {"items": list(items)}})
        self.assertEqual(len(self.mock.writes), self.base)      # saving writes nothing by itself
        self.app.schedule_commit({"host": self.mock.host, "id": p["id"]})
        return p

    def cfg(self):
        return self.mock.schema.decode(self.mock.blob).values

    def end(self):
        return self.cfg()["vc_charge_end"]

    def writes(self):
        return len(self.mock.writes) - self.base

    def at(self, t, online=True):
        self.clock.t = D(t)
        self.b.state = "online" if online else "offline"
        self.sch.tick(self.b)

    def allowed(self):
        return [v for c, v in self.mock.runtime if c == 97]

    def chrun(self, ch="limit"):
        return self.b.profile()["schedule_run"][ch]

    def test_prepare_lists_writes(self):
        p = self.use(WEEKNIGHT, WEEKEND)
        self.assertEqual([r["volts"] and r["volts"]["end"] for r in p["rows"]], [None, 4.21])

    def test_restores_value_from_before_the_window(self):
        self.use(TRIP)
        self.at("2026-10-31 12:30")
        self.assertEqual(self.writes(), 0)
        self.at("2026-10-31 13:00")
        self.assertAlmostEqual(self.end(), 4.21, places=5)
        self.assertAlmostEqual(self.chrun()["baseline"]["end"], self.mine, places=4)
        self.at("2026-10-31 15:00")
        self.assertAlmostEqual(self.end(), self.mine, places=5)
        self.assertEqual(self.writes(), 2)
        self.at("2026-10-31 15:30")
        self.assertEqual(self.writes(), 2)

    def test_revert_off_keeps_the_limit(self):
        self.use(dict(TRIP, revert=False))
        self.at("2026-10-31 13:00")
        self.at("2026-10-31 15:00")
        self.assertAlmostEqual(self.end(), 4.21, places=5)
        self.assertEqual(self.writes(), 1)
        self.assertIsNone(self.chrun()["baseline"])

    def test_single_event_has_no_end(self):
        self.use({"name": "Five", "repeat": "once", "date": "2026-10-31", "kind": "event", "start": "17:00",
                  "action": "limit", "pct": 100})
        self.at("2026-10-31 16:00")
        self.assertEqual(self.writes(), 0)
        self.at("2026-10-31 17:00")
        self.assertAlmostEqual(self.end(), 4.21, places=5)
        self.at("2026-11-01 17:00")
        self.assertEqual(self.writes(), 1)

    def test_new_item_ignores_slots_before_it_was_saved(self):
        stop = {"name": "Stop", "repeat": "weekly", "days": [4], "kind": "event", "start": "12:00",
                "action": "charge_off"}                     # Fridays; saved on a Saturday
        self.use(stop, dict(QUIET, repeat="weekly", days=[5], start="11:00", end="13:00"))
        self.at("2026-10-31 12:00")                         # Quiet was already running: it counts from now
        self.assertEqual(self.allowed()[-1:], [0])
        self.at("2026-10-31 13:00")
        self.assertEqual(self.allowed()[-1:], [1])          # ...and last Friday's Stop doesn't fire
        self.at("2026-11-06 12:00")
        self.assertEqual(self.allowed()[-1:], [0])          # next Friday it does

    def test_unchanged_items_keep_counting_after_a_resave(self):
        five = {"id": "five", "name": "Five", "repeat": "weekly", "days": [5], "kind": "event", "start": "11:00",
                "action": "limit", "pct": 100}
        self.use(five)
        self.at("2026-10-31 12:00")
        self.assertEqual(self.writes(), 0)                  # today's 11:00 was before it was saved
        self.clock.t = D("2026-11-07 12:00")
        self.use(five, dict(TRIP, date="2026-11-08"))       # re-saved after this week's 11:00
        self.at("2026-11-07 12:00")
        self.assertAlmostEqual(self.end(), 4.21, places=5)  # unchanged, so this week's event still counts
        p = self.app.schedule_prepare({"host": self.mock.host, "schedule": {"items": [dict(five, start="11:30")]}})
        self.app.schedule_commit({"host": self.mock.host, "id": p["id"]})   # changed: counts from now
        sch = self.b.profile()["schedule"]["items"][0]
        self.assertEqual(sch["since"], D("2026-11-07 12:00").timestamp())

    def test_back_to_back_windows_keep_original(self):
        self.use(TRIP, TOPUP)
        self.at("2026-10-31 13:00")
        self.at("2026-10-31 15:00")                  # Top up starts as Trip ends
        self.assertLess(self.end(), 4.21)
        self.assertGreater(self.end(), self.mine)
        self.at("2026-10-31 16:00")
        self.assertAlmostEqual(self.end(), self.mine, places=5)

    def test_offline_at_start_applies_on_reconnect(self):
        self.use(TRIP)
        self.at("2026-10-31 13:00", online=False)
        self.assertEqual(self.writes(), 0)
        self.at("2026-10-31 14:00")
        self.assertEqual(self.writes(), 1)
        self.assertAlmostEqual(self.chrun()["baseline"]["end"], self.mine, places=4)

    def test_offline_at_end_restores_on_reconnect(self):
        self.use(TRIP)
        self.at("2026-10-31 13:00")
        self.at("2026-10-31 15:00", online=False)
        self.at("2026-10-31 18:00", online=False)
        self.assertAlmostEqual(self.end(), 4.21, places=5)
        self.at("2026-10-31 19:00")
        self.assertAlmostEqual(self.end(), self.mine, places=5)

    def test_missed_one_time_window_is_not_resent(self):
        self.use(TRIP, QUIET)
        self.at("2026-10-31 12:30")
        self.at("2026-10-31 13:00", online=False)
        self.at("2026-10-31 14:59", online=False)
        self.at("2026-10-31 15:01")
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.allowed(), [])

    def test_restart_mid_window(self):
        self.use(TRIP)
        self.at("2026-10-31 13:00")
        self.sch = schedule.Scheduler(self.app, clock=self.clock)   # app restarted
        self.at("2026-10-31 14:00")
        self.assertEqual(self.writes(), 1)
        self.at("2026-10-31 15:00")
        self.assertAlmostEqual(self.end(), self.mine, places=5)

    def test_manual_change_during_window_is_kept(self):
        self.use(TRIP)
        self.at("2026-10-31 13:00")
        prop = self.app.prepare({"host": self.mock.host, "pct": 90})
        self.app.commit({"host": self.mock.host, "id": prop["id"]})
        mine = self.end()
        n = self.writes()
        self.at("2026-10-31 15:00")
        self.assertEqual(self.writes(), n)
        self.assertAlmostEqual(self.end(), mine, places=5)
        self.assertIn("kept your manual limit", self.sch.status(self.b)["last"])

    def test_no_charging_window_allows_again_afterwards(self):
        self.use(QUIET)
        self.at("2026-10-31 13:00")
        self.assertTrue(wait(lambda: self.allowed() == [0]))
        self.at("2026-10-31 15:00")
        self.assertTrue(wait(lambda: self.allowed() == [0, 1]))
        self.assertEqual(self.writes(), 0)

    def test_no_charging_window_keeps_your_own_pause(self):
        self.app.runtime({"host": self.mock.host, "cmd": "charge_allowed", "value": 0, "confirm": True})
        self.use(QUIET)
        self.at("2026-10-31 13:00")
        self.at("2026-10-31 15:00")
        time.sleep(0.3)
        self.assertEqual(self.allowed(), [0, 0])

    def test_override_skips_the_pause_until_next_boundary(self):
        self.use(QUIET, dict(QUIET, name="Again", start="16:00", end="17:00"))
        self.at("2026-10-31 13:00")
        self.assertTrue(self.sch.status(self.b)["paused_by_schedule"])
        self.app.runtime({"host": self.mock.host, "cmd": "charge_allowed", "value": 1, "confirm": True})
        st = self.sch.status(self.b)
        self.assertTrue(st["channels"]["charge"]["override"])
        self.assertFalse(st["paused_by_schedule"])
        self.sch._st(self.b.mac)["last_pause"] = 0.0   # a re-send would be due
        self.at("2026-10-31 13:30")
        time.sleep(0.3)
        self.assertEqual(self.allowed(), [0, 1])
        self.at("2026-10-31 16:00")                     # next window: the schedule is back in charge
        self.assertTrue(wait(lambda: self.allowed() == [0, 1, 0]))

    def test_turning_off_mid_window_restores(self):
        self.use(TRIP)
        self.at("2026-10-31 13:00")
        self.app.schedule_off({"host": self.mock.host})
        self.at("2026-10-31 13:30")
        self.assertAlmostEqual(self.end(), self.mine, places=5)
        self.assertEqual(self.chrun()["key"], "none")

    def test_balancing_window_writes_balance_current(self):
        normal = round(self.cfg()["balance_max_current"], 3)
        self.assertGreater(normal, 0)
        p = self.use(NOBAL)
        self.assertEqual(p["balance_normal_a"], normal)
        self.at("2026-10-31 13:00")
        self.assertEqual(self.cfg()["balance_max_current"], 0)
        self.assertAlmostEqual(self.end(), self.mine, places=5)   # charge settings untouched
        self.at("2026-10-31 15:00")
        self.assertAlmostEqual(self.cfg()["balance_max_current"], normal, places=4)
        self.assertEqual(self.writes(), 2)

    def test_timer_trumps_window_then_schedule_takes_over(self):
        self.use(QUIET)
        self.at("2026-10-31 13:00")
        self.assertTrue(wait(lambda: self.allowed() == [0]))
        tm = {"id": "t1", "label": "start charging, to 90%", "at": D("2026-10-31 13:30").timestamp(),
              "actions": {"charge": 1, "limit": {"pct": 90, "volts": {"start": 4.06, "end": 4.16}}}}
        self.b.boards.update(self.b.mac, timers=[tm])
        self.at("2026-10-31 13:30")
        self.assertTrue(wait(lambda: self.allowed() == [0, 1]))
        self.assertAlmostEqual(self.end(), 4.16, places=5)
        self.sch._st(self.b.mac)["last_pause"] = 0.0
        self.at("2026-10-31 14:00")                      # still inside the window: no re-pause
        time.sleep(0.3)
        self.assertEqual(self.allowed(), [0, 1])
        self.at("2026-10-31 15:00")                      # window ends: the schedule decides again
        self.assertEqual(self.b.profile()["timers"], [tm])   # still deciding the limit channel
        self.assertEqual(self.sch.status(self.b)["timers"], [])

    def test_timer_api(self):
        self.app.scheduler.clock = dt.datetime.now
        p = self.app.timer_prepare({"host": self.mock.host, "timer": {"in_s": 600, "charge": "on", "pct": 90}})
        self.assertAlmostEqual(p["timer"]["actions"]["limit"]["volts"]["end"] -
                               p["timer"]["actions"]["limit"]["volts"]["start"], 0.1, places=3)
        self.assertEqual(self.writes(), 0)
        self.app.timer_commit({"host": self.mock.host, "id": p["id"]})
        tid = self.b.profile()["timers"][0]["id"]
        self.assertEqual(len(self.app.scheduler.status(self.b)["timers"]), 1)
        self.app.timer_cancel({"host": self.mock.host, "timer_id": tid})
        self.assertEqual(self.b.profile()["timers"], [])

    def test_cell_change_turns_schedule_off_and_drops_limit_timers(self):
        self.use(WEEKEND)
        self.b.boards.update(self.b.mac, timers=[{"id": "a", "at": 2e9, "label": "x", "actions": {"limit": {}}},
                                                 {"id": "b", "at": 2e9, "label": "y", "actions": {"charge": 1}}])
        self.app.set_profile({"host": self.mock.host, "cell_id": "molicel_p42a"})
        st = self.b.profile()["schedule"]
        self.assertTrue(st["stale"])
        self.assertFalse(st["enabled"])
        self.assertEqual([t["id"] for t in self.b.profile()["timers"]], ["b"])
        self.at("2026-10-31 13:00")
        self.assertEqual(self.writes(), 0)

    def test_accepting_a_changed_curve_turns_stale_schedule_off(self):
        self.use(WEEKEND)
        self.app.accept_curve({"host": self.mock.host})          # same curve: nothing to review
        self.assertTrue(self.b.profile()["schedule"]["enabled"])
        sched = self.b.profile()["schedule"]
        items = [dict(r, volts={"start": 3.9, "end": 4.0}) if r["action"] == "limit" else r for r in sched["items"]]
        self.b.boards.update(self.b.mac, schedule=dict(sched, items=items),   # approved under an older curve
                             timers=[{"id": "a", "at": 2e9, "label": "x", "actions": {"limit": {"pct": 90, "volts": {"start": 3.9, "end": 4.0}}}},
                                     {"id": "b", "at": 2e9, "label": "y", "actions": {"charge": 1}}])
        self.app.accept_curve({"host": self.mock.host})
        st = self.b.profile()["schedule"]
        self.assertTrue(st["stale"])
        self.assertFalse(st["enabled"])
        self.assertEqual([t["id"] for t in self.b.profile()["timers"]], ["b"])

    def test_old_format_schedule_is_migrated_and_keeps_its_approval(self):
        v1 = {"approved": "2026-10-29 03:08", "default_pct": 60, "default_volts": {"end": 3.86, "start": 3.76}, "enabled": True,
              "rules": [{"days": [0, 1, 2, 3, 4, 5, 6], "enabled": True, "end": "10:00", "id": "34ce35aa", "mode": "limit",
                         "name": "Charge to 90 in the morning", "pct": 90, "repeat": "weekly", "start": "06:30",
                         "volts": {"end": 4.1, "start": 4.0}}]}
        m = schedule.migrate(v1)
        self.assertTrue(m["enabled"])
        self.assertEqual(m["approved"], v1["approved"])
        it = m["items"][0]
        self.assertEqual((it["kind"], it["action"], it["pct"], it["volts"]), ("window", "limit", 90, {"end": 4.1, "start": 4.0}))
        self.assertTrue(schedule.live(m))
        self.assertIsNone(schedule.migrate(m))                      # already current
        self.assertFalse(schedule.migrate({"rules": [{"repeat": "weekly"}], "enabled": True})["enabled"])  # unreadable: off

    def test_blocked_write_reports_error(self):
        self.use(TRIP)
        self.b.script = dict(self.b.script, verified=False)  # an unrecognized script locks writes
        self.at("2026-10-31 13:00")
        self.assertEqual(self.writes(), 0)
        self.assertIn("couldn't apply", self.sch.status(self.b)["error"])

    def test_requires_accepted_curve(self):
        self.app.set_profile({"host": self.mock.host, "cell_id": "molicel_p42a"})
        with self.assertRaises(server.ApiError):
            self.app.schedule_prepare({"host": self.mock.host, "schedule": {"items": [WEEKEND]}})
        # charging/balancing-only schedules don't need a curve
        self.app.schedule_prepare({"host": self.mock.host, "schedule": {"items": [WEEKNIGHT, NOBAL]}})


if __name__ == "__main__":
    unittest.main()

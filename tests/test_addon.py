"""The charger-latch add-on: availability, the upload against the mock BMS, and recovery when it fails."""

import hashlib
import json
import os

from tests import mock_bms
from tests.test_write import Base, wait

import addons  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
from vesc.bms_values import parse_bms_values  # noqa: E402

LATCH = addons.ADDONS["charger_latch"]
STOCK = mock_bms.STOCK_LIKE_SCRIPT


def changes():
    p = os.path.join(store.home(), "changes.log")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(x) for x in f if x.strip()]


class AddonBase(Base):
    def setUp(self):
        # The mock's script stands in for the stock one; the real stock fingerprint isn't shipped with a test copy.
        self._bases = dict(LATCH["bases"])
        LATCH["bases"][addons.fingerprint(STOCK)] = "test stock"
        super().setUp()
        self.setup_profile()
        self.set_charger(0.0)  # the 32s fixture was captured on the charger

    def set_charger(self, volts):
        v = parse_bms_values(self.mock.values)
        v["v_charge"] = volts
        self.mock.values = mock_bms.encode_values(v)
        self.assertTrue(wait(lambda: abs(self.b.values["v_charge"] - volts) < 0.01))

    def tearDown(self):
        super().tearDown()
        LATCH["bases"].clear()
        LATCH["bases"].update(self._bases)

    def body(self, **kw):
        return dict({"host": self.mock.host, "mac": self.b.mac}, **kw)

    def prepare(self):
        return self.app.addon_prepare(self.body(addon="charger_latch"))

    def addon(self):
        return next(a for a in self.app.snapshot(self.b)["addons"] if a["id"] == "charger_latch")

    def plug_charger(self):
        self.set_charger(self.b.values["v_tot"] + 4.0)


class TestStatus(AddonBase):
    def test_bundled_script(self):
        code = addons.code("charger_latch")
        self.assertEqual(hashlib.sha256(code).hexdigest(), LATCH["sha256"])
        self.assertNotIn(addons.fingerprint(code), self._bases)
        self.assertEqual(addons.stored_form(code), code + b"\x00\x00\x00")

    def test_fingerprint_ignores_comments_and_layout(self):
        a = b"(def x 1) ; one\n(defun f () {\n    (+ x 1)\n})\n"
        b = b";; header\n(def x 1)\n(defun f () { (+ x 1) })\x00\x00\x00"
        self.assertEqual(addons.fingerprint(a), addons.fingerprint(b))
        self.assertNotEqual(addons.fingerprint(a), addons.fingerprint(a.replace(b"1)", b"2)", 1)))

    def test_available_on_stock(self):
        self.assertEqual(self.addon()["state"], "available")

    def test_unavailable_on_other_script(self):
        LATCH["bases"].clear()
        self.assertEqual(self.addon()["state"], "unavailable")
        with self.assertRaises(server.ApiError):
            self.prepare()
        self.assertNotIn(132, self.mock.cmds)


class TestInstall(AddonBase):
    def test_install(self):
        p = self.prepare()
        self.assertEqual(p["kind"], "script")
        self.assertEqual(p["confirm_name"], "Test")
        self.assertIn("VESC Tool", p["undo"])
        self.assertNotIn("blob_before", p)
        self.assertFalse(any(c in self.mock.cmds for c in (131, 132, 133)), "prepare must not write")
        self.b.runtime_state = {"charge_allowed": {"value": 0}}

        r = self.app.commit(self.body(id=p["id"], confirm_text="test"))
        self.assertTrue(r["ok"])
        self.assertEqual(self.mock.script, addons.stored_form(addons.code("charger_latch")))
        self.assertTrue(self.mock.lisp_running)
        ops = [o for o in self.mock.lisp_ops if o[0] != "write"]
        self.assertEqual(ops, [("erase", len(self.mock.script) + 2 + 100), ("run", 1)])
        writes = [o[1] for o in self.mock.lisp_ops if o[0] == "write"]
        self.assertEqual(writes, list(range(0, len(self.mock.script) + 8, 384)))
        self.assertEqual(self.b.script["sha256"], LATCH["sha256"])
        self.assertTrue(self.b.script["verified"])
        self.assertEqual(self.b.runtime_state, {})
        self.assertEqual(self.addon()["state"], "installed")
        with open(r["backup"], "rb") as f:
            self.assertEqual(f.read(), STOCK)
        log = changes()[-1]
        self.assertEqual((log["event"], log["result"]), ("script_write", "ok"))
        # No config write happened.
        self.assertEqual(self.mock.writes, [])

    def test_needs_typed_name_even_with_confirmations_off(self):
        p = self.prepare()
        with self.assertRaises(server.ApiError):
            self.app.commit(self.body(id=p["id"], skipped_confirm=True))
        self.assertNotIn(132, self.mock.cmds)

    def test_refused_with_charger_plugged(self):
        self.plug_charger()
        with self.assertRaises(server.ApiError) as e:
            self.prepare()
        self.assertIn("unplug the charger", str(e.exception))

    def test_charger_plugged_after_prepare(self):
        p = self.prepare()
        self.plug_charger()
        with self.assertRaises(server.ApiError):
            self.app.commit(self.body(id=p["id"], confirm_text="Test"))
        self.assertNotIn(132, self.mock.cmds)

    def test_script_changed_since_prepare(self):
        p = self.prepare()
        self.mock.script = STOCK + b"\n"
        with self.assertRaises(server.ApiError) as e:
            self.app.commit(self.body(id=p["id"], confirm_text="Test"))
        self.assertIn("changed since", str(e.exception))
        self.assertNotIn(132, self.mock.cmds)

    def test_needs_risk_accepted(self):
        self.app.store.update(self.b.mac, risk_ack=None)
        with self.assertRaises(server.ApiError):
            self.prepare()


class TestFailures(AddonBase):
    def test_write_refused_puts_old_script_back(self):
        p = self.prepare()
        self.mock.lisp_fail_write_at = 384
        with self.assertRaises(server.ApiError) as e:
            self.app.commit(self.body(id=p["id"], confirm_text="Test"))
        self.assertIn("written back and restarted", str(e.exception))
        self.assertEqual(self.mock.script, STOCK)
        self.assertTrue(self.mock.lisp_running)
        self.assertEqual(changes()[-1]["result"], "error_restored")
        self.assertNotEqual(self.addon()["state"], "installed")

    def test_start_refused_puts_old_script_back(self):
        p = self.prepare()
        self.mock.lisp_fail_start = True
        with self.assertRaises(server.ApiError):
            self.app.commit(self.body(id=p["id"], confirm_text="Test"))
        # Restarting the old one fails too in this mock, so it must not claim the board is as it was.
        self.assertEqual(changes()[-1]["result"], "error_not_restored")

    def test_bad_readback_not_restored(self):
        p = self.prepare()
        self.mock.lisp_corrupt = True
        with self.assertRaises(server.ApiError) as e:
            self.app.commit(self.body(id=p["id"], confirm_text="Test"))
        msg = str(e.exception)
        self.assertIn("couldn't be put back", msg)
        self.assertIn("re-install the BMS package from VESC Tool", msg)
        self.assertEqual(changes()[-1]["result"], "error_not_restored")

    def test_erase_refused(self):
        p = self.prepare()
        self.mock.lisp_reject_erase = True
        with self.assertRaises(server.ApiError):
            self.app.commit(self.body(id=p["id"], confirm_text="Test"))
        self.assertNotIn(131, self.mock.cmds)


if __name__ == "__main__":
    import unittest
    unittest.main()

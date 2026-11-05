"""Generic settings writes (config_fields.py + safe_write.prepare_fields) against the mock BMS."""

import unittest

from tests.test_write import READS, Base

import server  # noqa: E402
import store  # noqa: E402


class TestFieldWrites(Base):
    def prep(self, changes):
        return self.app.prepare_fields({"host": self.mock.host, "changes": changes})

    def commit(self, prop, text=None):
        return self.app.commit({"host": self.mock.host, "id": prop["id"], "confirm_text": text})

    def test_fixed_width_change_touches_only_its_bytes(self):
        before = self.mock.blob
        prop = self.prep({"vc_balance_start": 0.02, "t_bal_max_cell": 45})
        self.assertEqual(self.mock.writes, [])
        self.assertTrue(set(self.mock.received) <= READS)
        self.assertIsNone(prop["confirm_name"])
        self.commit(prop)
        after = self.mock.writes[0]
        cfg = self.mock.schema.decode(before)
        allowed = set(range(*cfg.spans["vc_balance_start"])) | set(range(*cfg.spans["t_bal_max_cell"]))
        diff = {i for i in range(len(before)) if before[i] != after[i]}
        self.assertTrue(diff and diff <= allowed)
        new = self.mock.schema.decode(after).values
        self.assertAlmostEqual(new["vc_balance_start"], 0.02, places=5)
        self.assertEqual(new["t_bal_max_cell"], 45.0)
        self.assertEqual(store.read_log("changes")[-1]["result"], "ok")

    def test_string_change_reencodes_and_keeps_everything_else(self):
        before = self.mock.schema.decode(self.mock.blob).values
        prop = self.prep({"ble_name": "Garage"})
        self.commit(prop)
        new = self.mock.schema.decode(self.mock.writes[0]).values
        self.assertEqual(new["ble_name"], "Garage")
        for k, v in before.items():
            if k != "ble_name":
                self.assertEqual(new[k], v, k)

    def test_refusals(self):
        bad = [
            {"t_charge_min": -50},                      # below tool bound
            {"vc_balance_end": 0.05},                   # not below Balance Start
            {"vc_charge_end": 4.0},                     # charge fields only via the % control
            {"cells_ic1": 10},                          # read-only hardware setting
            {"soc_filter_const": 0.5},                  # not whitelisted
            {"ble_name": "123456789"},                  # longer than the firmware allows
            {"wifi_sta_key": "short"},                  # not a valid WPA key
            {"wifi_mode": 1, "wifi_sta_ssid": ""},      # station mode without SSID
            {"vc_balance_start": 0.015},                # no-op
        ]
        for ch in bad:
            with self.assertRaises(server.ApiError, msg=str(ch)):
                self.prep(ch)
        self.assertEqual(self.mock.writes, [])

    def test_balance_min_above_charge_start_is_a_caution(self):
        start = self.mock.schema.decode(self.mock.blob).values["vc_charge_start"]
        prop = self.prep({"vc_balance_min": round(start + 0.05, 2)})
        risks = prop["rows"][0]["risks"]
        self.assertEqual([r["level"] for r in risks], ["caution"])
        self.assertIsNone(prop["confirm_name"])
        self.commit(prop)
        self.assertEqual(len(self.mock.writes), 1)

    def test_danger_needs_typed_name(self):
        self.app.set_profile({"host": self.mock.host, "nickname": "Garage"})
        prop = self.prep({"block_sleep": 1})
        self.assertEqual(prop["confirm_name"], "Garage")
        self.assertEqual(prop["rows"][0]["risks"][0]["level"], "danger")
        with self.assertRaises(server.ApiError):
            self.commit(prop)
        with self.assertRaises(server.ApiError):
            self.commit(prop, "someone else")
        self.assertEqual(self.mock.writes, [])
        self.commit(prop, " garage ")
        self.assertEqual(self.mock.schema.decode(self.mock.writes[0]).values["block_sleep"], 1)

    def test_secrets_stay_out_of_the_log(self):
        prop = self.prep({"wifi_sta_key": "correct horse"})
        self.assertNotIn("correct horse", str(prop))
        self.commit(prop, prop["confirm_name"])
        self.assertEqual(self.mock.schema.decode(self.mock.writes[0]).values["wifi_sta_key"], "correct horse")
        with open(store.path("changes.log"), encoding="utf-8") as f:
            self.assertNotIn("correct horse", f.read())

    def test_config_changed_since_prepare(self):
        prop = self.prep({"t_bal_max_ic": 60})
        cfg = self.mock.schema.decode(self.mock.blob)
        self.mock.blob = cfg.patch_fields({"t_bal_max_ic": 55.0})
        with self.assertRaises(server.ApiError):
            self.commit(prop)
        self.assertEqual(self.mock.writes, [])

    def test_editable_snapshot_hides_secrets(self):
        ed = self.b.snapshot(detail=True)["editable"]
        comms = next(g for g in ed["groups"] if g["id"] == "comms")
        key = next(f for f in comms["fields"] if f["name"] == "wifi_sta_key")
        self.assertIsNone(key["value"])
        self.assertTrue(any(r["name"] == "vc_charge_end" for r in ed["read_only"]))


if __name__ == "__main__":
    unittest.main()

"""End-to-end tests of the write path against the mock BMS."""

import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.request

from tests import mock_bms

TMP = tempfile.mkdtemp(prefix="vbms_test_")
os.environ["VBMS_MANAGER_HOME"] = TMP

import board as board_mod  # noqa: E402
import safe_write  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

READS = {0, 92, 93, 96, 130}


def wait(pred, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    fixture = "vbms32_32s"
    accept_risk = True

    def setUp(self):
        # store.home(), not TMP: discover can import this module twice (as tests.test_write and test_write),
        # and only the last import's folder is the one VBMS_MANAGER_HOME points at.
        shutil.rmtree(store.home(), ignore_errors=True)
        os.makedirs(store.home(), exist_ok=True)  # a slow previous test's board thread may have recreated it
        self.mock = mock_bms.MockBMS(self.fixture)
        self.app = server.App(start_discovery=False)
        self.app.add_host({"host": self.mock.host})
        self.b = self.app.boards[self.mock.host]
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b.values is not None), self.b.error)
        if self.accept_risk:
            self.app.ack_risk({"host": self.mock.host, "accept": True})

    def tearDown(self):
        for b in self.app.boards.values():
            b.stop()
        self.mock.close()

    def setup_profile(self):
        self.app.set_profile({"host": self.mock.host, "cell_id": "reliance_rs50", "nickname": "Test"})
        self.app.accept_curve({"host": self.mock.host})


class TestWritePath(Base):
    def test_reads_only_until_confirmed(self):
        self.setup_profile()
        prop = self.app.prepare({"host": self.mock.host, "pct": 100})
        self.assertEqual(self.mock.writes, [])
        self.assertTrue(set(self.mock.received) <= READS, set(self.mock.received))
        self.assertAlmostEqual(prop["new"]["vc_charge_end"], 4.21, places=5)
        self.assertAlmostEqual(prop["new"]["vc_charge_start"], 4.11, places=5)
        self.assertEqual(prop["bytes_changed"] <= 8, True)

    def test_write_reuses_the_config_layout(self):
        # The layout takes ~18 round trips over WiFi; a write should only need a handful.
        self.setup_profile()
        self.mock.cmds.clear()
        prop = self.app.prepare({"host": self.mock.host, "pct": 80})
        self.app.commit({"host": self.mock.host, "id": prop["id"]})
        mine = [c for c in self.mock.cmds if c != 96]          # 96 = the 1 Hz values poll
        self.assertNotIn(92, mine)                              # 92 = config layout download
        self.assertLessEqual(len(mine), 8, mine)

    def test_full_write_changes_only_two_fields(self):
        self.setup_profile()
        before = self.mock.blob
        prop = self.app.prepare({"host": self.mock.host, "pct": 80})
        res = self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.mock.writes), 1)
        after = self.mock.writes[0]
        cfg = self.mock.schema.decode(before)
        diff = [i for i in range(len(before)) if before[i] != after[i]]
        allowed = set(range(*cfg.spans["vc_charge_start"])) | set(range(*cfg.spans["vc_charge_end"]))
        self.assertTrue(diff and set(diff) <= allowed)
        new = self.mock.schema.decode(after).values
        self.assertAlmostEqual(new["vc_charge_end"] - new["vc_charge_start"], 0.1, places=5)
        self.assertTrue(os.path.exists(res["backup"]))
        self.assertTrue(os.path.exists(res["backup"][:-4] + ".bin"))
        with open(res["backup"][:-4] + ".bin", "rb") as f:
            self.assertEqual(f.read(), before)
        log = store.read_log("changes")
        self.assertEqual(log[-1]["result"], "ok")
        # Only allowed commands were ever sent
        self.assertTrue(set(self.mock.received) <= READS | {95})

    def test_proposal_single_use(self):
        self.setup_profile()
        prop = self.app.prepare({"host": self.mock.host, "pct": 70})
        self.app.commit({"host": self.mock.host, "id": prop["id"]})
        with self.assertRaises(server.ApiError):
            self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertEqual(len(self.mock.writes), 1)

    def test_config_changed_since_prepare(self):
        self.setup_profile()
        prop = self.app.prepare({"host": self.mock.host, "pct": 70})
        cfg = self.mock.schema.decode(self.mock.blob)
        self.mock.blob = cfg.patch_float32_auto({"vc_charge_end": 4.0, "vc_charge_start": 3.9})  # someone else changed it
        with self.assertRaises(server.ApiError) as e:
            self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertIn("changed", str(e.exception))
        self.assertEqual(self.mock.writes, [])

    def test_expired(self):
        self.setup_profile()
        prop = self.app.prepare({"host": self.mock.host, "pct": 70})
        safe_write._proposals[prop["id"]]["created"] -= safe_write.PROPOSAL_TTL + 1
        with self.assertRaises(server.ApiError):
            self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertEqual(self.mock.writes, [])

    def test_mac_changed(self):
        self.setup_profile()
        prop = self.app.prepare({"host": self.mock.host, "pct": 70})
        self.mock.mac = b"\x02\x00\x00\x00\x00\x99"
        with self.assertRaises(server.ApiError):
            self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertEqual(self.mock.writes, [])

    def test_readback_mismatch_reported(self):
        self.setup_profile()
        self.mock.corrupt_readback = True
        prop = self.app.prepare({"host": self.mock.host, "pct": 70})
        with self.assertRaises(server.ApiError) as e:
            self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertIn("does not match", str(e.exception))
        self.assertEqual(store.read_log("changes")[-1]["result"], "mismatch")

    def test_firmware_rejects(self):
        self.setup_profile()
        self.mock.reject_writes = True
        prop = self.app.prepare({"host": self.mock.host, "pct": 70})
        with self.assertRaises(server.ApiError) as e:
            self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertIn("Could not set configuration", str(e.exception))

    def test_requires_cell_and_curve(self):
        with self.assertRaises(server.ApiError):
            self.app.prepare({"host": self.mock.host, "pct": 80})
        self.app.set_profile({"host": self.mock.host, "cell_id": "reliance_rs50"})
        with self.assertRaises(server.ApiError) as e:
            self.app.prepare({"host": self.mock.host, "pct": 80})
        self.assertIn("accept", str(e.exception))
        self.assertTrue(set(self.mock.received) <= READS)

    def test_custom_curve_resets_acceptance(self):
        self.setup_profile()
        self.assertTrue(self.b.curve_accepted())
        pts = [[60, 3.85], [80, 4.0], [100, 4.21]]
        self.app.set_profile({"host": self.mock.host, "custom_curve": pts})
        self.assertFalse(self.b.curve_accepted())

    def test_fix_gap(self):
        cfg = self.mock.schema.decode(self.mock.blob)
        self.mock.blob = cfg.patch_float32_auto({"vc_charge_end": 4.05, "vc_charge_start": 3.90})
        self.b.config = self.mock.schema.decode(self.mock.blob)
        self.app.set_profile({"host": self.mock.host, "cell_id": "reliance_rs50"})
        prop = self.app.prepare({"host": self.mock.host, "fix_gap": True})
        self.assertAlmostEqual(prop["new"]["vc_charge_start"], 3.95, places=5)
        self.assertAlmostEqual(prop["new"]["vc_charge_end"], 4.05, places=5)

    def test_explicit_start_end_with_custom_gap(self):
        self.setup_profile()
        prop = self.app.prepare({"host": self.mock.host, "end": 4.15, "start": 4.0})
        self.assertAlmostEqual(prop["new"]["vc_charge_start"], 4.0, places=5)
        self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertFalse(self.b.snapshot()["charge"]["gap_ok"])
        self.assertTrue(self.b.snapshot()["charge"]["gap_valid"])
        with self.assertRaises(server.ApiError):
            self.app.prepare({"host": self.mock.host, "end": 4.15, "start": 3.9})     # gap 0.25 V
        prop = self.app.prepare({"host": self.mock.host, "end": 4.1})                # start follows at 0.100 V
        self.assertAlmostEqual(prop["new"]["vc_charge_start"], 4.0, places=5)

    def test_balance_toggle_writes_balance_current(self):
        normal = self.mock.schema.decode(self.mock.blob).values["balance_max_current"]
        prop = self.app.balance_prepare({"host": self.mock.host, "allow": False})
        self.assertEqual([r["new"] for r in prop["rows"]], ["0.000 A"])
        self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertEqual(self.mock.schema.decode(self.mock.blob).values["balance_max_current"], 0)
        self.assertEqual(self.app.balance_prepare({"host": self.mock.host, "allow": False}), {"noop": True})
        prop = self.app.balance_prepare({"host": self.mock.host, "allow": True})
        self.app.commit({"host": self.mock.host, "id": prop["id"]})
        self.assertAlmostEqual(self.mock.schema.decode(self.mock.blob).values["balance_max_current"], normal, places=4)

    def test_skip_confirmations(self):
        self.setup_profile()
        with self.assertRaises(server.ApiError):
            self.app.prefs({"confirm_skip": True, "phrase": "skip confirmations"}, role="paired")
        with self.assertRaises(server.ApiError):
            self.app.prefs({"confirm_skip": True, "phrase": "yes"})
        self.app.prefs({"confirm_skip": True, "phrase": "Skip confirmations"})
        prop = self.app.prepare({"host": self.mock.host, "pct": 80})
        self.app.commit({"host": self.mock.host, "id": prop["id"], "skipped_confirm": True})
        last = store.read_log("changes")[-1]
        self.assertIn("skipped", last["confirm"])
        # danger changes still need the typed name, whatever the preference says
        prop = self.app.prepare_fields({"host": self.mock.host, "changes": {"block_sleep": 1}}) \
            if "block_sleep" in self.b.config.values else None
        if prop is not None:
            self.assertTrue(prop["confirm_name"])
            with self.assertRaises(server.ApiError):
                self.app.commit({"host": self.mock.host, "id": prop["id"], "skipped_confirm": True})
        self.app.prefs({"confirm_skip": False}, role="paired")   # anyone may turn it off
        self.assertFalse(self.app._skip_on())

    def test_runtime_needs_confirm(self):
        with self.assertRaises(server.ApiError):
            self.app.runtime({"host": self.mock.host, "cmd": "charge_allowed", "value": 0})
        self.app.runtime({"host": self.mock.host, "cmd": "charge_allowed", "value": 0, "confirm": True})
        self.assertTrue(wait(lambda: self.mock.runtime == [(97, 0)]))


class TestRestore(Base):
    def _write_once(self):
        self.setup_profile()
        before = self.mock.blob
        prop = self.app.prepare({"host": self.mock.host, "pct": 80})
        self.assertTrue(self.app.commit({"host": self.mock.host, "id": prop["id"]})["ok"])
        bks = self.app.backups({"host": self.mock.host})["backups"]
        self.assertEqual(len(bks), 1)
        self.assertTrue(bks[0]["restorable"])
        return before, bks[0]["id"]

    def test_restore_round_trip(self):
        before, bid = self._write_once()
        prop = self.app.restore_prepare({"host": self.mock.host, "backup": bid})
        self.assertTrue(prop.get("confirm_name"))
        self.assertEqual(len(self.mock.writes), 1)  # preparing sends nothing
        res = self.app.commit({"host": self.mock.host, "id": prop["id"], "confirm_text": prop["confirm_name"]})
        self.assertTrue(res["ok"])
        self.assertEqual(self.mock.blob, before)

    def test_refuses_foreign_and_unwhitelisted(self):
        _, bid = self._write_once()
        for bad in ("../../etc/passwd", "x_02000000ffff/20260101-000000_before.bin", None):
            with self.assertRaises(server.ApiError):
                self.app.restore_prepare({"host": self.mock.host, "backup": bad})
        import config_fields
        cfg = self.mock.schema.decode(self.mock.blob)
        name = next(n for n in self.mock.schema.order if n not in config_fields.FIELDS and n not in safe_write.FIELDS
                    and isinstance(cfg.values[n], float) and n in cfg.spans)
        path = os.path.join(store.home(), "backups", bid)
        with open(path, "rb") as f:
            bcfg = self.mock.schema.decode(f.read())
        with open(path, "wb") as f:
            f.write(bcfg.patch_float32_auto({name: cfg.values[name] + 1.0}))
        with self.assertRaises(server.ApiError) as e:
            self.app.restore_prepare({"host": self.mock.host, "backup": bid})
        self.assertIn("VESC Tool", str(e.exception))
        self.assertEqual(len(self.mock.writes), 1)


class TestRiskWarning(Base):
    accept_risk = False

    def test_locked_until_accepted(self):
        self.setup_profile()
        self.assertFalse(self.app.snapshot(self.b)["risk_ok"])
        with self.assertRaises(server.ApiError) as e:
            self.app.prepare({"host": self.mock.host, "pct": 80})
        self.assertIn("safety warning", str(e.exception))
        with self.assertRaises(server.ApiError):
            self.app.runtime({"host": self.mock.host, "cmd": "charge_allowed", "value": 0, "confirm": True})
        with self.assertRaises(server.ApiError):
            self.app.schedule_prepare({"host": self.mock.host, "schedule": {"default_pct": None, "rules": []}})
        with self.assertRaises(server.ApiError):
            self.app.ack_risk({"host": self.mock.host})  # needs an explicit accept
        self.assertEqual(self.mock.writes, [])
        self.assertEqual(self.mock.runtime, [])
        self.app.ack_risk({"host": self.mock.host, "accept": True})
        self.assertTrue(self.app.snapshot(self.b)["risk_ok"])
        self.app.prepare({"host": self.mock.host, "pct": 80})

    def test_new_warning_version_asks_again(self):
        self.app.ack_risk({"host": self.mock.host, "accept": True})
        self.app.store.update(self.b.mac, risk_ack={"version": safe_write.RISK_VERSION - 1, "time": "x"})
        self.assertIn("safety warning", " ".join(safe_write.preconditions(self.b)))


class TestUnknownScript(Base):
    def setUp(self):
        self._orig = mock_bms.STOCK_LIKE_SCRIPT
        super().setUp()

    def test_locked_until_ack(self):
        self.b.stop()
        self.mock.close()
        self.mock = mock_bms.MockBMS(self.fixture, script=b"(setq x (bms-get-param 'vc_charge_end))")
        self.app.remove_host({"host": self.b.host})
        self.app.add_host({"host": self.mock.host})
        self.b = self.app.boards[self.mock.host]
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b.script is not None))
        self.setup_profile()
        with self.assertRaises(server.ApiError) as e:
            self.app.prepare({"host": self.mock.host, "pct": 80})
        self.assertIn("script", str(e.exception))
        self.app.ack_script({"host": self.mock.host, "sha256": self.b.script["sha256"]})
        self.app.prepare({"host": self.mock.host, "pct": 80})


class TestBusy(unittest.TestCase):
    def test_busy_board(self):
        # store.home(), not TMP: discover can import this module twice (as tests.test_write and test_write),
        # and only the last import's folder is the one VBMS_MANAGER_HOME points at.
        shutil.rmtree(store.home(), ignore_errors=True)
        os.makedirs(store.home(), exist_ok=True)  # a slow previous test's board thread may have recreated it
        mock = mock_bms.MockBMS(silent=True)
        b = board_mod.Board(mock.host, store.BoardStore())
        b.start()
        try:
            self.assertTrue(wait(lambda: b.state == "busy", 15), b.state)
        finally:
            b.stop()
            mock.close()


class TestHttp(Base):
    fixture = "vbms32_18s"

    def test_token_and_host_checks(self):
        httpd = server.serve(self.app, "127.0.0.1", 0)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = server.make_handler(self.app, port)
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def post(path, body, token=None, host=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("X-VBMS-Token", token)
            if host:
                req.add_header("Host", host)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        self.assertEqual(post("/api/charge/prepare", {"host": self.mock.host, "pct": 80})[0], 403)
        self.assertEqual(post("/api/charge/prepare", {"host": self.mock.host, "pct": 80}, "wrong")[0], 403)
        self.assertEqual(post("/api/charge/prepare", {"host": self.mock.host, "pct": 80}, self.app.token,
                              host="evil.example:%d" % port)[0], 403)
        code, body = post("/api/charge/prepare", {"host": self.mock.host, "pct": 80}, self.app.token)
        self.assertEqual(code, 400)  # no cell profile yet, but authorised
        with urllib.request.urlopen(base + "/") as r:
            self.assertIn(self.app.token.encode(), r.read())
        with urllib.request.urlopen(base + "/api/state") as r:
            st = json.loads(r.read())
        self.assertEqual(len(st["boards"]), 1)
        self.assertEqual(len(st["boards"][0]["values"]["cells"]), 18)
        httpd.shutdown()


if __name__ == "__main__":
    unittest.main()

"""The /api/v1 API against the mock BMS: gating, keys, scopes, dry runs, writes, all-boards actions."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.test_write import Base, wait

import access  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import storage  # noqa: E402


class ApiBase(Base):
    def setUp(self):
        super().setUp()
        self.setup_profile()
        self.httpd = server.serve(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, body=None, key=None, headers=()):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", "Bearer " + key)
        for k, v in headers:
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode()

    def enable(self, scopes=("read", "control", "settings"), danger=False):
        self.app.prefs({"api": True, "phrase": "allow api requests"}, role="local")
        return self.app.key_create({"label": "ci", "scopes": list(scopes), "danger": danger})["key"]


class TestGate(ApiBase):
    def test_off_by_default_then_keys(self):
        self.assertEqual(self.call("GET", "/api/v1/status")[0], 403)
        with self.assertRaises(server.ApiError):
            self.app.prefs({"api": True, "phrase": "nope"}, role="local")
        with self.assertRaises(server.ApiError):
            self.app.prefs({"api": True, "phrase": "allow api requests"}, role="paired")
        key = self.enable(scopes=("read",))
        self.assertEqual(self.call("GET", "/api/v1/status")[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/status", key="vbms_wrong")[0], 401)
        code, st = self.call("GET", "/api/v1/status", key=key)
        self.assertEqual(code, 200, st)
        self.assertEqual(st["boards"], 1)
        self.assertNotIn(key, json.dumps(store.BoardStore().settings()))  # only the hash is kept
        # read-only key can't write
        code, r = self.call("POST", "/api/v1/boards/%s/charging" % self.b.mac, {"allowed": False}, key=key)
        self.assertEqual((code, r["code"]), (403, "scope"))
        # revoked key stops working
        self.app.key_revoke({"id": self.app.keys.list()[0]["id"]})
        self.assertEqual(self.call("GET", "/api/v1/status", key=key)[0], 401)

    def test_rate_limit(self):
        self.enable()
        for _ in range(access.KEY_FAILS):
            self.call("GET", "/api/v1/status", key="vbms_bad")
        self.assertEqual(self.call("GET", "/api/v1/status", key="vbms_bad")[0], 429)

    def test_board_ids_and_reads(self):
        key = self.enable()
        for ident in (self.b.mac, self.b.mac.replace(":", ""), self.mock.host, "test"):
            code, r = self.call("GET", "/api/v1/boards/%s" % ident, key=key)
            self.assertEqual(code, 200, ident)
            self.assertEqual(r["mac"], self.b.mac)
        self.assertEqual(self.call("GET", "/api/v1/boards/nope", key=key)[0], 404)
        code, r = self.call("GET", "/api/v1/boards", key=key)
        self.assertEqual(r["boards"][0]["state"], "online")
        self.assertIn("soc", r["boards"][0])
        self.assertIn("limit_pct", r["boards"][0])
        for sub in ("cells", "config", "history?metric=voltage", "charges", "events", "schedule", "storage"):
            code, r = self.call("GET", "/api/v1/boards/%s/%s" % (self.b.mac, sub), key=key)
            self.assertEqual(code, 200, (sub, r))
        self.assertEqual(self.call("GET", "/api/v1/notices", key=key)[0], 200)
        self.assertEqual(self.call("GET", "/api/v1/log/changes", key=key)[0], 200)


class TestWrites(ApiBase):
    def test_charge_limit_dry_then_real(self):
        key = self.enable()
        path = "/api/v1/boards/%s/charge-limit" % self.b.mac
        code, r = self.call("POST", path + "?dry_run=1", {"pct": 80}, key=key)
        self.assertEqual(code, 200, r)
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.mock.writes, [])
        code, r = self.call("POST", path, {"pct": 80}, key=key)
        self.assertEqual(code, 200, r)
        self.assertEqual(len(self.mock.writes), 1)
        self.assertAlmostEqual(self.b.config.values["vc_charge_end"], r["new"]["vc_charge_end"], places=4)
        log = [x for x in store.read_log("changes") if x.get("event") == "config_write"][-1]
        self.assertEqual(log["via"]["kind"], "api")
        self.assertEqual(log["via"]["key"], "ci")
        self.assertIn("API", log["confirm"])
        # out of range is a validation error, nothing written
        code, r = self.call("POST", path, {"pct": 30}, key=key)
        self.assertEqual(code, 422, r)
        self.assertEqual(len(self.mock.writes), 1)

    def test_risk_still_required(self):
        key = self.enable()
        self.app.store.update(self.b.mac, risk_ack=None)
        code, r = self.call("POST", "/api/v1/boards/%s/charge-limit" % self.b.mac, {"pct": 80}, key=key)
        self.assertEqual((code, r["code"]), (409, "risk"))
        self.assertEqual(self.mock.writes, [])

    def test_config_and_danger(self):
        key = self.enable()
        path = "/api/v1/boards/%s/config" % self.b.mac
        code, r = self.call("POST", path, {"changes": {"vc_balance_min": 3.55}}, key=key)
        self.assertEqual(code, 200, r)
        self.assertAlmostEqual(self.b.config.values["vc_balance_min"], 3.55, places=3)
        code, r = self.call("POST", path, {"changes": {"t_charge_min": -5}}, key=key)
        self.assertEqual((code, r["code"]), (403, "danger"), r)
        self.assertEqual(len(self.mock.writes), 1)
        key2 = self.app.key_create({"label": "admin", "scopes": ["read", "settings"], "danger": True})["key"]
        code, r = self.call("POST", path + "?dry_run=1", {"changes": {"t_charge_min": -5}}, key=key2)
        self.assertEqual(code, 200, r)
        self.assertEqual(len(self.mock.writes), 1)

    def test_charging_and_storage(self):
        key = self.enable()
        code, r = self.call("POST", "/api/v1/boards/%s/charging" % self.b.mac, {"allowed": False}, key=key)
        self.assertEqual(code, 200, r)
        self.assertEqual(self.b.runtime_state["charge_allowed"]["value"], 0)
        self.assertTrue(self.b.runtime_state["charge_allowed"]["why"].startswith("by API"))
        before = {k: round(self.b.config.values[k], 3) for k in storage.SAVED}
        code, r = self.call("POST", "/api/v1/boards/%s/storage" % self.b.mac, {"on": True}, key=key)
        self.assertEqual(code, 200, r)
        self.assertEqual(len(self.mock.writes), 1)   # one combined write
        self.assertAlmostEqual(self.b.config.values["vc_charge_end"], 3.75, places=3)
        self.assertAlmostEqual(self.b.config.values["vc_balance_min"], 3.40, places=3)
        self.assertTrue(storage.is_on(self.b.profile()))
        # the charge limit is held by storage mode
        code, r = self.call("POST", "/api/v1/boards/%s/charge-limit" % self.b.mac, {"pct": 90}, key=key)
        self.assertEqual(code, 422, r)
        code, r = self.call("POST", "/api/v1/boards/%s/storage" % self.b.mac, {"on": False}, key=key)
        self.assertEqual(code, 200, r)
        after = {k: round(self.b.config.values[k], 3) for k in storage.SAVED}
        self.assertEqual(before, after)
        self.assertFalse(storage.is_on(self.b.profile()))

    def test_all_boards(self):
        key = self.enable()
        code, r = self.call("POST", "/api/v1/all/charging?dry_run=1", {"allowed": False}, key=key)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["count"], 1)
        code, r = self.call("POST", "/api/v1/all/charging", {"allowed": False}, key=key)
        self.assertEqual(code, 200, r)
        self.assertTrue(r["ok"])
        code, r = self.call("POST", "/api/v1/all/charge-limit", {"pct": 85}, key=key)
        self.assertEqual(code, 200, r)
        self.assertTrue(r["results"][0]["ok"], r)


class TestInternet(ApiBase):
    def test_internet_client_gets_api_only(self):
        key = self.enable()
        xff = [("X-Forwarded-For", "8.8.8.8")]
        code, body = self.call("GET", "/api/state", headers=xff)
        self.assertEqual(code, 403)
        self.assertIn("API", body)
        self.assertEqual(self.call("GET", "/", headers=xff)[0], 403)
        # a same-computer proxy forwarding a LAN client is never "local"
        code, st = self.call("GET", "/api/state", headers=[("X-Forwarded-For", "192.168.1.9")])
        self.assertEqual(code, 200)
        self.assertEqual(st["access"]["role"], "viewer")
        # the API answers, Host header or not
        code, r = self.call("GET", "/api/v1/status", key=key, headers=xff + [("Host", "my.ddns.example")])
        self.assertEqual(code, 200, r)
        self.assertEqual(self.app.keys.list()[0]["last_ip"], "8.8.8.8")
        self.assertIsNotNone(self.app.internet_seen)


if __name__ == "__main__":
    unittest.main()

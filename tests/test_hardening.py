"""1.0.2 hardening: the web server under hostile clients, access rules, and the write path's failure handling."""

import gzip
import json
import os
import socket
import threading
import time
import unittest
import urllib.request

from tests.test_api import ApiBase
from tests.test_write import Base, wait

import access  # noqa: E402
import safe_write  # noqa: E402
import schedule  # noqa: E402
import server  # noqa: E402
import storage  # noqa: E402
import store  # noqa: E402


def raw(port, data, read_timeout=5.0):
    """Send raw bytes, return what came back before the server closed (or the timeout)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=read_timeout)
    try:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


class TestHttp(ApiBase):
    def port(self):
        return self.httpd.server_address[1]

    def test_negative_length_ui_post(self):
        t = time.time()
        out = raw(self.port(), b"POST /api/scan HTTP/1.0\r\nHost: 127.0.0.1\r\nX-VBMS-Token: " + self.app.token.encode()
                  + b"\r\nContent-Length: -1\r\n\r\n")
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        self.assertLess(time.time() - t, 3)          # answered at once, didn't wait for the client to hang up

    def test_api_body_not_read_without_key(self):
        self.enable()
        t = time.time()
        # A huge declared body and no key: refused straight away without reading anything.
        out = raw(self.port(), b"POST /api/v1/boards/x/charging HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                               b"Content-Length: 999999\r\n\r\n")
        self.assertIn(b" 401 ", out.split(b"\r\n")[0])
        self.assertLess(time.time() - t, 3)
        out = raw(self.port(), b"POST /api/v1/boards/x/charging HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                               b"Content-Length: -5\r\n\r\n")
        self.assertIn(b" 401 ", out.split(b"\r\n")[0])

    def test_api_bad_body_with_key(self):
        key = self.enable()
        out = raw(self.port(), ("POST /api/v1/boards/%s/charging HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: "
                                "Bearer %s\r\nContent-Length: -1\r\n\r\n" % (self.b.mac, key)).encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        self.assertIn(b"bad_json", out)

    def test_bad_limit_is_422(self):
        key = self.enable()
        code, r = self.call("GET", "/api/v1/log/changes?limit=abc", key=key)
        self.assertEqual((code, r["code"]), (422, "invalid"))

    def test_internal_error_text_not_returned(self):
        key = self.enable()
        orig = self.app.api._route
        self.app.api._route = lambda *a: 1 / 0
        try:
            code, r = self.call("GET", "/api/v1/status", key=key)
        finally:
            self.app.api._route = orig
        self.assertEqual((code, r["code"]), (500, "internal"))
        self.assertNotIn("ZeroDivision", r["error"])
        self.assertTrue(any(e.get("event") == "api_error" for e in store.read_log("events")))

    def test_slow_client_is_cut_off(self):
        old = server.REQUEST_DEADLINE
        server.REQUEST_DEADLINE = 1
        try:
            s = socket.create_connection(("127.0.0.1", self.port()), timeout=10)
            s.sendall(b"GET /api/state HTTP/1.0\r\nHost: 127.0.0.1\r\n")   # never finishes the headers
            t = time.time()
            try:
                got = s.recv(100)
            except OSError:
                got = b""
            s.close()
            self.assertEqual(got, b"")
            self.assertLess(time.time() - t, 6)
        finally:
            server.REQUEST_DEADLINE = old

    def test_connections_per_address_capped(self):
        old = server.MAX_PER_IP
        server.MAX_PER_IP = 2
        socks = []
        try:
            for _ in range(2):
                s = socket.create_connection(("127.0.0.1", self.port()), timeout=5)
                s.sendall(b"GET / HTTP/1.0\r\n")       # held open, still sending
                socks.append(s)
            time.sleep(0.3)
            extra = socket.create_connection(("127.0.0.1", self.port()), timeout=5)
            extra.sendall(b"GET /api/state HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            try:
                got = extra.recv(100)
            except OSError:
                got = b""
            extra.close()
            self.assertEqual(got, b"")
        finally:
            server.MAX_PER_IP = old
            for s in socks:
                s.close()
        # once those are gone, requests work again
        self.assertTrue(wait(lambda: self.call("GET", "/api/v1/status")[0] == 403))

    def test_state_slim_and_gzip(self):
        req = urllib.request.Request(self.base + "/api/state", headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
            full = json.loads(gzip.decompress(r.read()))
        self.assertIn("cells", full)
        self.assertIn("limits", full)
        with urllib.request.urlopen(self.base + "/api/state?live=1", timeout=10) as r:
            self.assertIsNone(r.headers.get("Content-Encoding"))
            live = json.loads(r.read())
        self.assertNotIn("cells", live)
        self.assertEqual(len(live["boards"]), 1)


class TestPairing(ApiBase):
    def post(self, path, body, headers):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers=dict({"Content-Type": "application/json", "X-VBMS-Token": self.app.token},
                                                  **headers))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_no_pairing_from_internet(self):
        self.app.prefs({"internet_ui": True}, role="local")
        # Through a proxy on this computer, the client is judged by the forwarded (internet) address: a viewer,
        # since the web page is allowed from the internet, but pairing is refused.
        code, r = self.post("/api/pair", {"code": self.app.pairing.code}, {"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(code, 403, r)
        code, r = self.post("/api/pair", {"code": self.app.pairing.code}, {"X-Forwarded-For": "192.168.1.20"})
        self.assertEqual(code, 200, r)

    def test_lan_untrusted(self):
        self.app.prefs({"lan_untrusted": True}, role="local")
        self.assertEqual(access.classify("192.168.1.20", "192.168.1.5", None, self.app.pairing, names=set(),
                                         lan_untrusted=True), "internet")
        self.assertEqual(access.classify("127.0.0.1", "127.0.0.1", None, self.app.pairing, names=set(),
                                         lan_untrusted=True), "local")
        code, _ = self.post("/api/pair", {"code": self.app.pairing.code}, {"X-Forwarded-For": "192.168.1.20"})
        self.assertEqual(code, 403)

    def test_code_rotates_after_wrong_guesses(self):
        p = self.app.pairing
        code = p.code
        for i in range(access.PAIR_ROTATE):
            with self.assertRaises(ValueError):
                p.pair("10.0.%d.1" % i, "x", "")
        self.assertNotEqual(p.code, code)


class TestKeyLimits(unittest.TestCase):
    class Store:
        def __init__(self):
            self.s = {}

        def settings(self):
            return dict(self.s)

        def update_settings(self, **kw):
            self.s.update(kw)

    def test_one_address_cant_block_others(self):
        keys = access.ApiKeys(self.Store())
        token, _ = keys.create("k", ["read"])
        for i in range(100):
            keys.check("vbms_bad", "203.0.113.%d" % (i % 10))
        self.assertTrue(keys.blocked("203.0.113.1"))
        self.assertFalse(keys.blocked("198.51.100.7"))
        self.assertIsNotNone(keys.check(token, "198.51.100.7"))

    def test_key_records_are_copies(self):
        st = self.Store()
        keys = access.ApiKeys(st)
        token, _ = keys.create("k", ["read"])
        before = st.s["api_keys"][0]
        keys.check(token, "198.51.100.7")
        self.assertNotIn("last_ip", before)    # the store's own dict wasn't edited behind its lock


class TestDangerFields(ApiBase):
    def test_tcp_hub_needs_danger(self):
        self.setup_profile()
        key = self.enable(scopes=("read", "settings"))
        code, r = self.call("POST", "/api/v1/boards/%s/config" % self.b.mac,
                            {"changes": {"tcp_hub_url": "evil.example.com"}}, key=key)
        self.assertEqual((code, r["code"]), (403, "danger"), r)
        self.assertEqual(self.mock.writes, [])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.home = store.home()
        os.makedirs(self.home, exist_ok=True)
        self.f = os.path.join(self.home, "boards.json")
        for n in os.listdir(self.home):
            if n.startswith("boards.json"):
                os.remove(os.path.join(self.home, n))

    def test_corrupt_file_set_aside_and_bak_used(self):
        s = store.BoardStore()
        s.update("AA", nickname="keep me")
        s._write_bak()
        with open(self.f, "w") as fh:
            fh.write('{"boards": {"AA": {"nick')       # cut off mid-write
        s2 = store.BoardStore()
        self.assertEqual(s2.get("AA").get("nickname"), "keep me")
        self.assertIn("boards.json.bak", s2.load_warning)
        aside = [n for n in os.listdir(self.home) if n.startswith("boards.json.corrupt-")]
        self.assertEqual(len(aside), 1)
        with open(os.path.join(self.home, aside[0])) as fh:
            self.assertIn("nick", fh.read())

    def test_corrupt_without_bak_never_overwrites(self):
        with open(self.f, "w") as fh:
            fh.write("not json")
        s = store.BoardStore()
        s.update("BB", nickname="new")
        self.assertIn("start empty", s.load_warning)
        self.assertTrue(any(n.startswith("boards.json.corrupt-") for n in os.listdir(self.home)))

    def test_log_rotation_and_tail(self):
        old = store.LOG_MAX_BYTES
        store.LOG_MAX_BYTES = 2000
        try:
            for i in range(200):
                store.append_log("rot", {"i": i, "pad": "x" * 40})
        finally:
            store.LOG_MAX_BYTES = old
        self.assertTrue(os.path.exists(store.path("rot.log.1")))
        self.assertLess(os.path.getsize(store.path("rot.log")), 2200)
        got = store.read_log("rot", 50)
        self.assertEqual([r["i"] for r in got], list(range(150, 200)))   # newest 50, oldest first, across files
        self.assertEqual(store.read_log("rot", 1)[0]["i"], 199)


class TestWriteFailures(Base):
    def setUp(self):
        super().setUp()
        self.setup_profile()

    def backups(self):
        return [b for b in safe_write.list_backups(self.b)]

    def test_two_writes_same_second_keep_both_backups(self):
        for pct in (80, 90):
            prop = safe_write.prepare(self.b, *self.limits(pct), register=False)
            safe_write.apply(self.b, prop)
        self.assertEqual(len(self.backups()), 2)
        for b in self.backups():
            p = os.path.join(store.home(), "backups", b["id"])
            self.assertTrue(os.path.exists(p))
            if os.name != "nt":  # Windows has no owner-only mode bits
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def limits(self, pct):
        start, end, _ = self.app.charge_target(self.b, {"pct": pct})
        return start, end

    def test_no_ack_but_applied(self):
        prop = safe_write.prepare(self.b, *self.limits(80), register=False)
        self.mock.ack_writes = False                   # stores the config, never answers
        res = safe_write.apply(self.b, prop)
        self.assertTrue(res["ok"])
        log = [x for x in store.read_log("changes") if x.get("event") == "config_write"][-1]
        self.assertEqual(log["result"], "ok")
        self.assertIn("no acknowledgement", log["write_note"])

    def test_refused_says_unchanged(self):
        prop = safe_write.prepare(self.b, *self.limits(80), register=False)
        self.mock.reject_writes = True
        with self.assertRaises(safe_write.WriteRefused) as cm:
            safe_write.apply(self.b, prop)
        self.assertIn("still has its previous settings", str(cm.exception))

    def test_dry_run_balancing_changes_nothing(self):
        self.app.store.update(self.b.mac, balance_normal_a=None)
        prop = safe_write.prepare_balance(self.b, False, register=False)
        self.assertIsNotNone(prop)
        self.assertNotIn("balance_normal_a", self.app.store.get(self.b.mac))
        safe_write.apply(self.b, prop)
        self.assertGreater(self.app.store.get(self.b.mac)["balance_normal_a"], 0)

    def test_pending_change_per_board(self):
        safe_write._register({"id": "other", "mac": "11:22:33:44:55:66", "created": time.time()})
        p1 = self.app.prepare({"host": self.mock.host, "pct": 80})
        p2 = self.app.prepare({"host": self.mock.host, "pct": 90})
        self.assertIsNotNone(safe_write.peek("other"))       # another board's confirmation is left open
        self.assertIsNone(safe_write.peek(p1["id"]))         # this board's older one is replaced
        self.assertIsNotNone(safe_write.peek(p2["id"]))

    def test_storage_saved_values_survive_failed_write(self):
        before = {k: round(self.b.config.values[k], 3) for k in storage.SAVED}
        self.mock.corrupt_readback = True                  # the write lands, but the read-back doesn't match
        with self.assertRaises(safe_write.WriteRefused):
            storage.apply(self.b, True, app=self.app)
        self.assertFalse(storage.is_on(self.b.profile()))
        self.mock.corrupt_readback = False
        storage.apply(self.b, True, app=self.app)          # board already has the storage values: no write
        saved = self.b.profile()["storage"]["saved"]
        self.assertEqual(before, {k: round(saved[k], 3) for k in storage.SAVED})

    def test_manual_pause_sent_again_after_reconnect(self):
        self.b.send_runtime("charge_allowed", 0, "by you")
        n = self.b.connect_count
        self.mock.runtime.clear()
        self.b.client.close()
        self.assertTrue(wait(lambda: self.b.connect_count > n and self.b.state == "online", 20))
        self.assertTrue(wait(lambda: self.b.runtime_state.get("charge_allowed")))
        self.assertEqual(self.b.runtime_state["charge_allowed"]["value"], 0)
        self.assertTrue(any(v == 0 for _, v in self.mock.runtime), self.mock.runtime)


class TestOverriding(Base):
    def test_override_taken_back_when_write_fails(self):
        self.setup_profile()
        sch = self.app.scheduler
        orig = schedule.desired
        schedule.desired = lambda *a, **k: {"key": "item1", "value": 0, "kind": "window", "source": {"name": "x"},
                                            "in_window": True}
        try:
            def boom():
                raise safe_write.WriteRefused("nope")
            with self.assertRaises(safe_write.WriteRefused):
                sch.overriding(self.b, "balance", 1, boom)
            self.assertIsNone(sch.run_state(self.b)["balance"]["override"])
            hit, res = sch.overriding(self.b, "balance", 1, lambda: "done")
            self.assertEqual((hit, res), (True, "done"))
            self.assertEqual(sch.run_state(self.b)["balance"]["override"], "item1")
        finally:
            schedule.desired = orig


if __name__ == "__main__":
    unittest.main()

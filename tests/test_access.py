"""Who may do what: loopback vs LAN viewer vs paired device, Host checks, pairing."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.test_write import Base

import access  # noqa: E402
import board as board_mod  # noqa: E402
import server  # noqa: E402

NAMES = {"localhost", "mymac", "mymac.local"}


class Sessions:
    def __init__(self, ok=()):
        self.ok = set(ok)

    def valid(self, tok):
        return tok in self.ok


class TestClassify(unittest.TestCase):
    def c(self, peer, host, cookie=None, ok=()):
        return access.classify(peer, host, cookie, Sessions(ok), NAMES)

    def test_loopback(self):
        self.assertEqual(self.c("127.0.0.1", "127.0.0.1:8642"), "local")
        self.assertEqual(self.c("::1", "[::1]:8642"), "local")
        self.assertEqual(self.c("::ffff:127.0.0.1", "localhost:8642"), "local")
        self.assertEqual(self.c("127.0.0.1", "evil.example:8642"), "reject")   # DNS rebinding
        self.assertEqual(self.c("127.0.0.1", "192.168.1.10:8642"), "reject")

    def test_lan(self):
        self.assertEqual(self.c("192.168.1.20", "192.168.1.10:8642"), "viewer")
        self.assertEqual(self.c("192.168.1.20", "MyMac.local:8642"), "viewer")
        self.assertEqual(self.c("192.168.1.20", "evil.example:8642"), "reject")
        self.assertEqual(self.c("192.168.1.20", "192.168.1.10:8642", "tok", ok={"tok"}), "paired")
        self.assertEqual(self.c("192.168.1.20", "192.168.1.10:8642", "nope", ok={"tok"}), "viewer")
        self.assertEqual(self.c("192.168.1.20", "evil.example", "tok", ok={"tok"}), "reject")


class FakeStore:
    def __init__(self):
        self.s = {}

    def settings(self):
        return dict(self.s)

    def update_settings(self, **kw):
        self.s.update(kw)


class TestPairing(unittest.TestCase):
    def test_pair_and_revoke(self):
        p = access.Pairing(FakeStore())
        with self.assertRaises(ValueError):
            p.pair("10.0.0.2", "000000" if p.code != "000000" else "111111", "x")
        tok = p.pair("10.0.0.2", p.code, "Phone")
        self.assertTrue(p.valid(tok))
        self.assertFalse(p.valid(tok + "x"))
        self.assertNotIn(tok, json.dumps(p.store.s))  # only the hash is stored
        p.revoke(p.devices()[0]["id"])
        self.assertFalse(p.valid(tok))

    def test_rate_limit(self):
        p = access.Pairing(FakeStore())
        wrong = "000000" if p.code != "000000" else "111111"
        for _ in range(access.PAIR_TRIES):
            with self.assertRaises(ValueError):
                p.pair("10.0.0.3", wrong, "")
        with self.assertRaises(ValueError) as e:
            p.pair("10.0.0.3", p.code, "")  # even the right code is refused now
        self.assertIn("too many", str(e.exception))
        p.pair("10.0.0.4", p.code, "")      # other clients aren't locked out


class TestSetupDone(unittest.TestCase):
    def test_migration(self):
        self.assertTrue(board_mod.setup_done({"cell_id": "x", "curve_accepted": "k"}))
        self.assertFalse(board_mod.setup_done({"cell_id": "x"}))
        self.assertFalse(board_mod.setup_done({}))
        self.assertFalse(board_mod.setup_done({"cell_id": "x", "curve_accepted": "k", "setup_done": False}))


class TestViewerHttp(Base):
    """A LAN viewer, simulated by treating every client as non-loopback."""
    fixture = "vbms32_18s"

    def test_viewer_cannot_write(self):
        orig = access.classify
        access.classify = lambda peer, host, cookie, sessions, names=None: orig("192.168.1.20", "192.168.1.10", cookie, sessions, names)
        try:
            httpd = server.serve(self.app, "127.0.0.1", 0)
            httpd.RequestHandlerClass = server.make_handler(self.app, httpd.server_address[1])
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % httpd.server_address[1]

            def post(path, body, cookie=None):
                req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("X-VBMS-Token", self.app.token)
                if cookie:
                    req.add_header("Cookie", cookie)
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read()), r.headers.get("Set-Cookie")
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read()), None

            with urllib.request.urlopen(base + "/api/state") as r:
                st = json.loads(r.read())
            self.assertEqual(st["access"]["role"], "viewer")
            self.assertNotIn("pair_code", st["access"])
            self.assertEqual(post("/api/runtime", {"host": self.mock.host, "cmd": "charge_allowed", "value": 0,
                                                   "confirm": True})[0], 403)
            self.assertEqual(post("/api/config/prepare", {"host": self.mock.host, "changes": {"ble_name": "x"}})[0], 403)
            for path, body in (("/api/logging/settings", {"interval_s": 1}), ("/api/alerts", {"delta": 50}),
                               ("/api/restore/prepare", {"host": self.mock.host, "backup": "x"}),
                               ("/api/board/pack", {"host": self.mock.host, "pack_ah": 10})):
                self.assertEqual(post(path, body)[0], 403, path)
            with urllib.request.urlopen(base + "/api/history?host=%s&metric=power&from=0&to=%d" % (self.mock.host, time.time())) as r:
                self.assertEqual(r.status, 200)  # reading history is fine for viewers
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/api/export?host=%s&token=%s" % (self.mock.host, self.app.token))
            code, _, cookie = post("/api/pair", {"code": self.app.pairing.code, "label": "test"})
            self.assertEqual(code, 200)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            jar = cookie.split(";")[0]
            code, body, _ = post("/api/config/prepare", {"host": self.mock.host, "changes": {"ble_name": "x"}}, jar)
            self.assertEqual(code, 200, body)
            self.assertEqual(self.mock.writes, [])
            self.assertEqual(post("/api/devices/revoke", {"id": "x"}, jar)[0], 403)  # only the host computer
            httpd.shutdown()
        finally:
            access.classify = orig


if __name__ == "__main__":
    unittest.main()

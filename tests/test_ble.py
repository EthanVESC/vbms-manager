"""Bluetooth link: the bridge protocol, switching a board by hand, and the same-board (MAC) check."""

import os
import shutil
import tempfile
import time
import unittest

from tests import mock_ble_bridge, mock_bms

TMP = tempfile.mkdtemp(prefix="vbms_test_")
os.environ.setdefault("VBMS_MANAGER_HOME", TMP)

import ble  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

ID_A = "11111111-2222-3333-4444-555555555555"
ID_B = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
MAC_A = b"\x02\x00\x00\x00\x00\x0a"
MAC_B = b"\x02\x00\x00\x00\x00\x0b"


def wait(pred, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def mac(b):
    return ":".join("%02x" % x for x in b)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(store.home(), ignore_errors=True)
        os.makedirs(store.home())
        self.mocks = []
        self.bridge = mock_ble_bridge.MockBridge()
        self.bridge.env()
        self.app = server.App(start_discovery=False, start_scheduler=False)

    def tearDown(self):
        for b in self.app.boards.values():
            b.stop()
        for m in self.mocks:
            m.close()
        self.bridge.close()

    def mock(self, m):
        mk = mock_bms.MockBMS("vbms32_18s", mac=m)
        self.mocks.append(mk)
        return mk


class TestBridge(Base):
    def test_unavailable_outside_the_app(self):
        self.bridge.close()
        self.assertFalse(ble.available())
        with self.assertRaises(ble.BleError):
            ble.scan()

    def test_search_identifies_by_mac(self):
        self.bridge.add(ID_A, self.mock(MAC_A))
        self.bridge.add(ID_B, self.mock(MAC_B))
        found = {e["id"]: e for e in ble.search()}
        self.assertEqual(found[ID_A]["mac"], mac(MAC_A))
        self.assertEqual(found[ID_B]["mac"], mac(MAC_B))
        self.assertEqual(found[ID_A]["host"], "ble:" + ID_A)

    def test_bluetooth_off(self):
        self.bridge.state = "off"
        self.assertEqual(ble.state(), "off")
        with self.assertRaises(ble.BleError):
            ble.scan()

    def test_bad_token_gets_nothing(self):
        os.environ["VBMS_BLE_TOKEN"] = "wrong"
        with self.assertRaises(ble.BleError):
            ble.state()


class TestBluetoothOnlyBoard(Base):
    def test_add_from_search_and_poll(self):
        m = self.mock(MAC_A)
        self.bridge.add(ID_A, m)
        self.app.ble_scan({})
        self.assertTrue(wait(lambda: not self.app.ble_status["searching"]))
        st = self.app.state()["ble"]
        self.assertTrue(st["available"])
        self.assertEqual([d["mac"] for d in st["found"]], [mac(MAC_A)])
        self.app.add_host({"host": "ble:" + ID_A.lower()})
        b = self.app.boards["ble:" + ID_A]
        self.assertTrue(wait(lambda: b.state == "online" and b.values is not None), b.error)
        snap = b.snapshot()
        self.assertEqual((snap["link"], snap["ble_only"]), ("ble", True))
        self.assertEqual(b.mac, mac(MAC_A))
        self.assertEqual(self.app.state()["ble"]["found"], [])     # added, so no longer offered
        with self.assertRaises(server.ApiError):
            self.app.set_link({"host": b.host, "link": "wifi"})

    def test_cant_add_a_board_twice(self):
        m = self.mock(MAC_A)
        self.app.add_host({"host": m.host})
        b = self.app.boards[m.host]
        self.assertTrue(wait(lambda: b.state == "online"), b.error)
        b.pause()                       # frees the mock's one connection so the search can identify it
        self.bridge.add(ID_A, m)
        self.app.ble_scan({})
        self.assertTrue(wait(lambda: not self.app.ble_status["searching"]))
        with self.assertRaises(server.ApiError):
            self.app.add_host({"host": "ble:" + ID_A})

    def test_bad_id_refused(self):
        with self.assertRaises(server.ApiError):
            self.app.add_host({"host": "ble:not-an-id"})


class TestSwitchLink(Base):
    def setUp(self):
        super().setUp()
        self.m = self.mock(MAC_A)
        self.app.add_host({"host": self.m.host})
        self.b = self.app.boards[self.m.host]
        self.assertTrue(wait(lambda: self.b.state == "online"), self.b.error)

    def test_wifi_to_bluetooth_and_back(self):
        self.bridge.add(ID_B, self.mock(MAC_B))   # another board nearby with the same Bluetooth name
        self.bridge.add(ID_A, self.m)
        self.app.set_link({"host": self.m.host, "link": "ble"})
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b.link == "ble" and self.b._ble_id), self.b.error)
        self.assertEqual(self.b._ble_id, ID_A)
        self.assertEqual(self.b.mac, mac(MAC_A))
        self.assertEqual(store.BoardStore().get(mac(MAC_A))["ble_id"], ID_A)
        self.assertIn(self.m.host, store.BoardStore().settings()["ble_links"])
        n = self.b.connect_count
        self.app.set_link({"host": self.m.host, "link": "wifi"})
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b.connect_count > n), self.b.error)
        self.assertEqual(self.b.link, "wifi")
        self.assertNotIn(self.m.host, store.BoardStore().settings()["ble_links"])

    def test_choice_survives_restart(self):
        self.bridge.add(ID_A, self.m)
        self.app.set_link({"host": self.m.host, "link": "ble"})
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b._ble_id == ID_A), self.b.error)
        for b in self.app.boards.values():
            b.stop()
        app2 = server.App(start_discovery=False, start_scheduler=False)
        try:
            b2 = app2.boards[self.m.host]
            self.assertEqual(b2.link, "ble")
            self.assertTrue(wait(lambda: b2.state == "online"), b2.error)
            self.assertEqual(self.bridge.requests.count("scan"), 1)   # used the remembered id, no new search
        finally:
            for b in app2.boards.values():
                b.stop()

    def test_wrong_board_refused(self):
        # The remembered Bluetooth id now answers as a different board: never use it.
        other = self.mock(MAC_B)
        self.bridge.add(ID_A, other)
        store.BoardStore().update(mac(MAC_A), ble_id=ID_A)
        self.b.boards.update(mac(MAC_A), ble_id=ID_A)
        self.app.set_link({"host": self.m.host, "link": "ble"})
        self.assertTrue(wait(lambda: self.b.state == "offline" and "different one" in self.b.error), self.b.error)
        self.assertEqual(self.b.mac, mac(MAC_A))
        self.assertIsNone(self.b.boards.get(mac(MAC_A)).get("ble_id"))   # forgotten, so it searches next time
        self.assertEqual(other.writes, [])

    def test_not_found(self):
        self.app.set_link({"host": self.m.host, "link": "ble"})
        self.assertTrue(wait(lambda: self.b.state == "offline" and "not found over Bluetooth" in self.b.error), self.b.error)

    def test_needs_the_app(self):
        self.bridge.close()
        with self.assertRaises(server.ApiError):
            self.app.set_link({"host": self.m.host, "link": "ble"})
        self.assertEqual(self.b.link, "wifi")

    def test_write_over_bluetooth(self):
        self.bridge.add(ID_A, self.m)
        self.app.set_link({"host": self.m.host, "link": "ble"})
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b._ble_id == ID_A), self.b.error)
        self.app.ack_risk({"host": self.m.host, "accept": True})
        self.app.set_profile({"host": self.m.host, "cell_id": "reliance_rs50", "nickname": "Test"})
        self.app.accept_curve({"host": self.m.host})
        prop = self.app.prepare({"host": self.m.host, "pct": 80})
        res = self.app.commit({"host": self.m.host, "id": prop["id"]})
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.m.writes), 1)


if __name__ == "__main__":
    unittest.main()

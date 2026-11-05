import os
import shutil
import socket
import unittest

from tests.test_write import TMP, wait

import discovery  # noqa: E402
import server  # noqa: E402
from tests import mock_bms  # noqa: E402
from vesc.client import COMM_FW_VERSION  # noqa: E402


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestScan(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP)
        self.mocks = []

    def tearDown(self):
        for m in self.mocks:
            m.close()

    def mock(self, **kw):
        m = mock_bms.MockBMS("vbms32_18s", **kw)
        self.mocks.append(m)
        return m

    def test_finds_identifies_and_only_reads(self):
        m = self.mock()
        busy = self.mock(mac=b"\x02\x00\x00\x00\x00\x02", silent=True)
        targets = [("127.0.0.1", m.port), ("127.0.0.1", busy.port), ("127.0.0.1", closed_port())]
        d = discovery.Discovery(targets=lambda: (targets, "127.0.0.0/24"))
        d.scan()
        found = {e["host"]: e for e in d.list()}
        self.assertEqual(set(found), {m.host, busy.host})
        self.assertEqual(found[m.host]["mac"], "02:00:00:00:00:01")
        self.assertFalse(found[m.host]["busy"])
        self.assertTrue(found[busy.host]["busy"])
        self.assertEqual(m.received, [COMM_FW_VERSION])
        self.assertEqual(m.writes, [])

    def test_skips_added_boards_and_shows_known_names(self):
        m = self.mock()
        app = server.App(start_discovery=False, start_scheduler=False)
        try:
            app.discovery.targets = lambda: ([("127.0.0.1", m.port)], "127.0.0.0/24")
            app.discovery.scan()
            self.assertEqual([e["host"] for e in app.state()["discovered"]], [m.host])
            app.add_host({"host": m.host})
            b = app.boards[m.host]
            self.assertTrue(wait(lambda: b.mac is not None))
            app.set_profile({"host": m.host, "nickname": "Garage"})
            self.assertEqual(app.state()["discovered"], [])          # added: not offered again
            n = len(m.received)
            app.discovery.scan()
            self.assertEqual(app.state()["discovered"], [])
            app.remove_host({"host": m.host})
            app.discovery.scan()
            self.assertEqual([e["nickname"] for e in app.state()["discovered"]], ["Garage"])
            self.assertGreater(len(m.received), n)
        finally:
            for b in app.boards.values():
                b.stop()


if __name__ == "__main__":
    unittest.main()

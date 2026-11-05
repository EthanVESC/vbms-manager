"""A slow or stalling WiFi link: missed replies are retried, late replies are not mistaken for new ones."""

import os
import shutil
import tempfile
import time
import unittest

from tests import mock_bms

TMP = tempfile.mkdtemp(prefix="vbms_test_")
os.environ.setdefault("VBMS_MANAGER_HOME", TMP)

import board as board_mod  # noqa: E402
import store  # noqa: E402
from vesc.client import VescClient, VescError  # noqa: E402


def wait(pred, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


class TestLateReply(unittest.TestCase):
    def test_late_reply_is_discarded(self):
        mock = mock_bms.MockBMS()
        c = VescClient("127.0.0.1", mock.port, timeout=0.3)
        try:
            c.connect()
            old = mock.values
            new = old[:-1] + bytes([old[-1] ^ 1])
            mock.late_values = 0.6
            with self.assertRaises(VescError):
                c.get_bms_values_raw()
            mock.values = new
            time.sleep(0.6)  # the late reply (old values) is now waiting in the socket
            self.assertEqual(c.get_bms_values_raw(), new)
            self.assertEqual(c.get_bms_values_raw(), new)
        finally:
            c.close()
            mock.close()


class TestStall(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(store.home(), ignore_errors=True)
        os.makedirs(store.home())
        self._saved = board_mod.REPLY_TIMEOUT_S, board_mod.LINK_GRACE_S
        board_mod.REPLY_TIMEOUT_S, board_mod.LINK_GRACE_S = 0.3, 2.5
        self.mock = mock_bms.MockBMS()
        self.b = board_mod.Board(self.mock.host, store.BoardStore())
        self.b.start()
        self.assertTrue(wait(lambda: self.b.state == "online" and self.b.values is not None))

    def tearDown(self):
        self.b.stop()
        self.mock.close()
        board_mod.REPLY_TIMEOUT_S, board_mod.LINK_GRACE_S = self._saved

    def test_short_stall_stays_online(self):
        self.mock.drop_values = 3  # about 1.9 s since the last good read, inside the grace window
        self.assertTrue(wait(lambda: self.b.missed_polls >= 3, 5))
        n = len(self.mock.cmds)
        self.assertTrue(wait(lambda: self.mock.cmds[n:].count(96) >= 2, 5))
        self.assertEqual(self.b.state, "online")
        self.assertEqual(self.b.connect_count, 1)
        self.assertLess(self.b.snapshot()["values_age"], 1.5)

    def test_long_stall_goes_offline(self):
        self.mock.drop_values = 100
        self.assertTrue(wait(lambda: self.b.state == "offline", 5), self.b.state)
        self.assertIn("timeout", self.b.error)


if __name__ == "__main__":
    unittest.main()

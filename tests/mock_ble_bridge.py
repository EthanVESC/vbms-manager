"""A stand-in for VBMS Manager.app's Bluetooth bridge (macos/BLEBridge.swift).

Speaks the same request-line protocol on 127.0.0.1. "Bluetooth devices" are
MockBMS instances: a connect relays the socket to that mock's TCP port, so the
Python side is exercised exactly as with the real bridge.
"""

import json
import os
import secrets
import socket
import threading


class MockBridge:
    def __init__(self):
        self.token = secrets.token_hex(8)
        self.devices = {}         # peripheral id -> {"mock": MockBMS, "name": str, "rssi": int, "advertising": bool}
        self.state = "on"
        self.requests = []        # ops received, oldest first
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def add(self, pid, mock, name="VESC BMS", rssi=-60):
        self.devices[pid.upper()] = {"mock": mock, "name": name, "rssi": rssi, "advertising": True}

    def env(self):
        """Point ble.py at this bridge (what the app does with the real one)."""
        os.environ["VBMS_BLE_PORT"] = str(self.port)
        os.environ["VBMS_BLE_TOKEN"] = self.token

    def close(self):
        self._stop = True
        os.environ.pop("VBMS_BLE_PORT", None)
        os.environ.pop("VBMS_BLE_TOKEN", None)
        try:
            self._srv.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop:
            try:
                s, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(s,), daemon=True).start()

    def _reply(self, s, obj):
        s.sendall(json.dumps(obj).encode() + b"\n")

    def _serve(self, s):
        f = s.makefile("rb")
        try:
            req = json.loads(f.readline() or b"{}")
        except ValueError:
            s.close()
            return
        if req.get("token") != self.token:
            s.close()
            return
        op = req.get("op")
        self.requests.append(op)
        if op == "state":
            self._reply(s, {"ok": True, "state": self.state})
            return s.close()
        if self.state != "on":
            self._reply(s, {"ok": False, "error": "Bluetooth is turned off on this Mac."})
            return s.close()
        if op == "scan":
            found = [{"id": pid, "name": d["name"], "rssi": d["rssi"], "nus": True}
                     for pid, d in self.devices.items() if d["advertising"]]
            self._reply(s, {"ok": True, "found": found})
            return s.close()
        if op == "connect":
            d = self.devices.get(str(req.get("id", "")).upper())
            if d is None or not d["advertising"]:
                self._reply(s, {"ok": False, "error": "no answer over Bluetooth (out of range, asleep, or connected to another app)"})
                return s.close()
            try:
                up = socket.create_connection(("127.0.0.1", d["mock"].port), timeout=2)
            except OSError as e:
                self._reply(s, {"ok": False, "error": str(e)})
                return s.close()
            self._reply(s, {"ok": True, "name": d["name"]})
            self._pipe(s, up)
            return
        self._reply(s, {"ok": False, "error": "unknown op"})
        s.close()

    def _pipe(self, a, b):
        def one(src, dst):
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            for x in (src, dst):
                try:
                    x.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                x.close()
        threading.Thread(target=one, args=(a, b), daemon=True).start()
        threading.Thread(target=one, args=(b, a), daemon=True).start()

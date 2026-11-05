"""Bluetooth link to a board, through VBMS Manager.app.

Python's standard library can't use Bluetooth, so the Mac app relays it
(macos/BLEBridge.swift): it listens on 127.0.0.1:$VBMS_BLE_PORT and, for each
connection, takes one JSON request line with $VBMS_BLE_TOKEN. After a
"connect" the socket carries the board's VESC packets, exactly as TCP does, so
VescClient works on it unchanged.

Outside the app (running from source, or on Windows/Linux) these variables
aren't set and Bluetooth simply isn't offered.

Boards are told apart by the MAC that COMM_FW_VERSION reports over the link,
never by Bluetooth name (they all default to "VESC BMS") or by macOS's
peripheral id (which is only stable on this Mac).
"""

import json
import os
import re
import socket
import threading

from vesc.client import BoardBusy, VescClient, VescError

PREFIX = "ble:"             # host key of a board added over Bluetooth: "ble:<peripheral id>"
SCAN_S = 4.0
CONNECT_TIMEOUT_S = 12.0
ID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")

_lock = threading.Lock()    # one scan at a time: probing holds each board's single Bluetooth slot


class BleError(VescError):
    pass


def available():
    return bool(os.environ.get("VBMS_BLE_PORT") and os.environ.get("VBMS_BLE_TOKEN"))


def is_host(host):
    return isinstance(host, str) and host.startswith(PREFIX)


def host_for(pid):
    return PREFIX + pid.upper()


def id_of(host):
    pid = host[len(PREFIX):] if is_host(host) else ""
    if not ID_RE.match(pid):
        raise BleError("not a Bluetooth board id: %r" % host)
    return pid.upper()


def _request(op, wait, **fields):
    """Open a bridge connection and send one request. Returns (socket, reply)."""
    if not available():
        raise BleError("Bluetooth is only available in the VBMS Manager Mac app")
    try:
        s = socket.create_connection(("127.0.0.1", int(os.environ["VBMS_BLE_PORT"])), timeout=2.0)
    except (OSError, ValueError) as e:
        raise BleError("the app's Bluetooth bridge isn't answering (%s)" % e)
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        req = dict(fields, op=op, token=os.environ["VBMS_BLE_TOKEN"])
        s.sendall(json.dumps(req).encode() + b"\n")
        s.settimeout(wait)
        line = bytearray()
        while not line.endswith(b"\n"):        # byte by byte: board data may follow the line
            ch = s.recv(1)
            if not ch:
                raise BleError("the Bluetooth bridge closed the connection")
            line += ch
            if len(line) > 1_000_000:
                raise BleError("bad reply from the Bluetooth bridge")
        rep = json.loads(line.decode("utf-8"))
    except socket.timeout:
        s.close()
        raise BleError("the Bluetooth bridge didn't answer in time")
    except (OSError, ValueError) as e:
        s.close()
        raise BleError("Bluetooth bridge error (%s)" % e)
    except BaseException:
        s.close()
        raise
    if not rep.get("ok"):
        s.close()
        raise BleError(rep.get("error") or "Bluetooth error")
    return s, rep


def state():
    """"on", "off", "denied", "unsupported" or "unknown"."""
    s, rep = _request("state", 5.0)
    s.close()
    return rep.get("state", "unknown")


def scan(secs=SCAN_S, names=()):
    """Nearby VESC devices: [{"id", "name", "rssi", "nus"}]. A board connected to another app doesn't advertise."""
    s, rep = _request("scan", secs + 5.0, secs=secs, names=list(names))
    s.close()
    return rep.get("found", [])


def opener(pid, timeout=CONNECT_TIMEOUT_S):
    """For VescClient(opener=...): returns a socket that is a byte pipe to the board."""
    def open_():
        s, _ = _request("connect", timeout + 5.0, id=pid, timeout=timeout)
        return s
    return open_


def client(pid, timeout=3.0):
    return VescClient("Bluetooth " + pid[:8], timeout=timeout, opener=opener(pid), label="the board over Bluetooth")


def probe(pid):
    """Identify the board at a peripheral id: {"hw_name", "mac", "version", "busy"}, or None."""
    c = client(pid, timeout=3.0)
    try:
        fw = c.connect()
        return {"hw_name": fw["hw_name"], "mac": fw["mac"], "version": fw["version"], "busy": False}
    except BoardBusy:
        return {"hw_name": None, "mac": None, "version": None, "busy": True}
    except (VescError, ValueError, IndexError):
        return None
    finally:
        c.close()


def search(names=(), skip=()):
    """Scan, then identify each VESC device found (one Bluetooth connection at a time).
    skip: peripheral ids not to probe (boards already connected over Bluetooth)."""
    with _lock:
        out = []
        for d in scan(names=names):
            e = dict(d, id=d["id"].upper(), host=host_for(d["id"]), mac=None, hw_name=None, version=None, busy=False)
            if e["id"] not in skip:
                e.update(probe(e["id"]) or {"failed": True})
            out.append(e)
        return out

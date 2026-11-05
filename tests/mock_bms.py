"""A fake VBMS32 on localhost that replays captured fixtures.

Implements the same framing and the commands VBMS Manager uses. Records every
command id it receives and every config blob written, so tests can assert
exactly what the tool sent.
"""

import os
import socket
import struct
import threading
import time

from vesc import packet
from vesc.confparams import ConfigSchema

HERE = os.path.dirname(os.path.abspath(__file__))

STOCK_LIKE_SCRIPT = b"""
(def is-charging false)
(defun main-ctrl () (loopwhile t {
    (setq charge-ok (and
            (< c-max (if is-charging
                    (bms-get-param 'vc_charge_end)
                    (bms-get-param 'vc_charge_start)
            ))
            (> c-min (bms-get-param 'vc_charge_min))
    ))
    (if (>= c-max (bms-get-param 'vc_charge_start)) (setq ah-cnt-soc 1.0))
}))
(setq charge-ok (and (< c-max (bms-get-param 'vc_charge_end)) true))
"""


def fixture(name):
    d = os.path.join(HERE, "fixtures", name)
    with open(os.path.join(d, "config.xml.z"), "rb") as f:
        xml_z = f.read()
    with open(os.path.join(d, "config.bin"), "rb") as f:
        blob = f.read()
    with open(os.path.join(d, "bms_values.bin"), "rb") as f:
        values = f.read()
    return xml_z, blob, values


def encode_values(d):
    """Inverse of vesc.bms_values.parse_bms_values (COMM_BMS_GET_VALUES reply, with the id byte)."""
    from vesc.buffer import Writer
    w = Writer()
    w.u8(96)
    for k in ("v_tot", "v_charge", "i_in", "i_in_ic"):
        w.f32(d[k], 1e6)
    w.f32(d["ah_cnt"], 1e3)
    w.f32(d["wh_cnt"], 1e3)
    w.u8(len(d["cells"]))
    for c in d["cells"]:
        w.f16(c, 1e3)
    for b in d["balancing"]:
        w.u8(1 if b else 0)
    w.u8(len(d["temps_adc"]))
    for t in d["temps_adc"]:
        w.f16(t, 1e2)
    for k in ("temp_ic", "temp_hum", "humidity", "temp_max_cell"):
        w.f16(d[k], 1e2)
    w.f16(d["soc"], 1e3)
    w.f16(d["soh"], 1e3)
    w.u8(d.get("can_id", 10))
    for k in ("ah_chg_total", "wh_chg_total", "ah_dis_total", "wh_dis_total"):
        w.f32_auto(d[k])
    w.f16(0, 1e-1)
    w.u8(1)
    w.cstr(d.get("status", ""))
    return w.bytes()


class Sim:
    """A pack that cycles rest -> charge to Charge End -> hold on the charger -> unplug -> ride.
    Time runs `speed` times faster for SOC, so a whole cycle takes a few minutes. For UI testing only."""

    def __init__(self, mock, speed=40.0, pack_ah=15.0):
        from vesc.bms_values import parse_bms_values
        self.mock, self.speed, self.ah = mock, speed, pack_ah
        base = parse_bms_values(mock.values)
        self.n = len(base["cells"])
        self.tot = {k: base[k] for k in ("ah_chg_total", "wh_chg_total", "ah_dis_total", "wh_dis_total")}
        self.soc, self.phase, self.since, self.last = 0.72, "rest", time.time(), time.time()
        self.offs = [((k * 37) % 7 - 3) / 1000 for k in range(self.n)]
        self.offs[6 % self.n] -= 0.008  # one low cell

    def end(self):
        return self.mock.schema.decode(self.mock.blob).values["vc_charge_end"]

    def payload(self):
        now = time.time()
        dt, self.last = now - self.last, now
        el = now - self.since
        ph = self.phase
        i_in = {"charge": 4.0, "ride": -9.0}.get(ph, 0.0)  # + = into pack
        self.soc = min(1.0, max(0.05, self.soc + i_in * dt * self.speed / 3600 / self.ah))
        load = 0.035 if ph == "charge" else -0.06 if ph == "ride" else 0.0
        cells = [3.30 + 0.90 * self.soc + load + o for o in self.offs]
        if ph == "charge" and max(cells) >= self.end():
            self.phase, self.since = "hold", now
        elif ph == "hold" and el > 60:
            self.phase, self.since = "unplugged", now
        elif ph == "unplugged" and el > 90:
            self.phase, self.since = "ride", now
        elif ph == "ride" and el > 120:
            self.phase, self.since = "rest", now
        elif ph == "rest" and el > 60:
            self.phase, self.since = "charge", now
        vt = sum(cells)
        if i_in > 0:
            self.tot["ah_chg_total"] += i_in * dt / 3600
            self.tot["wh_chg_total"] += i_in * vt * dt / 3600
        elif i_in < 0:
            self.tot["ah_dis_total"] += -i_in * dt / 3600
            self.tot["wh_dis_total"] += -i_in * vt * dt / 3600
        bal = [ph in ("charge", "hold") and c - min(cells) > 0.006 for c in cells]
        warm = 6.0 if ph == "charge" else 3.0 if ph == "ride" else 0.0
        cell_t = [24.0 + warm + k * 0.4 for k in range(4)]
        charger = ph in ("charge", "hold")
        status = " | ".join(x for x in ("CHARGING" if ph == "charge" else "", "BAL" if any(bal) else "") if x)
        d = {"v_tot": vt, "v_charge": vt + 4.0 if charger else 0.0, "i_in": -i_in, "i_in_ic": -i_in,
             "ah_cnt": 0.0, "wh_cnt": 0.0, "cells": cells, "balancing": bal,
             "temps_adc": [38.0, min(cell_t), max(cell_t), 33.0 + warm, -300.0] + cell_t,
             "temp_ic": 38.0, "temp_hum": 30.0, "humidity": 40.0, "temp_max_cell": max(cell_t),
             "soc": self.soc, "soh": 1.0, "status": status, **self.tot}
        return encode_values(d)


class MockBMS:
    def __init__(self, fixture_name="vbms32_18s", mac=b"\x02\x00\x00\x00\x00\x01", script=STOCK_LIKE_SCRIPT,
                 silent=False):
        self.xml_z, self.blob, self.values = fixture(fixture_name)
        self.schema = ConfigSchema.from_compressed(self.xml_z)
        self.mac = mac
        self.script = script
        self.silent = silent          # accept connections but never answer (simulates "busy")
        self.reject_writes = False    # simulate firmware refusing the blob
        self.corrupt_readback = False # simulate a write that didn't stick
        self.ack_writes = True        # False: store a config write but never answer (a lost acknowledgement)
        self.drop_values = 0          # ignore this many value requests (a stalled WiFi link)
        self.late_values = 0.0        # answer the next value request this many seconds late
        self.sim = None               # Sim(): time-varying values instead of the fixture
        self.lisp_flash = bytearray()  # the "lisp" partition: u32 len, u16 crc, u16 flags, code...
        self.lisp_running = True
        self.lisp_ops = []            # ("erase"|"write"|"run", arg)
        self.lisp_reject_erase = False
        self.lisp_fail_write_at = None  # refuse the write at this offset (once per value: set again to repeat)
        self.lisp_corrupt = False     # store a flipped byte so the read-back differs
        self.lisp_fail_start = False  # SET_RUNNING 1 replies not-OK
        self.received = []            # command ids
        self.writes = []              # blobs written
        self.cmds = []                # command ids received, oldest first
        self.runtime = []             # (cmd, value)
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def host(self):
        return "127.0.0.1:%d" % self.port

    def close(self):
        self._stop = True
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
            self._serve(s)  # one client at a time, like the firmware

    def _serve(self, s):
        p = packet.Parser()
        s.settimeout(0.5)
        while not self._stop:
            try:
                d = s.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not d:
                break
            for pl in p.feed(d):
                self.received.append(pl[0])
                if self.silent:
                    continue
                for r in self._handle(pl):
                    s.sendall(packet.encode(r))
        s.close()

    def _handle(self, pl):
        cmd = pl[0]
        self.cmds.append(cmd)
        if cmd == 0:
            yield (bytes([0, 7, 0]) + b"VBMS32\x00" + self.mac + bytes(6) +
                   bytes([0, 0, 2, 1, 0, 0, 1, 0]) + b"micro\x00" + b"\x11\x1a\xed\xd0")
        elif cmd == 92:
            ln, ofs = struct.unpack(">ii", pl[2:10])
            if ln + ofs <= len(self.xml_z):
                yield bytes([92, 0]) + struct.pack(">ii", len(self.xml_z), ofs) + self.xml_z[ofs:ofs + ln]
        elif cmd == 93:
            blob = self.blob
            if self.corrupt_readback and self.writes:
                blob = blob[:-1] + bytes([blob[-1] ^ 1])
            yield bytes([93, 0]) + blob
        elif cmd == 95:
            new = pl[2:]
            self.writes.append(new)
            try:
                cfg = self.schema.decode(new)
                ok = cfg.signature == self.schema.decode(self.blob).signature and not self.reject_writes
            except Exception:
                ok = False
            if ok:
                self.blob = new
                if self.ack_writes:
                    yield bytes([95])
            else:
                yield bytes([21]) + b"Warning: Could not set configuration"
        elif cmd == 96:
            if self.drop_values:
                self.drop_values -= 1
                return
            p = self.sim.payload() if self.sim else self.values
            if self.late_values:
                time.sleep(self.late_values)
                self.late_values = 0.0
            yield p
        elif cmd == 130:
            ln, ofs = struct.unpack(">ii", pl[1:9])
            if ln + ofs <= len(self.script):
                yield bytes([130]) + struct.pack(">ii", len(self.script), ofs) + self.script[ofs:ofs + ln]
        elif cmd == 132:
            self.lisp_ops.append(("erase", struct.unpack(">i", pl[1:5])[0]))
            self.lisp_running = False  # the firmware stops LispBM before erasing
            if self.lisp_reject_erase:
                yield bytes([132, 0])
                return
            self.lisp_flash = bytearray()
            self.script = b""
            yield bytes([132, 1])
        elif cmd == 131:
            ofs = struct.unpack(">I", pl[1:5])[0]
            data = pl[5:]
            self.lisp_ops.append(("write", ofs))
            if self.lisp_fail_write_at == ofs:
                self.lisp_fail_write_at = None
                yield bytes([131, 0]) + struct.pack(">I", ofs)
                return
            if len(self.lisp_flash) < ofs + len(data):
                self.lisp_flash += b"\xff" * (ofs + len(data) - len(self.lisp_flash))
            self.lisp_flash[ofs:ofs + len(data)] = data
            f = bytes(self.lisp_flash)
            self.script = b""
            if len(f) >= 6:
                n, crc = struct.unpack(">IH", f[:6])
                vb = f[6:6 + n + 2]
                if len(vb) == n + 2 and packet.crc16(vb) == crc:
                    self.script = vb[2:]
                    if self.lisp_corrupt:
                        self.script = self.script[:-4] + bytes([self.script[-4] ^ 1]) + self.script[-3:]
            yield bytes([131, 1]) + struct.pack(">I", ofs)
        elif cmd == 133:
            run = bool(pl[1])
            self.lisp_ops.append(("run", int(run)))
            ok = (not run) or (bool(self.script) and not self.lisp_fail_start)
            self.lisp_running = run and ok
            yield bytes([133, 1 if ok else 0])
        elif cmd in (97, 100):
            self.runtime.append((cmd, pl[1] if len(pl) > 1 else None))


if __name__ == "__main__":
    # python3 -m tests.mock_bms [fixture] [port] [--sim] [--lisp FILE]  - run a standalone fake board for UI testing
    import sys
    sim = "--sim" in sys.argv
    sys.argv = [a for a in sys.argv if a != "--sim"]
    lisp = None
    if "--lisp" in sys.argv:
        i = sys.argv.index("--lisp")
        with open(sys.argv[i + 1], "rb") as f:
            lisp = f.read()
        del sys.argv[i:i + 2]
    m = MockBMS(sys.argv[1] if len(sys.argv) > 1 else "vbms32_32s")
    if lisp is not None:
        m.script = lisp
    if len(sys.argv) > 2:
        m.close()
        m._srv = socket.socket()
        m._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        m._srv.bind(("127.0.0.1", int(sys.argv[2])))
        m._srv.listen(1)
        m.port = int(sys.argv[2])
        m._stop = False
        m.mac = b"\x02\x00\x00\x00" + m.port.to_bytes(2, "big")  # distinct board per port
        threading.Thread(target=m._accept, daemon=True).start()
    if sim:
        m.sim = Sim(m)
    print("mock BMS on", m.host, "(simulating charge cycles)" if sim else "", flush=True)
    while True:
        time.sleep(1)
        if m.writes:
            print("writes:", len(m.writes), "last start/end:",
                  [round(m.schema.decode(m.writes[-1]).values[k], 4) for k in ("vc_charge_start", "vc_charge_end")],
                  flush=True)
            m.writes.clear()

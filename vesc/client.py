"""TCP client for VESC Express based devices (VBMS32 etc.).

The firmware serves one TCP client at a time on port 65102. A second client's
connect() succeeds (it sits in the listen backlog) but never gets replies, so
"connected but silent" means the board is busy with another client such as
VESC Tool.

Command gating:
  - request() only sends commands from READ_COMMANDS.
  - send_runtime_command() only sends the RAM-only BMS controls.
  - write_custom_config() and upload_lisp() / lisp_set_running() are the only flash-writing
    calls; only safe_write.py uses them.
"""

import socket
import struct
import threading
import time

from . import packet
from .confparams import ConfigSchema

PORT = 65102

COMM_FW_VERSION = 0
COMM_PRINT = 21
COMM_GET_CUSTOM_CONFIG_XML = 92
COMM_GET_CUSTOM_CONFIG = 93
COMM_SET_CUSTOM_CONFIG = 95
COMM_BMS_GET_VALUES = 96
COMM_BMS_SET_CHARGE_ALLOWED = 97
COMM_BMS_FORCE_BALANCE = 100
COMM_LISP_READ_CODE = 130
COMM_LISP_WRITE_CODE = 131
COMM_LISP_ERASE_CODE = 132
COMM_LISP_SET_RUNNING = 133

READ_COMMANDS = {COMM_FW_VERSION, COMM_GET_CUSTOM_CONFIG_XML, COMM_GET_CUSTOM_CONFIG,
                 COMM_BMS_GET_VALUES, COMM_LISP_READ_CODE}
RUNTIME_COMMANDS = {COMM_BMS_SET_CHARGE_ALLOWED, COMM_BMS_FORCE_BALANCE}

CHUNK = 400
LISP_WRITE_CHUNK = 384        # what VESC Tool sends
LISP_ERASE_TIMEOUT = 10.0     # erases the whole partition (VESC Tool waits 8 s)


class VescError(Exception):
    pass


class BoardBusy(VescError):
    pass


class VescClient:
    def __init__(self, host, port=PORT, timeout=3.0, connect_timeout=5.0, opener=None, label=None):
        """opener: optional callable returning an already connected socket (the Bluetooth bridge, ble.py);
        label: how to name the device in errors."""
        self.host = host
        self.port = port
        self.opener = opener
        self.label = label or host
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._sock = None
        self._parser = packet.Parser()
        self._lock = threading.RLock()
        self._schema = None  # config layout, read once per connection
        self.prints = []  # text from COMM_PRINT, newest last

    # -- connection ---------------------------------------------------------

    def connect(self):
        with self._lock:
            self.close()
            if self.opener is not None:
                s = self.opener()
            else:
                try:
                    s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
                except OSError as e:
                    raise VescError("cannot reach %s:%d (%s)" % (self.host, self.port, e))
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = s
            self._parser = packet.Parser()
            self._schema = None
            try:
                return self.fw_version()
            except VescError:
                self.close()
                raise BoardBusy("%s accepted the connection but did not answer. Another client "
                                "(e.g. VESC Tool) is probably connected to it." % self.label)

    def close(self):
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._sock.close()
                self._sock = None

    @property
    def connected(self):
        return self._sock is not None

    # -- low level ----------------------------------------------------------

    def _exchange(self, payload: bytes, want_id: int, timeout=None, match=None):
        if self._sock is None:
            raise VescError("not connected")
        timeout = self.timeout if timeout is None else timeout
        self._drain()
        try:
            self._sock.sendall(packet.encode(payload))
        except OSError as e:
            self.close()
            raise VescError("send failed: %s" % e)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise VescError("timeout waiting for reply to command %d" % payload[0])
            self._sock.settimeout(min(left, 0.5))
            try:
                data = self._sock.recv(8192)
            except socket.timeout:
                continue
            except OSError as e:
                self.close()
                raise VescError("receive failed: %s" % e)
            if not data:
                self.close()
                raise VescError("connection closed by board")
            for p in self._parser.feed(data):
                if p[0] == COMM_PRINT:
                    self.prints.append(p[1:].decode("utf-8", errors="replace"))
                    del self.prints[:-50]
                    continue
                if p[0] == want_id and (match is None or match(p)):
                    return p

    def _drain(self):
        """Throw away replies that are already waiting: a reply that came in after its request timed out
        would otherwise be taken as the answer to the next request of the same command."""
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    data = self._sock.recv(8192)
                except (BlockingIOError, InterruptedError):
                    return
                except OSError as e:
                    self.close()
                    raise VescError("receive failed: %s" % e)
                if not data:
                    return  # closed by the board; the send/recv that follows reports it
                for p in self._parser.feed(data):
                    if p[0] == COMM_PRINT:
                        self.prints.append(p[1:].decode("utf-8", errors="replace"))
                        del self.prints[:-50]
        finally:
            if self._sock is not None:
                self._sock.settimeout(self.timeout)

    def request(self, payload: bytes, timeout=None, match=None) -> bytes:
        if payload[0] not in READ_COMMANDS:
            raise VescError("command %d is not a read command" % payload[0])
        with self._lock:
            return self._exchange(payload, payload[0], timeout, match)

    # -- reads --------------------------------------------------------------

    def fw_version(self) -> dict:
        p = self.request(bytes([COMM_FW_VERSION]))
        i = 3
        end = p.index(0, i)
        hw = p[i:end].decode(errors="replace")
        i = end + 1
        uuid = p[i:i + 12]
        i += 12
        info = {"major": p[1], "minor": p[2], "hw_name": hw,
                "mac": ":".join("%02x" % b for b in uuid[:6]),
                "uuid": uuid.hex()}
        if len(p) >= i + 4:
            info["test_version"] = p[i + 1]
            info["hw_type"] = p[i + 2]
            info["custom_config_num"] = p[i + 3]
        # After: phase filters, hw qml, qml flags, nrf flags, then FW name string
        j = i + 8
        if len(p) > j:
            try:
                end = p.index(0, j)
                info["fw_name"] = p[j:end].decode(errors="replace")
            except ValueError:
                pass
        info["version"] = "%d.%02d" % (info["major"], info["minor"])
        return info

    def _read_chunked(self, cmd: int, prefix: bytes, data_offset: int) -> bytes:
        """Read a chunked resource. Reply layout: id, prefix, total:i32, offset:i32, data."""
        hdr = 1 + len(prefix)

        def at(ofs):
            return lambda p: len(p) >= hdr + 8 and struct.unpack(">i", p[hdr + 4:hdr + 8])[0] == ofs

        p = self.request(bytes([cmd]) + prefix + struct.pack(">ii", 0, 0), match=at(0))
        total = struct.unpack(">i", p[hdr:hdr + 4])[0]
        out = bytearray()
        while len(out) < total:
            n = min(CHUNK, total - len(out))
            p = self.request(bytes([cmd]) + prefix + struct.pack(">ii", n, len(out)),
                             match=at(len(out)))
            chunk = p[data_offset:]
            if len(chunk) != n:
                raise VescError("short chunk for command %d" % cmd)
            out += chunk
        return bytes(out)

    def get_config_schema(self, fresh=False) -> ConfigSchema:
        """The config layout (~18 round trips to download). It can only change with a firmware
        update, which restarts the BMS and so the connection, so it's read once per connection."""
        with self._lock:
            if self._schema is None or fresh:
                data = self._read_chunked(COMM_GET_CUSTOM_CONFIG_XML, b"\x00", 10)
                self._schema = ConfigSchema.from_compressed(data)
            return self._schema

    def get_config_blob(self) -> bytes:
        p = self.request(bytes([COMM_GET_CUSTOM_CONFIG, 0]),
                         match=lambda p: len(p) > 2 and p[1] == 0)
        return p[2:]

    def get_bms_values_raw(self) -> bytes:
        return self.request(bytes([COMM_BMS_GET_VALUES]))

    def read_lisp_code(self) -> bytes:
        with self._lock:
            return self._read_chunked(COMM_LISP_READ_CODE, b"", 9)

    # -- runtime controls (RAM only, no flash write) -------------------------

    def send_runtime_command(self, cmd: int, value: int):
        if cmd not in RUNTIME_COMMANDS:
            raise VescError("command %d is not an allowed runtime command" % cmd)
        if value not in (0, 1):
            raise VescError("runtime command value must be 0 or 1")
        with self._lock:
            if self._sock is None:
                raise VescError("not connected")
            # These commands send no reply.
            try:
                self._sock.sendall(packet.encode(bytes([cmd, value])))
            except OSError as e:
                self.close()
                raise VescError("send failed: %s" % e)

    # -- config write (flash) -------------------------------------------------

    def write_custom_config(self, blob: bytes, timeout=5.0):
        """Send COMM_SET_CUSTOM_CONFIG. Only safe_write.py should call this."""
        with self._lock:
            if self._sock is not None:
                self._drain()  # so stale prints don't end up in this write's error message
            self.prints.clear()
            payload = bytes([COMM_SET_CUSTOM_CONFIG, 0]) + blob
            try:
                self._exchange(payload, COMM_SET_CUSTOM_CONFIG, timeout)
            except VescError as e:
                msg = "; ".join(self.prints)
                raise VescError("config write not acknowledged (%s)%s" % (e, (": " + msg) if msg else ""))

    # -- Lisp script write (flash) -------------------------------------------

    def _lisp_cmd(self, payload: bytes, timeout, match=None):
        try:
            return self._exchange(payload, payload[0], timeout, match)
        except VescError as e:
            msg = "; ".join(self.prints)
            raise VescError("%s%s" % (e, (" (board said: " + msg + ")") if msg else ""))

    def upload_lisp(self, stored: bytes):
        """Replace the Lisp script, as VESC Tool's package install does, but stop before restarting it.
        Only safe_write.py should call this.

        stored: exactly what read_lisp_code() should return afterwards (code, NUL, import table).
        Flash layout: u32 len(vb)-2, u16 crc16(vb), then vb = u16 flags 0 + stored.
        Erasing stops LispBM and clears the partition (including its saved image); the script stays
        stopped until lisp_set_running(True). Raises VescError unless every step is acknowledged and
        the code reads back identical."""
        vb = b"\x00\x00" + stored
        blob = struct.pack(">IH", len(vb) - 2, packet.crc16(vb)) + vb
        with self._lock:
            if self._sock is not None:
                self._drain()
            self.prints.clear()
            p = self._lisp_cmd(bytes([COMM_LISP_ERASE_CODE]) + struct.pack(">i", len(vb) + 100), LISP_ERASE_TIMEOUT)
            if len(p) < 2 or not p[1]:
                raise VescError("the board refused to erase the script area")
            for ofs in range(0, len(blob), LISP_WRITE_CHUNK):
                at = lambda r, ofs=ofs: len(r) >= 6 and struct.unpack(">I", r[2:6])[0] == ofs
                for attempt in range(3):
                    try:
                        p = self._lisp_cmd(bytes([COMM_LISP_WRITE_CODE]) + struct.pack(">I", ofs)
                                           + blob[ofs:ofs + LISP_WRITE_CHUNK], 2.0, at)
                        break
                    except VescError:
                        if attempt == 2 or self._sock is None:
                            raise
                if not p[1]:
                    raise VescError("the board refused the script write at offset %d" % ofs)
            back = self._read_chunked(COMM_LISP_READ_CODE, b"", 9)
            if back != stored:
                raise VescError("the script read back from the board doesn't match what was written")

    def lisp_set_running(self, running: bool) -> bool:
        """Start (True: restart LispBM from flash) or pause the script. True if the board says OK."""
        with self._lock:
            p = self._lisp_cmd(bytes([COMM_LISP_SET_RUNNING, 1 if running else 0]), 5.0)
            return len(p) >= 2 and bool(p[1])

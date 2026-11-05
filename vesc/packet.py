"""VESC packet framing.

Frame layout (same on UART, USB and TCP):
    0x02 <len:u8>  payload crc16 0x03   (len < 256)
    0x03 <len:u16> payload crc16 0x03   (len < 65536)
    0x04 <len:u24> payload crc16 0x03
CRC is CRC16-XMODEM (poly 0x1021, init 0) over the payload, big-endian.
"""

_CRC_TABLE = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) if _c & 0x8000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFF)


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def encode(payload: bytes) -> bytes:
    n = len(payload)
    if n == 0:
        raise ValueError("empty payload")
    if n < 256:
        head = bytes([2, n])
    elif n < 65536:
        head = bytes([3]) + n.to_bytes(2, "big")
    elif n < (1 << 24):
        head = bytes([4]) + n.to_bytes(3, "big")
    else:
        raise ValueError("payload too large")
    return head + payload + crc16(payload).to_bytes(2, "big") + b"\x03"


class Parser:
    """Incremental stream parser. Feed bytes, pop complete payloads."""

    def __init__(self, max_len=8192):
        # Replies we request are small (chunks of 400 bytes), so a huge length
        # field means we're looking at garbage; skipping it keeps resync fast.
        self.max_len = max_len
        self._buf = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> list:
        self._buf += data
        out = []
        while True:
            p = self._next()
            if p is None:
                return out
            out.append(p)

    def _next(self):
        buf = self._buf
        while True:
            # Resync on a valid start byte
            while buf and buf[0] not in (2, 3, 4):
                del buf[0]
            if not buf:
                return None
            hl = buf[0] - 1
            if len(buf) < 1 + hl:
                return None
            n = int.from_bytes(buf[1:1 + hl], "big")
            if n == 0 or n > self.max_len:
                del buf[0]
                continue
            total = 1 + hl + n + 3
            if len(buf) < total:
                # Could be a real frame still arriving, or a stray byte after a
                # corrupt frame. If a complete valid frame starts later in the
                # buffer, the candidate at 0 is junk: skip to that frame.
                j = self._find_valid(1)
                if j is None:
                    return None
                del buf[:j]
                continue
            payload = bytes(buf[1 + hl:1 + hl + n])
            crc = int.from_bytes(buf[1 + hl + n:1 + hl + n + 2], "big")
            if buf[total - 1] != 3 or crc != crc16(payload):
                self.crc_errors += 1
                del buf[0]
                continue
            del buf[:total]
            return payload

    def _find_valid(self, start):
        buf = self._buf
        for i in range(start, len(buf)):
            if buf[i] not in (2, 3, 4):
                continue
            hl = buf[i] - 1
            if len(buf) < i + 1 + hl:
                return None
            n = int.from_bytes(buf[i + 1:i + 1 + hl], "big")
            total = 1 + hl + n + 3
            if n == 0 or n > self.max_len or len(buf) < i + total:
                continue
            payload = bytes(buf[i + 1 + hl:i + 1 + hl + n])
            crc = int.from_bytes(buf[i + 1 + hl + n:i + 1 + hl + n + 2], "big")
            if buf[i + total - 1] == 3 and crc == crc16(payload):
                return i
        return None

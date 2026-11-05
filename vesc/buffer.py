"""Big-endian buffer helpers matching the VESC firmware's buffer.c."""

import math
import struct


def _f32(x: float) -> float:
    """Round a Python float to the nearest float32, like the firmware's float variables."""
    return struct.unpack(">f", struct.pack(">f", x))[0]


def float32_auto_encode(number: float) -> bytes:
    # Port of buffer_append_float32_auto(). Subnormals become 0.
    number = _f32(number)
    if not math.isfinite(number):
        raise ValueError("non-finite value")
    if abs(number) < 1.5e-38:
        number = 0.0
    sig, e = math.frexp(number)
    sig_abs = abs(sig)
    sig_i = 0
    if sig_abs >= 0.5:
        sig_i = int((sig_abs - 0.5) * 2.0 * 8388608.0)
        e += 126
    res = ((e & 0xFF) << 23) | (sig_i & 0x7FFFFF)
    if sig < 0:
        res |= 1 << 31
    return res.to_bytes(4, "big")


def float32_auto_decode(b: bytes) -> float:
    # Port of buffer_get_float32_auto().
    res = int.from_bytes(b[:4], "big")
    e = (res >> 23) & 0xFF
    sig_i = res & 0x7FFFFF
    neg = bool(res & (1 << 31))
    sig = 0.0
    if e != 0 or sig_i != 0:
        sig = sig_i / (8388608.0 * 2.0) + 0.5
        e -= 126
    if neg:
        sig = -sig
    return _f32(math.ldexp(sig, e))


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("buffer underrun at %d (+%d of %d)" % (self.pos, n, len(self.data)))
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self): return self._take(1)[0]
    def i8(self): return struct.unpack(">b", self._take(1))[0]
    def u16(self): return struct.unpack(">H", self._take(2))[0]
    def i16(self): return struct.unpack(">h", self._take(2))[0]
    def u32(self): return struct.unpack(">I", self._take(4))[0]
    def i32(self): return struct.unpack(">i", self._take(4))[0]
    def f16(self, scale): return self.i16() / scale
    def f32(self, scale): return self.i32() / scale
    def f32_auto(self): return float32_auto_decode(self._take(4))
    def raw(self, n): return self._take(n)

    def cstr(self) -> str:
        try:
            end = self.data.index(0, self.pos)
        except ValueError:
            raise ValueError("unterminated string at %d" % self.pos)
        s = self.data[self.pos:end].decode("utf-8", errors="replace")
        self.pos = end + 1
        return s


class Writer:
    def __init__(self):
        self.buf = bytearray()

    def u8(self, v): self.buf += struct.pack(">B", v)
    def i8(self, v): self.buf += struct.pack(">b", v)
    def u16(self, v): self.buf += struct.pack(">H", v)
    def i16(self, v): self.buf += struct.pack(">h", v)
    def u32(self, v): self.buf += struct.pack(">I", v)
    def i32(self, v): self.buf += struct.pack(">i", v)
    def f16(self, v, scale): self.i16(int(round(v * scale)))
    def f32(self, v, scale): self.i32(int(round(v * scale)))
    def f32_auto(self, v): self.buf += float32_auto_encode(v)
    def raw(self, b): self.buf += b

    def cstr(self, s: str):
        self.buf += s.encode("utf-8") + b"\x00"

    def bytes(self) -> bytes:
        return bytes(self.buf)

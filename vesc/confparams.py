"""Custom-config handling driven by the XML description the device sends.

The device describes its own config layout (COMM_GET_CUSTOM_CONFIG_XML). We use
that description to decode the binary config blob (COMM_GET_CUSTOM_CONFIG), so
the layout always matches the firmware that's actually on the board.

Blob layout: <signature:u32> then each field in <SerOrder> order.
Field encodings (matching VESC Tool's ConfigParams):
    type 1 double : vTx 7 int16/scale, vTx 8 int32/scale, vTx 9 float32_auto
    type 2 int    : vTx 1 u8, 2 i8, 3 u16, 4 i16, 5 u32, 6 i32
    type 3 string : NUL-terminated UTF-8
    type 4 enum, type 5 bool : int8
    type 6 bitfield : u8
"""

import html
import re
import xml.etree.ElementTree as ET
import zlib
from xml.sax.saxutils import escape

from .buffer import Reader, Writer, float32_auto_encode

T_DOUBLE, T_INT, T_STRING, T_ENUM, T_BOOL, T_BITFIELD = 1, 2, 3, 4, 5, 6

# Names whose values are secrets and get redacted in UI and exports by default
SECRET_FIELDS = {"wifi_sta_key", "wifi_ap_key", "tcp_hub_pass", "ble_pin"}

# VESC Tool writes this in <ConfigVersion> of saved custom configs
VT_CONFIG_VERSION = 4


class ConfigError(Exception):
    pass


def qt_uncompress(data: bytes) -> bytes:
    """Undo Qt's qCompress(): 4-byte big-endian uncompressed length + zlib stream."""
    if len(data) < 5:
        raise ConfigError("compressed XML too short")
    expected = int.from_bytes(data[:4], "big")
    out = zlib.decompress(data[4:])
    if len(out) != expected:
        raise ConfigError("XML length mismatch: header %d, got %d" % (expected, len(out)))
    return out


class FieldDef:
    __slots__ = ("name", "long_name", "type", "vtx", "scale", "enum_names", "suffix",
                 "min", "max", "decimals", "step", "max_len", "description")

    def __init__(self, name, el):
        self.name = name
        self.long_name = el.findtext("longName") or name
        self.type = int(el.findtext("type") or 0)
        self.vtx = int(el.findtext("vTx") or 0)
        self.scale = float(el.findtext("vTxDoubleScale") or 1)
        self.enum_names = [e.text or "" for e in el.findall("enumNames")]
        self.suffix = el.findtext("suffix") or ""
        if self.type == T_DOUBLE:
            self.min = _num(el.findtext("minDouble"))
            self.max = _num(el.findtext("maxDouble"))
        elif self.type == T_INT:
            self.min = _num(el.findtext("minInt"))
            self.max = _num(el.findtext("maxInt"))
        else:
            self.min = self.max = None
        self.decimals = int(el.findtext("editorDecimalsDouble") or 3)
        self.step = _num(el.findtext("stepDouble") if self.type == T_DOUBLE else el.findtext("stepInt"))
        self.max_len = int(el.findtext("maxLen") or 0) if self.type == T_STRING else None
        self.description = _plain(el.findtext("description") or "")

    def stored(self, val):
        """The value the device will hold after writing val (float32 / fixed-point rounding)."""
        w = Writer()
        self.write(w, val)
        return self.read(Reader(w.bytes()))

    def read(self, r: Reader):
        t, v = self.type, self.vtx
        if t == T_DOUBLE:
            if v == 7: return r.f16(self.scale)
            if v == 8: return r.f32(self.scale)
            if v == 9: return r.f32_auto()
        elif t == T_INT:
            if v == 1: return r.u8()
            if v == 2: return r.i8()
            if v == 3: return r.u16()
            if v == 4: return r.i16()
            if v == 5: return r.u32()
            if v == 6: return r.i32()
        elif t == T_STRING:
            return r.cstr()
        elif t in (T_ENUM, T_BOOL):
            return r.i8()
        elif t == T_BITFIELD:
            return r.u8()
        raise ConfigError("unsupported encoding for %s (type %d, vTx %d)" % (self.name, t, v))

    def write(self, w: Writer, val):
        t, v = self.type, self.vtx
        if t == T_DOUBLE:
            if v == 7: return w.f16(val, self.scale)
            if v == 8: return w.f32(val, self.scale)
            if v == 9: return w.f32_auto(val)
        elif t == T_INT:
            if v == 1: return w.u8(val)
            if v == 2: return w.i8(val)
            if v == 3: return w.u16(val)
            if v == 4: return w.i16(val)
            if v == 5: return w.u32(val)
            if v == 6: return w.i32(val)
        elif t == T_STRING:
            return w.cstr(val)
        elif t in (T_ENUM, T_BOOL):
            return w.i8(val)
        elif t == T_BITFIELD:
            return w.u8(val)
        raise ConfigError("unsupported encoding for %s (type %d, vTx %d)" % (self.name, t, v))


def _plain(desc):
    """VESC Tool descriptions are Qt rich-text HTML; reduce them to plain text."""
    t = re.sub(r"<style.*?</style>", "", desc, flags=re.S)
    t = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def _num(s):
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


class ConfigSchema:
    """Field definitions and serialization order parsed from the device XML."""

    def __init__(self, xml_text: str):
        self.xml_text = xml_text
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise ConfigError("bad config XML: %s" % e)
        params = root.find("Params")
        if params is None:
            raise ConfigError("config XML has no <Params>")
        self.config_name = params.findtext("config_name/valString") or ""
        self.order = [e.text for e in root.iter("ser") if e.text]
        if not self.order:
            raise ConfigError("config XML has no serialization order")
        self.fields = {}
        for name in self.order:
            el = params.find(name)
            if el is None:
                raise ConfigError("serialized field %s not defined" % name)
            self.fields[name] = FieldDef(name, el)
        # VESC Tool saves XML in definition order, not serialization order
        self.xml_order = [el.tag for el in params if el.tag in self.fields]

    @classmethod
    def from_compressed(cls, data: bytes):
        return cls(qt_uncompress(data).decode("utf-8"))

    def decode(self, blob: bytes):
        return Config(self, blob)


class Config:
    """A decoded config blob. Keeps the original bytes and each field's byte span."""

    def __init__(self, schema: ConfigSchema, blob: bytes):
        self.schema = schema
        self.blob = bytes(blob)
        r = Reader(self.blob)
        self.signature = r.u32()
        self.values = {}
        self.spans = {}
        for name in schema.order:
            start = r.pos
            self.values[name] = schema.fields[name].read(r)
            self.spans[name] = (start, r.pos)
        if r.remaining() != 0:
            raise ConfigError("%d unexpected trailing bytes in config" % r.remaining())

    def encode(self, values=None) -> bytes:
        """Serialize from values (defaults to the decoded values)."""
        vals = self.values if values is None else values
        w = Writer()
        w.u32(self.signature)
        for name in self.schema.order:
            self.schema.fields[name].write(w, vals[name])
        return w.bytes()

    def roundtrip_ok(self) -> bool:
        """True if re-encoding the decoded values reproduces the original bytes exactly."""
        try:
            return self.encode() == self.blob
        except Exception:
            return False

    def patch_float32_auto(self, changes: dict) -> bytes:
        """Return a copy of the original blob with only the given float32_auto fields replaced.

        Every byte outside those 4-byte slots is copied unchanged.
        """
        out = bytearray(self.blob)
        for name, val in changes.items():
            fd = self.schema.fields.get(name)
            if fd is None or fd.type != T_DOUBLE or fd.vtx != 9:
                raise ConfigError("%s is not a float32_auto field on this firmware" % name)
            a, b = self.spans[name]
            if b - a != 4:
                raise ConfigError("unexpected span for %s" % name)
            out[a:b] = float32_auto_encode(val)
        if len(out) != len(self.blob) or out[:4] != self.blob[:4]:
            raise ConfigError("patch changed length or signature")
        return bytes(out)

    def patch_fields(self, changes: dict) -> bytes:
        """Return a new blob with the given fields changed.

        Fixed-width fields are patched in place (every other byte copied unchanged).
        If a string changes, the blob is re-encoded from values; roundtrip_ok() has
        already proven that encoding reproduces this firmware's bytes exactly.
        """
        fields = self.schema.fields
        for name in changes:
            if name not in fields:
                raise ConfigError("%s is not in this firmware's config" % name)
        if any(fields[n].type == T_STRING for n in changes):
            vals = dict(self.values)
            vals.update(changes)
            out = self.encode(vals)
        else:
            buf = bytearray(self.blob)
            for name, val in changes.items():
                w = Writer()
                fields[name].write(w, val)
                a, b = self.spans[name]
                if len(w.bytes()) != b - a:
                    raise ConfigError("unexpected encoded size for %s" % name)
                buf[a:b] = w.bytes()
            out = bytes(buf)
        if out[:4] != self.blob[:4]:
            raise ConfigError("patch changed the config signature")
        return out

    def to_vesc_tool_xml(self, include_secrets=False) -> str:
        """Same layout VESC Tool uses when saving a custom config to XML."""
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<CustomConfiguration>",
                 "    <ConfigVersion>%d</ConfigVersion>" % VT_CONFIG_VERSION,
                 "    <config_name>%s</config_name>" % escape(self.schema.config_name)]
        for name in self.schema.xml_order:
            val = self.values[name]
            if name in SECRET_FIELDS and not include_secrets:
                val = "" if isinstance(val, str) else 0
            lines.append("    <%s>%s</%s>" % (name, escape(format_value(val)), name))
        lines.append("</CustomConfiguration>")
        return "\n".join(lines) + "\n"

    def public_fields(self, include_secrets=False) -> list:
        out = []
        for name in self.schema.order:
            fd = self.schema.fields[name]
            val = self.values[name]
            secret = name in SECRET_FIELDS
            if secret and not include_secrets:
                val = None
            item = {"name": name, "label": fd.long_name, "type": fd.type, "value": val,
                    "suffix": fd.suffix.strip(), "secret": secret}
            if fd.type == T_DOUBLE and val is not None:
                item["value"] = round(val, max(fd.decimals, 3) + 2)
            if fd.type == T_ENUM and fd.enum_names and val is not None and 0 <= val < len(fd.enum_names):
                item["enum"] = fd.enum_names[val]
            out.append(item)
        return out


def format_value(v) -> str:
    """Number formatting like Qt's QString::number(double) (6 significant digits)."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = "%.6g" % v
        return s
    return str(v)

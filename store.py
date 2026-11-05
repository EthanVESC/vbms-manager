"""Local data: per-board settings, backups, change log, CSV logs.

Everything lives under ~/.vbms_manager (override with VBMS_MANAGER_HOME).
"""

import json
import os
import re
import threading
import time


def home():
    return os.environ.get("VBMS_MANAGER_HOME") or os.path.join(os.path.expanduser("~"), ".vbms_manager")


def path(*parts):
    p = os.path.join(home(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def clock12(t, day=False):
    """10:05 PM (or Mon 10:05 PM) for a datetime or epoch seconds. Portable: no %-I."""
    import datetime as _dt
    if not isinstance(t, _dt.datetime):
        t = _dt.datetime.fromtimestamp(t)
    h = t.hour % 12 or 12
    s = "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")
    return ("%s %s" % (t.strftime("%a"), s)) if day else s


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "board"


def _atomic_write(p, text):
    """Replace p with text so that a crash or power cut leaves either the old file or the new one, never half."""
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    if hasattr(os, "O_DIRECTORY"):   # make the rename itself durable (SD cards on a Raspberry Pi)
        try:
            fd = os.open(os.path.dirname(p) or ".", os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


BAK_EVERY_S = 3600   # boards.json.bak: a known-good copy, refreshed at start and then at most hourly


class BoardStore:
    """boards.json: {mac: {...settings...}}, plus a list of manually added IPs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._file = path("boards.json")
        self._bak_t = 0.0
        self.load_warning = None   # set when boards.json couldn't be read and was set aside (shown in the UI)
        try:
            self._data = self._read(self._file)
        except FileNotFoundError:
            self._data = {}
        except (OSError, ValueError) as e:
            self._data = self._recover(e)
        else:
            self._write_bak()
        self._data.setdefault("boards", {})
        self._data.setdefault("hosts", [])
        self._data.setdefault("settings", {})

    @staticmethod
    def _read(p):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data

    def _recover(self, err):
        """boards.json is unreadable. Never overwrite it: move it aside, then fall back to the last good copy."""
        aside = "%s.corrupt-%s" % (self._file, time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(self._file, aside)
        except OSError:
            aside = None
        try:
            data = self._read(self._file + ".bak")
            how = "restored the last good copy (boards.json.bak)"
        except (OSError, ValueError):
            data = {}
            how = "no good copy was found, so board settings start empty"
        self.load_warning = "boards.json couldn't be read (%s); %s.%s" % (
            err, how, " The unreadable file was kept as %s." % os.path.basename(aside) if aside else "")
        append_log("events", {"event": "store_recovered", "kind": "store", "text": self.load_warning})
        import sys
        print("VBMS Manager: " + self.load_warning, file=sys.stderr)
        return data

    def _text(self):
        return json.dumps(self._data, indent=2, sort_keys=True)

    def _write_bak(self):
        try:
            _atomic_write(self._file + ".bak", self._text())
            self._bak_t = time.time()
        except OSError:
            pass

    def _save(self):
        _atomic_write(self._file, self._text())
        if time.time() - self._bak_t > BAK_EVERY_S:
            self._write_bak()

    def get(self, mac):
        with self._lock:
            return dict(self._data["boards"].get(mac, {}))

    def update(self, mac, **fields):
        with self._lock:
            b = self._data["boards"].setdefault(mac, {})
            for k, v in fields.items():
                if v is None:
                    b.pop(k, None)
                else:
                    b[k] = v
            self._save()
            return dict(b)

    def macs(self):
        with self._lock:
            return list(self._data["boards"])

    def hosts(self):
        with self._lock:
            return list(self._data["hosts"])

    def add_host(self, host):
        with self._lock:
            if host not in self._data["hosts"]:
                self._data["hosts"].append(host)
                self._save()

    def remove_host(self, host):
        with self._lock:
            if host in self._data["hosts"]:
                self._data["hosts"].remove(host)
                self._save()

    def settings(self):
        with self._lock:
            return dict(self._data["settings"])

    def update_settings(self, **fields):
        with self._lock:
            self._data["settings"].update(fields)
            self._save()
            return dict(self._data["settings"])


_log_lock = threading.Lock()
LOG_MAX_BYTES = 5 * 1024 * 1024   # a log rolls over to <name>.log.1 at this size
LOG_KEEP = 5                      # rolled files kept (.1 newest ... .5 oldest); charges.log feeds lifetime stats


def _rotate(p):
    for i in range(LOG_KEEP - 1, 0, -1):
        if os.path.exists("%s.%d" % (p, i)):
            os.replace("%s.%d" % (p, i), "%s.%d" % (p, i + 1))
    os.replace(p, p + ".1")


def append_log(name, record: dict):
    """Append one JSON line to ~/.vbms_manager/<name>.log (rolled over at LOG_MAX_BYTES)."""
    record = dict(record)
    record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    p = path(name + ".log")
    with _log_lock:
        try:
            if os.path.getsize(p) >= LOG_MAX_BYTES:
                _rotate(p)
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")


def _tail(p, n, block=65536):
    """The last n lines of a file, reading backwards (a long log isn't read whole for the latest 200)."""
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.splitlines()
    if pos > 0:
        lines = lines[1:]   # the first one is probably cut
    return lines[-n:] if n else []


def read_log(name, limit=200):
    """The latest `limit` records, oldest first, across the current log and its rolled-over files."""
    p = path(name + ".log")
    lines = []
    for f in [p] + ["%s.%d" % (p, i) for i in range(1, LOG_KEEP + 1)]:
        if len(lines) >= limit:
            break
        try:
            lines = _tail(f, limit - len(lines)) + lines
        except OSError:
            continue
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln.decode("utf-8")))
        except ValueError:
            pass
    return out

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
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


class BoardStore:
    """boards.json: {mac: {...settings...}}, plus a list of manually added IPs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._file = path("boards.json")
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {}
        self._data.setdefault("boards", {})
        self._data.setdefault("hosts", [])
        self._data.setdefault("settings", {})

    def _save(self):
        _atomic_write(self._file, json.dumps(self._data, indent=2, sort_keys=True))

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


def append_log(name, record: dict):
    """Append one JSON line to ~/.vbms_manager/<name>.log"""
    record = dict(record)
    record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    with _log_lock:
        with open(path(name + ".log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")


def read_log(name, limit=200):
    try:
        with open(path(name + ".log"), "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out

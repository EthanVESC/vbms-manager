"""Long-term recording: per-board history files, recording settings, queries and CSV export.

Every 1 Hz poll becomes a sample (short keys, see sample()). The Recorder averages samples
over the recording interval and appends one JSON line per interval to

    ~/.vbms_manager/history/<mac>/YYYY-MM-DD.jsonl

A header line ({"hdr": 1, ...}) starts each file and repeats whenever the level, interval,
cell count or temperature sensor names change. Closed days are gzipped. Offline time is
simply missing: nothing is interpolated.

Sign convention: current "i" and power "p" are positive INTO the pack (charging), the
opposite of the BMS's own i_in_ic.
"""

import collections
import csv
import datetime
import gzip
import io
import json
import os
import re
import threading
import time

import store

LEVELS = ("summary", "detailed", "everything")
INTERVALS = (1, 5, 10, 30, 60, 300)
RETENTIONS = (7, 30, 90, 365, 0)  # days; 0 = keep forever
DEFAULTS = {"enabled": True, "interval_s": 10, "level": "detailed", "retention_days": 90}

SUMMARY = ("v", "i", "p", "soc", "cmin", "cmax", "d", "tmax", "vc", "chg", "bal")
DETAILED = SUMMARY + ("c", "tp", "bm", "ah", "wh", "hum", "soh", "ahc", "whc", "ahd", "whd")
EVERYTHING = DETAILED + ("i_in", "st", "tadc", "tic", "thum", "pr", "ce", "cs")
FIELDS = {"summary": SUMMARY, "detailed": DETAILED, "everything": EVERYTHING}

# How each field combines over an interval.
AVG = {"v", "i", "p", "soc", "vc", "hum", "soh", "i_in", "tic", "thum", "pr", "c", "tp", "tadc"}
MIN = {"cmin"}
MAX = {"cmax", "d", "tmax"}
ANY = {"chg", "bal"}
OR = {"bm"}
# everything else: last value

_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$")


# ---------------------------------------------------------------- settings

def clean_settings(raw, base=None):
    s = dict(base or DEFAULTS)
    if not isinstance(raw, dict):
        raise ValueError("bad logging settings")
    if "enabled" in raw:
        s["enabled"] = bool(raw["enabled"])
    if "interval_s" in raw:
        try:
            iv = int(raw["interval_s"])
        except (TypeError, ValueError):
            raise ValueError("interval must be one of %s seconds" % (INTERVALS,))
        if iv not in INTERVALS:
            raise ValueError("interval must be one of %s seconds" % (INTERVALS,))
        s["interval_s"] = iv
    if "level" in raw:
        if raw["level"] not in LEVELS:
            raise ValueError("level must be one of %s" % ", ".join(LEVELS))
        s["level"] = raw["level"]
    if "retention_days" in raw:
        try:
            rd = int(raw["retention_days"])
        except (TypeError, ValueError):
            raise ValueError("retention must be one of %s days" % (RETENTIONS,))
        if rd not in RETENTIONS:
            raise ValueError("retention must be one of %s days" % (RETENTIONS,))
        s["retention_days"] = rd
    return s


def settings_from(all_settings):
    try:
        return clean_settings(all_settings.get("logging") or {})
    except ValueError:
        return dict(DEFAULTS)


# ---------------------------------------------------------------- samples

def sample(v, t, cfg=None):
    """One poll as a flat dict with short keys (the 'everything' set)."""
    temps = v.get("temps") or []
    i_in = -v["i_in_ic"]
    s = {"t": round(t, 1), "v": round(v["v_tot"], 3), "i": round(i_in, 3), "p": round(v["v_tot"] * i_in, 1),
         "soc": round(v["soc"], 4), "cmin": v["cell_min"], "cmax": v["cell_max"], "d": v["cell_delta_mv"],
         "tmax": round(max(x["value"] for x in temps), 1) if temps else None,
         "vc": round(v["v_charge"], 2), "chg": bool(v["is_charging"]), "bal": bool(v["is_balancing"]),
         "c": [round(x, 4) for x in v["cells"]], "tp": [round(x["value"], 2) for x in temps],
         "tpn": [x["name"] for x in temps],
         "bm": sum(1 << k for k, b in enumerate(v["balancing"]) if b),
         "ah": round(v["ah_cnt"], 4), "wh": round(v["wh_cnt"], 2), "hum": round(v["humidity"], 1),
         "soh": round(v["soh"], 4), "ahc": round(v["ah_chg_total"], 4), "whc": round(v["wh_chg_total"], 2),
         "ahd": round(v["ah_dis_total"], 4), "whd": round(v["wh_dis_total"], 2),
         "i_in": round(v["i_in"], 3), "st": v.get("status", ""), "tadc": [round(x, 2) for x in v["temps_adc"]],
         "tic": round(v["temp_ic"], 2), "thum": round(v["temp_hum"], 2), "pr": v.get("pressure")}
    if cfg:
        s["ce"] = round(cfg.get("vc_charge_end", 0), 4)
        s["cs"] = round(cfg.get("vc_charge_start", 0), 4)
    return s


def combine(samples, fields):
    """Average a bucket of samples into one row with the given fields."""
    out = {"t": round(sum(s["t"] for s in samples) / len(samples), 1), "n": len(samples)}
    last = samples[-1]
    for k in fields:
        vals = [s[k] for s in samples if s.get(k) is not None]
        if not vals:
            continue
        if k in AVG:
            if isinstance(vals[0], list):
                same = [x for x in vals if len(x) == len(vals[-1])]
                out[k] = [round(sum(col) / len(col), 4) for col in zip(*same)]
            else:
                out[k] = round(sum(vals) / len(vals), 4)
        elif k in MIN:
            out[k] = min(vals)
        elif k in MAX:
            out[k] = max(vals)
        elif k in ANY:
            out[k] = 1 if any(vals) else 0
        elif k in OR:
            m = 0
            for x in vals:
                m |= x
            out[k] = m
        else:
            out[k] = last.get(k)
    return out


# ---------------------------------------------------------------- files

def board_dir(mac):
    return os.path.join(store.home(), "history", mac.replace(":", ""))


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def day_files(mac):
    """{day: path} for every day file of a board, preferring the plain file while it exists."""
    d = board_dir(mac)
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in sorted(names):
        m = _DAY_RE.match(n)
        if m and (m.group(1) not in out or not m.group(2)):
            out[m.group(1)] = os.path.join(d, n)
    return out


def _open_day(path):
    """Open a day file for reading; it may have been gzipped since it was listed."""
    try:
        if path.endswith(".gz"):
            return gzip.open(path, "rt", encoding="utf-8")
        return open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        if not path.endswith(".gz") and os.path.exists(path + ".gz"):
            return gzip.open(path + ".gz", "rt", encoding="utf-8")
        return None


def iter_rows(path):
    """Yield (header, row) for every row of one day file."""
    f = _open_day(path)
    if f is None:
        return
    hdr = {}
    with f:
        for ln in f:
            try:
                r = json.loads(ln)
            except ValueError:
                continue  # a line being written right now
            if "hdr" in r:
                hdr = r
                continue
            yield hdr, r


def disk_usage(mac=None):
    root = board_dir(mac) if mac else os.path.join(store.home(), "history")
    total = 0
    for dp, _, fs in os.walk(root):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def compress_old(mac, today=None):
    """Gzip every closed day (not today)."""
    today = today or _day(time.time())
    for day, p in day_files(mac).items():
        if day >= today or p.endswith(".gz"):
            continue
        tmp = p + ".gz.tmp"
        try:
            with open(p, "rb") as src, gzip.open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    dst.write(chunk)
            os.replace(tmp, p + ".gz")
            os.remove(p)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass


def prune(mac, retention_days, now=None):
    """Delete day files older than the retention setting. 0 keeps everything."""
    if not retention_days:
        return 0
    cutoff = _day((now or time.time()) - retention_days * 86400)
    n = 0
    for day, p in day_files(mac).items():
        if day < cutoff:
            for q in (p, p[:-3] if p.endswith(".gz") else p + ".gz"):
                try:
                    os.remove(q)
                    n += 1
                except OSError:
                    pass
    return n


class Recorder:
    """Owned by a Board; fed one sample per poll from the board's thread."""

    def __init__(self, mac, get_settings):
        self.mac = mac
        self.get_settings = get_settings
        self._bucket = []
        self._key = None
        self._hdr = None
        self._hdr_day = None
        self._last_maint = 0.0
        self.rows_written = 0

    def add(self, s):
        st = self.get_settings()
        if not st["enabled"]:
            self.flush()
            return
        iv = st["interval_s"]
        key = (int(s["t"] // iv), iv, st["level"])
        if self._key is not None and key != self._key:
            self.flush()
        self._key = key
        self._bucket.append(s)
        if s["t"] - self._last_maint > 3600:
            self._last_maint = s["t"]
            self.maintain(st)

    def flush(self):
        if not self._bucket:
            return
        bucket, (_, iv, level) = self._bucket, self._key
        self._bucket, self._key = [], None
        row = combine(bucket, FIELDS[level])
        last = bucket[-1]
        hdr = {"hdr": 1, "level": level, "interval_s": iv, "cells": len(last.get("c") or []),
               "temps": last.get("tpn") or [], "sign": "+ = into pack"}
        day = _day(row["t"])
        p = os.path.join(board_dir(self.mac), day + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        lines = []
        if hdr != self._hdr or day != self._hdr_day or not os.path.exists(p):
            lines.append(json.dumps(hdr))
            self._hdr, self._hdr_day = hdr, day
        lines.append(json.dumps(row, separators=(",", ":")))
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.rows_written += 1

    def maintain(self, st=None):
        st = st or self.get_settings()
        try:
            compress_old(self.mac)
            prune(self.mac, st["retention_days"])
        except OSError:
            pass


# ---------------------------------------------------------------- queries

METRICS = {
    "power": ("W", 0), "current": ("A", 2), "voltage": ("V", 2), "soc": ("%", 1), "cells": ("V", 3),
    "spread": ("mV", 1), "temps": ("°C", 1), "charger": ("V", 1),
}


def extract(metric, hdr, r, cell=None):
    """{series name: value} for one row."""
    if metric == "power":
        return {"power": r.get("p")}
    if metric == "current":
        return {"current": r.get("i")}
    if metric == "voltage":
        return {"pack": r.get("v")}
    if metric == "soc":
        return {"SOC": r["soc"] * 100 if r.get("soc") is not None else None}
    if metric == "charger":
        return {"charger": r.get("vc")}
    if metric == "spread":
        return {"spread": r.get("d")}
    if metric == "cells":
        c = r.get("c")
        if cell == "all":
            return {"cell %d" % (k + 1): x for k, x in enumerate(c)} if c else {}
        if cell:
            k = int(cell) - 1
            return {"cell %d" % (k + 1): c[k]} if c and 0 <= k < len(c) else {}
        return {"highest cell": r.get("cmax"), "lowest cell": r.get("cmin")}
    if metric == "temps":
        names = hdr.get("temps") or []
        tp = r.get("tp")
        if tp and len(tp) == len(names):
            out = {}
            cells = [x for n, x in zip(names, tp) if n.startswith("Cell sensor") or n.startswith("Sensor")]
            if cells and cell != "all":
                out["cells (avg)"] = sum(cells) / len(cells)
            for n, x in zip(names, tp):
                if n in ("Cell min", "Cell max"):
                    continue
                if cell != "all" and (n.startswith("Cell sensor") or n.startswith("Sensor")):
                    continue
                out[n] = x
            return out
        return {"hottest": r.get("tmax")}
    raise ValueError("unknown metric %s" % metric)


class _Acc:
    __slots__ = ("s", "n", "lo", "hi", "t")

    def __init__(self):
        self.s = 0.0
        self.n = 0
        self.lo = float("inf")
        self.hi = float("-inf")
        self.t = 0.0

    def add(self, t, v, lo=None, hi=None, n=1):
        self.s += v * n
        self.n += n
        self.t += t * n
        self.lo = min(self.lo, v if lo is None else lo)
        self.hi = max(self.hi, v if hi is None else hi)


# Closed days, aggregated to one minute per (path, metric, cell): cheap long-range queries.
_cache = collections.OrderedDict()
_cache_lock = threading.Lock()
CACHE_MAX = 160


def _minute_series(path, metric, cell):
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_mtime, st.st_size, metric, cell)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    acc = collections.defaultdict(dict)
    for hdr, r in iter_rows(path):
        m = int(r["t"] // 60)
        for name, v in extract(metric, hdr, r, cell).items():
            if v is None:
                continue
            a = acc[name].get(m)
            if a is None:
                a = acc[name][m] = _Acc()
            a.add(r["t"], v)
    out = {name: [(a.t / a.n, a.s / a.n, a.lo, a.hi, a.n) for _, a in sorted(d.items())] for name, d in acc.items()}
    with _cache_lock:
        _cache[key] = out
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
    return out


def query(mac, t0, t1, metric, cell=None, max_points=600, live=(), now=None):
    """Downsampled history: {"series": [{"name", "pts": [[t, avg, min, max], ...]}], "w", "gap", ...}.

    live: the board's in-memory 1 s samples (newest last). They replace file rows for the
    time they cover, so short ranges show full resolution.
    """
    if metric not in METRICS:
        raise ValueError("unknown metric")
    now = now or time.time()
    t1 = min(t1, now)
    if t0 <= 0:  # "all": from the first recorded day
        days = sorted(day_files(mac))
        t0 = time.mktime(datetime.datetime.strptime(days[0], "%Y-%m-%d").timetuple()) if days else t1 - 86400
        if live:
            t0 = min(t0, live[0]["t"])
    if t1 <= t0:
        raise ValueError("empty range")
    max_points = max(20, min(2000, int(max_points)))
    w = max(1.0, (t1 - t0) / max_points)
    live = [s for s in live if t0 <= s["t"] <= t1]
    live_from = live[0]["t"] if live else float("inf")
    buckets = collections.defaultdict(dict)
    intervals = set()

    def put(name, t, v, lo=None, hi=None, n=1):
        b = int((t - t0) // w)
        a = buckets[name].get(b)
        if a is None:
            a = buckets[name][b] = _Acc()
        a.add(t, v, lo, hi, n)

    today = _day(now)
    for day, path in sorted(day_files(mac).items()):
        ds = time.mktime(datetime.datetime.strptime(day, "%Y-%m-%d").timetuple())
        if ds > t1 or ds + 90000 < t0 or ds >= live_from + 90000:
            continue
        if w >= 60 and day != today:
            for name, pts in _minute_series(path, metric, cell).items():
                for t, av, lo, hi, n in pts:
                    if t0 <= t <= t1 and t < live_from:
                        put(name, t, av, lo, hi, n)
            intervals.add(60)
            continue
        for hdr, r in iter_rows(path):
            t = r["t"]
            if t < t0 or t > t1 or t >= live_from:
                continue
            intervals.add(hdr.get("interval_s", 10))
            for name, v in extract(metric, hdr, r, cell).items():
                if v is not None:
                    put(name, t, v)
    if live:
        intervals.add(1)
        hdr = {"temps": live[-1].get("tpn") or []}
        for s in live:
            for name, v in extract(metric, hdr, s, cell).items():
                if v is not None:
                    put(name, s["t"], v)

    def order(name):
        m = re.match(r"cell (\d+)$", name)
        return (1, int(m.group(1))) if m else (0, name)

    series = []
    for name in sorted(buckets, key=order):
        pts = [[round(a.t / a.n, 1), round(a.s / a.n, 4), round(a.lo, 4), round(a.hi, 4)]
               for _, a in sorted(buckets[name].items())]
        series.append({"name": name, "pts": pts})
    iv = max(intervals) if intervals else 10
    unit, dec = METRICS[metric]
    return {"metric": metric, "unit": unit, "decimals": dec, "t0": t0, "t1": t1, "w": round(w, 2),
            "gap": round(max(3 * iv, 2.5 * w), 1), "series": series}


# ---------------------------------------------------------------- CSV export

def csv_rows(mac, t0, t1):
    """Yield CSV text chunks for every recorded row in [t0, t1]."""
    files = [p for day, p in sorted(day_files(mac).items())
             if time.mktime(datetime.datetime.strptime(day, "%Y-%m-%d").timetuple()) <= t1
             and time.mktime(datetime.datetime.strptime(day, "%Y-%m-%d").timetuple()) + 90000 >= t0]
    # First pass: column set (cell count, temp names) from the headers.
    ncells, tnames = 0, []
    for p in files:
        f = _open_day(p)
        if f is None:
            continue
        with f:
            for ln in f:
                if ln.startswith('{"hdr"'):
                    try:
                        h = json.loads(ln)
                    except ValueError:
                        continue
                    ncells = max(ncells, h.get("cells", 0))
                    tnames += [n for n in h.get("temps", []) if n not in tnames]
    scalar = [("v", "pack_V"), ("i", "current_in_A"), ("p", "power_in_W"), ("soc", "soc"), ("cmin", "cell_min_V"),
              ("cmax", "cell_max_V"), ("d", "spread_mV"), ("tmax", "temp_max_C"), ("vc", "charger_V"),
              ("chg", "charging"), ("bal", "balancing"), ("ah", "ah_cnt"), ("wh", "wh_cnt"), ("ahc", "ah_chg_total"),
              ("whc", "wh_chg_total"), ("ahd", "ah_dis_total"), ("whd", "wh_dis_total"), ("hum", "humidity"),
              ("soh", "soh"), ("i_in", "i_in_A"), ("tic", "temp_ic_C"), ("thum", "temp_board_C"),
              ("ce", "charge_end_V"), ("cs", "charge_start_V"), ("st", "status")]
    head = ["time", "interval_s", "samples"] + [n for _, n in scalar] + ["cell%d_V" % (k + 1) for k in range(ncells)] + \
        ["bal%d" % (k + 1) for k in range(ncells)] + ["temp_%s_C" % re.sub(r"\W+", "_", n).strip("_") for n in tnames]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    for p in files:
        for hdr, r in iter_rows(p):
            if not (t0 <= r["t"] <= t1):
                continue
            c = r.get("c") or []
            bm = r.get("bm")
            temps = dict(zip(hdr.get("temps") or [], r.get("tp") or []))
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t"])), hdr.get("interval_s", ""), r.get("n", "")] +
                       ["" if r.get(k) is None else r[k] for k, _ in scalar] +
                       [c[k] if k < len(c) else "" for k in range(ncells)] +
                       [("1" if bm >> k & 1 else "0") if bm is not None else "" for k in range(ncells)] +
                       [temps.get(n, "") for n in tnames])
            if buf.tell() > 1 << 16:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
    yield buf.getvalue()

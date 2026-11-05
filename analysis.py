"""Analysis over the recorded history: daily summary, weak-cell drift, idle self-discharge,
capacity per charge and a health card.

Each day file is read once into a small digest (cached in history/<mac>/digest.json for
closed days); everything else is built from the digests and the charge-session log.

Caveats the UI repeats: SOC comes from a curve measured on new cells of the chosen type (or a
typical curve, or the user's own), so it's approximate; while the BMS is awake it powers itself from the pack, so pack-level idle drop includes
that draw. Per-cell differences are the useful signal.
"""

import json
import os
import statistics
import threading
import time

import datalog
import store

REST_A = 0.3
CHARGER_V = 5.0
SD_MIN_S = 6 * 3600        # an idle stretch must be this long to measure self-discharge
SD_SETTLE_S = 30 * 60      # and the first reading taken this long after the rest began
DRIFT_DAYS = 14
DRIFT_MV = 5.0             # flag a cell whose rest offset falls this much over DRIFT_DAYS
LOWEST_FRAC = 0.7          # flag a cell that's the lowest in this share of rest samples
LOWEST_MIN_MV = 5.0        # only count "lowest" when the pack spread is at least this
DIGEST_VERSION = 2

_lock = threading.Lock()


def _same_counters(a, b, tol=0.01):
    """Nothing went in or out of the pack between two readings (BMS lifetime counters)."""
    for k in ("ahc", "ahd"):
        if a.get(k) is None or b.get(k) is None or not (0 <= b[k] - a[k] < tol):
            return False
    return True


# ---------------------------------------------------------------- per-day digest

def digest_day(path, day_start, day_end):
    st = {"wh_in": 0.0, "wh_out": 0.0, "ah_in": 0.0, "ah_out": 0.0, "chg_s": 0, "cover_s": 0,
          "soc_min": None, "soc_max": None, "tmax": None, "rest_d": [], "first": None, "last": None}
    offs, lowest, lowest_n, ncells = None, None, 0, 0
    segs, seg = [], None
    prev = None
    for hdr, r in datalog.iter_rows(path):
        n = r.get("n") or hdr.get("interval_s", 10)
        sec = n  # one poll per second, so samples = seconds covered
        st["cover_s"] += sec
        p, i = r.get("p"), r.get("i")
        if p is not None:
            st["wh_in" if p > 0 else "wh_out"] += abs(p) * sec / 3600
        if i is not None:
            st["ah_in" if i > 0 else "ah_out"] += abs(i) * sec / 3600
        if r.get("chg"):
            st["chg_s"] += sec
        if r.get("soc") is not None:
            sc = r["soc"] * 100
            st["soc_min"] = sc if st["soc_min"] is None else min(st["soc_min"], sc)
            st["soc_max"] = sc if st["soc_max"] is None else max(st["soc_max"], sc)
        if r.get("tmax") is not None:
            st["tmax"] = r["tmax"] if st["tmax"] is None else max(st["tmax"], r["tmax"])
        cnt = {k: r[k] for k in ("ahc", "ahd", "whc", "whd") if r.get(k) is not None}
        if cnt:
            st["first"] = st["first"] or dict(cnt, t=r["t"])
            st["last"] = dict(cnt, t=r["t"])
        rest = not r.get("chg") and i is not None and abs(i) < REST_A
        c = r.get("c")
        if rest and r.get("d") is not None:
            st["rest_d"].append(r["d"])
        # Drift: each cell's offset from the pack median at rest, not while bleeding.
        if rest and c and not r.get("bm"):
            if offs is None or len(offs) != len(c):
                ncells = len(c)
                offs, lowest, lowest_n = [[] for _ in c], [0] * len(c), 0
            med = statistics.median(c)
            for k, x in enumerate(c):
                offs[k].append((x - med) * 1000)
            if (r.get("d") or 0) >= LOWEST_MIN_MV:
                lowest[c.index(min(c))] += 1
                lowest_n += 1
        # Self-discharge segments: unbroken stretches with no charger, no current, no balancing.
        idle = rest and c and (r.get("vc") or 0) < CHARGER_V and not r.get("bm") and not r.get("bal")
        if idle and seg is not None and prev is not None and r["t"] - prev["t"] > 3 * hdr.get("interval_s", 10) \
                and not _same_counters(prev, r):
            segs.append(seg)
            seg = None
        if idle:
            row = {"t": r["t"], "c": c, "ahc": r.get("ahc"), "ahd": r.get("ahd")}
            if seg is None:
                seg = {"s": r["t"], "f": row, "a": None, "b": row}
            if seg["a"] is None and r["t"] - seg["s"] >= SD_SETTLE_S:
                seg["a"] = row
            seg["b"] = row
        elif seg is not None:
            segs.append(seg)
            seg = None
        prev = r
    if seg is not None:
        segs.append(seg)
    rd = st.pop("rest_d")
    st["rest_d_med"] = round(statistics.median(rd), 1) if rd else None
    st["rest_d_max"] = round(max(rd), 1) if rd else None
    span = max(1, min(day_end, time.time()) - day_start)
    st["coverage"] = round(min(1.0, st["cover_s"] / span), 3)
    for k in ("wh_in", "wh_out", "ah_in", "ah_out"):
        st[k] = round(st[k], 3)
    out = {"v": DIGEST_VERSION, "stats": st, "ncells": ncells, "segs": segs,
           "rest_off": [round(statistics.median(o), 2) if o else None for o in offs] if offs else None,
           "lowest": lowest, "lowest_n": lowest_n}
    return out


def _day_bounds(day):
    t0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
    return t0, t0 + 86400


def digests(mac, days=90, now=None):
    """{day: digest} for the last `days` days that have data."""
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    cutoff = time.strftime("%Y-%m-%d", time.localtime(now - days * 86400))
    cpath = os.path.join(datalog.board_dir(mac), "digest.json")
    with _lock:
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        out, dirty = {}, False
        for day, path in sorted(datalog.day_files(mac).items()):
            if day < cutoff:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = "%s|%d|%d" % (os.path.basename(path), st.st_size, int(st.st_mtime))
            hit = cache.get(day)
            if day != today and hit and hit.get("key") == key and hit["d"].get("v") == DIGEST_VERSION:
                out[day] = hit["d"]
                continue
            d = digest_day(path, *_day_bounds(day))
            out[day] = d
            if day != today:
                cache[day] = {"key": key, "d": d}
                dirty = True
        if dirty:
            for day in [d for d in cache if d < cutoff]:
                cache.pop(day)
            os.makedirs(os.path.dirname(cpath), exist_ok=True)
            tmp = cpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, separators=(",", ":"))
            os.replace(tmp, cpath)
    return out


# ---------------------------------------------------------------- charge log

def charges(mac, limit=200):
    """Closed sessions for one board, newest first, with later rest/capacity updates merged."""
    rows = store.read_log("charges", 20000)
    by_id, order = {}, []
    for r in rows:
        if r.get("mac") != mac:
            continue
        if r.get("event") == "rest_after":
            if r.get("id") in by_id:
                by_id[r["id"]].update(rest_after=r.get("rest_after"), capacity_ah=r.get("capacity_ah"))
            continue
        if r.get("id") not in by_id:
            order.append(r["id"])
        by_id[r["id"]] = r
    return [by_id[i] for i in reversed(order)][:limit]


# ---------------------------------------------------------------- analyses

def daily(ds, chg):
    per_day = {}
    for c in chg:
        per_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(c["start"])), []).append(c)
    out = []
    for day in sorted(ds):
        st = dict(ds[day]["stats"])
        f, l = st.pop("first"), st.pop("last")
        if f and l:
            st["wh_in_bms"] = round(l["whc"] - f["whc"], 2) if "whc" in f and "whc" in l else None
            st["wh_out_bms"] = round(l["whd"] - f["whd"], 2) if "whd" in f and "whd" in l else None
        st["day"] = day
        st["charges"] = len(per_day.get(day, []))
        st["low_coverage"] = st["coverage"] < 0.5
        out.append(st)
    return out


def drift(ds, chg):
    days = [d for d in sorted(ds) if ds[d].get("rest_off")]
    if not days:
        return {"available": False, "why": "Needs the 'detailed' recording level (every cell) and some time at rest."}
    n = ds[days[-1]]["ncells"]
    days = [d for d in days if ds[d]["ncells"] == n]
    offsets = [ds[d]["rest_off"] for d in days]
    flags = []
    recent = days[-DRIFT_DAYS:]
    if len(recent) >= 5:
        xs = list(range(len(recent)))
        for k in range(n):
            ys = [ds[d]["rest_off"][k] for d in recent]
            pairs = [(x, y) for x, y in zip(xs, ys) if y is not None]
            if len(pairs) < 5:
                continue
            # Theil-Sen: the median of pairwise slopes, so one odd day (partial, or spent balancing) can't swing it.
            slope = statistics.median((y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(pairs) for x2, y2 in pairs[i + 1:])
            change = slope * (DRIFT_DAYS - 1)
            if change <= -DRIFT_MV:
                flags.append({"cell": k + 1, "kind": "sagging",
                              "text": "Cell %d's resting voltage is falling against the rest of the pack "
                                      "(about %.1f mV over %d days)." % (k + 1, -change, DRIFT_DAYS)})
    low_n = sum(ds[d]["lowest_n"] for d in recent)
    if low_n >= 50:
        low = [sum(ds[d]["lowest"][k] for d in recent if len(ds[d]["lowest"]) == n) for k in range(n)]
        for k, cnt in enumerate(low):
            if cnt / low_n >= LOWEST_FRAC:
                flags.append({"cell": k + 1, "kind": "lowest",
                              "text": "Cell %d is the lowest cell in %d%% of resting readings (spread at least %g mV)."
                                      % (k + 1, round(100 * cnt / low_n), LOWEST_MIN_MV)})
    ends = [c for c in chg[:20] if len(c.get("cells1") or []) == n and (c.get("d1") or 0) >= LOWEST_MIN_MV]
    if len(ends) >= 5:
        hi = [0] * n
        for c in ends:
            hi[c["cells1"].index(max(c["cells1"]))] += 1
        for k, cnt in enumerate(hi):
            if cnt / len(ends) >= LOWEST_FRAC:
                flags.append({"cell": k + 1, "kind": "highest_at_end",
                              "text": "Cell %d ends %d of the last %d charges as the highest cell, so it's the one "
                                      "that stops charging." % (k + 1, cnt, len(ends))})
    mean_off = [statistics.mean([o[k] for o in offsets[-DRIFT_DAYS:] if o[k] is not None] or [0]) for k in range(n)]
    weakest = min(range(n), key=lambda k: mean_off[k]) + 1 if n else None
    return {"available": True, "days": days, "offsets": offsets, "ncells": n, "flags": flags,
            "weakest": weakest, "weakest_mv": round(mean_off[weakest - 1], 1) if weakest else None}


def self_discharge(ds):
    """Stitch rest segments across day files (and offline gaps where the counters didn't move)."""
    segs = []
    for day in sorted(ds):
        segs += ds[day]["segs"]
    merged = []
    for s in segs:
        m = merged[-1] if merged else None
        if m and len(s["f"]["c"]) == len(m["b"]["c"]) and s["f"]["t"] >= m["b"]["t"] and _same_counters(m["b"], s["f"]):
            if m["a"] is None:
                for cand in (s["f"], s["a"]):
                    if cand and cand["t"] - m["s"] >= SD_SETTLE_S:
                        m["a"] = cand
                        break
            m["b"] = s["b"]
        else:
            merged.append(dict(s))
    events = []
    for m in merged:
        a, b = m["a"], m["b"]
        if not a or b["t"] - a["t"] < SD_MIN_S or len(a["c"]) != len(b["c"]):
            continue
        days = (b["t"] - a["t"]) / 86400
        rates = [round((x - y) * 1000 / days, 2) for x, y in zip(a["c"], b["c"])]
        events.append({"from": a["t"], "to": b["t"], "hours": round((b["t"] - a["t"]) / 3600, 1),
                       "mv_per_day": rates, "pack_mv_per_day": round(statistics.mean(rates), 2)})
    if not events:
        return {"available": False, "events": [],
                "why": "Needs the 'detailed' level and an idle stretch of at least 6 hours (no charger, no current, "
                       "no balancing) that VBMS Manager saw the start and end of."}
    n = len(events[-1]["mv_per_day"])
    use = [e for e in events if len(e["mv_per_day"]) == n][-10:]
    w = [e["hours"] for e in use]
    rel = []
    for k in range(n):
        rel.append(round(sum((e["mv_per_day"][k] - e["pack_mv_per_day"]) * h for e, h in zip(use, w)) / sum(w), 2))
    mad = statistics.median([abs(x) for x in rel]) or 0.05
    flags = [{"cell": k + 1, "text": "Cell %d drops %.2f mV/day faster than the pack average while idle." % (k + 1, x)}
             for k, x in enumerate(rel) if x > max(0.5, 4 * mad)]
    return {"available": True, "events": events[-20:], "relative": rel, "flags": flags}


def capacity(chg, nominal_ah=None):
    pts = [{"t": c["start"], "ah": c["capacity_ah"], "id": c["id"]} for c in reversed(chg) if c.get("capacity_ah")]
    recent = [p["ah"] for p in pts[-5:]]
    est = round(statistics.median(recent), 2) if recent else None
    return {"points": pts, "estimate_ah": est, "nominal_ah": nominal_ah,
            "pct_of_nominal": round(100 * est / nominal_ah, 1) if est and nominal_ah else None}


def health(values, cap, dr, sd, daily_rows, chg):
    pack_ah = cap["nominal_ah"] or cap["estimate_ah"]
    v = values or {}
    spreads = [(d["day"], d["rest_d_med"]) for d in daily_rows if d.get("rest_d_med") is not None][-30:]
    trend = None
    if len(spreads) >= 7:
        a = statistics.mean(x for _, x in spreads[:len(spreads) // 2])
        b = statistics.mean(x for _, x in spreads[len(spreads) // 2:])
        trend = round(b - a, 1)
    return {
        "soh": v.get("soh"), "ah_in_total": v.get("ah_chg_total"), "ah_out_total": v.get("ah_dis_total"),
        "wh_in_total": v.get("wh_chg_total"), "wh_out_total": v.get("wh_dis_total"),
        "pack_ah": pack_ah, "pack_ah_source": "you" if cap["nominal_ah"] else "estimate" if cap["estimate_ah"] else None,
        "cycles": round(v["ah_chg_total"] / pack_ah, 1) if pack_ah and v.get("ah_chg_total") is not None else None,
        "capacity_ah": cap["estimate_ah"], "capacity_pct": cap["pct_of_nominal"],
        "rest_spread": spreads, "rest_spread_change": trend,
        "weakest": dr.get("weakest") if dr.get("available") else None,
        "weakest_mv": dr.get("weakest_mv") if dr.get("available") else None,
        "flags": (dr.get("flags") or []) + (sd.get("flags") or []),
        "charges_logged": len(chg), "charge_hours": round(sum(c["duration_s"] for c in chg) / 3600, 1),
    }


def report(mac, values=None, nominal_ah=None, days=90, now=None):
    ds = digests(mac, days, now)
    chg = charges(mac)
    rows = daily(ds, chg)
    dr = drift(ds, chg)
    sd = self_discharge(ds)
    cap = capacity(chg, nominal_ah)
    return {"daily": rows, "drift": dr, "self_discharge": sd, "capacity": cap,
            "health": health(values, cap, dr, sd, rows, chg)}

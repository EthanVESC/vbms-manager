"""Charge sessions: one record per charge, built from the 1 Hz samples (see datalog.sample).

A session opens when the BMS reports CHARGING and closes when it stops (or the board goes
offline). Ah/Wh are integrated from the samples' real timestamps; gaps longer than GAP_S
aren't integrated, they're counted. Current and power are positive into the pack.

Capacity estimate: if the pack was resting before the charge and rests again after it
(both inside the SOC curve's range), capacity ~= Ah added / (SOC after - SOC before). The
SOC comes from the board's SOC curve, measured on new cells of that type (or typical, or the
user's own), so the number is labelled as an estimate everywhere.
"""

import time

GAP_S = 5.0
REST_A = 0.3              # |current| below this counts as resting
REST_BEFORE_S = 20 * 60   # rest needed before a charge for a usable "before" voltage
REST_AFTER_S = 30 * 60    # rest needed after a charge for the "after" voltage
MIN_DSOC = 15.0           # % of SOC between the two rests for a capacity estimate
LIMIT_TOL = 0.015         # cmax within 15 mV of Charge End counts as "reached the limit"
CHARGER_V = 5.0

REASONS = {
    "limit": "reached the charge limit",
    "unplugged": "charger unplugged",
    "paused": "paused (by you or the schedule)",
    "fault": "charge fault",
    "temperature": "temperature outside the charging range",
    "offline": "board went offline",
    "unknown": "stopped by the BMS",
}


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def soc_from_curve(v, pts):
    """SOC % for a resting cell voltage, only inside the curve. None outside it."""
    if v is None or not pts:
        return None
    pts = sorted((float(p), float(x)) for p, x in pts)
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if v0 <= v <= v1:
            return p0 + (p1 - p0) * (v - v0) / (v1 - v0)
    return None


class ChargeTracker:
    def __init__(self, on_close, on_update=None):
        """on_close(record) when a session ends; on_update(record) when a later rest completes it."""
        self.on_close = on_close
        self.on_update = on_update or (lambda r: None)
        self.cur = None
        self.pending = None      # closed session waiting for its "rest after" reading
        self.last = None         # last closed session (summary for the UI)
        self._prev = None        # previous sample
        self._rest_since = None  # time the pack started resting (None = not resting)
        self._rest_cell = None   # mean cell voltage of the last rested sample
        self._seen_first = False

    # -- feeding

    def update(self, s, ctx):
        """s: datalog sample; ctx: {"end", "start", "paused", "fault", "t_min", "t_max", "soc_pts"}."""
        prev, self._prev = self._prev, s
        t = s["t"]
        rest_was = self._rest_since  # when the rest up to the previous sample began
        resting = (not s["chg"]) and abs(s["i"]) < REST_A
        if resting:
            if self._rest_since is None or (prev and t - prev["t"] > GAP_S and not self._gap_was_rest(prev, s)):
                self._rest_since = t
            if s.get("c"):
                self._rest_cell = (t, _avg(s["c"]))
        else:
            self._rest_since = None

        if self.pending:
            self._check_rest_after(s, prev, ctx)

        if s["chg"] and self.cur is None:
            self._open(s, prev, ctx, not self._seen_first, rest_was)
        elif self.cur is not None and not s["chg"]:
            self._close(s, prev, ctx, self._reason(s, prev, ctx))
        elif self.cur is not None:
            self._step(s, prev, ctx)
        self._seen_first = True

    def disconnected(self, t):
        """Board went offline: close any open session at the last sample we have."""
        if self.cur is not None and self._prev is not None:
            self._close(self._prev, self._prev, {}, "offline", at=self._prev["t"])
        self._seen_first = False  # a charge seen first after reconnecting started without us

    @staticmethod
    def _gap_was_rest(a, b):
        """Across an offline gap: nothing moved through the pack if the BMS's lifetime counters didn't change."""
        keys = ("ahc", "ahd")
        if any(a.get(k) is None or b.get(k) is None for k in keys):
            return False
        return all(0 <= b[k] - a[k] < 0.01 for k in keys) and not a["chg"] and not b["chg"]

    # -- session lifecycle

    def _open(self, s, prev, ctx, joined_late, rest_began):
        t = s["t"]
        rest_before = None
        # Plugging in the charger usually wakes a sleeping BMS, so the first sample after the gap
        # is already charging. If nothing was discharged while it slept, the readings from before
        # the gap still describe the rested pack, and its counters cover the whole charge.
        woke = (prev is not None and t - prev["t"] > GAP_S and not prev["chg"] and
                None not in (prev.get("ahd"), s.get("ahd"), prev.get("ahc"), s.get("ahc")) and
                0 <= s["ahd"] - prev["ahd"] < 0.01 and 0 <= s["ahc"] - prev["ahc"] < 0.5)
        if self._rest_cell and prev and rest_began is not None:
            rt, cell = self._rest_cell
            # The last rested sample just before this charge, after a long enough rest
            # (sleeping with nothing going through the pack counts as rest).
            if rt - rest_began >= REST_BEFORE_S and (t - rt < 120 or woke):
                rest_before = {"t": rt, "cell": round(cell, 4), "soc": soc_from_curve(cell, ctx.get("soc_pts"))}
        self.cur = {
            "id": time.strftime("%Y%m%d-%H%M%S", time.localtime(t)), "start": t, "joined_late": joined_late,
            "soc0": s["soc"], "v0": s["v"], "cmin0": s["cmin"], "cmax0": s["cmax"], "d0": s["d"],
            "ahc0": (prev if woke else s).get("ahc"), "whc0": (prev if woke else s).get("whc"),
            "ah": 0.0, "wh": 0.0, "i_peak": 0.0, "p_peak": 0.0, "i_sum": 0.0, "p_sum": 0.0, "dur_int": 0.0,
            "t_cell_max": None, "t_mos_max": None, "d_max": s["d"], "bal_s": 0.0, "gaps": 0,
            "end_limit": ctx.get("end"), "start_limit": ctx.get("start"), "rest_before": rest_before,
            "_recent": [(t, s["cmax"], s["i"])],
        }
        self._step(s, None, ctx)

    def _step(self, s, prev, ctx):
        c = self.cur
        t = s["t"]
        if prev is not None and prev["t"] < t:
            dt = t - prev["t"]
            if dt <= GAP_S:
                ia = (s["i"] + prev["i"]) / 2
                pa = (s["p"] + prev["p"]) / 2
                c["ah"] += ia * dt / 3600
                c["wh"] += pa * dt / 3600
                c["i_sum"] += ia * dt
                c["p_sum"] += pa * dt
                c["dur_int"] += dt
                if s["bal"]:
                    c["bal_s"] += dt
            else:
                c["gaps"] += 1
        c["i_peak"] = max(c["i_peak"], s["i"])
        c["p_peak"] = max(c["p_peak"], s["p"])
        c["d_max"] = max(c["d_max"] or 0, s["d"] or 0)
        tc = _cell_temp_max(s)
        if tc is not None:
            c["t_cell_max"] = tc if c["t_cell_max"] is None else max(c["t_cell_max"], tc)
        tm = _temp(s, "MOSFET")
        if tm is not None:
            c["t_mos_max"] = tm if c["t_mos_max"] is None else max(c["t_mos_max"], tm)
        if ctx.get("end") is not None:
            c["end_limit"] = ctx["end"]
            c["start_limit"] = ctx.get("start")
        c["_recent"].append((t, s["cmax"], s["i"]))
        cut = t - 900
        while c["_recent"] and c["_recent"][0][0] < cut:
            c["_recent"].pop(0)

    def _reason(self, s, prev, ctx):
        end = self.cur.get("end_limit")
        last = prev or s
        if ctx.get("fault"):
            return "fault"
        if s["vc"] < CHARGER_V:
            return "unplugged"
        if ctx.get("paused"):
            return "paused"
        if end is not None and max(last["cmax"] or 0, s["cmax"] or 0) >= end - LIMIT_TOL:
            return "limit"
        tc = _cell_temp_max(s)
        if tc is not None and ((ctx.get("t_max") is not None and tc >= ctx["t_max"]) or
                               (ctx.get("t_min") is not None and tc <= ctx["t_min"])):
            return "temperature"
        return "unknown"

    def _close(self, s, prev, ctx, reason, at=None):
        c, self.cur = self.cur, None
        if prev is not None and prev is not s and reason != "offline":
            self.cur = c
            self._step(s, prev, ctx)  # integrate up to the stop
            self.cur = None
        end_t = at or s["t"]
        dur = end_t - c["start"]
        rec = {
            "id": c["id"], "start": c["start"], "end": end_t, "duration_s": round(dur),
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["start"])),
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_t)),
            "soc0": c["soc0"], "soc1": s["soc"], "v0": c["v0"], "v1": s["v"],
            "cmin0": c["cmin0"], "cmax0": c["cmax0"], "cmin1": s["cmin"], "cmax1": s["cmax"],
            "d0": c["d0"], "d1": s["d"], "d_max": c["d_max"],
            "ah": round(c["ah"], 4), "wh": round(c["wh"], 2),
            "ah_bms": round(s["ahc"] - c["ahc0"], 4) if s.get("ahc") is not None and c["ahc0"] is not None else None,
            "wh_bms": round(s["whc"] - c["whc0"], 2) if s.get("whc") is not None and c["whc0"] is not None else None,
            "i_peak": round(c["i_peak"], 2), "p_peak": round(c["p_peak"], 1),
            "i_avg": round(c["i_sum"] / c["dur_int"], 2) if c["dur_int"] else None,
            "p_avg": round(c["p_sum"] / c["dur_int"], 1) if c["dur_int"] else None,
            "t_cell_max": c["t_cell_max"], "t_mos_max": c["t_mos_max"], "bal_s": round(c["bal_s"]),
            "end_limit": c["end_limit"], "start_limit": c["start_limit"], "reason": reason,
            "reason_text": REASONS.get(reason, reason), "joined_late": c["joined_late"], "gaps": c["gaps"],
            "cells1": [round(x, 4) for x in (s.get("c") or [])], "rest_before": c["rest_before"],
            "rest_after": None, "capacity_ah": None,
        }
        self.last = rec
        self.pending = rec if rec["rest_before"] and rec["rest_before"]["soc"] is not None else None
        self.on_close(rec)

    def _check_rest_after(self, s, prev, ctx):
        p = self.pending
        if s["chg"]:
            self.pending = None  # charging again before the pack rested
            return
        if not s.get("c") or abs(s["i"]) >= REST_A:
            return
        if prev and s["t"] - prev["t"] > GAP_S and not self._gap_was_rest(prev, s):
            self.pending = None  # offline and the pack was used meanwhile
            return
        if s["t"] - p["end"] < REST_AFTER_S:
            return
        cell = _avg(s["c"])
        soc1 = soc_from_curve(cell, ctx.get("soc_pts"))
        self.pending = None
        p["rest_after"] = {"t": s["t"], "cell": round(cell, 4), "soc": soc1}
        soc0 = p["rest_before"]["soc"]
        ah = p["ah_bms"] if p.get("ah_bms") and p["ah_bms"] > 0 else p["ah"]
        if soc1 is not None and soc1 - soc0 >= MIN_DSOC and ah > 0 and not p["gaps"]:
            p["capacity_ah"] = round(ah / ((soc1 - soc0) / 100), 2)
        self.on_update({"event": "rest_after", "id": p["id"], "rest_after": p["rest_after"],
                        "capacity_ah": p["capacity_ah"]})

    # -- live view

    def live(self, now=None):
        """What the UI shows while charging: elapsed, Ah/Wh so far, ETA to Charge End."""
        c = self.cur
        if c is None:
            return None
        now = now or (self._prev["t"] if self._prev else time.time())
        out = {"id": c["id"], "start": c["start"], "elapsed_s": round(now - c["start"]), "ah": round(c["ah"], 3),
               "wh": round(c["wh"], 1), "soc0": c["soc0"], "joined_late": c["joined_late"], "eta": None}
        out["eta"] = eta(c["_recent"], c.get("end_limit"), now - c["start"])
        return out


def eta(recent, end, elapsed):
    """Seconds until the highest cell reaches Charge End, from the last 10 min's slope. None if unsure."""
    if end is None or elapsed < 180 or len(recent) < 30:
        return None
    t1 = recent[-1][0]
    pts = [(t, v) for t, v, _ in recent if t >= t1 - 600 and v is not None]
    if len(pts) < 30 or pts[-1][0] - pts[0][0] < 120:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    var = sum((t - mt) ** 2 for t, _ in pts)
    if var <= 0:
        return None
    slope = sum((t - mt) * (v - mv) for t, v in pts) / var  # V/s
    if slope < 0.0002 / 60:  # under 0.2 mV/min: too flat to say
        return None
    left = end - pts[-1][1]
    if left <= 0:
        return 0
    sec = left / slope
    return round(sec) if sec < 86400 else None


def _temp(s, name):
    names = s.get("tpn") or []
    tp = s.get("tp") or []
    for n, x in zip(names, tp):
        if n == name:
            return x
    return None


def _cell_temp_max(s):
    names = s.get("tpn") or []
    tp = s.get("tp") or []
    xs = [x for n, x in zip(names, tp) if n.startswith("Cell sensor") or n.startswith("Sensor")]
    return max(xs) if xs else None

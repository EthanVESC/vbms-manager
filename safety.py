"""Safety notices and the low-SOC sleep settings.

Notices are slow, long-running checks (days, not seconds), so they use the board's
live values when it's connected and its last-known values (profile "last_seen")
while it's asleep or offline. The "since" times live in the profile ("safety_state")
so they survive restarts. Pure logic, no board I/O: evaluate() is called from the
scheduler loop about every 30 s.

Notices:
  below_nominal - pack under cells x nominal V/cell for N days without a charge
  low_cell      - a cell under the low-cell voltage (right away)
  stored_full   - SOC above X % without charging for N days (suggest storage mode)
  rested_low    - disconnected so the BMS can sleep (low SOC); until it reconnects
"""

import time

DEFAULTS = {
    "low_soc_on": True, "low_soc_pct": 20, "low_soc_hours": 2.0, "probe_hours": 6.0,
    "nominal_on": True, "nominal_v": 3.60, "nominal_days": 3.0,
    "low_cell_on": True, "low_cell_v": 3.20,
    "full_on": True, "full_pct": 90, "full_days": 7.0,
}
LIMITS = {
    "low_soc_pct": (5, 50), "low_soc_hours": (0.25, 48), "probe_hours": (0.5, 48),
    "nominal_v": (3.0, 3.9), "nominal_days": (0.5, 60),
    "low_cell_v": (2.8, 3.6),
    "full_pct": (70, 100), "full_days": (1, 90),
}
SWITCHES = ("low_soc_on", "nominal_on", "low_cell_on", "full_on")
LOW_SOC_HYST = 5          # % above the threshold before a resting board stays connected
NOMINAL_HYST_V = 0.05     # per cell, above nominal before "below nominal" clears
FULL_HYST = 5             # % below full_pct before "stored full" clears


def settings_from(all_settings):
    st = dict(DEFAULTS)
    st.update({k: v for k, v in (all_settings.get("safety") or {}).items() if k in DEFAULTS})
    return st


def clean(body, cur):
    """Validated copy of cur with the changes in body. Raises ValueError."""
    out = dict(cur)
    for k, (lo, hi) in LIMITS.items():
        if k in body:
            try:
                v = float(body[k])
            except (TypeError, ValueError):
                raise ValueError("%s must be a number" % k)
            if not lo <= v <= hi:
                raise ValueError("%s must be between %g and %g" % (k, lo, hi))
            out[k] = v
    for k in SWITCHES:
        if k in body:
            out[k] = bool(body[k])
    return out


def seen_record(v, t):
    return {"t": round(t), "v": round(v["v_tot"], 2), "soc": round(v["soc"], 3),
            "cmin": v["cell_min"], "cmax": v["cell_max"], "n": len(v["cells"]),
            "chg": bool(v["is_charging"]), "vc": round(v["v_charge"], 1)}


def low_soc_text(st, soc):
    return ("SOC has been under %g%% for %g h (now %.0f%%) with no charger, so VBMS Manager disconnected to let "
            "the BMS sleep instead of keeping it awake. Plug in the charger: it checks back every %g h (or press "
            "Reconnect)." % (st["low_soc_pct"], st["low_soc_hours"], soc * 100, st["probe_hours"]))


def _ago(s):
    d = s / 86400
    if d >= 1:
        return "%.0f day%s" % (d, "" if round(d) == 1 else "s")
    return "%.0f hours" % (s / 3600)


def evaluate(board, now=None, settings=None):
    """Work out this board's notices (and save changed "since" times). Returns the list."""
    if not board.mac:
        return []
    now = time.time() if now is None else now
    st = settings_from(settings if settings is not None else board.boards.settings())
    prof = board.profile()
    v = board.values if board.state == "online" and board.values else None
    if v is not None:
        cur = seen_record(v, now)
    else:
        cur = prof.get("last_seen")
    old = dict(prof.get("safety_state") or {})
    ss = dict(old)
    notices = []
    stor_on = bool((prof.get("storage") or {}).get("on"))

    if cur and cur.get("n"):
        n = cur["n"]
        charging = cur.get("chg")
        # below nominal for days
        nominal = n * st["nominal_v"]
        if charging or cur["v"] >= n * (st["nominal_v"] + NOMINAL_HYST_V):
            ss.pop("below_nominal", None)
        elif cur["v"] < nominal:
            ss.setdefault("below_nominal", cur["t"])
        if st["nominal_on"] and "below_nominal" in ss and now - ss["below_nominal"] >= st["nominal_days"] * 86400:
            notices.append({"key": "below_nominal", "level": "warn", "since": ss["below_nominal"],
                            "text": "Your pack voltage has been below nominal (%.1f V, %.2f V/cell) for %s: you "
                                    "should connect the charger. Last reading %.1f V." % (
                                        nominal, st["nominal_v"], _ago(now - ss["below_nominal"]), cur["v"])})
        # low cell, right away
        if st["low_cell_on"] and cur.get("cmin") is not None and cur["cmin"] < st["low_cell_v"] and not charging:
            notices.append({"key": "low_cell", "level": "bad", "since": cur["t"],
                            "text": "A cell is at %.3f V (under %.2f V): charge soon. Cells left this low can be "
                                    "damaged by over-discharge." % (cur["cmin"], st["low_cell_v"])})
        # stored full
        soc = cur["soc"] * 100
        if charging or stor_on or soc < st["full_pct"] - FULL_HYST:
            ss.pop("full", None)
        elif soc >= st["full_pct"]:
            ss.setdefault("full", cur["t"])
        if st["full_on"] and "full" in ss and now - ss["full"] >= st["full_days"] * 86400:
            notices.append({"key": "stored_full", "level": "warn", "since": ss["full"],
                            "text": "This pack has sat above %g%% for %s. Long storage at full charge ages "
                                    "cells: consider Storage mode." % (st["full_pct"], _ago(now - ss["full"]))})

    rest = board.resting()
    if rest and rest.get("reason") == "low_soc":
        notices.append({"key": "rested_low", "level": "warn", "since": rest["since"],
                        "text": "Disconnected so the BMS can sleep: SOC stayed under %g%% for %g h. Plug in the "
                                "charger; VBMS Manager checks back every %g h, or press Reconnect." % (
                                    st["low_soc_pct"], st["low_soc_hours"], rest["probe_s"] / 3600)})
    elif rest and rest.get("reason") == "storage":
        notices.append({"key": "rested_storage", "level": "info", "since": rest["since"],
                        "text": "Storage mode: disconnected so the BMS can sleep. VBMS Manager checks back every "
                                "%g h." % (rest["probe_s"] / 3600)})

    if ss != old:
        board.boards.update(board.mac, safety_state=ss)
    before = {x["key"] for x in board.notices}
    for x in notices:
        if x["key"] not in before and x["level"] in ("warn", "bad") and not x["key"].startswith("rested"):
            board._event("notice", x["text"], notice=x["key"])
    board.notices = notices
    return notices

"""Charge-limit math: percentage -> (vc_charge_start, vc_charge_end).

Pure functions, no I/O. Rules:
  - percentage is a whole number from 60 to 100
  - end comes from the cell curve; 100% is the profile's max end (4.21 V for NMC)
  - start is end - 0.100 V by default; a start/end pair set directly (the graph
    with its lines unlinked) may use any gap from 0.050 to 0.200 V
  - hard limits are checked independently of the curve; anything outside them
    is refused rather than silently clamped
"""

PCT_MIN, PCT_MAX = 60, 100
GAP = 0.100               # default: percentages, schedules and timers always use this
GAP_MIN, GAP_MAX = 0.050, 0.200   # hard range for a gap you set yourself
GAP_TOL = 0.0005          # float32 storage makes 4.21-4.11 read back as 0.09999990
ABS_MAX_END = 4.25        # never write an end above this, whatever a profile says
ABS_MIN_END = 3.60
CURVE_MIN_V = 2.50          # lowest voltage a curve point may have (0% sits near 2.8 V)


class LimitError(ValueError):
    pass


def validate_curve(curve, max_end):
    """curve: list of [pct, volts]. Must cover 60..100 (the bundled curves cover 0..100), strictly increasing in both."""
    if not isinstance(curve, list) or len(curve) < 2:
        raise LimitError("curve needs at least two points")
    pts = []
    for p in curve:
        try:
            pct, v = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            raise LimitError("curve point %r is not [percent, volts]" % (p,))
        pts.append((pct, v))
    pts.sort()
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if p1 <= p0 or v1 <= v0:
            raise LimitError("curve must be strictly increasing (problem near %g%%)" % p1)
    if pts[0][0] > PCT_MIN or pts[-1][0] < PCT_MAX:
        raise LimitError("curve must cover %d%%..%d%%" % (PCT_MIN, PCT_MAX))
    for p, v in pts:
        if not (CURVE_MIN_V <= v <= ABS_MAX_END):
            raise LimitError("curve voltage %.3f V at %g%% is out of range" % (v, p))
    if not (ABS_MIN_END <= max_end <= ABS_MAX_END):
        raise LimitError("profile max end %.3f V is outside %.2f..%.2f V" % (max_end, ABS_MIN_END, ABS_MAX_END))
    if interp(pts, PCT_MAX - 1) >= max_end:
        raise LimitError("curve reaches the max end voltage before 100%")
    return pts


def interp(pts, pct):
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if p0 <= pct <= p1:
            return v0 + (v1 - v0) * (pct - p0) / (p1 - p0)
    raise LimitError("%g%% is outside the curve" % pct)


def compute(pct, curve, max_end, device):
    """Return {'pct','end','start'} or raise LimitError.

    device: dict with the board's current vc_charge_min.
    """
    if isinstance(pct, bool) or not isinstance(pct, int):
        raise LimitError("percentage must be a whole number")
    if not (PCT_MIN <= pct <= PCT_MAX):
        raise LimitError("percentage must be between %d and %d" % (PCT_MIN, PCT_MAX))
    pts = validate_curve(curve, max_end)
    end = max_end if pct == PCT_MAX else interp(pts, pct)
    end = round(end, 3)
    start = round(end - GAP, 3)
    check(start, end, max_end, device)
    return {"pct": pct, "end": end, "start": start}


def check(start, end, max_end, device):
    """Hard limits for any start/end pair the tool might write."""
    if end > ABS_MAX_END:
        raise LimitError("end %.3f V is above the absolute limit %.2f V" % (end, ABS_MAX_END))
    if end > max_end + 1e-9:
        raise LimitError("end %.3f V is above this cell profile's max %.3f V" % (end, max_end))
    if end < ABS_MIN_END:
        raise LimitError("end %.3f V is below %.2f V" % (end, ABS_MIN_END))
    if not gap_valid(start, end):
        raise LimitError("start must be %.3f-%.3f V below end (it's %.3f V)" % (GAP_MIN, GAP_MAX, end - start))
    vmin = float(device.get("vc_charge_min", 0.0))
    if start <= vmin:
        raise LimitError("start %.3f V must be above Charge Minimum Voltage %.3f V" % (start, vmin))


def balance_note(start, end, bmin):
    """Caution text when Balance Minimum sits above the charge window (allowed, but balancing waits), else None."""
    if bmin is None or bmin <= start + GAP_TOL:
        return None
    if bmin > end + GAP_TOL:
        return ("Balance Minimum (%.3f V) is above Charge End (%.3f V): the lowest cell never reaches it on these "
                "charges, so the pack won't balance until you charge higher." % (bmin, end))
    return ("Balance Minimum (%.3f V) is above Charge Start (%.3f V): balancing only runs near the top of a charge, "
            "once the lowest cell passes %.3f V." % (bmin, start, bmin))


def gap_ok(start, end):
    """The default 0.100 V gap."""
    return abs((end - start) - GAP) <= GAP_TOL


def gap_valid(start, end):
    """Inside the hard range (0.050-0.200 V)."""
    return GAP_MIN - GAP_TOL <= end - start <= GAP_MAX + GAP_TOL


def pct_for_end(end, curve, max_end):
    """Best-effort percentage for an existing end voltage (for display only)."""
    try:
        pts = validate_curve(curve, max_end)
    except LimitError:
        return None
    if end >= max_end - 0.0005:
        return 100.0
    tol = 0.0005  # float32 storage: 3.86 reads back as 3.8599999
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if v0 - tol <= end <= v1 + tol:
            end = min(max(end, v0), v1)
            return round(p0 + (p1 - p0) * (end - v0) / (v1 - v0), 1)
    # Between the curve's 99% point and max end, or below the curve
    if end < pts[0][1] - tol:
        return None
    return 99.5

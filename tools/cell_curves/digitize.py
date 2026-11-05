"""Digitize discharge charts into resting-voltage-vs-SOC tables (see README.md here).

Reads a "Discharge, capacity" chart (800x400 PNG, frame = 4.2 V top / 2.8 V bottom) and
takes the top envelope of all curves, which is the lowest-current (0.2 A) discharge.
At 0.2 A (C/12 to C/25 for these cells) the voltage is within ~10 mV of the resting
voltage, so this is a close stand-in for the open-circuit voltage curve.

    python3 tools/cell_curves/digitize.py chart.png AH_PER_GRIDLINE

Prints SOC% -> volts at 5 % steps (SOC from the measured 0.2 A capacity to 2.8 V).
envelope_xy() does the same for a datasheet or lab chart, given its axis calibration.
Needs Pillow. Not used by the app at run time; the results are copied into cells/profiles.json.
"""

import sys

from PIL import Image

BG = (180, 180, 200)
V_TOP, V_BOTTOM = 4.2, 2.8


def _is_bg(p):
    return abs(p[0] - BG[0]) < 6 and abs(p[1] - BG[1]) < 6 and abs(p[2] - BG[2]) < 6


def _is_gridish(p):
    """Background, gridlines and their anti-aliasing: greyish-lavender, low saturation."""
    return max(p) - min(p) < 30 and 160 <= p[0] <= 225


def frame(px, w, h):
    rows = [y for y in range(h) if sum(_is_bg(px[x, y]) for x in range(0, w, 4)) > w // 16]
    cols = [x for x in range(w) if sum(_is_bg(px[x, y]) for y in range(0, h, 4)) > h // 16]
    return cols[0] - 1, cols[-1] + 1, rows[0] - 1, rows[-1] + 1   # the frame lines


def vgrid(px, left, right, top, bottom):
    """x of the vertical gridlines, read just under the top frame where no curve runs."""
    y = top + 6
    return [x for x in range(left + 2, right - 1) if px[x, y] == (199, 199, 214)]


def envelope(path, ah_per_grid):
    im = Image.open(path).convert("RGB")
    px, (w, h) = im.load(), im.size
    left, right, top, bottom = frame(px, w, h)
    grid = vgrid(px, left, right, top, bottom)
    step_px = (grid[-1] - grid[0]) / (len(grid) - 1)
    x0 = grid[0] - step_px      # the first gridline is ah_per_grid, so 0 Ah is one step left of it
    assert abs(x0 - left) < 2, "0 Ah should sit on the left frame"
    ah = lambda x: (x - x0) / step_px * ah_per_grid
    volts = lambda y: V_TOP - (y + 0.5 - top) / (bottom - top) * (V_TOP - V_BOTTOM)
    pts = []
    for x in range(left + 1, right):
        for y in range(top + 1, bottom):
            p = px[x, y]
            if y < top + 40 and x > left + (right - left) * 0.75:
                continue  # the www.lygte-info.dk watermark
            if not _is_gridish(p):
                pts.append((ah(x), volts(y)))
                break
    # The curve ends where it reaches the 2.8 V frame.
    end = next((a for a, v in pts if v <= V_BOTTOM + 0.03), pts[-1][0])
    pts = [(a, v) for a, v in pts if a <= end]
    # Monotonic: voltage never rises as capacity is taken out.
    mono, lo = [], 9.0
    for a, v in pts:
        lo = min(lo, v)
        mono.append((a, lo))
    return mono, end, (left, right, top, bottom, step_px, len(grid))


def at(pts, a):
    for (a0, v0), (a1, v1) in zip(pts, pts[1:]):
        if a0 <= a <= a1:
            return v0 if a1 == a0 else v0 + (v1 - v0) * (a - a0) / (a1 - a0)
    return pts[-1][1] if a > pts[-1][0] else pts[0][1]


def soc_table(pts, cap, step=5):
    return [[s, round(at(pts, cap * (1 - s / 100)), 3)] for s in range(0, 101, step)]



def envelope_xy(path, box, xcal, ycal, is_curve, exclude=(), cutoff=V_BOTTOM):
    """Top envelope of the pixels `is_curve` picks inside box=(x0, y0, x1, y1), for a datasheet chart.

    xcal/ycal: ((pixel, value), (pixel, value)) read off two gridlines or axis ends; x in Ah.
    exclude: boxes (x0, y0, x1, y1) with labels or legends to skip. Returns (points, capacity to cutoff).
    """
    im = Image.open(path).convert("RGB")
    px = im.load()
    (xa, va), (xb, vb) = xcal
    (ya, wa), (yb, wb) = ycal
    ah = lambda x: va + (x - xa) * (vb - va) / (xb - xa)
    volts = lambda y: wa + (y - ya) * (wb - wa) / (yb - ya)
    x0, y0, x1, y1 = box
    pts = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            if any(a <= x <= c and b <= y <= d for a, b, c, d in exclude):
                continue
            if is_curve(px[x, y]):
                # Datasheet lines are several pixels thick: use the middle of the topmost line.
                e = y
                while e + 1 <= y1 and is_curve(px[x, e + 1]):
                    e += 1
                pts.append((ah(x), volts((y + e) / 2)))
                break
    pts = [(max(a, 0.0), v) for a, v in pts]
    end = next((a for a, v in pts if v <= cutoff), pts[-1][0])
    pts = [(a, v) for a, v in pts if a <= end]
    mono, lo = [], 9.0
    for a, v in pts:
        lo = min(lo, v)
        mono.append((a, lo))
    return mono, end


if __name__ == "__main__":
    pts, cap, info = envelope(sys.argv[1], float(sys.argv[2]))
    print("frame/grid", info, "capacity to 2.8 V: %.3f Ah" % cap)
    print(soc_table(pts, cap))

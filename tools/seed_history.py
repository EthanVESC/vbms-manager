"""Write synthetic history for one board, for checking the History views without waiting weeks.

    VBMS_MANAGER_HOME=/tmp/vbms_test python3 tools/seed_history.py 02:00:00:00:21:0a --days 30

Never point this at your real ~/.vbms_manager: it writes made-up recordings and charge sessions.
Each day: rest, an evening ride, a charge to ~80 % at night, then the BMS sleeps (a gap with the
counters unchanged). Cell 7 drifts low over the period and cell 12 self-discharges faster.
"""

import argparse
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datalog  # noqa: E402
import store  # noqa: E402

TEMPS = ["IC", "Cell min", "Cell max", "MOSFET", "Cell sensor 1", "Cell sensor 2", "Cell sensor 3", "Cell sensor 4"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mac")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--cells", type=int, default=32)
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--pack-ah", type=float, default=15.0)
    a = ap.parse_args()
    if not os.environ.get("VBMS_MANAGER_HOME"):
        sys.exit("set VBMS_MANAGER_HOME to a test folder first")
    n, iv = a.cells, a.interval
    d = datalog.board_dir(a.mac)
    os.makedirs(d, exist_ok=True)
    today0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    soc, tot = 0.55, {"ahc": 500.0, "whc": 60000.0, "ahd": 480.0, "whd": 57000.0}
    sleep_drop = [0.0] * n
    charges = []
    for day in range(a.days, 0, -1):
        t0 = today0 - day * 86400
        rows, sess = [], None
        drift7 = -0.003 - 0.010 * (a.days - day) / a.days
        for k in range(0, 86400, iv):
            t = t0 + k
            h = k / 3600
            if 2 <= h < 7:  # asleep: no rows; cell 12 leaks a little faster
                for c in range(n):
                    sleep_drop[c] += (0.0008 + (0.0012 if c == 11 else 0)) * iv / 3600
                continue
            if 17.5 <= h < 18.5:
                i = -10.0
            elif 21 <= h and soc < 0.80:
                i = 4.5
            else:
                i = 0.0
            soc = min(1, max(0.05, soc + i * iv / 3600 / a.pack_ah))
            load = 0.035 if i > 0 else -0.07 if i < 0 else 0
            cells = [round(3.30 + 0.90 * soc + load + ((c * 37) % 7 - 3) / 1000 - sleep_drop[c] * 0.2
                           + (drift7 if c == 6 else 0), 4) for c in range(n)]
            vt = sum(cells)
            if i > 0:
                tot["ahc"] += i * iv / 3600
                tot["whc"] += i * vt * iv / 3600
            elif i < 0:
                tot["ahd"] += -i * iv / 3600
                tot["whd"] += -i * vt * iv / 3600
            warm = 6 if i > 0 else 4 if i < 0 else 0
            tp = [38, 0, 0, 33 + warm] + [round(22 + warm + s * 0.4, 1) for s in range(4)]
            tp[1], tp[2] = min(tp[4:]), max(tp[4:])
            chg = i > 0
            rows.append({"t": round(t, 1), "n": iv, "v": round(vt, 3), "i": i, "p": round(vt * i, 1), "soc": round(soc, 4),
                         "cmin": min(cells), "cmax": max(cells), "d": round((max(cells) - min(cells)) * 1000, 1),
                         "tmax": max(tp), "vc": round(vt + 4, 2) if chg else 0.0, "chg": int(chg), "bal": 0,
                         "c": cells, "tp": tp, "bm": 0, "ah": 0, "wh": 0, "hum": 40, "soh": 1.0,
                         "ahc": round(tot["ahc"], 4), "whc": round(tot["whc"], 2), "ahd": round(tot["ahd"], 4),
                         "whd": round(tot["whd"], 2)})
            if chg and sess is None:
                sess = {"start": t, "soc0": soc, "ah": 0, "wh": 0, "p_peak": 0, "v0": vt}
            if sess is not None and chg:
                sess["ah"] += i * iv / 3600
                sess["wh"] += i * vt * iv / 3600
                sess["p_peak"] = max(sess["p_peak"], vt * i)
            if sess is not None and not chg:
                cid = time.strftime("%Y%m%d-%H%M%S", time.localtime(sess["start"]))
                charges.append({"id": cid, "mac": a.mac, "start": sess["start"], "end": t, "duration_s": round(t - sess["start"]),
                                "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess["start"])),
                                "soc0": sess["soc0"], "soc1": soc, "ah": round(sess["ah"], 3), "wh": round(sess["wh"], 1),
                                "p_peak": round(sess["p_peak"], 1), "p_avg": round(sess["wh"] / max(1, (t - sess["start"]) / 3600), 1),
                                "i_peak": 4.5, "i_avg": 4.5, "t_cell_max": 29.2, "reason": "limit",
                                "reason_text": "reached the charge limit", "joined_late": False, "gaps": 0,
                                "cells1": cells, "d1": round((max(cells) - min(cells)) * 1000, 1),
                                "capacity_ah": round(a.pack_ah * (0.97 - 0.002 * (a.days - day)), 2) if day % 3 == 0 else None})
                sess = None
        name = time.strftime("%Y-%m-%d", time.localtime(t0)) + ".jsonl.gz"
        hdr = {"hdr": 1, "level": "detailed", "interval_s": iv, "cells": n, "temps": TEMPS, "sign": "+ = into pack"}
        with gzip.open(os.path.join(d, name), "wt", encoding="utf-8") as f:
            f.write(json.dumps(hdr) + "\n")
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    for c in charges:
        store.append_log("charges", c)
    print("wrote %d days and %d charge sessions to %s" % (a.days, len(charges), d))


if __name__ == "__main__":
    main()

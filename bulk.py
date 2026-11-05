"""All-boards actions: allow/pause charging, allow/disallow balancing, storage mode on/off.

The dashboard shows one confirmation listing every board (prepare -> commit); the API runs
them straight away (run). Each board goes through its normal path: charging is the RAM
command, balancing and storage are checked flash writes. One board failing doesn't stop the rest.
"""

import secrets
import threading
import time

import safe_write
import storage
from vesc.client import VescError

ACTIONS = {
    "charging_on": "Allow charging", "charging_off": "Pause charging",
    "balancing_on": "Allow balancing", "balancing_off": "Disallow balancing",
    "storage_on": "Storage mode on", "storage_off": "Storage mode off",
}
TTL = 120.0
_pending = {}
_lock = threading.Lock()


class BulkError(ValueError):
    pass


def _name(b):
    return b.profile().get("nickname") or (b.fw or {}).get("hw_name") or b.host


def plan_one(app, b, action):
    """(what will happen, or None) and (why it can't, or None) for one board."""
    prof = b.profile() if b.mac else {}
    stor = storage.is_on(prof)
    if not b.mac:
        return None, "never connected"
    if not safe_write.risk_accepted(prof):
        return None, "safety warning not accepted"
    online = b.state == "online" and b.config is not None
    if action == "storage_off":
        if not stor:
            return None, "not in storage mode"
        return ("settings from before put back" if online else
                "put back as soon as it's connected (it's %s)" % b.state), None
    if not online:
        return None, "board is %s" % b.state
    if action == "storage_on":
        if stor:
            return None, "already in storage mode"
        if not b.cell_profile():
            return None, "no cell type chosen"
        c = storage.cfg(prof)
        return "end %.2f / start %.2f V per cell, Balance Min %.2f V" % (c["end_v"], c["end_v"] - c["gap"],
                                                                         c["balance_min"]), None
    if action.startswith("balancing") and stor:
        return None, "in storage mode (it sets balancing)"
    if action == "charging_on":
        return "charging allowed (RAM command)", None
    if action == "charging_off":
        return "charging paused until the BMS restarts or wakes (RAM command)", None
    if action == "balancing_on":
        return "Balance Max Current back to %.2f A (flash write)" % app._balance_amps(b), None
    return "Balance Max Current = 0 A (flash write)", None


def prepare(app, action):
    if action not in ACTIONS:
        raise BulkError("unknown action")
    with app.lock:
        boards = list(app.boards.values())
    rows = []
    for b in boards:
        will, blocked = plan_one(app, b, action)
        rows.append({"host": b.host, "mac": b.mac, "name": _name(b), "state": b.state, "will": will,
                     "blocked": blocked})
    pid = secrets.token_hex(8)
    with _lock:
        for k in [k for k, v in _pending.items() if time.time() - v["created"] > TTL]:
            del _pending[k]
        _pending[pid] = {"action": action, "created": time.time(), "hosts": [r["host"] for r in rows if r["will"]]}
    return {"id": pid, "action": action, "label": ACTIONS[action], "rows": rows,
            "count": sum(1 for r in rows if r["will"])}


def run_one(app, b, action, why, via=None):
    """Do it on one board. Returns a short result text; raises safe_write.WriteRefused / StorageError / VescError."""
    if action in ("charging_on", "charging_off"):
        val = 1 if action == "charging_on" else 0
        if app.scheduler.override(b, "charge", val):
            why += ", overriding the schedule until its next change"
        b.send_runtime("charge_allowed", val, why)
        return "charging %s" % ("allowed" if val else "paused")
    if action in ("balancing_on", "balancing_off"):
        allow = action == "balancing_on"
        prop = safe_write.prepare_balance(b, allow, register=False,
                                          reason="%s balancing (%s)" % ("allow" if allow else "disallow", why))
        app.scheduler.override(b, "balance", 1 if allow else 0)
        if prop is None:
            return "already %s" % ("allowed" if allow else "disallowed")
        safe_write.apply(b, prop, via)
        return "balancing %s" % ("allowed" if allow else "disallowed")
    on = action == "storage_on"
    if not on and not (b.state == "online" and b.config is not None):
        storage.request_off_later(b)
        return "storage off as soon as it's connected"
    res = storage.apply(b, on, via=via, reason="storage mode %s (%s)" % ("on" if on else "off", why), app=app)
    return "storage mode %s%s" % ("on" if on else "off", " (no change needed)" if res.get("noop") else "")


def run(app, action, why, via=None, hosts=None):
    """Every board (or the given hosts) that can take the action. Returns per-board results."""
    if action not in ACTIONS:
        raise BulkError("unknown action")
    with app.lock:
        boards = list(app.boards.values())
    out = []
    for b in boards:
        if hosts is not None and b.host not in hosts:
            continue
        will, blocked = plan_one(app, b, action)
        row = {"host": b.host, "mac": b.mac, "name": _name(b)}
        if blocked:
            out.append(dict(row, ok=False, skipped=True, result=blocked))
            continue
        try:
            out.append(dict(row, ok=True, result=run_one(app, b, action, why, via)))
        except (safe_write.WriteRefused, storage.StorageError, VescError, OSError) as e:
            out.append(dict(row, ok=False, result=str(e)))
    return {"ok": all(r["ok"] or r.get("skipped") for r in out), "action": action, "results": out}


def commit(app, pid, skipped=False):
    with _lock:
        p = _pending.pop(pid, None)
    if p is None or time.time() - p["created"] > TTL:
        raise BulkError("this is no longer pending; try again")
    why = "by you (all boards)" + (" (no confirmation: skip confirmations is on)" if skipped else "")
    return run(app, p["action"], why, hosts=set(p["hosts"]))

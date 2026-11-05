"""Storage mode: settings for long-term storage, and putting the old ones back afterwards.

On:  save Charge Start/End, Balance Min and Balance Max Current (profile storage.saved), then write
     the storage values in ONE write (safe_write.prepare_storage):
       Charge End   = end_v per cell (default 3.75 V), Start = End - gap (default 0.100 V)
       Balance Min  = balance_min (default 3.40 V), balancing allowed (Balance Max Current > 0)
     While it's on the schedule and timers leave the board alone, and (option "sleep", default on)
     the tool disconnects once there's no charger so the BMS can sleep (board.py _check_rest).
Off: write the saved values back (same checks) and reconnect normally.

The pack isn't discharged: the BMS can't, so a pack above storage voltage just stays there until used.
"""

import time

import charge_limit
import safe_write

DEFAULTS = {"end_v": 3.75, "gap": 0.100, "balance_min": 3.40, "balance": True, "sleep": True}
LIMITS = {"end_v": (charge_limit.ABS_MIN_END, 4.00), "gap": (charge_limit.GAP_MIN, charge_limit.GAP_MAX),
          "balance_min": (3.00, 4.20)}
SAVED = ("vc_charge_start", "vc_charge_end", "vc_balance_min", "balance_max_current")


class StorageError(ValueError):
    pass


def cfg(prof):
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (prof.get("storage_cfg") or {}).items() if k in DEFAULTS})
    return c


def clean_cfg(body, cur):
    out = dict(cur)
    for k, (lo, hi) in LIMITS.items():
        if k in body:
            try:
                v = round(float(body[k]), 3)
            except (TypeError, ValueError):
                raise StorageError("%s must be a number" % k)
            if not lo - 1e-9 <= v <= hi + 1e-9:
                raise StorageError("%s must be between %g and %g" % (k, lo, hi))
            out[k] = v
    for k in ("balance", "sleep"):
        if k in body:
            out[k] = bool(body[k])
    if out["end_v"] - out["gap"] < 3.0:
        raise StorageError("Charge Start would be below 3.0 V")
    return out


def is_on(prof):
    return bool((prof.get("storage") or {}).get("on"))


def plan(board, on, reason=None, register=True):
    """A write proposal for turning storage on/off (or re-applying changed storage settings).

    Returns (proposal_or_None, info). None = the board already has those values; call finish(board, info)
    directly. Raises safe_write.WriteRefused / StorageError.
    """
    prof = board.profile()
    st = prof.get("storage") or {}
    if on:
        c = cfg(prof)
        cur_amps = safe_write.read_balance_current(board)   # also refreshes board.config
        if c["balance"]:
            amps = cur_amps if cur_amps > 0 else float(prof.get("balance_normal_a") or 1.0)
        else:
            amps = 0.0
        end = c["end_v"]
        start = round(end - c["gap"], 3)
        why = reason or "storage mode on: end %.3f / start %.3f V, Balance Min %.2f V" % (end, start, c["balance_min"])
        target = {"vc_charge_start": start, "vc_charge_end": end, "vc_balance_min": c["balance_min"],
                  "balance_max_current": amps}
        prop = safe_write.prepare_storage(board, start, end, c["balance_min"], amps, why, register=False)
        info = {"on": True, "saved": st.get("saved") if st.get("on") else _pending_saved(board, st, target)}
        if not c["balance"] and cur_amps > 0:
            info["balance_normal_a"] = cur_amps   # remembered in finish(), once storage mode is really on
    else:
        if not st.get("on"):
            raise StorageError("storage mode is not on")
        saved = st.get("saved") or {}
        if any(k not in saved for k in SAVED):
            raise StorageError("the settings from before storage mode weren't saved; set the charge limit and "
                               "balancing by hand, then turn storage mode off again")
        why = reason or "storage mode off: back to the settings from before"
        prop = safe_write.prepare_storage(board, saved["vc_charge_start"], saved["vc_charge_end"],
                                          saved["vc_balance_min"], saved["balance_max_current"], why, register=False)
        info = {"on": False}
    if prop is None:
        if on and info["saved"] is None:
            info["saved"] = {k: round(board.config.values[k], 4) for k in SAVED}
        return None, info
    if on and info["saved"] is None:
        before = board.config.schema.decode(prop["blob_before"]).values
        info["saved"] = {k: round(before[k], 4) for k in SAVED}
    if on and not st.get("on"):
        info["target"] = target
    prop["storage"] = info
    if register:
        safe_write._register(prop)
    return prop, info


def _pending_saved(board, st, target):
    """The settings from before an earlier storage-on write that failed half way (sent, but not confirmed): if the
    board now holds exactly the storage values, that write did land, and these are the values to put back later.
    Otherwise None (they'd be stale)."""
    pend = st.get("pending") or {}
    vals = board.config.values if board.config is not None else {}
    if pend.get("saved") and pend.get("target") == target and all(
            k in vals and abs(vals[k] - v) < 0.0006 for k, v in target.items()):
        return pend["saved"]
    return None


def remember_before_write(board, info):
    """Called just before a storage-on write: keep the settings from before somewhere that survives the write
    failing after it reached the board (read-back mismatch, lost connection), so they aren't lost."""
    if info.get("on") and info.get("target"):
        st = dict(board.profile().get("storage") or {})
        st["pending"] = {"saved": info["saved"], "target": info["target"]}
        board.boards.update(board.mac, storage=st)


def finish(board, info, app=None):
    """Record storage on/off after its write (or when no write was needed)."""
    if info["on"]:
        if info.get("balance_normal_a"):
            board.boards.update(board.mac, balance_normal_a=info["balance_normal_a"])
        board.boards.update(board.mac, storage={"on": True, "since": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                "saved": info["saved"]})
        board._event("storage", "storage mode on")
    else:
        board.boards.update(board.mac, storage=None)
        board._event("storage", "storage mode off: settings from before put back")
        if board.resting() and board.resting().get("reason") == "storage":
            board.resume("storage mode turned off")
    if app is not None:
        app._reevaluate(board)


def apply(board, on, via=None, reason=None, app=None):
    """Turn storage on/off without the confirmation step (API, all-boards actions already approved)."""
    prop, info = plan(board, on, reason, register=False)
    if prop:
        remember_before_write(board, info)
    res = safe_write.apply(board, prop, via) if prop else {"ok": True, "kind": "storage", "noop": True}
    finish(board, info, app)
    return res


def request_off_later(board):
    """Storage off for a board that's asleep or offline: done by the scheduler as soon as it's back.
    A resting board stops resting, so it reconnects the moment the BMS wakes."""
    st = dict(board.profile().get("storage") or {})
    if not st.get("on"):
        raise StorageError("storage mode is not on")
    st["off_pending"] = True
    board.boards.update(board.mac, storage=st)
    board._event("storage", "storage mode off requested; it's applied as soon as the board is connected")
    if board.resting():
        board.resume("storage mode off requested")

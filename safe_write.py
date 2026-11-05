"""The only code path that writes configuration (or a Lisp script) to a BMS.

Four kinds of proposal share one commit():
  prepare()         - Charge Start / End from the charge-limit control
  prepare_fields()  - any other whitelisted setting (config_fields.py)
  prepare_restore() - put back an automatic pre-write backup, only if every field it
                      changes is one of the above and passes the same checks
  prepare_addon()   - replace the stock Lisp script with an add-on (addons.py); see
                      _commit_script() for its own steps

Two phases, so nothing is sent without the user confirming:

prepare()  - fresh read of the device XML and config blob
           - require that re-encoding the decoded blob gives identical bytes
             (proves the layout parser fits this firmware exactly)
           - check hard limits and the 0.100 V start/end gap
           - build the new blob by patching ONLY the two 4-byte float32_auto
             slots of vc_charge_start and vc_charge_end
           - returns a proposal for the confirmation dialog; sends nothing

commit()   - only after the user clicked Confirm on that exact proposal
           - verifies it's still the same board (MAC) and that the config on
             the board hasn't changed since prepare()
           - saves a VESC Tool-compatible XML backup (+ raw .bin) of the
             current config
           - sends COMM_SET_CUSTOM_CONFIG, waits for the ACK
           - reads the config back and requires it to match the intended
             bytes exactly
           - logs everything to ~/.vbms_manager/changes.log
"""

import os
import re
import secrets
import threading
import time

import addons
import charge_limit
import config_fields
import script_check
import sessions
import store
from vesc.confparams import SECRET_FIELDS

PROPOSAL_TTL = 120.0
FIELDS = ("vc_charge_start", "vc_charge_end")
# Bump when the safety warning (web/js/wizard.js) changes enough that every board must accept it again.
RISK_VERSION = 4


class WriteRefused(Exception):
    pass


_proposals = {}
_plock = threading.Lock()


def _diff(a: bytes, b: bytes):
    return [i for i in range(len(a)) if a[i] != b[i]]


def risk_accepted(prof):
    ack = prof.get("risk_ack") or {}
    return ack.get("version") == RISK_VERSION


def preconditions(board):
    """Reasons writes are blocked for this board right now (empty list = allowed)."""
    reasons = []
    if not risk_accepted(board.profile()):
        reasons.append("the safety warning for this board has not been accepted (Board → Edit board setup)")
    if board.client is None or not board.client.connected:
        reasons.append("board is not connected")
    if board.config is None:
        reasons.append("config has not been read")
    elif not board.config.roundtrip_ok():
        reasons.append("config layout check failed (decoded config does not re-encode to identical bytes)")
    else:
        for name in FIELDS:
            fd = board.config.schema.fields.get(name)
            if fd is None or fd.type != 1 or fd.vtx != 9:
                reasons.append("%s is missing or has an unexpected encoding on this firmware" % name)
    sc = board.script
    if sc is None:
        reasons.append("running script has not been checked")
    elif not sc["verified"] and sc["sha256"] not in (board.profile().get("script_ack") or []):
        reasons.append("the running script has not been verified or acknowledged")
    v = board.values
    if v and v.get("charge_fault"):
        reasons.append("a charge over-current fault is latched (FLT_CHG_OC); disconnect the charger to clear it first")
    return reasons


def prepare(board, start, end, reason="", register=True):
    reasons = preconditions(board)
    if reasons:
        raise WriteRefused("; ".join(reasons))

    prof = board.profile()
    resolved = board.cell_profile()
    max_end = resolved[2] if resolved else None
    if max_end is None:
        raise WriteRefused("choose a cell type for this board first")

    with board.client._lock:
        fw = board.client.fw_version()
        schema = board.client.get_config_schema()
        cfg = schema.decode(board.client.get_config_blob())
    if fw["mac"] != board.mac:
        raise WriteRefused("the device at %s is not the board it was (MAC changed)" % board.host)
    if not cfg.roundtrip_ok():
        raise WriteRefused("config layout check failed on fresh read")

    try:
        charge_limit.check(start, end, max_end, cfg.values)
    except charge_limit.LimitError as e:
        raise WriteRefused(str(e))

    new_blob = cfg.patch_float32_auto({"vc_charge_start": start, "vc_charge_end": end})
    changed = _diff(cfg.blob, new_blob)
    allowed = set()
    for name in FIELDS:
        a, b = cfg.spans[name]
        allowed.update(range(a, b))
    if not set(changed) <= allowed:
        raise WriteRefused("internal check failed: patch touched bytes outside the two charge fields")
    check_cfg = schema.decode(new_blob)
    for name in schema.order:
        if name in FIELDS:
            continue
        if check_cfg.values[name] != cfg.values[name]:
            raise WriteRefused("internal check failed: %s would change" % name)
    new_vals = {n: check_cfg.values[n] for n in FIELDS}
    if (not charge_limit.gap_valid(new_vals["vc_charge_start"], new_vals["vc_charge_end"])
            or abs((new_vals["vc_charge_end"] - new_vals["vc_charge_start"]) - (end - start)) > charge_limit.GAP_TOL):
        raise WriteRefused("internal check failed: stored values would not keep the requested gap")

    pid = secrets.token_hex(8)
    prop = {
        "id": pid,
        "mac": board.mac,
        "host": board.host,
        "name": prof.get("nickname") or fw.get("hw_name"),
        "reason": reason,
        "old": {n: cfg.values[n] for n in FIELDS},
        "new": new_vals,
        "requested": {"vc_charge_start": start, "vc_charge_end": end},
        "kind": "charge",
        "rows": [{"name": n, "label": schema.fields[n].long_name, "group": "charging",
                  "old": "%.3f V" % cfg.values[n], "new": "%.3f V" % new_vals[n], "risks": []} for n in FIELDS],
        "confirm_name": None,
        "bytes_changed": len(changed),
        "blob_before": cfg.blob,
        "blob_after": new_blob,
        "created": time.time(),
    }
    note = charge_limit.balance_note(new_vals["vc_charge_start"], new_vals["vc_charge_end"], cfg.values.get("vc_balance_min"))
    if note:
        prop["rows"][0]["risks"].append({"level": "caution", "text": note})
    if not register:
        return prop
    with _plock:
        _proposals.clear()  # only one pending proposal at a time
        _proposals[pid] = prop
    return public(prop)


def apply_scheduled(board, start, end, reason):
    """Charge Start/End write for a schedule rule the user already approved.

    Same checks, backup, read-back and log as an interactive write; the user's
    approval happened when they saved the schedule (see schedule.py).
    """
    prop = prepare(board, start, end, reason, register=False)
    return _commit(board, prop)


def prepare_fields(board, changes, reason="", register=True):
    """Proposal for whitelisted non-charge settings. Sends nothing."""
    reasons = preconditions(board)
    if reasons:
        raise WriteRefused("; ".join(reasons))
    if not isinstance(changes, dict):
        raise WriteRefused("bad change list")
    prof = board.profile()

    with board.client._lock:
        fw = board.client.fw_version()
        schema = board.client.get_config_schema()
        cfg = schema.decode(board.client.get_config_blob())
    if fw["mac"] != board.mac:
        raise WriteRefused("the device at %s is not the board it was (MAC changed)" % board.host)
    if not cfg.roundtrip_ok():
        raise WriteRefused("config layout check failed on fresh read")

    try:
        stored, rows = config_fields.validate(schema, cfg.values, changes)
    except config_fields.FieldError as e:
        raise WriteRefused(str(e))
    if any(n in FIELDS for n in stored):
        raise WriteRefused("Charge Start / End only change through the charge limit control")

    new_blob = cfg.patch_fields(stored)
    check_cfg = schema.decode(new_blob)
    for name in schema.order:
        want = stored[name] if name in stored else cfg.values[name]
        if check_cfg.values[name] != want:
            raise WriteRefused("internal check failed: %s would not be stored as intended" % name)
    changed = _diff(cfg.blob, new_blob) if len(new_blob) == len(cfg.blob) else None
    if changed is not None and not any(schema.fields[n].type == 3 for n in stored):
        allowed = set()
        for name in stored:
            allowed.update(range(*cfg.spans[name]))
        if not set(changed) <= allowed:
            raise WriteRefused("internal check failed: patch touched bytes outside the changed fields")

    name = prof.get("nickname") or fw.get("hw_name")
    danger = any(r["level"] == "danger" for row in rows for r in row["risks"])
    pid = secrets.token_hex(8)
    prop = {
        "id": pid,
        "mac": board.mac,
        "host": board.host,
        "name": name,
        "reason": reason or "settings: " + ", ".join(r["label"] for r in rows),
        "kind": "fields",
        "rows": rows,
        "old": {n: _loggable(n, cfg.values[n]) for n in stored},
        "new": {n: _loggable(n, v) for n, v in stored.items()},
        "confirm_name": name if danger else None,
        "may_disconnect": any(config_fields.FIELDS[n]["group"] == "comms" for n in stored),
        "bytes_changed": len(changed) if changed is not None else None,
        "blob_before": cfg.blob,
        "blob_after": new_blob,
        "created": time.time(),
    }
    if not register:
        return prop
    with _plock:
        _proposals.clear()
        _proposals[pid] = prop
    return public(prop)


def read_balance_current(board):
    """Balance Max Current read fresh from the board (A)."""
    c = board.client
    if c is None:
        raise WriteRefused("board is not connected")
    with c._lock:
        cfg = c.get_config_schema().decode(c.get_config_blob())
    board.config = cfg
    return round(cfg.values["balance_max_current"], 3)


def prepare_balance(board, allow, register=True, reason=""):
    """Disallow balancing = Balance Max Current 0 A (the script only balances while the pack current is below
    it, so it never does). Allow = the value saved before disallowing (balance_normal_a), default 1.0 A."""
    cur = read_balance_current(board)
    if allow:
        amps = float(board.profile().get("balance_normal_a") or (cur if cur > 0 else 1.0))
    else:
        if cur > 0:
            board.boards.update(board.mac, balance_normal_a=cur)
        amps = 0.0
    if abs(cur - amps) < 0.0005:
        return None  # already there
    why = reason or ("allow balancing" if allow else "disallow balancing")
    return prepare_fields(board, {"balance_max_current": amps}, why, register=register)


def apply_balance(board, allow, reason):
    """Scheduled/timer balancing change the user already approved. None if the board already had it."""
    prop = prepare_balance(board, allow, register=False, reason=reason)
    return _commit(board, prop) if prop else None


_BACKUP_RE = re.compile(r"^[A-Za-z0-9_.-]+/\d{8}-\d{6}_before\.bin$")


def _backup_dirs(mac):
    root = os.path.join(store.home(), "backups")
    suffix = "_" + mac.replace(":", "")
    try:
        return [(d, os.path.join(root, d)) for d in sorted(os.listdir(root)) if d.endswith(suffix)]
    except OSError:
        return []


def list_backups(board):
    """The automatic pre-write backups of this board, newest first, with what restoring each would change."""
    out = []
    cfg = board.config
    for folder, full in _backup_dirs(board.mac):
        for f in os.listdir(full):
            if not f.endswith("_before.bin"):
                continue
            p = os.path.join(full, f)
            stamp = f[:15]
            item = {"id": folder + "/" + f, "time": "%s-%s-%s %s:%s:%s" % (stamp[:4], stamp[4:6], stamp[6:8], stamp[9:11],
                                                                           stamp[11:13], stamp[13:15]),
                    "size": os.path.getsize(p), "xml": os.path.exists(p[:-4] + ".xml")}
            if cfg is not None:
                try:
                    with open(p, "rb") as fh:
                        bcfg = cfg.schema.decode(fh.read())
                    diff = [n for n in cfg.schema.order if bcfg.values[n] != cfg.values[n]]
                    item["changes"] = [cfg.schema.fields[n].long_name for n in diff]
                    item["restorable"] = bool(diff) and all(n in config_fields.FIELDS or n in FIELDS for n in diff)
                except Exception:
                    item["changes"], item["restorable"] = None, False
            out.append(item)
    return sorted(out, key=lambda x: x["time"], reverse=True)


def prepare_restore(board, backup_id):
    """Proposal to write a saved backup back to the board. Sends nothing.

    Refused unless the backup has the same layout as the board's config now, and every field
    that would change is one VBMS Manager edits anyway; those go through the same limits as
    an interactive change. Anything else has to be restored in VESC Tool.
    """
    reasons = preconditions(board)
    if reasons:
        raise WriteRefused("; ".join(reasons))
    if not isinstance(backup_id, str) or not _BACKUP_RE.match(backup_id) or ".." in backup_id:
        raise WriteRefused("unknown backup")
    folder, fname = backup_id.split("/")
    dirs = dict(_backup_dirs(board.mac))
    if folder not in dirs:
        raise WriteRefused("that backup isn't from this board")
    try:
        with open(os.path.join(dirs[folder], fname), "rb") as f:
            blob = f.read()
    except OSError:
        raise WriteRefused("backup file not found")

    prof = board.profile()
    with board.client._lock:
        fw = board.client.fw_version()
        schema = board.client.get_config_schema()
        cfg = schema.decode(board.client.get_config_blob())
    if fw["mac"] != board.mac:
        raise WriteRefused("the device at %s is not the board it was (MAC changed)" % board.host)
    if not cfg.roundtrip_ok():
        raise WriteRefused("config layout check failed on fresh read")
    if len(blob) != len(cfg.blob):
        raise WriteRefused("the backup has a different config layout (firmware or script changed since), "
                           "so it can't be restored here")
    try:
        bcfg = schema.decode(blob)
    except Exception as e:
        raise WriteRefused("the backup doesn't decode with this firmware's config layout (%s)" % e)
    if not bcfg.roundtrip_ok() or bcfg.signature != cfg.signature:
        raise WriteRefused("the backup doesn't match this firmware's config layout")

    diff = [n for n in schema.order if bcfg.values[n] != cfg.values[n]]
    if not diff:
        raise WriteRefused("the board already has exactly these settings; nothing to restore")
    outside = [n for n in diff if n not in config_fields.FIELDS and n not in FIELDS]
    if outside:
        raise WriteRefused("this backup also changes settings VBMS Manager doesn't edit (%s). Restore it in VESC Tool "
                           "(Load XML on the BMS config page) instead." % ", ".join(schema.fields[n].long_name
                                                                                   for n in outside))
    rows = []
    if any(n in FIELDS for n in diff):
        resolved = board.cell_profile()
        if not resolved:
            raise WriteRefused("choose a cell type for this board first")
        try:
            charge_limit.check(bcfg.values["vc_charge_start"], bcfg.values["vc_charge_end"], resolved[2], bcfg.values)
        except charge_limit.LimitError as e:
            raise WriteRefused("the backup's charge settings fail today's limits: %s" % e)
        rows += [{"name": n, "label": schema.fields[n].long_name, "group": "charging",
                  "old": "%.3f V" % cfg.values[n], "new": "%.3f V" % bcfg.values[n], "risks": []}
                 for n in FIELDS if n in diff]
    other = {n: bcfg.values[n] for n in diff if n not in FIELDS}
    if other:
        try:
            stored, frows = config_fields.validate(schema, cfg.values, other)
        except config_fields.FieldError as e:
            raise WriteRefused("the backup has a value outside what VBMS Manager allows: %s" % e)
        if any(stored[n] != bcfg.values[n] for n in stored):
            raise WriteRefused("internal check failed: a backup value would not be stored as saved")
        rows += frows
    # Every field outside the diff must already match, so the bytes written are the backup itself.
    check = schema.decode(blob)
    for n in schema.order:
        if n not in diff and check.values[n] != cfg.values[n]:
            raise WriteRefused("internal check failed: %s would change" % n)

    name = prof.get("nickname") or fw.get("hw_name")
    pid = secrets.token_hex(8)
    prop = {
        "id": pid, "mac": board.mac, "host": board.host, "name": name,
        "reason": "restore backup from %s" % fname[:15], "kind": "restore", "rows": rows,
        "old": {n: _loggable(n, cfg.values[n]) for n in diff},
        "new": {n: _loggable(n, bcfg.values[n]) for n in diff},
        "confirm_name": name,  # putting back a whole config is always a deliberate act
        "may_disconnect": any(n in config_fields.FIELDS and config_fields.FIELDS[n]["group"] == "comms" for n in diff),
        "bytes_changed": len(_diff(cfg.blob, blob)), "backup_id": backup_id,
        "blob_before": cfg.blob, "blob_after": blob, "created": time.time(),
    }
    with _plock:
        _proposals.clear()
        _proposals[pid] = prop
    return public(prop)


def _loggable(name, val):
    if name in SECRET_FIELDS:
        return "(hidden)"
    return val


def public(prop):
    return {k: v for k, v in prop.items() if not k.startswith("blob_")}


def cancel(pid):
    with _plock:
        _proposals.pop(pid, None)


def commit(board, pid, confirm_text=None, skipped_confirm=False):
    with _plock:
        prop = _proposals.get(pid)
        if prop is not None and prop.get("confirm_name"):
            if str(confirm_text or "").strip().lower() != prop["confirm_name"].strip().lower():
                raise WriteRefused('type the board name "%s" to confirm this change' % prop["confirm_name"])
        prop = _proposals.pop(pid, None)
    if prop is None:
        raise WriteRefused("this change is no longer pending; prepare it again")
    if skipped_confirm and not prop.get("confirm_name") and prop.get("kind") != "restore":
        prop["skipped_confirm"] = True
    return _commit(board, prop)


def _commit(board, prop):
    if time.time() - prop["created"] > PROPOSAL_TTL:
        raise WriteRefused("confirmation expired; prepare the change again")
    if prop["mac"] != board.mac:
        raise WriteRefused("proposal is for a different board")
    reasons = preconditions(board)
    if reasons:
        raise WriteRefused("; ".join(reasons))
    if prop.get("kind") == "script":
        return _commit_script(board, prop)

    log = {"event": "config_write", "kind": prop.get("kind", "charge"), "mac": board.mac, "host": board.host,
           "name": prop["name"], "old": prop["old"], "new": prop["new"], "reason": prop["reason"],
           "rows": [{k: r[k] for k in ("label", "old", "new")} for r in prop.get("rows", [])]}
    if prop.get("skipped_confirm"):
        log["confirm"] = "skipped (Preferences: skip confirmations)"
    c = board.client
    with c._lock:
        fw = c.fw_version()
        if fw["mac"] != prop["mac"]:
            raise WriteRefused("the device at %s is not the board it was (MAC changed)" % board.host)
        schema = c.get_config_schema()
        current = c.get_config_blob()
        if current != prop["blob_before"]:
            raise WriteRefused("the config on the board changed since this was prepared; nothing was written")
        cfg = schema.decode(current)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = store.safe_name("%s_%s" % (prop["name"], board.mac.replace(":", "")))
        bpath = store.path("backups", folder, "%s_before.xml" % stamp)
        with open(bpath, "w", encoding="utf-8") as f:
            f.write(cfg.to_vesc_tool_xml(include_secrets=True))
        with open(bpath[:-4] + ".bin", "wb") as f:
            f.write(current)
        log["backup"] = bpath

        try:
            c.write_custom_config(prop["blob_after"])
        except Exception as e:
            log.update(result="error", error="write: %s" % e)
            store.append_log("changes", log)
            raise WriteRefused("write failed: %s. Backup saved at %s" % (e, bpath))

        try:
            readback = c.get_config_blob()
        except Exception as e:
            log.update(result="unverified", error="read-back: %s" % e)
            store.append_log("changes", log)
            if prop.get("may_disconnect"):
                raise WriteRefused("the write was sent, but the board stopped answering before it could be read "
                                   "back. It may have left the network. Backup: %s" % bpath)
            raise WriteRefused("write was sent but read-back failed (%s). Check the board in VESC Tool. "
                               "Backup: %s" % (e, bpath))

    if readback != prop["blob_after"]:
        log.update(result="mismatch", readback=readback.hex())
        store.append_log("changes", log)
        raise WriteRefused("read-back does not match what was written. Check the board in VESC Tool and "
                           "restore from the backup if needed: %s" % bpath)

    log["result"] = "ok"
    store.append_log("changes", log)
    board.config = schema.decode(readback)
    board._event("config", "%s: %s" % (prop["reason"], ", ".join("%s %s -> %s" % (r["label"], r["old"], r["new"])
                                                                  for r in prop.get("rows", []))))
    return {"ok": True, "kind": prop.get("kind", "charge"), "new": prop["new"], "rows": prop.get("rows", []),
            "backup": bpath}


# -- Lisp script add-ons ----------------------------------------------------------

UNDO = ("To undo it, re-install the BMS package from VESC Tool (VESC Packages page). "
        "VBMS Manager can't remove it.")


def _charger_reason(board):
    v = board.values
    if v is None:
        return "no live data from the board yet"
    if v["is_charging"] or v["v_charge"] >= sessions.CHARGER_V:
        return ("unplug the charger first: the BMS script is stopped while it's replaced, so nothing would "
                "be watching the charge")
    return None


def prepare_addon(board, addon_id):
    """Proposal to replace the running stock script with an add-on. Sends nothing."""
    reasons = preconditions(board)
    if reasons:
        raise WriteRefused("; ".join(reasons))
    if addon_id not in addons.ADDONS:
        raise WriteRefused("unknown add-on")
    why = _charger_reason(board)
    if why:
        raise WriteRefused(why)
    try:
        new_code = addons.code(addon_id)
    except (OSError, ValueError) as e:
        raise WriteRefused(str(e))

    prof = board.profile()
    with board.client._lock:
        fw = board.client.fw_version()
        raw = board.client.read_lisp_code()
    if fw["mac"] != board.mac:
        raise WriteRefused("the device at %s is not the board it was (MAC changed)" % board.host)
    info = dict(script_check.analyze(raw), fingerprint=addons.fingerprint(raw))
    board.script = info
    st = addons.status(addon_id, info)
    if st["state"] != "available":
        raise WriteRefused("%s can't be installed: %s" % (st["name"], st["why"]))
    new = addons.stored_form(new_code)
    new_info = script_check.analyze(new)
    if not new_info["verified"]:
        raise WriteRefused("internal check failed: the add-on's charge logic doesn't pass the script check")

    name = prof.get("nickname") or fw.get("hw_name")
    pid = secrets.token_hex(8)
    a = addons.ADDONS[addon_id]
    prop = {
        "id": pid, "mac": board.mac, "host": board.host, "name": name,
        "reason": "install add-on: %s" % a["name"], "kind": "script", "addon": addon_id,
        "rows": [{"name": "lisp", "label": "Lisp script", "group": "script",
                  "old": "%s · %d lines · %s…" % (st["base"], info["lines"], info["sha256"][:12]),
                  "new": "%s + %s · %d lines · %s…" % (st["base"], a["name"].lower(), new_info["lines"],
                                                       new_info["sha256"][:12]),
                  "risks": [{"level": "danger", "text": "Replaces the whole Lisp script that runs the BMS (charging, "
                             "balancing, sleep). The script is stopped for up to half a minute while it's written. "
                             + UNDO}]}],
        "old": {"script_sha256": info["sha256"]}, "new": {"script_sha256": new_info["sha256"]},
        "confirm_name": name,  # replacing the script is always a deliberate act
        "undo": UNDO,
        "blob_before": raw, "blob_after": new, "created": time.time(),
    }
    with _plock:
        _proposals.clear()
        _proposals[pid] = prop
    return public(prop)


def _put_back(c, stored, log):
    """Write the previous script back and start it. If that fails, try just starting whatever is in flash:
    the firmware only runs code with a valid CRC, and it only counts if it reads back as the previous script."""
    try:
        c.upload_lisp(stored)
        if c.lisp_set_running(True):
            return True
        log["restore_error"] = "the board didn't start the previous script"
    except Exception as e:
        log["restore_error"] = str(e)
    try:
        return c.lisp_set_running(True) and c.read_lisp_code() == stored
    except Exception:
        return False


def _commit_script(board, prop):
    """Upload an add-on script. Steps: same board, charger unplugged, script unchanged since prepare();
    save a copy of the old script; erase + write + read back (the script is stopped from the erase on);
    restart it. If anything fails after the erase, the old script is written back and restarted."""
    why = _charger_reason(board)
    if why:
        raise WriteRefused(why)
    log = {"event": "script_write", "kind": "script", "addon": prop["addon"], "mac": board.mac, "host": board.host,
           "name": prop["name"], "old": prop["old"], "new": prop["new"], "reason": prop["reason"]}
    c = board.client
    with c._lock:
        fw = c.fw_version()
        if fw["mac"] != prop["mac"]:
            raise WriteRefused("the device at %s is not the board it was (MAC changed)" % board.host)
        current = c.read_lisp_code()
        if current != prop["blob_before"]:
            raise WriteRefused("the script on the board changed since this was prepared; nothing was written")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = store.safe_name("%s_%s" % (prop["name"], board.mac.replace(":", "")))
        bpath = store.path("backups", folder, "%s_script_before.lisp" % stamp)
        with open(bpath, "wb") as f:
            f.write(current.rstrip(b"\x00"))
        log["backup"] = bpath

        try:
            c.upload_lisp(prop["blob_after"])
            if not c.lisp_set_running(True):
                raise WriteRefused("the board didn't start the new script")
        except Exception as e:
            log["error"] = str(e)
            restored = _put_back(c, current, log)
            log["result"] = "error_restored" if restored else "error_not_restored"
            store.append_log("changes", log)
            board._event("script", "add-on install failed (%s); %s" % (
                e, "the previous script was put back" if restored else "the previous script could NOT be put back"))
            if restored:
                raise WriteRefused("install failed (%s). The previous script was written back and restarted, so the "
                                   "board is as it was. Copy of it: %s" % (e, bpath))
            raise WriteRefused("install failed (%s) and the previous script couldn't be put back, so the BMS script "
                               "may not be running: charging, balancing and sleep aren't being managed. Keep the "
                               "charger unplugged and re-install the BMS package from VESC Tool now. Copy of the "
                               "previous script: %s" % (e, bpath))

        try:
            back = c.read_lisp_code()
        except Exception:
            back = prop["blob_after"]  # upload_lisp() already read it back identical
    info = dict(script_check.analyze(back), fingerprint=addons.fingerprint(back))
    board.script = info
    board.runtime_state = {}  # the script restarted, so live commands (pause charging, force balance) reset
    log["result"] = "ok"
    store.append_log("changes", log)
    board._event("script", "%s installed (script %s…, restarted). %s" % (
        addons.ADDONS[prop["addon"]]["name"], info["sha256"][:12], UNDO))
    return {"ok": True, "kind": "script", "rows": prop["rows"], "backup": bpath, "undo": UNDO,
            "new": prop["new"]}

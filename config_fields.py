"""BMS settings the UI may edit, the tool's own limits for them, and their risks.

Only fields listed here can be written through the generic settings path.
Charge Start / End are deliberately absent: they only change through the
charge-limit control (charge_limit.py), which keeps its own rules.

Limits: the tool's bounds below AND the firmware XML's min/max both apply.
Risks: {"op", "v", "level", "text"} rules evaluated against the NEW value of a
changed field. op: eq | ne | lt | gt | changed. level "danger" makes the write
dialog require typing the board's name. The same rules are sent to the UI so
it can warn while you type.

Semantics in the help texts come from the stock vbms32 script (how it calls
bms-get-param) and the firmware's own field descriptions.
"""

import math

from vesc.confparams import T_BOOL, T_DOUBLE, T_ENUM, T_INT, T_STRING

GROUPS = [
    ("balancing", "Balancing"),
    ("charging", "Charging"),
    ("sleep_soc", "Sleep & SOC"),
    ("protection", "Protection"),
    ("comms", "WiFi, Bluetooth & CAN"),
]

REBOOT = " Takes effect after the BMS restarts or next wakes from sleep."
OFF_NET = ("If this is wrong the board can't rejoin your WiFi, so it disappears from this tool and VESC Tool "
           "over WiFi. You'd have to fix it over USB or Bluetooth.")


def R(op, v, level, text):
    return {"op": op, "v": v, "level": level, "text": text}


FIELDS = {
    # ---- balancing
    "vc_balance_start": dict(group="balancing", min=0.003, max=0.100,
                             risks=[R("lt", 0.005, "caution", "Below 5 mV the balancer runs almost constantly and "
                                                               "keeps the BMS warm.")]),
    "vc_balance_end": dict(group="balancing", min=0.001, max=0.099),
    "vc_balance_min": dict(group="balancing", min=3.00, max=4.20),
    "max_bal_ch": dict(group="balancing", min=1, max=32,
                       risks=[R("gt", 10, "caution", "More cells bleeding at once means more heat in the BMS.")]),
    "balance_max_current": dict(group="balancing", min=0.0, max=100.0),
    "t_bal_max_cell": dict(group="balancing", min=0.0, max=60.0,
                           risks=[R("gt", 50, "caution", "Balancing adds heat. Letting it run with cells above "
                                                          "50 °C isn't recommended.")]),
    "t_bal_max_ic": dict(group="balancing", min=0.0, max=100.0,
                         risks=[R("gt", 80, "caution", "The bleed resistors heat the BMS board itself; above 80 °C "
                                                        "is getting hot.")]),
    # ---- charging
    "max_charge_current": dict(group="charging", min=0.5, max=100.0,
                               risks=[R("changed", None, "caution",
                                        "Charge current above this latches a charge fault (FLT_CHG_OC) and stops "
                                        "charging. Keep it above your charger's output and within what the pack, "
                                        "wiring and charge port can take.")]),
    "min_charge_current": dict(group="charging", min=0.0, max=10.0),
    "t_charge_max": dict(group="charging", min=0.0, max=60.0,
                         risks=[R("gt", 45, "caution", "Most Li-ion cells are rated for charging up to about 45 °C. "
                                                        "Check your cell datasheet.")]),
    "t_charge_min": dict(group="charging", min=-20.0, max=40.0,
                         risks=[R("lt", 0, "danger", "Charging Li-ion below 0 °C plates lithium on the anode. That "
                                                      "permanently damages cells and can make them unsafe.")]),
    "t_charge_max_mos": dict(group="charging", min=20.0, max=120.0,
                             risks=[R("gt", 90, "caution", "High MOSFET temperatures shorten the BMS's life.")]),
    "v_charge_detect": dict(group="charging", min=1.0, max=200.0,
                            risks=[R("changed", None, "caution", "If this is above your charger's voltage the BMS "
                                                                  "won't see the charger, and won't wake for it.")]),
    "vc_charge_min": dict(group="charging", min=1.50, max=3.50,
                          risks=[R("lt", 2.0, "caution", "Cells that have been below about 2 V may be internally "
                                                         "damaged. A lower limit lets the BMS charge them anyway.")]),
    # ---- sleep & SOC
    "block_sleep": dict(group="sleep_soc",
                        risks=[R("eq", 1, "danger", "The BMS stays awake all the time and slowly drains the pack. "
                                                    "Only use this while setting up or debugging, and turn it off "
                                                    "when you're done.")]),
    "sleep_regular": dict(group="sleep_soc", min=1.0, max=600.0,
                          risks=[R("lt", 5, "caution", "Waking this often uses more standby power.")]),
    "sleep_long": dict(group="sleep_soc", min=1.0, max=999.0),
    "min_current_sleep": dict(group="sleep_soc", min=0.05, max=5.0),
    "vc_empty": dict(group="sleep_soc", min=2.50, max=3.60),
    "vc_full": dict(group="sleep_soc", min=3.80, max=4.25),
    "batt_ah": dict(group="sleep_soc", min=0.1, max=999.0),
    "soc_use_ah": dict(group="sleep_soc"),
    "min_current_ah_wh_cnt": dict(group="sleep_soc", min=0.0, max=5.0),
    # ---- protection
    "psw_scd_en": dict(group="protection",
                       risks=[R("eq", 0, "danger", "Turns off hardware short-circuit protection. A short on the "
                                                   "output could then destroy the BMS, the wiring or the pack.")]),
    "psw_scd_tres": dict(group="protection",
                         risks=[R("changed", None, "caution", "A higher threshold lets more current through before "
                                                               "the hardware trips. Trip current = threshold / shunt "
                                                               "resistance (0.2 mΩ on VBMS32 V1).")]),
    "t_psw_en": dict(group="protection",
                     risks=[R("eq", 0, "danger", "Turns off over-temperature protection of the output switch.")]),
    "t_psw_max_mos": dict(group="protection", min=40.0, max=150.0,
                          risks=[R("gt", 110, "caution", "The output MOSFETs may be damaged before this trips.")]),
    "psw_t_pchg": dict(group="protection", min=0.1, max=10.0,
                       risks=[R("changed", None, "caution", "Precharge protects the output switch from the "
                                                             "controller's capacitor inrush.")]),
    "psw_wait_init": dict(group="protection"),
    # ---- comms
    "wifi_mode": dict(group="comms",
                      risks=[R("ne", 1, "danger", "Only Station mode keeps the board on your WiFi. " + OFF_NET + REBOOT)]),
    "wifi_sta_ssid": dict(group="comms", risks=[R("changed", None, "danger", OFF_NET + REBOOT)]),
    "wifi_sta_key": dict(group="comms", risks=[R("changed", None, "danger", OFF_NET + REBOOT)]),
    "use_tcp_local": dict(group="comms",
                          risks=[R("eq", 0, "danger", "Turns off the TCP server that this tool and VESC Tool use "
                                                      "over WiFi." + REBOOT)]),
    "wifi_ap_ssid": dict(group="comms"),
    "wifi_ap_key": dict(group="comms"),
    "use_tcp_hub": dict(group="comms",
                        risks=[R("eq", 1, "caution", "Connects the board to the internet TCP hub below. Anyone with "
                                                     "the hub ID and password can connect to it.")]),
    "tcp_hub_url": dict(group="comms"),
    "tcp_hub_port": dict(group="comms", min=1, max=65535),
    "tcp_hub_id": dict(group="comms"),
    "tcp_hub_pass": dict(group="comms"),
    "ble_mode": dict(group="comms",
                     risks=[R("eq", 0, "danger", "Turns Bluetooth off. If WiFi stops working you'd need USB to "
                                                 "reach the BMS." + REBOOT)]),
    "ble_name": dict(group="comms"),
    "ble_pin": dict(group="comms", min=0, max=999999),
    "controller_id": dict(group="comms", min=1, max=254,
                          risks=[R("changed", None, "danger", "Every device on the CAN bus needs a unique ID. A clash "
                                                              "with the motor controller breaks CAN communication.")]),
    "can_baud_rate": dict(group="comms",
                          risks=[R("changed", None, "danger", "Must match every other device on the CAN bus (motor "
                                                              "controller etc.), or CAN stops working.")]),
    "can_status_rate_hz": dict(group="comms", min=0, max=200),
}

# Shown in the UI as read-only, with the reason
READ_ONLY = {
    "vc_charge_start": "Set with the charge limit control.",
    "vc_charge_end": "Set with the charge limit control.",
    "cells_ic1": "Hardware wiring. Change in VESC Tool only if the pack itself changed.",
    "cells_ic2": "Hardware wiring. Change in VESC Tool only if the pack itself changed.",
    "temp_num": "Hardware wiring. Change in VESC Tool only if the sensors changed.",
}


def _str_len_ok(n_min, n_max):
    return lambda v: v == "" or n_min <= len(v.encode("utf-8")) <= n_max


# (fields involved, check(values) -> bool, message). Run only if one of the fields changes.
CROSS = [
    (("vc_balance_start", "vc_balance_end"), lambda v: v["vc_balance_end"] < v["vc_balance_start"],
     "Balance End must be smaller than Balance Start."),
    (("vc_charge_min",), lambda v: v["vc_charge_min"] < v["vc_charge_start"],
     "Charge Minimum must be below Charge Start."),
    (("t_charge_min", "t_charge_max"), lambda v: v["t_charge_min"] < v["t_charge_max"],
     "Charge Temp Min must be below Charge Temp Max."),
    (("min_charge_current", "max_charge_current"), lambda v: v["min_charge_current"] < v["max_charge_current"],
     "Min Charge Current must be below Max Charge Current."),
    (("vc_empty", "vc_full"), lambda v: v["vc_empty"] < v["vc_full"],
     "Cell Voltage Empty must be below Cell Voltage Full."),
    (("max_bal_ch",), lambda v: v["max_bal_ch"] <= v["cells_ic1"] + v["cells_ic2"],
     "Max Balance Channels can't be more than the number of cells."),
    (("wifi_mode", "wifi_sta_ssid"), lambda v: v["wifi_mode"] != 1 or v["wifi_sta_ssid"].strip() != "",
     "Station mode needs a network name (SSID)."),
    (("wifi_sta_key",), lambda v: _str_len_ok(8, 63)(v["wifi_sta_key"]),
     "A WiFi password is either empty (open network) or at least 8 characters."),
    (("wifi_ap_key",), lambda v: _str_len_ok(8, 63)(v["wifi_ap_key"]),
     "The access point password must be empty or at least 8 characters."),
    (("use_tcp_hub", "tcp_hub_id"), lambda v: not v["use_tcp_hub"] or len(v["tcp_hub_id"]) >= 3,
     "The TCP hub ID needs at least 3 characters."),
]


class FieldError(ValueError):
    pass


def risk_hits(rules, new, old):
    out = []
    for r in rules or []:
        op, v = r["op"], r["v"]
        hit = ((op == "changed" and new != old) or (op == "eq" and new == v) or (op == "ne" and new != v) or
               (op == "lt" and new < v) or (op == "gt" and new > v))
        if hit:
            out.append({"level": r["level"], "text": r["text"]})
    return out


def coerce(fd, raw):
    """Turn a JSON value from the UI into the field's Python type, or raise FieldError."""
    label = fd.long_name
    if fd.type == T_STRING:
        if not isinstance(raw, str):
            raise FieldError("%s must be text" % label)
        if "\x00" in raw or any(ord(c) < 32 for c in raw):
            raise FieldError("%s contains control characters" % label)
        if fd.max_len and len(raw.encode("utf-8")) > fd.max_len:
            raise FieldError("%s can be at most %d characters" % (label, fd.max_len))
        return raw
    if isinstance(raw, bool):
        raw = int(raw)
    if fd.type == T_DOUBLE:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise FieldError("%s must be a number" % label)
        if not math.isfinite(val):
            raise FieldError("%s must be a number" % label)
        return val
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise FieldError("%s must be a whole number" % label)
    if isinstance(raw, float) and raw != val:
        raise FieldError("%s must be a whole number" % label)
    if fd.type == T_BOOL and val not in (0, 1):
        raise FieldError("%s must be on or off" % label)
    if fd.type == T_ENUM and not (0 <= val < len(fd.enum_names)):
        raise FieldError("%s: unknown option" % label)
    return val


def check_bounds(fd, spec, val):
    if fd.type not in (T_DOUBLE, T_INT):
        return
    lo, hi = bounds(fd, spec)
    if lo is not None and val < lo - 1e-9:
        raise FieldError("%s must be at least %g%s" % (fd.long_name, lo, fd.suffix))
    if hi is not None and val > hi + 1e-9:
        raise FieldError("%s must be at most %g%s" % (fd.long_name, hi, fd.suffix))
    if fd.type == T_DOUBLE and fd.vtx in (7, 8):
        lim = 32767 if fd.vtx == 7 else 2147483647
        if abs(val * fd.scale) > lim:
            raise FieldError("%s is out of range for this firmware" % fd.long_name)


def bounds(fd, spec):
    def pick(a, b, f):
        xs = [x for x in (a, b) if x is not None]
        return f(xs) if xs else None
    return pick(spec.get("min"), fd.min, max), pick(spec.get("max"), fd.max, min)


def validate(schema, current: dict, changes: dict):
    """Check a set of changes against the fresh config values.

    Returns (stored_changes, rows). stored_changes holds the values exactly as
    the BMS will store them, minus no-op changes. Raises FieldError.
    """
    if not changes:
        raise FieldError("nothing to change")
    stored = {}
    for name, raw in changes.items():
        if name in READ_ONLY:
            raise FieldError("%s can't be changed here: %s" % (name, READ_ONLY[name]))
        spec = FIELDS.get(name)
        fd = schema.fields.get(name)
        if spec is None or fd is None:
            raise FieldError("%s is not an editable setting" % name)
        val = coerce(fd, raw)
        check_bounds(fd, spec, val)
        val = fd.stored(val)
        if val != current[name]:
            stored[name] = val
    if not stored:
        raise FieldError("those values are already set on the board")
    merged = dict(current)
    merged.update(stored)
    for names, ok, msg in CROSS:
        if any(n in stored for n in names) and all(n in merged for n in names) and not ok(merged):
            raise FieldError(msg)
    rows = []
    for name, val in stored.items():
        fd = schema.fields[name]
        rows.append({"name": name, "label": fd.long_name, "group": FIELDS[name]["group"],
                     "old": display(fd, current[name]), "new": display(fd, val),
                     "risks": risk_hits(FIELDS[name].get("risks"), val, current[name])})
    if "vc_balance_min" in stored:
        from charge_limit import balance_note
        note = balance_note(merged["vc_charge_start"], merged["vc_charge_end"], merged["vc_balance_min"])
        if note:
            next(r for r in rows if r["name"] == "vc_balance_min")["risks"].append({"level": "caution", "text": note})
    return stored, rows


def display(fd, val):
    from vesc.confparams import SECRET_FIELDS
    if fd.name in SECRET_FIELDS:
        return "(hidden)" if fd.type != T_STRING else ("(empty)" if val == "" else "(%d characters, hidden)" % len(val))
    if fd.type == T_BOOL:
        return "On" if val else "Off"
    if fd.type == T_ENUM:
        return fd.enum_names[val] if 0 <= val < len(fd.enum_names) else str(val)
    if fd.type == T_DOUBLE:
        return ("%.*f" % (min(fd.decimals, 4), val)) + fd.suffix
    if fd.type == T_INT:
        return str(val) + fd.suffix
    return val


def editable(cfg):
    """Description of every editable/read-only field for the UI, grouped."""
    from vesc.confparams import SECRET_FIELDS
    schema = cfg.schema
    groups = []
    for gid, title in GROUPS:
        items = []
        for name, spec in FIELDS.items():
            if spec["group"] != gid or name not in schema.fields:
                continue
            fd = schema.fields[name]
            lo, hi = bounds(fd, spec)
            val = cfg.values[name]
            item = {"name": name, "label": fd.long_name, "type": fd.type, "suffix": fd.suffix.strip(),
                    "help": fd.description, "min": lo, "max": hi, "risks": spec.get("risks") or [],
                    "secret": name in SECRET_FIELDS}
            if fd.type == T_DOUBLE:
                item["decimals"] = min(fd.decimals, 4)
                item["step"] = fd.step or 10 ** -min(fd.decimals, 4)
                item["value"] = round(val, min(fd.decimals, 4) + 2)
            elif fd.type == T_INT:
                item["step"] = fd.step or 1
                item["value"] = val
            elif fd.type == T_ENUM:
                item["options"] = fd.enum_names
                item["value"] = val
            elif fd.type == T_STRING:
                item["max_len"] = fd.max_len
                item["value"] = val
            else:
                item["value"] = val
            if item["secret"]:
                item["value"] = None
                item["is_set"] = bool(val) if fd.type == T_STRING else True
            items.append(item)
        groups.append({"id": gid, "title": title, "fields": items})
    ro = [{"name": n, "label": schema.fields[n].long_name, "value": cfg.values[n], "why": why,
           "suffix": schema.fields[n].suffix.strip()}
          for n, why in READ_ONLY.items() if n in schema.fields]
    return {"groups": groups, "read_only": ro}

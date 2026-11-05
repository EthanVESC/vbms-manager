"""Optional Lisp script add-ons that board setup can offer.

An add-on is a complete replacement script shipped in lisp/. It's only offered on a
board whose running script is the exact stock script the add-on was made from, compared
by fingerprint (comments and whitespace ignored), so it can never replace a script the
tool hasn't seen. Installing goes through safe_write.prepare_addon(). VBMS Manager can't
remove an add-on: you re-install the BMS package from VESC Tool to undo it.

charger_latch: the stock vbms32 "micro" script plus a sticky charger latch, written by
@fluffhead (the same sticky charge latch found in Flowstate VESC). Works around
a firmware bug: at the end of a charge the BMS sleeps, wakes straight back up, ignores
Charge Start and charges to Charge End, sleeps again, and so cycles on and off quickly.
That doesn't happen while the BMS stays awake, so every main-loop pass the script checks
for charger voltage (bms-get-vchg above 25 V) and blocks sleep until a few seconds after
it's gone. It doesn't fix the firmware bug itself.
"""

import hashlib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

ADDONS = {
    "charger_latch": {
        "name": "Charger latch",
        "credit": "@fluffhead",
        "same_as": "Flowstate VESC",
        # App Store page for the Flowstate VESC iOS app; the UI links it when set.
        "app_url": "https://apps.apple.com/us/app/flowstate-vesc/id6784881030",
        "file": "vbms32_charger_latch.lisp",
        "sha256": "e5e1ee2ed8ca8a4f29a7b7319a2b1566b22ad23b915c2593c06545058fb836c3",
        # fingerprint() of each stock script this add-on may replace
        "bases": {"4d94950cc42baf77fe16d0b4f5035bc514e5ad2d10d0c6f2c4a5ae46220aa917": "stock vbms32 (micro)"},
    },
}

_TOKEN = re.compile(r"[(){}]|[^\s(){}]+")


def fingerprint(code: bytes) -> str:
    """SHA-256 of the script's tokens, without comments, so two copies that differ only in comments or
    layout match. (Strings are split on spaces too; that's fine for comparing like with like.)"""
    text = code.rstrip(b"\x00").decode("utf-8", errors="replace")
    text = re.sub(r";[^\n]*", "", text)
    return hashlib.sha256(" ".join(_TOKEN.findall(text)).encode("utf-8")).hexdigest()


def code(addon_id) -> bytes:
    """The add-on's script, checked against its pinned SHA-256."""
    a = ADDONS[addon_id]
    with open(os.path.join(HERE, "lisp", a["file"]), "rb") as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest() != a["sha256"]:
        raise ValueError("the bundled %s script has been changed; reinstall VBMS Manager" % a["name"])
    return data


def stored_form(script: bytes) -> bytes:
    """The bytes COMM_LISP_READ_CODE returns once the script is uploaded: the code, a NUL, and an
    empty import table (u16 count 0)."""
    return script.rstrip(b"\x00") + b"\x00" + b"\x00\x00"


def status(addon_id, script_info):
    """state: installed | available | unavailable, plus why. script_info: the board's script_check.analyze()
    result with "fingerprint" added (Board.load_script)."""
    a = ADDONS[addon_id]
    out = {"id": addon_id, "name": a["name"], "credit": a["credit"], "same_as": a["same_as"],
           "app_url": a["app_url"] or None}
    if script_info is None:
        return dict(out, state="unavailable", why="the running script hasn't been read yet")
    if script_info["sha256"] == a["sha256"]:
        return dict(out, state="installed", why="this board is running it")
    fp = script_info.get("fingerprint")
    if fp in a["bases"]:
        return dict(out, state="available", base=a["bases"][fp])
    if script_info.get("sticky_charger_latch"):
        return dict(out, state="unavailable", why="this board already runs a different charger-latch script")
    return dict(out, state="unavailable",
                why="this board's script isn't the stock vbms32 script the add-on was made from (a different "
                    "package version or a modified script), so it can't be installed here")

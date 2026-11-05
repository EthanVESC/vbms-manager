"""Cell profiles and SOC curves (see profiles.json)."""

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")


def load():
    with open(_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["by_id"] = {c["id"]: c for c in data["cells"]}
    return data


def resolve(board_profile: dict, data=None):
    """Return (cell, curve_points, max_end, curve_info) for a board's saved profile, or None.

    board_profile keys: cell_id, custom_curve (optional list).
    100% always means the cell profile's max_end (4.21 V end / 4.11 V start for NMC).
    """
    data = data or load()
    cell = data["by_id"].get(board_profile.get("cell_id") or "")
    if cell is None:
        return None
    max_end = float(cell["max_end"])
    custom = board_profile.get("custom_curve")
    if custom:
        info = {"name": "Custom curve (entered by user)", "verified": True,
                "source": "Entered in VBMS Manager board setup", "custom": True}
        return cell, custom, max_end, info
    curve = data["curves"][cell["curve"]]
    info = {k: curve[k] for k in ("name", "verified", "source")}
    info["custom"] = False
    return cell, curve["points"], max_end, info

"""Parser for COMM_BMS_GET_VALUES replies (layout from vesc_express main/bms.c)."""

from .buffer import Reader


def parse_bms_values(payload: bytes) -> dict:
    """payload includes the leading packet id byte."""
    r = Reader(payload, 1)
    d = {
        "v_tot": r.f32(1e6),
        "v_charge": r.f32(1e6),
        "i_in": r.f32(1e6),
        "i_in_ic": r.f32(1e6),
        "ah_cnt": r.f32(1e3),
        "wh_cnt": r.f32(1e3),
    }
    n = r.u8()
    d["cells"] = [r.f16(1e3) for _ in range(n)]
    d["balancing"] = [r.u8() != 0 for _ in range(n)]
    nt = r.u8()
    d["temps_adc"] = [r.f16(1e2) for _ in range(nt)]
    d["temp_ic"] = r.f16(1e2)
    d["temp_hum"] = r.f16(1e2)
    d["humidity"] = r.f16(1e2)
    d["temp_max_cell"] = r.f16(1e2)
    d["soc"] = r.f16(1e3)
    d["soh"] = r.f16(1e3)
    d["can_id"] = r.u8()
    d["ah_chg_total"] = r.f32_auto()
    d["wh_chg_total"] = r.f32_auto()
    d["ah_dis_total"] = r.f32_auto()
    d["wh_dis_total"] = r.f32_auto()
    # Newer fields; older firmware may stop before these
    d["pressure"] = r.f16(1e-1) if r.remaining() >= 2 else None
    d["data_version"] = r.u8() if r.remaining() >= 1 else 0
    d["status"] = r.cstr() if r.remaining() >= 1 else ""

    cells = d["cells"]
    if cells:
        d["cell_min"] = min(cells)
        d["cell_max"] = max(cells)
        d["cell_delta_mv"] = round((d["cell_max"] - d["cell_min"]) * 1000.0, 1)
    else:
        d["cell_min"] = d["cell_max"] = d["cell_delta_mv"] = None

    d["temps"] = label_temps(d["temps_adc"], d["data_version"])
    status = d["status"]
    d["is_charging"] = "CHARGING" in status
    d["is_balancing"] = "BAL" in status.split(" | ") or any(d["balancing"])
    d["charge_fault"] = "FLT_CHG_OC" in status
    return d


def label_temps(temps, data_version):
    """With data_version 1 the vbms32 script fills temps_adc as:
    0 IC, 1 cell min, 2 cell max, 3 MOSFET, 4 ambient (-300 = unused), 5.. cell sensors."""
    out = []
    names = ["IC", "Cell min", "Cell max", "MOSFET", "Ambient"]
    for i, t in enumerate(temps):
        if t <= -299:
            continue
        if data_version == 1:
            name = names[i] if i < len(names) else "Cell sensor %d" % (i - 4)
        else:
            name = "Sensor %d" % (i + 1)
        out.append({"name": name, "value": t})
    return out

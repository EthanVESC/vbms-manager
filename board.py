"""A connected board: background polling, cached state, history and event logging."""

import collections
import threading
import time

import addons
import analysis
import ble
import cells
import charge_limit
import config_fields
import datalog
import safe_write
import script_check
import sessions
import store
from vesc.bms_values import parse_bms_values
from vesc.client import COMM_BMS_FORCE_BALANCE, COMM_BMS_SET_CHARGE_ALLOWED, BoardBusy, VescClient, VescError

POLL_S = 1.0
CONFIG_REFRESH_S = 60.0
RETRY_S = 10.0
REPLY_TIMEOUT_S = 3.0         # per request
LINK_GRACE_S = 15.0           # missed replies are retried on the same connection until this long without one
HISTORY_LEN = 3600            # 1 s samples kept in memory: the last hour at full resolution
ALERT_ON_S, ALERT_OFF_S = 5, 30  # an alert must hold this long to be logged as raised / cleared
ALERT_DEFAULTS = {"delta": 30, "temp": 55, "over": 30, "notify": False,
                  "charge_done": False, "charge_early": False, "offline_charging": False}
RUNTIME_CMDS = {"charge_allowed": COMM_BMS_SET_CHARGE_ALLOWED, "force_balance": COMM_BMS_FORCE_BALANCE}


class Board:
    def __init__(self, host, boards: store.BoardStore):
        self.host = host
        self.boards = boards
        self.client = None
        self.state = "connecting"     # connecting | online | busy | offline | paused | error
        self.error = ""
        self.fw = None
        self.mac = None
        self.config = None
        self.script = None
        self.values = None
        self.values_time = 0.0
        self.history = collections.deque(maxlen=HISTORY_LEN)
        self.events = collections.deque(maxlen=200)
        self._was_charging = None
        self._was_fault = False
        self._was_bal = None
        self._was_charger = None
        self._alerts = {}             # key -> {"on": bool, "n": consecutive samples disagreeing}
        self._state_logged = None     # last connection state written to the event log
        self.recorder = None
        self.tracker = sessions.ChargeTracker(self._charge_closed, self._charge_updated)
        self._last_charge = None      # newest closed session (from the log until one closes here)
        self.connect_count = 0
        self.missed_polls = 0         # value reads that timed out but were retried, this connection
        self.runtime_state = {}       # last live command sent per cmd: {"value", "time", "why"}
        # Link: "wifi" (TCP to host) or "ble" (through the Mac app). Only you switch it (set_link); a board added
        # from a Bluetooth search (host "ble:<id>") is always on Bluetooth.
        self.ble_only = ble.is_host(host)
        self._last_mac = next((m for m in boards.macs() if boards.get(m).get("last_host") == host), None)
        self.link = "ble" if self.ble_only or host in (boards.settings().get("ble_links") or {}) else "wifi"
        self._ble_id = None           # macOS peripheral id of the current Bluetooth link
        self._switching = False
        self.peers = lambda: []       # the other boards (set by the server), so one board isn't connected twice
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._run, name="board-%s" % host, daemon=True)

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._paused.set()
        self._close()
        self._offline()

    def pause(self):
        """Disconnect and stay disconnected so VESC Tool can use the board."""
        self._paused.set()
        self._close()
        self.state = "paused"
        self._note_state("freed", "disconnected so VESC Tool can connect")

    def resume(self):
        self._paused.clear()
        self.state = "connecting"
        if self.mac:
            self._event("reconnect", "reconnecting (asked by you)")

    def set_link(self, link):
        """Switch between WiFi and Bluetooth (asked by you; it never switches by itself)."""
        if link not in ("wifi", "ble"):
            raise ValueError("unknown link")
        if self.ble_only:
            raise ValueError("this board was added over Bluetooth; add its IP to use it over WiFi")
        if link == self.link:
            return
        links = dict(self.boards.settings().get("ble_links") or {})
        if link == "ble":
            if not ble.available():
                raise ValueError("Bluetooth is only available in the VBMS Manager Mac app")
            mac = self.mac or self._last_mac
            if not mac:
                raise ValueError("this board hasn't been identified yet; it needs to connect over WiFi once, "
                                 "or add it from a Bluetooth search")
            links[self.host] = mac
        else:
            links.pop(self.host, None)
        self.boards.update_settings(ble_links=links)
        self.link = link
        self._event("link", "switched to %s (asked by you)" % ("Bluetooth" if link == "ble" else "WiFi"))
        self._switching = self.state == "online"   # the dropped connection isn't worth an "offline" event
        self._close()
        self._wake.set()

    def _offline(self):
        """Connection ended: close an open charge session and write out the partial interval."""
        if self.tracker.cur is not None:
            self.tracker.disconnected(time.time())
        if self.recorder is not None:
            try:
                self.recorder.flush()
            except OSError:
                pass

    def _close(self):
        c = self.client
        if c is not None:
            c.close()

    def _run(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                self.state = "paused"
                time.sleep(0.5)
                continue
            try:
                self._connect_and_load()
                self._poll_loop()
            except BoardBusy as e:
                self.state, self.error = "busy", str(e)
                self._note_state("busy", "another app (probably VESC Tool) is connected to the board")
            except (VescError, OSError, ValueError) as e:
                if self._switching:   # set_link() dropped the connection on purpose
                    self._switching = False
                    self.state, self.error = "connecting", ""
                else:
                    if self.state == "online":
                        self._event("disconnected", str(e))
                    self.state = "offline" if isinstance(e, (VescError, OSError)) else "error"
                    self.error = str(e)
                    self._note_state(self.state, str(e))
            finally:
                self._close()
                self._offline()
            if self._paused.is_set():
                continue
            self._wake.wait(RETRY_S)
            self._wake.clear()

    def _connect_and_load(self):
        self.state, self.error = "connecting", ""
        if self.link == "ble":
            self.client = self._ble_client()
        else:
            host, port = self.host, 65102
            if self.host.count(":") == 1:
                host, port = self.host.split(":")[0], int(self.host.split(":")[1])
            self.client = VescClient(host, port, timeout=REPLY_TIMEOUT_S)
        try:
            fw = self.client.connect()
        except ble.BleError as e:
            if self.link == "ble" and not self.ble_only and "hasn't seen" in str(e):
                self.boards.update(self.mac or self._last_mac, ble_id=None)  # search again next time
            raise
        want = self.mac or self._last_mac
        if self.link == "ble" and not self.ble_only and fw["mac"] != want:
            # A WiFi board switched to Bluetooth must be the same board, never just one with the same name.
            self.boards.update(want, ble_id=None)
            raise VescError("the board found over Bluetooth is a different one (MAC %s, expected %s)" % (fw["mac"], want))
        for o in self.peers():
            if o is not self and o.mac == fw["mac"] and o.state == "online":
                raise VescError("this board is already connected here as %s" % o.label())
        self.fw = fw
        if self.mac and fw["mac"] != self.mac:
            self._event("identity", "different board at %s (was %s, now %s)" % (self.host, self.mac, fw["mac"]))
            self.config = self.script = self.values = None
            self.history.clear()
            self.tracker = sessions.ChargeTracker(self._charge_closed, self._charge_updated)
            self._last_charge = None
            self._was_charging = self._was_bal = self._was_charger = None
        self.mac = fw["mac"]
        if self.recorder is None or self.recorder.mac != self.mac:
            self.recorder = datalog.Recorder(self.mac, self.log_settings)
        prof = self.boards.update(self.mac, last_host=self.host,
                                  **({"ble_id": self._ble_id} if self.link == "ble" else {}))
        if not prof.get("first_seen"):
            self.boards.update(self.mac, first_seen=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.config = self.client.get_config_schema().decode(self.client.get_config_blob())
        raw = self.client.read_lisp_code()
        self.script = dict(script_check.analyze(raw), fingerprint=addons.fingerprint(raw))
        self._config_time = time.time()
        self.state = "online"
        self.connect_count += 1
        self.missed_polls = 0
        self.runtime_state = {}  # the BMS may have restarted; live commands reset with it
        self._event("connected", "%s %s (FW %s)" % (fw["hw_name"], self.mac, fw["version"]))
        self._state_logged = "online"

    def label(self):
        return "Bluetooth" if self.ble_only else self.host

    def _ble_client(self):
        if not ble.available():
            raise VescError("Bluetooth is only available in the VBMS Manager Mac app")
        if self.ble_only:
            pid = ble.id_of(self.host)
        else:
            mac = self.mac or self._last_mac
            pid = self.boards.get(mac).get("ble_id")
            if not pid:
                self.state = "searching"
                pid = self._find_ble(mac)
        self._ble_id = pid
        return ble.client(pid, timeout=REPLY_TIMEOUT_S)

    def _find_ble(self, mac):
        """Search Bluetooth for the board with this MAC. Remembers every known board it identifies."""
        names = [self.config.values.get("ble_name")] if self.config else []
        found = ble.search(names=[n for n in names if n])
        known = set(self.boards.macs())
        for e in found:
            if e.get("mac") in known:
                self.boards.update(e["mac"], ble_id=e["id"])
        hit = next((e for e in found if e.get("mac") == mac), None)
        if hit is None:
            raise VescError("not found over Bluetooth. It may be out of range or asleep, connected to VESC Tool "
                            "over Bluetooth, or have Bluetooth off (BLE Mode in its config)")
        return hit["id"]

    def _poll_loop(self):
        last_ok = time.time()
        while not self._stop.is_set() and not self._paused.is_set():
            t0 = time.time()
            try:
                v = parse_bms_values(self.client.get_bms_values_raw())
                self.values, self.values_time = v, t0
                last_ok = time.time()
                self._track(v, t0)
                if t0 - self._config_time > CONFIG_REFRESH_S:
                    self.config = self.config.schema.decode(self.client.get_config_blob())
                    self._config_time = t0
            except VescError:
                # The WiFi link can stall for a few seconds (seen on a 32s board while it charges). Retry on the
                # same connection; only a closed socket or LINK_GRACE_S of silence counts as offline.
                if not self.client.connected or time.time() - last_ok > LINK_GRACE_S:
                    raise
                self.missed_polls += 1
                continue
            self._stop.wait(max(0.05, POLL_S - (time.time() - t0)))

    # -- tracking -------------------------------------------------------------

    def _track(self, v, t):
        cfg = self.config.values if self.config else {}
        smp = datalog.sample(v, t, cfg)
        self.history.append(smp)
        if self.recorder is not None:
            try:
                self.recorder.add(smp)
            except OSError as e:
                self.error = "couldn't write the history log: %s" % e
        rt = self.runtime_state.get("charge_allowed")
        r = self.cell_profile()
        self.tracker.update(smp, {"end": cfg.get("vc_charge_end"), "start": cfg.get("vc_charge_start"),
                                  "paused": bool(rt and rt["value"] == 0), "fault": v["charge_fault"],
                                  "t_min": cfg.get("t_charge_min"), "t_max": cfg.get("t_charge_max"),
                                  "soc_pts": r[1] if r else None})

        ch = v["is_charging"]
        if self._was_charging is not None and ch != self._was_charging:
            self._event("charge_start" if ch else "charge_stop",
                        "max cell %.3f V, min %.3f V, current %.2f A (start %.3f / end %.3f)" % (
                            v["cell_max"], v["cell_min"], v["i_in_ic"],
                            cfg.get("vc_charge_start", 0), cfg.get("vc_charge_end", 0)),
                        cell_max=v["cell_max"], cell_min=v["cell_min"])
        if v["charge_fault"] and not self._was_fault:
            self._event("charge_fault", "charge over-current fault latched (FLT_CHG_OC), current %.2f A" % v["i_in_ic"])
        self._was_fault = v["charge_fault"]
        self._was_charging = ch

        bal = v["is_balancing"]
        if self._was_bal is not None and bal != self._was_bal:
            n = sum(1 for x in v["balancing"] if x)
            self._event("balance_start" if bal else "balance_stop",
                        ("%d cells bleeding, spread %.1f mV" % (n, v["cell_delta_mv"] or 0)) if bal
                        else "spread %.1f mV" % (v["cell_delta_mv"] or 0))
        self._was_bal = bal
        charger = v["v_charge"] >= sessions.CHARGER_V
        if self._was_charger is not None and charger != self._was_charger:
            self._event("charger_in" if charger else "charger_out",
                        ("charger connected, %.1f V" % v["v_charge"]) if charger else "charger disconnected")
        self._was_charger = charger
        self._check_alerts(v, cfg)

    def _check_alerts(self, v, cfg):
        a = alert_settings(self.boards.settings())
        temps = [x["value"] for x in v["temps"]]
        now = {
            "fault": (v["charge_fault"], "charge over-current fault latched (FLT_CHG_OC)"),
            "delta": (v["cell_delta_mv"] is not None and v["cell_delta_mv"] > a["delta"],
                      "cell imbalance %.1f mV (alert above %g mV)" % (v["cell_delta_mv"] or 0, a["delta"])),
            "temp": (bool(temps) and max(temps) > a["temp"],
                     "temperature %.1f °C (alert above %g °C)" % (max(temps) if temps else 0, a["temp"])),
            "over": (bool(v["is_charging"] and cfg.get("vc_charge_end") and
                          v["cell_max"] > cfg["vc_charge_end"] + a["over"] / 1000),
                     "charging with max cell %.3f V above Charge End %.3f V" % (v["cell_max"], cfg.get("vc_charge_end", 0))),
        }
        for k, (on, text) in now.items():
            st = self._alerts.setdefault(k, {"on": False, "n": 0})
            if on == st["on"]:
                st["n"] = 0
                continue
            st["n"] += 1
            if st["n"] >= (ALERT_ON_S if on else ALERT_OFF_S):
                st.update(on=on, n=0)
                self._event("alert" if on else "alert_clear", text if on else "cleared: " + text, alert=k)

    def _note_state(self, kind, text):
        if self._state_logged != kind:
            self._state_logged = kind
            self._event(kind, text)

    def log_settings(self):
        return datalog.settings_from(self.boards.settings())

    # -- charge sessions

    def _charge_closed(self, rec):
        rec = dict(rec, mac=self.mac, host=self.host, name=self.profile().get("nickname"))
        self._last_charge = rec
        store.append_log("charges", rec)
        mins = rec["duration_s"] / 60
        self._event("charge_session", "%s: %dh %02dm, SOC %.0f%% -> %.0f%%, +%.2f Ah / +%.0f Wh, peak %.0f W" % (
            rec["reason_text"], mins // 60, mins % 60, rec["soc0"] * 100, rec["soc1"] * 100, rec["ah"], rec["wh"],
            rec["p_peak"]), id=rec["id"], reason=rec["reason"])

    def _charge_updated(self, upd):
        store.append_log("charges", dict(upd, mac=self.mac))
        if self._last_charge and self._last_charge.get("id") == upd["id"]:
            self._last_charge.update(rest_after=upd["rest_after"], capacity_ah=upd["capacity_ah"])
        if upd.get("capacity_ah"):
            self._event("capacity", "capacity estimate from the last charge: %.2f Ah" % upd["capacity_ah"])

    def last_charge(self):
        if self._last_charge is None and self.mac:
            got = analysis.charges(self.mac, 1)
            self._last_charge = got[0] if got else {}
        return self._last_charge or None

    def send_runtime(self, cmd, value, why, quiet=False):
        """RAM-only live command (no flash write). quiet: don't log repeats."""
        val = 1 if value else 0
        if self.client is None:
            raise VescError("not connected")
        self.client.send_runtime_command(RUNTIME_CMDS[cmd], val)
        self.runtime_state[cmd] = {"value": val, "time": time.strftime("%H:%M:%S"), "t": time.time(), "why": why}
        if not quiet:
            store.append_log("changes", {"event": "runtime", "cmd": cmd, "value": val, "why": why,
                                         "mac": self.mac, "host": self.host, "name": self.profile().get("nickname")})
            self._event("runtime", "%s = %d (%s; until the BMS restarts)" % (cmd, val, why))

    def read_charge_fresh(self):
        """Read the config from the board now (not the cached copy); return its Charge Start/End."""
        c = self.client
        if c is None:
            raise VescError("not connected")
        with c._lock:
            cfg = c.get_config_schema().decode(c.get_config_blob())
        self.config, self._config_time = cfg, time.time()
        return {"start": round(cfg.values["vc_charge_start"], 4), "end": round(cfg.values["vc_charge_end"], 4)}

    def _event(self, kind, text, **extra):
        e = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "text": text}
        self.events.append(e)
        rec = dict(e, mac=self.mac, host=self.host, name=self.profile().get("nickname"), **extra)
        store.append_log("events", rec)

    # -- profile helpers ----------------------------------------------------

    def profile(self):
        return self.boards.get(self.mac) if self.mac else {}

    def cell_profile(self):
        return cells.resolve(self.profile())

    def curve_accepted(self):
        r = self.cell_profile()
        if not r:
            return False
        _, pts, max_end, _ = r
        return self.profile().get("curve_accepted") == _curve_key(pts, max_end)

    def accept_curve(self):
        r = self.cell_profile()
        if not r:
            raise ValueError("choose a cell type first")
        _, pts, max_end, _ = r
        pts = charge_limit.validate_curve(pts, max_end)
        upd = {"curve_accepted": _curve_key(pts, max_end)}
        # A schedule or timer approved under an older version of this curve still carries its old
        # voltages: turn the schedule off for review and drop those timers, as a cell change does.
        end = lambda pct: round(max_end if pct >= charge_limit.PCT_MAX else charge_limit.interp(pts, pct), 3)
        old = lambda v: v and v.get("volts") and abs(v["volts"]["end"] - end(v["pct"])) > 0.0005
        prof = self.profile()
        sched = prof.get("schedule")
        if sched and any(old(r) for r in sched.get("items", []) if r.get("action") == "limit"):
            upd["schedule"] = dict(sched, enabled=False, stale=True)
        timers = prof.get("timers") or []
        if any(old(t["actions"].get("limit")) for t in timers):
            upd["timers"] = [t for t in timers if not old(t["actions"].get("limit"))]
        self.boards.update(self.mac, **upd)

    # -- snapshot for the UI ------------------------------------------------

    def snapshot(self, detail=False):
        prof = self.profile()
        out = {"host": self.host, "state": self.state, "error": self.error, "mac": self.mac,
               "link": self.link, "ble_only": self.ble_only, "ble": ble.available(),
               "link_known": bool(self.mac or self._last_mac),
               "fw": self.fw, "nickname": prof.get("nickname"), "values": self.values,
               "setup_done": setup_done(prof), "runtime": self.runtime_state,
               "risk_ok": safe_write.risk_accepted(prof), "risk_ack_time": (prof.get("risk_ack") or {}).get("time"),
               "values_age": round(time.time() - self.values_time, 1) if self.values_time else None,
               "missed_polls": self.missed_polls,
               "events": list(self.events)[-20:], "charging_now": self.tracker.live(),
               "pack_ah": prof.get("pack_ah")}
        lc = self.last_charge()
        if lc:
            out["last_charge"] = {k: lc.get(k) for k in ("id", "start", "end", "duration_s", "soc0", "soc1", "ah",
                                                          "wh", "p_peak", "reason", "reason_text", "capacity_ah",
                                                          "joined_late")}
        cfg = self.config
        if cfg:
            vals = cfg.values
            out["charge"] = {k: round(vals[k], 4) for k in ("vc_charge_start", "vc_charge_end", "vc_charge_min",
                                                             "vc_balance_min", "vc_balance_start", "vc_balance_end",
                                                             "vc_full", "vc_empty", "max_charge_current",
                                                             "min_charge_current", "t_charge_max", "t_charge_min",
                                                             "t_charge_max_mos") if k in vals}
            out["charge"]["gap_ok"] = charge_limit.gap_ok(vals["vc_charge_start"], vals["vc_charge_end"])
            out["charge"]["gap_valid"] = charge_limit.gap_valid(vals["vc_charge_start"], vals["vc_charge_end"])
            out["cells_configured"] = int(vals.get("cells_ic1", 0)) + int(vals.get("cells_ic2", 0))
            out["layout_ok"] = cfg.roundtrip_ok()
            out["limits_cfg"] = {k: round(vals[k], 4) for k in ("t_charge_max", "t_charge_min", "t_charge_max_mos",
                                                                 "t_bal_max_cell", "t_bal_max_ic", "t_psw_max_mos",
                                                                 "vc_empty", "vc_full", "vc_balance_start",
                                                                 "vc_balance_end", "vc_balance_min", "max_bal_ch",
                                                                 "balance_max_current")
                                 if k in vals}
        r = self.cell_profile()
        if r:
            cell, pts, max_end, info = r
            out["cell"] = {"id": cell["id"], "name": cell["name"], "max_end": max_end, "curve": pts,
                           "curve_info": info, "accepted": self.curve_accepted()}
            if cfg:
                out["cell"]["current_pct"] = charge_limit.pct_for_end(cfg.values["vc_charge_end"], pts, max_end)
        sc = self.script
        if sc:
            acked = sc["sha256"] in (prof.get("script_ack") or [])
            out["script"] = {k: sc[k] for k in ("sha256", "lines", "verified", "problems", "hysteresis_found",
                                                "wake_path_uses_end", "sticky_charger_latch", "unexpected")}
            out["script"]["acknowledged"] = acked
            if detail:
                out["script"]["excerpts"] = sc["excerpts"]
                out["script"]["references"] = sc["references"]
        out["addons"] = [addons.status(a, sc) for a in addons.ADDONS] if self.mac else []
        out["write_blockers"] = safe_write.preconditions(self) if self.state == "online" else ["board is not online"]
        out["schedule"] = prof.get("schedule")
        if detail:
            if cfg:
                out["config"] = cfg.public_fields()
                out["editable"] = config_fields.editable(cfg)
            out["events"] = list(self.events)[-60:]
        return out


def setup_done(prof):
    """Boards set up before the wizard existed (cell type + accepted curve) count as done."""
    if "setup_done" in prof:
        return bool(prof["setup_done"])
    return bool(prof.get("cell_id") and prof.get("curve_accepted"))


def alert_settings(all_settings):
    a = dict(ALERT_DEFAULTS)
    a.update({k: v for k, v in (all_settings.get("alerts") or {}).items() if k in ALERT_DEFAULTS})
    return a


def _curve_key(pts, max_end):
    return "%s|%.3f" % (",".join("%g:%.3f" % (float(p), float(v)) for p, v in pts), max_end)


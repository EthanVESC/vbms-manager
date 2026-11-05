"""Local web server: JSON API + static UI.

Binds to 127.0.0.1 by default; --lan binds every interface. Who may do what
is decided per request in access.py:

  local / paired : everything
  viewer         : read-only GETs; may POST /api/pair only
  reject         : nothing (Host header isn't ours: DNS rebinding)

Every POST must also carry the per-run token in an X-VBMS-Token header (a
custom header, so other websites can't send it without a CORS preflight we
never approve).
"""

import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import access
import analysis
import api as api_mod
import ble
import board as board_mod
import cells
import charge_limit
import datalog
import docs_page
import safe_write
import bulk
import safety
import schedule
import storage
import store
from board import Board
from discovery import Discovery

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class App:
    def __init__(self, start_discovery=True, start_scheduler=True):
        self.token = secrets.token_urlsafe(24)
        self.lan = False
        self.port = 8642
        self.store = store.BoardStore()
        self.pairing = access.Pairing(self.store)
        self.keys = access.ApiKeys(self.store)
        self.version = "dev"
        self.httpd = None
        self.internet_seen = None     # last time a client from outside your networks reached the server
        self.cells = cells.load()
        self.boards = {}
        self.lock = threading.Lock()
        self._disk = (0.0, 0)  # (time, bytes): history disk use, refreshed once a minute
        self.discovery = Discovery(known=self._known, names=lambda mac: self.store.get(mac).get("nickname"))
        self.ble_found = []           # last Bluetooth search (started from the UI only: probing holds each board's slot)
        self.ble_status = {"searching": False, "error": "", "last_scan": None}
        if start_discovery:
            self.discovery.start()
        for mac in self.store.macs():
            sched = schedule.migrate(self.store.get(mac).get("schedule"))
            if sched is not None:
                self.store.update(mac, schedule=sched)
        self.scheduler = schedule.Scheduler(self)
        self.api = api_mod.Api(self)
        if start_scheduler:
            self.scheduler.start()
        for h in self.store.hosts():
            self._start_board(h)

    def _known(self):
        with self.lock:
            boards = list(self.boards.values())
        return {b.host for b in boards}, {b.mac for b in boards if b.mac}

    def _start_board(self, host):
        with self.lock:
            if host in self.boards:
                return self.boards[host]
            b = Board(host, self.store)
            b.peers = lambda: list(self.boards.values())
            self.boards[host] = b
        b.start()
        return b

    def board(self, host) -> Board:
        b = self.boards.get(host)
        if b is None:
            raise ApiError("unknown board %s" % host)
        return b

    def snapshot(self, b, detail=False):
        out = b.snapshot(detail=detail)
        out["schedule_status"] = self.scheduler.status(b) if b.mac else None
        return out

    # -- API handlers ---------------------------------------------------------

    def state(self, role="local"):
        cells_pub = {"curves": self.cells["curves"], "cells": self.cells["cells"], "note": self.cells["_note"]}
        acc = {"role": role, "lan": self.lan, "can_write": role in ("local", "paired")}
        if role == "local":
            acc["pair_code"] = self.pairing.code
            acc["devices"] = self.pairing.devices()
            acc["urls"] = ["http://%s:%d/" % (ip, self.port)
                           for ip in access.lan_ips()] if self.lan else []
            acc["api_keys"] = self.keys.list()
            acc["internet_seen"] = self.internet_seen
        return {"boards": [self.snapshot(b) for b in self.boards.values()],
                "discovered": self.discovery.list(),
                "discovery": self.discovery.status(),
                "discovery_error": self.discovery.error,
                "ble": self._ble_state(),
                "cells": cells_pub,
                "limits": {"pct_min": charge_limit.PCT_MIN, "pct_max": charge_limit.PCT_MAX,
                           "gap": charge_limit.GAP, "gap_min": charge_limit.GAP_MIN,
                           "gap_max": charge_limit.GAP_MAX},
                "settings": self._public_settings(),
                "recording": {"bytes": self._disk_bytes(), "levels": datalog.LEVELS, "intervals": datalog.INTERVALS,
                              "retentions": datalog.RETENTIONS},
                "access": acc}

    def _public_settings(self):
        st = {k: v for k, v in self.store.settings().items() if k not in ("paired_devices", "api_keys")}
        st["safety"] = safety.settings_from(st)
        st["api"] = st.get("api") or {"on": False}
        st["internet_ui"] = bool(st.get("internet_ui"))
        st["logging"] = datalog.settings_from(st)
        st["alerts"] = board_mod.alert_settings(st)
        st["alerts_saved"] = "alerts" in self.store.settings()
        return st

    def _disk_bytes(self):
        t, n = self._disk
        if time.time() - t > 60:
            n = datalog.disk_usage()
            self._disk = (time.time(), n)
        return n

    def add_host(self, body):
        host = str(body.get("host", "")).strip()
        if ble.is_host(host):
            try:
                host = ble.host_for(ble.id_of(host))
            except ble.BleError as e:
                raise ApiError(str(e))
            e = next((d for d in self.ble_found if d["host"] == host), None)
            other = self._mac_host(e and e.get("mac"))
            if other:
                raise ApiError("that board is already added (%s). Use Board → Connect over Bluetooth on it instead."
                               % other)
        elif not host or any(c in host for c in "/ \\"):
            raise ApiError("enter an IP address or hostname")
        self.store.add_host(host)
        self._start_board(host)
        return {"ok": True}

    def scan(self, body):
        self.discovery.scan_now()
        return {"ok": True}

    # -- Bluetooth (Mac app only)

    def _mac_host(self, mac):
        with self.lock:
            return next((b.label() for b in self.boards.values() if mac and b.mac == mac), None)

    def _ble_state(self):
        st = dict(self.ble_status, available=ble.available())
        if st["last_scan"]:
            st["last_scan_age"] = round(time.time() - st["last_scan"])
        with self.lock:
            macs = {b.mac for b in self.boards.values() if b.mac}
            hosts = set(self.boards)
        st["found"] = [dict(d, nickname=self.store.get(d["mac"]).get("nickname") if d.get("mac") else None)
                       for d in self.ble_found if d["host"] not in hosts and d.get("mac") not in macs]
        return st

    def ble_scan(self, body):
        if not ble.available():
            raise ApiError("Bluetooth is only available in the VBMS Manager Mac app")
        if self.ble_status["searching"]:
            return {"ok": True}
        self.ble_status.update(searching=True, error="")

        def run():
            with self.lock:
                boards = list(self.boards.values())
            names = {b.config.values.get("ble_name") for b in boards if b.config} - {None, ""}
            # Boards already on Bluetooth here can't be probed (one connection each); they're known anyway.
            skip = {b._ble_id for b in boards if b.link == "ble" and b.state == "online" and b._ble_id}
            try:
                found = ble.search(names=sorted(names), skip=skip)
                known = set(self.store.macs())
                for e in found:
                    if e.get("mac") in known:
                        self.store.update(e["mac"], ble_id=e["id"])
                self.ble_found = found
                self.ble_status["error"] = ""
            except ble.BleError as e:
                self.ble_status["error"] = str(e)
            except Exception as e:  # never leave the search stuck on "searching"
                self.ble_status["error"] = "Bluetooth search failed (%s: %s)" % (type(e).__name__, e)
            finally:
                self.ble_status.update(searching=False, last_scan=time.time())
        threading.Thread(target=run, name="ble-search", daemon=True).start()
        return {"ok": True}

    def set_link(self, body):
        b = self.board(body.get("host"))
        try:
            b.set_link(body.get("link"))
        except ValueError as e:
            raise ApiError(str(e))
        return {"ok": True}

    def remove_host(self, body):
        host = body.get("host")
        with self.lock:
            b = self.boards.pop(host, None)
        if b:
            b.stop()
        self.store.remove_host(host)
        links = self.store.settings().get("ble_links") or {}
        if host in links:
            self.store.update_settings(ble_links={h: m for h, m in links.items() if h != host})
        return {"ok": True}

    def set_profile(self, body):
        b = self._online_mac(body)
        upd = {}
        if "nickname" in body:
            upd["nickname"] = (str(body["nickname"]).strip()[:40]) or None
        if "cell_id" in body:
            cid = body["cell_id"] or None
            if cid is not None and cid not in self.cells["by_id"]:
                raise ApiError("unknown cell type")
            upd["cell_id"] = cid
            upd["curve_accepted"] = None
        if "custom_curve" in body:
            cc = body["custom_curve"] or None
            if cc is not None:
                cell = self.cells["by_id"].get(body.get("cell_id") or b.profile().get("cell_id"))
                if not cell:
                    raise ApiError("choose a cell type first")
                try:
                    pts = charge_limit.validate_curve(cc, cell["max_end"])
                except charge_limit.LimitError as e:
                    raise ApiError(str(e))
                cc = [[p, round(v, 3)] for p, v in pts]
            upd["custom_curve"] = cc
        if "setup_done" in body:
            upd["setup_done"] = bool(body["setup_done"])
        if ("cell_id" in upd or "custom_curve" in upd) and b.profile().get("schedule"):
            # The schedule's approved voltages came from the old curve: make the user review it again.
            upd["schedule"] = dict(b.profile()["schedule"], enabled=False, stale=True)
        if ("cell_id" in upd or "custom_curve" in upd) and b.profile().get("timers"):
            # Limit timers carry voltages from the old curve: drop them.
            upd["timers"] = [t for t in b.profile()["timers"] if "limit" not in t["actions"]]
        self.store.update(b.mac, **upd)
        return self.snapshot(b, detail=True)

    def accept_curve(self, body):
        b = self._online_mac(body)
        try:
            b.accept_curve()
        except (ValueError, charge_limit.LimitError) as e:
            raise ApiError(str(e))
        return self.snapshot(b, detail=True)

    def ack_script(self, body):
        b = self._online_mac(body)
        sha = body.get("sha256")
        if not b.script or b.script["sha256"] != sha:
            raise ApiError("script changed; review it again")
        acks = list(b.profile().get("script_ack") or [])
        if sha not in acks:
            acks.append(sha)
        self.store.update(b.mac, script_ack=acks)
        return self.snapshot(b, detail=True)

    def ack_risk(self, body):
        b = self._online_mac(body)
        if body.get("accept") is not True:
            raise ApiError("not accepted")
        self.store.update(b.mac, risk_ack={"version": safe_write.RISK_VERSION, "time": time.strftime("%Y-%m-%d %H:%M")})
        b._event("setup", "safety warning accepted")
        return self.snapshot(b, detail=True)

    def _risk_ok(self, b):
        if not safe_write.risk_accepted(b.profile()):
            raise ApiError("accept the safety warning for this board first (Board → Edit board setup)")

    def prepare(self, body):
        b = self._online_mac(body)
        start, end, reason = self.charge_target(b, body)
        try:
            return safe_write.prepare(b, start, end, reason)
        except safe_write.WriteRefused as e:
            raise ApiError(str(e))

    def charge_target(self, b, body):
        """(start, end, reason) for a charge-limit request: {pct}, {end[, start]} or {fix_gap}."""
        if storage.is_on(b.profile()):
            raise ApiError("storage mode is on for this board; turn it off first (it holds the charge limit)")
        vals = b.config.values
        if body.get("fix_gap"):
            end = round(vals["vc_charge_end"], 3)
            start = round(end - charge_limit.GAP, 3)
            reason = "fix gap: start = end - 0.100 V"
        elif body.get("end") is not None:
            # Set directly on the graph: start/end as given (gap 0.050-0.200 V), checked like any other pair.
            try:
                end = round(float(body["end"]), 3)
                start = round(float(body["start"]), 3) if body.get("start") is not None \
                    else round(end - charge_limit.GAP, 3)
            except (TypeError, ValueError):
                raise ApiError("charge start/end must be numbers")
            self._curve(b)
            reason = "charge end %.3f / start %.3f V (set on the graph)" % (end, start)
        else:
            _, pts, max_end = self._curve(b)
            try:
                pct = int(body.get("pct"))
            except (TypeError, ValueError):
                raise ApiError("percentage must be a whole number")
            try:
                res = charge_limit.compute(pct, pts, max_end, vals)
            except charge_limit.LimitError as e:
                raise ApiError(str(e))
            start, end = res["start"], res["end"]
            reason = "charge limit %d%%" % pct
        return start, end, reason

    def _curve(self, b):
        r = b.cell_profile()
        if not r:
            raise ApiError("choose a cell type for this board first")
        if not b.curve_accepted():
            raise ApiError("review and accept the SOC curve for this board first")
        cell, pts, max_end, _ = r
        return cell, pts, max_end

    def prepare_fields(self, body):
        b = self._online_mac(body)
        try:
            return safe_write.prepare_fields(b, body.get("changes"))
        except safe_write.WriteRefused as e:
            raise ApiError(str(e))

    def commit(self, body):
        b = self._online_mac(body)
        try:
            res = safe_write.commit(b, body.get("id"), body.get("confirm_text"),
                                    skipped_confirm=body.get("skipped_confirm") is True and self._skip_on())
        except safe_write.WriteRefused as e:
            raise ApiError(str(e))
        if res.get("storage"):
            storage.finish(b, res["storage"], self)
        return res

    def addon_prepare(self, body):
        b = self._online_mac(body)
        try:
            return safe_write.prepare_addon(b, body.get("addon"))
        except safe_write.WriteRefused as e:
            raise ApiError(str(e))

    def cancel(self, body):
        safe_write.cancel(body.get("id"))
        return {"ok": True}

    def runtime(self, body):
        b = self._online_mac(body)
        self._risk_ok(b)
        if body.get("confirm") is not True:
            raise ApiError("not confirmed")
        if body.get("cmd") not in ("charge_allowed", "force_balance"):
            raise ApiError("unknown command")
        val = 1 if body.get("value") else 0
        why = "by you"
        if body["cmd"] == "charge_allowed" and self.scheduler.override(b, "charge", val):
            why = "by you, overriding the schedule until its next change"
        if body.get("skipped_confirm") is True and self._skip_on():
            why += " (no confirmation: skip confirmations is on)"
        b.send_runtime(body["cmd"], val, why)
        return {"ok": True}

    def balance_prepare(self, body):
        """Allow / disallow balancing (Balance Max Current, a flash write) from the toggle."""
        b = self._online_mac(body)
        self._risk_ok(b)
        allow = body.get("allow") is True
        try:
            prop = safe_write.prepare_balance(b, allow)
        except safe_write.WriteRefused as e:
            raise ApiError(str(e))
        self.scheduler.override(b, "balance", 1 if allow else 0)
        return prop or {"noop": True}

    # -- preferences

    def _skip_on(self):
        return bool((self.store.settings().get("confirm_skip") or {}).get("on"))

    def prefs(self, body, role="local"):
        if "confirm_skip" in body:
            on = body["confirm_skip"] is True
            if on:
                if role != "local":
                    raise ApiError("skip confirmations can only be turned on from the computer running VBMS Manager")
                if str(body.get("phrase", "")).strip().lower() != "skip confirmations":
                    raise ApiError('type "skip confirmations" to turn this on')
            self.store.update_settings(confirm_skip={"on": on, "since": time.strftime("%Y-%m-%d %H:%M:%S")})
            store.append_log("changes", {"event": "prefs", "confirm_skip": on, "role": role})
        if "api" in body:
            on = body["api"] is True
            if role != "local":
                raise ApiError("API requests can only be turned on or off from the computer running VBMS Manager")
            if on and str(body.get("phrase", "")).strip().lower() != "allow api requests":
                raise ApiError('type "allow api requests" to turn this on')
            self.store.update_settings(api={"on": on, "since": time.strftime("%Y-%m-%d %H:%M:%S")})
            store.append_log("changes", {"event": "prefs", "api": on, "role": role})
        if "internet_ui" in body:
            if role != "local":
                raise ApiError("this can only be changed from the computer running VBMS Manager")
            on = body["internet_ui"] is True
            self.store.update_settings(internet_ui=on)
            store.append_log("changes", {"event": "prefs", "internet_ui": on, "role": role})
        if "safety" in body:
            try:
                st = safety.clean(body["safety"] or {}, safety.settings_from(self.store.settings()))
            except ValueError as e:
                raise ApiError(str(e))
            self.store.update_settings(safety=st)
            store.append_log("changes", {"event": "prefs", "safety": st, "role": role})
        return {"ok": True, "settings": self._public_settings()}

    def key_create(self, body):
        try:
            token, rec = self.keys.create(body.get("label"), body.get("scopes"), body.get("danger") is True)
        except ValueError as e:
            raise ApiError(str(e))
        store.append_log("changes", {"event": "api_key", "action": "create", "id": rec["id"], "label": rec["label"],
                                     "scopes": rec["scopes"], "danger": rec["danger"]})
        return {"ok": True, "key": token, "record": rec}

    def key_revoke(self, body):
        try:
            self.keys.revoke(body.get("id"))
        except ValueError as e:
            raise ApiError(str(e))
        store.append_log("changes", {"event": "api_key", "action": "revoke", "id": body.get("id")})
        return {"ok": True, "api_keys": self.keys.list()}

    # -- storage mode

    def storage_prepare(self, body):
        b = self._online_mac(body)
        self._risk_ok(b)
        try:
            prop, info = storage.plan(b, body.get("on") is True)
        except (safe_write.WriteRefused, storage.StorageError) as e:
            raise ApiError(str(e))
        if prop is None:
            storage.finish(b, info, self)
            return {"noop": True, "board": self.snapshot(b, detail=True)}
        return safe_write.public(prop)

    def storage_settings(self, body):
        b = self._mac_board(body)
        try:
            c = storage.clean_cfg(body.get("cfg") or {}, storage.cfg(b.profile()))
        except storage.StorageError as e:
            raise ApiError(str(e))
        self.store.update(b.mac, storage_cfg=c)
        return self.snapshot(b, detail=True)

    # -- all boards at once

    def bulk_prepare(self, body):
        try:
            return bulk.prepare(self, body.get("action"))
        except bulk.BulkError as e:
            raise ApiError(str(e))

    def bulk_commit(self, body):
        try:
            return bulk.commit(self, body.get("id"), skipped=body.get("skipped_confirm") is True and self._skip_on())
        except bulk.BulkError as e:
            raise ApiError(str(e))

    # -- schedule

    def schedule_prepare(self, body):
        b = self._online_mac(body)
        self._risk_ok(b)
        try:
            sched = schedule.clean(body.get("schedule"))
            if schedule.needs_volts(sched):
                _, pts, max_end = self._curve(b)
                sched = schedule.attach_volts(sched, pts, max_end, b.config.values)
        except schedule.ScheduleError as e:
            raise ApiError(str(e))
        pid = schedule.propose(b.mac, sched)
        bal = self._balance_amps(b)
        rows = [{"rule": schedule.describe(r), "action": r["action"], "enabled": r["enabled"], "kind": r["kind"],
                 "revert": r.get("revert", True), "volts": r.get("volts")} for r in sched["items"]]
        return {"id": pid, "name": self._name(b), "host": b.host, "mac": b.mac, "rows": rows,
                "enabled": sched["enabled"], "balance_normal_a": bal,
                "blockers": b.snapshot().get("write_blockers")
                if any(r["action"] != "charge_off" and r["action"] != "charge_on" for r in sched["items"]) else []}

    def _name(self, b):
        return b.profile().get("nickname") or b.fw.get("hw_name")

    def _balance_amps(self, b):
        """What "allow balancing" writes back: the saved value, the board's current one, or 1.0 A."""
        cur = round(b.config.values.get("balance_max_current", 0.0), 3)
        return b.profile().get("balance_normal_a") or (cur if cur > 0 else 1.0)

    def schedule_commit(self, body):
        b = self._online_mac(body)
        self._risk_ok(b)
        try:
            sched = schedule.take(body.get("id"), b.mac)
        except schedule.ScheduleError as e:
            raise ApiError(str(e))
        sched["approved"] = time.strftime("%Y-%m-%d %H:%M")
        schedule.stamp_since(sched, b.profile().get("schedule"), self.scheduler.clock().timestamp())
        self.store.update(b.mac, schedule=sched)
        self._reevaluate(b)
        schedule.log_schedule_saved(b, sched)
        b._event("schedule", "schedule saved (%d items, %s)%s" % (
            len(sched["items"]), "on" if sched["enabled"] else "off",
            " without confirmation (skip confirmations is on)" if body.get("skipped_confirm") and self._skip_on()
            else ""))
        return self.snapshot(b, detail=True)

    def _reevaluate(self, b):
        """Re-decide every channel on the next tick, keeping the values saved before the current windows."""
        run = b.profile().get("schedule_run")
        if run:
            self.store.update(b.mac, schedule_run={ch: dict(v, key="", override=None) for ch, v in run.items()
                                                   if isinstance(v, dict)})
        self.scheduler.state.pop(b.mac, None)

    def schedule_off(self, body):
        b = self._online_mac(body)
        sched = b.profile().get("schedule")
        if sched:
            self.store.update(b.mac, schedule=dict(sched, enabled=False))
            # The scheduler's next tick gives back what the board had before the current window
            # (now, or when the board reconnects).
            self.scheduler.state.pop(b.mac, None)
            b._event("schedule", "schedule turned off")
        return self.snapshot(b, detail=True)

    # -- one-time timers

    def timer_prepare(self, body):
        b = self._online_mac(body)
        self._risk_ok(b)
        try:
            tm = schedule.clean_timer(body.get("timer"), time.time())
            if "limit" in tm["actions"]:
                _, pts, max_end = self._curve(b)
                tm["actions"]["limit"]["volts"] = schedule._volts_fn(pts, max_end, b.config.values)(
                    tm["actions"]["limit"]["pct"])
        except schedule.ScheduleError as e:
            raise ApiError(str(e))
        if len(self.scheduler.timers(b)) >= schedule.MAX_TIMERS:
            raise ApiError("at most %d timers per board; cancel one first" % schedule.MAX_TIMERS)
        pid = schedule.propose(b.mac, tm, kind="timer")
        return {"id": pid, "name": self._name(b), "host": b.host, "mac": b.mac, "timer": tm,
                "at_text": store.clock12(tm["at"], day=True), "balance_normal_a": self._balance_amps(b),
                "blockers": b.snapshot().get("write_blockers")
                if "limit" in tm["actions"] or "balance" in tm["actions"] else []}

    def timer_commit(self, body):
        b = self._online_mac(body)
        self._risk_ok(b)
        try:
            tm = schedule.take(body.get("id"), b.mac, kind="timer")
        except schedule.ScheduleError as e:
            raise ApiError(str(e))
        if tm["at"] <= time.time():
            raise ApiError("that time has already passed; set the timer again")
        self.store.update(b.mac, timers=self.scheduler.timers(b) + [tm])
        store.append_log("changes", {"event": "timer", "mac": b.mac, "host": b.host, "name": self._name(b),
                                     "at": store.clock12(tm["at"], day=True), "what": tm["label"],
                                     "confirm": "skipped" if body.get("skipped_confirm") and self._skip_on()
                                     else "confirmed"})
        b._event("timer", "timer set for %s: %s" % (store.clock12(tm["at"], day=True), tm["label"]))
        return self.snapshot(b, detail=True)

    def timer_cancel(self, body):
        b = self._online_mac(body)
        timers = self.scheduler.timers(b)
        keep = [t for t in timers if t["id"] != body.get("timer_id")]
        for t in timers:
            if t["id"] == body.get("timer_id"):
                self.store.update(b.mac, timers=keep)
                b._event("timer", "timer cancelled: %s, %s" % (t["label"], store.clock12(t["at"], day=True)))
        return self.snapshot(b, detail=True)

    # -- connection / misc

    def pause(self, body):
        b = self.board(body.get("host"))
        b.pause() if body.get("pause") else b.resume()
        return {"ok": True}

    def find_board(self, ident):
        """A board by MAC (with or without colons), host, or nickname (case-insensitive)."""
        ident = str(ident or "").strip()
        if not ident:
            return None
        flat = ident.replace(":", "").replace("-", "").lower()
        with self.lock:
            boards = list(self.boards.values())
        for b in boards:
            if b.host == ident:
                return b
        for b in boards:
            if b.mac and b.mac.replace(":", "").lower() == flat:
                return b
        for b in boards:
            if b.mac and (b.profile().get("nickname") or "").strip().lower() == ident.lower():
                return b
        return None

    # -- recording, history, analysis

    def logging_settings(self, body):
        try:
            st = datalog.clean_settings(body, datalog.settings_from(self.store.settings()))
        except ValueError as e:
            raise ApiError(str(e))
        self.store.update_settings(logging=st)
        with self.lock:
            boards = list(self.boards.values())
        for b in boards:
            if b.recorder is not None:
                b.recorder.maintain(st)  # apply a shorter retention now
        self._disk = (0.0, 0)
        return {"ok": True, "logging": st}

    def alerts(self, body):
        cur = board_mod.alert_settings(self.store.settings())
        limits = {"delta": (5, 500), "temp": (20, 100), "over": (0, 200)}
        for k, (lo, hi) in limits.items():
            if k in body:
                try:
                    v = float(body[k])
                except (TypeError, ValueError):
                    raise ApiError("%s must be a number" % k)
                if not lo <= v <= hi:
                    raise ApiError("%s must be between %g and %g" % (k, lo, hi))
                cur[k] = v
        for k in ("notify", "charge_done", "charge_early", "offline_charging"):
            if k in body:
                cur[k] = bool(body[k])
        self.store.update_settings(alerts=cur)
        return {"ok": True, "alerts": cur}

    def pack(self, body):
        b = self._online_mac(body)
        v = body.get("pack_ah")
        if v in (None, ""):
            self.store.update(b.mac, pack_ah=None)
        else:
            try:
                v = round(float(v), 2)
            except (TypeError, ValueError):
                raise ApiError("pack capacity must be a number of Ah")
            if not 0.5 <= v <= 1000:
                raise ApiError("pack capacity must be between 0.5 and 1000 Ah")
            self.store.update(b.mac, pack_ah=v)
        return self.snapshot(b, detail=True)

    def _mac_board(self, q):
        b = q["_board"] if q.get("_board") is not None else self.board(q.get("host"))
        if not b.mac:
            raise ApiError("board has not been read yet")
        return b

    def history(self, q):
        b = self._mac_board(q)
        now = time.time()
        try:
            t1 = float(q.get("to") or now)
            t0 = float(q.get("from") or t1 - 900)
            return datalog.query(b.mac, t0, t1, q.get("metric", "power"), cell=q.get("cell") or None,
                                 max_points=int(q.get("points") or 600), live=list(b.history), now=now)
        except ValueError as e:
            raise ApiError(str(e))

    def charges(self, q):
        b = self._mac_board(q)
        out = analysis.charges(b.mac, int(q.get("limit") or 200))
        return {"charges": out, "live": b.tracker.live()}

    def analysis(self, q):
        b = self._mac_board(q)
        return analysis.report(b.mac, values=b.values, nominal_ah=b.profile().get("pack_ah"))

    def backups(self, q):
        b = self._mac_board(q)
        return {"backups": safe_write.list_backups(b)}

    def restore_prepare(self, body):
        b = self._online_mac(body)
        try:
            return safe_write.prepare_restore(b, body.get("backup"))
        except safe_write.WriteRefused as e:
            raise ApiError(str(e))

    def revoke(self, body):
        self.pairing.revoke(body.get("id"))
        return {"ok": True}

    def _online_mac(self, body) -> Board:
        b = body["_board"] if body.get("_board") is not None else self.board(body.get("host"))
        if not b.mac or b.config is None:
            raise ApiError("board has not been read yet")
        if body.get("mac") and body["mac"] != b.mac:
            raise ApiError("board identity changed; reload the page")
        return b


class ApiError(Exception):
    pass


def make_handler(app: App, port: int):
    app.port = port

    class Handler(BaseHTTPRequestHandler):
        server_version = "VBMSManager"

        def log_message(self, fmt, *args):
            pass

        def _role(self):
            cookie = access.parse_cookie(self.headers.get("Cookie")).get(access.COOKIE)
            role = access.classify(self.client_address[0], self.headers.get("Host", ""), cookie, app.pairing,
                                   headers=self.headers, internet_ui=bool(app.store.settings().get("internet_ui")))
            if role == "internet" or not access.is_lan(access._ip(self._client_ip())):
                app.internet_seen = time.strftime("%Y-%m-%d %H:%M:%S")
            return role

        def _client_ip(self):
            return access.client_ip(self.client_address[0], self.headers)[0]

        def _internet_page(self):
            data = (b"This VBMS Manager only answers API requests (/api/v1, with an API key) from outside your "
                    b"network.\nTo use the web page from here, allow it in Preferences on the computer running "
                    b"VBMS Manager, or use a VPN.\nAPI reference: /docs/api\n")
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _api_v1(self, method):
            u = urlparse(self.path)
            ip = self._client_ip()
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 1_000_000:
                        raise ValueError
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    return self._json({"error": "body must be a JSON object", "code": "bad_json"}, 400)
            code, obj = app.api.handle(method, u.path, q, body, self.headers, ip)
            return self._json(obj, code)

        def _json(self, obj, code=200, headers=()):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/v1/") or self.path == "/api/v1":
                return self._api_v1("GET")
            role = self._role()
            if urlparse(self.path).path.rstrip("/") == docs_page.PATH:
                return self._docs()   # public reference, no token: every client may read it
            if role == "internet":
                return self._internet_page()
            if role == "reject":
                return self._json({"error": "bad host"}, 403)
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/state":
                    return self._json(app.state(role))
                if u.path == "/api/board":
                    return self._json(app.snapshot(app.board(q.get("host")), detail=True))
                if u.path == "/api/log":
                    name = q.get("name")
                    if name not in ("changes", "events"):
                        raise ApiError("unknown log")
                    return self._json(store.read_log(name, int(q.get("limit") or 300)))
                gets = {"/api/history": app.history, "/api/charges": app.charges, "/api/analysis": app.analysis,
                        "/api/backups": app.backups}
                if u.path in gets:
                    return self._json(gets[u.path](q))
                if u.path == "/api/history/csv":
                    b = app._mac_board(q)
                    t1 = float(q.get("to") or time.time())
                    t0 = float(q.get("from") or t1 - 86400)
                    name = store.safe_name(b.profile().get("nickname") or b.mac.replace(":", ""))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="%s_%s_%s.csv"' % (
                        name, time.strftime("%Y%m%d-%H%M", time.localtime(t0)),
                        time.strftime("%Y%m%d-%H%M", time.localtime(t1))))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    for chunk in datalog.csv_rows(b.mac, t0, t1):
                        self.wfile.write(chunk.encode("utf-8"))
                    return
                if u.path == "/api/export":
                    if q.get("token") != app.token or role not in ("local", "paired"):
                        return self._json({"error": "pair this device to export configs"}, 403)
                    b = app.board(q.get("host"))
                    if b.config is None:
                        raise ApiError("board has not been read yet")
                    xml = b.config.to_vesc_tool_xml(include_secrets=q.get("secrets") == "1").encode("utf-8")
                    name = store.safe_name(b.profile().get("nickname") or b.mac.replace(":", ""))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Disposition", 'attachment; filename="%s_config.xml"' % name)
                    self.send_header("Content-Length", str(len(xml)))
                    self.end_headers()
                    self.wfile.write(xml)
                    return
            except (ApiError, ValueError) as e:
                return self._json({"error": str(e)}, 400)
            return self._static(u.path)

        def _docs(self):
            data = docs_page.page(getattr(app, "version", ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
            if not full.startswith(WEB + os.sep) or not os.path.isfile(full):
                self.send_error(404)
                return
            with open(full, "rb") as f:
                data = f.read()
            if full.endswith("index.html"):
                data = data.replace(b"__VBMS_TOKEN__", app.token.encode())
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".js"):
                ctype = "text/javascript"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or
                                                      ctype.endswith("javascript") or ctype.endswith("svg+xml")
                                                      else ""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path.startswith("/api/v1/") or self.path == "/api/v1":
                return self._api_v1("POST")
            role = self._role()
            if role == "internet":
                return self._internet_page()
            if role == "reject" or self.headers.get("X-VBMS-Token") != app.token:
                return self._json({"error": "forbidden"}, 403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1_000_000:
                    raise ValueError
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise ApiError("bad request")
            except (ValueError, ApiError):
                return self._json({"error": "bad JSON"}, 400)
            path = urlparse(self.path).path
            if path == "/api/pair":
                if role != "viewer":
                    return self._json({"ok": True})
                try:
                    tok = app.pairing.pair(self.client_address[0], body.get("code"), body.get("label"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                cookie = "%s=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict" % (access.COOKIE, tok)
                return self._json({"ok": True}, headers=[("Set-Cookie", cookie)])
            if role not in ("local", "paired"):
                return self._json({"error": "this device is view-only; pair it to make changes"}, 403)
            routes = {
                "/api/hosts/add": app.add_host,
                "/api/hosts/remove": app.remove_host,
                "/api/scan": app.scan,
                "/api/ble/scan": app.ble_scan,
                "/api/board/link": app.set_link,
                "/api/board/profile": app.set_profile,
                "/api/board/accept_curve": app.accept_curve,
                "/api/board/ack_script": app.ack_script,
                "/api/board/ack_risk": app.ack_risk,
                "/api/board/pause": app.pause,
                "/api/board/pack": app.pack,
                "/api/logging/settings": app.logging_settings,
                "/api/alerts": app.alerts,
                "/api/restore/prepare": app.restore_prepare,
                "/api/addon/prepare": app.addon_prepare,
                "/api/charge/prepare": app.prepare,
                "/api/config/prepare": app.prepare_fields,
                "/api/write/commit": app.commit,
                "/api/write/cancel": app.cancel,
                "/api/charge/commit": app.commit,
                "/api/charge/cancel": app.cancel,
                "/api/runtime": app.runtime,
                "/api/schedule/prepare": app.schedule_prepare,
                "/api/schedule/commit": app.schedule_commit,
                "/api/schedule/off": app.schedule_off,
                "/api/balance/prepare": app.balance_prepare,
                "/api/timer/prepare": app.timer_prepare,
                "/api/timer/commit": app.timer_commit,
                "/api/timer/cancel": app.timer_cancel,
                "/api/storage/prepare": app.storage_prepare,
                "/api/storage/settings": app.storage_settings,
                "/api/bulk/prepare": app.bulk_prepare,
                "/api/bulk/commit": app.bulk_commit,
            }
            if path == "/api/prefs":
                try:
                    return self._json(app.prefs(body, role))
                except ApiError as e:
                    return self._json({"error": str(e)}, 400)
            if path in ("/api/keys/create", "/api/keys/revoke"):
                if role != "local":
                    return self._json({"error": "API keys can only be managed from the computer running VBMS Manager"},
                                      403)
                try:
                    return self._json((app.key_create if path.endswith("create") else app.key_revoke)(body))
                except ApiError as e:
                    return self._json({"error": str(e)}, 400)
            if path == "/api/devices/revoke":
                if role != "local":
                    return self._json({"error": "only the computer running VBMS Manager can remove devices"}, 403)
                return self._json(app.revoke(body))
            fn = routes.get(path)
            if fn is None:
                return self._json({"error": "not found"}, 404)
            try:
                return self._json(fn(body))
            except ApiError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # keep the server alive; report the problem
                return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    return Handler


class _Server(ThreadingHTTPServer):
    def finish_request(self, request, client_address):
        request.settimeout(60)    # a forwarded port gets slow or idle clients; don't hold a thread forever
        self.RequestHandlerClass(request, client_address, self)

    def handle_error(self, request, client_address):
        """Dropped connections are routine on a forwarded port: don't print a traceback for each."""
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, OSError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def serve(app: App, host="127.0.0.1", port=8642):
    app.lan = host not in ("127.0.0.1", "localhost", "::1")
    httpd = _Server((host, port), make_handler(app, port))
    httpd.daemon_threads = True
    app.httpd = httpd
    return httpd

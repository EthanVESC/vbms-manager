"""The key-authenticated HTTP API: /api/v1/...

Off until "Allow API requests" is turned on in Preferences (from the computer running
VBMS Manager, typed phrase). That switch is the approval for every API write: API writes
don't ask again, but they go through exactly the same checks as the UI (safety warning
accepted, script verified, hard limits, fresh read, backup, read-back, changes.log).

Auth: "Authorization: Bearer <key>" (or "X-API-Key: <key>"). Keys are made in Preferences
with scopes:
  read      - every GET
  control   - charge limit, charging pause/allow, balancing, storage mode, schedule,
              timers, connection
  settings  - edit config fields (caution level); "danger" fields need the key's danger tick
Restoring backups and installing add-ons are never available over the API.

Every write takes ?dry_run=1 (or "dry_run": true) to see what would change without writing.
See docs/API.md.
"""

import sys
import time

import charge_limit
import bulk
import config_fields
import safe_write
import schedule
import storage
import store
from vesc.client import VescError

DANGER_HELP = "this key can't change danger-level settings (give it the danger tick in Preferences)"


class ApiFail(Exception):
    def __init__(self, status, code, msg, **extra):
        Exception.__init__(self, msg)
        self.status, self.code, self.extra = status, code, extra


def _truthy(v):
    return v is True or str(v).lower() in ("1", "true", "yes")


class Api:
    def __init__(self, app):
        self.app = app
        self.ApiError = sys.modules[type(app).__module__].ApiError   # server.ApiError, without a circular import

    # -- entry point -----------------------------------------------------------

    def handle(self, method, path, query, body, headers, ip):
        """Returns (http status, JSON object)."""
        try:
            key = self._auth(headers, ip)
            return 200, self._route(method, path.rstrip("/"), query, body, key, ip)
        except ApiFail as e:
            return e.status, dict({"error": str(e), "code": e.code}, **e.extra)
        except Exception as e:  # keep the server alive; report the problem
            return 500, {"error": "%s: %s" % (type(e).__name__, e), "code": "internal"}

    def _auth(self, headers, ip):
        if not (self.app.store.settings().get("api") or {}).get("on"):
            raise ApiFail(403, "api_off", "API requests are turned off. Turn on \"Allow API requests\" in "
                                          "Preferences on the computer running VBMS Manager.")
        tok = None
        auth = headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            tok = auth[7:].strip()
        elif headers.get("X-API-Key"):
            tok = headers.get("X-API-Key").strip()
        if self.app.keys.blocked(ip):
            raise ApiFail(429, "rate_limited", "too many bad keys from this address; wait a minute")
        if not tok:
            raise ApiFail(401, "no_key", "send your API key as: Authorization: Bearer <key>")
        key = self.app.keys.check(tok, ip)
        if key is None:
            raise ApiFail(401, "bad_key", "unknown or revoked API key")
        return key

    @staticmethod
    def _need(key, scope):
        if scope not in key["scopes"]:
            raise ApiFail(403, "scope", "this key doesn't have the \"%s\" scope" % scope)

    def _board(self, ident):
        b = self.app.find_board(ident)
        if b is None:
            raise ApiFail(404, "no_board", "no board %r (use its MAC, IP/host or nickname)" % ident)
        return b

    def _online(self, b):
        if not b.mac or b.config is None or b.state != "online":
            raise ApiFail(409, "offline", "board is %s; it must be connected for this" % b.state)
        return b

    def _risk(self, b):
        if not safe_write.risk_accepted(b.profile()):
            raise ApiFail(409, "risk", "accept the safety warning for this board in the web UI first")

    @staticmethod
    def _via(key, ip):
        return {"kind": "api", "key": key["label"], "key_id": key["id"], "ip": ip}

    @staticmethod
    def _why(key):
        return 'by API (key "%s")' % key["label"]

    # -- routing ---------------------------------------------------------------

    def _route(self, method, path, q, body, key, ip):
        parts = [p for p in path.split("/") if p][2:]  # drop "api", "v1"
        dry = _truthy(q.get("dry_run")) or _truthy(body.get("dry_run"))
        if method == "GET":
            self._need(key, "read")
            return self._get(parts, q, key)
        if method != "POST":
            raise ApiFail(405, "method", "use GET or POST")
        if parts[:1] == ["all"] and len(parts) == 2:
            self._need(key, "control")
            return self._all(parts[1], body, key, ip, dry)
        if len(parts) >= 3 and parts[0] == "boards":
            b = self._board(parts[1])
            return self._post_board(b, parts[2:], body, key, ip, dry)
        raise ApiFail(404, "not_found", "no such endpoint (see docs/API.md)")

    # -- reads -----------------------------------------------------------------

    def _get(self, parts, q, key):
        app = self.app
        if parts in ([], ["status"]):
            with app.lock:
                boards = list(app.boards.values())
            return {"ok": True, "version": app.version, "time": time.time(), "boards": len(boards),
                    "online": sum(1 for b in boards if b.state == "online"), "key": key["label"],
                    "scopes": key["scopes"], "danger": key["danger"]}
        if parts == ["boards"]:
            with app.lock:
                boards = list(app.boards.values())
            return {"boards": [self.summary(b) for b in boards]}
        if parts == ["notices"]:
            with app.lock:
                boards = list(app.boards.values())
            return {"notices": [dict(n, board=self.summary(b, brief=True)) for b in boards for n in b.notices]}
        if parts == ["log", "changes"]:
            return {"changes": store.read_log("changes", min(int(q.get("limit") or 200), 2000))}
        if parts == ["log", "events"]:
            return {"events": store.read_log("events", min(int(q.get("limit") or 200), 2000))}
        if len(parts) >= 2 and parts[0] == "boards":
            b = self._board(parts[1])
            sub = parts[2:]
            if not sub:
                return app.snapshot(b, detail=True)
            if not b.mac:
                raise ApiFail(409, "unread", "board has not been read yet")
            qq = dict(q, _board=b)
            if sub == ["cells"]:
                v = b.values or {}
                return {"time": b.values_time, "cells": v.get("cells"), "balancing": v.get("balancing"),
                        "cell_min": v.get("cell_min"), "cell_max": v.get("cell_max"),
                        "delta_mv": v.get("cell_delta_mv"), "temps": v.get("temps")}
            if sub == ["config"]:
                if b.config is None:
                    raise ApiFail(409, "unread", "config has not been read yet")
                return {"editable": config_fields.editable(b.config), "charge": app.snapshot(b).get("charge")}
            if sub == ["history"]:
                try:
                    return app.history(qq)
                except ValueError as e:
                    raise ApiFail(422, "invalid", str(e))
            if sub == ["charges"]:
                return app.charges(qq)
            if sub == ["analysis"]:
                return app.analysis(qq)
            if sub == ["events"]:
                return {"events": list(b.events)}
            if sub == ["schedule"]:
                return {"schedule": b.profile().get("schedule"), "timers": app.scheduler.timers(b),
                        "status": app.scheduler.status(b)}
            if sub == ["storage"]:
                return {"storage": b.profile().get("storage"), "cfg": storage.cfg(b.profile())}
        raise ApiFail(404, "not_found", "no such endpoint (see docs/API.md)")

    def summary(self, b, brief=False):
        prof = b.profile() if b.mac else {}
        out = {"id": b.mac, "host": b.host, "name": prof.get("nickname") or (b.fw or {}).get("hw_name"),
               "state": b.state}
        if brief:
            return out
        v = b.values if b.state == "online" else None
        out.update(online=b.state == "online", storage=storage.is_on(prof), rest=b.resting(),
                   notices=list(b.notices), last_seen=prof.get("last_seen"))
        if v:
            out.update(soc=round(v["soc"] * 100, 1), pack_v=round(v["v_tot"], 3), current_a=round(v["i_in_ic"], 2),
                       charger_v=round(v["v_charge"], 2), charging=v["is_charging"], balancing=v["is_balancing"],
                       cells=len(v["cells"]), cell_min=v["cell_min"], cell_max=v["cell_max"],
                       delta_mv=v["cell_delta_mv"], temp_max=max([t["value"] for t in v["temps"]] or [None]),
                       fault=v["charge_fault"], values_age_s=round(time.time() - b.values_time, 1))
        if b.config is not None:
            vals = b.config.values
            out.update(charge_start=round(vals["vc_charge_start"], 3), charge_end=round(vals["vc_charge_end"], 3),
                       balance_min=round(vals.get("vc_balance_min", 0), 3),
                       balance_max_current=round(vals.get("balance_max_current", 0), 3))
            r = b.cell_profile()
            if r:
                out["limit_pct"] = charge_limit.pct_for_end(vals["vc_charge_end"], r[1], r[2])
        rt = b.runtime_state.get("charge_allowed")
        out["charging_paused"] = bool(rt and rt["value"] == 0)
        return out

    # -- writes ----------------------------------------------------------------

    def _write(self, b, prop, key, ip, dry):
        """Run (or, dry, describe) a register=False proposal."""
        if prop is None:
            return {"ok": True, "noop": True, "dry_run": dry, "text": "the board already has these settings"}
        if prop.get("confirm_name") and not key["danger"]:
            raise ApiFail(403, "danger", DANGER_HELP,
                          fields=[r["label"] for r in prop["rows"] if any(x["level"] == "danger" for x in r["risks"])])
        if dry:
            out = safe_write.public(prop)
            out.pop("id", None)
            return dict(out, ok=True, dry_run=True)
        res = safe_write.apply(b, prop, self._via(key, ip))
        return dict(res, dry_run=False)

    def _post_board(self, b, sub, body, key, ip, dry):
        app = self.app
        why = self._why(key)
        if sub == ["connection"]:
            self._need(key, "control")
            if body.get("connected") is None:
                raise ApiFail(422, "invalid", 'send {"connected": true|false}')
            if dry:
                return {"ok": True, "dry_run": True}
            if _truthy(body["connected"]):
                b.resume(why)
            else:
                b.pause()
            return {"ok": True, "state": b.state}
        if sub == ["storage", "settings"]:
            self._need(key, "control")
            try:
                c = storage.clean_cfg(body, storage.cfg(b.profile()))
            except storage.StorageError as e:
                raise ApiFail(422, "invalid", str(e))
            if not b.mac:
                raise ApiFail(409, "unread", "board has not been read yet")
            if not dry:
                b.boards.update(b.mac, storage_cfg=c)
            return {"ok": True, "dry_run": dry, "cfg": c}
        if sub == ["storage"]:
            self._need(key, "control")
            if body.get("on") is None:
                raise ApiFail(422, "invalid", 'send {"on": true|false}')
            on = _truthy(body["on"])
            if not b.mac:
                raise ApiFail(409, "unread", "board has not been read yet")
            self._risk(b)
            if not on and b.state != "online" and storage.is_on(b.profile()):
                if dry:
                    return {"ok": True, "dry_run": True, "text": "storage off as soon as it's connected"}
                storage.request_off_later(b)
                return {"ok": True, "pending": True, "text": "storage off as soon as it's connected"}
            self._online(b)
            try:
                prop, info = storage.plan(b, on, "storage mode %s (%s)" % ("on" if on else "off", why), register=False)
            except (safe_write.WriteRefused, storage.StorageError) as e:
                raise ApiFail(409, "refused", str(e))
            if prop is None:
                if not dry:
                    storage.finish(b, info, app)
                return {"ok": True, "noop": True, "dry_run": dry}
            res = self._write(b, prop, key, ip, dry)
            if not dry:
                storage.finish(b, info, app)
            return res

        self._online(b)
        self._risk(b)
        try:
            if sub == ["charge-limit"]:
                self._need(key, "control")
                ApiError = self.ApiError
                try:
                    start, end, reason = app.charge_target(b, body)
                except ApiError as e:
                    raise ApiFail(422, "invalid", str(e))
                prop = safe_write.prepare(b, start, end, "%s (%s)" % (reason, why), register=False)
                return self._write(b, prop, key, ip, dry)
            if sub == ["charging"]:
                self._need(key, "control")
                if body.get("allowed") is None:
                    raise ApiFail(422, "invalid", 'send {"allowed": true|false}')
                val = 1 if _truthy(body["allowed"]) else 0
                if dry:
                    return {"ok": True, "dry_run": True, "charge_allowed": val}
                w = why
                if app.scheduler.override(b, "charge", val):
                    w += ", overriding the schedule until its next change"
                b.send_runtime("charge_allowed", val, w)
                return {"ok": True, "charge_allowed": val, "note": "RAM only: the BMS forgets this when it restarts"}
            if sub == ["balancing"]:
                self._need(key, "control")
                if body.get("force") is not None:
                    val = 1 if _truthy(body["force"]) else 0
                    if not dry:
                        b.send_runtime("force_balance", val, why)
                    return {"ok": True, "dry_run": dry, "force_balance": val}
                if body.get("allowed") is None:
                    raise ApiFail(422, "invalid", 'send {"allowed": true|false} or {"force": true|false}')
                if storage.is_on(b.profile()):
                    raise ApiFail(409, "storage", "storage mode is on for this board (it sets balancing)")
                allow = _truthy(body["allowed"])
                prop = safe_write.prepare_balance(b, allow, register=False, reason="%s balancing (%s)" % (
                    "allow" if allow else "disallow", why))
                if not dry:
                    app.scheduler.override(b, "balance", 1 if allow else 0)
                return self._write(b, prop, key, ip, dry)
            if sub == ["config"]:
                self._need(key, "settings")
                changes = body.get("changes")
                if not isinstance(changes, dict) or not changes:
                    raise ApiFail(422, "invalid", 'send {"changes": {"field_name": value, ...}}')
                prop = safe_write.prepare_fields(b, changes, "settings (%s): %s" % (why, ", ".join(changes)),
                                                 register=False)
                return self._write(b, prop, key, ip, dry)
            if sub == ["schedule"]:
                self._need(key, "control")
                return self._schedule(b, body, key, dry)
            if sub == ["schedule", "off"]:
                self._need(key, "control")
                if dry:
                    return {"ok": True, "dry_run": True}
                app.schedule_off({"_board": b})
                return {"ok": True}
            if sub == ["timers"]:
                self._need(key, "control")
                return self._timer(b, body, key, dry)
            if len(sub) == 3 and sub[0] == "timers" and sub[2] == "cancel":
                self._need(key, "control")
                if not any(t["id"] == sub[1] for t in app.scheduler.timers(b)):
                    raise ApiFail(404, "no_timer", "no such timer")
                if not dry:
                    app.timer_cancel({"_board": b, "timer_id": sub[1]})
                return {"ok": True, "dry_run": dry}
        except safe_write.WriteRefused as e:
            raise ApiFail(409, "refused", str(e))
        except (VescError, OSError) as e:
            raise ApiFail(409, "link", "board connection problem: %s" % e)
        raise ApiFail(404, "not_found", "no such endpoint (see docs/API.md)")

    def _schedule(self, b, body, key, dry):
        app = self.app
        ApiError = self.ApiError
        try:
            prop = app.schedule_prepare({"_board": b, "schedule": body.get("schedule")})
        except ApiError as e:
            raise ApiFail(422, "invalid", str(e))
        if dry:
            schedule.take(prop["id"], b.mac)
            return dict(prop, ok=True, dry_run=True, id=None)
        app.schedule_commit({"_board": b, "id": prop["id"]})
        b._event("schedule", "schedule saved %s" % self._why(key))
        return {"ok": True, "rows": prop["rows"], "enabled": prop["enabled"]}

    def _timer(self, b, body, key, dry):
        app = self.app
        ApiError = self.ApiError
        try:
            prop = app.timer_prepare({"_board": b, "timer": body.get("timer")})
        except ApiError as e:
            raise ApiFail(422, "invalid", str(e))
        if dry:
            schedule.take(prop["id"], b.mac, kind="timer")
            return dict(prop, ok=True, dry_run=True, id=None)
        app.timer_commit({"_board": b, "id": prop["id"]})
        return {"ok": True, "timer": prop["timer"], "at_text": prop["at_text"]}

    def _all(self, what, body, key, ip, dry):
        app = self.app
        why = self._why(key)
        if what == "charge-limit":
            with app.lock:
                boards = list(app.boards.values())
            out = []
            for b in boards:
                row = {"host": b.host, "mac": b.mac, "name": (b.profile().get("nickname") if b.mac else None)}
                try:
                    out.append(dict(row, **self._post_board(b, ["charge-limit"], body, key, ip, dry)))
                except ApiFail as e:
                    out.append(dict(row, ok=False, error=str(e), code=e.code))
            return {"ok": all(r.get("ok") for r in out), "dry_run": dry, "results": out}
        acts = {"charging": ("charging_on", "charging_off", "allowed"),
                "balancing": ("balancing_on", "balancing_off", "allowed"),
                "storage": ("storage_on", "storage_off", "on")}
        if what not in acts:
            raise ApiFail(404, "not_found", "use /all/charge-limit, /all/charging, /all/balancing or /all/storage")
        on_a, off_a, field = acts[what]
        if body.get(field) is None:
            raise ApiFail(422, "invalid", 'send {"%s": true|false}' % field)
        action = on_a if _truthy(body[field]) else off_a
        if dry:
            p = bulk.prepare(app, action)
            bulk._pending.pop(p["id"], None)
            return dict(p, id=None, ok=True, dry_run=True)
        return bulk.run(app, action, why, via=self._via(key, ip))

"""Charge schedules and one-time timers.

A board's schedule (saved in boards.json under its MAC):

    {"enabled": true, "items": [item, ...], "approved": "2026-10-28 21:00"}
    item = {"id", "name", "enabled", "repeat": "weekly"|"once", "days": [0..6 Mon=0], "date": "YYYY-MM-DD",
            "kind": "window"|"event", "start": "HH:MM", "end": "HH:MM" (windows only),
            "action": "limit"|"charge_off"|"charge_on"|"balance_off"|"balance_on",
            "pct": 60..100 (limit), "revert": bool (limit windows),
            "volts": {"start", "end"},       # volts filled in by the server on approval
            "since": epoch}                  # when the item started counting (set on approval)

A window runs from start to end local time; if end <= start it runs past
midnight into the next day. End "24:00" means the end of the day. An event
happens at its start time and has no end. Timers (board profile "timers") are
one-time events set from the Overview/Charging/Balancing screens:

    timer = {"id", "at": epoch, "actions": {"limit": {"pct", "volts"}, "charge": 0|1, "balance": 0|1},
             "label", "created"}

Channels: every action belongs to one channel, and channels never interfere:
  limit    Charge Start/End (config write)
  charge   the live Pause/Allow charging command (RAM only, re-sent while paused)
  balance  Balance Max Current (config write: 0 A = disallowed, the saved value = allowed)

Resolution (per channel): the latest boundary wins. A window start, an event or
a timer sets its own value. A window end hands over to a window that is still
running on that channel (one-time windows first, then the list order), or, if
none is, reverts to what the board had before the windows (limit windows can
turn revert off and keep their limit). So a timer beats a running window until
the next scheduled boundary on its channel, and then the schedule takes over.
At the same instant: end, then start, then event, then timer.

Approval: saving a schedule (or a timer) shows exactly what each item writes.
Once confirmed, the scheduler applies it through safe_write (backup, read-back,
change log) without asking again. Any edit needs a new approval.

Edge-triggered: a channel is only changed when its deciding boundary changes,
so a manual change holds until the next boundary. Before a window first changes
a channel, the scheduler reads the board's current value and keeps it
(persisted). On revert it writes that back, unless the board no longer holds
what the schedule wrote (you changed it by hand; that wins). Offline boards
catch up on reconnect: the latest boundary is applied, so a window that started
and ended while the board was offline is skipped.
"""

import datetime as dt
import re
import secrets
import threading
import time

import charge_limit
import safe_write
import safety
import storage
import store

MAX_ITEMS = 40
MAX_TIMERS = 10
PROPOSAL_TTL = 300.0
TICK_S = 2.0
SAFETY_S = 30.0          # how often safety notices are re-evaluated per board
PAUSE_RESEND_S = 30.0
WRITE_RETRY_S = 300.0
CRASH_BACKOFF_S = 30.0
LOOKBACK_DAYS = 8
TIMER_MAX_S = 7 * 86400
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CHANNELS = ("limit", "charge", "balance")
ACTIONS = {"limit": "limit", "charge_off": "charge", "charge_on": "charge",
           "balance_off": "balance", "balance_on": "balance"}
_RANK = {"end": 0, "start": 1, "event": 2, "timer": 3}


class ScheduleError(ValueError):
    pass


# ---------------------------------------------------------------- validation

def _minutes(s):
    if s == "24:00":
        return 1440
    m = _TIME.match(str(s or ""))
    if not m:
        raise ScheduleError("times must look like 07:30")
    return int(m.group(1)) * 60 + int(m.group(2))


def _upgrade(raw):
    """Schedules saved before single events: {"rules": [{mode: block|limit}], "default_pct"}."""
    if raw.get("items") is None and raw.get("rules") is not None:
        items = []
        for r in raw.get("rules") or []:
            if isinstance(r, dict):
                items.append(dict(r, kind="window", revert=True,
                                  action="charge_off" if r.get("mode") == "block" else r.get("mode")))
        raw = dict(raw, items=items)
    return raw


def migrate(sched):
    """Bring a schedule saved by an older version up to the current format, or None if it's current.

    The runner only reads "items", so an old {"rules": ...} schedule would otherwise do nothing.
    Approved voltages and the approval carry over; the old schedule is kept under "v1".
    """
    if not isinstance(sched, dict) or sched.get("items") is not None or sched.get("rules") is None:
        return None
    keep = {k: sched[k] for k in ("enabled", "approved", "stale") if k in sched}
    try:
        out = clean(sched)
    except ScheduleError:
        return dict(keep, items=[], enabled=False, stale=True, v1=sched)
    old = {r.get("id"): r for r in sched["rules"] if isinstance(r, dict)}
    for it in out["items"]:
        v = old.get(it["id"], {}).get("volts")
        if it["action"] == "limit" and v:
            it["volts"] = v
    return dict(out, **keep, v1=sched)


def clean(raw):
    """Validate a schedule from the UI. Returns a clean copy without volts."""
    if not isinstance(raw, dict):
        raise ScheduleError("bad schedule")
    raw = _upgrade(raw)
    items_in = raw.get("items") or []
    if not isinstance(items_in, list) or len(items_in) > MAX_ITEMS:
        raise ScheduleError("at most %d schedule items" % MAX_ITEMS)
    items = []
    for i, r in enumerate(items_in):
        if not isinstance(r, dict):
            raise ScheduleError("bad schedule item")
        name = str(r.get("name") or "").strip()[:40] or "Item %d" % (i + 1)
        out = {"id": str(r.get("id") or secrets.token_hex(4))[:16], "name": name,
               "enabled": r.get("enabled", True) is not False}
        rep = r.get("repeat")
        if rep == "weekly":
            days = sorted({int(d) for d in (r.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6})
            if not days:
                raise ScheduleError('"%s": pick at least one day' % name)
            out.update(repeat="weekly", days=days)
        elif rep == "once":
            try:
                dt.date.fromisoformat(str(r.get("date")))
            except ValueError:
                raise ScheduleError('"%s": pick a date' % name)
            out.update(repeat="once", date=str(r.get("date")))
        else:
            raise ScheduleError('"%s": choose repeating or one time' % name)
        kind = r.get("kind", "window")
        a = _minutes(r.get("start"))
        if a == 1440:
            raise ScheduleError('"%s": can\'t start at 24:00' % name)
        if kind == "window":
            b = _minutes(r.get("end"))
            if a == b:
                raise ScheduleError('"%s": start and end time are the same' % name)
            out.update(kind="window", start=r["start"], end=r["end"])
        elif kind == "event":
            out.update(kind="event", start=r["start"])
        else:
            raise ScheduleError('"%s": choose a block or a single event' % name)
        action = r.get("action")
        if action not in ACTIONS:
            raise ScheduleError('"%s": choose what it does' % name)
        out["action"] = action
        if action == "limit":
            out["pct"] = _pct(r.get("pct"))
            if kind == "window":
                out["revert"] = r.get("revert", True) is not False
        items.append(out)
    return {"enabled": raw.get("enabled", True) is not False, "items": items}


_DEF = ("enabled", "repeat", "days", "date", "kind", "start", "end", "action", "pct", "revert")


def stamp_since(sched, old, now_epoch):
    """Give every item the time it started counting. Occurrences before that are ignored, so a new
    weekly event doesn't fire for last week's slot when it's saved. Unchanged items keep theirs."""
    prev = {r["id"]: r for r in (old or {}).get("items", [])} if live(old) else {}
    for it in sched["items"]:
        o = prev.get(it["id"])
        same = o is not None and all(o.get(k) == it.get(k) for k in _DEF)
        it["since"] = o.get("since", 0) if same else round(now_epoch)
    return sched


def _pct(v):
    try:
        p = int(v)
    except (TypeError, ValueError):
        raise ScheduleError("charge limits are whole percentages")
    if isinstance(v, float) and v != p or not (charge_limit.PCT_MIN <= p <= charge_limit.PCT_MAX):
        raise ScheduleError("charge limits must be %d-%d%%" % (charge_limit.PCT_MIN, charge_limit.PCT_MAX))
    return p


def _volts_fn(pts, max_end, device):
    def volts(p):
        try:
            r = charge_limit.compute(p, pts, max_end, device)
        except charge_limit.LimitError as e:
            raise ScheduleError("%d%%: %s" % (p, e))
        return {"start": r["start"], "end": r["end"]}
    return volts


def attach_volts(sched, pts, max_end, device):
    """Compute the exact voltages each limit item will write."""
    volts = _volts_fn(pts, max_end, device)
    out = dict(sched)
    out["items"] = [dict(r, volts=volts(r["pct"])) if r["action"] == "limit" else dict(r) for r in sched["items"]]
    return out


def needs_volts(sched):
    return any(r["action"] == "limit" for r in sched["items"])


def clean_timer(raw, now_epoch):
    """{"in_s" | "at", "pct", "charge": "on"|"off", "balance": "on"|"off"} -> timer without volts."""
    if not isinstance(raw, dict):
        raise ScheduleError("bad timer")
    try:
        at = float(raw["at"]) if raw.get("at") is not None else now_epoch + float(raw.get("in_s"))
    except (TypeError, ValueError):
        raise ScheduleError("say when the timer should fire")
    if not now_epoch < at <= now_epoch + TIMER_MAX_S:
        raise ScheduleError("a timer must fire within the next 7 days")
    actions = {}
    if raw.get("pct") not in (None, ""):
        actions["limit"] = {"pct": _pct(raw["pct"])}
    for ch in ("charge", "balance"):
        v = raw.get(ch)
        if v in ("on", "off"):
            actions[ch] = 1 if v == "on" else 0
        elif v not in (None, ""):
            raise ScheduleError("bad %s action" % ch)
    if not actions:
        raise ScheduleError("choose what the timer does")
    return {"id": secrets.token_hex(4), "at": round(at), "actions": actions, "label": describe_timer(actions),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}


# ---------------------------------------------------------------- resolution

def channel(item):
    return ACTIONS[item["action"]]


def item_value(item):
    if item["action"] == "limit":
        return {"pct": item["pct"], "volts": item.get("volts")}
    return 1 if item["action"].endswith("_on") else 0


def _occ(item, day):
    """(start, end) datetimes of the item's occurrence that STARTS on date `day`, or None. end is None for events."""
    if item["repeat"] == "weekly":
        if day.weekday() not in item["days"]:
            return None
    elif day.isoformat() != item["date"]:
        return None
    midnight = dt.datetime.combine(day, dt.time(0, 0))
    a = _minutes(item["start"])
    s = midnight + dt.timedelta(minutes=a)
    if item["kind"] == "event":
        return s, None
    b = _minutes(item["end"])
    e = midnight + dt.timedelta(minutes=b)
    if b <= a:
        e += dt.timedelta(days=1)
    return s, e


def _ordered(sched):
    if not live(sched):
        return []
    rules = [r for r in sched.get("items", []) if r.get("enabled", True)]
    return [r for r in rules if r["repeat"] == "once"] + [r for r in rules if r["repeat"] == "weekly"]


def live(sched):
    return bool(sched and sched.get("enabled") and not sched.get("stale"))


def _days(item, d0, d1):
    if item["repeat"] == "once":
        return [dt.date.fromisoformat(item["date"])]
    return [d0 + dt.timedelta(days=k) for k in range((d1 - d0).days + 1)]


def _bounds(items, timers, ch, d0, d1):
    """Every boundary on channel `ch` for occurrences starting d0..d1 (plus all one-time items and timers):
    (time, rank, -priority, kind, source)."""
    out = []
    for prio, it in enumerate(items):
        if channel(it) != ch:
            continue
        since = dt.datetime.fromtimestamp(it.get("since") or 0)
        for day in _days(it, d0, d1):
            occ = _occ(it, day)
            if not occ:
                continue
            s, e = occ
            if e is None:
                if s >= since:
                    out.append((s, _RANK["event"], -prio, "event", it))
            elif e > since:
                # A block that was already running when it was added counts from then.
                out.append((max(s, since), _RANK["start"], -prio, "start", it))
                out.append((e, _RANK["end"], -prio, "end", it))
    for t in timers or []:
        if ch in t["actions"]:
            out.append((dt.datetime.fromtimestamp(t["at"]), _RANK["timer"], 0, "timer", t))
    return out


def _active_windows(items, ch, now):
    """Windows on `ch` running at `now`, highest priority first, with their start time."""
    out = []
    for it in items:
        if channel(it) != ch or it["kind"] != "window":
            continue
        for day in (now.date(), now.date() - dt.timedelta(days=1)):
            occ = _occ(it, day)
            if occ and occ[0] <= now < occ[1]:
                out.append((it, occ[0]))
                break
    return out


def desired(sched, timers, ch, now):
    """What channel `ch` should be at `now`, or None if nothing in the schedule or timers decides it.

    {"key", "kind", "value" (or "revert"), "source" (item or timer), "in_window"}
    """
    items = _ordered(sched)
    bs = [b for b in _bounds(items, timers, ch, now.date() - dt.timedelta(days=LOOKBACK_DAYS), now.date())
          if b[0] <= now]
    if not bs:
        return None
    t, _, _, kind, src = max(bs, key=lambda b: b[:3])
    active = _active_windows(items, ch, now)
    if kind == "end":
        if active:
            it, s = active[0]
            return {"key": "start:%s@%s" % (it["id"], s.isoformat()), "kind": "start", "value": item_value(it),
                    "source": it, "in_window": True}
        keep = ch == "limit" and src.get("revert") is False
        return {"key": "end:%s@%s" % (src["id"], t.isoformat()), "kind": "end",
                "value": item_value(src) if keep else "revert", "source": src, "in_window": False}
    value = src["actions"][ch] if kind == "timer" else item_value(src)
    return {"key": "%s:%s@%s" % (kind, src["id"], t.isoformat()), "kind": kind, "value": value, "source": src,
            "in_window": bool(active)}


def signature(d):
    """What a decision sets, ignoring which occurrence set it (Sat 00-24 then Sun 00-24 is one stretch)."""
    if d is None:
        return None
    v = d["value"]
    return (d["source"]["id"], "revert" if v == "revert" else repr(sorted(v.items()) if isinstance(v, dict) else v))


def next_change(sched, timers, now, channels=CHANNELS, horizon_days=8):
    """Next datetime any of `channels` changes what it's set to, within the horizon, or None."""
    items = _ordered(sched)
    marks = set()
    for ch in channels:
        for b in _bounds(items, timers, ch, now.date() - dt.timedelta(days=1),
                         now.date() + dt.timedelta(days=horizon_days)):
            if now < b[0] <= now + dt.timedelta(days=horizon_days):
                marks.add(b[0])
    cur = {ch: signature(desired(sched, timers, ch, now)) for ch in channels}
    for t in sorted(marks):
        if any(signature(desired(sched, timers, ch, t)) != cur[ch] for ch in channels):
            return t
    return None


# ---------------------------------------------------------------- proposals

_proposals = {}
_plock = threading.Lock()


def propose(mac, obj, kind="schedule"):
    pid = secrets.token_hex(8)
    with _plock:
        for k in [k for k, v in _proposals.items()
                  if (v["mac"] == mac and v["kind"] == kind) or time.time() - v["created"] > PROPOSAL_TTL]:
            del _proposals[k]
        _proposals[pid] = {"mac": mac, "obj": obj, "kind": kind, "created": time.time()}
    return pid


def take(pid, mac, kind="schedule"):
    with _plock:
        p = _proposals.pop(pid, None)
    if p is None or time.time() - p["created"] > PROPOSAL_TTL or p["kind"] != kind:
        raise ScheduleError("this %s is no longer pending; save it again" % kind)
    if p["mac"] != mac:
        raise ScheduleError("this %s was for a different board" % kind)
    return p["obj"]


# ---------------------------------------------------------------- runner

EMPTY_CH = {"key": "none", "sig": None, "baseline": None, "wrote": None, "value": None, "override": None}


def _same(ch, a, b):
    if a is None or b is None:
        return a is b
    if ch == "limit":
        return abs(a["end"] - b["end"]) < 0.0005 and abs(a["start"] - b["start"]) < 0.0005
    return a == b


class Scheduler:
    """Background thread applying every board's schedule and timers.

    Per board and channel, the run state is saved in boards.json ("schedule_run") so it
    survives restarts and disconnects:
      key       the boundary deciding the channel now ("start:<id>@<time>", ...) or "none"
      baseline  what the board had before the first window changed it (volts, or 0/1)
      wrote     what the scheduler last set, to tell its own value from a manual change
      value     the value in force (for re-sending a pause)
      override  a key you overrode by hand (the schedule leaves that channel alone until the key changes)
    """

    def __init__(self, app, clock=None):
        self.app = app
        self.clock = clock or dt.datetime.now
        self.state = {}  # mac -> transient: {"last_pause", "conn", "error", "retry", "last"}
        self._locks = {}  # mac -> RLock around every read-modify-write of that board's schedule_run
        self._locks_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def lock(self, mac):
        """The board's schedule lock. Held for a whole tick (writes included) and by every manual override, so a
        tick can't save a stale copy of schedule_run over an override made while it was writing."""
        with self._locks_lock:
            return self._locks.setdefault(mac, threading.RLock())

    def forget(self, b):
        """Drop the transient state, so the next tick re-decides everything."""
        with self.lock(b.mac):
            self.state.pop(b.mac, None)

    def _run(self):
        while not self._stop.is_set():
            for b in list(self.app.boards.values()):
                st = self._st(b.mac)
                if time.time() < st.get("crash_until", 0.0):
                    continue
                try:
                    with self.lock(b.mac):
                        self.housekeeping(b)
                        self.tick(b)
                except Exception as e:  # never let one board stop the scheduler; back off instead of every 2 s
                    st["error"] = "%s: %s" % (type(e).__name__, e)
                    st["crash_until"] = time.time() + CRASH_BACKOFF_S
            self._stop.wait(TICK_S)

    def housekeeping(self, b, now=None):
        """Safety notices (every SAFETY_S) and a storage-off that was asked for while the board was away."""
        if not b.mac:
            return
        st = self._st(b.mac)
        t = time.time() if now is None else now
        if t - st.get("safety_t", 0.0) >= SAFETY_S:
            st["safety_t"] = t
            safety.evaluate(b)
        stor = b.profile().get("storage") or {}
        if stor.get("off_pending") and b.state == "online" and b.config is not None \
                and safe_write.risk_accepted(b.profile()) and not self._retry_wait(st, "storage", t):
            try:
                storage.apply(b, False, via={"kind": "pending"}, app=self.app)
            except (safe_write.WriteRefused, storage.StorageError) as e:
                self._failed(b, st, "storage", "storage mode off", e, t)

    def _st(self, mac):
        return self.state.setdefault(mac, {"last_pause": 0.0, "conn": None, "error": "", "retry": {}, "last": ""})

    @staticmethod
    def run_state(b):
        saved = b.profile().get("schedule_run") or {}
        return {ch: dict(EMPTY_CH, **(saved.get(ch) or {})) for ch in CHANNELS}

    @staticmethod
    def _save(b, run):
        b.boards.update(b.mac, schedule_run=run)

    @staticmethod
    def timers(b):
        return list(b.profile().get("timers") or [])

    def _now_epoch(self, now):
        return now.timestamp()

    # -- status for the UI

    def status(self, b):
        if not b.mac:
            return None
        sched = b.profile().get("schedule")
        timers = self.timers(b)
        if not sched and not timers:
            return None
        now = self.clock()
        st = self._st(b.mac)
        run = self.run_state(b)
        chans = {}
        for ch in CHANNELS:
            d = desired(sched, timers, ch, now)
            if d is None:
                continue
            nxt = next_change(sched, timers, now, channels=(ch,))
            src = d["source"]
            val = d["value"]
            if val == "revert":
                val = run[ch]["baseline"] if ch == "limit" else (run[ch]["baseline"] if run[ch]["baseline"] is not None
                                                                 else 1)
                if ch == "limit" and isinstance(val, dict):
                    val = {"volts": val, "pct": None}
            chans[ch] = {"source": "Timer: " + src["label"] if d["kind"] == "timer" else src["name"],
                         "kind": d["kind"], "value": val, "reverted": d["value"] == "revert",
                         "until_t": nxt.timestamp() if nxt else None,
                         "override": run[ch]["override"] == d["key"]}
        nxt = next_change(sched, timers, now) if (live(sched) or timers) else None
        ch = chans.get("charge")
        return {"enabled": bool(sched and sched.get("enabled")), "stale": bool(sched and sched.get("stale")),
                "configured": bool(sched), "channels": chans, "next_t": nxt.timestamp() if nxt else None,
                "paused_by_schedule": bool(ch and ch["value"] == 0 and not ch["override"]),
                "timers": [t for t in timers if t["at"] > self._now_epoch(now)],
                "error": st["error"], "last": st["last"]}

    def override(self, b, ch, value):
        """You changed `ch` by hand: the schedule leaves it alone until its next boundary.
        True if the schedule or a timer was deciding that channel."""
        if not b.mac:
            return False
        with self.lock(b.mac):
            d = desired(b.profile().get("schedule"), self.timers(b), ch, self.clock())
            if d is None:
                return False
            run = self.run_state(b)
            run[ch]["override"] = d["key"]
            run[ch]["value"] = value
            self._save(b, run)
            return True

    def overriding(self, b, ch, value, fn):
        """Mark `ch` as changed by hand, then run fn() (the write). If fn raises, the override is taken back, so a
        refused or failed write doesn't leave the schedule standing aside for a change that never happened.
        The lock is held throughout, so the scheduler can't write the schedule's value in the middle.
        Returns (overrode the schedule?, fn's result)."""
        if not b.mac:
            return False, fn()
        with self.lock(b.mac):
            before = (b.profile().get("schedule_run") or {}).get(ch)
            hit = self.override(b, ch, value)
            try:
                return hit, fn()
            except BaseException:
                if hit:
                    run = self.run_state(b)
                    run[ch] = dict(EMPTY_CH, **(before or {}))
                    self._save(b, run)
                raise

    # -- the loop body

    def tick(self, b):
        if b.state != "online" or not b.mac or b.config is None:
            return
        if not safe_write.risk_accepted(b.profile()):
            return  # no unattended commands to a board whose safety warning hasn't been accepted
        if storage.is_on(b.profile()):
            return  # storage mode holds the charge limit and balancing; the schedule waits until it's off
        sched = b.profile().get("schedule")
        timers = self.timers(b)
        st = self._st(b.mac)
        run = self.run_state(b)
        now, t = self.clock(), time.time()
        reconnected = st["conn"] != b.connect_count
        st["conn"] = b.connect_count
        deciding = set()
        for ch in CHANNELS:
            d = desired(sched, timers, ch, now)
            if d is not None and d["kind"] == "timer":
                deciding.add(d["source"]["id"])
            self._tick_ch(b, ch, d, run, st, now, t, reconnected)
        # A timer that has fired and no longer decides any channel can never matter again.
        keep = [x for x in timers if x["at"] > self._now_epoch(now) or x["id"] in deciding]
        if len(keep) != len(timers):
            b.boards.update(b.mac, timers=keep)

    def _tick_ch(self, b, ch, d, run, st, now, t, reconnected):
        r = run[ch]
        if d is None:
            if r["key"] != "none":
                # Nothing decides this channel any more (schedule turned off, items deleted): give it back.
                target = r["baseline"]
                if target is None and ch != "limit" and r["wrote"] == 0:
                    target = 1
                if target is not None and r["wrote"] is not None and r["override"] != r["key"]:
                    why = "no schedule item applies now" if live(b.profile().get("schedule")) else "schedule off"
                    if not self._revert(b, ch, r, target, why, st, t, now):
                        return
                run[ch] = dict(EMPTY_CH)
                self._save(b, run)
            return

        name = ("timer (%s)" % d["source"]["label"]) if d["kind"] == "timer" else d["source"]["name"]
        sig = list(signature(d))
        if r["key"] != d["key"] and r["sig"] == sig and d["value"] != "revert":
            # The same item carries on (an all-day window into the next day): nothing to apply.
            r["override"] = d["key"] if r["override"] == r["key"] else r["override"]
            r["key"] = d["key"]
            if not d["in_window"]:
                r["baseline"] = None  # a limit window that keeps its limit afterwards
            self._save(b, run)
        if r["key"] != d["key"]:
            if d["in_window"] and r["baseline"] is None and r["wrote"] is None:
                # First window to touch this channel: remember what the board has now, before changing it.
                r["baseline"] = self._read(b, ch)
                self._save(b, run)
            if d["value"] == "revert":
                if r["baseline"] is not None and r["wrote"] is not None and r["override"] != r["key"]:
                    if not self._revert(b, ch, r, r["baseline"], "%s ended" % name, st, t, now):
                        return
                r.update(baseline=None, wrote=None, value=None)
            else:
                if not self._apply(b, ch, d["value"], name, st, t, now):
                    return
                r["wrote"] = self._as_board(ch, d["value"])
                r["value"] = d["value"] if ch != "limit" else None
                if not d["in_window"]:
                    r["baseline"] = None
            if d["kind"] == "timer":
                b._event("timer", "timer fired: %s" % d["source"]["label"])
            r["key"], r["sig"], r["override"] = d["key"], sig, None
            self._save(b, run)
            st["error"] = ""
            return

        if ch == "charge" and r["value"] == 0 and r["override"] != d["key"]:
            if reconnected or t - st["last_pause"] > PAUSE_RESEND_S:
                b.send_runtime("charge_allowed", 0, "by schedule (%s)" % name, quiet=True)
                st["last_pause"] = t

    # -- per-channel actions

    def _read(self, b, ch):
        if ch == "limit":
            return b.read_charge_fresh()
        if ch == "charge":
            rt = b.runtime_state.get("charge_allowed")
            return rt["value"] if rt else 1
        return 1 if safe_write.read_balance_current(b) > 0 else 0

    @staticmethod
    def _as_board(ch, value):
        return value["volts"] if ch == "limit" else value

    def _revert(self, b, ch, r, target, why, st, t, now):
        """Put back `target` unless the board no longer holds what the schedule wrote. False = retry later."""
        cur = self._read(b, ch)
        if not _same(ch, cur, r["wrote"]):
            b._event("schedule", "%s was changed by hand; keeping it" % {"limit": "the charge limit", "charge":
                     "charging pause/allow", "balance": "balancing"}[ch])
            st["last"] = "%s: kept your manual %s" % (store.clock12(now, day=True),
                                                      {"limit": "limit", "charge": "charging setting",
                                                       "balance": "balancing setting"}[ch])
            return True
        if ch == "limit":
            return self._write(b, target, "%s, back to the limit from before" % why, st, t, now)
        if ch == "charge":
            if target == 0:
                b._event("schedule", "charging stays paused: you had paused it before the schedule did")
            else:
                b.send_runtime("charge_allowed", 1, "by schedule (%s)" % why)
            return True
        return self._balance(b, bool(target), "%s, balancing %s again" % (why, "allowed" if target else "off"),
                             st, t, now)

    def _apply(self, b, ch, value, name, st, t, now):
        if ch == "limit":
            return self._write(b, value["volts"], "%s (%d%%)" % (name, value["pct"]), st, t, now)
        if ch == "charge":
            b.send_runtime("charge_allowed", value, "by schedule (%s)" % name)
            st["last_pause"] = t
            st["last"] = "%s: %s charging" % (store.clock12(now, day=True), "allowed" if value else "paused")
            return True
        return self._balance(b, bool(value), "%s (balancing %s)" % (name, "allowed" if value else "off"),
                             st, t, now)

    def _retry_wait(self, st, key, t):
        return t < st["retry"].get(key, 0.0)

    def _failed(self, b, st, key, label, e, t):
        st["error"] = "couldn't apply %s: %s" % (label, e)
        st["retry"][key] = t + WRITE_RETRY_S
        b._event("schedule", st["error"])
        return False

    def _write(self, b, v, label, st, t, now):
        """Write Charge Start/End unless the board already has them. False = not done (retried later)."""
        if self._retry_wait(st, "limit", t):
            return False
        if _same("limit", b.read_charge_fresh(), v):
            return True
        try:
            safe_write.apply_scheduled(b, v["start"], v["end"], "schedule: %s" % label)
        except safe_write.WriteRefused as e:
            return self._failed(b, st, "limit", label, e, t)
        b._event("schedule", "%s: end %.3f / start %.3f V" % (label, v["end"], v["start"]))
        st["last"] = "%s: %s" % (store.clock12(now, day=True), label)
        st["error"] = ""
        st["retry"].pop("limit", None)
        return True

    def _balance(self, b, allow, label, st, t, now):
        if self._retry_wait(st, "balance", t):
            return False
        try:
            res = safe_write.apply_balance(b, allow, "schedule: %s" % label)
        except safe_write.WriteRefused as e:
            return self._failed(b, st, "balance", label, e, t)
        if res is not None:
            b._event("schedule", label)
            st["last"] = "%s: %s" % (store.clock12(now, day=True), label)
        st["error"] = ""
        st["retry"].pop("balance", None)
        return True


# ---------------------------------------------------------------- text

def log_schedule_saved(b, sched):
    store.append_log("changes", {"event": "schedule", "mac": b.mac, "host": b.host,
                                 "name": b.profile().get("nickname"), "enabled": sched["enabled"],
                                 "rules": [describe(r) for r in sched["items"]]})


def _hm12(s):
    m = _minutes(s)
    if m == 1440:
        return "midnight"
    h, mi = divmod(m, 60)
    return "%d:%02d %s" % (h % 12 or 12, mi, "AM" if h < 12 else "PM")


def describe_action(action, pct=None):
    return {"limit": "charge to %s%%" % pct, "charge_off": "no charging", "charge_on": "allow charging",
            "balance_off": "no balancing", "balance_on": "allow balancing"}[action]


def describe(r):
    when = (",".join(DAY_NAMES[d] for d in r["days"]) if r["repeat"] == "weekly" else r["date"])
    if r["kind"] == "event":
        return "%s: %s at %s, %s" % (r["name"], when, _hm12(r["start"]), describe_action(r["action"], r.get("pct")))
    what = describe_action(r["action"], r.get("pct"))
    if r["action"] == "limit" and r.get("revert") is False:
        what += " (kept afterwards)"
    return "%s: %s %s-%s, %s" % (r["name"], when, _hm12(r["start"]), _hm12(r["end"]), what)


def describe_timer(actions):
    parts = []
    if "charge" in actions:
        parts.append("start charging" if actions["charge"] else "stop charging")
    if "limit" in actions:
        parts.append(("to %d%%" if "charge" in actions and actions["charge"] else "set limit %d%%")
                     % actions["limit"]["pct"])
    if "balance" in actions:
        parts.append("allow balancing" if actions["balance"] else "stop balancing")
    return ", ".join(parts)

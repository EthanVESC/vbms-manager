// Shared helpers, API access and app-wide state.

export const TOKEN = window.VBMS_TOKEN;
export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fx = (d) => (v) => (v == null || isNaN(v) ? "–" : Number(v).toFixed(d));
export const f1 = fx(1), f2 = fx(2), f3 = fx(3);
export const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

// ---------------------------------------------------------------- times: always 12-hour with AM/PM

const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const ampm = (h, m) => `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
// "22:05" -> "10:05 PM" ("24:00" -> "midnight")
export const hm12 = (s) => { if (!s) return ""; if (s === "24:00") return "midnight"; const [h, m] = s.split(":").map(Number); return ampm(h, m); };
// Date or epoch seconds -> "10:05 PM"; day: "Mon 10:05 PM"
export function clock12(t, day = false) {
  const d = t instanceof Date ? t : new Date(t * 1000);
  return (day ? WD[d.getDay()] + " " : "") + ampm(d.getHours(), d.getMinutes());
}
// Like clock12(t, true) but "today"/"tomorrow" when it's close.
export function whenAt(t) {
  const d = new Date(t * 1000), now = new Date();
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86400000);
  return days === 0 ? clock12(d) : days === 1 ? "tomorrow " + clock12(d) : clock12(d, true);
}
// "2026-10-29 22:05:01" (server log time) -> "Oct 29, 10:05:01 PM"; today -> "10:05:01 PM"
export function stamp12(s) {
  const m = /^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?/.exec(s || "");
  if (!m) return s || "";
  const [, y, mo, dd, h, mi, se] = m.map((x, i) => (i ? Number(x) : x));
  const t = ampm(h, mi).replace(" ", se != null && !isNaN(se) ? `:${String(se).padStart(2, "0")} ` : " ");
  const today = new Date();
  if (today.getFullYear() === y && today.getMonth() + 1 === mo && today.getDate() === dd) return t;
  return `${MO[mo - 1]} ${dd}${y !== today.getFullYear() ? " " + y : ""}, ${t}`;
}
// "22:05:01" (time of day only) -> "10:05:01 PM"
export const hms12 = (s) => { const m = /^(\d\d):(\d\d)(?::(\d\d))?$/.exec(s || ""); return m ? ampm(+m[1], +m[2]).replace(" ", m[3] ? `:${m[3]} ` : " ") : s || ""; };

// Cell voltage scale used everywhere (the user asked for 3.0–4.2 V).
export const V_LO = 3.0, V_HI = 4.2;
export const vPct = (v) => clamp((v - V_LO) / (V_HI - V_LO), 0, 1) * 100;

export const S = {
  state: null,        // /api/state
  detail: new Map(),  // host -> /api/board
  route: { board: null, tab: "overview" },
};

export const canWrite = () => !!(S.state && S.state.access && S.state.access.can_write);
// Preferences → Skip confirmations: routine writes and live commands go straight through.
export const skipConfirm = () => !!(S.state && S.state.settings && S.state.settings.confirm_skip && S.state.settings.confirm_skip.on);
// window.confirm, unless confirmations are skipped.
export const ask = (q) => skipConfirm() || window.confirm(q);
export const dis = () => (canWrite() ? "" : " disabled");
export const boardName = (b) => b.nickname || (b.fw ? b.fw.hw_name : b.ble_only ? "Bluetooth board" : b.host);

export async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-VBMS-Token": TOKEN }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  let data;
  try { data = await r.json(); } catch { data = { error: "bad response (" + r.status + ")" }; }
  if (!r.ok || data.error) throw new Error(data.error || "request failed");
  return data;
}

// Alert thresholds live in VBMS Manager's settings (shared by every device); older versions kept them in this browser.
export function alertCfg() {
  const d = { delta: 30, temp: 55, over: 30, notify: false, charge_done: false, charge_early: false, offline_charging: false };
  if (S.state && S.state.settings && S.state.settings.alerts_saved) return Object.assign(d, S.state.settings.alerts);
  try { return Object.assign(d, JSON.parse(localStorage.getItem("vbms_alerts") || "{}")); } catch { return d; }
}

// Staying connected keeps the BMS awake. Shown on the dashboard, the Connection panel and the safety step.
export const AWAKE_WARN = "While VBMS Manager is connected to a board, its BMS never goes to sleep. It keeps powering itself " +
  "from the pack, so it will drain your battery over the course of weeks: in practice a few percent per day. If the board is " +
  "left plugged in, the charger will also kick on for about 30 seconds roughly twice a day to top the pack back up.";
export const AWAKE_FIX = "Before you store the board or leave it for days, turn on Storage mode (it lets the BMS sleep once it's " +
  "settled), quit VBMS Manager, or use Free for VESC Tool on the board's Board tab. A board whose SOC stays very low is " +
  "disconnected automatically so it can sleep (Preferences → Safety).";

export function store(key, val) {
  try { if (val === undefined) return JSON.parse(localStorage.getItem(key) || "null"); localStorage.setItem(key, JSON.stringify(val)); } catch { return null; }
}

// Mirrors charge_limit.compute for previews only; the server recomputes and checks.
export function endFor(p, cell) {
  if (!cell) return null;
  if (p >= 100) return cell.max_end;
  const pts = cell.curve.map(([a, v]) => [Number(a), Number(v)]).sort((x, y) => x[0] - y[0]);
  for (let i = 0; i < pts.length - 1; i++) {
    const [p0, v0] = pts[i], [p1, v1] = pts[i + 1];
    if (p >= p0 && p <= p1) return Math.round((v0 + (v1 - v0) * (p - p0) / (p1 - p0)) * 1000) / 1000;
  }
  return null;
}

export function cellsCount(b) {
  return b.values ? b.values.cells.length : b.cells_configured || 0;
}

// Per-cell problem level: "" | "warn" | "bad"
export function cellLevel(b, v, median) {
  const a = alertCfg();
  const ch = b.charge || {}, lim = b.limits_cfg || {};
  if (v > V_HI + 0.0005) return "bad";
  // Above Charge End only matters while charging (the BMS should have stopped); resting above a
  // limit that was just lowered is normal.
  if (b.values && b.values.is_charging && ch.vc_charge_end != null && v > ch.vc_charge_end + a.over / 1000) return "bad";
  if (lim.vc_empty != null && v < lim.vc_empty) return "bad";
  if (median != null && Math.abs(v - median) * 1000 > a.delta) return "warn";
  return "";
}

export function median(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export function boardAlerts(b) {
  const out = [];
  const v = b.values, a = alertCfg();
  // Safety notices come from the server and also cover boards that are asleep or offline.
  for (const n of b.notices || []) out.push({ k: "n_" + n.key, t: n.text, bad: n.level === "bad", notice: n.level });
  if (!v || b.state !== "online") return out;
  if (v.charge_fault) out.push({ k: "fault", t: "Charge over-current fault latched (FLT_CHG_OC). Unplug the charger for 5 s to clear.", bad: true });
  if (v.cell_delta_mv != null && v.cell_delta_mv > a.delta) out.push({ k: "delta", t: `Cell imbalance ${f1(v.cell_delta_mv)} mV (alert above ${a.delta} mV)` });
  const tmax = Math.max(...(v.temps || []).map((t) => t.value), -999);
  if (tmax > a.temp) out.push({ k: "temp", t: `Temperature ${f1(tmax)} °C (alert above ${a.temp} °C)` });
  if (b.charge && v.is_charging && v.cell_max > b.charge.vc_charge_end + a.over / 1000)
    out.push({ k: "over", t: `Charging, but max cell ${f3(v.cell_max)} V is ${Math.round((v.cell_max - b.charge.vc_charge_end) * 1000)} mV above charge end ${f3(b.charge.vc_charge_end)} V` });
  const ss = b.schedule_status;
  if (ss && ss.error) out.push({ k: "sched", t: "Schedule: " + ss.error, bad: true });
  const lim = b.limits_cfg || {};
  if (lim.balance_max_current === 0) out.push({ k: "nobal", t: "Balancing is off (Balance Max Current is 0 A)." });
  return out;
}

export function stateChip(b) {
  const v = b.values;
  if (b.state === "online") {
    if (v && v.charge_fault) return ["Fault", "bad"];
    if (v && v.is_charging) return [v.is_balancing ? "Charging · Balancing" : "Charging", "info"];
    if (b.runtime && b.runtime.charge_allowed && b.runtime.charge_allowed.value === 0) return ["Charging paused", "warn"];
    if (v && v.is_balancing) return ["Balancing", "warn"];
    if (v && v.v_charge > 5) return ["Charger connected", "ok"];
    if (b.storage && b.storage.on) return ["Storage mode", "info"];
    return ["Online", "ok"];
  }
  if (b.state === "resting") return b.rest && b.rest.reason === "storage" ? ["Storage · sleeping", "info"] : ["Sleeping (low SOC)", "warn"];
  return ({ connecting: ["Connecting…", ""], searching: ["Looking over Bluetooth…", ""], busy: ["Busy (another app)", "warn"], offline: ["Offline", ""],
    paused: ["Freed for VESC Tool", "warn"], error: ["Error", "bad"] })[b.state] || [b.state, ""];
}

// ---------------------------------------------------------------- compact indicator states

export const cellTemps = (v) => (v ? v.temps.filter((t) => /^Cell sensor|^Sensor/.test(t.name)) : []);
export const avg = (xs) => (xs.length ? xs.reduce((a, x) => a + x, 0) / xs.length : null);

export function maxTemp(v) {
  const real = v ? v.temps.filter((t) => !/^Cell (min|max)$/.test(t.name)) : [];
  return real.length ? real.reduce((a, t) => (t.value > a.value ? t : a)) : null;
}

// What the charging indicator shows and what a tap does.
export function chargeState(b) {
  const v = b.values, ss = b.schedule_status || {}, ch = b.charge || {};
  if (!v || b.state !== "online") return { k: "offline", tip: "Board offline" };
  const rt = b.runtime && b.runtime.charge_allowed;
  if (v.is_charging) return { k: "charging", tip: `Charging at ${f2(Math.abs(v.i_in_ic))} A · tap to stop` };
  if (ss.paused_by_schedule) { const c = ss.channels.charge; return { k: "paused_sched", tip: `Paused by ${c.source}${c.until_t ? " until " + whenAt(c.until_t) : ""} · tap to allow charging` }; }
  if (rt && rt.value === 0) return { k: "paused_user", tip: `You paused charging (${rt.t ? clock12(rt.t) : hms12(rt.time)}) · tap to allow it again` };
  const charger = v.v_charge > 5;
  if (charger && ch.vc_charge_start != null && v.cell_max >= ch.vc_charge_start)
    return { k: "held", tip: `Charger connected. Not charging: highest cell ${f3(v.cell_max)} V is at or above Charge Start ${f3(ch.vc_charge_start)} V · tap to pause charging` };
  if (charger) return { k: "idle", tip: "Charger connected, not charging (the BMS is holding off: check temperatures and faults) · tap to pause charging" };
  return { k: "none", tip: "No charger connected · tap to pause charging" };
}

// Whether charging / balancing is allowed right now, and why (for the switches).
export function allowState(b, kind) {
  const ss = b.schedule_status, c = ss && ss.channels && ss.channels[kind];
  const cause = c && !c.override ? `${c.source.startsWith("Timer") ? c.source : "schedule: " + c.source}${c.until_t ? " until " + whenAt(c.until_t) : ""}` : "";
  if (kind === "charge") {
    const rt = b.runtime && b.runtime.charge_allowed;
    const on = !(rt && rt.value === 0);
    // Match the prefix only: a manual change can say "by you, overriding the schedule …".
    const why = (rt && rt.why) || "";
    const who = !rt ? "" : why.startsWith("by API") ? "API" : !why.startsWith("by schedule") ? "you" : /\(Timer:/.test(why) ? "timer" : "schedule";
    return { on, cause: c && c.value === (on ? 1 : 0) ? cause : rt ? `${who}, ${rt.t ? clock12(rt.t) : hms12(rt.time)}` : "" };
  }
  const lim = b.limits_cfg || {};
  if (lim.balance_max_current == null) return { on: null, cause: "" };
  const on = lim.balance_max_current > 0;
  return { on, cause: c && c.value === (on ? 1 : 0) ? cause : on ? "" : "Balance Max Current is 0 A" };
}

export function balanceState(b) {
  const v = b.values;
  if (!v || b.state !== "online") return { k: "offline", n: 0, tip: "Board offline" };
  const forced = !!(b.runtime && b.runtime.force_balance && b.runtime.force_balance.value === 1);
  const n = v.balancing.filter(Boolean).length;
  if (forced) return { k: "forced", n, tip: `Force balance is on${n ? `, ${n} cells bleeding` : ""} · tap to go back to automatic` };
  if (n) return { k: "bleeding", n, tip: `Balancing: ${n} cells bleeding (automatic) · tap to force balance on` };
  return { k: "auto", n, tip: "Not balancing; the script balances automatically when its conditions are met · tap to force balance on" };
}

// Per-viewer UI state: which compact views are expanded (host|cells, host|temps).
const uiOpen = new Set(store("vbms_ui_open") || []);
export const isOpen = (k) => uiOpen.has(k);
export function toggleOpen(k) {
  if (uiOpen.has(k)) uiOpen.delete(k); else uiOpen.add(k);
  store("vbms_ui_open", [...uiOpen]);
}

let toastTimer = 0;
// action: optional {label, run} shown as a button in the toast (e.g. "View in log").
export function toast(text, action) {
  let el = $("#toast");
  if (!el) { el = document.createElement("div"); el.id = "toast"; el.className = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
  el.innerHTML = `<span>${esc(text)}</span>${action ? `<button type="button">${esc(action.label)}</button>` : ""}`;
  if (action) el.querySelector("button").onclick = () => { el.hidden = true; action.run(); };
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), action ? 7000 : 4000);
}

export function showMsg(title, html) {
  $("#msgTitle").textContent = title;
  $("#msgBody").innerHTML = html;
  if (!$("#msgDlg").open) $("#msgDlg").showModal();
}

export function fail(e) {
  showMsg("Not done", `<p>${esc(e.message)}</p><p class="muted">Nothing was written to the board unless this message says otherwise.</p>`);
}

// main.js fills these in; other modules call them after changing something.
export const hooks = { refresh: async () => {}, reloadDetail: async () => {}, rerender: () => {}, openLog: () => {} };

// Put a tooltip next to rect r (a DOMRect or {left, right, top, bottom}) without covering it:
// above if it fits, otherwise below, centred and kept on screen.
export function placeTip(el, r) {
  const w = el.offsetWidth, h = el.offsetHeight, gap = 8;
  const x = Math.min(window.innerWidth - w - gap, Math.max(gap, (r.left + r.right) / 2 - w / 2));
  const y = r.top - h - gap >= gap ? r.top - h - gap : Math.min(r.bottom + gap, window.innerHeight - h - gap);
  el.style.left = x + "px";
  el.style.top = y + "px";
}

// Allow switches with a command on its way: "host|charge" -> the state it's switching to.
// They draw in that position with a spinner and ignore taps until the board has answered.
export const switching = new Map();

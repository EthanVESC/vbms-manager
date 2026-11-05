// One renderer per tab. Each builds a skeleton once, then fills slots; a slot
// is only touched when its markup changes, so hovering and typing aren't disturbed.

import { setInner, AWAKE_FIX, AWAKE_WARN, alertCfg, allowState, avg, switching, balanceState, boardAlerts, boardName, canWrite, cellLevel, cellsCount, cellTemps, chargeState, clamp, clock12, dis, esc, f1, f2, f3, hms12, isOpen, maxTemp, median, stamp12, stateChip, TOKEN, V_HI, V_LO, vPct } from "./core.js";
import { applyBar, editable, isPending, lockBtn, pv } from "./drag.js";
import { readOnlyList, renderForm } from "./forms.js";
import { scheduleSummary, renderSchedule } from "./schedule.js";
import { cellStrip, indicator, miniBattery, rangeGauge, socRing, spreadMeter, tempList, thermo } from "./viz.js";
import { clock, dur, when } from "./charts.js";
import { chartSlot, drawPanel, fetchCached, rangeChips, recordingLine } from "./history.js";
import { keyPanel } from "./key.js";
import { addonPanel, chargeBlockers, currentPct, timerList } from "./writes.js";

function skeleton(root, key, html) {
  if (root.dataset.view !== key) { root.dataset.view = key; root.innerHTML = html; return true; }
  return false;
}

function slot(root, name, html) {
  const el = root.querySelector(`[data-slot="${name}"]`);
  if (el && el._html !== html) { el._html = html; setInner(el, html); }
  return el;
}

const group = (d, id) => d && d.editable && d.editable.groups.find((g) => g.id === id);
const noData = (b) => `<div class="muted">${b.state === "online" ? "Reading…" : "No live data. " + esc(b.error || "")}</div>`;

// ---------------------------------------------------------------- shared bits

export function banner(b) {
  const v = b.values;
  const msgs = [];
  if (b.state !== "online" && b.error) msgs.push({ t: b.error });
  const bt = linkHint(b) + setupHint(b);
  if (b.state === "online" && b.cells_configured && v && b.cells_configured !== v.cells.length)
    msgs.push({ t: `Config says ${b.cells_configured} cells but the BMS reports ${v.cells.length}.`, bad: true });
  if (b.values_age != null && b.values_age > 5 && b.state === "online") msgs.push({ t: `Live data is ${b.values_age}s old. The board is slow to answer over ${b.link === "ble" ? "Bluetooth" : "WiFi"}; still trying.` });
  msgs.push(...boardAlerts(b));
  if (!msgs.length) return bt ? `<div class="banner">${bt}</div>` : "";
  return `<div class="banner${msgs.some((m) => m.bad) ? " bad" : ""}">${msgs.length === 1 ? esc(msgs[0].t) : "<ul>" + msgs.map((m) => `<li>${esc(m.t)}</li>`).join("") + "</ul>"}${bt ? "<br>" + bt : ""}</div>`;
}

// Board setup was closed before the end: say so, with a way back in.
function setupHint(b) {
  if (!b.mac || b.setup_done !== false || !canWrite()) return "";
  return `${b.risk_ok === false ? "Board setup isn't finished: VBMS Manager only reads from this board until it is." : "Board setup isn't finished."} <button type="button" class="small" data-act="wizard" data-host="${esc(b.host)}">Finish board setup ›</button>`;
}

// Offer Bluetooth when a WiFi board can't be reached. Switching is always by hand.
function linkHint(b) {
  if (!b.ble || b.ble_only || b.link === "ble" || !b.link_known || !["offline", "error"].includes(b.state)) return "";
  return `Can't reach it over WiFi? <button type="button" class="small" data-act="link-ble"${dis()}>Connect over Bluetooth</button>`;
}

const BT_ICON = `<svg class="bticon" viewBox="0 0 12 18" aria-hidden="true"><path d="M2 5l8 7-4 4V2l4 4-8 7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

function connPanel(b) {
  const over = b.link === "ble" ? `${BT_ICON} <b>Bluetooth</b>` : `<b>WiFi</b> <span class="mono">${esc(b.host)}</span>`;
  const st = b.state === "online" ? "Connected over" : b.state === "searching" ? "Looking for it over" : "Using";
  let sw = "";
  if (!b.ble_only && b.link === "ble") sw = `<button type="button" class="ghost" data-act="link-wifi"${dis()}>Back to WiFi</button>`;
  else if (!b.ble_only && b.ble && b.link_known) sw = `<button type="button" class="ghost" data-act="link-ble"${dis()}>Connect over Bluetooth</button>`;
  const note = b.link === "ble"
    ? `It stays on Bluetooth until you switch back${b.ble_only ? " (it was added over Bluetooth; add its IP to use WiFi)" : ""}. The Mac must be within Bluetooth range. While it's connected here, VESC Tool can't connect to it over Bluetooth, but it can over WiFi.`
    : `A board talks to one app at a time over WiFi. While it's connected here, VESC Tool can't connect over WiFi${b.ble ? ", but it can over Bluetooth" : ""}.${b.ble && !b.link_known ? " Bluetooth is offered once the board has connected over WiFi once." : ""}`;
  const r = b.rest;
  const rest = r ? `<div class="banner small"><b>${r.reason === "storage" ? "Storage mode: letting the BMS sleep." : "Disconnected so the BMS can sleep (low SOC)."}</b>
      Since ${esc(clock12(r.since, true))}. VBMS Manager checks in every ${f1(r.probe_s / 3600)} h (next ${esc(clock12(r.next, true))}) and stays connected if the charger is plugged in.</div>` : "";
  return `${rest}<p class="small">${r ? "Resting, normally over" : st} ${over}</p>
    <div class="row">${b.state === "paused" || r ? `<button type="button" data-act="resume"${dis()}>${r ? "Reconnect now" : "Reconnect"}</button>` : `<button type="button" class="ghost" data-act="pause"${dis()}>Free for VESC Tool</button>`}
    ${sw}<button type="button" class="ghost" data-act="remove"${dis()}>Remove board</button></div>
    <p class="muted small">${note}</p>
    <div class="risk caution small"><b>⚠ Keeps the BMS awake</b> ${esc(AWAKE_WARN)} Free for VESC Tool disconnects it so it can sleep. Storage mode and the low-SOC disconnect (Preferences → Safety) do it automatically.</div>`;
}

// Storage mode panel (Board tab): status, the per-board storage settings, on/off.
function storagePanel(b) {
  if (!b.mac) return `<div class="muted">Not read yet.</div>`;
  const st = b.storage, c = b.storage_cfg || {}, n = cellsCount(b), on = !!(st && st.on);
  const v = b.values;
  const status = !on ? `<p class="small">Off. Turning it on saves this board's Charge Start/End, Balance Min and Balance Max Current, then writes the storage settings below in one checked write. Turning it off puts the saved values back.</p>`
    : `<p><span class="chip info">Storage mode on</span> since ${esc(stamp12(st.since))}${st.off_pending ? ` · <b>turning off as soon as it's connected</b>` : ""}</p>
       ${v && v.cell_max > c.end_v + 0.02 ? `<p class="small">The pack is above storage voltage (highest cell ${f3(v.cell_max)} V). The BMS can't discharge it, so it stays there until you ride it down.</p>` : ""}
       <p class="muted small">Schedules and timers wait while storage mode is on. Saved for later: Charge ${f3(st.saved && st.saved.vc_charge_start)} / ${f3(st.saved && st.saved.vc_charge_end)} V, Balance Min ${f3(st.saved && st.saved.vc_balance_min)} V, Balance Max Current ${f2(st.saved && st.saved.balance_max_current)} A.</p>`;
  const num = (id, val, min, max, step) => `<input type="number" id="${id}" value="${val}" min="${min}" max="${max}" step="${step}"${dis()}>`;
  return `${status}
    <div class="storegrid small">
      <label>Charge End per cell ${num("stEnd", f3(c.end_v), 3.6, 4.0, 0.01)} V${n ? ` <span class="muted">(${f1(c.end_v * n)} V pack)</span>` : ""}</label>
      <label>Start below End ${num("stGap", f3(c.gap), 0.05, 0.2, 0.01)} V <span class="muted">(Start ${f3(c.end_v - c.gap)} V)</span></label>
      <label>Balance Min ${num("stBal", f3(c.balance_min), 3.0, 4.2, 0.01)} V</label>
      <label class="check"><input type="checkbox" id="stBalOn"${c.balance ? " checked" : ""}${dis()}> Allow balancing in storage</label>
      <label class="check"><input type="checkbox" id="stSleep"${c.sleep ? " checked" : ""}${dis()}> Let the BMS sleep once there's no charger (checks in every few hours)</label>
    </div>
    <div class="row"><button type="button" class="ghost" data-act="storage-save"${dis()}>Save storage settings</button>
      <button type="button" data-act="${on ? "storage-off" : "storage-on"}"${dis()}>${on ? "Turn storage mode off…" : "Turn storage mode on…"}</button></div>
    <p class="muted small">Defaults: Charge End 3.75 V/cell, Start 0.100 V below, Balance Min 3.40 V with balancing allowed. ${on ? "Saving changed settings applies them at the next storage-mode switch; to apply now, turn it off and on." : ""}</p>`;
}

// "" for the standard 0.100 V gap; otherwise a note (red when outside 0.050–0.200 V).
function gapNote(ch) {
  if (ch.gap_ok) return "";
  const g = f3(ch.vc_charge_end - ch.vc_charge_start);
  return ch.gap_valid ? ` · <em class="info-t">gap ${g} V (custom)</em>` : ` · <em class="bad-t">gap ${g} V, outside 0.050–0.200</em>`;
}

function limitButton(b) {
  if (!b.charge) return `<div class="muted">Charge settings appear once the board has been read.</div>`;
  const pct = currentPct(b);
  const locked = chargeBlockers(b).length > 0;
  return `<button type="button" class="limitbtn" data-act="limit">
      <span class="lb-pct">${pct != null ? pct + "<small>%</small>" : f3(b.charge.vc_charge_end) + "<small> V</small>"}</span>
      <span class="lb-txt"><b>Charge limit</b><span>end ${f3(b.charge.vc_charge_end)} V · start ${f3(b.charge.vc_charge_start)} V${gapNote(b.charge)}</span>
      ${locked ? `<span class="muted small">Locked until board setup is finished</span>` : ""}
      ${b.values && !b.values.is_charging && b.values.cell_max > b.charge.vc_charge_end ? `<span class="muted small">Pack is above this limit, so it won't charge until the highest cell drops below ${f3(b.charge.vc_charge_start)} V.</span>` : ""}</span>
      <span class="chev" aria-hidden="true">›</span></button>`;
}

// Charge-stop limit for a sensor, by name (same mapping the BMS script uses).
function tempLimit(name, lim) {
  return /MOSFET/.test(name) ? lim.t_charge_max_mos : /^IC$/.test(name) ? lim.t_bal_max_ic : lim.t_charge_max;
}
const tempCls = (t, max) => (max != null && t >= max ? "bad" : max != null && t >= max - 5 ? "warn" : t > alertCfg().temp ? "bad" : "");

function tbar(name, value, max, cls = "", attrs = "") {
  const lo = -10, hi = 80, w = Math.max(2, Math.min(100, ((value - lo) / (hi - lo)) * 100));
  const c = max != null && value >= max ? "bad" : max != null && value >= max - 5 ? "warn" : "ok";
  return `<div class="tbar ${c} ${cls}"${attrs}><span class="tb-name">${name}</span><span class="tb-track"><i style="width:${w.toFixed(1)}%"></i></span><b>${f1(value)}°</b></div>`;
}

// Overview temperatures: cell sensors as one average row (tap: each sensor), then the others.
function tempBars(b) {
  const v = b.values;
  if (!v || !v.temps.length) return `<div class="muted">No temperature sensors reported.</div>`;
  const lim = b.limits_cfg || {}, cells = cellTemps(v), open = isOpen(b.host + "|temps");
  const others = v.temps.filter((t) => !/^Cell (min|max)$/.test(t.name) && !cells.includes(t));
  let rows = "";
  if (cells.length) {
    const av = avg(cells.map((t) => t.value));
    rows += `<button type="button" class="tbar-btn" data-act="temps" data-host="${esc(b.host)}" aria-expanded="${open}" data-tip="${open ? "Tap to show only the average" : `Average of ${cells.length} cell sensors · tap for each one`}">
      ${tbar(`Cells <small>${open ? "avg" : "avg of " + cells.length} ${open ? "▴" : "▾"}</small>`, av, lim.t_charge_max)}</button>`;
    if (open) rows += cells.map((t) => tbar(esc(t.name.replace(/^Cell sensor/, "Cell")), t.value, lim.t_charge_max, "sub")).join("");
  }
  rows += others.map((t) => tbar(esc(t.name), t.value, tempLimit(t.name, lim))).join("");
  return `<div class="tbars">${rows}</div>`;
}

// The row of small tap-able indicators used on the overview and the all-boards cards.
function indicators(b) {
  const v = b.values, h = b.host;
  if (!v) return "";
  const cs = chargeState(b), bs = balanceState(b), lim = b.limits_cfg || {}, a = alertCfg();
  const open = (k) => (isOpen(h + "|" + k) ? " open" : "");
  const chIcon = cs.k === "paused_user" ? "boltOff" : cs.k === "paused_sched" ? "boltClock" : "bolt";
  const chText = { charging: f1(Math.abs(v.i_in_ic)) + "<small>A</small>", paused_user: "paused", paused_sched: "sched", held: "held", idle: "idle", none: "", offline: "" }[cs.k];
  const chCls = { charging: "on pulse", paused_user: "warn", paused_sched: "warn", held: "", idle: "", none: "off", offline: "off" }[cs.k];
  const d = v.cell_delta_mv;
  const dCls = d > a.delta ? "bad" : lim.vc_balance_start != null && d > lim.vc_balance_start * 1000 ? "warn" : "ok";
  const ct = cellTemps(v), at = avg(ct.map((t) => t.value)), mt = maxTemp(v);
  return `<div class="inds">
    ${indicator(chIcon, chText, { act: "charge", host: h, cls: chCls, tip: cs.tip, disabled: !canWrite() })}
    ${indicator("scale", bs.k === "forced" ? "forced" : bs.k === "bleeding" ? String(bs.n) : "auto", { act: "balance", host: h, cls: bs.k === "forced" ? "on" : bs.k === "bleeding" ? "bal" : "", tip: bs.tip, disabled: !canWrite() })}
    ${indicator("delta", f1(d) + "<small>mV</small>", { act: "cells", host: h, cls: dCls + open("cells"), tip: `Cell spread ${f1(d)} mV (highest − lowest) · tap for all cells` })}
    ${at != null ? indicator("therm", f1(at) + "°", { act: "temps", host: h, cls: tempCls(at, lim.t_charge_max) + open("temps"), tip: `Cells average ${f1(at)} °C (${ct.length} sensors) · tap for each sensor` }) : ""}
    ${mt ? indicator("flame", f1(mt.value) + "°", { act: "temps", host: h, cls: tempCls(mt.value, tempLimit(mt.name, lim)), tip: `Hottest right now: ${mt.name} ${f1(mt.value)} °C` }) : ""}
  </div>`;
}

// Charging / balancing allowed: a switch that always sends the opposite when tapped.
export function allowSwitch(b, kind) {
  if (b.state !== "online" || !b.values) return "";
  const a = allowState(b, kind);
  if (a.on == null) return "";
  const label = kind === "charge" ? "Charging" : "Balancing";
  const to = switching.get(b.host + "|" + kind);
  if (to != null) {
    const doing = kind === "charge" ? (to ? "allowing…" : "pausing…") : (to ? "allowing… (writing flash)" : "turning off… (writing flash)");
    return `<button type="button" class="aswitch ${to ? "on" : "off"} busy" role="switch" aria-checked="${to}" aria-busy="true" aria-disabled="true"
      data-tip="Waiting for the board to confirm"><i aria-hidden="true"></i><span><b>${label}</b> ${doing}</span></button>`;
  }
  const state = a.on ? "allowed" : kind === "charge" ? "paused" : "off";
  const verb = a.on ? (kind === "charge" ? "pause charging" : "stop balancing (Balance Max Current 0 A)") : kind === "charge" ? "allow charging" : "allow balancing";
  return `<button type="button" class="aswitch ${a.on ? "on" : "off"}" role="switch" aria-checked="${a.on}" data-act="allow-${kind}" data-host="${esc(b.host)}"
    data-tip="${label} ${state}${a.cause ? " (" + esc(a.cause) + ")" : ""} · tap to ${verb}"${canWrite() ? "" : ' aria-disabled="true"'}><i aria-hidden="true"></i><span><b>${label}</b> ${state}${a.cause ? `<small>${esc(a.cause)}</small>` : ""}</span></button>`;
}

const laterBtn = (b, init = {}) => `<button type="button" class="ghost small" data-act="later" data-host="${esc(b.host)}" data-init="${esc(JSON.stringify(init))}"${dis()}>⏱ Later…</button>`;

function switches(b, kinds = ["charge", "balance"], later = true) {
  const sw = kinds.map((k) => allowSwitch(b, k)).join("");
  return sw ? `<div class="switches">${sw}${later ? laterBtn(b) : ""}</div>` : "";
}

// What the indicators expand into: all cells (ring or Δ tap) and/or each cell sensor (thermometer tap).
function expanded(b, { temps = true } = {}) {
  const v = b.values;
  if (!v) return "";
  let out = "";
  if (isOpen(b.host + "|cells")) {
    const med = median(v.cells);
    out += `<div class="xpand">${cellStrip(v.cells, { balancing: v.balancing, levels: v.cells.map((c) => cellLevel(b, c, med)) })}</div>`;
  }
  if (temps && isOpen(b.host + "|temps")) {
    const lim = b.limits_cfg || {}, cells = cellTemps(v);
    const others = v.temps.filter((t) => !/^Cell (min|max)$/.test(t.name) && !cells.includes(t));
    out += `<div class="xpand">${tempList(cells.map((t) => ({ name: t.name.replace(/^Cell sensor/, "Cell"), value: t.value })), { max: lim.t_charge_max })}
      ${others.length ? `<div class="muted small">Also: ${others.map((t) => `${esc(t.name)} ${f1(t.value)}°`).join(" · ")}</div>` : ""}</div>`;
  }
  return out;
}

function ring(b) {
  const v = b.values;
  return socRing(v ? v.soc : null, { limitPct: currentPct(b), charging: !!(v && v.is_charging), host: b.host });
}

function limitLine(b) {
  if (!b.charge) return "";
  const pct = currentPct(b);
  return `<button type="button" class="limitline" data-act="limit" data-host="${esc(b.host)}"><span class="ll-k">Charge limit</span><b>${pct != null ? pct + "%" : f3(b.charge.vc_charge_end) + " V"}</b>
    <span class="muted">end ${f3(b.charge.vc_charge_end)} · start ${f3(b.charge.vc_charge_start)} V${gapNote(b.charge)}</span><span class="chev" aria-hidden="true">›</span></button>`;
}

function heroStats(b) {
  const v = b.values;
  if (!v) return noData(b);
  const cur = -v.i_in_ic;
  const dir = cur > 0.05 ? "in" : cur < -0.05 ? "out" : "";
  const [chip, cls] = stateChip(b);
  return `<div class="statline"><span class="chip ${cls}">${esc(chip)}</span><span><b>${f2(v.v_tot)}</b> V</span>
    <span>${dir === "in" ? "↓" : dir === "out" ? "↑" : ""}<b>${f2(Math.abs(cur))}</b> A${dir ? " " + (dir === "in" ? "in" : "out") : ""}</span>
    <span class="muted">${v.v_charge > 1 ? "charger " + f1(v.v_charge) + " V" : "no charger"}</span></div>`;
}


// ---------------------------------------------------------------- overview

export function overview(root, b) {
  skeleton(root, "overview|" + b.host, `<div data-slot="banner"></div>
    <section class="panel hero">
      <div class="hero-ring" data-slot="ring"></div>
      <div class="hero-side"><div data-slot="inds"></div><div data-slot="switches"></div><div data-slot="hero"></div><div data-slot="limit"></div><div data-slot="sched"></div></div>
      <div class="hero-x" data-slot="x"></div>
    </section>
    <div class="grid2">
      <section class="panel link" data-goto="cells"><h3>Cells <span class="more">Details ›</span></h3><div data-slot="range"></div><div data-slot="spread"></div><div data-slot="abar"></div></section>
      <section class="panel link" data-goto="temps"><h3>Temperatures <span class="more">Details ›</span></h3><div data-slot="temps"></div></section>
    </div>${keyPanel()}`);
  const v = b.values;
  root.dataset.dhost = b.host;
  slot(root, "banner", banner(b));
  slot(root, "ring", ring(b));
  slot(root, "inds", indicators(b));
  slot(root, "switches", switches(b));
  slot(root, "hero", heroStats(b));
  slot(root, "limit", limitLine(b));
  slot(root, "sched", scheduleSummary(b));
  slot(root, "x", expanded(b, { temps: false }));
  slot(root, "range", v ? `<div class="gauge-head"><span>Lowest / highest cell</span><b>${f3(v.cell_min)} / ${f3(v.cell_max)} V</b></div>${chargeGauge(b)}` : noData(b));
  slot(root, "spread", v ? `<div class="gauge-head"><span>Imbalance</span><b>${f1(v.cell_delta_mv)} mV</b></div>${spreadGauge(b)}` : "");
  slot(root, "abar", applyBar(b, ["vc_charge_end", "vc_charge_start", "vc_balance_start", "vc_balance_end"]));
  slot(root, "temps", tempBars(b));
}

// Gauges with draggable thresholds (drafts from drag.js are drawn instead of the board's values).
function chargeGauge(b) {
  const v = b.values;
  return rangeGauge(v.cell_min, v.cell_max, { vc_charge_start: pv(b, "vc_charge_start"), vc_charge_end: pv(b, "vc_charge_end") },
    { edit: editable(b) && !chargeBlockers(b).length, pending: isPending(b, "vc_charge_end") });
}
function spreadGauge(b) {
  return spreadMeter(b.values.cell_delta_mv, { balStart: pv(b, "vc_balance_start") * 1000, balEnd: pv(b, "vc_balance_end") * 1000, alert: alertCfg().delta,
    edit: editable(b) && !(b.write_blockers || []).length, pending: isPending(b, "vc_balance_start") || isPending(b, "vc_balance_end") });
}

// ---------------------------------------------------------------- cells

function cellChart(b) {
  const v = b.values;
  if (!v || !v.cells.length) return noData(b);
  const cells = v.cells, n = cells.length, med = median(cells);
  const ch = b.charge || {};
  const iMax = cells.indexOf(v.cell_max), iMin = cells.indexOf(v.cell_min), dmv = v.cell_delta_mv || 0;
  const grid = [4.2, 3.9, 3.6, 3.3, 3.0].map((g) => `<div class="cc-grid" style="bottom:${vPct(g)}%"><span>${g.toFixed(1)}</span></div>`).join("");
  const ed = editable(b) && !chargeBlockers(b).length, edBal = editable(b) && !(b.write_blockers || []).length;
  const line = (val, cls, lab, f, can) => val == null ? "" : `<div class="cc-line ${cls}${can ? " drag" : ""}${isPending(b, f) ? " pending" : ""}" style="bottom:${vPct(val)}%"${can ? ` data-drag="${f}" tabindex="0" role="slider" aria-label="${lab}" aria-valuemin="${V_LO}" aria-valuemax="${V_HI}" aria-valuenow="${f3(val)}" aria-valuetext="${f3(val)} V"` : ""}><span${can ? ` data-edit="${f}" data-tip="Click to type a value · drag the line to move it"` : ""}>${lab} ${f3(val)}</span></div>`;
  const bars = cells.map((cv, i) => {
    const lvl = cellLevel(b, cv, med);
    const tip = `Cell ${i + 1}: ${cv.toFixed(3)} V · ${Math.round((cv - v.cell_min) * 1000)} mV above lowest${v.balancing[i] ? " · balancing" : ""}${lvl === "bad" ? " · out of range" : lvl === "warn" ? " · outlier" : ""}`;
    const hl = dmv > 0 && i === iMax ? " hi" : dmv > 0 && i === iMin ? " lo" : "";
    const mark = hl === " hi" ? `<em class="cc-mark">▲ max</em>` : hl === " lo" ? `<em class="cc-mark">▼ min</em>` : "";
    return `<div class="cc-cell ${lvl}${hl}" data-tip="${esc(tip)}" tabindex="0">${mark}<div class="cc-bar" style="height:${vPct(cv).toFixed(2)}%"></div></div>`;
  }).join("");
  const nums = cells.map((_, i) => `<span>${n <= 20 || i % 2 === 0 || i === n - 1 ? i + 1 : ""}</span>`).join("");
  const dots = cells.map((_, i) => `<span>${v.balancing[i] ? '<i class="bal-dot" title="balancing"></i>' : ""}</span>`).join("");
  const dev = cells.map((cv, i) => `<i class="${dmv > 0 && i === iMax ? "hi" : dmv > 0 && i === iMin ? "lo" : ""}"><b style="height:${dmv > 0 ? Math.max(4, ((cv - v.cell_min) * 1000 / dmv) * 100).toFixed(1) : 4}%"></b></i>`).join("");
  return `<div class="cellchart" style="--n:${n}">
      <div class="cc-plot" data-dragbox data-axis="y" data-v0="${V_LO}" data-v1="${V_HI}">${grid}${bars}${line(pv(b, "vc_charge_end"), "end", "end", "vc_charge_end", ed)}${line(pv(b, "vc_charge_start"), "start", "start", "vc_charge_start", ed)}${line(pv(b, "vc_balance_min"), "bmin", "balance min", "vc_balance_min", edBal)}${ed ? `<div class="cc-lock" style="bottom:${vPct((pv(b, "vc_charge_end") + pv(b, "vc_charge_start")) / 2)}%">${lockBtn(b)}</div>` : ""}</div>
      <div class="cc-row cc-dots">${dots}</div><div class="cc-row cc-nums">${nums}</div>
      <div class="cc-devcap"><span>mV above lowest</span><b>Δ ${f1(dmv)} mV</b></div><div class="cc-row cc-dev">${dev}</div></div>
    <div class="legend"><span><i class="sw cellsw"></i>cell (${V_LO.toFixed(1)}–${V_HI.toFixed(1)} V scale)</span><span><i class="sw hisw"></i>▲ highest</span><span><i class="sw losw"></i>▼ lowest</span><span><i class="bal-dot"></i>balancing</span><span><i class="sw warnsw"></i>outlier (&gt; ${alertCfg().delta} mV from median)</span><span><i class="sw badsw"></i>out of range</span><span><i class="ln end"></i>charge end</span><span><i class="ln start"></i>charge start</span><span><i class="ln bmin"></i>balance min</span></div>
    ${ed ? `<p class="muted small">Drag the charge lines to change Charge End/Start (they move together, 0.100 V apart, unless you unlock 🔒), or click a label to type a value. Nothing is written until you click Write.</p>` : ""}`;
}

export function cellsView(root, b, d) {
  skeleton(root, "cells|" + b.host, `<div data-slot="banner"></div>
    <section class="panel"><h3>Cell voltages <span class="more" data-slot="cmeta"></span></h3><div data-slot="chart"></div><div data-slot="abar"></div></section>
    <div class="grid2">
      <section class="panel"><h3>Lowest / highest cell</h3><div data-slot="range"></div></section>
      <section class="panel"><h3>Imbalance</h3><div data-slot="spread"></div></section>
    </div>
    <section class="panel"><h3>History <span data-slot="hchips"></span></h3>${chartSlot("cells")}</section>`);
  const v = b.values;
  root.dataset.dhost = b.host;
  slot(root, "banner", banner(b));
  slot(root, "hchips", rangeChips(b.host, "cells"));
  drawPanel(root, b, "cells", { metric: "cells" });
  slot(root, "cmeta", v ? `${v.cells.length} cells · Δ ${f1(v.cell_delta_mv)} mV · ▲ #${v.cells.indexOf(v.cell_max) + 1} ${f3(v.cell_max)} · ▼ #${v.cells.indexOf(v.cell_min) + 1} ${f3(v.cell_min)} V` : "");
  slot(root, "chart", cellChart(b));
  slot(root, "abar", applyBar(b, ["vc_charge_end", "vc_charge_start", "vc_balance_min", "vc_balance_start", "vc_balance_end"]));
  slot(root, "range", v ? `<div class="bignums"><div><span>Lowest</span><b>${f3(v.cell_min)}<small> V</small></b></div><div><span>Highest</span><b>${f3(v.cell_max)}<small> V</small></b></div></div>${chargeGauge(b)}` : noData(b));
  slot(root, "spread", v ? `<div class="bignums"><div><span>Spread</span><b>${f1(v.cell_delta_mv)}<small> mV</small></b></div><div><span>Balancing</span><b>${v.balancing.filter(Boolean).length}<small> cells</small></b></div></div>${spreadGauge(b)}` : "");
}

// ---------------------------------------------------------------- temps

function sensorLimits(name, lim) {
  if (/MOSFET/.test(name)) return [{ v: lim.t_charge_max_mos, label: "charge", kind: "max" }, { v: lim.t_psw_max_mos, label: "cut-off", kind: "max" }];
  if (/^IC$/.test(name)) return [{ v: lim.t_bal_max_ic, label: "balance", kind: "max" }];
  if (/Cell|Sensor/.test(name)) return [{ v: lim.t_charge_max, label: "charge", kind: "max" }, { v: lim.t_bal_max_cell, label: "balance", kind: "max" }, { v: lim.t_charge_min, label: "min", kind: "min" }];
  return [];
}

// One temperature gauge: a range of sensor readings against draggable limits.
// marks: [{f, kind: "min"|"max", label}]
function tempGauge(b, title, temps, marks) {
  const can = editable(b) && !(b.write_blockers || []).length;
  const lo = -20, hi = 70, X0 = 16, X1 = 384, x = (t) => X0 + (X1 - X0) * clamp((t - lo) / (hi - lo), 0, 1);
  const tmin = Math.min(...temps), tmax = Math.max(...temps);
  const val = (m) => pv(b, m.f);
  const mins = marks.filter((m) => m.kind === "min"), maxs = marks.filter((m) => m.kind === "max");
  const okLo = mins.length ? Math.max(...mins.map(val)) : lo, okHi = Math.min(...maxs.map(val));
  const ok = tmin > okLo && tmax < okHi;
  const ticks = [-20, 0, 20, 40, 60].map((t) => `<text x="${x(t)}" y="72" text-anchor="middle" class="g-lab">${t}°</text>`).join("");
  const mk = (m) => { const v = val(m); return `<g class="g-handle tg-${m.kind}${isPending(b, m.f) ? " pending" : ""}"${can ? ` data-drag="${m.f}" tabindex="0" role="slider" aria-label="${esc(m.label)}" aria-valuenow="${v}"` : ""}>
    ${can ? `<rect x="${x(v) - 9}" y="4" width="18" height="50" class="g-hit"/>` : ""}<line x1="${x(v)}" x2="${x(v)}" y1="30" y2="52" class="tg-line"/><text x="${x(v)}" y="24" text-anchor="middle" class="g-lab tg-lab">${Math.round(v)}°</text></g>`; };
  return `<div class="gauge-head"><span>${title}</span><b class="${ok ? "ok-t" : "bad-t"}">${ok ? "allowed" : "blocked by temperature"}</b></div>
    <svg class="gauge${can ? " editable" : ""}" viewBox="0 0 400 78" role="${can ? "group" : "img"}" aria-label="${esc(title)}: ${f1(tmin)} to ${f1(tmax)} degrees"${can ? ` data-dragbox data-axis="x" data-vb="400" data-p0="${X0}" data-p1="${X1}" data-v0="${lo}" data-v1="${hi}"` : ""}>
      <rect x="${X0}" y="38" width="${X1 - X0}" height="10" rx="5" class="g-track"/>
      <rect x="${x(okLo)}" y="38" width="${Math.max(0, x(okHi) - x(okLo))}" height="10" class="cw-ok"/>
      <rect x="${x(tmin) - 2}" y="33" width="${Math.max(4, x(tmax) - x(tmin) + 4)}" height="20" rx="4" class="g-seg"/>${ticks}${marks.map(mk).join("")}</svg>
    <p class="small muted tg-keys">${marks.map((m) => `<button type="button" class="linklike" data-edit="${m.f}"${can ? "" : " disabled"}>${esc(m.label)} ${f1(val(m))} °C</button>`).join(" · ")}</p>`;
}

function chargeWindow(b) {
  const v = b.values, lim = b.limits_cfg || {};
  if (!v || lim.t_charge_min == null) return "";
  const cellT = v.temps.filter((t) => /^Cell sensor|^Sensor/.test(t.name)).map((t) => t.value);
  if (!cellT.length) return "";
  const ic = v.temps.find((t) => t.name === "IC");
  return tempGauge(b, "Cells vs. charging range", cellT, [{ f: "t_charge_min", kind: "min", label: "Charge min" }, { f: "t_charge_max", kind: "max", label: "Charge max" }]) +
    tempGauge(b, "Cells vs. balancing limit", cellT, [{ f: "t_bal_max_cell", kind: "max", label: "Balance max (cells)" }]) +
    (ic && lim.t_bal_max_ic != null ? tempGauge(b, "BMS chip vs. balancing limit", [ic.value], [{ f: "t_bal_max_ic", kind: "max", label: "Balance max (IC)" }]) : "") +
    (editable(b) ? `<p class="muted small">Drag a mark, or click its value below to type one. Nothing is written until you click Write.</p>` : "");
}

export function tempsView(root, b, d) {
  skeleton(root, "temps|" + b.host, `<div data-slot="banner"></div>
    <section class="panel"><h3>Sensors</h3><div data-slot="thermos" class="thermos"></div></section>
    <div class="grid2"><section class="panel"><div data-slot="window"></div><div data-slot="abar"></div></section><section class="panel"><h3>Board environment</h3><div data-slot="env"></div></section></div>
    <section class="panel"><h3>History <span data-slot="hchips"></span></h3>${chartSlot("temps")}</section>`);
  const v = b.values, lim = b.limits_cfg || {};
  root.dataset.dhost = b.host;
  slot(root, "banner", banner(b));
  slot(root, "hchips", rangeChips(b.host, "temps"));
  drawPanel(root, b, "temps", { metric: "temps", cell: isOpen(b.host + "|temps") ? "all" : "" });
  if (!v) { slot(root, "thermos", noData(b)); return; }
  const temps = v.temps.filter((t) => !/^Cell (min|max)$/.test(t.name));
  const cells = cellTemps(v), open = isOpen(b.host + "|temps");
  const th = (t) => thermo(t.name, t.value, sensorLimits(t.name, lim).filter((l) => l.v != null));
  let thermos = "";
  if (cells.length) {
    const av = avg(cells.map((t) => t.value));
    thermos += `<button type="button" class="thermo-btn${open ? " open" : ""}" data-act="temps" data-host="${esc(b.host)}" aria-expanded="${open}" data-tip="${open ? "Tap to show only the average" : `Average of ${cells.length} cell sensors · tap for each one`}">
      ${thermo(open ? `Cells avg ▴` : `Cells avg of ${cells.length} ▾`, av, sensorLimits("Cell", lim).filter((l) => l.v != null))}</button>`;
    if (open) thermos += cells.map(th).join("");
  }
  thermos += temps.filter((t) => !cells.includes(t)).map(th).join("");
  slot(root, "thermos", thermos);
  slot(root, "window", chargeWindow(b) || `<div class="muted">No cell temperature sensors reported.</div>`);
  slot(root, "abar", applyBar(b, ["t_charge_min", "t_charge_max", "t_bal_max_cell", "t_bal_max_ic"]));
  slot(root, "env", `<div class="bignums"><div><span>Board sensor</span><b>${f1(v.temp_hum)}<small> °C</small></b></div><div><span>Humidity</span><b>${f1(v.humidity)}<small> %</small></b></div></div>
    <div class="humbar"><i style="width:${Math.max(0, Math.min(100, v.humidity || 0))}%"></i></div>
    <p class="muted small">From the humidity sensor on the BMS board. High humidity inside the enclosure can mean water got in.</p>`);
}

// ---------------------------------------------------------------- charging

// One line: the charge in progress (elapsed, energy, ETA) or the last one, linking to History.
export function chargeLine(b) {
  const c = b.charging_now, l = b.last_charge;
  const link = `<button type="button" class="linklike" data-goto="history" data-hv="view" data-v="charges" data-host="${esc(b.host)}">History ›</button>`;
  if (c) return `<p class="chgline"><b>Charging ${dur(c.elapsed_s)}</b> · +${Math.round(c.wh)} Wh (+${f2(c.ah)} Ah)${c.joined_late ? " so far seen" : ""}${c.eta != null
    ? ` · reaches the limit ~<b>${clock(Date.now() / 1000 + c.eta)}</b>` : ""} ${link}</p>${c.eta != null ? `<p class="muted small">ETA extrapolates the highest cell toward Charge End over the last 10 minutes.</p>` : ""}`;
  if (l) return `<p class="chgline muted">Last charge ${esc(when(l.start))} · ${dur(l.duration_s)} · ${Math.round(l.soc0 * 100)} → ${Math.round(l.soc1 * 100)}% · +${Math.round(l.wh)} Wh · ${esc(l.reason_text)} ${link}</p>`;
  return "";
}

export function chargingView(root, b, d) {
  skeleton(root, "charging|" + b.host, `<div data-slot="banner"></div>
    <div class="grid2">
      <section class="panel"><h3>Charge limit</h3><div data-slot="limit"></div><div data-slot="chgline"></div></section>
      <section class="panel"><h3>Live control</h3><div data-slot="live"></div></section>
    </div>
    <section class="panel link" data-goto="schedule"><h3>Schedule <span class="more">Open schedule ›</span></h3><div data-slot="schedule"></div></section>
    <section class="panel"><h3>Charging settings</h3><div data-form="charging"></div></section>`);
  slot(root, "banner", banner(b));
  slot(root, "limit", limitButton(b));
  slot(root, "chgline", chargeLine(b));
  const rt = b.runtime && b.runtime.charge_allowed;
  const rtt = rt && (rt.t ? clock12(rt.t) : hms12(rt.time));
  slot(root, "live", `${switches(b, ["charge"], false) || noData(b)}
    <div class="live-state ${rt && rt.value === 0 ? "paused" : ""}">${rt ? (rt.value === 0 ? `Paused ${esc(rt.why)}, ${esc(rtt)}` : `Allowed ${esc(rt.why)}, ${esc(rtt)}`) : "Charging follows the BMS settings."}</div>
    <div class="row">${laterBtn(b, { charge: "on" })}<span class="muted small">Start or stop charging, or set a limit, at a time you pick</span></div>
    ${timerList(b)}
    <p class="muted small">Pause/Allow is a live command: it isn't saved to the BMS and resets when it restarts or wakes from sleep. A tap overrides the schedule until its next change.</p>`);
  slot(root, "schedule", scheduleSummary(b));
  renderForm(root.querySelector('[data-form="charging"]'), b, group(d, "charging"));
}

// ---------------------------------------------------------------- schedule

export function scheduleView(root, b) {
  skeleton(root, "schedule|" + b.host, `<div data-slot="banner"></div><div class="schedview" data-sched-root></div>`);
  slot(root, "banner", banner(b));
  renderSchedule(root.querySelector("[data-sched-root]"), b);
}

// ---------------------------------------------------------------- balancing

function balChecks(b) {
  const v = b.values, lim = b.limits_cfg || {};
  if (!v) return "";
  const cellT = v.temps.filter((t) => /^Cell sensor|^Sensor/.test(t.name)).map((t) => t.value);
  const ic = (v.temps.find((t) => t.name === "IC") || {}).value;
  const checks = [
    [`Pack current ${f2(Math.abs(v.i_in_ic))} A below ${f1(lim.balance_max_current)} A`, Math.abs(v.i_in_ic) < lim.balance_max_current],
    [`Lowest cell ${f3(v.cell_min)} V at or above ${f2(lim.vc_balance_min)} V`, v.cell_min >= lim.vc_balance_min],
    cellT.length ? [`Cells ${f1(Math.max(...cellT))} °C below ${f1(lim.t_bal_max_cell)} °C`, Math.max(...cellT) < lim.t_bal_max_cell] : null,
    ic != null ? [`BMS ${f1(ic)} °C below ${f1(lim.t_bal_max_ic)} °C`, ic < lim.t_bal_max_ic] : null,
  ].filter(Boolean);
  return `<ul class="checks">${checks.map(([t, ok]) => `<li class="${ok ? "ok" : "no"}">${ok ? "✓" : "✕"} ${esc(t)}</li>`).join("")}</ul>
    <p class="muted small">These are the conditions the vbms32 script checks before it balances. Cells more than Balance Start above the lowest cell then bleed until they're within Balance End (never two neighbouring cells at once, at most Max Balance Channels at a time).</p>`;
}

export function balancingView(root, b, d) {
  skeleton(root, "balancing|" + b.host, `<div data-slot="banner"></div>
    <div class="grid2">
      <section class="panel"><h3>Right now</h3><div data-slot="sw"></div><div data-slot="now"></div><div data-slot="abar"></div></section>
      <section class="panel"><h3>Can it balance?</h3><div data-slot="checks"></div></section>
    </div>
    <section class="panel"><h3>Balancing settings</h3><div data-form="balancing"></div></section>`);
  const v = b.values, lim = b.limits_cfg || {};
  root.dataset.dhost = b.host;
  slot(root, "banner", banner(b));
  slot(root, "sw", `${switches(b, ["balance"], false)}<div class="row">${laterBtn(b, { balance: "off" })}<span class="muted small">Stop or allow balancing at a time you pick</span></div>${timerList(b)}
    ${lim.balance_max_current === 0 ? `<p class="small">Balancing is off: Balance Max Current is 0 A, so the script's "pack current below the max" check never passes.</p>` : ""}`);
  slot(root, "abar", applyBar(b, ["vc_balance_start", "vc_balance_end"]));
  if (v) {
    const bal = v.balancing.map((x, i) => (x ? i + 1 : 0)).filter(Boolean);
    slot(root, "now", `<div class="bignums"><div><span>Spread</span><b>${f1(v.cell_delta_mv)}<small> mV</small></b></div><div><span>Balancing</span><b>${bal.length}<small> cells</small></b></div></div>
      ${spreadGauge(b)}
      ${cellStrip(v.cells, { balancing: v.balancing, levels: v.cells.map((c) => cellLevel(b, c, median(v.cells))) })}
      <p class="small">${bal.length ? "Bleeding cells " + bal.join(", ") : "No cells bleeding."}</p>
      <div class="row"><button type="button" class="ghost" data-act="rt" data-cmd="force_balance" data-val="1"${dis()}>Force balance on</button>
      <button type="button" class="ghost" data-act="rt" data-cmd="force_balance" data-val="0"${dis()}>Force balance off</button></div>
      <p class="muted small">Force balance is a live command; it resets when the BMS restarts.</p>`);
    slot(root, "checks", balChecks(b));
  } else slot(root, "now", noData(b));
  renderForm(root.querySelector('[data-form="balancing"]'), b, group(d, "balancing"));
}

// ---------------------------------------------------------------- settings

export function settingsView(root, b, d) {
  skeleton(root, "settings|" + b.host, `<div data-slot="banner"></div>
    <section class="panel"><h3>Sleep &amp; state of charge</h3><div data-form="sleep_soc"></div></section>
    <section class="panel"><h3>Protection</h3><div class="banner">These protect the BMS and pack from shorts and overheating. Leave them on unless you know exactly why.</div><div data-form="protection"></div></section>
    <section class="panel"><h3>WiFi, Bluetooth &amp; CAN</h3><div class="banner bad">A wrong value here can knock the board off your WiFi or CAN bus. Changes apply after the BMS restarts; fixing a mistake then needs USB or Bluetooth.</div><div data-form="comms"></div></section>
    <section class="panel"><h3>Not editable here</h3><div data-slot="ro"></div></section>`);
  slot(root, "banner", banner(b));
  for (const id of ["sleep_soc", "protection", "comms"]) renderForm(root.querySelector(`[data-form="${id}"]`), b, group(d, id));
  slot(root, "ro", d && d.editable ? readOnlyList(d.editable) : `<div class="muted">Loading…</div>`);
}

// ---------------------------------------------------------------- board

function backupList(b) {
  if (!b.mac) return "";
  const e = fetchCached("bk|" + b.host, "/api/backups?host=" + encodeURIComponent(b.host), 30000);
  if (!e.data) return `<div class="muted">${e.err ? esc(e.err) : "Loading…"}</div>`;
  const list = e.data.backups;
  if (!list.length) return `<p class="muted small">None yet. One is saved before every write to this board.</p>`;
  const shown = list.slice(0, 8);
  return `<div class="backups">${shown.map((k) => `<div class="bk"><span><b>${esc(stamp12(k.time))}</b>
      <span class="muted small">${k.changes == null ? "can't compare with this firmware" : k.changes.length ? "restoring changes " + esc(k.changes.join(", ")) : "same as the board now"}</span></span>
      ${k.restorable ? `<button type="button" class="small ghost" data-act="restore" data-id="${esc(k.id)}"${dis()}>Restore…</button>` : ""}</div>`).join("")}</div>
    ${list.length > shown.length ? `<p class="muted small">${list.length - shown.length} older backups in ~/.vbms_manager/backups.</p>` : ""}
    <p class="muted small">Restore only offers backups whose differences are all settings VBMS Manager edits, and they go through the same checks, confirmation, backup and read-back as any change. Restore others in VESC Tool with Load XML.</p>`;
}

export function boardView(root, b, d) {
  skeleton(root, "board|" + b.host, `<div data-slot="banner"></div>
    <div class="grid2">
      <section class="panel"><h3>This board</h3><div data-slot="ident"></div></section>
      <section class="panel"><h3>Running script</h3><div data-slot="script"></div></section>
      <section class="panel"><h3>Logging &amp; backup</h3><div data-slot="log"></div></section>
      <section class="panel"><h3>Connection</h3><div data-slot="conn"></div></section>
    </div>
    <section class="panel"><h3>Storage mode <span class="more">for leaving the board for weeks</span></h3><div data-slot="storage"></div></section>
    <section class="panel"><h3>Add-ons <span class="more">optional, replaces the BMS script</span></h3><div data-slot="addons"></div></section>
    <section class="panel"><h3>Backups <span class="more">saved automatically before every write</span></h3><div data-slot="backups"></div></section>
    <section class="panel"><h3>Recent events</h3><div data-slot="events"></div></section>
    <section class="panel"><details><summary>Full BMS configuration (read-only)</summary><div data-slot="cfg"></div></details></section>`);
  slot(root, "banner", banner(b));
  if (!b.mac) { slot(root, "ident", `<div class="muted">Not read yet (${esc(b.state)}).</div>`); }
  else {
    slot(root, "ident", `<dl class="kv"><dt>Name</dt><dd>${esc(boardName(b))}</dd>
      <dt>Cell type</dt><dd>${b.cell ? esc(b.cell.name) : '<b class="bad-t">not chosen</b>'}</dd>
      <dt>SOC curve</dt><dd>${b.cell ? (b.cell.accepted ? "accepted" : '<b class="bad-t">not accepted yet</b>') + (b.cell.curve_info.custom ? " (custom)" : "") : "–"}</dd>
      <dt>Hardware</dt><dd>${b.fw ? `${esc(b.fw.hw_name)} · FW ${esc(b.fw.version)}` : "–"}</dd>
      <dt>Cells</dt><dd>${cellsCount(b)}s${b.cell ? ` · ${f1(b.cell.max_end * cellsCount(b))} V at 100%` : ""}</dd>
      <dt>MAC</dt><dd class="mono">${esc(b.mac)}</dd>${b.ble_only ? `<dt>Link</dt><dd>Bluetooth</dd>` : `<dt>IP</dt><dd class="mono">${esc(b.host)}${b.link === "ble" ? " (using Bluetooth now)" : ""}</dd>`}
      <dt>Config check</dt><dd>${b.layout_ok ? "decoded config re-encodes byte-for-byte" : '<b class="bad-t">mismatch, writes disabled</b>'}</dd></dl>
      <div class="row"><button type="button" data-act="wizard"${dis()}>Edit board setup</button></div>`);
  }
  const sc = d && d.script;
  slot(root, "script", !sc ? `<div class="muted">${d ? "Not read yet." : "Loading…"}</div>` : `<div>${sc.verified ? `<span class="chip ok">Charge logic matches the known vbms32 pattern</span>` :
      sc.acknowledged ? `<span class="chip warn">Unrecognized, acknowledged by you</span>` : `<span class="chip bad">Unrecognized charge logic, writes locked</span>`}</div>
    <ul class="small">
      <li>${sc.hysteresis_found ? "While charging it stops at <b>Charge End</b>; after stopping it restarts once the highest cell drops below <b>Charge Start</b>." : "Start/end hysteresis check <b>not found</b>."}</li>
      ${sc.wake_path_uses_end ? `<li>When the BMS wakes with a charger connected it charges if the highest cell is below <b>Charge End</b> (not Start), so sleep/wake cycles can top up without the 0.1 V hysteresis.</li>` : ""}
      ${sc.sticky_charger_latch ? `<li>Has a "sticky charger latch" that keeps the BMS awake on the charger, which avoids the wake-path top-up.</li>` : ""}
      <li class="muted">${sc.lines} lines · SHA-256 <span class="mono">${esc(sc.sha256.slice(0, 16))}…</span></li></ul>
    ${sc.problems.length ? `<div class="banner bad">${sc.problems.map(esc).join("<br>")}</div>` : ""}
    <details><summary>Show the charge code it found</summary>${(sc.excerpts || []).map((x) => `<div class="muted small">line ${x.line}</div><pre class="code">${esc(x.text)}</pre>`).join("")}
    ${sc.unexpected.length ? `<b>Unreviewed references:</b>${sc.unexpected.map((x) => `<pre class="code">${x.line}: ${esc(x.text)}</pre>`).join("")}` : ""}</details>
    ${!sc.verified && !sc.acknowledged ? `<p class="small">Review and acknowledge it in <button type="button" class="linklike" data-act="wizard"${dis()}>board setup</button>.</p>` : ""}`);
  const exp = (s) => `/api/export?host=${encodeURIComponent(b.host)}${s ? "&secrets=1" : ""}&token=${encodeURIComponent(TOKEN)}`;
  slot(root, "log", `<p class="small">${recordingLine()}</p>
    <div class="row small"><span class="muted">Download CSV:</span>${[["86400", "24 h"], ["604800", "7 days"], ["2592000", "30 days"], ["0", "all"]].map(([sp, l]) =>
      `<button type="button" class="small ghost" data-act="csvdl" data-span="${sp}">${l}</button>`).join("")}</div>
    ${canWrite() ? `<div class="row"><a class="btn ghost small" href="${exp(false)}">Export config XML</a><a class="btn ghost small" href="${exp(true)}">Export with WiFi keys</a></div>` : `<p class="muted small">Pair this device to export configs.</p>`}
    <p class="muted small">VESC Tool can load these (Load XML on the BMS config page). A full backup is also saved automatically before every write, in ~/.vbms_manager/backups.</p>`);
  slot(root, "addons", b.mac ? addonPanel(b, "data-act") : `<div class="muted">Not read yet.</div>`);
  slot(root, "backups", backupList(b));
  slot(root, "conn", connPanel(b));
  if (!root.querySelector("[data-slot=storage]").contains(document.activeElement)) slot(root, "storage", storagePanel(b));
  slot(root, "events", `<div class="events">${((d && d.events) || b.events || []).slice().reverse().map((e) => `<div><span class="muted">${esc(stamp12(e.time))}</span> <b>${esc(e.kind)}</b> ${esc(e.text)}</div>`).join("") || '<span class="muted">None yet.</span>'}</div>`);
  slot(root, "cfg", d && d.config ? `<div class="scroll"><table class="cfg">${d.config.map((f) =>
    `<tr><td>${esc(f.label)} <span class="muted mono">${esc(f.name)}</span></td><td>${f.secret ? "••••" : esc(f.enum ?? (typeof f.value === "number" ? +f.value.toFixed(6) : f.value))}${f.suffix ? " " + esc(f.suffix) : ""}</td></tr>`).join("")}</table></div>` : `<div class="muted">Loading…</div>`);
}

// ---------------------------------------------------------------- all boards

export function allView(root, boards) {
  delete root.dataset.dhost;
  skeleton(root, "all", `<div data-slot="bulk"></div><div class="allgrid" data-slot="cards"></div>${keyPanel()}`);
  const bb = (action, label, cls = "ghost") => `<button type="button" class="small ${cls}" data-bulk="${action}"${dis()}>${label}</button>`;
  slot(root, "bulk", `<section class="panel bulkbar"><b>All boards</b>
    <span class="bgroup"><span class="muted small">Charging</span>${bb("charging_on", "Allow")}${bb("charging_off", "Pause")}</span>
    <span class="bgroup"><span class="muted small">Balancing</span>${bb("balancing_on", "Allow")}${bb("balancing_off", "Disallow")}</span>
    <span class="bgroup"><span class="muted small">Storage mode</span>${bb("storage_on", "All into storage")}${bb("storage_off", "All out")}</span></section>`);
  slot(root, "cards", boards.map((b) => {
    const v = b.values;
    const [chip, cls] = stateChip(b);
    const pct = currentPct(b);
    const notes = (b.notices || []).filter((n) => n.level !== "info");
    return `<div class="panel mini">${notes.length ? `<div class="banner small${notes.some((n) => n.level === "bad") ? " bad" : ""}">${notes.map((n) => esc(n.text)).join("<br>")}</div>` : ""}
      <button type="button" class="mini-head" data-pick="${esc(b.host)}"><b>${esc(boardName(b))}</b><span class="chip ${cls}">${esc(chip)}</span><span class="chev" aria-hidden="true">›</span></button>
      <div class="mini-body"><div class="mini-ring">${ring(b)}</div><div>${v ? indicators(b) : noData(b)}${switches(b, ["charge", "balance"], false)}</div></div>
      ${expanded(b)}
      <div class="mini-foot"><span>${v ? f2(v.v_tot) + " V" : ""}</span><span>${v ? f3(v.cell_min) + "–" + f3(v.cell_max) + " V" : ""}</span><span>${b.storage && b.storage.on ? "storage mode" : pct != null ? "limit " + pct + "%" : ""}</span></div>
    </div>`;
  }).join(""));
}

export function pill(b, active) {
  const v = b.values;
  return `<button type="button" class="pill${active ? " on" : ""}" data-pick="${esc(b.host)}" title="${b.link === "ble" ? "Over Bluetooth" : esc(b.host)}">
    ${miniBattery(v ? v.soc : null, b.state)}<span class="pname">${esc(boardName(b))}</span>${b.link === "ble" ? BT_ICON : ""}<span class="psoc">${v ? Math.round(v.soc * 100) + "%" : ""}</span><i class="dot ${stateChip(b)[1]}" aria-hidden="true"></i><span class="sr-only">, ${esc(stateChip(b)[0])}</span></button>`;
}

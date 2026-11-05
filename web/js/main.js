// App shell: polling, routing (#b=<host>&t=<tab>), header, tabs, event wiring.

import { $, $$, alertCfg, api, ask, AWAKE_FIX, AWAKE_WARN, balanceState, boardAlerts, boardName, canWrite, esc, f3, fail, hooks, placeTip, S, showMsg, skipConfirm, stamp12, store, toast, toggleOpen } from "./core.js";
import { dur } from "./charts.js";
import { drawDevices, initDevices, initGuide, openGuide } from "./guide.js";
import { forget, historyView, resetHistoryCache } from "./history.js";
import { allView, balancingView, boardView, cellsView, chargingView, overview, pill, settingsView, tempsView } from "./views.js";
import { initWizard, openWizard, wizardOpen, wizardSeen } from "./wizard.js";
import { cancelTimer, installAddon, openChargeSheet, openLaterSheet, restoreBackup, runtime, sendRuntime, toggleBalance, toggleCharge } from "./writes.js";
import "./drag.js";

const TABS = [
  ["overview", "Overview", null, overview],
  ["cells", "Cells", true, cellsView],
  ["temps", "Temps", true, tempsView],
  ["charging", "Charging", true, chargingView],
  ["balancing", "Balancing", true, balancingView],
  ["history", "History", true, historyView],
  ["settings", "Settings", true, settingsView],
  ["board", "Board", true, boardView],
];
const ALL = "*";
const alertState = new Map();
const chargeSeen = new Map(); // host -> id of the newest closed charge session we've seen

// ---------------------------------------------------------------- routing

function readRoute() {
  const p = new URLSearchParams(location.hash.slice(1));
  S.route.board = p.get("b");
  S.route.tab = TABS.some((t) => t[0] === p.get("t")) ? p.get("t") : "overview";
}

function go(board, tab) {
  const p = new URLSearchParams();
  p.set("b", board ?? S.route.board ?? "");
  p.set("t", tab ?? S.route.tab);
  history.replaceState(null, "", "#" + p.toString());
  readRoute();
  render();
  loadDetailIfNeeded(true);
  window.scrollTo({ top: 0 });
}

function currentBoard() {
  const bs = S.state ? S.state.boards : [];
  if (S.route.board === ALL && bs.length > 1) return ALL;
  return bs.find((b) => b.host === S.route.board) || bs[0] || null;
}

// ---------------------------------------------------------------- data

async function refresh() {
  try {
    S.state = await api("/api/state");
    $("#lost").hidden = true;
    render();
  } catch (e) {
    $("#lost").hidden = false;
    $("#lost").innerHTML = `<b>Lost contact with VBMS Manager</b> (${esc(e.message)}). Is it still running?`;
  }
}

async function reloadDetail(host) {
  try {
    S.detail.set(host, await api("/api/board?host=" + encodeURIComponent(host)));
    render();
  } catch { /* the next poll shows connection problems */ }
}

let lastDetail = 0;
function loadDetailIfNeeded(now = false) {
  const b = currentBoard();
  if (!b || b === ALL) return;
  const tab = TABS.find((t) => t[0] === S.route.tab);
  if (!tab[2]) return;
  if (now || !S.detail.has(b.host) || Date.now() - lastDetail > 5000) {
    lastDetail = Date.now();
    reloadDetail(b.host);
  }
}

hooks.refresh = refresh;
hooks.reloadDetail = reloadDetail;
hooks.rerender = () => render();

// ---------------------------------------------------------------- rendering

function setHtml(el, html) {
  if (el._html !== html) { el._html = html; el.innerHTML = html; }
}

function notify(b) {
  const list = boardAlerts(b);
  const prev = alertState.get(b.host) || new Set();
  const a = alertCfg();
  const can = "Notification" in window && Notification.permission === "granted";
  if (a.notify && can) {
    for (const x of list) if (!prev.has(x.k)) new Notification(boardName(b) + ": BMS alert", { body: x.t });
  }
  alertState.set(b.host, new Set(list.map((x) => x.k)));
  // Charge sessions: notify once when a new one closes (not for ones that closed before this page loaded).
  const lc = b.last_charge, seen = chargeSeen.get(b.host);
  if (lc && lc.id !== seen) {
    chargeSeen.set(b.host, lc.id);
    if (seen !== undefined && can) {
      const body = `${dur(lc.duration_s)}, ${Math.round(lc.soc0 * 100)} → ${Math.round(lc.soc1 * 100)}%, +${Math.round(lc.wh)} Wh`;
      if (lc.reason === "limit" && a.charge_done) new Notification(`${boardName(b)}: charge finished`, { body });
      else if (lc.reason === "offline" && a.offline_charging) new Notification(`${boardName(b)}: went offline while charging`, { body });
      else if (lc.reason !== "limit" && lc.reason !== "offline" && a.charge_early) new Notification(`${boardName(b)}: charging stopped (${lc.reason_text})`, { body });
    }
    forget("charges|" + b.host);
    forget("an|" + b.host);
  } else if (!lc && seen === undefined) chargeSeen.set(b.host, null);
}

function scanStatus(s) {
  const ds = s.discovery || {}, n = s.discovered.length;
  const st = s.discovery_error ? `<span class="bad-t">${esc(s.discovery_error)}</span>`
    : ds.scanning ? `Searching ${esc(ds.subnet || "your network")} for boards…`
    : ds.last_scan_age != null ? (n ? `Found ${n} board${n > 1 ? "s" : ""} not added yet (listed at the top).` : `No new boards on ${esc(ds.subnet)}. Boards only join WiFi while they're awake (for example on the charger).`)
    : "";
  const again = ds.scanning || !canWrite() ? "" : ` <button type="button" class="small ghost" data-scan>Search again</button>`;
  return (st ? `<span class="muted small">${st}</span>${again}` : again) + bleStatus(s);
}

// Bluetooth search (Mac app only). Started by hand: identifying a board holds its one Bluetooth connection.
function bleStatus(s) {
  const b = s.ble || {};
  if (!b.available) return "";
  const n = (b.found || []).length;
  const st = b.searching ? "Searching Bluetooth…"
    : b.error ? `<span class="bad-t">${esc(b.error)}</span>`
    : b.last_scan_age != null ? (n ? `Found ${n} board${n > 1 ? "s" : ""} over Bluetooth (listed at the top).` : "No new boards over Bluetooth. A board connected to VESC Tool over Bluetooth doesn't show up.")
    : "No network? Boards can also connect over Bluetooth.";
  const btn = b.searching || !canWrite() ? "" : ` <button type="button" class="small ghost" data-blescan>Search Bluetooth</button>`;
  return `<span class="scanbreak"></span><span class="muted small">${st}</span>${btn}`;
}

function render() {
  const s = S.state;
  if (!s) return;
  s.boards.forEach(notify);
  document.body.classList.toggle("viewer", !canWrite());
  $("#skipChip").hidden = !skipConfirm();
  $("#viewerBar").hidden = canWrite();
  $("#btnAdd").disabled = !canWrite();

  // Network search: only boards found and not added yet get a bar. What the search is doing
  // stays quiet, in the Add board dialog (and the empty-state card).
  const disc = s.discovered;
  const label = (d) => d.nickname || d.name || (d.busy ? "VESC device (busy)" : d.hw_name || "VESC device");
  const found = disc.map((d) => `<span class="found" title="${d.mac ? "MAC " + esc(d.mac) + (d.version ? " · FW " + esc(d.version) : "") : d.busy ? "Something (probably VESC Tool) is connected to it, so it couldn't be identified" : ""}">
    <b>${esc(label(d))}</b> <span class="mono">${esc(d.host)}</span> <button type="button" class="small" data-add="${esc(d.host)}"${canWrite() ? "" : " disabled"}>Add</button></span>`).join("");
  const bfound = (s.ble && s.ble.found) || [];
  const blabel = (d) => d.mac ? d.nickname || d.hw_name || d.name || "VESC device" : `${d.name || "VESC device"} (not identified)`;
  const bfoundHtml = bfound.map((d) => `<span class="found" title="${d.mac ? "MAC " + esc(d.mac) + (d.version ? " · FW " + esc(d.version) : "") + (d.rssi ? ` · signal ${d.rssi} dBm` : "") : "Couldn't connect to it to identify it (busy, asleep or out of range). Search again."}">
    <b>${esc(blabel(d))}</b> <span class="muted small">Bluetooth${d.name && d.mac && d.name !== blabel(d) ? " · " + esc(d.name) : ""}</span> <button type="button" class="small" data-add="${esc(d.host)}"${canWrite() && d.mac ? "" : " disabled"}>Add</button></span>`).join("");
  $("#discovered").hidden = !disc.length && !bfound.length;
  setHtml($("#discovered"), (disc.length ? "<b>Found on your network:</b> " + found : "") +
    (bfound.length ? `${disc.length ? "<br>" : ""}<b>Found over Bluetooth:</b> ` + bfoundHtml : ""));
  $("#discoveryError").hidden = true;
  // Battery-drain warning: shown while a board is connected, until this viewer dismisses it.
  const awake = s.boards.some((b) => b.state === "online") && !store("vbms_awake_ok");
  $("#awakeNotice").hidden = !awake;
  setHtml($("#awakeNotice"), awake ? `<span><b>⚠ Connected boards stay awake and slowly drain the pack.</b> ${esc(AWAKE_WARN)} ${esc(AWAKE_FIX)}</span>
    <button type="button" class="small ghost" data-awake-ok>Got it</button>` : "");
  setHtml($("#addScan"), $("#addDlg").open ? scanStatus(s) : "");
  setHtml($("#emptyScan"), s.boards.length ? "" : scanStatus(s));

  const cur = currentBoard();
  setHtml($("#pills"), (s.boards.length > 1 ? `<button type="button" class="pill all${cur === ALL ? " on" : ""}" data-pick="${ALL}">All boards</button>` : "") +
    s.boards.map((b) => pill(b, cur !== ALL && cur && cur.host === b.host)).join(""));

  $("#empty").hidden = s.boards.length > 0;
  $("#tabs").hidden = !cur || cur === ALL;
  const view = $("#view");
  if (!cur) { view.innerHTML = ""; view.dataset.view = ""; return; }
  if (cur === ALL) { allView(view, s.boards); return; }

  setHtml($("#tabs"), TABS.map(([id, label]) => `<button type="button" role="tab" class="tab${S.route.tab === id ? " on" : ""}" data-tab="${id}" aria-selected="${S.route.tab === id}">${label}</button>`).join(""));
  const tab = TABS.find((t) => t[0] === S.route.tab);
  tab[3](view, cur, S.detail.get(cur.host));

  // First time a new board is seen (or its safety warning isn't accepted yet): open the setup wizard once per page.
  if (canWrite() && !wizardOpen() && !document.querySelector("dialog[open]")) {
    const nb = s.boards.find((b) => b.mac && b.state === "online" && (b.setup_done === false || b.risk_ok === false) && !wizardSeen.has(b.mac));
    if (nb) {
      wizardSeen.add(nb.mac);
      if (cur.host !== nb.host) go(nb.host, "overview");
      openWizard(nb.host, true);
    }
  }
}

// ---------------------------------------------------------------- actions

async function act(btn) {
  const cur = currentBoard();
  const host = btn.dataset.host;
  const b = host ? S.state.boards.find((x) => x.host === host) : cur && cur !== ALL ? cur : null;
  if (!b) return;
  const a = btn.dataset.act;
  try {
    if (a === "limit") return b.charge && openChargeSheet(b);
    if (a === "cells" || a === "temps") { toggleOpen(b.host + "|" + a); return render(); }
    if (a === "charge" || a === "allow-charge") return toggleCharge(b);
    if (a === "allow-balance") return toggleBalance(b);
    if (a === "balance") return balanceTap(b);
    if (a === "later") { if (!canWrite()) return toast("View-only on this device. Pair it to set timers."); return openLaterSheet(b, JSON.parse(btn.dataset.init || "{}")); }
    if (a === "timer-cancel") return cancelTimer(b, btn.dataset.id);
    if (a === "csvdl") {
      const now = Math.floor(Date.now() / 1000), sp = Number(btn.dataset.span);
      location.href = `/api/history/csv?host=${encodeURIComponent(b.host)}&from=${sp ? now - sp : 0}&to=${now}`;
      return;
    }
    if (!canWrite()) return;
    if (a === "restore") { await restoreBackup(b, btn.dataset.id); forget("bk|" + b.host); return render(); }
    if (a === "addon") return installAddon(b, btn.dataset.id);
    if (a === "rt") return runtime(b, btn.dataset.cmd, Number(btn.dataset.val), btn.textContent.trim());
    if (a === "wizard") return openWizard(b.host);
    if (a === "pause" || a === "resume") await api("/api/board/pause", { host: b.host, pause: a === "pause" });
    if (a === "link-ble" || a === "link-wifi") {
      const ble = a === "link-ble";
      if (ble && !window.confirm(`Connect to ${boardName(b)} over Bluetooth instead of WiFi?\n\nThe Mac needs to be within Bluetooth range of the board. VBMS Manager checks it's the same board (by its MAC) before using it. It stays on Bluetooth until you switch back.`)) return;
      await api("/api/board/link", { host: b.host, link: ble ? "ble" : "wifi" });
      toast(ble ? `Connecting to ${boardName(b)} over Bluetooth…` : `Back to WiFi for ${boardName(b)}.`);
    }
    if (a === "remove") {
      if (!window.confirm(`Remove ${boardName(b)} from this list? Its saved settings are kept.`)) return;
      await api("/api/hosts/remove", { host: b.host });
      go("", "overview");
    }
    await refresh();
    reloadDetail(b.host);
  } catch (e) { fail(e); }
}

// Balance indicator: toggles the live Force Balance command. "Off" hands control back to the script.
async function balanceTap(b) {
  const bs = balanceState(b);
  if (bs.k === "offline") return;
  if (!canWrite()) return toast("View-only on this device. Pair it to control balancing.");
  const name = boardName(b), on = bs.k !== "forced";
  const q = on ? `Force balancing on ${name}?\n\nLive command: it isn't saved and resets when the BMS restarts or sleeps.`
    : `Turn Force Balance off on ${name}?\n\nThe script goes back to balancing automatically when its conditions are met.`;
  if (ask(q) && await sendRuntime(b, "force_balance", on ? 1 : 0)) toast(on ? `Force balance on (${name}).` : `Balancing back to automatic (${name}).`);
}

// role="button" elements that aren't <button> (the SOC ring and its limit tick) work from the keyboard too.
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches('[role="button"]:not(button)')) { e.preventDefault(); e.target.dispatchEvent(new MouseEvent("click", { bubbles: true })); }
});

document.addEventListener("click", (e) => {
  const t = e.target;
  const pick = t.closest("[data-pick]");
  if (pick) return go(pick.dataset.pick, null);
  const tab = t.closest("[data-tab]");
  if (tab) return go(null, tab.dataset.tab);
  if (t.closest("[data-awake-ok]")) { store("vbms_awake_ok", true); render(); return; }
  if (t.closest("[data-blescan]")) { api("/api/ble/scan", {}).then(() => setTimeout(refresh, 300)).catch((x) => showMsg("Couldn't search Bluetooth", esc(x.message))); return; }
  if (t.closest("[data-scan]")) { api("/api/scan", {}).then(() => setTimeout(refresh, 300)).catch((x) => showMsg("Couldn't search", esc(x.message))); return; }
  const add = t.closest("[data-add]");
  if (add) { api("/api/hosts/add", { host: add.dataset.add }).then(refresh).catch((x) => showMsg("Couldn't add", esc(x.message))); return; }
  const btn = t.closest("[data-act]");
  if (btn && btn.tagName !== "INPUT") { e.preventDefault(); return act(btn); }
  const gt = t.closest("[data-goto]");
  if (gt && !t.closest("a, input, select, label, [data-drag], [data-edit], [data-lock], .applybar")) return go(null, gt.dataset.goto);
});


// Tooltips: hover on desktop, tap on touch screens.
let tip = null;
function showTip(el) {
  if (!tip) { tip = document.createElement("div"); tip.className = "tip"; document.body.appendChild(tip); }
  tip.textContent = el.dataset.tip; tip.hidden = false;
  placeTip(tip, el.getBoundingClientRect());
}
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-tip]");
  if (el) showTip(el); else if (tip) tip.hidden = true;
});
document.addEventListener("focusin", (e) => { const el = e.target.closest("[data-tip]"); if (el) showTip(el); });
document.addEventListener("pointerdown", (e) => {
  if (e.pointerType !== "touch") return;
  const el = e.target.closest("[data-tip]");
  if (el) showTip(el); else if (tip) tip.hidden = true;
});
window.addEventListener("scroll", () => { if (tip) tip.hidden = true; }, { passive: true });

// ---------------------------------------------------------------- header dialogs

$("#btnAdd").onclick = () => { $("#addDlg").showModal(); $("#addHost").focus(); render(); };
$("#addCancel").onclick = () => { $("#addHost").value = ""; $("#addDlg").close(); };
$("#addForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const h = $("#addHost").value.trim();
  if (!h) return;
  try { await api("/api/hosts/add", { host: h }); $("#addHost").value = ""; $("#addDlg").close(); await refresh(); go(h, "overview"); } catch (x) { showMsg("Couldn't add", esc(x.message)); }
});
$("#btnGuide").onclick = () => openGuide();
$$("[data-guide]").forEach((b) => (b.onclick = () => openGuide(Number(b.dataset.guide) || 0)));
$("#btnDevices").onclick = () => { drawDevices(); $("#devDlg").showModal(); };
$("#viewerPair").onclick = () => { drawDevices(); $("#devDlg").showModal(); };
$("#btnLogs").onclick = () => { $("#logDlg").showModal(); showLog("changes"); };
hooks.openRecording = () => { if (!$("#logDlg").open) $("#logDlg").showModal(); showLog("recording"); };
$$("#logDlg [data-log]").forEach((t) => (t.onclick = () => showLog(t.dataset.log)));
const AL_CHECKS = [["alNotify", "notify"], ["alDone", "charge_done"], ["alEarly", "charge_early"], ["alOffline", "offline_charging"]];
$("#btnAlerts").onclick = () => {
  const a = alertCfg();
  $("#alDelta").value = a.delta; $("#alTemp").value = a.temp; $("#alOver").value = a.over;
  for (const [id, k] of AL_CHECKS) $("#" + id).checked = !!a[k];
  $("#alSave").disabled = !canWrite();
  $("#alertDlg").showModal();
};
$("#alSave").onclick = async () => {
  const a = { delta: Number($("#alDelta").value) || 30, temp: Number($("#alTemp").value) || 55, over: Number($("#alOver").value) || 30 };
  for (const [id, k] of AL_CHECKS) a[k] = $("#" + id).checked;
  try {
    await api("/api/alerts", a);
    $("#alertDlg").close();
    if (AL_CHECKS.some(([, k]) => a[k]) && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
    await refresh();
  } catch (x) { showMsg("Couldn't save alerts", esc(x.message)); }
};

// Preferences: write confirmations on/off. Turning them off needs the warning dialog (and this computer).
$("#btnPrefs").onclick = () => openPrefs();
$("#skipChip").onclick = () => openPrefs();
function openPrefs() {
  const local = S.state.access.role === "local", cs = S.state.settings.confirm_skip;
  $("#prefConfirm").checked = !skipConfirm();
  $("#prefConfirm").disabled = !canWrite() || (!local && !skipConfirm());
  $("#prefNote").innerHTML = skipConfirm() ? `<span class="bad-t">Confirmations are off</span> since ${esc(stamp12(cs.since))}. ⛔ danger changes and restores still ask.`
    : local ? "Recommended: keep this on." : "Only the computer running VBMS Manager can turn confirmations off.";
  $("#prefDlg").showModal();
}
$("#prefConfirm").onchange = async (e) => {
  const want = e.target.checked;
  e.target.checked = !want; // stays as it was until the change is saved
  if (want) {
    try { await api("/api/prefs", { confirm_skip: false }); await refresh(); toast("Write confirmations are back on."); openPrefs(); } catch (x) { fail(x); }
    return;
  }
  const dlg = $("#skipDlg"), phrase = $("#skipPhrase"), chk = $("#skipCheck"), ok = $("#skipOk");
  phrase.value = ""; chk.checked = false; ok.disabled = true;
  const upd = () => (ok.disabled = !(chk.checked && phrase.value.trim().toLowerCase() === "skip confirmations"));
  phrase.oninput = upd; chk.onchange = upd;
  dlg.returnValue = "";
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok" || ok.disabled) return;
    try { await api("/api/prefs", { confirm_skip: true, phrase: phrase.value }); await refresh(); toast("Write confirmations are off."); openPrefs(); } catch (x) { fail(x); }
  };
  $("#prefDlg").close();
  dlg.showModal();
};

// Alerts used to live in this browser only; move them to VBMS Manager once so every device shares them.
let alertsMigrated = false;
function migrateAlerts() {
  if (alertsMigrated || !S.state || !canWrite()) return;
  alertsMigrated = true;
  let old = null;
  try { old = JSON.parse(localStorage.getItem("vbms_alerts") || "null"); } catch {}
  if (old && !S.state.settings.alerts_saved) api("/api/alerts", old).then(() => { try { localStorage.removeItem("vbms_alerts"); } catch {} refresh(); }).catch(() => {});
}

let evKind = "";
async function showLog(name) {
  $$("#logDlg [data-log]").forEach((t) => t.classList.toggle("on", t.dataset.log === name));
  const body = $("#logBody");
  if (name === "recording") return showRecording(body);
  try {
    const rows = (await api("/api/log?name=" + name + "&limit=" + (name === "events" ? 2000 : 300))).reverse();
    if (!rows.length) { body.innerHTML = `<p class="muted">Nothing logged yet.</p>`; return; }
    if (name === "changes") {
      const what = (r) => {
        if (r.event === "runtime") return esc(`${r.cmd} = ${r.value}`) + (r.why ? ` <span class="muted">(${esc(r.why)})</span>` : "");
        if (r.event === "schedule") return `Schedule ${r.enabled ? "saved" : "off"}<br><span class="muted">${(r.rules || []).map(esc).join("<br>")}</span>`;
        if (r.event === "timer") return `Timer for ${esc(r.at)}: ${esc(r.what)}${r.confirm === "skipped" ? ' <span class="muted">(no confirmation)</span>' : ""}`;
        if (r.event === "prefs") return `Write confirmations turned <b>${r.confirm_skip ? "off" : "on"}</b>`;
        if (r.rows && r.rows.length) return r.rows.map((x) => `${esc(x.label)}: ${esc(x.old)} → <b>${esc(x.new)}</b>`).join("<br>") + `<br><span class="muted">${esc(r.reason || "")}</span>`;
        return `start ${f3(r.old?.vc_charge_start)} → ${f3(r.new?.vc_charge_start)}<br>end ${f3(r.old?.vc_charge_end)} → ${f3(r.new?.vc_charge_end)}<br><span class="muted">${esc(r.reason || "")}</span>`;
      };
      body.innerHTML = `<table><tr><th>Time</th><th>Board</th><th>Change</th><th>Result</th></tr>${rows.map((r) => `<tr><td>${esc(stamp12(r.time))}</td><td>${esc(r.name || "")}<br><span class="mono muted">${esc(r.mac)}</span></td><td>${what(r)}</td><td>${esc(r.result || (r.event === "config_write" ? "" : r.event === "runtime" ? "sent" : "saved"))}${r.confirm ? `<br><span class="muted small">${esc(r.confirm)}</span>` : ""}${r.error ? "<br>" + esc(r.error) : ""}</td></tr>`).join("")}</table>`;
    } else {
      const kinds = [...new Set(rows.map((r) => r.kind))].sort();
      const shown = rows.filter((r) => !evKind || r.kind === evKind).slice(0, 500);
      body.innerHTML = `<label class="row small">Show <select id="evKind"><option value="">all events (${rows.length})</option>${kinds.map((k) => `<option${k === evKind ? " selected" : ""}>${esc(k)}</option>`).join("")}</select></label>
        <table><tr><th>Time</th><th>Board</th><th>Event</th></tr>${shown.map((r) => `<tr><td>${esc(stamp12(r.time))}</td><td>${esc(r.name || r.host || "")}</td><td><b>${esc(r.kind)}</b> ${esc(r.text)}</td></tr>`).join("")}</table>`;
      $("#evKind").onchange = (e) => { evKind = e.target.value; showLog("events"); };
    }
  } catch (e) { body.textContent = e.message; }
}

const mb = (n) => (n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.round(n / 1e3) + " kB");

function showRecording(body) {
  const st = S.state.settings.logging, rec = S.state.recording;
  const cells = Math.max(16, ...S.state.boards.map((b) => (b.values ? b.values.cells.length : b.cells_configured || 0)));
  const est = () => {
    const iv = Number($("#recIv").value), lvl = $("#recLvl").value;
    // Row sizes measured from real files (32 cells, 8 sensors: ~490 bytes at "detailed").
    let row = 150;
    if (lvl !== "summary") row += cells * 7.5 + 8 * 6 + 100;
    if (lvl === "everything") row += 8 * 7 + 120;
    const day = (86400 / iv) * row;
    $("#recEst").textContent = `≈ ${mb(day)} per board per day while it's connected, roughly ${mb(day / 8)} once the day is compressed.`;
  };
  const opt = (vals, cur, lab) => vals.map((v) => `<option value="${v}"${v === cur ? " selected" : ""}>${lab(v)}</option>`).join("");
  const lvlLabel = { summary: "Summary: pack V, A, W, SOC, lowest/highest cell, spread, hottest temp, charger",
    detailed: "Detailed: + every cell, every temp sensor, balancing, Ah/Wh counters",
    everything: "Everything: + raw status, ADC temps, IC/board temps, charge settings" };
  body.innerHTML = `<div class="recform">
    <label class="check"><input type="checkbox" id="recOn"${st.enabled ? " checked" : ""}> Record history for every board</label>
    <label class="row">Every <select id="recIv">${opt(rec.intervals, st.interval_s, (v) => (v < 60 ? v + " s" : v / 60 + " min"))}</select></label>
    <p class="muted small">Values are averaged over the interval; lowest/highest cell and the hottest temperature keep their extremes.</p>
    <label class="row">Detail <select id="recLvl">${opt(rec.levels, st.level, (v) => lvlLabel[v])}</select></label>
    <label class="row">Keep <select id="recRet">${opt(rec.retentions, st.retention_days, (v) => (v ? v + " days" : "forever"))}</select></label>
    <p class="small" id="recEst"></p>
    <p class="muted small">History is stored in ~/.vbms_manager/history (${mb(rec.bytes)} now). Charge sessions and events are always kept. Weak-cell drift and self-discharge need Detailed or Everything. Charge sessions are measured from every 1 s reading whatever the interval.</p>
    <div class="dlg-actions"><button type="button" id="recSave"${canWrite() ? "" : " disabled"}>Save</button></div></div>`;
  $("#recIv").onchange = est; $("#recLvl").onchange = est; est();
  $("#recSave").onclick = async () => {
    try {
      await api("/api/logging/settings", { enabled: $("#recOn").checked, interval_s: Number($("#recIv").value), level: $("#recLvl").value, retention_days: Number($("#recRet").value) });
      resetHistoryCache();
      await refresh();
      showRecording(body);
      toast("Recording settings saved.");
    } catch (x) { showMsg("Couldn't save", esc(x.message)); }
  };
}

// ---------------------------------------------------------------- start

initWizard();
initGuide();
initDevices();
readRoute();
window.addEventListener("hashchange", () => { readRoute(); render(); loadDetailIfNeeded(true); window.scrollTo({ top: 0 }); });
refresh().then(() => { migrateAlerts(); loadDetailIfNeeded(true); });
setInterval(() => { refresh().then(() => loadDetailIfNeeded()); }, 1000);

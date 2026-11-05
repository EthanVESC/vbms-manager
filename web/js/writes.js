// Everything that changes a board goes through here: confirm dialogs and the
// prepare -> confirm -> commit flow, the charge limit sheet, the allow toggles
// and one-time timers ("Later").

import { $, allowState, api, switching, ask, boardName, canWrite, cellsCount, clock12, endFor, esc, f1, f3, fail, hooks, S, showMsg, skipConfirm, toast, whenAt } from "./core.js";
import { socRing } from "./viz.js";

const RISK = { caution: "⚠ Caution", danger: "⛔ Danger" };

function riskHtml(risks) {
  return (risks || []).map((r) => `<div class="risk ${r.level}"><b>${RISK[r.level] || r.level}</b> ${esc(r.text)}</div>`).join("");
}

// Skip confirmations (Preferences) never covers ⛔ danger changes or restores.
const skippable = (p) => skipConfirm() && !p.confirm_name && p.kind !== "restore";

// Generic confirm dialog for a write proposal from the server.
export function confirmWrite(p) {
  if (skippable(p)) return Promise.resolve({ ok: true, skipped: true });
  return new Promise((resolve) => {
    const dlg = $("#confirmDlg");
    const rows = (p.rows || []).map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(r.old)}</td><td class="new">${esc(r.new)}</td></tr>
      ${r.risks && r.risks.length ? `<tr class="riskrow"><td colspan="3">${riskHtml(r.risks)}</td></tr>` : ""}`).join("");
    const bytes = p.bytes_changed != null ? `${p.bytes_changed} bytes differ; ` : "";
    $("#confirmBody").innerHTML = `<p><b>${esc(p.name || "Board")}</b> · <span class="mono">${esc(p.host)}</span> · MAC <span class="mono">${esc(p.mac)}</span><br><span class="muted">${esc(p.reason)}</span></p>
      <table class="diff"><tr><th>Setting</th><th>Now</th><th>New</th></tr>${rows}</table>
      ${p.may_disconnect ? `<div class="risk danger"><b>Connection</b> WiFi/Bluetooth/CAN changes apply after the BMS restarts. If they're wrong you'll need USB or Bluetooth to fix them. A backup with your current WiFi key is saved first.</div>` : ""}
      ${skipConfirm() ? `<p class="small"><b>Asked even with confirmations off:</b> ${p.kind === "restore" ? "restoring a backup" : "this is a ⛔ danger change"}.</p>` : ""}
      ${p.kind === "script" ? `<p class="muted small">The BMS config isn't touched. A copy of the current script is saved first. The new script is read back and must match exactly before it's started; if anything fails, the current script is written back. This confirmation expires in 2 minutes.</p>`
        : `<p class="muted small">Only the settings above change (${bytes}every other setting is sent back exactly as read). The current config is backed up first and the board is read back afterwards to confirm. This confirmation expires in 2 minutes.</p>`}`;
    const chk = $("#confirmCheck"), ok = $("#confirmOk"), nameWrap = $("#confirmNameWrap"), name = $("#confirmName");
    nameWrap.hidden = !p.confirm_name;
    $("#confirmNameLabel").textContent = p.confirm_name || "";
    name.value = "";
    const upd = () => (ok.disabled = !chk.checked || (p.confirm_name && name.value.trim().toLowerCase() !== p.confirm_name.trim().toLowerCase()));
    chk.checked = false; upd();
    chk.onchange = upd; name.oninput = upd;
    dlg.onclose = () => resolve(dlg.returnValue === "ok" && !ok.disabled ? { ok: true, text: name.value } : { ok: false });
    dlg.returnValue = "";
    dlg.showModal();
  });
}

// during(fn): runs the commit, e.g. to show a switch as working while the board is written.
async function finish(b, prop, during = (fn) => fn()) {
  const body = { host: b.host, mac: b.mac };
  const c = await confirmWrite(prop);
  if (!c.ok) { api("/api/write/cancel", { id: prop.id }).catch(() => {}); return null; }
  const res = await during(async () => {
    const r = await api("/api/write/commit", { ...body, id: prop.id, confirm_text: c.text, skipped_confirm: !!c.skipped });
    await hooks.refresh();
    return r;
  });
  const list = (res.rows || []).map((r) => `${r.label}: ${r.new}`).join(", ");
  if (res.kind === "script") showMsg("Installed and verified", `<p>The board read back exactly the new script and restarted it. It beeps twice when it's up.</p>
    <p><b>${esc(res.undo)}</b></p>
    <p class="muted small">Copy of the previous script:<br><span class="mono">${esc(res.backup)}</span></p>`);
  else if (c.skipped) toast(`Written and verified: ${list}`);
  else showMsg("Written and verified", `<p>The board read back exactly what was written:</p>
    <ul>${(res.rows || []).map((r) => `<li>${esc(r.label)}: <b>${esc(r.new)}</b></li>`).join("")}</ul>
    <p class="muted small">Backup of the previous config:<br><span class="mono">${esc(res.backup)}</span></p>`);
  await hooks.reloadDetail(b.host);
  return res;
}

async function run(b, prepPath, prepBody) {
  const prop = await api(prepPath, { host: b.host, mac: b.mac, ...prepBody });
  return finish(b, prop);
}

export async function writeFields(b, changes) {
  try { return await run(b, "/api/config/prepare", { changes }); } catch (e) { fail(e); return null; }
}

export async function restoreBackup(b, id) {
  try { return await run(b, "/api/restore/prepare", { backup: id }); } catch (e) { fail(e); return null; }
}

// ---------------------------------------------------------------- Lisp script add-ons

const pluggedIn = (b) => !!(b.values && (b.values.is_charging || b.values.v_charge >= 5));

// The add-on panel for board setup (attr "data-wz") and the Board tab (attr "data-act").
export function addonPanel(b, attr) {
  const a = (b.addons || []).find((x) => x.id === "charger_latch");
  if (!a) return `<p class="muted small">Not read yet.</p>`;
  const blockers = [...(b.write_blockers || [])];
  if (a.state === "available" && pluggedIn(b)) blockers.push("unplug the charger first (the BMS script is stopped while it's replaced)");
  const chip = a.state === "installed" ? `<span class="chip ok">✓ Installed on this board</span>`
    : a.state === "available" ? `<span class="chip">Available · this board runs the ${esc(a.base)} script</span>`
    : `<span class="chip warn">Not available</span> <span class="small">${esc(a.why)}</span>`;
  return `<p class="small"><b>Charger latch</b> works around a bug, probably in the firmware: at the end of a charge the BMS goes to sleep, wakes straight back up, ignores Charge Start and charges again until Charge End, then sleeps again. So it cycles on and off rapidly while the charger is connected.</p>
    <p class="small">That doesn't happen while the BMS stays awake. The add-on checks for a charger on every pass of the script's main loop and keeps the BMS awake until a few seconds after the charger is unplugged. It's a workaround: it doesn't fix whatever is wrong in the firmware, and the BMS doesn't sleep while a charger is connected.</p>
    <p class="small">The latch script is by <b>${esc(a.credit)}</b>. It's the same sticky charge latch found in ${esc(a.same_as)}${a.app_url ? `, and if you'd like, you can <a href="${esc(a.app_url)}" target="_blank" rel="noopener">get the ${esc(a.same_as)} iOS app on the App Store</a>` : ""}.</p>
    <p>${chip}</p>
    ${a.state === "available" ? `<p class="small">Installing replaces the board's Lisp script with the stock vbms32 script plus the latch (comments removed). The BMS config and your settings aren't changed.</p>
      ${blockers.length ? `<div class="locked">Can't install right now:<ul>${blockers.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
      <div class="row"><button type="button" class="danger" ${attr}="addon" data-id="${esc(a.id)}"${blockers.length || !canWrite() ? " disabled" : ""}>Install charger latch…</button></div>` : ""}
    <div class="banner small"><b>Undo:</b> to remove it, re-install the BMS package from VESC Tool (VESC Packages page). VBMS Manager can't take it off.</div>`;
}

export async function installAddon(b, id) {
  const name = (b.addons || []).find((x) => x.id === id)?.name || "add-on";
  const busy = (fn) => {
    showMsg(`Installing ${name.toLowerCase()}…`, `<p>Writing the new script to ${esc(boardName(b))}. This can take up to half a minute. Keep the board connected and the charger unplugged.</p>`);
    return fn();
  };
  try {
    const prop = await api("/api/addon/prepare", { host: b.host, mac: b.mac, addon: id });
    return await finish(b, prop, busy);
  } catch (e) { fail(e); return null; }
}

export async function writeCharge(b, req) {
  try { return await run(b, "/api/charge/prepare", req); } catch (e) { fail(e); return null; }
}

export async function runtime(b, cmd, value, label) {
  if (!canWrite()) return;
  const extra = cmd === "charge_allowed" && b.schedule_status && b.schedule_status.channels && b.schedule_status.channels.charge
    ? "\n\nThe schedule leaves charging alone until its next change." : "";
  if (!ask(`${label} on ${boardName(b)}?\n\nThis is a live command. It isn't saved to the BMS and resets when the BMS restarts or sleeps.${extra}`)) return;
  await sendRuntime(b, cmd, value);
}

// Live command; the caller has already asked (or confirmations are off). True = sent.
export async function sendRuntime(b, cmd, value) {
  try {
    await api("/api/runtime", { host: b.host, mac: b.mac, cmd, value, confirm: true, skipped_confirm: skipConfirm() });
    await hooks.refresh();
    return true;
  } catch (e) { fail(e); return false; }
}

// ---------------------------------------------------------------- allow toggles (always fire)

// Shows the switch moving straight away and ignores more taps until fn() is done.
async function switchingTo(b, kind, to, fn) {
  const key = b.host + "|" + kind;
  switching.set(key, to);
  hooks.rerender();
  try { return await fn(); } finally { switching.delete(key); hooks.rerender(); }
}

export async function toggleCharge(b) {
  if (!canWrite()) return toast("View-only on this device. Pair it to control charging.");
  if (switching.has(b.host + "|charge")) return;
  const a = allowState(b, "charge"), name = boardName(b), to = a.on ? 0 : 1;
  const sched = b.schedule_status && b.schedule_status.channels && b.schedule_status.channels.charge;
  const q = to ? `Allow charging on ${name}?` : `Pause charging on ${name}?`;
  if (!ask(`${q}\n\nLive command: it isn't saved and resets when the BMS restarts or sleeps.${sched ? "\nThe schedule takes over again at its next change." : ""}`)) return;
  if (await switchingTo(b, "charge", !!to, () => sendRuntime(b, "charge_allowed", to))) toast(to ? `Charging allowed on ${name}.` : `Charging paused on ${name}.`);
}

export async function toggleBalance(b) {
  if (!canWrite()) return toast("View-only on this device. Pair it to control balancing.");
  const a = allowState(b, "balance");
  if (a.on == null || switching.has(b.host + "|balance")) return;
  // Working while preparing and while writing; the confirmation (if any) shows the real state.
  const working = (fn) => switchingTo(b, "balance", !a.on, fn);
  try {
    const prop = await working(() => api("/api/balance/prepare", { host: b.host, mac: b.mac, allow: !a.on }));
    if (prop.noop) { await hooks.refresh(); return toast("The board already has that."); }
    if (await finish(b, prop, working)) toast(a.on ? `Balancing off on ${boardName(b)} (Balance Max Current 0 A).` : `Balancing allowed again on ${boardName(b)}.`);
  } catch (e) { fail(e); }
}

// ---------------------------------------------------------------- "when" picker (timers)

const HOURS = Array.from({ length: 12 }, (_, i) => i + 1);
const MINS = Array.from({ length: 12 }, (_, i) => i * 5);
const pad = (n) => String(n).padStart(2, "0");
const opts = (vals, cur, lab = (v) => v) => vals.map((v) => `<option value="${v}"${v === cur ? " selected" : ""}>${lab(v)}</option>`).join("");

// 12-hour time selects: hour, minute (5 min steps), AM/PM. name: prefix for the selects.
export function timeSelects(name, hhmm) {
  const [h, m] = (hhmm === "24:00" ? "00:00" : hhmm || "00:00").split(":").map(Number);
  const mm = Math.round(m / 5) * 5 % 60;
  return `<span class="tsel"><select name="${name}_h" aria-label="Hour">${opts(HOURS, h % 12 || 12)}</select><b>:</b><select name="${name}_m" aria-label="Minute">${opts(MINS, mm, pad)}</select><select name="${name}_p" aria-label="AM or PM">${opts(["AM", "PM"], h < 12 ? "AM" : "PM")}</select></span>`;
}
export function readTime(form, name) {
  const h = Number(form.elements[name + "_h"].value) % 12 + (form.elements[name + "_p"].value === "PM" ? 12 : 0);
  return `${pad(h)}:${pad(Number(form.elements[name + "_m"].value))}`;
}

function whenPicker() {
  const now = new Date(Date.now() + 3600e3);
  return `<div class="when">
    <div class="presets">${[[600, "10 min"], [1800, "30 min"], [3600, "1 h"], [7200, "2 h"], [14400, "4 h"], [28800, "8 h"]].map(([s, l]) => `<button type="button" class="chipbtn" data-in="${s}">in ${l}</button>`).join("")}</div>
    <div class="row wrow"><label class="check"><input type="radio" name="wmode" value="in" checked> In</label>
      <select name="in_h" aria-label="Hours">${opts(Array.from({ length: 24 }, (_, i) => i), 0, (v) => v + " h")}</select>
      <select name="in_m" aria-label="Minutes">${opts(MINS, 10, (v) => v + " min")}</select></div>
    <div class="row wrow"><label class="check"><input type="radio" name="wmode" value="at"> At</label>${timeSelects("at", `${pad(now.getHours())}:00`)}</div>
    <p class="small" data-role="fires"></p></div>`;
}

// Epoch seconds the picker points at.
function pickedAt(form) {
  if (form.elements.wmode.value === "in") return Date.now() / 1000 + Number(form.elements.in_h.value) * 3600 + Number(form.elements.in_m.value) * 60;
  const [h, m] = readTime(form, "at").split(":").map(Number);
  const d = new Date(); d.setHours(h, m, 0, 0);
  if (d.getTime() <= Date.now() + 30e3) d.setDate(d.getDate() + 1);
  return d.getTime() / 1000;
}

function wireWhen(form, onChange) {
  const box = form.querySelector(".when");
  box.addEventListener("click", (e) => {
    const t = e.target.closest("[data-in]");
    if (!t) return;
    const s = Number(t.dataset.in);
    form.elements.in_h.value = Math.floor(s / 3600);
    form.elements.in_m.value = Math.floor((s % 3600) / 60);
    form.elements.wmode.value = "in";
    onChange();
  });
  box.addEventListener("change", (e) => {
    if (/^at_/.test(e.target.name)) form.elements.wmode.value = "at";
    if (/^in_/.test(e.target.name)) form.elements.wmode.value = "in";
    onChange();
  });
  const fires = () => (box.querySelector("[data-role=fires]").innerHTML = `Fires <b>${whenAt(pickedAt(form))}</b>`);
  fires();
  return fires;
}

// prepare -> (approve) -> commit a timer. t: {at, pct, charge: "on"|"off", balance: "on"|"off"}
export async function setTimer(b, t) {
  try {
    const p = await api("/api/timer/prepare", { host: b.host, mac: b.mac, timer: t });
    const lim = p.timer.actions.limit;
    const what = [
      "charge" in p.timer.actions ? `${p.timer.actions.charge ? "Allow" : "Pause"} charging (live command)` : "",
      lim ? `Charge limit ${lim.pct}%: end <b>${f3(lim.volts.end)} V</b> / start <b>${f3(lim.volts.start)} V</b> (config write)` : "",
      "balance" in p.timer.actions ? (p.timer.actions.balance ? `Allow balancing: Balance Max Current back to <b>${f1(p.balance_normal_a)} A</b> (config write)` : "Stop balancing: Balance Max Current <b>0 A</b> (config write)") : "",
    ].filter(Boolean);
    const ok = skipConfirm() || await approve("Approve timer", `<p><b>${esc(p.name)}</b> · <span class="mono">${esc(p.host)}</span></p>
      <p>At <b>${esc(whenAt(p.timer.at))}</b> (${esc(clock12(p.timer.at, true))}):</p><ul>${what.map((x) => `<li>${x}</li>`).join("")}</ul>
      ${p.blockers && p.blockers.length ? `<div class="banner bad">Writes are blocked on this board right now (${p.blockers.map(esc).join("; ")}). The timer will fail unless that's fixed by then.</div>` : ""}
      <p class="small">The timer beats the schedule when it fires. The schedule takes over again at its next change. Writes get the same checks, backup, read-back and log as a manual change. VBMS Manager has to be running then.</p>`,
    "I approve VBMS Manager doing this automatically at that time.", "Set timer");
    if (!ok) return false;
    await api("/api/timer/commit", { host: b.host, mac: b.mac, id: p.id, skipped_confirm: skipConfirm() });
    await hooks.refresh();
    toast(`Timer set: ${p.timer.label}, ${whenAt(p.timer.at)}.`);
    return true;
  } catch (e) { fail(e); return false; }
}

export async function cancelTimer(b, id) {
  try { await api("/api/timer/cancel", { host: b.host, mac: b.mac, timer_id: id }); await hooks.refresh(); toast("Timer cancelled."); } catch (e) { fail(e); }
}

// The approval dialog (#schedDlg) used by schedules and timers. Resolves true when approved.
export function approve(title, html, checkText, okText) {
  return new Promise((resolve) => {
    const dlg = $("#schedDlg");
    $("#schedTitle").textContent = title;
    $("#schedBody").innerHTML = html;
    $("#schedCheckText").textContent = checkText;
    const chk = $("#schedCheck"), ok = $("#schedOk");
    ok.textContent = okText;
    chk.checked = false; ok.disabled = true;
    chk.onchange = () => (ok.disabled = !chk.checked);
    dlg.returnValue = "";
    dlg.onclose = () => resolve(dlg.returnValue === "ok" && chk.checked);
    dlg.showModal();
  });
}

// Pending timers, each with a cancel button.
export function timerList(b) {
  const ts = (b.schedule_status && b.schedule_status.timers) || [];
  if (!ts.length) return "";
  return `<ul class="timers">${ts.map((t) => `<li><span class="tm-ico" aria-hidden="true">⏱</span><span><b>${esc(whenAt(t.at))}</b> ${esc(t.label)}</span>
    <button type="button" class="icon ghost" data-act="timer-cancel" data-host="${esc(b.host)}" data-id="${esc(t.id)}" aria-label="Cancel timer"${canWrite() ? "" : " disabled"}>✕</button></li>`).join("")}</ul>`;
}

// "Later…": start/stop charging, a limit, start/stop balancing at a time.
export function openLaterSheet(b, init = {}) {
  const dlg = $("#sheetDlg"), lim = S.state.limits, form = $("#sheetDlg form"), body = $("#sheetBody");
  $("#sheetTitle").textContent = "Later · " + boardName(b);
  const seg = (name, cur, choices) => `<div class="seg" role="radiogroup">${choices.map(([v, l]) => `<label><input type="radio" name="${name}" value="${v}"${v === cur ? " checked" : ""}><span>${l}</span></label>`).join("")}</div>`;
  const pct = init.pct ?? (currentPct(b) ?? 80);
  body.innerHTML = `
    <div class="fgrid"><span class="small muted">Charging</span>${seg("charge", init.charge || "", [["", "Leave"], ["on", "Start"], ["off", "Stop"]])}
      <span class="small muted">Charge limit</span><label class="check"><input type="checkbox" name="setpct"${init.pct != null ? " checked" : ""}> Set to <b data-role="pct">${pct}%</b></label>
      <span></span><input type="range" name="pct" min="${lim.pct_min}" max="${lim.pct_max}" step="1" value="${pct}" aria-label="Charge limit">
      <span class="small muted">Balancing</span>${seg("balance", init.balance || "", [["", "Leave"], ["on", "Allow"], ["off", "Stop"]])}</div>
    ${whenPicker()}
    <p class="muted small">"Start charging" allows charging (it won't start if the pack is above Charge Start). "Stop balancing" sets Balance Max Current to 0 A.</p>`;
  const ok = $("#sheetOk");
  const upd = () => {
    body.querySelector("[data-role=pct]").textContent = form.elements.pct.value + "%";
    const any = form.elements.charge.value || form.elements.setpct.checked || form.elements.balance.value;
    ok.hidden = false;
    ok.disabled = !canWrite() || !any;
    ok.textContent = canWrite() ? "Set timer…" : "View-only: pair to change";
  };
  const fires = wireWhen(form, upd);
  body.oninput = (e) => { if (e.target.name === "pct") form.elements.setpct.checked = true; upd(); fires(); };
  body.onclick = null;
  ok.onclick = (e) => {
    e.preventDefault();
    const t = { at: pickedAt(form), charge: form.elements.charge.value || null, balance: form.elements.balance.value || null,
      pct: form.elements.setpct.checked ? Number(form.elements.pct.value) : null };
    dlg.close();
    setTimer(b, t);
  };
  upd();
  dlg.showModal();
}

// ---------------------------------------------------------------- charge limit sheet

export function chargeBlockers(b) {
  const out = [...(b.write_blockers || [])];
  if (!b.cell) out.push("choose this board's cell type (Board tab → Edit board setup)");
  else if (!b.cell.accepted) out.push("review and accept the SOC curve (Board tab → Edit board setup)");
  return out;
}

export function currentPct(b) {
  return b.cell && b.cell.current_pct != null ? Math.round(b.cell.current_pct) : null;
}

export function openChargeSheet(b) {
  const dlg = $("#sheetDlg"), form = $("#sheetDlg form");
  const lim = S.state.limits;
  const blockers = chargeBlockers(b);
  const ch = b.charge;
  const n = cellsCount(b);
  const cur = currentPct(b);
  let pct = cur != null ? Math.max(lim.pct_min, Math.min(lim.pct_max, cur)) : 80;
  $("#sheetTitle").textContent = "Charge limit · " + boardName(b);
  const body = $("#sheetBody");
  const gap = ch.vc_charge_end - ch.vc_charge_start;
  const gapFix = !ch.gap_ok && !(b.write_blockers || []).length
    ? `<div class="banner${ch.gap_valid ? "" : " bad"}">Charge Start is ${f3(gap)} V below End${ch.gap_valid ? " (a custom gap)" : ", outside 0.050–0.200 V"}. <button type="button" class="small" data-sheet="fixgap"${canWrite() ? "" : " disabled"}>Set Start to ${f3(ch.vc_charge_end - 0.1)} V (0.100 V gap)</button></div>` : "";
  body.innerHTML = `${gapFix}
    <div class="sheet-bat" data-role="bat"></div>
    <div class="now muted">Now: end <b>${f3(ch.vc_charge_end)} V</b> · start <b>${f3(ch.vc_charge_start)} V</b>${cur != null ? ` · ≈ ${cur}%` : ""}</div>
    ${blockers.length ? `<div class="locked">Charge changes are locked:<ul>${blockers.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : `
    <div class="seg" role="radiogroup" aria-label="When"><label><input type="radio" name="when" value="now" checked><span>Now</span></label><label><input type="radio" name="when" value="later"><span>Later</span></label></div>
    <div class="stepper"><button type="button" class="round ghost" data-sheet="minus" aria-label="1% less">−</button>
      <input type="range" min="${lim.pct_min}" max="${lim.pct_max}" step="1" value="${pct}" data-role="pct" aria-label="Charge limit">
      <button type="button" class="round ghost" data-sheet="plus" aria-label="1% more">+</button></div>
    <div class="presets">${[70, 80, 90, 100].map((p) => `<button type="button" class="chipbtn" data-sheet="preset" data-p="${p}">${p}%</button>`).join("")}</div>
    <div class="preview" data-role="preview"></div>
    <div data-role="later" hidden><label class="check"><input type="checkbox" name="startchg"> Also start charging then</label>${whenPicker()}</div>
    <p class="muted small">The BMS stops when the highest cell reaches Charge End while current is flowing, so the resting voltage, and true charge, end up slightly below the target. Start is End − 0.100 V${ch.gap_ok ? "" : " (this sets the gap back to 0.100 V)"}. For a custom gap, drag the lines on the Cells tab.</p>`}`;
  const r = body.querySelector("[data-role=pct]");
  const later = () => !!(form.elements.when && form.elements.when.value === "later");
  let fires = () => {};
  const upd = () => {
    if (r) pct = Number(r.value);
    const end = endFor(pct, b.cell);
    body.querySelector("[data-role=bat]").innerHTML = socRing(b.values ? b.values.soc : null, { limitPct: pct, act: false, label: `limit → ${pct}%` });
    const pv = body.querySelector("[data-role=preview]");
    if (pv) pv.innerHTML = end == null ? "" : `New: end <b>${f3(end)} V</b> · start <b>${f3(end - 0.1)} V</b> · pack ≈ <b>${f1(end * n)} V</b>`;
    const lt = body.querySelector("[data-role=later]");
    if (lt) lt.hidden = !later();
    const ok = $("#sheetOk");
    ok.hidden = !!blockers.length;
    ok.disabled = !canWrite() || (!later() && cur === pct && ch.gap_ok);
    ok.textContent = !canWrite() ? "View-only: pair to change" : later() ? `Set ${pct}% later…` : `Set ${pct}%${skipConfirm() ? "" : "…"}`;
    fires();
  };
  if (r) {
    r.oninput = upd;
    fires = wireWhen(form, upd);
    form.elements.when.forEach((x) => (x.onchange = upd));
  }
  body.oninput = null;
  body.onclick = async (e) => {
    const t = e.target.closest("[data-sheet]");
    if (!t) return;
    const a = t.dataset.sheet;
    if (a === "fixgap") { dlg.close(); writeCharge(b, { fix_gap: true }); return; }
    if (!r) return;
    if (a === "minus") r.value = Math.max(lim.pct_min, Number(r.value) - 1);
    if (a === "plus") r.value = Math.min(lim.pct_max, Number(r.value) + 1);
    if (a === "preset") r.value = t.dataset.p;
    upd();
  };
  $("#sheetOk").onclick = (e) => {
    e.preventDefault();
    dlg.close();
    if (later()) setTimer(b, { at: pickedAt(form), pct, charge: form.elements.startchg.checked ? "on" : null });
    else writeCharge(b, { pct });
  };
  upd();
  dlg.showModal();
}

// ---------------------------------------------------------------- storage mode

export async function setStorage(b, on) {
  if (!canWrite()) return toast("View-only on this device. Pair it to change storage mode.");
  try {
    const prop = await api("/api/storage/prepare", { host: b.host, mac: b.mac, on });
    if (prop.noop) { await hooks.refresh(); await hooks.reloadDetail(b.host); return toast(on ? `Storage mode on (${boardName(b)} already had those settings).` : `Storage mode off (${boardName(b)}).`); }
    if (await finish(b, prop)) toast(on ? `Storage mode on for ${boardName(b)}.` : `Storage mode off for ${boardName(b)}: settings from before are back.`);
  } catch (e) { fail(e); }
}

export async function saveStorageCfg(b, cfg) {
  try { await api("/api/storage/settings", { host: b.host, cfg }); await hooks.refresh(); await hooks.reloadDetail(b.host); toast("Storage settings saved."); return true; }
  catch (e) { fail(e); return false; }
}

// ---------------------------------------------------------------- all boards at once

export async function bulkAction(action) {
  if (!canWrite()) return toast("View-only on this device. Pair it to change boards.");
  let p;
  try { p = await api("/api/bulk/prepare", { action }); } catch (e) { return fail(e); }
  const rows = p.rows.map((r) => `<tr><td><b>${esc(r.name)}</b></td><td>${r.will ? esc(r.will) : `<span class="muted">skipped: ${esc(r.blocked)}</span>`}</td></tr>`).join("");
  if (!p.count) return showMsg(p.label + " (all boards)", `<p>No board can take this right now.</p><table class="diff">${rows}</table>`);
  const flash = /balancing|storage/.test(action);
  const ok = skipConfirm() || await approve(`${p.label}: all boards`, `<table class="diff"><tr><th>Board</th><th>What happens</th></tr>${rows}</table>
    <p class="muted small">${flash ? "Each board gets its own checked write: fresh read, backup, write, read-back, logged." : "A live command: it isn't saved and resets when a BMS restarts or wakes."} A board that fails doesn't stop the others.</p>`,
    `Apply this to the ${p.count} board${p.count > 1 ? "s" : ""} listed.`, p.label);
  if (!ok) return;
  if (flash) showMsg(p.label + "…", `<p>Working through ${p.count} board${p.count > 1 ? "s" : ""}. Each write takes a few seconds.</p>`);
  try {
    const res = await api("/api/bulk/commit", { id: p.id, skipped_confirm: skipConfirm() });
    await hooks.refresh();
    const bad = res.results.filter((r) => !r.ok && !r.skipped);
    const list = res.results.map((r) => `<li><b>${esc(r.name)}</b>: ${r.ok ? "" : r.skipped ? '<span class="muted">skipped, </span>' : '<span class="bad-t">failed, </span>'}${esc(r.result)}</li>`).join("");
    if (!flash && !bad.length) { if ($("#msgDlg").open) $("#msgDlg").close(); return toast(`${p.label}: done on every board.`); }
    showMsg(bad.length ? `${p.label}: ${bad.length} failed` : `${p.label}: done`, `<ul>${list}</ul>`);
  } catch (e) { fail(e); }
}

// First-time board setup (shown once per new board; reopened from Board → Edit board setup).
// Saves profile data in VBMS Manager only. Nothing here writes to the BMS, except the optional
// add-on step, which goes through the usual prepare -> confirm -> commit write flow.
// Step 0 is the safety warning: until it's accepted, the server refuses every write and command to the board.

import { AWAKE_FIX, AWAKE_WARN, $, api, cellsCount, esc, f1, f3, fail, hooks, S, stamp12 } from "./core.js";
import { curveChart } from "./viz.js";
import { addonPanel, installAddon, openChargeSheet } from "./writes.js";

const STEPS = ["Safety", "Name", "Cells", "SOC curve", "Script", "Add-ons", "Done"];
const DONE = STEPS.length - 1;
let W = null; // {host, step, opened}
export const wizardSeen = new Set(); // macs auto-opened this page session
export const wizardOpen = () => W !== null;
// Each dlg.close() fires exactly one "close" event, a moment later. Closes we make ourselves (Finish,
// Set a charge limit) are counted here so their event can't end a wizard opened in the meantime for the
// next new board.
let ownCloses = 0;
function closeOwn(dlg) { if (dlg.open) { ownCloses++; dlg.close(); } }

const board = () => S.state.boards.find((x) => x.host === W.host);

export async function openWizard(host, auto = false) {
  const b = S.state.boards.find((x) => x.host === host);
  if (!b || !b.mac) return;
  if (auto) wizardSeen.add(b.mac);
  W = { host, step: 0 };
  const dlg = $("#wizDlg");
  await draw();
  if (!dlg.open) dlg.showModal();
}

async function detail() {
  await hooks.reloadDetail(W.host);
  return S.detail.get(W.host);
}

// done: Finish / Set a charge limit (marks setup done). Closing early (✕, Esc) doesn't: the board
// shows a "Finish board setup" banner instead, and writes stay locked until the steps are done.
async function finish(openLimit, done = true) {
  if (!W) return;
  const b = board();
  const host = W.host;
  W = null;
  if (done && b && b.mac && !b.setup_done) {
    try { await api("/api/board/profile", { host, mac: b.mac, setup_done: true }); } catch (e) { fail(e); }
  }
  await hooks.refresh();
  if (openLimit) {
    const nb = S.state.boards.find((x) => x.host === host);
    if (nb && nb.charge) openChargeSheet(nb);
  }
}

async function draw() {
  const b = board(), d = await detail();
  if (!b || !d) return;
  const body = $("#wizBody");
  const n = cellsCount(b);
  const cfg = Object.fromEntries((d.config || []).map((f) => [f.name, f.value]));
  $("#wizSteps").innerHTML = STEPS.map((s, i) => `<li class="${i === W.step ? "on" : i < W.step ? "done" : ""}">${s}</li>`).join("");
  let h = "", next = "Next", canNext = true;
  if (W.step === 0) {
    h = riskHtml(b);
    canNext = b.risk_ok;
  } else if (W.step === 1) {
    h = `<h3>${b.setup_done ? "Board setup" : "New board found"}</h3>
      <dl class="kv"><dt>Hardware</dt><dd>${esc(b.fw.hw_name)} · FW ${esc(b.fw.version)}</dd><dt>MAC</dt><dd class="mono">${esc(b.mac)}</dd><dt>IP</dt><dd class="mono">${esc(b.host)}</dd>
      <dt>Cells</dt><dd>${esc(cfg.cells_ic1)} + ${esc(cfg.cells_ic2)} = <b>${b.cells_configured}s</b> configured${b.values ? `, ${b.values.cells.length} reporting` : ""}</dd>
      <dt>Temp sensors</dt><dd>${esc(cfg.temp_num)}</dd></dl>
      ${b.values && b.cells_configured !== b.values.cells.length ? `<div class="banner bad">The config and the live data disagree on the number of cells. Check the board in VESC Tool before going on.</div>` : ""}
      <label class="field">Give it a name<input id="wzName" maxlength="40" placeholder="e.g. Street board" value="${esc(b.nickname || "")}"></label>
      <p class="muted small">Cell counts come from the BMS config and can only be changed in VESC Tool.</p>`;
  } else if (W.step === 2) {
    const cur = b.cell && b.cell.id;
    h = `<h3>Which cells are in this ${n}s pack?</h3>
      ${[...new Set(S.state.cells.cells.map((c) => c.group))].map((g) => `${g ? `<h4 class="pickgroup">${esc(g)}</h4>` : ""}
      <div class="cellpick">${S.state.cells.cells.filter((c) => c.group === g).map((c) => `<label class="pick"><input type="radio" name="wzCell" value="${esc(c.id)}" ${c.id === cur ? "checked" : ""}>
        <span><b>${esc(c.name)}</b><small>${c.format ? esc(c.format) + " · " : ""}${c.capacity_mah ? c.capacity_mah + " mAh · " : ""}100% = ${c.max_end.toFixed(2)} V/cell = ${f1(c.max_end * n)} V pack</small>${c.note ? `<small>${esc(c.note)}</small>` : ""}</span></label>`).join("")}</div>`).join("")}
      <p class="muted small">The cell type sets the 100% voltage and the SOC curve used by the charge limit. Each listed cell has its own measured curve. Stock packs use the cells Future Motion is reported to have fitted; if you can read the print on your cells, pick the cell instead. LiFePO4 packs (Onewheel V1/Plus) aren't supported.</p>`;
    canNext = !!cur;
  } else if (W.step === 3) {
    const c = b.cell;
    if (!c) { h = `<p>Choose a cell type first.</p>`; canNext = false; }
    else {
      h = `<h3>SOC curve for ${esc(c.name)}</h3>
        <div class="${c.curve_info.verified ? "muted" : "banner"} small"><b>${esc(c.curve_info.name)}</b>${c.curve_info.verified ? "" : " · not measured data for this cell"}<br>${esc(c.curve_info.source)}</div>
        ${curveChart(c.curve, c.max_end)}
        <p class="small">This curve turns a percentage into a Charge End voltage, and a resting voltage into a charge estimate for the History tab. 100% is always end ${c.max_end.toFixed(3)} V / start ${(c.max_end - 0.1).toFixed(3)} V. The 0.100 V gap, that ceiling, and the Charge Minimum check apply whatever the curve says.</p>
        <p class="muted small">The BMS stops at Charge End while current is still flowing, when cells read a little higher than at rest, so the pack settles a few percent below the chosen limit. Cells of the same model also vary, and age shifts the curve.</p>
        <details><summary>Edit the voltages</summary><div class="curvewrap"><table class="curve"><tr><th>SOC %</th>${c.curve.map(([p]) => `<th>${p}</th>`).join("")}</tr>
          <tr><td>Volts</td>${c.curve.map(([p, v]) => `<td><input data-curve="${p}" type="number" step="0.001" value="${Number(v).toFixed(3)}"></td>`).join("")}</tr></table></div>
          <div class="row"><button type="button" class="small ghost" data-wz="savecurve">Save as custom curve</button>${c.curve_info.custom ? `<button type="button" class="small ghost" data-wz="resetcurve">Back to the default curve</button>` : ""}</div></details>
        <div class="row">${c.accepted ? `<span class="chip ok">✓ Curve accepted for this board</span>` : `<button type="button" data-wz="accept">I've reviewed this curve, use it</button>`}</div>`;
      canNext = c.accepted;
    }
  } else if (W.step === 4) {
    const sc = d.script;
    if (!sc) h = `<p class="muted">The script hasn't been read yet.</p>`;
    else if (sc.verified) h = `<h3>Running script</h3><p><span class="chip ok">✓ Charge logic matches the known vbms32 pattern</span></p><p class="small">Charge End stops charging; charging restarts once the highest cell drops below Charge Start.${sc.wake_path_uses_end ? " Note: after waking from sleep on the charger, this script charges up to Charge End without the 0.1 V hysteresis." : ""}</p>`;
    else {
      h = `<h3>Running script</h3><p><span class="chip ${sc.acknowledged ? "warn" : "bad"}">${sc.acknowledged ? "Unrecognized, acknowledged" : "Unrecognized charge logic"}</span></p>
        ${sc.problems.length ? `<div class="banner bad">${sc.problems.map(esc).join("<br>")}</div>` : ""}
        <details open><summary>Charge code found</summary>${(sc.excerpts || []).map((x) => `<div class="muted small">line ${x.line}</div><pre class="code">${esc(x.text)}</pre>`).join("")}
        ${sc.unexpected.length ? `<b>Unreviewed references:</b>${sc.unexpected.map((x) => `<pre class="code">${x.line}: ${esc(x.text)}</pre>`).join("")}` : ""}</details>
        ${sc.acknowledged ? "" : `<label class="check"><input type="checkbox" id="wzAck"> I've read this script's charge logic and confirm Charge Start / End mean what this tool expects.</label>
        <div class="row"><button type="button" class="danger" data-wz="ack" data-sha="${esc(sc.sha256)}">Acknowledge script</button></div>
        <p class="muted small">Until you do, every BMS write from this tool stays locked for this board.</p>`}`;
    }
  } else if (W.step === 5) {
    h = `<h3>Optional add-on: charger latch</h3>${addonPanel(b, "data-wz")}
      <p class="muted small">This step is optional: press Next to skip it. It's also on the Board tab later.</p>`;
  } else {
    const ok = (x) => (x ? "✓" : "✕");
    const sc = d.script;
    h = `<h3>All set${b.nickname ? ", " + esc(b.nickname) : ""}</h3>
      <ul class="checks"><li class="${b.nickname ? "ok" : "no"}">${ok(b.nickname)} Name</li><li class="${b.cell ? "ok" : "no"}">${ok(b.cell)} Cell type${b.cell ? ": " + esc(b.cell.name) : ""}</li>
      <li class="${b.cell && b.cell.accepted ? "ok" : "no"}">${ok(b.cell && b.cell.accepted)} SOC curve accepted</li><li class="${sc && (sc.verified || sc.acknowledged) ? "ok" : "no"}">${ok(sc && (sc.verified || sc.acknowledged))} Script checked</li>
      ${(b.addons || []).map((a) => `<li class="${a.state === "installed" ? "ok" : ""}">${a.state === "installed" ? "✓" : "–"} ${esc(a.name)} ${a.state === "installed" ? "installed" : "not installed (optional)"}</li>`).join("")}</ul>
      <p class="small">Current charge limit: end <b>${f3(b.charge.vc_charge_end)} V</b> / start <b>${f3(b.charge.vc_charge_start)} V</b>${b.cell && b.cell.current_pct != null ? ` (≈ ${Math.round(b.cell.current_pct)}%)` : ""}.</p>
      <p class="muted small">You can reopen this from the Board tab → Edit board setup.</p>`;
    next = "Finish";
  }
  body.innerHTML = h;
  $("#wizBack").hidden = W.step === 0;
  if (W.step === 0 && !b.risk_ok) { next = "Accept and continue"; canNext = !!($("#wzRisk") && $("#wzRisk").checked); }
  $("#wizNext").textContent = next;
  $("#wizNext").disabled = !canNext;
  $("#wizLimit").hidden = W.step !== DONE || !(b.cell && b.cell.accepted);
  $("#wizSkip").hidden = W.step === 0 || W.step >= DONE || canNext;
}

// The warning has to stay accurate: if it changes in substance, bump RISK_VERSION in safe_write.py
// so every board asks again.
function riskHtml(b) {
  return `<h3>Read this before you use VBMS Manager with this board</h3>
    <div class="banner bad risk">
      <p><b>This software was written by an AI (Claude, made by Anthropic). No human has reviewed it.</b></p>
      <p>Trusting unverified, AI-generated software to manage a lithium battery that's connected to a charger in your home is a bad idea.</p>
      <ul>
        <li>You use it <b>entirely at your own risk</b>. It comes with no warranty of any kind.</li>
        <li>It may damage your BMS, battery, charger or board. Worse, it could cause a <b>lithium battery fire</b>.</li>
        <li>It has not been tested in every scenario or on every hardware, firmware and script version. It was only tried on two VBMS32 boards (18s and 32s) running vesc_express FW 7.00. It may malfunction.</li>
        <li>It doesn't replace the BMS's own protections, a correct charger, or charging the way you would without it. Keep checking your boards with VESC Tool.</li>
      </ul>
    </div>

    <details class="riskmore"><summary>How it works, what it can change and how it checks itself</summary>
    <h4>How it works</h4>
    <p class="small">It talks to the BMS over WiFi using VESC's own protocol (TCP port 65102), or over Bluetooth with the same protocol if you switch it in the Mac app. Most of the time it only <b>reads</b>: cell voltages, temperatures, the config, and the Lisp script running on the BMS.</p>

    <h4>What it can change on the BMS</h4>
    <ul class="small">
      <li><b>Charge limit:</b> only the two 4-byte values <span class="mono">vc_charge_start</span> and <span class="mono">vc_charge_end</span> in the BMS's custom config (stored in flash, the same config VESC Tool's BMS page writes). Every other byte is sent back exactly as the board sent it.</li>
      <li><b>Other settings you edit</b> (balancing, charging, sleep/SOC, protection, WiFi/Bluetooth/CAN): only the bytes of the settings you changed, from a fixed list of allowed settings. Text settings such as the WiFi name change length, so for those the config is re-encoded, and every other setting must still decode identically.</li>
      <li><b>Live controls</b> (pause/allow charging, force balance): commands that only change RAM. The BMS forgets them when it restarts or wakes.</li>
      <li><b>Optional charger-latch add-on</b>, only if you install it (board setup or the Board tab, with the board's name typed to confirm): replaces the BMS's Lisp script with the stock vbms32 script plus a charger latch. It's only offered when the board runs exactly that stock script. VBMS Manager can't remove it: to undo it, re-install the BMS package from VESC Tool. The installer has only been tested against a simulated board, not a real one.</li>
      <li>Nothing else. It never changes firmware, and sends no other commands that write.</li>
    </ul>

    <h4>How it checks itself</h4>
    <ul class="small">
      <li>It reads the config layout from the board itself. It decodes the config and encodes it again, and the result must match the board's bytes exactly. If it doesn't, writes stay off for this board.</li>
      <li>Before anything is sent, it compares the new config with the old one byte by byte. Only the intended bytes may differ, and every other setting must decode to the same value.</li>
      <li>Hard limits, checked separately from the SOC curve: Charge Start 0.050–0.200 V below End (0.100 V unless you unlink it on the graph), End no higher than the cell's maximum (4.21 V, never above 4.25 V) and no lower than 3.60 V, plus the tool's own bounds on every other editable setting.</li>
      <li>It reads the running script and checks how it uses Charge Start/End. An unrecognized script keeps writes locked until you review it.</li>
      <li>When you confirm, it reads the board again. It checks that it's the same board (MAC address) and that the config hasn't changed. Then it saves a full backup, writes, reads the config back, and requires an exact match. Every write is logged.</li>
      <li>The add-on only installs with the charger unplugged, because the script is stopped while it's written. A copy of the old script is saved first. The new one must read back exactly before it's started; if anything fails, the old one is written back.</li>
    </ul>
    </details>

    <h4>Your approval</h4>
    <ul class="small">
      <li><b>Every write needs your approval.</b> Before anything is sent, you see the exact old and new values, tick a box and click <i>Write to BMS</i>. Risky changes also need you to type the board's name. An approval works once and expires after 2 minutes. Live controls ask you to confirm too.</li>
      <li><b>You can turn these confirmations off</b> (Preferences, only from this computer). Then one tap writes or sends straight away, from any paired device. Danger changes and restores still ask.</li>
      <li><b>Schedules and timers are approved in advance.</b> When you approve one, you approve all of its writes ahead of time. From then on, VBMS Manager writes Charge Start/End, pauses or allows charging, and turns balancing off or on (by writing Balance Max Current = 0 A, then your value back) <b>automatically, without asking you again</b>, until you edit it or turn it off. Scheduled writes still get the same checks, backup, read-back and log.</li>
      <li><b>API requests are approved in advance too, if you turn them on</b> (Preferences, only from this computer, off by default). Then anything holding one of your API keys can change charge limits, pause charging, turn balancing and storage mode on or off and, with the settings scope, edit settings, <b>without asking you</b>, from wherever your server can be reached. API writes get the same checks, backup, read-back and log. Restores and add-ons are never available over the API.</li>
      <li><b>Storage mode</b> (if you use it) writes Charge Start/End, Balance Min and Balance Max Current in one write, and writes your previous values back when you turn it off.</li>
    </ul>

    <h4>What it can't protect you from</h4>
    <ul class="small">
      <li><b>It keeps your BMS awake and slowly drains the pack.</b> ${esc(AWAKE_WARN)} ${esc(AWAKE_FIX)}</li>
      <li><b>It disconnects by itself sometimes, on purpose.</b> A board whose SOC stays under 20% for 2 hours (Preferences → Safety), or a board in storage mode with no charger, is disconnected so the BMS can sleep. It reconnects briefly every few hours to check. While disconnected, nothing here watches that board, and safety notices use its last reading.</li>
      <li>Schedules only run while VBMS Manager is running and the computer is awake. If it stops, the board keeps whatever limit it last had, and a paused board can start charging again once the BMS restarts or wakes.</li>
      <li>After the BMS wakes up, a few seconds of charging can happen before the tool reconnects and pauses it again.</li>
      <li>The charge percentage comes from a resting-voltage curve for the cell type you pick, measured on new cells, so it's approximate: cells vary and age, and the BMS stops on the voltage while charging, so the pack settles a little below the chosen percentage. "Other NMC/NCA" uses a typical curve, not your cells' own.</li>
      <li>Anyone you pair from another device can make changes to this board too.</li>
    </ul>

    ${b.risk_ok
      ? `<p><span class="chip ok">✓ Accepted for this board${b.risk_ack_time ? " on " + esc(stamp12(b.risk_ack_time)) : ""}</span></p>`
      : `<label class="check"><input type="checkbox" id="wzRisk"> <span>I've read this. I understand that this software was written by AI, hasn't been reviewed by a human, and may malfunction, damage my hardware or cause a fire. I accept the risk of using it with this board.</span></label>
         <p class="muted small">Until you accept, VBMS Manager only reads from this board. Every write and command stays locked.</p>`}`;
}

async function act(fn) {
  try { await fn(); await hooks.refresh(); await draw(); } catch (e) { fail(e); }
}

export function initWizard() {
  const dlg = $("#wizDlg");
  dlg.addEventListener("close", () => {
    if (ownCloses > 0) { ownCloses--; return; }
    finish(false, false);
  });
  $("#wizBody").addEventListener("change", (e) => {
    if (e.target.id === "wzRisk") { $("#wizNext").disabled = !e.target.checked; return; }
    if (e.target.name === "wzCell") {
      const b = board();
      act(() => api("/api/board/profile", { host: b.host, mac: b.mac, cell_id: e.target.value, custom_curve: null }));
    }
  });
  $("#wizBody").addEventListener("click", (e) => {
    const t = e.target.closest("[data-wz]");
    if (!t) return;
    const b = board(), body = { host: b.host, mac: b.mac };
    const a = t.dataset.wz;
    if (a === "accept") act(() => api("/api/board/accept_curve", body));
    if (a === "savecurve") act(() => api("/api/board/profile", { ...body, custom_curve: [...$("#wizBody").querySelectorAll("[data-curve]")].map((i) => [Number(i.dataset.curve), Number(i.value)]) }));
    if (a === "resetcurve") act(() => api("/api/board/profile", { ...body, custom_curve: null }));
    if (a === "addon") act(() => installAddon(b, t.dataset.id));
    if (a === "ack") {
      if (!$("#wzAck").checked) return window.alert("Tick the box first to confirm you've reviewed the charge logic.");
      act(() => api("/api/board/ack_script", { ...body, sha256: t.dataset.sha }));
    }
  });
  $("#wizBack").onclick = () => { W.step = Math.max(0, W.step - 1); draw(); };
  $("#wizSkip").onclick = () => { W.step += 1; draw(); };
  $("#wizNext").onclick = async () => {
    const b = board();
    if (W.step === 0 && !b.risk_ok) {
      if (!$("#wzRisk").checked) return;
      try { await api("/api/board/ack_risk", { host: b.host, mac: b.mac, accept: true }); await hooks.refresh(); } catch (e) { return fail(e); }
    }
    if (W.step === 1) {
      const nm = $("#wzName").value.trim();
      if (nm !== (b.nickname || "")) { try { await api("/api/board/profile", { host: b.host, mac: b.mac, nickname: nm }); await hooks.refresh(); } catch (e) { return fail(e); } }
    }
    if (W.step === DONE) { closeOwn(dlg); return finish(false); }
    W.step += 1;
    draw();
  };
  $("#wizLimit").onclick = () => { closeOwn(dlg); finish(true); };
}


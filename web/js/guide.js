// Setup guide (getting a VBMS onto WiFi) and the devices / pairing dialog.

import { $, api, esc, fail, hooks, S, stamp12 } from "./core.js";

// VESC Tool screenshots in web/img/guide/<key>.png. A figure stays hidden until its image loads, so a
// missing screenshot just leaves the text. Marks are numbered boxes [left, top, width, height, what to do, badge side] in percent
// of the image, matching the numbered steps next to it, so they can be re-aimed without a new image.
const VT = "";   // the VESC Tool version the screenshots were taken with, e.g. "VESC Tool 6.06"
const SHOTS = {
  "01-connect": { alt: "VESC Tool's Connection page: connect over USB or scan for Bluetooth", marks: [] },
  "02-hwname": { alt: "The connected device's hardware name reads VBMS32", marks: [] },
  "04-wifi": { alt: "VESC BMS page, WiFi tab", marks: [
    [20.0, 4.5, 3.6, 2.6, "Open the WiFi tab", "below"],
    [16.4, 10.0, 73.2, 2.5, "WiFi Mode: Station"],
    [16.4, 15.0, 73.2, 5.4, "Your network name and WiFi password"],
    [16.4, 30.4, 73.2, 2.5, "Enable Local TCP: True"],
    [48.1, 87.7, 15.8, 2.9, "Write"]] },
  "06-ble-name": { alt: "The Bluetooth / device name field", marks: [] },
  "08-block-sleep": { alt: "VESC BMS page, Sleep tab", marks: [
    [45.4, 4.5, 3.4, 2.6, "Open the Sleep tab", "below"],
    [16.4, 15.2, 73.2, 2.5, "Block Sleep Mode: True only while you set up, then False again"],
    [48.1, 87.7, 15.8, 2.9, "Write"]] },
};

// Screenshots that exist in web/img/guide. The others are slots for later: they're left out entirely,
// so the guide doesn't ask the server for files that aren't there. Add a key here with its .png.
const HAVE = new Set(["04-wifi", "08-block-sleep"]);

function shot(key) {
  const s = SHOTS[key];
  if (!HAVE.has(key)) return "";
  const alt = s.alt + (s.marks.length ? ": " + s.marks.map((m, i) => `${i + 1}. ${m[4]}`).join(", ") : "");
  return `<figure class="shot" hidden><button type="button" class="shot-img" data-zoom aria-label="Enlarge: ${esc(s.alt)}"><span class="shot-frame"><img src="img/guide/${key}.png" alt="${esc(alt)}">${s.marks.map((m, i) => `<span class="shot-mark${m[5] ? " " + m[5] : ""}" style="left:${m[0]}%;top:${m[1]}%;width:${m[2]}%;height:${m[3]}%"><b>${i + 1}</b></span>`).join("")}</span></button><figcaption>${esc(s.alt)} <span class="muted">· ${VT ? VT + " · " : ""}tap to enlarge</span>${s.marks.length ? `<ol class="shot-steps">${s.marks.map((m) => `<li>${esc(m[4])}</li>`).join("")}</ol>` : ""}</figcaption></figure>`;
}

const STEPS = [
  ["What you need", () => `
    <ul>
      <li>A <b>VBMS32</b> (or another VESC Express–based BMS) running the vbms32 Lisp package.</li>
      <li><b>VESC Tool</b> on a computer or phone, and either a <b>USB-C cable</b> to the BMS or <b>Bluetooth</b>.</li>
      <li>Your WiFi network name and password. The BMS's ESP32 only does <b>2.4 GHz</b> WiFi, so pick a 2.4 GHz network (most routers offer one).</li>
      <li>This computer on the same network as the board.</li>
    </ul>
    <p class="muted small">The BMS sleeps when it's idle and only talks to anything while awake. Putting it on the charger is the easiest way to keep it awake while you set it up.</p>`],
  ["Connect with VESC Tool", () => `
    <ol>
      <li>Wake the BMS: plug in the charger.</li>
      <li>In VESC Tool, connect over <b>USB</b> (cable into the BMS's USB-C port) or scan for <b>Bluetooth</b> devices and pick the BMS. Its Bluetooth name defaults to <b>VESC BMS</b>.</li>
      <li>Check you're talking to the BMS, not the motor controller: the hardware name should say <b>VBMS32</b>. If the controller and BMS share a CAN bus, use VESC Tool's CAN list to select the BMS.</li>
    </ol>${shot("01-connect")}${shot("02-hwname")}`],
  ["WiFi settings", () => `
    <p>Once connected, VESC Tool adds <b>VESC BMS</b> to the menu on the left, under VESC Packages. Open it, go to the <b>WiFi</b> tab and set:</p>
    <table class="diff"><tr><th>Setting</th><th>Value</th></tr>
      <tr><td>WiFi Mode</td><td><b>Station</b></td></tr>
      <tr><td>Station Mode SSID</td><td>your network's name</td></tr>
      <tr><td>Station Mode Key</td><td>your WiFi password</td></tr>
      <tr><td>Enable Local TCP</td><td><b>on</b> (this is what VBMS Manager connects to, port 65102)</td></tr>
      <tr><td>Name <span class="muted">(optional)</span></td><td>up to 8 characters, e.g. <b>Commuter</b>. Shows up in auto-discovery so boards are easy to tell apart.</td></tr>
    </table>${shot("04-wifi")}${shot("06-ble-name")}
    <p>Click <b>Write</b> at the bottom of the page. WiFi and Bluetooth settings apply after the BMS restarts: unplug the charger, wait for it to sleep, and plug it back in.</p>
    <div class="risk caution"><b>⚠ Block Sleep Mode</b> (Sleep tab) keeps the BMS awake, which can help during setup. Set it back to False and Write when you're done, or the BMS slowly drains the pack.</div>${shot("08-block-sleep")}`],
  ["Find its IP address", () => `
    <ul>
      <li>If your network passes broadcasts, the board appears here by itself under <b>Found on your network</b> within a few seconds of waking.</li>
      <li>Otherwise look in your router's list of connected devices for a new device (often named <i>espressif</i> or similar) while the board is on the charger.</li>
      <li>Reserve that address in your router (<b>DHCP reservation</b> / static lease) so it doesn't change. Otherwise the board may get a different IP next time and this tool won't find it.</li>
    </ul>`],
  ["Add it here", () => `
    <ol>
      <li><b>Disconnect VESC Tool</b> from the board. Over WiFi the BMS talks to one app at a time; while VESC Tool is connected this tool shows it as <i>Busy</i>.</li>
      <li>Click <b>+ Add board</b> at the top and enter the IP (or click <b>Add</b> on a found board).</li>
      <li>The first time VBMS Manager sees the board it walks you through <b>board setup</b>: name, cell type, SOC curve, a check of the charge logic in its script, and the optional charger-latch add-on.</li>
    </ol>
    <p class="muted small">Need VESC Tool again later? Use <b>Board → Free for VESC Tool</b>, or quit VBMS Manager.</p>`],
  ["Use it from your phone", () => {
    const a = S.state.access || {};
    const urls = a.urls && a.urls.length ? `<p>Open one of these on your phone:</p><ul>${a.urls.map((u) => `<li class="mono">${esc(u)}</li>`).join("")}</ul>` : "";
    return `<ol>
      <li>Start VBMS Manager with <span class="mono">python3 vbms_manager.py --lan</span> (it remembers this; <span class="mono">--local-only</span> turns it off).</li>
      <li>Open the address it prints on your phone or tablet. Other devices start <b>view-only</b>.</li>
      <li>To change settings from that device, tap <b>Pair</b> and enter the 6-digit code shown on this computer (under <b>Devices</b>). The code changes every time VBMS Manager starts; paired devices stay paired until you remove them.</li>
      <li>If the phone can't connect, your computer's firewall is probably blocking it. On a Mac, click <b>Allow</b> when asked about incoming connections for Python, or add Python under System Settings → Network → Firewall → Options.</li>
    </ol>${a.role === "local" ? (a.lan ? urls + `<p>Pairing code: <b class="paircode">${esc(a.pair_code)}</b></p>` : `<div class="banner">Network access is off. Restart with <span class="mono">--lan</span> to turn it on.</div>`) : ""}`;
  }],
  ["Troubleshooting", () => `
    <dl class="kv">
      <dt>Busy</dt><dd>Another app (usually VESC Tool) is connected to the board over WiFi. Disconnect it.</dd>
      <dt>Offline</dt><dd>The board is asleep (put it on the charger), its IP changed, or it's not on the network (check the SSID/key, and that the network is 2.4 GHz).</dd>
      <dt>Not found automatically</dt><dd>Some routers and mesh systems block broadcasts. Add the board by IP.</dd>
      <dt>Writes locked</dt><dd>Finish board setup (cell type, curve, script check), and clear any charge fault by unplugging the charger for 5 s.</dd>
    </dl>`],
];

let step = 0;

function draw() {
  $("#guideNav").innerHTML = STEPS.map(([t], i) => `<li><button type="button" class="${i === step ? "on" : ""}" data-gs="${i}"><span>${i + 1}</span>${esc(t)}</button></li>`).join("");
  $("#guideBody").innerHTML = `<h3>${step + 1}. ${esc(STEPS[step][0])}</h3>${STEPS[step][1]()}`;
  $("#guidePrev").disabled = step === 0;
  $("#guideNext").textContent = step === STEPS.length - 1 ? "Done" : "Next";
}

export function openGuide(at = 0) {
  step = at;
  draw();
  $("#guideDlg").showModal();
}

// A screenshot shows once its image has loaded; clicking it toggles a full-size view.
document.addEventListener("load", (e) => {
  if (e.target.matches && e.target.matches(".shot img")) {
    const f = e.target.closest(".shot");
    f.hidden = false;
    f.style.setProperty("--ar", e.target.naturalWidth / e.target.naturalHeight);   // sizes the full-window view
  }
}, true);

export function initGuide() {
  $("#guideBody").addEventListener("click", (e) => { const z = e.target.closest("[data-zoom]"); if (z) z.closest(".shot").classList.toggle("big"); });
  $("#guideNav").onclick = (e) => { const t = e.target.closest("[data-gs]"); if (t) { step = Number(t.dataset.gs); draw(); } };
  $("#guidePrev").onclick = () => { step = Math.max(0, step - 1); draw(); };
  $("#guideNext").onclick = () => { if (step === STEPS.length - 1) $("#guideDlg").close(); else { step += 1; draw(); } };
}

// ---------------------------------------------------------------- devices / pairing

function guessLabel() {
  const ua = navigator.userAgent;
  return /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android device" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows PC" : "Device";
}

export function drawDevices() {
  const a = S.state.access || {};
  const box = $("#devBody");
  if (a.role === "local") {
    box.innerHTML = a.lan ? `<p>Other devices on your network can open:</p><ul>${(a.urls || []).map((u) => `<li class="mono">${esc(u)}</li>`).join("") || "<li>(no network address found)</li>"}</ul>
      <p>They start view-only. To let one make changes, enter this code on it:</p><p class="paircode big">${esc(a.pair_code)}</p>
      <p class="muted small">The code changes every time VBMS Manager starts.</p>` :
      `<div class="banner">Only this computer can open VBMS Manager. Restart it with <span class="mono">python3 vbms_manager.py --lan</span> to use it from your phone.</div>`;
    box.innerHTML += `<h3>Paired devices</h3>${(a.devices || []).length ? `<ul class="devlist">${a.devices.map((d) => `<li><span><b>${esc(d.label)}</b> <span class="muted small">paired ${esc(stamp12(d.created))}</span></span><button type="button" class="small ghost" data-revoke="${esc(d.id)}">Remove</button></li>`).join("")}</ul>` : `<p class="muted">None yet.</p>`}`;
  } else if (a.role === "paired") {
    box.innerHTML = `<p><span class="chip ok">Paired</span> This device can make changes.</p><p class="muted small">To unpair it, remove it under Devices on the computer running VBMS Manager.</p>`;
  } else {
    box.innerHTML = `<p>This device is <b>view-only</b>. To make changes from it, enter the pairing code shown on the computer running VBMS Manager (under Devices, and in its terminal).</p>
      <div class="pairform"><label class="field">Pairing code<input id="pairCode" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="123456"></label>
      <label class="field">Name for this device<input id="pairLabel" maxlength="40" value="${esc(guessLabel())}"></label><button type="button" id="pairBtn">Pair</button></div>`;
    const pair = async () => {
      try {
        await api("/api/pair", { code: $("#pairCode").value, label: $("#pairLabel").value });
        await hooks.refresh();
        drawDevices();
      } catch (x) { fail(x); }
    };
    $("#pairBtn").onclick = pair;
    box.querySelectorAll("input").forEach((i) => (i.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); pair(); } }));
  }
}

export function initDevices() {
  $("#devBody").addEventListener("click", async (e) => {
    const t = e.target.closest("[data-revoke]");
    if (!t || !window.confirm("Remove this device? It goes back to view-only.")) return;
    try { await api("/api/devices/revoke", { id: t.dataset.revoke }); await hooks.refresh(); drawDevices(); } catch (x) { fail(x); }
  });
}

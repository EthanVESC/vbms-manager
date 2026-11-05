// The symbol key: what every compact symbol and colour means, and what tapping it does.
// Shown as a collapsed section at the bottom of Overview / All boards, never as a pop-up, so it can't
// cover the tooltips it explains. Open/closed is remembered per browser.

import { store } from "./core.js";
import { icon, socRing } from "./viz.js";

const ind = (name, text, cls = "") => `<span class="ind ${cls}" aria-hidden="true">${icon(name)}${text ? `<span>${text}</span>` : ""}</span>`;
const row = (sym, what, tap = "") => `<tr><td class="k-sym">${sym}</td><td>${what}${tap ? `<div class="k-tap">Tap: ${tap}</div>` : ""}</td></tr>`;

function body() {
  return `<table class="keytab">
    <tr><th colspan="2">State of charge ring</th></tr>
    ${row(`<span class="k-ring">${socRing(0.62, { limitPct: 80, act: false })}</span>`,
      "The coloured arc is state of charge from the BMS: green at 50% and up, amber 20–50%, red below 20%. It pulses while charging. The faint arc is the room left up to the charge limit.", "every cell's voltage")}
    ${row(`<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v20" class="k-tick"/></svg>`,
      "The tick on the ring is the charge limit (Charge End, as a percentage of your cell curve).", "change the charge limit")}
    <tr><th colspan="2">Charging</th></tr>
    ${row(ind("bolt", "2.1<small>A</small>", "on"), "Charging, at this current.", "stop charging (live command, right away)")}
    ${row(ind("boltOff", "paused", "warn"), "You paused charging.", "allow charging again (asks first)")}
    ${row(ind("boltClock", "sched", "warn"), "A no-charging window of the schedule paused it.", "charge anyway until the window ends (asks first)")}
    ${row(ind("bolt", "held"), "Charger connected, but the highest cell is at or above Charge Start, so the BMS waits until it drops below.")}
    ${row(ind("bolt", "idle"), "Charger connected and the BMS isn't charging for another reason (temperature, fault).")}
    ${row(ind("bolt", "", "off"), "No charger connected.")}
    <tr><th colspan="2">Balancing</th></tr>
    ${row(ind("scale", "auto"), "Not balancing right now. The vbms32 script balances automatically when its conditions are met.", "force balancing on")}
    ${row(ind("scale", "3", "bal"), "Automatic balancing: this many cells are bleeding.", "force balancing on")}
    ${row(ind("scale", "forced", "on"), "Force Balance is on (live command; resets when the BMS restarts or sleeps).", "back to automatic")}
    <tr><th colspan="2">Cells</th></tr>
    ${row(ind("delta", "8.0<small>mV</small>", "ok"), "Cell spread: highest minus lowest cell. Green is fine, amber is above Balance Start, red is above your imbalance alert.", "every cell's voltage")}
    ${row(`<span class="k-sw"><i class="sw hisw"></i>▲</span>`, "The highest cell.")}
    ${row(`<span class="k-sw"><i class="sw losw"></i>▼</span>`, "The lowest cell.")}
    ${row(`<span class="k-sw"><i class="sw cellsw"></i></span>`, "A cell, on the fixed 3.0–4.2 V scale. The row under it shows each cell's mV above the lowest (0 to Δ), which is where imbalance shows up.")}
    ${row(`<span class="k-sw"><i class="bal-dot"></i></span>`, "This cell is bleeding (balancing).")}
    ${row(`<span class="k-sw"><i class="sw warnsw"></i><i class="sw badsw"></i></span>`, "Amber: outlier, more than your alert threshold from the median. Red: out of range (above 4.2 V, below Empty, or above Charge End while charging).")}
    <tr><th colspan="2">Temperatures</th></tr>
    ${row(ind("therm", "24.1°"), "Average of the cell temperature sensors. Amber within 5 °C of the charge limit, red at or above it.", "each cell sensor")}
    ${row(ind("flame", "31.0°"), "The hottest sensor on the board right now (cells, MOSFET, IC or ambient). Hover or tap to see which.", "each cell sensor")}
  </table>
  <p class="muted small">Hover a symbol (or touch it) to see its value and what a tap does. Live commands (stop/allow charging, force balance) aren't saved to the BMS and reset when it restarts or sleeps.</p>`;
}

export function keyPanel() {
  return `<details class="panel keypanel" data-keypanel${store("vbms_key_open") ? " open" : ""}>
    <summary>What the symbols mean</summary>${body()}</details>`;
}

// toggle doesn't bubble, so listen in the capture phase.
document.addEventListener("toggle", (e) => {
  if (e.target.matches && e.target.matches("[data-keypanel]")) store("vbms_key_open", e.target.open);
}, true);

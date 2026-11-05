// Drag-to-edit thresholds on the graphs (charge lines, balance and temperature limits).
//
// A container marks its value axis:  data-dragbox data-axis="x"|"y" data-v0 data-v1
//   (HTML: v0 at the left/bottom edge, v1 at the right/top edge; SVG: also data-vb (viewBox width)
//    and data-p0/data-p1, the viewBox x of v0/v1).
// A handle inside it:                data-drag="<field>"
// A label anyone can click to type:  data-edit="<field>"
// Moves only change a per-board draft ("pending"); the apply bar under the graph writes it
// through the normal confirm flow. Charge End/Start move together (0.100 V) unless unlinked.

import { canWrite, clamp, esc, f3, hooks, placeTip, S, skipConfirm, toast } from "./core.js";
import { writeCharge, writeFields } from "./writes.js";

const GAP = 0.1, GAP_MIN = 0.05, GAP_MAX = 0.2;
// label, unit, display scale (balance thresholds are stored in V, shown in mV), step, fallback bounds
export const FIELDS = {
  vc_charge_end: { label: "Charge End", unit: "V", step: 0.005, dec: 3, min: 3.6, max: 4.25 },
  vc_charge_start: { label: "Charge Start", unit: "V", step: 0.005, dec: 3, min: 3.4, max: 4.2 },
  vc_balance_min: { label: "Balance Minimum", unit: "V", step: 0.01, dec: 3, min: 3.0, max: 4.2 },
  vc_balance_start: { label: "Balance Start", unit: "mV", scale: 1000, step: 1, dec: 0, min: 3, max: 100 },
  vc_balance_end: { label: "Balance End", unit: "mV", scale: 1000, step: 1, dec: 0, min: 1, max: 99 },
  t_charge_min: { label: "Charge Min Temp", unit: "°C", step: 1, dec: 1, min: -20, max: 20 },
  t_charge_max: { label: "Charge Max Temp", unit: "°C", step: 1, dec: 1, min: 0, max: 60 },
  t_bal_max_cell: { label: "Balance Max Cell Temp", unit: "°C", step: 1, dec: 1, min: 0, max: 60 },
  t_bal_max_ic: { label: "Balance Max IC Temp", unit: "°C", step: 1, dec: 1, min: 0, max: 100 },
};
const CHARGE = ["vc_charge_end", "vc_charge_start"];

const drafts = new Map();  // host -> {field: value (stored units)}
const unlinked = new Set(); // hosts whose charge lines move independently

const scale = (f) => FIELDS[f].scale || 1;
export const linked = (host) => !unlinked.has(host);
export const pending = (host) => drafts.get(host) || {};
const actual = (b, f) => (b.charge && b.charge[f] != null ? b.charge[f] : b.limits_cfg ? b.limits_cfg[f] : null);
// Value to draw: the draft if there is one, else the board's.
export const pv = (b, f) => (f in pending(b.host) ? pending(b.host)[f] : actual(b, f));
export const isPending = (b, f) => f in pending(b.host);
export const editable = (b) => canWrite() && !!b.charge;

// The board's own bounds for a field (from the server's editable description), in stored units.
function bounds(b, f) {
  const sp = FIELDS[f];
  let lo = sp.min / scale(f), hi = sp.max / scale(f);
  const d = S.detail.get(b.host);
  const fd = d && d.editable && d.editable.groups.flatMap((g) => g.fields).find((x) => x.name === f);
  if (fd && fd.min != null) { lo = fd.min; hi = fd.max; }
  if (f === "vc_charge_end" && b.cell) hi = Math.min(hi, b.cell.max_end);
  return [lo, hi];
}

const round = (v, f) => { const st = FIELDS[f].step / scale(f); return Math.round(Math.round(v / st) * st * 1e6) / 1e6; };

// Set a field in the draft, keeping the charge pair valid (linked: start = end - 0.100).
export function setDraft(b, f, v, { snap = true } = {}) {
  const d = { ...pending(b.host) };
  const [lo, hi] = bounds(b, f);
  v = clamp(snap ? round(v, f) : v, lo, hi);
  if (CHARGE.includes(f)) {
    let end = f === "vc_charge_end" ? v : pv(b, "vc_charge_end"), start = f === "vc_charge_start" ? v : pv(b, "vc_charge_start");
    if (linked(b.host)) {
      if (f === "vc_charge_end") start = end - GAP;
      else { end = clamp(start + GAP, ...bounds(b, "vc_charge_end")); start = end - GAP; }
    } else if (f === "vc_charge_end") end = clamp(end, start + GAP_MIN, start + GAP_MAX);
    else start = clamp(start, end - GAP_MAX, end - GAP_MIN);
    d.vc_charge_end = Math.round(end * 1000) / 1000; d.vc_charge_start = Math.round(start * 1000) / 1000;
    if (Math.abs(d.vc_charge_end - actual(b, "vc_charge_end")) < 5e-4 && Math.abs(d.vc_charge_start - actual(b, "vc_charge_start")) < 5e-4) { delete d.vc_charge_end; delete d.vc_charge_start; }
  } else {
    if (f === "vc_balance_end") v = Math.min(v, pv(b, "vc_balance_start") - 0.001);
    if (f === "vc_balance_start") v = Math.max(v, pv(b, "vc_balance_end") + 0.001);
    if (f === "t_charge_min") v = Math.min(v, pv(b, "t_charge_max") - 1);
    if (f === "t_charge_max") v = Math.max(v, pv(b, "t_charge_min") + 1);
    if (Math.abs(v - actual(b, f)) < 1e-6) delete d[f]; else d[f] = v;
  }
  if (Object.keys(d).length) drafts.set(b.host, d); else drafts.delete(b.host);
}

export function reset(host, fields) {
  const d = { ...pending(host) };
  for (const f of fields) delete d[f];
  if (Object.keys(d).length) drafts.set(host, d); else drafts.delete(host);
}

// Number with unit, in display units.
export const show = (f, v) => (v == null ? "–" : `${(v * scale(f)).toFixed(FIELDS[f].dec)} ${FIELDS[f].unit}`);

// ---------------------------------------------------------------- the apply bar

// fields: the ones this graph edits. Shown only when one of them has a draft.
export function applyBar(b, fields) {
  const d = pending(b.host), mine = fields.filter((f) => f in d);
  if (!mine.length || !canWrite()) return "";
  const isCharge = mine.some((f) => CHARGE.includes(f));
  const shown = isCharge ? [...CHARGE, ...mine.filter((f) => !CHARGE.includes(f))] : mine;
  const inp = (f) => `<label class="ab-f">${FIELDS[f].label}<span class="num"><input type="number" data-abf="${f}" value="${(pv(b, f) * scale(f)).toFixed(FIELDS[f].dec)}" step="${FIELDS[f].dec ? Math.pow(10, -FIELDS[f].dec) : 1}"><span class="unit">${FIELDS[f].unit}</span></span></label>`;
  const gap = isCharge ? pv(b, "vc_charge_end") - pv(b, "vc_charge_start") : null;
  const pct = isCharge && b.cell ? pctFor(pv(b, "vc_charge_end"), b.cell) : null;
  return `<div class="applybar" data-ab="${esc(fields.join(","))}">
    ${shown.map(inp).join("")}
    ${isCharge ? `<span class="ab-gap${Math.abs(gap - GAP) > 5e-4 ? " custom" : ""}">gap ${f3(gap)} V${pct != null ? ` · ≈ ${pct}%` : ""}</span>` : ""}
    <span class="ab-btns"><button type="button" class="ghost small" data-abact="reset">Reset</button><button type="button" class="small" data-abact="write">Write${skipConfirm() ? "" : "…"}</button></span>
    ${isCharge && !linked(b.host) ? `<p class="risk caution small"><b>⚠ Custom gap.</b> Charge Start is normally exactly 0.100 V below End (the hysteresis that stops the charger clicking on and off). You can set 0.050–0.200 V. A small gap restarts charging sooner after it stops.</p>` : ""}</div>`;
}

function pctFor(end, cell) {
  if (end >= cell.max_end - 5e-4) return 100;
  const pts = cell.curve.map(([p, v]) => [Number(p), Number(v)]).sort((a, c) => a[0] - c[0]);
  for (let i = 0; i < pts.length - 1; i++) {
    const [p0, v0] = pts[i], [p1, v1] = pts[i + 1];
    if (end >= v0 && end <= v1) return Math.round(p0 + (p1 - p0) * (end - v0) / (v1 - v0));
  }
  return end < pts[0][1] ? "<" + pts[0][0] : "99";
}

// Lock between the charge labels: linked (0.100 V) or not.
export function lockBtn(b) {
  if (!editable(b)) return "";
  const on = linked(b.host);
  return `<button type="button" class="lockbtn${on ? "" : " open"}" data-lock="${esc(b.host)}" data-tip="${on ? "Start follows End at 0.100 V · click to unlink" : "Start and End move separately (0.050–0.200 V) · click to link at 0.100 V"}" aria-label="${on ? "Unlink charge start from end" : "Link charge start to end"}">${on ? "🔒" : "🔓"}</button>`;
}

// ---------------------------------------------------------------- pointer handling (installed once)

function boardOf(el) {
  const host = el.closest("[data-dhost]")?.dataset.dhost;
  return host && S.state ? S.state.boards.find((x) => x.host === host) : null;
}

function valueAt(box, rect, x, y) {
  const v0 = Number(box.dataset.v0), v1 = Number(box.dataset.v1);
  if (box.dataset.axis === "y") return v0 + (v1 - v0) * (1 - (y - rect.top) / rect.height);
  const vb = Number(box.dataset.vb || 0);
  let fr = (x - rect.left) / rect.width;
  if (vb) { const u = fr * vb, p0 = Number(box.dataset.p0), p1 = Number(box.dataset.p1); fr = (u - p0) / (p1 - p0); }
  return v0 + (v1 - v0) * fr;
}

let tipEl = null;
function tip(text, x, y) {
  if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tip"; document.body.appendChild(tipEl); }
  tipEl.hidden = !text; tipEl.textContent = text || "";
  if (text) placeTip(tipEl, { left: x, right: x, top: y - 20, bottom: y + 20 });   // clear of the pointer and handle
}

document.addEventListener("pointerdown", (e) => {
  const h = e.target.closest("[data-drag]");
  if (!h || e.button > 0 || e.target.closest("[data-lock], [data-edit]")) return;
  const b = boardOf(h), box = h.closest("[data-dragbox]");
  if (!b || !box || !editable(b)) return;
  e.preventDefault();
  const f = h.dataset.drag, rect = box.getBoundingClientRect();
  // Keep where on the handle it was grabbed (no jump), and only start after a few pixels so a click changes nothing.
  const grab = valueAt(box, rect, e.clientX, e.clientY) - pv(b, f), x0 = e.clientX, y0 = e.clientY;
  let started = false;
  const move = (ev) => {
    if (!started && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 3) return;
    if (!started) document.body.classList.add("dragging-val");
    started = true;
    setDraft(b, f, valueAt(box, rect, ev.clientX, ev.clientY) - grab);
    tip(`${FIELDS[f].label} ${show(f, pv(b, f))}${CHARGE.includes(f) ? ` · ${f === "vc_charge_end" ? "Start" : "End"} ${show(f === "vc_charge_end" ? "vc_charge_start" : "vc_charge_end", pv(b, f === "vc_charge_end" ? "vc_charge_start" : "vc_charge_end"))}` : ""}`, ev.clientX, ev.clientY);
    hooks.rerender();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    document.body.classList.remove("dragging-val");
    tip("");
    hooks.rerender();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
});

// Keyboard: arrows on a focused handle nudge it one step.
document.addEventListener("keydown", (e) => {
  const h = e.target.closest && e.target.closest("[data-drag]");
  if (!h || !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
  const b = boardOf(h);
  if (!b || !editable(b)) return;
  e.preventDefault();
  const f = h.dataset.drag, st = FIELDS[f].step / scale(f) * (e.shiftKey ? 10 : 1);
  setDraft(b, f, pv(b, f) + (e.key === "ArrowUp" || e.key === "ArrowRight" ? st : -st));
  hooks.rerender();
});

document.addEventListener("click", async (e) => {
  const lock = e.target.closest("[data-lock]");
  if (lock) {
    const host = lock.dataset.lock;
    if (unlinked.has(host)) {
      unlinked.delete(host);
      const b = S.state.boards.find((x) => x.host === host);
      if (b && isPending(b, "vc_charge_end")) setDraft(b, "vc_charge_end", pv(b, "vc_charge_end"));
    } else unlinked.add(host);
    hooks.rerender();
    return;
  }
  const ed = e.target.closest("[data-edit]");
  if (ed) {
    const b = boardOf(ed);
    if (!b || !editable(b)) return;
    const f = ed.dataset.edit;
    if (!isPending(b, f)) drafts.set(b.host, { ...pending(b.host), [f]: actual(b, f) });
    hooks.rerender();
    setTimeout(() => { const i = document.querySelector(`[data-dhost="${CSS.escape(b.host)}"] [data-abf="${f}"]`); if (i) { i.focus(); i.select(); } }, 30);
    return;
  }
  const ab = e.target.closest("[data-abact]");
  if (!ab) return;
  const bar = ab.closest("[data-ab]"), b = boardOf(bar);
  if (!b) return;
  const fields = bar.dataset.ab.split(",");
  if (ab.dataset.abact === "reset") { reset(b.host, [...fields, ...CHARGE.filter((f) => fields.includes(f))]); hooks.rerender(); return; }
  const d = pending(b.host), mine = fields.filter((f) => f in d && Math.abs(d[f] - actual(b, f)) > 1e-6);
  if (!mine.length) { reset(b.host, fields); hooks.rerender(); return toast("Nothing changed."); }
  const charge = mine.some((f) => CHARGE.includes(f));
  const rest = Object.fromEntries(mine.filter((f) => !CHARGE.includes(f)).map((f) => [f, d[f]]));
  let ok = true;
  if (charge) ok = !!(await writeCharge(b, { end: d.vc_charge_end ?? actual(b, "vc_charge_end"), start: d.vc_charge_start ?? actual(b, "vc_charge_start") }));
  if (ok && Object.keys(rest).length) ok = !!(await writeFields(b, rest));
  if (ok) { reset(b.host, fields.concat(CHARGE)); hooks.rerender(); }
});

// Typed values in the apply bar: applied on Enter / leaving the field.
document.addEventListener("change", (e) => {
  const i = e.target.closest && e.target.closest("[data-abf]");
  if (!i) return;
  const b = boardOf(i), f = i.dataset.abf;
  if (!b || i.value === "") return;
  setDraft(b, f, Number(i.value) / scale(f), { snap: false });
  // a typed value isn't snapped, but stored to the field's precision
  const d = pending(b.host);
  if (f in d) d[f] = Math.round(d[f] * 1e4) / 1e4;
  hooks.rerender();
});

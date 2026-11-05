// Schedule tab: what the schedule is doing now, a palette of blocks/events you drag onto a 7-day
// timeline (an agenda list on phones), the item list, the item editor and the approval dialog. The server (schedule.py) decides what
// actually happens; the timeline just draws the items where they are.

import { $, setInner, api, ask, boardName, canWrite, clamp, dis, endFor, esc, f1, f3, fail, hm12, hooks, placeTip, S, skipConfirm, toast, whenAt } from "./core.js";
import { approve, currentPct, readTime, timeSelects, timerList } from "./writes.js";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const SNAP = 5;               // minutes
const NEW_BLOCK = 120;        // minutes, for a block dropped on the timeline
const LANES = [["limit", "Limit"], ["charge", "Charging"], ["balance", "Balancing"]];
const ACTIONS = {
  limit: { lane: "limit", block: "Charge to…", event: "Set limit" },
  charge_off: { lane: "charge", block: "No charging", event: "Stop charging" },
  charge_on: { lane: "charge", block: "Allow charging", event: "Start charging" },
  balance_off: { lane: "balance", block: "No balancing", event: "Stop balancing" },
  balance_on: { lane: "balance", block: "Allow balancing", event: "Allow balancing" },
};
const drafts = new Map(); // host -> schedule being edited

const mins = (s) => (s === "24:00" ? 1440 : Number(s.slice(0, 2)) * 60 + Number(s.slice(3, 5)));
const hhmm = (m) => { m = ((Math.round(m) % 1440) + 1440) % 1440; return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
const snap = (m) => Math.round(m / SNAP) * SNAP;
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const wd = (d) => (d.getDay() + 6) % 7; // Mon = 0
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const midnight = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
const pctX = (m) => (m / 1440) * 100;

const empty = () => ({ enabled: true, items: [] });
const clone = (x) => JSON.parse(JSON.stringify(x));
// Schedules saved before single events had "rules" with mode block|limit.
function upgrade(s) {
  if (!s) return s;
  if (!s.items && s.rules) return { ...s, items: s.rules.map((r) => ({ ...r, kind: "window", revert: true, action: r.mode === "block" ? "charge_off" : "limit" })) };
  return s;
}
const current = (b) => drafts.get(b.host) || upgrade(b.schedule) || empty();
// Unsaved schedule edits (for the tab dot and the "leave page?" check).
export const hasDraft = (host) => (host == null ? drafts.size > 0 : drafts.has(host));

function describeWhen(r) {
  if (r.repeat === "once") return new Date(r.date + "T00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  const d = [...r.days].sort();
  if (d.length === 7) return "Every day";
  if (d.join() === "0,1,2,3,4") return "Weekdays";
  if (d.join() === "5,6") return "Weekends";
  return d.map((i) => DAYS[i]).join(", ");
}
const describeTime = (r) => (r.kind === "event" ? `at ${hm12(r.start)}` : r.start === "00:00" && r.end === "24:00" ? "all day"
  : `${hm12(r.start)}–${hm12(r.end)}${mins(r.end) <= mins(r.start) && r.end !== "24:00" ? " (next day)" : ""}`);
export function describeWhat(r) {
  const a = ACTIONS[r.action] || ACTIONS.limit;
  const base = r.action === "limit" ? (r.kind === "event" ? `Set limit ${r.pct}%` : `Charge to ${r.pct}%`) : r.kind === "event" ? a.event : a.block;
  return base + (r.action === "limit" && r.kind === "window" && r.revert === false ? ", kept afterwards" : "");
}

// ---------------------------------------------------------------- status lines

function chanText(c, lane) {
  if (lane === "limit") return c.value && c.value.pct != null ? `${c.value.pct}%` : c.reverted ? "back to yours" : "–";
  return lane === "charge" ? (c.value ? "allowed" : "paused") : c.value ? "allowed" : "off";
}

function statusLines(ss) {
  if (!ss || !ss.channels) return "";
  return LANES.filter(([k]) => ss.channels[k]).map(([k, l]) => {
    const c = ss.channels[k], ended = c.reverted && c.kind === "end";
    return `<span class="sl"><i class="lane-dot lane-${k}"></i>${l}: <b>${esc(chanText(c, k))}</b> <span class="muted">${esc(c.source)}${ended ? " ended" : ""}${c.override ? ", overridden by you" : ""}${c.until_t ? " · until " + esc(whenAt(c.until_t)) : ""}</span></span>`;
  }).join("");
}

export function scheduleSummary(b) {
  const ss = b.schedule_status;
  const timers = timerList(b);
  const draft = drafts.has(b.host) ? ` <span class="chip warn">Edited, not saved</span>` : "";
  if (!ss || (!ss.configured && !(ss.timers || []).length)) return `<p class="muted small sched-sum">No charge schedule.${draft} <button type="button" class="linklike" data-goto="schedule">Set one up ›</button></p>`;
  if (ss.configured && !ss.enabled && !timers) return `<p class="muted small sched-sum">Schedule is off${ss.stale ? " (the cell profile changed; review it)" : ""}.${draft} <button type="button" class="linklike" data-goto="schedule">Schedule ›</button></p>`;
  return `<div class="small sched-sum">${ss.enabled ? `<span class="chip info">Schedule on</span>` : ""}${draft}${ss.next_t ? ` <span class="muted">next change ${esc(whenAt(ss.next_t))}</span>` : ""}${statusLines(ss)}${timers}<button type="button" class="linklike" data-goto="schedule">Schedule ›</button></div>`;
}

// ---------------------------------------------------------------- timeline

// Segments of item r drawn on day `day` (minutes 0..1440): its own occurrence and yesterday's overnight spill.
function segments(r, day) {
  const on = (d) => (r.repeat === "weekly" ? r.days.includes(wd(d)) : iso(d) === r.date);
  const out = [];
  const a = mins(r.start);
  if (r.kind === "event") { if (on(day)) out.push({ a, b: a, first: true, last: true }); return out; }
  const b = mins(r.end), over = b <= a;
  if (on(day)) out.push({ a, b: over ? 1440 : b, first: true, last: !over });
  if (over && b > 0 && on(addDays(day, -1))) out.push({ a: 0, b, first: false, last: true });
  return out;
}

function timeline(b, sched) {
  const today = midnight(new Date()), items = sched.items || [];
  const now = new Date(), nm = now.getHours() * 60 + now.getMinutes();
  const timers = (b.schedule_status && b.schedule_status.timers) || [];
  const laneLabs = `<div class="tl-lanes">${LANES.map(([k, l]) => `<span class="lane-${k}">${l}</span>`).join("")}</div>`;
  const axis = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) => `<span style="left:${pctX(h * 60)}%"${h % 6 ? ' class="minor"' : ""}>${h % 6 ? "" : h === 0 || h === 24 ? "12 AM" : h === 12 ? "12 PM" : (h % 12) + (h < 12 ? " AM" : " PM")}</span>`).join("");
  let rows = "";
  for (let i = 0; i < 7; i++) {
    const day = addDays(today, i);
    const lanes = LANES.map(([lane]) => {
      let html = "";
      items.forEach((r, idx) => {
        if (ACTIONS[r.action].lane !== lane) return;
        for (const sg of segments(r, day)) {
          const cls = `act-${r.action}${r.enabled === false ? " off" : ""}`;
          const tip = `${r.name === describeWhat(r) ? "" : r.name + ": "}${describeWhat(r)}, ${describeWhen(r)} ${describeTime(r)}${r.repeat === "weekly" && r.days.length > 1 ? " (dragging moves every day it repeats)" : ""}`;
          if (r.kind === "event") {
            html += `<button type="button" class="tl-pin ${cls}" style="left:${pctX(sg.a)}%" data-item="${idx}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"${dis()}><span>${r.action === "limit" ? r.pct + "%" : ""}</span></button>`;
          } else {
            const w = pctX(sg.b - sg.a);
            html += `<div class="tl-b ${cls}" style="left:${pctX(sg.a)}%;width:${w}%" data-item="${idx}" data-a="${sg.a}" data-b="${sg.b}" data-tip="${esc(tip)}" tabindex="0" role="button" aria-label="${esc(tip)}">
              ${sg.first && canWrite() ? `<i class="tl-h l" data-edge="start"></i>` : ""}<span>${w > 5 ? esc(r.action === "limit" ? r.pct + "%" : ACTIONS[r.action].block) : ""}</span>${sg.last && canWrite() ? `<i class="tl-h r" data-edge="end"></i>` : ""}</div>`;
          }
        }
      });
      for (const t of timers) {
        const d = new Date(t.at * 1000);
        if (midnight(d).getTime() !== day.getTime() || !(lane in t.actions)) continue;
        html += `<span class="tl-timer" style="left:${pctX(d.getHours() * 60 + d.getMinutes())}%" data-tip="Timer ${esc(whenAt(t.at))}: ${esc(t.label)}">⏱</span>`;
      }
      return `<div class="tl-lane lane-${lane}" data-lane="${lane}">${html}</div>`;
    }).join("");
    rows += `<div class="tl-row" data-day="${iso(day)}" data-wd="${wd(day)}"><div class="tl-lab${i === 0 ? " today" : ""}">${i === 0 ? "Today" : DAYS[wd(day)] + " " + day.getDate()}</div>
      ${laneLabs}<div class="tl-track">${lanes}${i === 0 ? `<i class="tl-now" style="left:${pctX(nm)}%"></i>` : ""}</div></div>`;
  }
  return `<div class="tl" data-tl><div class="tl-row tl-axisrow"><div class="tl-lab"></div><div></div><div class="tl-axis">${axis}</div></div>${rows}
    <div class="tl-legend"><span><i class="sw act-limit"></i>limit</span><span><i class="sw act-charge_on"></i>charging allowed</span><span><i class="sw act-charge_off"></i>no charging</span><span><i class="sw act-balance_on"></i>balancing allowed</span><span><i class="sw act-balance_off"></i>no balancing</span><span class="muted">◆ single event · ⏱ timer · ▏now</span></div></div>`;
}

// Phones: the same week as a list, day by day.
function agenda(b, sched) {
  const today = midnight(new Date()), items = sched.items || [];
  const timers = (b.schedule_status && b.schedule_status.timers) || [];
  let out = "";
  for (let i = 0; i < 7; i++) {
    const day = addDays(today, i), rows = [];
    items.forEach((r, idx) => { for (const sg of segments(r, day)) if (sg.first) rows.push({ m: sg.a, r, idx }); });
    for (const t of timers) {
      const d = new Date(t.at * 1000);
      if (midnight(d).getTime() === day.getTime()) rows.push({ m: d.getHours() * 60 + d.getMinutes(), t });
    }
    rows.sort((x, y) => x.m - y.m);
    const lab = i === 0 ? "Today" : i === 1 ? "Tomorrow" : day.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
    out += `<div class="ag-day"><div class="ag-lab">${esc(lab)}</div>${rows.map((x) => x.t
      ? `<div class="ag-row"><span class="ag-t">${esc(hm12(hhmm(x.m)))}</span><span class="ag-ico" aria-hidden="true">⏱</span><span class="ag-what"><b>Timer</b><small>${esc(x.t.label)}</small></span></div>`
      : `<button type="button" class="ag-row${x.r.enabled === false ? " off" : ""}" data-item="${x.idx}"${dis()}><span class="ag-t">${esc(x.r.kind === "event" ? hm12(x.r.start) : describeTime(x.r))}</span>
          <i class="sw act-${x.r.action}${x.r.kind === "event" ? " ev" : ""}" aria-hidden="true"></i><span class="ag-what"><b>${esc(describeWhat(x.r))}</b>${x.r.name !== describeWhat(x.r) ? `<small>${esc(x.r.name)}</small>` : ""}${x.r.enabled === false ? `<small>turned off</small>` : ""}</span><span class="chev" aria-hidden="true">›</span></button>`).join("")
      || `<div class="ag-none">Nothing scheduled</div>`}</div>`;
  }
  return out;
}

function palette() {
  const chip = (a, kind) => `<button type="button" class="pal act-${a} ${kind}" data-pal="${a}" data-kind="${kind}"${dis()}>${kind === "event" ? "◆ " : ""}${ACTIONS[a][kind === "event" ? "event" : "block"]}</button>`;
  return `<div class="palette"><div><span class="small muted"><span class="hide-sm">Drag a block onto a day, or tap one to add it:</span><span class="show-sm">Tap to add a block:</span></span>${Object.keys(ACTIONS).map((a) => chip(a, "window")).join("")}</div>
    <div><span class="small muted">Or a single event (no end time):</span>${Object.keys(ACTIONS).filter((a) => a !== "balance_on").map((a) => chip(a, "event")).join("")}${chip("balance_on", "event")}</div></div>`;
}

// ---------------------------------------------------------------- panel

export function renderSchedule(box, b) {
  if (!box || box._dragging) return;
  const sched = current(b), dirty = drafts.has(b.host), ss = b.schedule_status;
  const now = new Date();
  const key = JSON.stringify([sched, dirty, ss, canWrite(), skipConfirm(), b.cell && b.cell.curve, currentPct(b), now.getHours(), now.getMinutes()]);
  if (box._key === key) return;
  box._key = key;
  const on = b.schedule && b.schedule.enabled;
  const st = dirty ? `<span class="chip warn">Edited, not saved</span>` : !b.schedule ? `<span class="chip">Not set up</span>` : on ? `<span class="chip ok">Schedule on</span>` : `<span class="chip warn">Schedule off</span>`;
  const items = sched.items || [];
  const lines = statusLines(ss);
  const ell = skipConfirm() ? "" : "…";
  setInner(box, `
    <section class="panel"><h3>Right now</h3>
      <div class="sched-head">${st}${ss && ss.next_t ? `<span class="small">Next change <b>${esc(whenAt(ss.next_t))}</b></span>` : ""}</div>
      <div class="small sched-now">${lines || `<span class="muted">The schedule isn't changing anything on this board right now.</span>`}</div>
      ${ss && ss.stale ? `<div class="banner">The cell type or SOC curve changed, so the schedule was turned off. Review it and save to turn it back on.</div>` : ""}
      ${ss && ss.error ? `<div class="banner bad">${esc(ss.error)}</div>` : ""}
      ${ss && ss.last ? `<p class="muted small">Last action: ${esc(ss.last)}</p>` : ""}
      <h3 class="sub">Timers</h3>
      ${timerList(b) || `<p class="muted small">No timers. A timer does one thing at a time you pick, then the schedule takes over again.</p>`}
      <div class="row"><button type="button" class="ghost small" data-act="later" data-host="${esc(b.host)}"${dis()}>⏱ Add a timer…</button></div>
    </section>
    <section class="panel"><h3>This week</h3>
      ${palette()}
      <div class="tl-wrap">${timeline(b, sched)}</div>
      <div class="agenda">${agenda(b, sched)}</div>
    </section>
    <section class="panel"><h3>Schedule items${items.length ? ` (${items.length})` : ""}</h3>
      <div class="rules">${items.map((r, i) => `<div class="rule${r.enabled === false ? " off" : ""}">
        <i class="sw act-${r.action}${r.kind === "event" ? " ev" : ""}"></i>
        <div class="rule-txt"><b>${esc(r.name)}</b><span>${esc(describeWhen(r))} · ${esc(describeTime(r))}${r.name === describeWhat(r) ? "" : " · " + esc(describeWhat(r))}${r.action === "limit" && b.cell ? ` <span class="muted">(end ${f3(endFor(r.pct, b.cell))} V)</span>` : ""}</span></div>
        <div class="rule-btns">
          <label class="switch sm" title="Enabled"><input type="checkbox" data-sched="toggle" data-i="${i}" aria-label="Item on" ${r.enabled === false ? "" : "checked"}${dis()}><span></span></label>
          <button type="button" class="icon ghost" data-sched="edit" data-i="${i}" aria-label="Edit"${dis()}>✎</button>
          <button type="button" class="icon ghost" data-sched="del" data-i="${i}" aria-label="Delete"${dis()}>✕</button>
        </div></div>`).join("") || `<p class="muted small">Nothing scheduled yet. Tap "No charging" above to keep the pack from charging overnight, for example.</p>`}</div>
      ${dirty ? "" : `<div class="row">${on ? `<button type="button" class="ghost" data-sched="off"${dis()}>Turn schedule off</button>` : b.schedule ? `<button type="button" data-sched="on"${dis()}>Turn schedule on${ell}</button>` : ""}</div>`}
    </section>
    ${dirty ? `<div class="formbar schedbar"><span>Unsaved schedule changes. Nothing changes on the board until you save.</span><button type="button" class="ghost small" data-sched="discard">Discard</button><button type="button" data-sched="save"${dis()}>Save schedule${ell}</button></div>` : ""}
    <details class="panel howto"><summary>How the schedule works</summary>
      <p class="small">Each lane (limit, charging, balancing) works on its own. When blocks overlap, the one that started last wins, and when it ends the one still running takes over. Single events and timers hold until the next change. At the end of a block the board goes back to what it had before, unless a limit block is set to keep its limit. A limit you set by hand holds until the next change. "No charging" uses the live Pause command. Balancing off writes Balance Max Current = 0 A.</p>
      <p class="small"><b>Schedules only run while VBMS Manager is running on this computer.</b></p></details>`);
  box.onclick = (e) => onClick(e, b);
  box.onchange = (e) => onChange(e, b);
  box.onpointerdown = (e) => onPointerDown(e, b, box);
}

function edit(b, fn) {
  const d = clone(current(b));
  fn(d);
  drafts.set(b.host, d);
  hooks.rerender();
}

function onChange(e, b) {
  const t = e.target;
  if (t.dataset.sched === "toggle") edit(b, (d) => { d.items[Number(t.dataset.i)].enabled = t.checked; });
}

async function onClick(e, b) {
  if (e.target.closest(".tl-b, .tl-pin, .ag-row[data-item]")) {
    const el = e.target.closest("[data-item]");
    if (el && canWrite() && !el._moved) itemDialog(b, Number(el.dataset.item));
    return;
  }
  const pal = e.target.closest("[data-pal]");
  if (pal) { if (!pal._dragged) itemDialog(b, null, newItem(b, pal.dataset.pal, pal.dataset.kind, new Date())); pal._dragged = false; return; }
  const t = e.target.closest("[data-sched]");
  if (!t || t.tagName === "INPUT" || t.tagName === "SELECT") return;
  const i = Number(t.dataset.i), a = t.dataset.sched;
  if (a === "edit") return itemDialog(b, i);
  if (a === "del") { if (ask("Delete this item? (Nothing changes until you save the schedule.)")) edit(b, (d) => d.items.splice(i, 1)); return; }
  if (a === "discard") { drafts.delete(b.host); hooks.rerender(); return; }
  if (a === "save") return save(b, current(b));
  if (a === "on") return save(b, { ...clone(current(b)), enabled: true });
  if (a === "off") {
    if (!ask("Turn the schedule off? Anything a block changed goes back to what the board had before it (now, or when it reconnects), and a paused charge is allowed again. Timers still run.")) return;
    try { await api("/api/schedule/off", { host: b.host, mac: b.mac }); await hooks.refresh(); } catch (x) { fail(x); }
  }
}

// A new item at `at` (snapped), repeating weekly on that weekday. Limits default away from the current one.
function newItem(b, action, kind, at, dayIso) {
  const m = snap(at.getHours() * 60 + at.getMinutes()) % 1440, cp = currentPct(b);
  const it = { id: Math.random().toString(16).slice(2, 10), name: "", enabled: true, kind, action, repeat: "weekly", days: [wd(at)],
    date: dayIso || iso(at), start: hhmm(m), pct: cp != null && cp >= 90 ? 80 : 90 };
  if (kind === "window") { it.end = m + NEW_BLOCK === 1440 ? "24:00" : hhmm(m + NEW_BLOCK); it.revert = true; }
  it.name = describeWhat(it);
  return it;
}

// ---------------------------------------------------------------- dragging (palette -> timeline, move, resize)

function trackAt(box, x, y) {
  for (const row of box.querySelectorAll(".tl-row[data-day]")) {
    const tr = row.querySelector(".tl-track").getBoundingClientRect();
    if (y >= tr.top - 4 && y <= tr.bottom + 4) return { row, tr, m: clamp(((x - tr.left) / tr.width) * 1440, 0, 1439) };
  }
  return null;
}

function tipAt(text, x, y) {
  let t = document.getElementById("dragTip");
  if (!t) { t = document.createElement("div"); t.id = "dragTip"; t.className = "tip"; document.body.appendChild(t); }
  t.textContent = text; t.hidden = !text;
  if (text) placeTip(t, { left: x, right: x, top: y - 20, bottom: y + 20 });
}

function onPointerDown(e, b, box) {
  if (!canWrite() || e.button > 0) return;
  const pal = e.target.closest("[data-pal]");
  const blk = e.target.closest(".tl-b[data-item], .tl-pin[data-item]");
  if (!pal && !blk) return;
  const x0 = e.clientX, y0 = e.clientY;
  let moved = false, ghost = null;
  const sched = current(b);
  const idx = blk ? Number(blk.dataset.item) : null, it = blk ? sched.items[idx] : null;
  const edge = e.target.closest("[data-edge]") ? e.target.dataset.edge : null;
  const s0 = it ? mins(it.start) : 0, e0 = it && it.kind === "window" ? mins(it.end) : 0;
  let result = null;

  const move = (ev) => {
    const dx = ev.clientX - x0, dy = ev.clientY - y0;
    if (!moved && Math.hypot(dx, dy) < 6) return;
    if (!moved) { moved = true; box._dragging = true; (pal || blk).setPointerCapture?.(e.pointerId); }
    ev.preventDefault();
    if (pal) {
      if (!ghost) { ghost = pal.cloneNode(true); ghost.classList.add("ghost-drag"); document.body.appendChild(ghost); }
      ghost.style.left = ev.clientX - 20 + "px"; ghost.style.top = ev.clientY - 14 + "px";
      const hit = trackAt(box, ev.clientX, ev.clientY);
      result = hit ? { day: hit.row.dataset.day, m: snap(hit.m) } : null;
      tipAt(hit ? `${ACTIONS[pal.dataset.pal][pal.dataset.kind === "event" ? "event" : "block"]} · ${new Date(hit.row.dataset.day + "T00:00").toLocaleDateString(undefined, { weekday: "short" })} ${hm12(hhmm(result.m))}` : "Drop on a day", ev.clientX, ev.clientY);
      return;
    }
    const tr = blk.closest(".tl-track").getBoundingClientRect();
    const dm = snap((dx / tr.width) * 1440);
    let s = s0, en = e0;
    if (edge === "start") s = Math.min(s0 + dm, (e0 <= s0 ? e0 + 1440 : e0) - SNAP);
    else if (edge === "end") en = Math.max((e0 <= s0 ? e0 + 1440 : e0) + dm, s0 + SNAP);
    else { s = s0 + dm; en = e0 + dm; }
    result = { start: hhmm(s), end: it.kind === "window" ? (((en % 1440) + 1440) % 1440 === 0 ? "24:00" : hhmm(en)) : undefined };
    if (!edge) blk.style.transform = `translateX(${(dm / 1440) * tr.width}px)`;
    else {
      const sa = Number(blk.dataset.a), sb = Number(blk.dataset.b);
      const na = edge === "start" ? clamp(sa + (s - s0), 0, sb - SNAP) : sa;
      const nb = edge === "end" ? clamp(sb + (en - (e0 <= s0 ? e0 + 1440 : e0)), sa + SNAP, 1440) : sb;
      blk.style.left = pctX(na) + "%"; blk.style.width = pctX(nb - na) + "%";
    }
    tipAt(it.kind === "event" ? `at ${hm12(result.start)}` : `${hm12(result.start)} – ${hm12(result.end)}${it.repeat === "weekly" && it.days.length > 1 ? " · every " + describeWhen(it) : ""}`, ev.clientX, ev.clientY);
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    tipAt("", 0, 0);
    if (ghost) ghost.remove();
    box._dragging = false;
    if (!moved) return;
    if (pal) {
      pal._dragged = true;
      if (!result) { box._key = ""; hooks.rerender(); return; }
      const d = new Date(result.day + "T00:00"); d.setMinutes(result.m);
      itemDialog(b, null, newItem(b, pal.dataset.pal, pal.dataset.kind, d, result.day));
      return;
    }
    blk._moved = true;
    setTimeout(() => (blk._moved = false), 0);
    box._key = "";
    if (result && (result.start !== it.start || result.end !== it.end)) {
      if (result.start === "24:00" || result.start === result.end) { hooks.rerender(); return; }
      edit(b, (dd) => { dd.items[idx].start = result.start; if (result.end) dd.items[idx].end = result.end; });
    } else hooks.rerender();
  };
  window.addEventListener("pointermove", move, { passive: false });
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
}

// ---------------------------------------------------------------- approval

function rowWrites(r, p) {
  if (r.action === "limit") return r.volts ? `end <b>${f3(r.volts.end)} V</b> / start <b>${f3(r.volts.start)} V</b>${r.kind === "window" ? (r.revert ? ", back to the limit from before at the end" : ", kept at the end") : ""}` : "";
  if (r.action === "charge_off") return "Pause charging (live command, no write)" + (r.kind === "window" ? ", allowed again at the end" : "");
  if (r.action === "charge_on") return "Allow charging (live command, no write)";
  if (r.action === "balance_off") return `Balance Max Current <b>0 A</b> (config write)${r.kind === "window" ? `, back to ${f1(p.balance_normal_a)} A at the end` : ""}`;
  return `Balance Max Current back to <b>${f1(p.balance_normal_a)} A</b> (config write)`;
}

async function save(b, sched) {
  const body = { host: b.host, mac: b.mac };
  let p;
  try { p = await api("/api/schedule/prepare", { ...body, schedule: { ...sched, enabled: true } }); } catch (x) { return fail(x); }
  const ok = skipConfirm() || await approve("Approve schedule", `<p><b>${esc(p.name)}</b> · <span class="mono">${esc(p.host)}</span> · MAC <span class="mono">${esc(p.mac)}</span></p>
    <table class="diff"><tr><th>Item</th><th>Does</th></tr>
    ${p.rows.map((r) => `<tr class="${r.enabled ? "" : "muted"}"><td>${esc(r.rule)}${r.enabled ? "" : " (disabled)"}</td><td>${rowWrites(r, p)}</td></tr>`).join("") || `<tr><td colspan="2" class="muted">No items</td></tr>`}</table>
    ${p.blockers && p.blockers.length ? `<div class="banner bad">Writes are blocked on this board right now (${p.blockers.map(esc).join("; ")}). The schedule will save, but config writes will fail until that's fixed.</div>` : ""}
    <p class="small">When an item starts or ends, VBMS Manager makes these changes without asking again, using the same checks, backup and read-back as a manual change. Each one appears in the change log. Changing the schedule needs a new approval.</p>
    <p class="small">Before the first block changes something, the board's current value is read and kept, and it's put back when the blocks end, unless you changed it by hand in the meantime (then yours stays). If the board is offline at a start or end, it catches up when it reconnects. A block that started and ended while it was offline is skipped.</p>`,
  "I approve VBMS Manager making these changes to this BMS automatically at these times.", "Save and turn on");
  if (!ok) return;
  try {
    await api("/api/schedule/commit", { ...body, id: p.id, skipped_confirm: skipConfirm() });
    drafts.delete(b.host);
    await hooks.refresh();
    toast(`Schedule saved and on for ${boardName(b)}. Keep VBMS Manager running for it to work.`);
  } catch (x) { fail(x); }
}

// ---------------------------------------------------------------- item editor

function itemDialog(b, idx, fresh) {
  const sched = current(b);
  const isNew = idx == null;
  const today = iso(new Date());
  const r = isNew ? fresh || newItem(b, "charge_off", "window", new Date()) : { date: today, days: [0, 1, 2, 3, 4], pct: 80, revert: true, end: "06:00", ...clone(sched.items[idx]) };
  if (!r.end) r.end = hhmm(mins(r.start) + NEW_BLOCK);
  const autoName = !r.name || r.name === describeWhat(r);
  const dlg = $("#ruleDlg"), form = $("#ruleDlg form"), f = $("#ruleForm");
  $("#ruleTitle").textContent = isNew ? "New schedule item" : "Edit schedule item";
  const lim = S.state.limits, cp = currentPct(b);
  const radio = (name, v, label, cur) => `<label><input type="radio" name="${name}" value="${v}" ${cur === v ? "checked" : ""}><span>${label}</span></label>`;
  f.innerHTML = `
    <label class="field">What<select name="action">${Object.keys(ACTIONS).map((a) => `<option value="${a}"${a === r.action ? " selected" : ""}>${ACTIONS[a][r.kind === "event" ? "event" : "block"].replace("…", " a limit")}</option>`).join("")}</select></label>
    <div data-show="limit"><div class="stepper"><input type="range" name="pct" min="${lim.pct_min}" max="${lim.pct_max}" step="1" value="${r.pct}"><span class="pct" data-role="pct"></span></div><p class="muted small" data-role="volts"></p></div>
    <div class="seg" role="radiogroup" aria-label="Kind">${radio("kind", "window", "Block (from–until)", r.kind)}${radio("kind", "event", "Single event (no end time)", r.kind)}</div>
    <div class="seg" role="radiogroup" aria-label="Repeat">${radio("repeat", "weekly", "Repeats weekly", r.repeat)}${radio("repeat", "once", "One time", r.repeat)}</div>
    <div data-show="weekly"><div class="days">${DAYS.map((d, i) => `<label><input type="checkbox" name="day" value="${i}" ${r.days.includes(i) ? "checked" : ""}><span>${d}</span></label>`).join("")}</div>
      <div class="presets"><button type="button" class="chipbtn" data-days="0,1,2,3,4">Weekdays</button><button type="button" class="chipbtn" data-days="5,6">Weekends</button><button type="button" class="chipbtn" data-days="0,1,2,3,4,5,6">Every day</button></div></div>
    <div data-show="once"><label class="field">Date<input type="date" name="date" value="${esc(r.date)}" min="${today}"></label></div>
    <div class="row times"><label class="field"><span data-role="fromlab">From</span>${timeSelects("start", r.start)}</label>
      <label class="field" data-show="window">Until${timeSelects("end", r.end)}</label>
      <label class="check" data-show="window"><input type="checkbox" name="allday" ${r.start === "00:00" && r.end === "24:00" ? "checked" : ""}> All day</label></div>
    <p class="muted small" data-role="overnight"></p>
    <label class="check" data-show="revert"><input type="checkbox" name="revert" ${r.revert !== false ? "checked" : ""}> <span>At the end, go back to the limit from before${cp != null ? ` (now <b>${cp}%</b>)` : ""}</span></label>
    <label class="field"><span>Name <span class="muted small">(optional)</span></span><input name="name" maxlength="40" placeholder="${esc(describeWhat(r))}" value="${autoName ? "" : esc(r.name)}"></label>
    <p class="muted small" data-role="explain"></p>
    ${isNew ? "" : `<div class="row"><button type="button" class="ghost small" data-role="delete">Delete this item</button></div>`}`;
  const sync = () => {
    const fd = new FormData(form);
    const rep = fd.get("repeat"), action = fd.get("action"), kind = fd.get("kind"), all = fd.get("allday") === "on";
    f.querySelectorAll("[data-show]").forEach((el) => {
      const s = el.dataset.show;
      el.hidden = s === "revert" ? !(action === "limit" && kind === "window") : s === "window" ? kind !== "window" : s === "limit" ? action !== "limit" : s !== rep;
    });
    f.querySelector("[data-role=fromlab]").textContent = kind === "event" ? "At" : "From";
    [...form.elements.action.options].forEach((o) => (o.textContent = ACTIONS[o.value][kind === "event" ? "event" : "block"].replace("…", " a limit")));
    ["start_h", "start_m", "start_p", "end_h", "end_m", "end_p"].forEach((n) => (form.elements[n].disabled = kind === "window" && all));
    const p = Number(fd.get("pct"));
    f.querySelector("[data-role=pct]").textContent = p + "%";
    const end = endFor(p, b.cell);
    f.querySelector("[data-role=volts]").innerHTML = end != null ? `Writes end <b>${f3(end)} V</b> / start <b>${f3(end - 0.1)} V</b>` : "Choose a cell type and accept its curve first (Board tab).";
    const s = readTime(form, "start"), e = readTime(form, "end");
    f.querySelector("[data-role=overnight]").textContent = kind === "window" && !all && mins(e) <= mins(s) && e !== "00:00" ? `Runs overnight: ${hm12(s)} until ${hm12(e)} the next day.` : "";
    f.querySelector("[data-role=explain]").textContent = kind === "event"
      ? "A single event changes the board once at that time. It holds until the next schedule change, a timer or your own change."
      : action === "limit" ? "" : action.startsWith("charge") ? "Uses the live Pause/Allow command for the whole block; afterwards it goes back to how it was." : "Writes Balance Max Current (0 A = no balancing) at the start and puts your value back at the end.";
  };
  f.oninput = sync;
  f.onchange = sync;
  f.onclick = (e) => {
    if (e.target.closest("[data-role=delete]")) {
      if (ask("Delete this item? (Nothing changes until you save the schedule.)")) { dlg.close(); edit(b, (d) => d.items.splice(idx, 1)); }
      return;
    }
    const t = e.target.closest("[data-days]");
    if (!t) return;
    const set = t.dataset.days.split(",");
    f.querySelectorAll("[name=day]").forEach((c) => (c.checked = set.includes(c.value)));
  };
  sync();
  dlg.returnValue = "";
  $("#ruleOk").onclick = (e) => {
    const fd = new FormData(form);
    const all = fd.get("allday") === "on", kind = fd.get("kind");
    const out = { id: r.id || Math.random().toString(16).slice(2, 10), enabled: r.enabled !== false, kind, action: fd.get("action"), repeat: fd.get("repeat"),
      start: kind === "window" && all ? "00:00" : readTime(form, "start") };
    if (kind === "window") { const en = all ? "24:00" : readTime(form, "end"); out.end = en === "00:00" ? "24:00" : en; }
    if (out.repeat === "weekly") out.days = fd.getAll("day").map(Number); else out.date = fd.get("date");
    if (out.action === "limit") { out.pct = Number(fd.get("pct")); if (kind === "window") out.revert = fd.get("revert") === "on"; }
    out.name = String(fd.get("name") || "").trim() || describeWhat(out);
    const err = out.repeat === "weekly" && !out.days.length ? "Pick at least one day." : out.repeat === "once" && !out.date ? "Pick a date." : kind === "window" && out.start === out.end ? "Start and end can't be the same." : "";
    if (err) { e.preventDefault(); window.alert(err); return; }
    if (!isNew) {   // "Done" without changes: no draft, nothing to save
      const was = sched.items[idx];
      const norm = (k, v) => JSON.stringify(k === "days" ? [...(v || [])].sort() : v);
      if (Object.keys(out).every((k) => k === "id" || norm(k, out[k]) === norm(k, was[k]))) return;
    }
    edit(b, (d) => { d.items = d.items || []; if (isNew) d.items.push(out); else d.items[idx] = out; });
  };
  dlg.showModal();
}

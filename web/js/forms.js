// Settings forms built from the server's `editable` description.

import { canWrite, esc } from "./core.js";
import { writeFields } from "./writes.js";

const T_DOUBLE = 1, T_INT = 2, T_STRING = 3, T_ENUM = 4, T_BOOL = 5;
const dirty = new Map(); // "host|group" -> {name: value}

export function riskHits(rules, nv, ov) {
  return (rules || []).filter((r) =>
    (r.op === "changed" && nv !== ov) || (r.op === "eq" && nv === r.v) || (r.op === "ne" && nv !== r.v) ||
    (r.op === "lt" && nv < r.v) || (r.op === "gt" && nv > r.v));
}

function parse(f, el) {
  if (f.type === T_BOOL) return el.checked ? 1 : 0;
  if (f.type === T_ENUM) return Number(el.value);
  if (f.type === T_STRING) return el.value;
  if (el.value.trim() === "") return null;
  return Number(el.value);
}

function same(f, a, b) {
  if (f.type === T_DOUBLE) return a != null && b != null && Math.abs(a - b) < Math.pow(10, -(f.decimals || 3)) / 2;
  return a === b;
}

function input(f, val, disabled) {
  const d = disabled ? " disabled" : "";
  const id = `f_${f.name}`;
  if (f.type === T_BOOL) return `<label class="switch"><input type="checkbox" id="${id}" data-field="${f.name}" ${val ? "checked" : ""}${d}><span></span></label>`;
  if (f.type === T_ENUM) return `<select id="${id}" data-field="${f.name}"${d}>${f.options.map((o, i) => `<option value="${i}" ${i === val ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  if (f.type === T_STRING) {
    if (f.secret) return `<input type="password" id="${id}" data-field="${f.name}" autocomplete="new-password" placeholder="${f.is_set ? "unchanged (set)" : "not set"}" maxlength="${f.max_len || 64}" value="${esc(val ?? "")}"${d}>`;
    return `<input type="text" id="${id}" data-field="${f.name}" maxlength="${f.max_len || 64}" spellcheck="false" value="${esc(val ?? "")}"${d}>`;
  }
  if (f.secret) return `<input type="password" inputmode="numeric" id="${id}" data-field="${f.name}" autocomplete="off" placeholder="unchanged" value="${val ?? ""}"${d}>`;
  const v = val == null ? "" : f.type === T_DOUBLE ? Number(val).toFixed(f.decimals) : val;
  return `<span class="num"><input type="number" id="${id}" data-field="${f.name}" value="${v}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""} step="${f.step || "any"}"${d}>${f.suffix ? `<span class="unit">${esc(f.suffix)}</span>` : ""}</span>`;
}

// A rule's value in the field's own terms: on/off, the option's name, or a number with its unit.
function ruleVal(f, v) {
  if (f.type === T_BOOL) return v ? "on" : "off";
  if (f.type === T_ENUM) return f.options[v] ?? v;
  return `${v}${f.suffix ? " " + f.suffix : ""}`;
}

function ruleWhen(f, r) {
  if (r.op === "changed") return "If you change it";
  if (r.op === "eq") return f.type === T_BOOL ? `If you turn it ${ruleVal(f, r.v)}` : `If you set it to ${ruleVal(f, r.v)}`;
  if (r.op === "ne") return `If you set it to anything other than ${ruleVal(f, r.v)}`;
  if (r.op === "lt") return `Below ${ruleVal(f, r.v)}`;
  if (r.op === "gt") return `Above ${ruleVal(f, r.v)}`;
  return "";
}

// The ⛔/⚠ marker shows the setting's warnings in a box under the row (riskInfo).
function badge(f) {
  const rs = f.risks || [];
  if (!rs.length) return "";
  const lv = rs.some((r) => r.level === "danger") ? "danger" : "caution";
  const what = lv === "danger" ? "Can be dangerous to change" : "Change with care";
  return `<button type="button" class="rbadge ${lv}" data-rtoggle="${f.name}" aria-expanded="false" aria-controls="rinfo_${f.name}" title="${what} · click for the warning">${lv === "danger" ? "⛔" : "⚠"}</button>`;
}

function riskInfo(f) {
  const rs = f.risks || [];
  if (!rs.length) return "";
  return `<div class="rinfo" id="rinfo_${f.name}" hidden>${rs.map((r) => `<div class="risk ${r.level}"><b>${r.level === "danger" ? "⛔ Danger" : "⚠ Caution"}</b> · ${esc(ruleWhen(f, r))}: ${esc(r.text)}</div>`).join("")}</div>`;
}

/** Render one group's form into `box`. Rebuilds only when the board's values change. */
export function renderForm(box, b, group, { intro = "", only = null } = {}) {
  if (!group) { box.innerHTML = `<div class="muted">Loading settings…</div>`; return; }
  const fields = only ? group.fields.filter((f) => only.includes(f.name)) : group.fields;
  const key = JSON.stringify([b.host, canWrite(), fields.map((f) => [f.name, f.value, f.is_set])]);
  if (box.dataset.key === key) return;
  if (box.contains(document.activeElement) && box.dataset.key) return; // don't yank the field you're typing in
  box.dataset.key = key;
  const dk = b.host + "|" + group.id + (only ? "|" + only.join(",") : "");
  const dmap = dirty.get(dk) || {};
  dirty.set(dk, dmap);
  const ro = !canWrite();
  box.innerHTML = `${intro}<div class="form">${fields.map((f) => {
    const val = f.name in dmap ? dmap[f.name] : f.secret ? (f.type === T_STRING ? "" : null) : f.value;
    return `<div class="frow${f.name in dmap ? " dirty" : ""}" data-row="${f.name}">
      <div class="flabel"><label for="f_${f.name}">${esc(f.label)}</label>${badge(f)}
        ${f.help ? `<details class="fhelp"><summary aria-label="About ${esc(f.label)}">?</summary><p>${esc(f.help).replace(/\n/g, "<br>")}</p></details>` : ""}
        ${f.min != null && (f.type === T_DOUBLE || f.type === T_INT) && !f.secret ? `<span class="range muted">${f.min}–${f.max}${f.suffix ? " " + esc(f.suffix) : ""}</span>` : ""}</div>
      <div class="finput">${input(f, val, ro)}</div>
      ${riskInfo(f)}<div class="frisk" data-risk="${f.name}"></div></div>`;
  }).join("")}</div>
  <div class="formbar" hidden><span data-role="count"></span><button type="button" class="ghost small" data-formbtn="discard">Discard</button><button type="button" data-formbtn="review">Review changes…</button></div>`;

  const byName = Object.fromEntries(fields.map((f) => [f.name, f]));
  const sync = () => {
    for (const f of fields) {
      const row = box.querySelector(`[data-row="${f.name}"]`);
      const isD = f.name in dmap;
      row.classList.toggle("dirty", isD);
      const hits = isD ? riskHits(f.risks, dmap[f.name], f.value) : [];
      row.querySelector(".frisk").innerHTML = hits.map((r) => `<div class="risk ${r.level}"><b>${r.level === "danger" ? "⛔ Danger" : "⚠ Caution"}</b> ${esc(r.text)}</div>`).join("");
    }
    const n = Object.keys(dmap).length;
    const bar = box.querySelector(".formbar");
    bar.hidden = n === 0;
    bar.querySelector("[data-role=count]").textContent = `${n} unsaved change${n === 1 ? "" : "s"}`;
  };
  const onEdit = (e) => {
    const el = e.target.closest("[data-field]");
    if (!el) return;
    const f = byName[el.dataset.field];
    const v = parse(f, el);
    const blankSecret = f.secret && (v === "" || v == null);
    if (blankSecret || (!f.secret && same(f, v, f.value)) || v == null) delete dmap[f.name];
    else dmap[f.name] = v;
    sync();
  };
  box.oninput = onEdit;
  box.onchange = onEdit;
  box.onclick = async (e) => {
    const rt = e.target.closest("[data-rtoggle]");
    if (rt) {
      const info = box.querySelector("#rinfo_" + rt.dataset.rtoggle);
      info.hidden = !info.hidden;
      rt.setAttribute("aria-expanded", String(!info.hidden));
      return;
    }
    const t = e.target.closest("[data-formbtn]");
    if (!t) return;
    if (t.dataset.formbtn === "discard") { for (const k of Object.keys(dmap)) delete dmap[k]; box.dataset.key = ""; renderForm(box, b, group, { intro, only }); return; }
    const res = await writeFields(b, { ...dmap });
    if (res) { for (const k of Object.keys(dmap)) delete dmap[k]; box.dataset.key = ""; }
  };
  sync();
}

export function hasDirty(host) {
  for (const [k, m] of dirty) if (k.startsWith(host + "|") && Object.keys(m).length) return true;
  return false;
}

export function readOnlyList(ed) {
  if (!ed) return "";
  return `<dl class="kv">${ed.read_only.map((r) => `<dt>${esc(r.label)}</dt><dd>${esc(typeof r.value === "number" ? +r.value.toFixed(3) : r.value)}${r.suffix ? " " + esc(r.suffix) : ""} <span class="muted small">${esc(r.why)}</span></dd>`).join("")}</dl>`;
}


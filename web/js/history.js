// History: the History tab (graphs, charge sessions, daily energy, health) and the
// range-chip history panels on the Cells and Temps tabs. Data comes from /api/history,
// /api/charges and /api/analysis, cached here and refetched on its own clock (not every
// 1 s render); the pointer layer (crosshair, readout, drag-to-zoom) lives outside the
// slots so redraws don't disturb it.

import { api, canWrite, esc, f1, f2, fail, hooks, S, store } from "./core.js";
import { cellBars, clock, dayBars, dayLabel, driftMap, dur, historyChart, pointsAt, spark, when } from "./charts.js";

const RANGES = [["15m", 900], ["1h", 3600], ["6h", 21600], ["24h", 86400], ["7d", 604800], ["30d", 2592000], ["all", 0]];
const PANEL_RANGES = ["15m", "1h", "24h", "7d"];
const METRICS = [["power", "Power"], ["current", "Current"], ["voltage", "Voltage"], ["soc", "SOC"], ["cells", "Cells"],
  ["spread", "Spread"], ["temps", "Temps"], ["charger", "Charger"]];
const VIEWS = [["graphs", "Graphs"], ["charges", "Charges"], ["daily", "Daily"], ["health", "Health"]];
const span = (r) => (RANGES.find((x) => x[0] === r) || RANGES[3])[1];

const ui = Object.assign({ view: "graphs", metric: "power", range: "24h", cell: "", temps: "", days: 30, panels: {} },
  store("vbms_hist") || {});
const zoom = new Map(); // host|chart -> [t0, t1]
const save = () => store("vbms_hist", ui);

// ---------------------------------------------------------------- data

const cache = new Map();
function want(key, url, maxAge) {
  let e = cache.get(key);
  if (!e) {
    e = { data: null, at: 0, pending: false, err: null };
    cache.set(key, e);
    if (cache.size > 60) cache.delete(cache.keys().next().value);
  }
  if (!e.pending && Date.now() - e.at > maxAge) {
    e.pending = true;
    api(url).then((d) => { e.data = d; e.err = null; }).catch((x) => { e.err = x.message; })
      .finally(() => { e.pending = false; e.at = Date.now(); hooks.rerender(); });
  }
  return e;
}
export const fetchCached = (key, url, maxAge) => want(key, url, maxAge);
export const forget = (prefix) => { for (const k of [...cache.keys()]) if (k.startsWith(prefix)) cache.delete(k); };

function sessionsFor(b) {
  const e = want("charges|" + b.host, "/api/charges?host=" + encodeURIComponent(b.host), 60000);
  const out = ((e.data && e.data.charges) || []).map((c) => ({ start: c.start, end: c.end }));
  if (b.charging_now) out.push({ start: b.charging_now.start, end: Date.now() / 1000 });
  return out;
}

function series(b, id, req) {
  const z = zoom.get(b.host + "|" + id);
  const now = Date.now() / 1000;
  const sp = span(req.range);
  const t0 = z ? z[0] : sp ? now - sp : 0, t1 = z ? z[1] : now;
  const iv = (S.state.settings.logging || {}).interval_s || 10;
  const age = z ? 3600e3 : (sp && sp <= 21600 ? Math.max(iv, 5) * 1000 : 60000);
  const q = `host=${encodeURIComponent(b.host)}&metric=${req.metric}&cell=${encodeURIComponent(req.cell || "")}&points=${req.points || 400}`;
  const key = `h|${b.host}|${id}|${req.metric}|${req.cell || ""}|${z ? z.join("-") : req.range}`;
  return want(key, `/api/history?${q}&from=${Math.floor(t0)}&to=${Math.ceil(t1)}`, age);
}

// ---------------------------------------------------------------- chart + pointer layer

const charts = new WeakMap();
const ro = "ResizeObserver" in window ? new ResizeObserver(() => hooks.rerender()) : null;

export const chartSlot = (id) => `<div class="hc" data-chart="${id}"><div class="hc-plot"></div><div class="hc-x" hidden></div><div class="hc-sel" hidden></div><div class="hc-tip" hidden></div></div><div class="hc-leg" data-leg="${id}"></div>`;

function legend(d, slotOf) {
  const ss = d ? d.series.filter((x) => x.pts.length) : [];
  if (ss.length > 8) return `<div class="legend"><span class="muted">${ss.length} lines · hover for the highest and lowest at that moment</span></div>`;
  if (ss.length < 2) return "";
  return `<div class="legend">${ss.map((x, i) => `<span><i class="sw" style="background:var(--s${slotOf(x.name, i) % 8})"></i>${esc(x.name)}</span>`).join("")}</div>`;
}

function slotOf(metric) {
  const fixed = { "highest cell": 7, "lowest cell": 0, "cells (avg)": 0, hottest: 7, IC: 1, MOSFET: 2, Ambient: 3 };
  return (name, i) => {
    if (name in fixed) return fixed[name];
    const m = /^Cell sensor (\d+)$/.exec(name);
    if (m) return (3 + Number(m[1])) % 8;
    return metric === "temps" ? i : 0;
  };
}

function drawChart(root, id, entry, { host, height = 210, sessions = [] }) {
  const el = root.querySelector(`[data-chart="${id}"]`);
  if (!el) return;
  const plot = el.querySelector(".hc-plot");
  let html;
  const data = entry.data;
  // While a new metric/range loads, keep the previous chart up (dimmed) so the page doesn't jump.
  if (!data && !entry.err && plot._html && charts.get(el) && charts.get(el).data) { plot.classList.add("stale"); return; }
  if (!data) html = `<div class="hc-empty muted small">${entry.err ? esc(entry.err) : "Loading…"}</div>`;
  const mono = data && data.series.length > 8;
  const opts = { width: el.clientWidth || 640, height, sessions, mono, slotOf: slotOf(data && data.metric) };
  const res = data ? historyChart(data, opts) : { html, geom: null };
  if (plot._html !== res.html) { plot._html = res.html; plot.innerHTML = res.html; }
  const leg = root.querySelector(`[data-leg="${id}"]`), lh = legend(data, opts.slotOf);
  if (leg && leg._html !== lh) { leg._html = lh; leg.innerHTML = lh; }
  plot.classList.toggle("stale", !!entry.pending && !!data);
  charts.set(el, { data, geom: res.geom, mono, slot: opts.slotOf, onZoom: (z) => { zoom.set(host + "|" + id, z); if (!z) zoom.delete(host + "|" + id); hooks.rerender(); } });
  if (!el._bound) { el._bound = true; bind(el); if (ro) ro.observe(el); }
}

const fmtV = (v, d) => (v == null ? "–" : Number(v).toFixed(d));

function readout(st, t) {
  const d = st.data;
  let rows = pointsAt(d, t).filter((r) => r.p);
  if (!rows.length) return "";
  const unit = d.unit === "%" ? "%" : " " + d.unit;
  const line = (r) => `<div><span>${st.mono ? "" : `<i class="sw" style="background:var(--s${st.slot(r.name, r.i) % 8})"></i>`}${esc(r.name)}</span><b>${fmtV(r.p[1], d.decimals)}${unit}</b>${r.p[3] - r.p[2] > 1e-9 && !st.mono ? `<em>${fmtV(r.p[2], d.decimals)}–${fmtV(r.p[3], d.decimals)}</em>` : ""}</div>`;
  if (rows.length > 8) {
    rows.sort((a, b) => b.p[1] - a.p[1]);
    const hi = rows.slice(0, 4), lo = rows.slice(-4);
    return `<div class="tt-h">${when(rows[0].p[0])}</div>${hi.map(line).join("")}<div class="muted">…</div>${lo.map(line).join("")}
      <div class="muted">spread ${((hi[0].p[1] - lo[lo.length - 1].p[1]) * (d.unit === "V" ? 1000 : 1)).toFixed(1)}${d.unit === "V" ? " mV" : unit}</div>`;
  }
  return `<div class="tt-h">${when(rows[0].p[0])}${d.w > 90 ? ` <span class="muted">· ${dur(d.w)} avg</span>` : ""}</div>${rows.map(line).join("")}`;
}

function bind(el) {
  const x = el.querySelector(".hc-x"), sel = el.querySelector(".hc-sel"), tip = el.querySelector(".hc-tip");
  let drag = null;
  const pos = (e) => {
    const st = charts.get(el), g = st && st.geom;
    if (!g) return null;
    const r = el.getBoundingClientRect(), px = e.clientX - r.left;
    const f = Math.max(0, Math.min(1, (px - g.L) / (g.W - g.L - g.R)));
    return { st, g, px: g.L + f * (g.W - g.L - g.R), t: g.t0 + f * (g.t1 - g.t0), w: r.width };
  };
  const hide = () => { x.hidden = tip.hidden = true; };
  el.addEventListener("pointermove", (e) => {
    const p = pos(e);
    if (!p) return hide();
    x.hidden = false; x.style.left = p.px + "px"; x.style.top = p.g.T + "px"; x.style.height = (p.g.H - p.g.T - p.g.B) + "px";
    const html = readout(p.st, p.t);
    tip.hidden = !html; tip.innerHTML = html;
    const tw = tip.offsetWidth;
    tip.style.left = (p.px + 12 + tw > p.w ? Math.max(0, p.px - 12 - tw) : p.px + 12) + "px";
    if (drag) {
      const a = Math.min(drag.px, p.px), b = Math.max(drag.px, p.px);
      sel.hidden = b - a < 4; sel.style.left = a + "px"; sel.style.width = (b - a) + "px"; sel.style.top = p.g.T + "px"; sel.style.height = (p.g.H - p.g.T - p.g.B) + "px";
    }
  });
  el.addEventListener("pointerleave", () => { if (!drag) hide(); });
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const p = pos(e);
    if (p) { drag = { px: p.px, t: p.t }; el.setPointerCapture(e.pointerId); }
  });
  const end = (e) => {
    if (!drag) return;
    const p = pos(e), d = drag;
    drag = null; sel.hidden = true;
    if (p && Math.abs(p.px - d.px) > 10) {
      const a = Math.min(d.t, p.t), b = Math.max(d.t, p.t);
      if (b - a >= 60) p.st.onZoom([Math.floor(a), Math.ceil(b)]);
    }
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", () => { drag = null; sel.hidden = true; });
  el.addEventListener("dblclick", () => { const st = charts.get(el); if (st) st.onZoom(null); });
}

// ---------------------------------------------------------------- panels on the Cells / Temps tabs

export function rangeChips(host, id) {
  const r = ui.panels[id] || "15m", z = zoom.get(host + "|" + id);
  return `<span class="chips">${z ? `<button type="button" class="chipbtn on" data-hv="pzoom" data-id="${id}" data-host="${esc(host)}">Zoomed · reset</button>` : ""}${PANEL_RANGES.map((x) =>
    `<button type="button" class="chipbtn${!z && x === r ? " on" : ""}" data-hv="prange" data-id="${id}" data-r="${x}" data-host="${esc(host)}">${x}</button>`).join("")}</span>`;
}

export function drawPanel(root, b, id, req) {
  if (!b.mac) return;
  const e = series(b, id, { ...req, range: ui.panels[id] || "15m", points: 300 });
  drawChart(root, id, e, { host: b.host, height: 150, sessions: sessionsFor(b) });
}

// ---------------------------------------------------------------- the History tab

function seg(items, cur, kind, host) {
  return items.map(([k, label]) => `<button type="button" class="${k === cur ? "on" : ""}" data-hv="${kind}" data-v="${k}" data-host="${esc(host)}">${label}</button>`).join("");
}

function head(b) {
  return `<div class="hv-bar"><div class="seg" role="tablist">${seg(VIEWS, ui.view, "view", b.host)}</div>
    <span class="muted small">${recordingLine()}</span></div>`;
}

export function recordingLine() {
  const st = (S.state && S.state.settings.logging) || {};
  if (!st.enabled) return `Recording is off · <button type="button" class="linklike" data-hv="settings">turn on</button>`;
  return `Recording every ${st.interval_s < 60 ? st.interval_s + " s" : st.interval_s / 60 + " min"} · ${st.level} · ${st.retention_days ? "keep " + st.retention_days + " days" : "kept forever"} · <button type="button" class="linklike" data-hv="settings">change</button>`;
}

export function historyView(root, b) {
  if (!b.mac) { root.dataset.view = ""; root.innerHTML = `<section class="panel"><div class="muted">History appears once the board has been read.</div></section>`; return; }
  const views = { graphs, charges, daily, health };
  (views[ui.view] || graphs)(root, b);
}

function skeleton(root, key, html) {
  if (root.dataset.view !== key) { root.dataset.view = key; root.innerHTML = html; return true; }
  return false;
}
function slot(root, name, html) {
  const el = root.querySelector(`[data-slot="${name}"]`);
  if (el && el._html !== html) { el._html = html; el.innerHTML = html; }
  return el;
}

function rangeSummary(d) {
  const s = d && d.series.filter((x) => x.pts.length);
  if (!s || !s.length) return "";
  const unit = d.unit === "%" ? "%" : " " + d.unit;
  if (d.metric === "power") {
    let inW = 0, outW = 0, pk = 0, pkOut = 0;
    const pts = s[0].pts;
    for (let i = 0; i < pts.length; i++) {
      const dt = i ? Math.min(pts[i][0] - pts[i - 1][0], d.gap) : 0;
      const v = pts[i][1];
      if (v > 0) inW += v * dt; else outW -= v * dt;
      pk = Math.max(pk, pts[i][3]); pkOut = Math.min(pkOut, pts[i][2]);
    }
    return `≈ <b>${Math.round(inW / 3600)} Wh</b> into the pack · <b>${Math.round(outW / 3600)} Wh</b> out · peak <b>${Math.round(pk)} W</b> in${pkOut < -1 ? `, <b>${Math.round(-pkOut)} W</b> out` : ""}`;
  }
  if (s.length > 2) return `${s.length} series`;
  return s.map((x) => {
    const lo = Math.min(...x.pts.map((p) => p[2])), hi = Math.max(...x.pts.map((p) => p[3]));
    return `${s.length > 1 ? esc(x.name) + " " : ""}<b>${fmtV(lo, d.decimals)}–${fmtV(hi, d.decimals)}${unit}</b>`;
  }).join(" · ");
}

function graphs(root, b) {
  skeleton(root, "history|graphs|" + b.host, `<section class="panel hv">
      <div data-slot="head"></div>
      <div class="hv-ctrl"></div>
      <div class="hv-sum small" data-slot="sum"></div>
      ${chartSlot("main")}
      <p class="muted small">Drag across the chart to zoom in, double-click to zoom back out. Shaded bands are charge sessions. Power and current are positive into the pack.</p>
    </section>`);
  slot(root, "head", head(b));
  const n = b.values ? b.values.cells.length : b.cells_configured || 0;
  const z = zoom.get(b.host + "|main");
  const sub = ui.metric === "cells"
    ? `<select data-hvsel="cell" data-host="${esc(b.host)}" aria-label="Which cells"><option value="">Highest &amp; lowest</option><option value="all"${ui.cell === "all" ? " selected" : ""}>Every cell</option>${Array.from({ length: n }, (_, k) => `<option value="${k + 1}"${String(ui.cell) === String(k + 1) ? " selected" : ""}>Cell ${k + 1}</option>`).join("")}</select>`
    : ui.metric === "temps" ? `<select data-hvsel="temps" data-host="${esc(b.host)}" aria-label="Which sensors"><option value="">Cells averaged</option><option value="all"${ui.temps === "all" ? " selected" : ""}>Every sensor</option></select>` : "";
  const ctrl = root.querySelector(".hv-ctrl");
  const ctrlHtml = `<div class="seg">${seg(METRICS, ui.metric, "metric", b.host)}</div>${sub}
    <div class="seg">${z ? `<button type="button" class="on" data-hv="zoomout" data-host="${esc(b.host)}">${esc(when(z[0]))} – ${esc(when(z[1]))} ✕</button>` : seg(RANGES.map(([k]) => [k, k === "all" ? "All" : k]), ui.range, "range", b.host)}</div>`;
  if (ctrl._html !== ctrlHtml) { ctrl._html = ctrlHtml; ctrl.innerHTML = ctrlHtml; }
  const cell = ui.metric === "cells" ? ui.cell : ui.metric === "temps" ? ui.temps : "";
  const e = series(b, "main", { metric: ui.metric, cell, range: ui.range, points: 500 });
  drawChart(root, "main", e, { host: b.host, height: 240, sessions: sessionsFor(b) });
  slot(root, "sum", e.data ? rangeSummary(e.data) : "");
}

const reasonCls = { limit: "ok", unplugged: "", paused: "warn", fault: "bad", temperature: "warn", offline: "warn", unknown: "" };

function charges(root, b) {
  skeleton(root, "history|charges|" + b.host, `<section class="panel hv"><div data-slot="head"></div><div data-slot="body"></div>
    <p class="muted small">Energy is integrated from the 1 s readings while VBMS Manager was connected. "Joined late" means the charge started before it connected. The capacity estimate needs a rest before and after the charge, both inside the SOC curve's range, so it only appears for some charges, and it's only as good as the curve.</p></section>`);
  slot(root, "head", head(b));
  const e = want("charges|" + b.host, "/api/charges?host=" + encodeURIComponent(b.host), 30000);
  if (!e.data) { slot(root, "body", `<div class="muted">${e.err ? esc(e.err) : "Loading…"}</div>`); return; }
  const live = b.charging_now;
  const rows = e.data.charges;
  if (!rows.length && !live) { slot(root, "body", `<div class="muted">No charges recorded yet.</div>`); return; }
  const tr = (c) => `<tr class="click" data-hv="session" data-a="${c.start}" data-b="${c.end}" data-host="${esc(b.host)}" tabindex="0">
      <td>${esc(when(c.start))}</td><td>${dur(c.duration_s)}</td><td>${Math.round(c.soc0 * 100)} → ${Math.round(c.soc1 * 100)}%</td>
      <td>+${f2(c.ah)} Ah<br><span class="muted">+${Math.round(c.wh)} Wh</span></td><td>${Math.round(c.p_peak)} W<br><span class="muted">avg ${c.p_avg != null ? Math.round(c.p_avg) : "–"} W</span></td>
      <td>${c.t_cell_max != null ? f1(c.t_cell_max) + "°" : "–"}</td>
      <td><span class="chip ${reasonCls[c.reason] || ""}">${esc(c.reason_text)}</span>${c.joined_late ? `<br><span class="muted small">joined late</span>` : ""}</td>
      <td>${c.capacity_ah ? `≈ ${f2(c.capacity_ah)} Ah` : '<span class="muted">–</span>'}</td></tr>`;
  const liveRow = live ? `<tr class="click live" data-hv="session" data-a="${live.start}" data-b="${Math.ceil(Date.now() / 1000)}" data-host="${esc(b.host)}" tabindex="0">
      <td>${esc(when(live.start))}</td><td>${dur(live.elapsed_s)}</td><td>${Math.round(live.soc0 * 100)} → ${b.values ? Math.round(b.values.soc * 100) : "–"}%</td>
      <td>+${f2(live.ah)} Ah<br><span class="muted">+${Math.round(live.wh)} Wh</span></td><td colspan="2">${b.values ? Math.round(-b.values.i_in_ic * b.values.v_tot) + " W now" : ""}</td>
      <td><span class="chip info">charging</span>${live.eta != null ? `<br><span class="muted small">~${clock(Date.now() / 1000 + live.eta)}</span>` : ""}</td><td></td></tr>` : "";
  slot(root, "body", `<div class="scroll"><table class="htable"><tr><th>Start</th><th>Time</th><th>SOC</th><th>Added</th><th>Power</th><th>Max cell temp</th><th>Stopped</th><th>Capacity</th></tr>${liveRow}${rows.map(tr).join("")}</table></div>`);
}

function daily(root, b) {
  skeleton(root, "history|daily|" + b.host, `<section class="panel hv"><div data-slot="head"></div>
    <div class="hv-ctrl" data-slot="ctrl"></div>${'<div class="hc" data-bars><div data-slot="bars"></div></div>'}<div data-slot="table"></div>
    <p class="muted small">Wh are integrated from the recorded power. Days marked "partly recorded" had the board offline or asleep for over half the day (a sleeping BMS uses next to nothing, so that's normal). Resting spread is the median cell spread with no current flowing.</p></section>`);
  slot(root, "head", head(b));
  slot(root, "ctrl", `<div class="seg">${seg([[30, "30 days"], [90, "90 days"]], ui.days, "days", b.host)}</div>`);
  const e = analysisFor(b);
  if (!e.data) { slot(root, "bars", `<div class="muted">${e.err ? esc(e.err) : "Loading…"}</div>`); return; }
  const days = e.data.daily.slice(-ui.days);
  const box = root.querySelector("[data-bars]");
  slot(root, "bars", dayBars(days, { width: box.clientWidth || 640 }));
  if (ro && !box._ro) { box._ro = true; ro.observe(box); }
  slot(root, "table", days.length ? `<div class="scroll"><table class="htable"><tr><th>Day</th><th>Into pack</th><th>Out</th><th>Charging</th><th>SOC</th><th>Max temp</th><th>Resting spread</th><th>Recorded</th></tr>
    ${days.slice().reverse().map((d) => `<tr class="click" data-day="${esc(d.day)}" data-host="${esc(b.host)}" tabindex="0"><td>${esc(dayLabel(new Date(d.day + "T12:00:00").getTime() / 1000))}</td>
      <td>${Math.round(d.wh_in)} Wh</td><td>${Math.round(d.wh_out)} Wh</td><td>${d.chg_s ? dur(d.chg_s) : "–"}${d.charges ? ` <span class="muted">(${d.charges})</span>` : ""}</td>
      <td>${d.soc_min != null ? Math.round(d.soc_min) + "–" + Math.round(d.soc_max) + "%" : "–"}</td><td>${d.tmax != null ? f1(d.tmax) + "°" : "–"}</td>
      <td>${d.rest_d_med != null ? f1(d.rest_d_med) + " mV" : "–"}</td><td>${Math.round(d.coverage * 100)}%${d.low_coverage ? ' <span class="muted">partly</span>' : ""}</td></tr>`).join("")}</table></div>` : "");
}

function analysisFor(b) {
  return want("an|" + b.host, "/api/analysis?host=" + encodeURIComponent(b.host), 60000);
}

function tile(label, value, sub = "") {
  return `<div class="tile"><span>${label}</span><b>${value}</b>${sub ? `<em>${sub}</em>` : ""}</div>`;
}

function health(root, b) {
  skeleton(root, "history|health|" + b.host, `<section class="panel hv"><div data-slot="head"></div><div class="tiles" data-slot="tiles"></div>
      <div class="hv-pack" data-slot="pack"></div><div data-slot="flags"></div></section>
    <div class="grid2"><section class="panel"><h3>Capacity per charge</h3><div data-slot="cap"></div></section>
      <section class="panel"><h3>Resting cell spread</h3><div data-slot="spread"></div></section></div>
    <section class="panel"><h3>Cell drift <span class="more">each cell vs the pack median at rest, per day</span></h3><div data-slot="drift"></div></section>
    <section class="panel"><h3>Idle self-discharge <span class="more">per cell, vs the pack average</span></h3><div data-slot="sd"></div></section>`);
  slot(root, "head", head(b));
  slot(root, "pack", `<label class="row small">Pack capacity <input type="number" step="0.1" min="0.5" max="1000" data-hvpack value="${b.pack_ah ?? ""}" placeholder="e.g. 15"${canWrite() ? "" : " disabled"}> Ah
    <button type="button" class="small ghost" data-hv="pack" data-host="${esc(b.host)}"${canWrite() ? "" : " disabled"}>Save</button></label>
    <span class="muted small">Optional: cell capacity × cells in parallel (e.g. 5 Ah × 3P = 15 Ah). Used for cycle count and health %. Saved in VBMS Manager only.</span>`);
  const e = analysisFor(b);
  if (!e.data) { slot(root, "tiles", `<div class="muted">${e.err ? esc(e.err) : "Loading…"}</div>`); return; }
  const a = e.data, h = a.health;
  const kwh = (x) => (x == null ? "–" : x >= 1000 ? f2(x / 1000) + " kWh" : Math.round(x) + " Wh");
  slot(root, "tiles", [
    tile("Cycles", h.cycles != null ? f1(h.cycles) : "–", h.pack_ah ? `${f2(h.ah_in_total)} Ah ÷ ${f1(h.pack_ah)} Ah${h.pack_ah_source === "estimate" ? " (estimated)" : ""}` : "set the pack capacity"),
    tile("Capacity", h.capacity_ah != null ? `≈ ${f2(h.capacity_ah)} Ah` : "–", h.capacity_pct != null ? `${h.capacity_pct}% of ${f1(a.capacity.nominal_ah)} Ah` : "median of recent estimates"),
    tile("BMS SOH", h.soh != null ? Math.round(h.soh * 100) + "%" : "–", "as the BMS reports it"),
    tile("Energy in / out", `${kwh(h.wh_in_total)} / ${kwh(h.wh_out_total)}`, "BMS lifetime counters"),
    tile("Weakest cell", h.weakest ? "#" + h.weakest : "–", h.weakest_mv != null ? `${f1(h.weakest_mv)} mV vs median at rest` : "needs the detailed level"),
    tile("Resting spread", h.rest_spread.length ? f1(h.rest_spread[h.rest_spread.length - 1][1]) + " mV" : "–", h.rest_spread_change != null ? `${h.rest_spread_change > 0 ? "+" : ""}${h.rest_spread_change} mV over the period` : "latest day"),
    tile("Charges logged", String(h.charges_logged), h.charge_hours ? h.charge_hours + " h charging" : ""),
  ].join(""));
  slot(root, "flags", h.flags.length ? `<div class="banner"><b>Worth a look</b><ul>${h.flags.map((f) => `<li>${esc(f.text)}</li>`).join("")}</ul><span class="small muted">These are patterns in the logged data, not a diagnosis. Check with VESC Tool and a meter before acting on them.</span></div>`
    : `<p class="muted small">No cell stands out in the recorded data.</p>`);
  const cp = a.capacity.points;
  slot(root, "cap", cp.length ? `${spark(cp.map((p) => [p.t, p.ah]), { unit: " Ah", dec: 2 })}<p class="small">${cp.length} estimate${cp.length > 1 ? "s" : ""}, latest ≈ <b>${f2(cp[cp.length - 1].ah)} Ah</b> (${esc(when(cp[cp.length - 1].t))}).</p>`
    : `<p class="muted small">No estimates yet. One appears after a charge that started from a rested pack and was followed by 30 min of rest, with both rests inside the SOC curve and at least 15% apart.</p>`);
  const rs = h.rest_spread;
  slot(root, "spread", rs.length >= 2 ? `${spark(rs.map(([d, v]) => [new Date(d + "T12:00:00").getTime() / 1000, v]), { unit: " mV", dec: 1 })}<p class="small muted">Median spread with no current flowing, per day. Rising over weeks can mean a cell is drifting or balancing can't keep up.</p>`
    : `<p class="muted small">Needs a few days of recordings.</p>`);
  const dr = a.drift;
  slot(root, "drift", dr.available ? driftMap(dr, dr.flags.map((f) => f.cell)) : `<p class="muted small">${esc(dr.why)}</p>`);
  const sd = a.self_discharge;
  slot(root, "sd", sd.available ? `${cellBars(sd.relative, { flagged: sd.flags.map((f) => f.cell) })}
      <p class="small">From ${sd.events.length} idle stretch${sd.events.length > 1 ? "es" : ""} (latest ${esc(when(sd.events[sd.events.length - 1].from))}, ${sd.events[sd.events.length - 1].hours} h): pack average ${f2(sd.events[sd.events.length - 1].pack_mv_per_day)} mV/day.</p>
      <p class="muted small">While the BMS is awake it powers itself from the pack, so the pack figure includes that draw. The useful part is how each cell differs from the others.</p>`
    : `<p class="muted small">${esc(sd.why)}</p>`);
}

// ---------------------------------------------------------------- events

function boardOf(host) { return S.state && S.state.boards.find((x) => x.host === host); }

function openGraphs(host, a, b) {
  ui.view = "graphs"; save();
  zoom.set(host + "|main", [Math.floor(a), Math.ceil(b)]);
  hooks.rerender();
  window.scrollTo({ top: 0 });
}

document.addEventListener("click", async (e) => {
  const day = e.target.closest("[data-day]");
  if (day) {
    const t0 = new Date(day.dataset.day + "T00:00:00").getTime() / 1000;
    const host = day.dataset.host || (S.route && S.route.board) || (S.state.boards[0] || {}).host;
    return openGraphs(host, t0, Math.min(t0 + 86400, Date.now() / 1000));
  }
  const t = e.target.closest("[data-hv]");
  if (!t) return;
  const k = t.dataset.hv, host = t.dataset.host, v = t.dataset.v;
  if (k === "view") { ui.view = v; save(); return hooks.rerender(); }
  if (k === "metric") { ui.metric = v; save(); return hooks.rerender(); }
  if (k === "range") { ui.range = v; save(); zoom.delete(host + "|main"); return hooks.rerender(); }
  if (k === "days") { ui.days = Number(v); save(); return hooks.rerender(); }
  if (k === "zoomout") { zoom.delete(host + "|main"); return hooks.rerender(); }
  if (k === "prange") { ui.panels[t.dataset.id] = t.dataset.r; save(); zoom.delete(host + "|" + t.dataset.id); return hooks.rerender(); }
  if (k === "pzoom") { zoom.delete(host + "|" + t.dataset.id); return hooks.rerender(); }
  if (k === "session") return openGraphs(host, Number(t.dataset.a) - 300, Math.min(Date.now() / 1000, Number(t.dataset.b) + 300));
  if (k === "settings") return hooks.openRecording && hooks.openRecording();
  if (k === "pack") {
    const b = boardOf(host), inp = document.querySelector("[data-hvpack]");
    try {
      await api("/api/board/pack", { host, mac: b.mac, pack_ah: inp.value === "" ? null : Number(inp.value) });
      forget("an|" + host);
      await hooks.refresh();
    } catch (x) { fail(x); }
  }
});

document.addEventListener("change", (e) => {
  const s = e.target.closest("[data-hvsel]");
  if (!s) return;
  ui[s.dataset.hvsel] = s.value; save(); hooks.rerender();
});

document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr[data-hv], tr[data-day], g[data-day]")) { e.preventDefault(); e.target.dispatchEvent(new MouseEvent("click", { bubbles: true })); }
});

// After recording settings change, cached history may be at the wrong resolution.
export function resetHistoryCache() { cache.clear(); }

// SVG visuals. Every function returns markup; colours come from CSS tokens.

import { clamp, esc, f1, f3, V_HI, V_LO } from "./core.js";

const socColor = (s) => (s < 0.2 ? "var(--bad)" : s < 0.5 ? "var(--warn)" : "var(--ok)");

export function miniBattery(soc, state) {
  const s = soc == null ? 0 : clamp(soc, 0, 1);
  const fill = state === "online" ? socColor(s) : "var(--muted)";
  return `<svg class="minibat" viewBox="0 0 30 15" aria-hidden="true"><rect x=".75" y=".75" width="25.5" height="13.5" rx="3" class="bat-body"/><rect x="27" y="4.5" width="2.5" height="6" rx="1" class="bat-nub"/><rect x="2.5" y="2.5" width="${(22 * s).toFixed(1)}" height="10" rx="1.5" fill="${fill}"/></svg>`;
}

// Min/max cell on the 3.0–4.2 V scale with Charge Start / End marks.
// ch: {vc_charge_start, vc_charge_end}; edit: the marks can be dragged (drag.js); pending: they're a draft.
export function rangeGauge(min, max, ch = {}, { edit = false, pending = false } = {}) {
  const X0 = 16, X1 = 384, x = (v) => X0 + (X1 - X0) * clamp((v - V_LO) / (V_HI - V_LO), 0, 1);
  const ticks = [3.0, 3.3, 3.6, 3.9, 4.2].map((t) =>
    `<line x1="${x(t)}" x2="${x(t)}" y1="50" y2="56" class="g-tick"/><text x="${x(t)}" y="70" text-anchor="middle" class="g-lab">${t.toFixed(1)}</text>`).join("");
  const mark = (v, cls, lab, f) => v == null ? "" :
    `<g class="g-mark${edit ? " g-handle" : ""}${pending ? " pending" : ""}"${edit ? ` data-drag="${f}" tabindex="0" role="slider" aria-label="${lab}" aria-valuenow="${f3(v)}"` : ""}>
      ${edit ? `<rect x="${x(v) - 9}" y="8" width="18" height="44" class="g-hit"/><line x1="${x(v)}" x2="${x(v)}" y1="28" y2="50" class="g-${cls}l"/>` : ""}
      <path d="M${x(v)},28 l-5,-8 h10 z" class="g-${cls}"><title>${lab} ${f3(v)} V${edit ? " · drag to change" : ""}</title></path></g>`;
  let seg = "", label = "No data";
  if (min != null && max != null) {
    const a = x(min), b = Math.max(x(max), a + 3);
    label = `${f3(min)} to ${f3(max)} V`;
    seg = `<rect x="${a - 1.5}" y="34" width="${b - a + 3}" height="14" rx="4" class="g-seg"><title>${label}</title></rect>`;
  }
  return `<svg class="gauge${edit ? " editable" : ""}" viewBox="0 0 400 76" role="img" aria-label="Cell range ${label}"${edit ? ` data-dragbox data-axis="x" data-vb="400" data-p0="${X0}" data-p1="${X1}" data-v0="${V_LO}" data-v1="${V_HI}"` : ""}>
    <rect x="${X0}" y="36" width="${X1 - X0}" height="10" rx="5" class="g-track"/>
    ${ticks}${seg}${mark(ch.vc_charge_start, "start", "Charge start", "vc_charge_start")}${mark(ch.vc_charge_end, "end", "Charge end", "vc_charge_end")}
  </svg>`;
}

// Cell spread (max − min) against balance thresholds and the imbalance alert.
// edit: Balance Start / End can be dragged (drag.js; stored in V, drawn in mV).
export function spreadMeter(deltaMv, { balStart, balEnd, alert, edit = false, pending = false }) {
  const top = Math.max(alert * 1.25, (deltaMv || 0) * 1.1, (balStart || 0) * 1.5, 10);
  const X0 = 16, X1 = 384, x = (mv) => X0 + (X1 - X0) * clamp(mv / top, 0, 1);
  const d = deltaMv || 0;
  const cls = d > alert ? "bad" : balStart != null && d > balStart ? "warn" : "ok";
  const m = (mv, cls2, lab, up, f) => mv == null ? "" :
    `<g class="${edit && f ? "g-handle" : ""}${pending ? " pending" : ""}"${edit && f ? ` data-drag="${f}" tabindex="0" role="slider" aria-label="${lab}"` : ""}>${edit && f ? `<rect x="${x(mv) - 9}" y="${up ? 8 : 26}" width="18" height="44" class="g-hit"/>` : ""}<line x1="${x(mv)}" x2="${x(mv)}" y1="26" y2="56" class="sm-${cls2}"/><text x="${x(mv)}" y="${up ? 20 : 70}" text-anchor="middle" class="g-lab sm-${cls2}t">${lab}</text></g>`;
  return `<svg class="gauge${edit ? " editable" : ""}" viewBox="0 0 400 76" role="img" aria-label="Cell spread ${f1(d)} millivolts"${edit ? ` data-dragbox data-axis="x" data-vb="400" data-p0="${X0}" data-p1="${X1}" data-v0="0" data-v1="${top / 1000}"` : ""}>
    <rect x="${X0}" y="34" width="${X1 - X0}" height="14" rx="7" class="g-track"/>
    <rect x="${X0}" y="34" width="${Math.max(4, x(d) - X0)}" height="14" rx="7" class="sm-fill ${cls}"/>
    ${m(alert, "alert", `alert ${alert}`, false)}${m(balEnd, "bend", `stop ${Math.round(balEnd)}`, false, "vc_balance_end")}${m(balStart, "bstart", `balance ${Math.round(balStart)}`, true, "vc_balance_start")}
  </svg>`;
}

// Vertical thermometer. limits: [{v, label, kind: "max"|"min"}]
export function thermo(name, value, limits = [], { lo = -10, hi = 80 } = {}) {
  // Limits at the same temperature share one mark: "charge/balance 65°"
  const merged = [];
  for (const l of limits) {
    const m = merged.find((x) => Math.abs(x.v - l.v) < 0.5 && x.kind === l.kind);
    if (m) m.label += "/" + l.label; else merged.push({ ...l });
  }
  limits = merged;
  hi = Math.max(hi, ...limits.map((l) => l.v + 10), (value ?? 0) + 5);
  const Y0 = 150, Y1 = 14, y = (t) => Y0 - (Y0 - Y1) * clamp((t - lo) / (hi - lo), 0, 1);
  const maxes = limits.filter((l) => l.kind === "max").map((l) => l.v);
  const mins = limits.filter((l) => l.kind === "min").map((l) => l.v);
  const lim = maxes.length ? Math.min(...maxes) : null;
  let zone = "ok";
  if (value == null) zone = "none";
  else if (lim != null && value >= lim) zone = "bad";
  else if (lim != null && value >= lim - 5) zone = "warn";
  else if (mins.length && value <= Math.max(...mins)) zone = "cold";
  const fillTop = value == null ? Y0 : y(value);
  const marks = limits.map((l) => `<line x1="30" x2="46" y1="${y(l.v)}" y2="${y(l.v)}" class="th-mark ${l.kind}"/><text x="49" y="${y(l.v) + 3.5}" class="th-lab">${esc(l.label)} ${Math.round(l.v)}°</text>`).join("");
  return `<figure class="thermo ${zone}">
    <svg viewBox="0 0 150 186" role="img" aria-label="${esc(name)} ${f1(value)} degrees">
      <rect x="30" y="8" width="16" height="150" rx="8" class="th-tube"/>
      <circle cx="38" cy="166" r="14" class="th-bulb-bg"/>
      <rect x="34" y="${fillTop}" width="8" height="${Math.max(0, 162 - fillTop)}" rx="4" class="th-fill"/>
      <circle cx="38" cy="166" r="10" class="th-fill"/>
      ${marks}
    </svg>
    <figcaption><b>${f1(value)}<small> °C</small></b><span>${esc(name)}</span></figcaption>
  </figure>`;
}

// Line chart. series: [{name, cls, pts: [[t, v], ...]}]
export function trend(series, { decimals = 3, unit = "", height = 120 } = {}) {
  const all = series.flatMap((s) => s.pts);
  if (all.length < 2) return `<div class="muted small">Not enough data yet. The chart fills in over the next few minutes.</div>`;
  const t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
  let lo = Math.min(...all.map((p) => p[1])), hi = Math.max(...all.map((p) => p[1]));
  const minSpan = decimals >= 3 ? 0.01 : 2;
  if (hi - lo < minSpan) { const m = (hi + lo) / 2; lo = m - minSpan / 2; hi = m + minSpan / 2; }
  const W = 600, H = height, L = 52, R = 8, T = 8, B = 20;
  const x = (t) => L + (W - L - R) * ((t - t0) / Math.max(1, t1 - t0));
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const grid = [0, 0.5, 1].map((f) => { const v = lo + (hi - lo) * f; return `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="tr-grid"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" class="tr-lab">${v.toFixed(decimals)}${unit}</text>`; }).join("");
  const mins = Math.round((t1 - t0) / 60);
  const paths = series.map((s) => s.pts.length < 2 ? "" : `<path d="${s.pts.map((p, i) => (i ? "L" : "M") + x(p[0]).toFixed(1) + "," + y(p[1]).toFixed(1)).join("")}" class="tr-line ${s.cls || ""}"/>`).join("");
  return `<svg class="trend" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Trend over ${mins} minutes">${grid}${paths}
    <text x="${L}" y="${H - 4}" class="tr-lab">−${mins} min</text><text x="${W - R}" y="${H - 4}" text-anchor="end" class="tr-lab">now</text></svg>
    <div class="legend">${series.map((s) => `<span><i class="ln-solid ${s.cls || ""}"></i>${esc(s.name)}</span>`).join("")}</div>`;
}

export function curveChart(curve, maxEnd, hiPct) {
  const pts = curve.map(([p, v]) => [Number(p), Number(v)]).sort((a, b) => a[0] - b[0]);
  const W = 400, H = 160, L = 44, R = 20, T = 10, B = 24;
  const lo = Math.min(...pts.map((p) => p[1])) - 0.02, hi = Math.max(maxEnd, ...pts.map((p) => p[1])) + 0.02;
  const x = (p) => L + (W - L - R) * ((p - pts[0][0]) / (100 - pts[0][0]));
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const path = pts.map((p, i) => (i ? "L" : "M") + x(p[0]).toFixed(1) + "," + y(p[1]).toFixed(1)).join("");
  const dots = pts.map((p) => `<circle cx="${x(p[0])}" cy="${y(p[1])}" r="2.5" class="cv-dot"><title>${p[0]}% = ${f3(p[1])} V</title></circle>`).join("");
  const xl = pts.filter((p) => p[0] % 20 === 0 || p === pts[0]).map((p) => `<text x="${x(p[0])}" y="${H - 6}" text-anchor="middle" class="tr-lab">${p[0]}%</text>`).join("");
  // The charge-limit control covers 60-100%.
  const band = pts[0][0] < 60 ? `<rect x="${x(60)}" y="${T}" width="${x(100) - x(60)}" height="${H - T - B}" class="cv-band"><title>Charge-limit range (60–100%)</title></rect>` : "";
  const yl = [lo + 0.02, (lo + hi) / 2, hi - 0.02].map((v) => `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" class="tr-lab">${v.toFixed(2)}</text><line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="tr-grid"/>`).join("");
  const hl = hiPct != null ? `<line x1="${x(hiPct)}" x2="${x(hiPct)}" y1="${T}" y2="${H - B}" class="cv-hl"/>` : "";
  return `<svg class="curvechart" viewBox="0 0 ${W} ${H}" role="img" aria-label="SOC curve">${band}${yl}${hl}<path d="${path}" class="tr-line"/>${dots}${xl}</svg>`;
}

// ---------------------------------------------------------------- compact indicators

// SOC ring. The charge limit is a tick on the ring (tap: change it); the faint arc is the room left up to it.
export function socRing(soc, { limitPct = null, charging = false, host = "", act = true, label = "SOC" } = {}) {
  const s = soc == null || isNaN(soc) ? null : clamp(soc, 0, 1);
  const R = 50, C = 2 * Math.PI * R, pct = s == null ? "–" : Math.round(s * 100);
  const at = (p) => { const a = (p / 100) * 2 * Math.PI - Math.PI / 2; return [60 + Math.cos(a), 60 + Math.sin(a)]; };
  const pt = (p, r) => { const [cx, cy] = at(p); return [60 + (cx - 60) * r, 60 + (cy - 60) * r]; };
  const col = s == null ? "var(--line2)" : socColor(s);
  const h = host ? ` data-host="${esc(host)}"` : "";
  let room = "", tick = "";
  if (limitPct != null) {
    const lp = clamp(limitPct, 0, 100);
    if (s != null && lp > s * 100) {
      room = `<circle cx="60" cy="60" r="${R}" class="ring-room" stroke="${col}" stroke-dasharray="${(C * (lp / 100 - s)).toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${(-C * s).toFixed(2)}" transform="rotate(-90 60 60)"/>`;
    }
    const [x1, y1] = pt(lp, 38), [x2, y2] = pt(lp, 62), [hx, hy] = pt(lp, 50);
    const ttl = `Charge limit ${Math.round(lp)}%${act ? ", tap to change" : ""}`;
    tick = `<g class="ring-limit"${act ? ` data-act="limit"${h} role="button" tabindex="0" data-tip="${ttl}"` : ""} aria-label="${ttl}">
      <circle cx="${hx.toFixed(1)}" cy="${hy.toFixed(1)}" r="14" class="ring-hit"/>
      <line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" class="ring-tick-bg"/>
      <line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" class="ring-tick"/></g>`;
  }
  const fill = s == null ? "" : `<circle cx="60" cy="60" r="${R}" class="ring-fill${charging ? " charging" : ""}" stroke="${col}" stroke-dasharray="${(C * s).toFixed(2)} ${C.toFixed(2)}" transform="rotate(-90 60 60)"/>`;
  const body = `<circle cx="60" cy="60" r="${R}" class="ring-track"/>${room}${fill}
    <text x="60" y="66" text-anchor="middle" class="ring-pct">${pct}<tspan class="ring-unit">%</tspan></text>
    <text x="60" y="84" text-anchor="middle" class="ring-sub">${esc(label)}</text>`;
  return `<svg class="ring" viewBox="-6 -6 132 132" role="img" aria-label="${esc(label)} ${pct} percent">
    ${act ? `<g data-act="cells"${h} role="button" tabindex="0" class="ring-tap" data-tip="State of charge ${pct}% · tap for all cells">${body}</g>` : body}${tick}</svg>`;
}

export const ICON = {
  bolt: `<path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12z" class="i-fill"/>`,
  boltOff: `<path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12z" class="i-fill"/><path d="M3 3l18 18" class="i-slash"/>`,
  boltClock: `<path d="M11 2 3.5 12.5H9L8 20l4.2-5.6" class="i-fill"/><circle cx="17" cy="17" r="5" class="i-line"/><path d="M17 14.5V17l1.6 1.2" class="i-line"/>`,
  scale: `<path d="M12 4v16M6 20h12M5 7h14M8 7l-3 7h6zM16 7l-3 7h6z" class="i-line"/>`,
  delta: `<path d="M12 4 3.5 20h17z" class="i-line"/>`,
  therm: `<path d="M14 14.5V5a2 2 0 0 0-4 0v9.5a4 4 0 1 0 4 0z" class="i-line"/><path d="M12 10v7" class="i-line"/>`,
  flame: `<path d="M12 3c3 4 6 6.5 6 10.5a6 6 0 0 1-12 0c0-2.5 1.5-4 3-5.5 0 2 1 3 2 3.5 0-3-.5-5.5 1-8.5z" class="i-line"/>`,
  // tab icons
  gauge: `<path d="M4.5 17a8 8 0 1 1 15 0" class="i-line"/><path d="M12 13.5 15.5 9" class="i-line"/><circle cx="12" cy="14" r="1.6" class="i-fill"/>`,
  cells: `<rect x="3" y="7" width="16" height="10" rx="2" class="i-line"/><path d="M21 10.5v3M7 10v4M11 10v4M15 10v4" class="i-line"/>`,
  calendar: `<rect x="3.5" y="5" width="17" height="15" rx="2" class="i-line"/><path d="M3.5 10h17M8 3v4M16 3v4M8 14h2M14 14h2M8 17h2" class="i-line"/>`,
  chart: `<path d="M4 4v16h16" class="i-line"/><path d="M7.5 15l3.5-4 3 2.5L19 8" class="i-line"/>`,
  sliders: `<path d="M5 6h9M18 6h1M5 12h3M12 12h7M5 18h11M20 18h-1" class="i-line"/><circle cx="16" cy="6" r="2" class="i-line"/><circle cx="10" cy="12" r="2" class="i-line"/><circle cx="18" cy="18" r="2" class="i-line"/>`,
  chip: `<rect x="6" y="6" width="12" height="12" rx="2" class="i-line"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" class="i-line"/>`,
  clock: `<circle cx="12" cy="12" r="8.5" class="i-line"/><path d="M12 7.5V12l3 2" class="i-line"/>`,
};

export function icon(name) {
  return `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${ICON[name]}</svg>`;
}

// Small round-cornered icon button with a short value. tip explains it and what a tap does.
export function indicator(name, text, { act, host = "", cls = "", tip = "", disabled = false } = {}) {
  return `<button type="button" class="ind ${cls}" data-act="${act}" data-host="${esc(host)}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"${disabled ? " aria-disabled=\"true\"" : ""}>${icon(name)}${text !== "" && text != null ? `<span>${text}</span>` : ""}</button>`;
}

// All cells, condensed: bars on the fixed 3.0–4.2 V scale with the highest/lowest cell picked out,
// and a second row showing each cell's mV above the lowest (0 to Δ), which is where imbalance shows.
export function cellStrip(cells, { balancing = [], levels = [], tall = false } = {}) {
  if (!cells || !cells.length) return "";
  const n = cells.length, lo = Math.min(...cells), hi = Math.max(...cells);
  const iMax = cells.indexOf(hi), iMin = cells.indexOf(lo), dmv = (hi - lo) * 1000;
  const pct = (v) => clamp((v - V_LO) / (V_HI - V_LO), 0, 1) * 100;
  const cls = (i) => (i === iMax && dmv > 0 ? " hi" : i === iMin && dmv > 0 ? " lo" : "") + (levels[i] ? " " + levels[i] : "");
  const tip = (v, i) => `Cell ${i + 1}: ${v.toFixed(3)} V · ${Math.round((v - lo) * 1000)} mV above lowest${i === iMax && dmv > 0 ? " · highest" : i === iMin && dmv > 0 ? " · lowest" : ""}${balancing[i] ? " · balancing" : ""}`;
  const bars = cells.map((v, i) => `<i class="cs-c${cls(i)}" data-tip="${tip(v, i)}" tabindex="0"><b style="height:${pct(v).toFixed(1)}%"></b></i>`).join("");
  const dev = cells.map((v, i) => `<i class="cs-c${cls(i)}"><b style="height:${dmv > 0 ? Math.max(4, ((v - lo) * 1000 / dmv) * 100).toFixed(1) : 4}%"></b>${balancing[i] ? '<em class="bal-dot"></em>' : ""}</i>`).join("");
  const nums = cells.map((_, i) => `<span>${i === iMax && dmv > 0 ? "▲" : i === iMin && dmv > 0 ? "▼" : n <= 20 || i % 2 === 0 ? i + 1 : ""}</span>`).join("");
  return `<div class="cstrip${tall ? " tall" : ""}" style="--n:${n}">
    <div class="cs-head"><span class="cs-delta"><b>Δ ${f1(dmv)}</b> mV</span><span><i class="sw hisw"></i>▲ #${iMax + 1} ${f3(hi)} V</span><span><i class="sw losw"></i>▼ #${iMin + 1} ${f3(lo)} V</span></div>
    <div class="cs-row cs-v">${bars}</div>
    <div class="cs-cap muted">mV above lowest (0–${f1(dmv)})</div>
    <div class="cs-row cs-d">${dev}</div>
    <div class="cs-row cs-n">${nums}</div></div>`;
}

// Cell-sensor temperatures, one small bar each, with the average.
export function tempList(temps, { max = null } = {}) {
  if (!temps.length) return `<div class="muted small">No cell temperature sensors reported.</div>`;
  const lo = -10, hi = 80;
  const av = temps.reduce((a, t) => a + t.value, 0) / temps.length;
  return `<div class="tbars">${temps.map((t) => {
    const c = max != null && t.value >= max ? "bad" : max != null && t.value >= max - 5 ? "warn" : "ok";
    const w = clamp((t.value - lo) / (hi - lo), 0.02, 1) * 100;
    return `<div class="tbar ${c}"><span class="tb-name">${esc(t.name)}</span><span class="tb-track"><i style="width:${w.toFixed(1)}%"></i><u style="left:${(clamp((av - lo) / (hi - lo), 0, 1) * 100).toFixed(1)}%"></u></span><b>${f1(t.value)}°</b></div>`;
  }).join("")}<div class="muted small">Average ${f1(av)} °C (marked on each bar)${max != null ? ` · charging stops at ${Math.round(max)} °C` : ""}</div></div>`;
}

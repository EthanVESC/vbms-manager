// History charts (SVG strings, like viz.js). Each returns markup plus, for the time chart,
// the geometry the pointer layer in history.js needs to map x back to time.

import { clamp, esc } from "./core.js";

const DAY = 86400;
const pad2 = (n) => String(n).padStart(2, "0");
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// 12-hour clock: "9:05 PM". short: "9 PM" on the hour (axis ticks).
export function clock(t, short = false) {
  const d = new Date(t * 1000), h = d.getHours(), m = d.getMinutes();
  return `${h % 12 || 12}${short && !m ? "" : ":" + pad2(m)} ${h < 12 ? "AM" : "PM"}`;
}
export function dayLabel(t) { const d = new Date(t * 1000); return `${WD[d.getDay()]} ${MO[d.getMonth()]} ${d.getDate()}`; }

// "2:05 PM" today, "Tue 2:05 PM" this week, "Oct 12 2:05 PM" before that.
export function when(t) {
  const d = new Date(t * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return clock(t);
  if (Date.now() / 1000 - t < 6 * DAY) return `${WD[d.getDay()]} ${clock(t)}`;
  return `${MO[d.getMonth()]} ${d.getDate()} ${clock(t)}`;
}

export function dur(s) {
  if (s == null) return "–";
  s = Math.round(s);
  if (s < 60) return s + "s";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${pad2(m)}m` : `${m}m`;
}

function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((s) => s >= raw) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(+v.toFixed(10));
  return { ticks: out, step };
}

const T_STEPS = [60, 300, 900, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];

function timeTicks(t0, t1, maxN) {
  const step = T_STEPS.find((s) => (t1 - t0) / s <= maxN) || 30 * DAY;
  const tz = new Date(t0 * 1000).getTimezoneOffset() * 60;
  const out = [];
  for (let t = Math.ceil((t0 - tz) / step) * step + tz; t <= t1; t += step) {
    const d = new Date(t * 1000);
    const midnight = d.getHours() === 0 && d.getMinutes() === 0;
    const lab = step >= DAY ? `${MO[d.getMonth()]} ${d.getDate()}` : midnight ? WD[d.getDay()] : clock(t, true);
    out.push({ t, lab, strong: midnight });
  }
  return out;
}

// Line chart of downsampled history: avg line, min/max band, gaps left open, charge sessions shaded.
// data: /api/history reply. series get a class from slotOf(name, index) (fixed per entity).
export function historyChart(data, { width = 640, height = 210, sessions = [], mono = false, slotOf = (_, i) => i } = {}) {
  const series = data.series.filter((s) => s.pts.length);
  if (!series.length) return { html: `<div class="hc-empty muted small">Nothing recorded in this range yet. History fills in while VBMS Manager is connected to the board.</div>`, geom: null };
  const t0 = data.t0, t1 = data.t1, band = !mono && series.length <= 2;
  let lo = Infinity, hi = -Infinity;
  for (const s of series) for (const p of s.pts) { lo = Math.min(lo, band ? p[2] : p[1]); hi = Math.max(hi, band ? p[3] : p[1]); }
  const zero = ["power", "current"].includes(data.metric);
  if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  const minSpan = data.decimals >= 3 ? 0.01 : data.decimals === 2 ? 0.1 : 1;
  if (hi - lo < minSpan) { const m = (hi + lo) / 2; lo = m - minSpan / 2; hi = m + minSpan / 2; }
  const yt = niceTicks(lo, hi, 4);
  lo = Math.min(lo, yt.ticks[0] ?? lo); hi = Math.max(hi, yt.ticks[yt.ticks.length - 1] ?? hi);
  const W = Math.max(280, Math.round(width)), H = height, L = 50, R = 10, T = 10, B = 24;
  const x = (t) => L + (W - L - R) * ((t - t0) / Math.max(1, t1 - t0));
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const dec = data.decimals, unit = data.unit === "°C" ? "°" : "";
  const fmtY = (v) => (yt.step < 1 ? v.toFixed(Math.max(dec, Math.ceil(-Math.log10(yt.step)))) : String(Math.round(v))) + unit;
  const grid = yt.ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${zero && v === 0 ? "hc-zero" : "hc-grid"}"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" class="hc-lab">${fmtY(v)}</text>`).join("");
  const xt = timeTicks(t0, t1, Math.floor((W - L - R) / 80)).map((k) => `<line x1="${x(k.t).toFixed(1)}" x2="${x(k.t).toFixed(1)}" y1="${H - B}" y2="${H - B + 4}" class="hc-axis"/><text x="${x(k.t).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="hc-lab${k.strong ? " strong" : ""}">${esc(k.lab)}</text>`).join("");
  const sess = sessions.filter((s) => s.end > t0 && s.start < t1).map((s) => {
    const a = x(Math.max(t0, s.start)), b = x(Math.min(t1, s.end));
    return `<rect x="${a.toFixed(1)}" y="${T}" width="${Math.max(1.5, b - a).toFixed(1)}" height="${H - T - B}" class="hc-sess"/>`;
  }).join("");
  const segs = (pts) => {
    const out = [];
    let cur = [];
    for (let i = 0; i < pts.length; i++) {
      if (i && pts[i][0] - pts[i - 1][0] > data.gap) { out.push(cur); cur = []; }
      cur.push(pts[i]);
    }
    if (cur.length) out.push(cur);
    return out;
  };
  let bands = "", lines = "";
  series.forEach((s, i) => {
    const cls = mono ? "hc-mono" : "s" + (slotOf(s.name, i) % 8);
    for (const seg of segs(s.pts)) {
      if (band && seg.length > 1 && seg.some((p) => p[3] - p[2] > 1e-9)) {
        const up = seg.map((p) => `${x(p[0]).toFixed(1)},${y(p[3]).toFixed(1)}`), dn = seg.slice().reverse().map((p) => `${x(p[0]).toFixed(1)},${y(p[2]).toFixed(1)}`);
        bands += `<path d="M${up.join("L")}L${dn.join("L")}Z" class="hc-band ${cls}"/>`;
      }
      lines += seg.length === 1
        ? `<circle cx="${x(seg[0][0]).toFixed(1)}" cy="${y(seg[0][1]).toFixed(1)}" r="2" class="hc-dot ${cls}"/>`
        : `<path d="M${seg.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("L")}" class="hc-line ${cls}"/>`;
    }
  });
  const html = `<svg class="hchart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(data.metric)} history">
    ${sess}${grid}<line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" class="hc-axis"/>${xt}${bands}${lines}</svg>`;
  return { html, geom: { t0, t1, L, R, T, B, W, H, lo, hi } };
}

// Nearest point of each series to time t.
export function pointsAt(data, t) {
  return data.series.filter((s) => s.pts.length).map((s, i) => {
    const p = s.pts;
    let a = 0, b = p.length - 1;
    while (b - a > 1) { const m = (a + b) >> 1; if (p[m][0] < t) a = m; else b = m; }
    const q = Math.abs(p[a][0] - t) <= Math.abs(p[b][0] - t) ? p[a] : p[b];
    return { name: s.name, i, p: Math.abs(q[0] - t) <= Math.max(data.gap / 2, data.w * 1.5) ? q : null };
  });
}

// Daily energy: Wh into the pack above the line, Wh out below it.
export function dayBars(days, { width = 640, height = 170 } = {}) {
  if (!days.length) return `<div class="muted small">No days recorded yet.</div>`;
  const W = Math.max(280, Math.round(width)), H = height, L = 50, R = 10, T = 8, B = 22;
  const top = Math.max(1, ...days.map((d) => d.wh_in)), bot = Math.max(1, ...days.map((d) => d.wh_out));
  const yt = niceTicks(-bot, top, 4);
  const lo = Math.min(-bot, yt.ticks[0]), hi = Math.max(top, yt.ticks[yt.ticks.length - 1]);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const n = days.length, bw = (W - L - R) / n, gap = Math.min(2, bw * 0.2);
  const grid = yt.ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "hc-zero" : "hc-grid"}"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" class="hc-lab">${v >= 1000 || v <= -1000 ? (Math.abs(v) / 1000).toFixed(1) + "k" : Math.abs(Math.round(v))}</text>`).join("");
  const every = Math.ceil(n / Math.max(1, Math.floor((W - L - R) / 56)));
  const bars = days.map((d, i) => {
    const x0 = L + i * bw + gap / 2, w = Math.max(1, bw - gap), z = y(0);
    const hIn = z - y(d.wh_in), hOut = y(-d.wh_out) - z;
    const tip = `${d.day}: ${Math.round(d.wh_in)} Wh in, ${Math.round(d.wh_out)} Wh out · charging ${dur(d.chg_s)}${d.low_coverage ? " · partly recorded" : ""}`;
    const dt = new Date(d.day + "T12:00:00");
    const lab = i % every === 0 ? `<text x="${(x0 + w / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="hc-lab">${MO[dt.getMonth()]} ${dt.getDate()}</text>` : "";
    return `<g class="db${d.low_coverage ? " partial" : ""}" data-day="${esc(d.day)}" data-tip="${esc(tip)}" tabindex="0" role="button" aria-label="${esc(tip)}">
      <rect x="${x0.toFixed(1)}" y="${T}" width="${w.toFixed(1)}" height="${H - T - B}" class="db-hit"/>
      ${hIn > 0.5 ? `<rect x="${x0.toFixed(1)}" y="${(z - hIn).toFixed(1)}" width="${w.toFixed(1)}" height="${hIn.toFixed(1)}" rx="${Math.min(3, w / 3).toFixed(1)}" class="db-in"/>` : ""}
      ${hOut > 0.5 ? `<rect x="${x0.toFixed(1)}" y="${(z + 1).toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(0, hOut - 1).toFixed(1)}" rx="${Math.min(3, w / 3).toFixed(1)}" class="db-out"/>` : ""}${lab}</g>`;
  }).join("");
  return `<svg class="hchart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Energy per day">${grid}${bars}</svg>
    <div class="legend"><span><i class="sw dbsw-in"></i>Wh into the pack (charging)</span><span><i class="sw dbsw-out"></i>Wh out of the pack</span><span class="muted">tap a day to see its graphs</span></div>`;
}

// Cells x days heatmap of each cell's resting offset from the pack median (mV). Diverging, gray at 0.
export function driftMap(dr, flagged = []) {
  const days = dr.days, off = dr.offsets, n = dr.ncells;
  const abs = off.flat().filter((v) => v != null).map(Math.abs).sort((a, b) => a - b);
  const lim = Math.max(3, abs[Math.floor(abs.length * 0.95)] || 3);
  const col = (v) => {
    if (v == null) return "var(--panel2)";
    const k = clamp(v / lim, -1, 1), pct = Math.round(Math.abs(k) * 100);
    return `color-mix(in oklab, ${k < 0 ? "var(--div-lo)" : "var(--div-hi)"} ${pct}%, var(--div-mid))`;
  };
  const cells = [];
  for (let c = 0; c < n; c++) {
    const flag = flagged.includes(c + 1);
    cells.push(`<div class="dm-row${flag ? " flag" : ""}"><span class="dm-lab">${c + 1}</span>${days.map((d, j) => {
      const v = off[j][c];
      return `<i style="background:${col(v)}" data-tip="${esc(`Cell ${c + 1}, ${d}: ${v == null ? "no rest data" : (v > 0 ? "+" : "") + v.toFixed(1) + " mV vs pack median at rest"}`)}"></i>`;
    }).join("")}</div>`);
  }
  return `<div class="driftmap" style="--d:${days.length}">${cells.join("")}
    <div class="dm-row dm-axis"><span class="dm-lab"></span><b>${esc(days[0])}</b><b>${esc(days[days.length - 1])}</b></div></div>
    <div class="legend"><span><i class="sw" style="background:var(--div-lo)"></i>below median (−${lim.toFixed(0)} mV)</span><span><i class="sw" style="background:var(--div-mid)"></i>at median</span><span><i class="sw" style="background:var(--div-hi)"></i>above (+${lim.toFixed(0)} mV)</span></div>`;
}

// Per-cell diverging bars around 0 (self-discharge relative to the pack, mV/day).
export function cellBars(vals, { unit = "mV/day", flagged = [] } = {}) {
  const lim = Math.max(0.5, ...vals.map(Math.abs));
  return `<div class="cbars" style="--n:${vals.length}">${vals.map((v, k) => {
    const h = (Math.abs(v) / lim) * 50;
    return `<i class="${flagged.includes(k + 1) ? "flag" : ""}" data-tip="${esc(`Cell ${k + 1}: ${(v > 0 ? "+" : "") + v.toFixed(2)} ${unit} vs the pack average`)}"><b style="${v >= 0 ? "bottom:50%" : "top:50%"};height:${h.toFixed(1)}%" class="${v >= 0 ? "up" : "dn"}"></b></i>`;
  }).join("")}</div><div class="cbars-n" style="--n:${vals.length}">${vals.map((_, k) => `<span>${vals.length <= 20 || k % 2 === 0 ? k + 1 : ""}</span>`).join("")}</div>`;
}

// Small trend of single values over time (capacity estimates, rest spread).
export function spark(pts, { width = 260, height = 56, unit = "", dec = 1 } = {}) {
  if (pts.length < 2) return "";
  const W = width, H = height, P = 6;
  const ts = pts.map((p) => p[0]), vs = pts.map((p) => p[1]);
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const x = (t) => P + (W - 2 * P) * ((t - t0) / Math.max(1, t1 - t0)), y = (v) => P + (H - 2 * P) * (1 - (v - lo) / (hi - lo));
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="trend from ${vs[0].toFixed(dec)} to ${vs[vs.length - 1].toFixed(dec)}${unit}">
    <path d="M${pts.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("L")}" class="hc-line s0"/>
    ${pts.map((p) => `<circle cx="${x(p[0]).toFixed(1)}" cy="${y(p[1]).toFixed(1)}" r="3" class="sp-dot"><title>${p[1].toFixed(dec)}${unit}</title></circle>`).join("")}</svg>`;
}

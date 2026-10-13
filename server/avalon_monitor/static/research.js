// Research results pages: renders the latest document a project pushed with `avm results push`
// (schema "<name>-results/<n>") and per-metric history across pushes. Everything is rendered as
// text/SVG from the pushed document only - nothing is ever fetched from the project itself.
import { $, h, fmt, state, api, on, navigate, toast } from "./core.js?v=1.5.2";

const NS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const c of kids.flat()) if (c != null) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  return el;
}
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const fv = (v) => (num(v) == null ? "—" : Math.abs(v) < 10 ? v.toFixed(3) : v.toPrecision(4));
const fci = (ci) => (Array.isArray(ci) && num(ci[0]) != null && num(ci[1]) != null ? `[${fv(ci[0])}, ${fv(ci[1])}]` : null);
const clip = (t, n) => { t = String(t ?? ""); if (t.length <= n) return t; const a = Math.ceil((n - 1) / 2), b = Math.floor((n - 1) / 2); return t.slice(0, a) + "…" + (b ? t.slice(-b) : ""); };
// Categorical order (validated against both surfaces); identity is always also carried by a text label.
const SERIES = ["--c-blue", "--c-orange", "--c-aqua", "--c-violet", "--c-magenta", "--c-yellow"];

// ------------------------------------------------------------- tooltip
let tipEl;
function tip(el, text) {
  const show = (e) => {
    if (!tipEl) { tipEl = h("div", { class: "viz-tip" }); document.body.append(tipEl); }
    const t = typeof text === "function" ? text(e) : text;
    if (!t) { tipEl.style.display = "none"; return; }
    tipEl.textContent = t; tipEl.style.display = "block";
    const x = Math.min(e.clientX + 14, window.innerWidth - tipEl.offsetWidth - 8);
    const y = e.clientY + 16 + tipEl.offsetHeight > window.innerHeight ? e.clientY - tipEl.offsetHeight - 10 : e.clientY + 16;
    tipEl.style.left = x + "px"; tipEl.style.top = y + "px";
  };
  el.addEventListener("mousemove", show);
  el.addEventListener("mouseleave", () => { if (tipEl) tipEl.style.display = "none"; });
}

function domainOf(values, preferUnit) {
  const vs = values.filter((v) => num(v) != null);
  if (preferUnit || (vs.length && vs.every((v) => v >= 0 && v <= 1))) return [0, 1];
  if (!vs.length) return [0, 1];
  let lo = Math.min(0, ...vs), hi = Math.max(...vs);
  if (hi === lo) hi = lo + 1;
  return [lo, hi + (hi - lo) * 0.04];
}
function ticksFor([lo, hi]) {
  if (lo === 0 && hi === 1) return [0, 0.25, 0.5, 0.75, 1];
  const raw = (hi - lo) / 4, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let t = Math.ceil(lo / step - 1e-9) * step; t <= hi + step * 1e-9; t += step) out.push(Math.abs(t) < step * 1e-9 ? 0 : +t.toPrecision(12));
  return out;
}

// A figure redraws itself whenever its card changes width (phone <-> desktop, sidebar toggles).
function responsive(draw) {
  const box = h("div", { class: "viz" });
  let w = 0;
  let raf = 0;
  const ro = new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => { const nw = Math.floor(box.clientWidth); if (nw && nw !== w) { w = nw; box.replaceChildren(draw(w)); } });
  });
  ro.observe(box);
  return box;
}

// ------------------------------------------------------------ headline
function intervalPlot(row, width, scale) {
  const H = 28, pad = 8, [lo, hi] = scale;
  const x = (v) => pad + ((Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * (width - pad * 2);
  const g = s("svg", { width, height: H, class: "ivl" });
  for (const t of ticksFor(scale)) g.append(s("line", { x1: x(t), x2: x(t), y1: 6, y2: H - 6, class: "grid" }));
  g.append(s("line", { x1: x(lo), x2: x(hi), y1: H / 2, y2: H / 2, class: "grid" }));
  if (num(row.chance) != null) g.append(s("line", { x1: x(row.chance), x2: x(row.chance), y1: 5, y2: H - 5, class: "chance" }));
  const cmp = row.comparator && num(row.comparator.value) != null ? row.comparator : null;
  if (cmp) g.append(s("rect", { x: x(cmp.value) - 1.5, y: 4, width: 3, height: H - 8, rx: 1.5, class: "cmp" }));
  const ci = Array.isArray(row.ci) && num(row.ci[0]) != null && num(row.ci[1]) != null ? row.ci : null;
  if (ci) {
    g.append(s("line", { x1: x(ci[0]), x2: x(ci[1]), y1: H / 2, y2: H / 2, class: "whisk" }));
    for (const c of ci) g.append(s("line", { x1: x(c), x2: x(c), y1: H / 2 - 4, y2: H / 2 + 4, class: "whisk" }));
  }
  if (num(row.value) != null) g.append(s("circle", { cx: x(row.value), cy: H / 2, r: 5, class: "pt" }));
  tip(g, () => [
    `${row.result || row.id}: ${fv(row.value)}${ci ? "  95% CI " + fci(ci) : "  (no interval recorded)"}`,
    num(row.chance) != null ? `chance ${fv(row.chance)}` : null,
    cmp ? `${cmp.label || "comparator"} ${fv(cmp.value)}` : null,
  ].filter(Boolean).join("\n"));
  return g;
}

function scaleAxis(width, scale) {
  const pad = 8, [lo, hi] = scale;
  const x = (v) => pad + ((v - lo) / (hi - lo)) * (width - pad * 2);
  return s("svg", { width, height: 14, class: "ivl-axis" }, ticksFor(scale).map((t, i, a) =>
    s("text", { x: x(t), y: 11, "text-anchor": i === 0 ? "start" : i === a.length - 1 ? "end" : "middle" }, String(+t.toFixed(2)))));
}

function sparkline(points) {
  const W = 96, H = 26, pad = 4;
  const pts = points.filter((p) => num(p.value) != null);
  const g = s("svg", { width: W, height: H, class: "spark" });
  if (!pts.length) return g;
  const vs = pts.map((p) => p.value);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (hi - lo < 1e-9) { lo -= 0.01; hi += 0.01; }
  const x = (i) => (pts.length === 1 ? W - pad : pad + (i / (pts.length - 1)) * (W - pad * 2));
  const y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - pad * 2);
  if (pts.length > 1) g.append(s("polyline", { points: pts.map((p, i) => `${x(i)},${y(p.value)}`).join(" "), class: "line" }));
  const end = s("circle", { cx: x(pts.length - 1), cy: y(vs[vs.length - 1]), r: 3, class: "pt" });
  const hover = s("circle", { r: 3.5, class: "pt hover", style: "display:none" });
  g.append(end, hover, s("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent" }));
  tip(g, (e) => {
    const r = g.getBoundingClientRect();
    const i = pts.length === 1 ? 0 : Math.max(0, Math.min(pts.length - 1, Math.round(((e.clientX - r.left - pad) / (W - pad * 2)) * (pts.length - 1))));
    hover.setAttribute("cx", x(i)); hover.setAttribute("cy", y(pts[i].value)); hover.style.display = "";
    const p = pts[i];
    return `${fv(p.value)}${fci(p.ci) ? " " + fci(p.ci) : ""}\npush #${p.push} · ${fmt.when(p.received)}${p.commit ? " · " + p.commit : ""}`;
  });
  g.addEventListener("mouseleave", () => { hover.style.display = "none"; });
  return g;
}

function headlineTable(doc, history, viewing) {
  const rows = (doc.headline || []).filter((r) => r && typeof r === "object");
  if (!rows.length) return null;
  // per-id history up to (and including) the push being viewed
  const upto = history.filter((p) => p.id <= viewing);
  const series = new Map();
  for (const p of upto) for (const hl of p.headline || []) {
    if (!series.has(hl.id)) series.set(hl.id, []);
    series.get(hl.id).push({ ...hl, push: p.id, received: p.received, commit: p.commit });
  }
  const IW = 240;
  const tbody = h("tbody");
  let group = null;
  for (const r of rows) {
    if ((r.group || "") !== group) {
      group = r.group || "";
      if (group) tbody.append(h("tr", { class: "grp" }, h("td", { colspan: 6 }, group)));
    }
    const scale = Array.isArray(r.scale) && num(r.scale[0]) != null && num(r.scale[1]) != null && r.scale[1] > r.scale[0] ? r.scale : [0, 1];
    const hist = series.get(r.id) || [];
    const prev = hist.length > 1 ? hist[hist.length - 2].value : null;
    const delta = num(prev) != null && num(r.value) != null && Math.abs(r.value - prev) >= 0.0005 ? r.value - prev : null;
    tbody.append(h("tr", {},
      h("td", { class: "res" }, h("div", { class: "res-name" }, r.result || r.id), h("div", { class: "res-sub" }, [r.metric, r.compliance].filter(Boolean).join(" · "))),
      h("td", { class: "r num" }, r.n == null ? "—" : String(r.n)),
      h("td", { class: "r num val" }, h("b", {}, fv(r.value)), h("div", { class: "res-sub" }, fci(r.ci) || "no CI"),
        delta != null ? h("div", { class: "delta " + (delta > 0 ? "up" : "down") }, `${delta > 0 ? "▲ +" : "▼ "}${fv(delta)} vs previous`) : null),
      h("td", { class: "plot" }, intervalPlot(r, IW, scale)),
      h("td", { class: "r num muted" }, r.comparator && num(r.comparator.value) != null ? h("span", { title: r.comparator.label || "" }, fv(r.comparator.value)) : "—"),
      h("td", { class: "trend" }, hist.length ? sparkline(hist) : null, h("div", { class: "res-sub" }, `${hist.length} push${hist.length === 1 ? "" : "es"}`)),
    ));
  }
  const legend = h("div", { class: "viz-legend" },
    h("span", {}, h("i", { class: "lg-pt" }), "result · 95% CI"),
    h("span", {}, h("i", { class: "lg-chance" }), "chance"),
    h("span", {}, h("i", { class: "lg-cmp" }), "comparator"));
  return h("div", { class: "card table-card research-headline" },
    h("div", { class: "fig-head" }, h("h3", {}, "Headline results"), legend),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Result"), h("th", { class: "r" }, "n"), h("th", { class: "r" }, "Value"),
        h("th", { class: "plot" }, scaleAxis(IW, [0, 1])), h("th", { class: "r" }, "Comparator"), h("th", {}, "Trend"))),
      tbody)));
}

// ---------------------------------------------------------- programmes
const STATUS_CLASS = { running: "run", closed: "", fail: "bad", failed: "bad", partial: "warn", reference: "outline", registered: "outline" };
function programmes(doc) {
  const rows = (doc.programmes || []).filter((r) => r && typeof r === "object");
  if (!rows.length) return null;
  const counts = {};
  for (const p of rows) counts[p.status] = (counts[p.status] || 0) + 1;
  const list = h("div", { class: "prog-list" });
  for (const p of rows) {
    list.append(h("div", { class: "prog-row" },
      h("span", { class: "chip " + (STATUS_CLASS[String(p.status || "").toLowerCase()] ?? "outline") }, p.status_text || p.status || "?"),
      h("div", { class: "prog-body" },
        h("div", { class: "res-name" }, p.title || p.id),
        p.key_result ? h("div", { class: "prog-key" }, p.key_result) : null,
        h("div", { class: "res-sub" }, [p.registration, p.updated ? "updated " + p.updated : null, p.eta ? "ETA " + p.eta : null].filter(Boolean).join(" · "))),
    ));
  }
  return h("div", { class: "card table-card" },
    h("div", { class: "fig-head" }, h("h3", {}, "Programmes"),
      h("div", { class: "viz-legend" }, Object.entries(counts).map(([k, n]) => h("span", { class: "chip " + (STATUS_CLASS[k] ?? "outline") }, `${n} ${k}`)))),
    list);
}

// ---------------------------------------------------------------- bars
function barFigure(fig) {
  const rows = (fig.rows || []).filter((r) => r && typeof r === "object");
  const dom = Array.isArray(fig.domain) && num(fig.domain[0]) != null && num(fig.domain[1]) != null && fig.domain[1] > fig.domain[0]
    ? fig.domain : domainOf(rows.flatMap((r) => [r.value, ...(Array.isArray(r.ci) ? r.ci : [])]), fig.unit === "rate");
  const draw = (W) => {
    const narrow = W < 480, BH = 12, top = 4, axisH = 18, RH = narrow ? 34 : 22, off = narrow ? 7 : 0;
    const labW = narrow ? 0 : Math.min(250, Math.max(110, W * 0.36)), valW = 54;
    const x0 = narrow ? 2 : labW + 8, x1 = W - valW;
    const x = (v) => x0 + ((Math.max(dom[0], Math.min(dom[1], v)) - dom[0]) / (dom[1] - dom[0])) * (x1 - x0);
    const H = top + rows.length * RH + axisH;
    const g = s("svg", { width: W, height: H, class: "bars" });
    const band = Array.isArray(fig.band) && num(fig.band[0]) != null && num(fig.band[1]) != null && fig.band[1] > fig.band[0] ? fig.band : null;
    if (band) {
      const bx = s("rect", { x: x(band[0]), y: top, width: Math.max(1, x(band[1]) - x(band[0])), height: H - axisH - top + 2, class: "band" });
      tip(bx, `${fig.band_label || "band"}: ${fv(band[0])} to ${fv(band[1])}`);
      g.append(bx);
    }
    for (const t of ticksFor(dom)) {
      g.append(s("line", { x1: x(t), x2: x(t), y1: top, y2: H - axisH + 2, class: t === 0 && dom[0] < 0 ? "zero" : "grid" }));
      g.append(s("text", { x: x(t), y: H - 4, "text-anchor": "middle", class: "tick" }, String(+t.toFixed(3))));
    }
    const maxChars = Math.floor((narrow ? W - valW : labW) / 6.4);
    rows.forEach((r, i) => {
      const yc = top + i * RH + RH / 2 + off;
      const v = num(r.value);
      g.append(narrow ? s("text", { x: x0, y: yc - 10, class: "lab" }, clip(r.label, maxChars))
                      : s("text", { x: labW, y: yc + 4, "text-anchor": "end", class: "lab" }, clip(r.label, maxChars)));
      if (v != null) {
        // bars grow from 0 (or the domain edge nearest it) in either direction:
        // square at the baseline, 4px rounded data-end
        const xa = x(Math.min(dom[1], Math.max(dom[0], 0))), xb = x(v), wbar = Math.abs(xb - xa), rr = Math.min(4, wbar / 2, BH / 2), dir = xb >= xa ? 1 : -1;
        if (wbar > 0.5) g.append(s("path", { class: "bar", d: `M${xa},${yc - BH / 2}h${dir * (wbar - rr)}a${rr},${rr} 0 0 ${dir > 0 ? 1 : 0} ${dir * rr},${rr}v${BH - 2 * rr}a${rr},${rr} 0 0 ${dir > 0 ? 1 : 0} ${-dir * rr},${rr}h${-dir * (wbar - rr)}z` }));
        if (Array.isArray(r.ci) && num(r.ci[0]) != null && num(r.ci[1]) != null) {
          g.append(s("line", { x1: x(r.ci[0]), x2: x(r.ci[1]), y1: yc, y2: yc, class: "whisk-bar" }));
          for (const c of r.ci) g.append(s("line", { x1: x(c), x2: x(c), y1: yc - 4, y2: yc + 4, class: "whisk-bar" }));
        }
      }
      g.append(s("text", { x: W - 2, y: yc + 4, "text-anchor": "end", class: "val" }, fv(v)));
      const hit = s("rect", { x: 0, y: yc - RH / 2 - off, width: W, height: RH, fill: "transparent" });
      const extra = Object.entries(r).filter(([k, val]) => !["label", "value", "ci"].includes(k) && (typeof val === "number" || typeof val === "string")).map(([k, val]) => `${k} ${val}`);
      tip(hit, [`${r.label}: ${fv(v)}`, fci(r.ci) ? "95% CI " + fci(r.ci) : null, ...extra].filter(Boolean).join("\n"));
      g.append(hit);
    });
    return g;
  };
  const better = fig.better === "lower" ? "lower is better" : fig.better === "higher" ? "higher is better" : null;
  return h("div", { class: "card table-card fig" },
    h("div", { class: "fig-head" }, h("h3", {}, fig.title || fig.id), h("span", { class: "muted fig-cap" }, [fig.unit, better].filter(Boolean).join(" · ")),
      Array.isArray(fig.band) ? h("span", { class: "viz-legend" }, h("span", {}, h("i", { class: "lg-band" }), fig.band_label || "band")) : null),
    rows.length ? responsive(draw) : h("div", { class: "muted" }, "no rows"));
}

// ------------------------------------------------------- distributions
const quant = (sorted, q) => { const i = (sorted.length - 1) * q, a = Math.floor(i), b = Math.ceil(i); return sorted[a] + (sorted[b] - sorted[a]) * (i - a); };
function stats(values) {
  const v = values.filter((x) => num(x) != null).sort((a, b) => a - b);
  if (!v.length) return null;
  return { n: v.length, min: v[0], q1: quant(v, 0.25), med: quant(v, 0.5), q3: quant(v, 0.75), max: v[v.length - 1], mean: v.reduce((a, b) => a + b, 0) / v.length, v };
}

function stripFigure(fig) {
  const ser = (fig.series || []).filter((x) => x && Array.isArray(x.values)).map((x) => ({ ...x, st: stats(x.values) }));
  const dom = domainOf(ser.flatMap((x) => x.values), false);
  const draw = (W) => {
    const narrow = W < 480, top = 4, axisH = 18, RH = narrow ? 40 : 26, off = narrow ? 7 : 0;
    const labW = narrow ? 0 : Math.min(250, Math.max(110, W * 0.36)), valW = 54;
    const x0 = narrow ? 4 : labW + 8, x1 = W - valW;
    const x = (v) => x0 + ((v - dom[0]) / (dom[1] - dom[0])) * (x1 - x0);
    const H = top + ser.length * RH + axisH;
    const g = s("svg", { width: W, height: H, class: "dist" });
    for (const t of ticksFor(dom)) {
      g.append(s("line", { x1: x(t), x2: x(t), y1: top, y2: H - axisH + 2, class: "grid" }));
      g.append(s("text", { x: x(t), y: H - 4, "text-anchor": "middle", class: "tick" }, String(+t.toFixed(2))));
    }
    const maxChars = Math.floor((narrow ? W - valW : labW) / 6.4);
    ser.forEach((sr, i) => {
      const yc = top + i * RH + RH / 2 + off, st = sr.st;
      g.append(narrow ? s("text", { x: x0, y: yc - 12, class: "lab" }, clip(sr.label, maxChars))
                      : s("text", { x: labW, y: yc + 4, "text-anchor": "end", class: "lab" }, clip(sr.label, maxChars)));
      if (!st) return;
      g.append(s("line", { x1: x(st.min), x2: x(st.max), y1: yc, y2: yc, class: "whisk thin" }));
      g.append(s("rect", { x: x(st.q1), y: yc - 7, width: Math.max(1, x(st.q3) - x(st.q1)), height: 14, rx: 3, class: "box" }));
      // deterministic jitter so the strip doesn't dance between redraws
      st.v.forEach((v, j) => g.append(s("circle", { cx: x(v), cy: yc + (((j * 7919) % 11) - 5), r: 1.8, class: "dot" })));
      g.append(s("line", { x1: x(st.med), x2: x(st.med), y1: yc - 8, y2: yc + 8, class: "med" }));
      g.append(s("text", { x: W - 2, y: yc + 4, "text-anchor": "end", class: "val" }, fv(st.med)));
      const hit = s("rect", { x: 0, y: yc - RH / 2 - off, width: W, height: RH, fill: "transparent" });
      tip(hit, `${sr.label}\nn ${st.n} · median ${fv(st.med)} · mean ${fv(st.mean)}\nIQR ${fv(st.q1)}–${fv(st.q3)} · range ${fv(st.min)}–${fv(st.max)}`);
      g.append(hit);
    });
    return g;
  };
  return h("div", { class: "card table-card fig" },
    h("div", { class: "fig-head" }, h("h3", {}, fig.title || fig.id), h("span", { class: "muted fig-cap" }, "box = IQR · tick = median · value = median")),
    ser.length ? responsive(draw) : h("div", { class: "muted" }, "no series"));
}

// One row per labelled point (e.g. per test user), one coloured dot per series: joins the same unit across series.
function pointFigure(fig) {
  const labels = fig.point_labels.map(String);
  const ser = (fig.series || []).filter((x) => x && Array.isArray(x.values)).slice(0, SERIES.length);
  const dom = domainOf(ser.flatMap((x) => x.values), false);
  const draw = (W) => {
    const RH = 18, top = 4, axisH = 18, labW = 46, valW = 10;
    const x0 = labW + 8, x1 = W - valW;
    const x = (v) => x0 + ((v - dom[0]) / (dom[1] - dom[0])) * (x1 - x0);
    const H = top + labels.length * RH + axisH;
    const g = s("svg", { width: W, height: H, class: "dist" });
    for (const t of ticksFor(dom)) {
      g.append(s("line", { x1: x(t), x2: x(t), y1: top, y2: H - axisH + 2, class: "grid" }));
      g.append(s("text", { x: x(t), y: H - 4, "text-anchor": "middle", class: "tick" }, String(+t.toFixed(2))));
    }
    labels.forEach((lab, i) => {
      const yc = top + i * RH + RH / 2;
      const vals = ser.map((sr) => num(sr.values[i]));
      const present = vals.filter((v) => v != null);
      g.append(s("text", { x: labW, y: yc + 4, "text-anchor": "end", class: "lab" }, lab));
      if (present.length > 1) g.append(s("line", { x1: x(Math.min(...present)), x2: x(Math.max(...present)), y1: yc, y2: yc, class: "join" }));
      vals.forEach((v, k) => { if (v != null) g.append(s("circle", { cx: x(v), cy: yc, r: 4, class: "sdot", style: `fill:var(${SERIES[k]})` })); });
      const hit = s("rect", { x: 0, y: yc - RH / 2, width: W, height: RH, fill: "transparent" });
      tip(hit, [`${lab}`, ...ser.map((sr, k) => `${sr.label}: ${fv(vals[k])}`)].join("\n"));
      g.append(hit);
    });
    return g;
  };
  const legend = h("div", { class: "viz-legend" }, ser.map((sr, k) => {
    const st = stats(sr.values);
    return h("span", {}, h("i", { class: "lg-sw", style: `background:var(${SERIES[k]})` }), `${sr.label}${st ? " · median " + fv(st.med) : ""}`);
  }));
  return h("div", { class: "card table-card fig" },
    h("div", { class: "fig-head" }, h("h3", {}, fig.title || fig.id), legend),
    labels.length ? responsive(draw) : h("div", { class: "muted" }, "no points"));
}

// ---------------------------------------------------------------- page
const view = { project: null, push: null };

export async function renderResearch(params = {}) {
  const app = $("#app"); app.innerHTML = "";
  state.view = "research";
  let projects;
  try { projects = (await api("/api/v1/projects")).projects; } catch (e) { app.append(h("div", { class: "empty" }, `Cannot load projects: ${e.message}`)); return; }
  if (!projects.length) {
    app.append(h("div", { class: "page-head" }, h("h2", {}, "Research")),
      h("div", { class: "empty" }, "No results pushed yet. From any machine with the agent: ", h("code", {}, "avm results push <project> results.json")));
    return;
  }
  const project = params.project && projects.some((p) => p.project === params.project) ? params.project : projects[0].project;
  view.project = project; view.push = params.push ? Number(params.push) : null;
  let cur, hist;
  try {
    [cur, hist] = await Promise.all([api(`/api/v1/projects/${project}/results${view.push ? "?push=" + view.push : ""}`), api(`/api/v1/projects/${project}/results/history`)]);
  } catch (e) { app.append(h("div", { class: "empty" }, `Cannot load results: ${e.message}`)); return; }
  if (state.view !== "research" || view.project !== project) return;  // navigated away meanwhile
  const pushes = hist.pushes;
  const latestId = pushes.length ? pushes[pushes.length - 1].id : cur.id;
  const doc = cur.doc || {};

  const projSel = projects.length > 1 ? h("select", { onchange: (e) => navigate(`#/research/${e.target.value}`) },
    projects.map((p) => h("option", { value: p.project, selected: p.project === project ? "" : null }, p.project))) : null;
  const pushSel = h("select", { title: "View an earlier push", onchange: (e) => navigate(`#/research/${project}${Number(e.target.value) === latestId ? "" : "?push=" + e.target.value}`) },
    [...pushes].reverse().map((p) => h("option", { value: p.id, selected: p.id === cur.id ? "" : null },
      `#${p.id} · ${fmt.when(p.received)}${p.commit ? " · " + p.commit : ""}${p.id === latestId ? " (latest)" : ""}`)));
  const meta = h("div", { class: "research-meta" },
    h("span", { class: "chip" + (cur.id === latestId ? " run" : " warn") }, cur.id === latestId ? "latest" : "earlier push"),
    h("span", {}, `generated ${doc.generated_at ? new Date(doc.generated_at).toLocaleString() : "?"}`),
    cur.commit ? h("span", { class: "num" }, `commit ${cur.commit}`) : null,
    h("span", {}, `pushed ${fmt.ago(Date.now() / 1000 - cur.received)}${cur.host ? " from " + cur.host : ""}`),
    h("span", {}, `${pushes.length} push${pushes.length === 1 ? "" : "es"} in history`));

  const notes = (doc.notes || []).filter((n) => typeof n === "string" && n.trim());
  const notesEl = notes.length ? h("div", { class: "card research-notes" }, h("h3", {}, "Read before quoting any number"), h("ul", {}, notes.map((n) => h("li", {}, n)))) : null;

  const figs = [
    ...(doc.bars || []).filter((f) => f && typeof f === "object").map(barFigure),
    ...(doc.distributions || []).filter((f) => f && typeof f === "object").map((f) => (Array.isArray(f.point_labels) && f.point_labels.length ? pointFigure(f) : stripFigure(f))),
  ];
  app.append(
    h("div", { class: "page-head" }, h("h2", {}, "Research"), h("span", { class: "muted" }, project), h("div", { class: "filters" }, projSel, pushSel)),
    meta,
    headlineTable(doc, pushes, cur.id) || h("div", { class: "empty" }, "This push has no headline results."),
    notesEl,
    programmes(doc),
    figs.length ? h("div", { class: "research-figs" }, figs) : null,
  );
}

on("results", (m) => {
  if (state.view !== "research") return;
  if (m.project === view.project && !view.push) { toast(`New ${m.project} results (#${m.id})`); renderResearch({ project: view.project }); }
  else if (!view.project) renderResearch({});
});

// Minimal canvas time-series chart + sparkline. No dependencies.
// TimeChart: multi-series line/area, recessive grid, crosshair tooltip, live append.

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function niceMax(v, bytes = false) {
  if (!(v > 0)) return bytes ? 1024 : 1;
  let scale = 1;
  if (bytes) { while (v / scale >= 1024) scale *= 1024; v /= scale; }
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / p;
  const n = m <= 1 ? 1 : m <= 2 ? 2 : m <= 4 ? 4 : m <= 5 ? 5 : m <= 8 ? 8 : 10;
  return n * p * scale;
}

function timeTicks(t0, t1, width) {
  const span = t1 - t0;
  const target = Math.max(2, Math.floor(width / 90));
  const steps = [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  let step = steps[steps.length - 1];
  for (const s of steps) { if (span / s <= target) { step = s; break; } }
  const ticks = [];
  const tz = new Date().getTimezoneOffset() * 60;
  for (let t = Math.ceil((t0 - tz) / step) * step + tz; t <= t1; t += step) ticks.push(t);
  return { ticks, step };
}

function fmtTick(t, step, span) {
  const d = new Date(t * 1000);
  const hh = String(d.getHours()).padStart(2, "0"), mm = String(d.getMinutes()).padStart(2, "0");
  if (span > 2 * 86400) {
    const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return step >= 86400 ? day : `${day} ${hh}:${mm}`;
  }
  if (span > 86400 && d.getHours() === 0 && d.getMinutes() === 0) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${hh}:${mm}`;
}

export function fmtTime(t) {
  const d = new Date(t * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export class TimeChart {
  /**
   * @param {HTMLElement} el container (position: relative)
   * @param {object} opts { unit, format(v)->string, yMax (fixed) | null (auto), yMin=0, area=bool, window (sec) }
   */
  constructor(el, opts = {}) {
    this.el = el;
    this.opts = Object.assign({ yMin: 0, yMax: null, area: true, bytes: false, format: (v) => String(Math.round(v)), window: 3600 }, opts);
    this.series = [];
    this.canvas = document.createElement("canvas");
    this.tip = document.createElement("div");
    this.tip.className = "tooltip";
    el.appendChild(this.canvas);
    el.appendChild(this.tip);
    this.ctx = this.canvas.getContext("2d");
    this.hover = null;
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(el);
    this.canvas.addEventListener("mousemove", (e) => this._onMove(e));
    this.canvas.addEventListener("mouseleave", () => { this.hover = null; this.tip.style.display = "none"; this.draw(); });
    this.canvas.addEventListener("touchstart", (e) => this._onMove(e.touches[0]), { passive: true });
    this.canvas.addEventListener("touchmove", (e) => this._onMove(e.touches[0]), { passive: true });
    this.pad = { l: 44, r: 10, t: 8, b: 22 };
  }

  destroy() { this.ro.disconnect(); this.el.innerHTML = ""; }

  /** series: [{ key, label, color, data: [[ts, v|null], ...] }] */
  setSeries(series, end = null) {
    this.series = series.map((s) => ({ ...s, data: s.data.slice() }));
    this.end = end;
    this.draw();
  }

  append(key, ts, v) {
    const s = this.series.find((x) => x.key === key);
    if (!s) return;
    const last = s.data[s.data.length - 1];
    if (last && ts <= last[0]) return;
    s.data.push([ts, v]);
    const cutoff = ts - this.opts.window;
    while (s.data.length > 2 && s.data[1][0] < cutoff) s.data.shift();
  }

  setWindow(sec) { this.opts.window = sec; this.draw(); }

  _range() {
    const now = Date.now() / 1000;
    const t1 = this.end || now;
    const t0 = t1 - this.opts.window;
    let max = 0;
    for (const s of this.series) for (const [t, v] of s.data) if (v != null && t >= t0 && v > max) max = v;
    const yMax = this.opts.yMax != null ? this.opts.yMax : niceMax(max * 1.1, this.opts.bytes);
    return { t0, t1, yMin: this.opts.yMin, yMax: Math.max(yMax, this.opts.yMin + 1e-9) };
  }

  _onMove(e) {
    const r = this.canvas.getBoundingClientRect();
    const x = e.clientX - r.left;
    const { t0, t1 } = this._range();
    const w = r.width - this.pad.l - this.pad.r;
    const t = t0 + ((x - this.pad.l) / w) * (t1 - t0);
    this.hover = Math.min(t1, Math.max(t0, t));  // hovering the gutters snaps to the nearest edge
    this.draw();
  }

  draw() {
    const r = this.el.getBoundingClientRect();
    const W = Math.max(50, r.width), H = Math.max(50, r.height);
    const dpr = window.devicePixelRatio || 1;
    if (this.canvas.width !== Math.round(W * dpr) || this.canvas.height !== Math.round(H * dpr)) {
      this.canvas.width = Math.round(W * dpr); this.canvas.height = Math.round(H * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const { l, r: pr, t: pt, b: pb } = this.pad;
    const pw = W - l - pr, ph = H - pt - pb;
    const { t0, t1, yMin, yMax } = this._range();
    const X = (t) => l + ((t - t0) / (t1 - t0)) * pw;
    const Y = (v) => pt + ph - ((v - yMin) / (yMax - yMin)) * ph;
    const ink3 = css("--ink-3"), grid = css("--grid"), axis = css("--axis");

    // grid + y labels (gutter sized to the widest label)
    ctx.font = "11px " + css("--font");
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    const yTicks = 4;
    let widest = 0;
    for (let i = 0; i <= yTicks; i++) widest = Math.max(widest, ctx.measureText(this.opts.format(yMin + ((yMax - yMin) * i) / yTicks)).width);
    const gutter = Math.ceil(widest) + 12;
    if (gutter !== this.pad.l) { this.pad.l = gutter; return this.draw(); }
    for (let i = 0; i <= yTicks; i++) {
      const v = yMin + ((yMax - yMin) * i) / yTicks;
      const y = Math.round(Y(v)) + 0.5;
      ctx.strokeStyle = i === 0 ? axis : grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(l, y); ctx.lineTo(W - pr, y); ctx.stroke();
      ctx.fillStyle = ink3;
      ctx.fillText(this.opts.format(v), l - 6, y);
    }
    // x ticks
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const { ticks, step } = timeTicks(t0, t1, pw);
    for (const t of ticks) {
      const x = Math.round(X(t)) + 0.5;
      ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(x, pt); ctx.lineTo(x, pt + ph); ctx.stroke();
      ctx.fillStyle = ink3; ctx.fillText(fmtTick(t, step, t1 - t0), x, pt + ph + 6);
    }

    // series
    ctx.save();
    ctx.beginPath(); ctx.rect(l, pt - 1, pw, ph + 2); ctx.clip();
    const single = this.series.length === 1;
    for (const s of this.series) {
      const color = s.color;
      let started = false;
      ctx.beginPath();
      let firstX = null, lastX = null;
      for (const [t, v] of s.data) {
        if (t < t0 - 60 || t > t1 + 1) continue;
        if (v == null) { started = false; continue; }
        const x = X(t), y = Y(Math.min(v, yMax));
        if (!started) { ctx.moveTo(x, y); started = true; if (firstX == null) firstX = x; } else ctx.lineTo(x, y);
        lastX = x;
      }
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.stroke();
      if (this.opts.area && single && firstX != null) {
        // area under the line (single series only)
        ctx.lineTo(lastX, Y(yMin)); ctx.lineTo(firstX, Y(yMin)); ctx.closePath();
        const g = ctx.createLinearGradient(0, pt, 0, pt + ph);
        g.addColorStop(0, color + "55"); g.addColorStop(1, color + "05");
        ctx.fillStyle = g; ctx.fill();
      }
    }
    ctx.restore();

    // hover crosshair + tooltip
    if (this.hover != null && this.hover >= t0 && this.hover <= t1) {
      const x = Math.round(X(this.hover)) + 0.5;
      ctx.strokeStyle = css("--ink-3"); ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, pt); ctx.lineTo(x, pt + ph); ctx.stroke(); ctx.setLineDash([]);
      const rows = [];
      let tAt = null;
      for (const s of this.series) {
        // nearest point at or before hover
        let best = null;
        for (const p of s.data) { if (p[0] <= this.hover) best = p; else break; }
        if (best && this.hover - best[0] < Math.max(120, (t1 - t0) / 40) && best[1] != null) {
          rows.push({ s, v: best[1] });
          tAt = tAt == null ? best[0] : Math.max(tAt, best[0]);
          ctx.fillStyle = s.color; ctx.strokeStyle = css("--surface");
          ctx.beginPath(); ctx.arc(X(best[0]), Y(Math.min(best[1], yMax)), 4, 0, Math.PI * 2); ctx.fill(); ctx.lineWidth = 2; ctx.stroke();
        }
      }
      if (rows.length) {
        this.tip.innerHTML = `<div class="t">${fmtTime(tAt)}</div>` + rows.map((r) =>
          `<div class="row"><span><i style="background:${r.s.color}"></i>${r.s.label}</span><b class="num">${this.opts.format(r.v, true)}</b></div>`).join("");
        this.tip.style.display = "block";
        const tw = this.tip.offsetWidth;
        let left = x + 12; if (left + tw > W - 4) left = x - tw - 12;
        this.tip.style.left = Math.max(0, left) + "px";
        this.tip.style.top = pt + "px";
      } else this.tip.style.display = "none";
    }
  }
}

/** Tiny area sparkline (fixed 0..yMax). values: [[ts, v|null]] */
export function drawSparkline(canvas, values, color, { yMax = 100, span = 900 } = {}) {
  const r = canvas.getBoundingClientRect();
  const W = Math.max(10, r.width), H = Math.max(10, r.height);
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!values || !values.length) return;
  const t1 = values[values.length - 1][0], t0 = t1 - span;
  let max = yMax;
  if (yMax == null) { max = 0; for (const [, v] of values) if (v != null && v > max) max = v; max = niceMax(max * 1.1); }
  const X = (t) => ((t - t0) / (t1 - t0 || 1)) * (W - 2) + 1;
  const Y = (v) => H - 2 - (Math.min(v, max) / max) * (H - 4);
  ctx.beginPath();
  let started = false, firstX = null, lastX = null;
  for (const [t, v] of values) {
    if (t < t0) continue;
    if (v == null) { started = false; continue; }
    const x = X(t), y = Y(v);
    if (!started) { ctx.moveTo(x, y); started = true; if (firstX == null) firstX = x; } else ctx.lineTo(x, y);
    lastX = x;
  }
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.lineJoin = "round"; ctx.stroke();
  if (firstX != null) {
    ctx.lineTo(lastX, H); ctx.lineTo(firstX, H); ctx.closePath();
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, color + "50"); g.addColorStop(1, color + "00");
    ctx.fillStyle = g; ctx.fill();
  }
}

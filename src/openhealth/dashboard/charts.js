"use strict";

// Charts: four small SVG primitives (stacked columns, lines, scatter, horizontal bars), no library.
// Views build a spec, put chartHtml(spec) in the page, then call wireCharts(root).
//
// - SVG is drawn at the container's real width (redrawn on resize), so text is always 12 px and
//   never scaled down on a phone. Colours are CSS classes on tokens, so a theme switch needs no
//   redraw.
// - columns / lines / scatter are groups of marks. Each mark is focusable (arrow keys, Home and End
//   move between them, one Tab stop per chart) and shows a tooltip on hover and on focus; its
//   aria-label carries the exact values. `data` (a tableHtml spec) becomes a "View data" table.
// - bars are static: every value is printed on the chart and the whole chart has one aria-label.
//
// spec = { id, type, ariaLabel, legend: [{ swatch, label }], note (trusted HTML), data (tableHtml
//   spec without id), ...per type }
//
//   columns  categories: [{ axis, title, values: { seriesKey: count } }], series: [{ key, label, swatch }]
//            swatch "AUTO" colours the segment and its legend chip with the outcome tokens.
//   lines    x: { title, format }, series: [{ key, label, swatch, dashed }], marker: { x, label },
//            points: [{ x, title, values: { seriesKey: rate | null }, extra: [[label, value]] }]
//            (y values are rates from 0 to 1)
//   scatter  x, y: { kind: "rate" | "log", title, format?, from? }, diagonal, square, sizeBy,
//            points: [{ x, y, weight?, shape?, tone?, ring?, title, rows: [[label, value]] }]
//   bars     rows: [{ label, value, chosen, tone }]   (value is a rate; tone: "block" | "review")

const CHARTS = new Map(); // id -> { spec, marks }

const px = (value) => Math.round(value * 10) / 10;
// 12 px system font, a little generous so labels never collide.
const textWidth = (text) => String(text).length * 6.8;
const rateTick = (value) => `${+(value * 100).toFixed(1)}%`;

// ---- axes ------------------------------------------------------------------------------------

function niceAxis(lo, hi, { target = 4, integer = false } = {}) {
  if (!(hi > lo)) hi = lo + (integer ? 1 : 0.1);
  const raw = (hi - lo) / target;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const multiples = integer ? [1, 2, 5, 10] : [1, 2, 2.5, 5, 10];
  let step = multiples.map((m) => m * magnitude).find((s) => s >= raw - 1e-12);
  if (integer) step = Math.max(1, step);
  const first = Math.floor(lo / step + 1e-9) * step;
  const last = Math.ceil(hi / step - 1e-9) * step;
  const ticks = [];
  for (let i = 0; first + i * step <= last + step * 1e-6; i++) ticks.push(+(first + i * step).toFixed(10));
  return { min: ticks[0], max: ticks[ticks.length - 1], ticks };
}

// kind "rate": 0..1 shown as percent, up to 100 %. It starts at 0, so differences are not
// exaggerated; values that all sit within 15 points near the top zoom in to the tenth below them,
// and the axis is then marked `broken` so the chart draws a break and says where it starts.
// kind "log": positive values on decades; zero (a free configuration) gets its own slot before them.
// kind "number": a plain linear axis.
function makeAxis(kind, values, { format, from } = {}) {
  const known = values.filter((v) => v !== null && v !== undefined && Number.isFinite(v));
  if (kind === "rate") {
    const [lowest, highest] = [Math.min(...known, 1), Math.max(...known, 0)];
    const zoom = from === undefined && known.length > 0 && highest - lowest < 0.15 && lowest >= 0.5;
    // Zoomed: the tenth below the lowest value (at least the top tenth, so values all at 100 % still
    // have an axis).
    const low = from ?? (zoom ? Math.min(0.9, Math.floor(lowest * 10) / 10) : 0);
    const axis = niceAxis(low, 1);
    axis.max = 1;
    axis.ticks = axis.ticks.filter((t) => t <= 1 + 1e-9);
    return { ...axis, format: format ?? rateTick, kind, broken: axis.min > 0 };
  }
  if (kind === "log") {
    const positive = known.filter((v) => v > 0);
    const lo = 10 ** Math.floor(Math.log10(Math.min(...positive)));
    let hi = 10 ** Math.ceil(Math.log10(Math.max(...positive)));
    if (hi <= lo) hi = lo * 10;
    const ticks = [];
    for (let v = lo; v <= hi * 1.0001; v *= 10) ticks.push(v);
    return { min: lo, max: hi, ticks, format, kind, free: known.some((v) => v === 0) };
  }
  const axis = niceAxis(Math.min(...known), Math.max(...known), { target: 5 });
  return { ...axis, format: format ?? String, kind };
}

// Position of a value along the axis, from `start` to `end` (pixels).
function axisAt(axis, value, start, end, freeGutter = 0) {
  if (axis.kind === "log") {
    if (value <= 0) return start + 8;
    const lo = start + freeGutter;
    return lo + ((Math.log10(value) - Math.log10(axis.min)) / (Math.log10(axis.max) - Math.log10(axis.min))) * (end - lo);
  }
  return start + ((value - axis.min) / (axis.max - axis.min)) * (end - start);
}

// ---- marks (tooltip + accessible name) ---------------------------------------------------------

const markLabel = (mark) => `${mark.title}: ${mark.rows.map(([label, value]) => `${label} ${value}`).join(", ")}`;
const markTip = (mark) => `<div class="tip-title">${esc(mark.title)}</div>${mark.rows.map(([label, value, swatch]) =>
  `<div class="tip-row">${swatch ? `<i class="${esc(swatch)}"></i>` : ""}<span>${esc(label)}</span><b>${esc(value)}</b></div>`).join("")}`;
const markAttrs = (index, mark, ax, ay) =>
  `class="mark" data-i="${index}" data-ax="${px(ax)}" data-ay="${px(ay)}" tabindex="-1" role="img" aria-label="${esc(markLabel(mark))}"`;

const svgOpen = (spec, width, height, role) =>
  `<svg class="chart-svg" width="${px(width)}" height="${px(height)}" viewBox="0 0 ${px(width)} ${px(height)}" role="${role}" aria-label="${esc(spec.ariaLabel)}">`;
// Axes, gridlines and tick labels: drawn for the eye only. A screen reader gets the chart's summary
// and each mark's exact values, not a stream of stray tick numbers.
const decor = (markup) => `<g aria-hidden="true">${markup}</g>`;

// Two short strokes across the foot of a y axis that does not start at 0 (a truncated scale).
const axisBreak = (axis, x, y) => (axis.broken
  ? `<path class="axis-break" d="M${px(x - 5)},${px(y - 10)} l10,-5 M${px(x - 5)},${px(y - 15)} l10,-5"/>` : "");

// ---- stacked columns ---------------------------------------------------------------------------

function drawColumns(spec, width) {
  const H = 220;
  const cats = spec.categories;
  const total = (cat) => spec.series.reduce((sum, s) => sum + (cat.values[s.key] ?? 0), 0);
  const axis = { ...niceAxis(0, Math.max(...cats.map(total), 1), { integer: true }), kind: "number" };
  const labelW = Math.max(...axis.ticks.map((t) => textWidth(t.toLocaleString()))) + 10;
  const m = { t: 10, r: 8, b: 26, l: labelW };
  const plotH = H - m.t - m.b;
  const slot = Math.min((width - m.l - m.r) / cats.length, 56);
  const svgW = m.l + slot * cats.length + m.r; // a short history stays compact instead of stretching
  const y = (v) => m.t + plotH - (v / axis.max) * plotH;
  const barW = Math.min(slot * 0.7, 36);
  const stride = Math.ceil((Math.max(...cats.map((c) => textWidth(c.axis))) + 8) / slot);

  const marks = [];
  const grid = axis.ticks.map((t) => `<line class="grid" x1="${m.l}" x2="${px(svgW - m.r)}" y1="${px(y(t))}" y2="${px(y(t))}"/>
    <text class="tick" x="${m.l - 6}" y="${px(y(t) + 4)}" text-anchor="end">${t.toLocaleString()}</text>`).join("");
  const dayLabels = cats.map((cat, i) => ((cats.length - 1 - i) % stride === 0
    ? `<text class="tick" x="${px(m.l + i * slot + slot / 2)}" y="${H - 8}" text-anchor="middle">${esc(cat.axis)}</text>` : "")).join("");
  const columns = cats.map((cat, i) => {
    const x0 = m.l + i * slot;
    let top = m.t + plotH;
    const segments = spec.series.map((s) => {
      const v = cat.values[s.key] ?? 0;
      if (!v) return "";
      const h = (v / axis.max) * plotH;
      top -= h;
      return `<rect class="f-${esc(s.swatch)} seg" x="${px(x0 + (slot - barW) / 2)}" y="${px(top)}" width="${px(barW)}" height="${px(h)}"/>`;
    }).join("");
    const mark = {
      title: cat.title,
      rows: [...spec.series.map((s) => [s.label, String(cat.values[s.key] ?? 0), `bg-${s.swatch}`]), ["Total", String(total(cat))]],
    };
    marks.push(mark);
    return `<g ${markAttrs(i, mark, x0 + slot / 2, y(total(cat)))}>
      <rect class="hl" x="${px(x0)}" y="${m.t}" width="${px(slot)}" height="${plotH}"/>${segments}
      <rect class="hit" x="${px(x0)}" y="${m.t}" width="${px(slot)}" height="${plotH}"/></g>`;
  }).join("");
  const axisLine = `<line class="axis" x1="${m.l}" x2="${px(svgW - m.r)}" y1="${m.t + plotH}" y2="${m.t + plotH}"/>`;
  return { marks, svg: `${svgOpen(spec, svgW, H, "group")}${decor(grid + axisLine + dayLabels)}${columns}</svg>` };
}

// ---- lines -------------------------------------------------------------------------------------

function drawLines(spec, width) {
  const H = 250;
  const yAxis = makeAxis("rate", spec.points.flatMap((p) => spec.series.map((s) => p.values[s.key])));
  const xValues = [...spec.points.map((p) => p.x), spec.marker?.x].filter((v) => v !== undefined);
  const xAxis = makeAxis("number", xValues, { format: spec.x.format });
  const labelW = Math.max(...yAxis.ticks.map((t) => textWidth(yAxis.format(t)))) + 10;
  const m = { t: spec.marker ? 22 : 10, r: 14, b: 44, l: labelW + 6 };
  const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
  const x = (v) => axisAt(xAxis, v, m.l, m.l + plotW);
  const y = (v) => m.t + plotH - ((v - yAxis.min) / (yAxis.max - yAxis.min)) * plotH;

  const grid = yAxis.ticks.map((t) => `<line class="grid" x1="${m.l}" x2="${m.l + plotW}" y1="${px(y(t))}" y2="${px(y(t))}"/>
    <text class="tick" x="${m.l - 6}" y="${px(y(t) + 4)}" text-anchor="end">${esc(yAxis.format(t))}</text>`).join("");
  const xTicks = xAxis.ticks.map((t) => `<text class="tick" x="${px(x(t))}" y="${m.t + plotH + 16}" text-anchor="middle">${esc(xAxis.format(t))}</text>`).join("");
  const paths = spec.series.map((s) => {
    let d = "", pen = false;
    for (const p of spec.points) {
      const v = p.values[s.key];
      if (v === null || v === undefined) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${px(x(p.x))},${px(y(v))}`;
      pen = true;
    }
    const dots = spec.points.filter((p) => p.values[s.key] !== null && p.values[s.key] !== undefined)
      .map((p) => `<circle class="f-${esc(s.swatch)}" cx="${px(x(p.x))}" cy="${px(y(p.values[s.key]))}" r="3"/>`).join("");
    return `<path class="line st-${esc(s.swatch)}${s.dashed ? " dashed" : ""}" d="${d}"/>${dots}`;
  }).join("");

  const marks = [];
  const columns = spec.points.map((p, i) => {
    const [before, after] = [spec.points[i - 1], spec.points[i + 1]];
    const left = before ? (x(before.x) + x(p.x)) / 2 : m.l;
    const right = after ? (x(after.x) + x(p.x)) / 2 : m.l + plotW;
    const known = spec.series.filter((s) => p.values[s.key] !== null && p.values[s.key] !== undefined);
    const mark = {
      title: p.title,
      rows: [...spec.series.map((s) => [s.label, p.values[s.key] === null || p.values[s.key] === undefined ? "n/a" : rateTick(p.values[s.key]), `line ${s.swatch}${s.dashed ? " dashed" : ""}`]), ...(p.extra ?? [])],
    };
    marks.push(mark);
    const focus = known.map((s) => `<circle class="focus-dot st-${esc(s.swatch)}" cx="${px(x(p.x))}" cy="${px(y(p.values[s.key]))}" r="6"/>`).join("");
    const anchor = known.length ? Math.min(...known.map((s) => y(p.values[s.key]))) : m.t + plotH;
    return `<g ${markAttrs(i, mark, x(p.x), anchor)}>
      <line class="guide" x1="${px(x(p.x))}" x2="${px(x(p.x))}" y1="${m.t}" y2="${m.t + plotH}"/>${focus}
      <rect class="hit" x="${px(left)}" y="${m.t}" width="${px(right - left)}" height="${plotH}"/></g>`;
  }).join("");

  let marker = "";
  if (spec.marker) {
    const mx = x(spec.marker.x);
    const anchorEnd = mx > m.l + plotW * 0.6;
    marker = `<line class="ref" x1="${px(mx)}" x2="${px(mx)}" y1="${m.t}" y2="${m.t + plotH}"/>
      <text class="tick strong" x="${px(mx + (anchorEnd ? -5 : 5))}" y="${m.t - 8}" text-anchor="${anchorEnd ? "end" : "start"}">${esc(spec.marker.label)}</text>`;
  }
  const title = `<text class="tick" x="${px(m.l + plotW / 2)}" y="${H - 6}" text-anchor="middle">${esc(spec.x.title)}</text>`;
  const axisLine = `<line class="axis" x1="${m.l}" x2="${m.l + plotW}" y1="${m.t + plotH}" y2="${m.t + plotH}"/>`;
  return {
    marks, axisNote: yAxis.broken ? `The vertical axis starts at ${yAxis.format(yAxis.min)}.` : "",
    svg: `${svgOpen(spec, width, H, "group")}${decor(grid + axisLine + axisBreak(yAxis, m.l, m.t + plotH) + xTicks + title + marker + paths)}${columns}</svg>`,
  };
}

// ---- scatter -----------------------------------------------------------------------------------

function drawScatter(spec, width) {
  const size = spec.square ? Math.min(width, 440) : width;
  const xAxis = makeAxis(spec.x.kind, spec.points.map((p) => p.x), spec.x);
  const yAxis = makeAxis(spec.y.kind, spec.points.map((p) => p.y), spec.y);
  const labelW = Math.max(...yAxis.ticks.map((t) => textWidth(yAxis.format(t)))) + 10;
  const m = { t: 12, r: 16, b: 46, l: labelW + 30 };
  const plotW = size - m.l - m.r;
  const plotH = spec.square ? plotW : 240; // a square plot keeps the calibration diagonal at 45 degrees
  const H = m.t + plotH + m.b;
  const gutter = xAxis.free ? 64 : 0; // room for the "$0" slot before the first decade's label
  const x = (v) => axisAt(xAxis, v, m.l, m.l + plotW, gutter);
  const y = (v) => m.t + plotH - ((v - yAxis.min) / (yAxis.max - yAxis.min)) * plotH;
  const biggest = Math.max(...spec.points.map((p) => p.weight ?? 0), 1);
  const radius = (p) => (spec.sizeBy ? 4 + 10 * Math.sqrt((p.weight ?? 0) / biggest) : 5.5);

  const yGrid = yAxis.ticks.map((t) => `<line class="grid" x1="${m.l}" x2="${m.l + plotW}" y1="${px(y(t))}" y2="${px(y(t))}"/>
    <text class="tick" x="${m.l - 6}" y="${px(y(t) + 4)}" text-anchor="end">${esc(yAxis.format(t))}</text>`).join("");
  // Every tick gets a gridline; a label is skipped when it would touch the one before it.
  let labelEnd = xAxis.free ? m.l + 8 + textWidth(xAxis.format(0)) / 2 : -Infinity;
  const xGrid = xAxis.ticks.map((t) => {
    const [at, w] = [x(t), textWidth(xAxis.format(t))];
    const fits = at - w / 2 >= labelEnd + 8;
    if (fits) labelEnd = at + w / 2;
    return `<line class="grid faint" x1="${px(at)}" x2="${px(at)}" y1="${m.t}" y2="${m.t + plotH}"/>${fits
      ? `<text class="tick" x="${px(at)}" y="${m.t + plotH + 16}" text-anchor="middle">${esc(xAxis.format(t))}</text>` : ""}`;
  }).join("");
  const free = xAxis.free
    ? `<line class="grid dashed" x1="${px(m.l + 24)}" x2="${px(m.l + 24)}" y1="${m.t}" y2="${m.t + plotH}"/>
       <text class="tick" x="${px(m.l + 8)}" y="${m.t + plotH + 16}" text-anchor="middle">${esc(xAxis.format(0))}</text>` : "";
  const diagonal = spec.diagonal
    ? `<line class="ref" x1="${px(x(xAxis.min))}" y1="${px(y(yAxis.min))}" x2="${px(x(xAxis.max))}" y2="${px(y(yAxis.max))}"/>` : "";
  const axisTitles = `<text class="tick" x="${px(m.l + plotW / 2)}" y="${H - 6}" text-anchor="middle">${esc(spec.x.title)}</text>
    <text class="tick" transform="translate(12 ${px(m.t + plotH / 2)}) rotate(-90)" text-anchor="middle">${esc(spec.y.title)}</text>`;

  const marks = [];
  const dots = [...spec.points].sort((a, b) => a.x - b.x).map((p, i) => {
    const [cx, cy, r] = [x(p.x), y(p.y), radius(p)];
    const mark = { title: p.title, rows: p.rows };
    marks.push(mark);
    const tone = p.tone ?? "c1";
    const shape = p.shape === "diamond"
      ? `<rect class="dot f-${tone}" x="${px(cx - r * 0.85)}" y="${px(cy - r * 0.85)}" width="${px(r * 1.7)}" height="${px(r * 1.7)}" transform="rotate(45 ${px(cx)} ${px(cy)})"/>`
      : `<circle class="dot f-${tone}" cx="${px(cx)}" cy="${px(cy)}" r="${px(r)}"/>`;
    return `<g ${markAttrs(i, mark, cx, cy - r)}>
      <circle class="focus-ring" cx="${px(cx)}" cy="${px(cy)}" r="${px(r + 8)}"/>${shape}
      ${p.ring ? `<circle class="ring" cx="${px(cx)}" cy="${px(cy)}" r="${px(r + 4)}"/>` : ""}
      <circle class="hit" cx="${px(cx)}" cy="${px(cy)}" r="${px(Math.max(r + 8, 12))}"/></g>`;
  }).join("");
  const frameRect = `<rect class="frame" x="${m.l}" y="${m.t}" width="${px(plotW)}" height="${px(plotH)}"/>`;
  return {
    marks, axisNote: yAxis.broken ? `The vertical axis starts at ${yAxis.format(yAxis.min)}.` : "",
    svg: `${svgOpen(spec, size, H, "group")}${decor(yGrid + xGrid + free + frameRect + diagonal + axisTitles + axisBreak(yAxis, m.l, m.t + plotH))}${dots}</svg>`,
  };
}

// ---- horizontal bars ---------------------------------------------------------------------------

function drawBars(spec, width) {
  const rowH = 26;
  const labelW = Math.min(Math.max(...spec.rows.map((r) => textWidth(r.label))) + 12, width * 0.4);
  const valueW = 50;
  const barW = Math.max(width - labelW - valueW - 4, 40);
  const fits = (label) => textWidth(label) <= labelW - 12;
  const rows = spec.rows.map((r, i) => {
    const top = i * rowH;
    const tone = r.tone ? `f-${r.tone}` : r.chosen ? "f-c1" : "f-muted";
    const name = fits(r.label) ? esc(r.label) : `${esc(r.label.slice(0, Math.max(3, Math.floor((labelW - 24) / 6.8))))}…`;
    return `<g class="row${r.chosen ? " chosen" : ""}">
      <text class="bar-label" x="0" y="${top + 17}">${name}${fits(r.label) ? "" : `<title>${esc(r.label)}</title>`}</text>
      <rect class="track" x="${px(labelW)}" y="${top + 9}" width="${px(barW)}" height="8" rx="4"/>
      <rect class="${tone}${r.chosen ? "" : " soft"}" x="${px(labelW)}" y="${top + 9}" width="${px(Math.max(r.value * barW, r.value > 0 ? 2 : 0))}" height="8" rx="4"/>
      <text class="bar-value" x="${px(width)}" y="${top + 17}" text-anchor="end">${esc(pct(r.value))}</text></g>`;
  }).join("");
  return { marks: [], svg: `${svgOpen(spec, width, spec.rows.length * rowH + 2, "img")}${rows}</svg>` };
}

const DRAW = { columns: drawColumns, lines: drawLines, scatter: drawScatter, bars: drawBars };

// ---- page pieces -------------------------------------------------------------------------------

function chartHtml(spec) {
  CHARTS.set(spec.id, { spec, marks: [] });
  const legend = spec.legend?.length
    ? `<div class="legend chart-legend">${spec.legend.map((item) => `<span><i class="${esc(item.swatch)}"></i>${esc(item.label)}</span>`).join("")}</div>` : "";
  const data = spec.data
    ? `<details class="chart-data"><summary>View data</summary>${tableHtml({ caption: "", ...spec.data, id: `${spec.id}-data` })}</details>` : "";
  return `<figure class="chart" data-chart="${esc(spec.id)}">${legend}
    <div class="chart-frame"><div class="chart-plot"></div><div class="chart-tip" aria-hidden="true" hidden></div></div>
    <p class="chart-note chart-axis-note" hidden></p>
    ${spec.note ? `<figcaption class="chart-note">${spec.note}</figcaption>` : ""}${data}</figure>`;
}

function wireCharts(root) {
  root.querySelectorAll("[data-chart]").forEach((figure) => {
    if (figure.dataset.wired) return;
    figure.dataset.wired = "1";
    const entry = CHARTS.get(figure.dataset.chart);
    const plot = figure.querySelector(".chart-plot");
    const frame = figure.querySelector(".chart-frame");
    const tip = figure.querySelector(".chart-tip");
    const markElements = () => [...plot.querySelectorAll(".mark")];
    let drawnWidth = 0;

    const hideTip = () => { tip.hidden = true; };
    const showTip = (mark) => {
      const model = entry.marks[Number(mark.dataset.i)];
      if (!model) return;
      tip.innerHTML = markTip(model);
      tip.hidden = false;
      const [ax, ay] = [Number(mark.dataset.ax), Number(mark.dataset.ay)];
      const [w, h] = [tip.offsetWidth, tip.offsetHeight];
      // Above the mark when there is room; otherwise beside it, so it never covers what it describes.
      const [room, span] = [frame.clientWidth, frame.clientHeight];
      let [left, top] = [ax - w / 2, ay - h - 10];
      if (top < 0) {
        left = ax + 16 + w <= room ? ax + 16 : ax - w - 16;
        top = Math.min(Math.max(ay - 4, 0), Math.max(span - h, 0));
      }
      tip.style.left = `${Math.min(Math.max(left, 0), Math.max(room - w, 0))}px`;
      tip.style.top = `${top}px`;
    };
    // Roving tabindex: the chart is one Tab stop, arrow keys move between marks.
    const rove = (index, { focus = false } = {}) => {
      const marks = markElements();
      if (!marks.length) return;
      const target = marks[Math.min(Math.max(index, 0), marks.length - 1)];
      marks.forEach((mark) => mark.setAttribute("tabindex", mark === target ? "0" : "-1"));
      plot.dataset.mark = target.dataset.i;
      if (focus) target.focus({ preventScroll: true });
    };
    const settle = () => {
      const active = document.activeElement;
      if (plot.contains(active) && active.matches(".mark:focus-visible")) showTip(active);
      else hideTip();
    };

    const paint = () => {
      const width = Math.floor(plot.clientWidth);
      if (!width || width === drawnWidth) return; // hidden, or nothing changed
      drawnWidth = width;
      const hadFocus = plot.contains(document.activeElement);
      const { svg, marks, axisNote = "" } = DRAW[entry.spec.type](entry.spec, width);
      entry.marks = marks;
      plot.innerHTML = svg;
      const note = figure.querySelector(".chart-axis-note");
      note.textContent = axisNote;
      note.hidden = !axisNote;
      hideTip();
      rove(Number(plot.dataset.mark ?? 0), { focus: hadFocus });
    };

    plot.addEventListener("mouseover", (event) => {
      const mark = event.target.closest(".mark");
      if (mark) showTip(mark);
    });
    plot.addEventListener("mouseleave", settle);
    plot.addEventListener("focusin", (event) => {
      const mark = event.target.closest(".mark");
      if (!mark) return;
      rove(markElements().indexOf(mark));
      if (mark.matches(":focus-visible")) showTip(mark);
    });
    plot.addEventListener("focusout", (event) => {
      if (!event.relatedTarget?.closest?.(".mark") || !plot.contains(event.relatedTarget)) hideTip();
    });
    plot.addEventListener("keydown", (event) => {
      const marks = markElements();
      const at = marks.indexOf(event.target.closest(".mark"));
      if (event.key === "Escape") return hideTip();
      const next = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: marks.length - 1 }[event.key];
      if (next === undefined || at < 0) return;
      event.preventDefault();
      rove(next, { focus: true });
    });

    const observer = new ResizeObserver(() => {
      if (figure.isConnected) return paint();
      observer.disconnect();
      if (CHARTS.get(figure.dataset.chart) === entry) CHARTS.delete(figure.dataset.chart);
    });
    observer.observe(plot);
    paint();
  });
}

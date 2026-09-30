/* Small charts, drawn as inline SVG.
 *
 * No library: the container may have no internet, and three shapes do not
 * justify one. Each chart scales to the width of its box (a viewBox with
 * preserveAspectRatio="none" for the plot, text kept outside it so it does not
 * stretch), carries a one-line description for a screen reader, and has a
 * hover target per day that is wider than the bar it belongs to. */
import { esc } from "./ui.js";
import { day, num } from "./i18n.js";

/* Columns per day, optionally stacked. `series` is [{key, label, cls}], drawn
   bottom up in that order. Every day gets a <title>, which is the tooltip on
   a pointer and what a screen reader reads out on focus. */
export function columns(rows, series, { label = "", height = 120, unit = "" } = {}) {
  if (!rows.length) return "";
  const width = rows.length * 10;
  const top = Math.max(1, ...rows.map((row) => series.reduce((sum, s) => sum + (row[s.key] || 0), 0)));
  const bars = rows.map((row, index) => {
    const x = index * 10 + 2;
    let y = height;
    const parts = series.map((s) => {
      const value = row[s.key] || 0;
      if (!value) return "";
      const h = Math.max(2, (value / top) * (height - 4));
      y -= h;
      // The surface gap between stacked segments, rather than an outline.
      const rect = `<rect class="${esc(s.cls)}" x="${x}" y="${y.toFixed(1)}" width="6"
        height="${Math.max(1, h - 1).toFixed(1)}" rx="1.5"/>`;
      return rect;
    }).join("");
    const words = [day(row.date, { weekday: "short", day: "numeric", month: "short" }),
      ...series.map((s) => `${s.label}: ${num(row[s.key] || 0)}${unit}`)].join(" · ");
    return `<g class="col"><rect class="hit" x="${index * 10}" y="0" width="10" height="${height}"/>${
      parts}<title>${esc(words)}</title></g>`;
  }).join("");
  return `<figure class="chart">
    <svg class="plot" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"
         aria-label="${esc(label)}">${bars}</svg>
    <figcaption class="axis"><span>${esc(day(rows[0].date))}</span><span>${
      esc(day(rows[rows.length - 1].date))}</span></figcaption>
  </figure>`;
}

/* A line of the last days in one colour, for a number on a tile. */
export function sparkline(values, { label = "", height = 32 } = {}) {
  if (values.length < 2) return "";
  const width = 100;
  const top = Math.max(1, ...values);
  const step = width / (values.length - 1);
  const points = values.map((v, i) => [i * step, height - 2 - (v / top) * (height - 4)]);
  const line = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"
    aria-label="${esc(label)}"><path class="spark-area" d="${line}L${width},${height}L0,${height}Z"/>
    <path class="spark-line" d="${line}" vector-effect="non-scaling-stroke"/></svg>`;
}

/* Horizontal bars with their label and value beside them — for the rules that
   find the most, and for what a library is made of. Plain HTML: text in an
   SVG that stretches is text nobody can read. */
export function hbars(rows, { value = (r) => r.value, text = (r) => r.label,
                             note = (r) => num(value(r)), cls = "" } = {}) {
  const top = Math.max(1, ...rows.map(value));
  return `<ul class="hbars ${esc(cls)}">${rows.map((row) => `
    <li><span class="hbar-label">${esc(text(row))}</span>
      <span class="hbar-track"><span class="hbar-fill" style="width:${
        Math.max(1.5, (value(row) / top) * 100).toFixed(1)}%"></span></span>
      <span class="hbar-value">${esc(note(row))}</span></li>`).join("")}</ul>`;
}

/* One bar split into its parts, with a legend underneath — what a library
   holds per resolution. */
export function split(rows, { value, text, note }) {
  const total = rows.reduce((sum, r) => sum + value(r), 0) || 1;
  return `<div class="split" role="img" aria-label="${esc(rows.map((r) => `${text(r)} ${note(r)}`).join(", "))}">${
    rows.map((row, i) => `<span class="split-part tone-${i % 4}" style="width:${
      ((value(row) / total) * 100).toFixed(2)}%" title="${esc(`${text(row)}: ${note(row)}`)}"></span>`).join("")}</div>
    <ul class="legend">${rows.map((row, i) => `<li><span class="swatch tone-${i % 4}"></span>${
      esc(text(row))} <span class="muted">${esc(note(row))}</span></li>`).join("")}</ul>`;
}

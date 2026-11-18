// SPDX-License-Identifier: Apache-2.0
// Data tables: a semantic <table> with a caption, a sticky header, right-aligned tabular numbers,
// keyboard-operable sorting (aria-sort) and an optional sticky first column. Views build HTML with tableHtml(spec), put it in the page, then call wireTables(root).
// Sorting moves the existing rows rather than redrawing the table, so anything a row holds and the
// focused sort button survive it.
//
// spec = {
//   id,                      unique on the page
//   caption,                 trusted HTML ("" for none)
//   columns: [{
//     key, label,            label is trusted HTML
//     numeric,               right-aligned, tabular
//     cell(row),             trusted HTML for the cell
//     sort(row),             value to sort by; omit for an unsortable column
//     firstDir,              "ascending" | "descending" on the first click (default: numbers
//                            descending, text ascending)
//     className,             extra class on th and td
//   }],
//   rows,
//   defaultCompare(a, b),    the order before anyone sorts (default: as given)
//   rowClass(row), rowAttrs(row),   trusted strings
//   stickyFirst,             keep the first column in view when the table scrolls sideways
//   limitHeight,             scroll inside a max height, so the sticky header stays visible
// }

import { esc, icon } from "./ui.js";

const unwired = new Map(); // specs waiting for their HTML to be attached
const bound = new WeakMap(); // row data lives as long as its table
export const tableRows = (container) => bound.get(container)?.spec.rows;

export function tableHtml(spec) {
  const entry = { spec, sort: null };
  unwired.set(spec.id, entry);
  const classes = ["table-scroll", spec.stickyFirst ? "sticky-first" : "", spec.limitHeight ? "limit" : ""].filter(Boolean).join(" ");
  return `<div class="${classes}" data-table="${esc(spec.id)}">${tableInner(entry)}</div>`;
}

function sortedIndices(entry) {
  const { spec, sort } = entry;
  const indices = spec.rows.map((_, i) => i);
  if (!sort) return spec.defaultCompare ? indices.sort((a, b) => spec.defaultCompare(spec.rows[a], spec.rows[b])) : indices;
  const column = spec.columns.find((c) => c.key === sort.key);
  const sign = sort.dir === "ascending" ? 1 : -1;
  const values = spec.rows.map((row) => column.sort(row));
  return indices.sort((a, b) => {
    const [x, y] = [values[a], values[b]];
    if (x === y) return spec.defaultCompare ? spec.defaultCompare(spec.rows[a], spec.rows[b]) : 0;
    if (x === null || x === undefined) return 1; // missing values last, either direction
    if (y === null || y === undefined) return -1;
    return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * sign;
  });
}

function tableInner(entry) {
  const { spec, sort } = entry;
  const cls = (c) => [c.numeric ? "num" : "", c.className ?? ""].filter(Boolean).join(" ");
  const header = spec.columns.map((c) => {
    const ariaSort = sort && sort.key === c.key ? sort.dir : "none";
    const label = c.sort ? `<button type="button" class="sort" data-sort="${esc(c.key)}">${c.label}${sortIcon(ariaSort)}</button>` : c.label;
    return `<th scope="col" class="${cls(c)}"${c.sort ? ` aria-sort="${ariaSort}"` : ""}>${label}</th>`;
  }).join("");
  const body = sortedIndices(entry).map((index) => {
    const row = spec.rows[index];
    const cells = spec.columns.map((c) => `<td class="${cls(c)}">${c.cell(row)}</td>`).join("");
    return `<tr class="${spec.rowClass ? spec.rowClass(row) : ""}" data-row="${index}" ${spec.rowAttrs ? spec.rowAttrs(row) : ""}>${cells}</tr>`;
  }).join("");
  const caption = spec.caption ? `<caption>${spec.caption}</caption>` : "";
  return `<table class="data">${caption}<thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
}

const sortIcon = (ariaSort) => icon(ariaSort === "ascending" ? "chevron-up" : ariaSort === "descending" ? "chevron-down" : "chevrons");

// Put the rendered table in the entry's current order: rows are moved (data-row is the row's index
// in spec.rows), and each header's aria-sort and arrow updated in place.
function applySort(container, entry) {
  const tbody = container.querySelector("tbody");
  const rowElements = new Map([...tbody.rows].map((tr) => [Number(tr.dataset.row), tr]));
  sortedIndices(entry).forEach((index) => tbody.append(rowElements.get(index)));
  container.querySelectorAll("th[aria-sort]").forEach((th) => {
    const button = th.querySelector("button[data-sort]");
    const ariaSort = entry.sort?.key === button.dataset.sort ? entry.sort.dir : "none";
    th.setAttribute("aria-sort", ariaSort);
    button.lastElementChild.outerHTML = sortIcon(ariaSort); // the arrow comes after the label
  });
}

export function wireTables(root) {
  // Arrow keys scroll a wide criteria table on narrow screens. At full width there is
  // nothing to scroll, so Tab goes directly to the next action.
  root.querySelectorAll(".table-wrap:has(> table.criteria)").forEach((region) => {
    if (region.dataset.scrollWired) return;
    region.dataset.scrollWired = "1";
    const fit = () => { region.tabIndex = region.scrollWidth > region.clientWidth ? 0 : -1; };
    const observer = new ResizeObserver(() => region.isConnected ? fit() : observer.disconnect());
    observer.observe(region);
    observer.observe(region.firstElementChild);
    fit();
  });
  root.querySelectorAll("[data-table]").forEach((container) => {
    const id = container.dataset.table;
    if (bound.has(container) || !unwired.has(id)) return;
    const entry = unwired.get(id);
    unwired.delete(id);
    bound.set(container, entry);
    container.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-sort]");
      if (!button) return;
      event.stopPropagation();
      const key = button.dataset.sort;
      const column = entry.spec.columns.find((c) => c.key === key);
      const first = column.firstDir ?? (column.numeric ? "descending" : "ascending");
      entry.sort = entry.sort && entry.sort.key === key
        ? { key, dir: entry.sort.dir === "ascending" ? "descending" : "ascending" }
        : { key, dir: first };
      applySort(container, entry);
    });
    // Edge shadows show that the table scrolls sideways, and under the sticky first column.
    const edges = () => {
      container.classList.toggle("scrolled", container.scrollLeft > 0);
      container.classList.toggle("more-right", container.scrollLeft + container.clientWidth < container.scrollWidth - 1);
    };
    container.addEventListener("scroll", edges, { passive: true });
    new ResizeObserver(edges).observe(container);
    edges();
  });
}

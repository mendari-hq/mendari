"use strict";

// Data tables: a semantic <table> with a caption, a sticky header, right-aligned tabular numbers,
// keyboard-operable sorting (aria-sort), an optional rank column and an optional sticky first
// column. Views build HTML with tableHtml(spec), put it in the page, then call wireTables(root).
// Row listeners belong on the container (event delegation): sorting re-renders the rows.
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
//   rank,                    add a "Rank" column numbered in the current order; 1-3 get a chip
//   rowClass(row), rowAttrs(row),   trusted strings
//   stickyFirst,             keep the first column in view when the table scrolls sideways
//   limitHeight,             scroll inside a max height, so the sticky header stays visible
// }

const TABLES = new Map(); // id -> { spec, sort: { key, dir } | null }

function tableHtml(spec) {
  TABLES.set(spec.id, { spec, sort: null });
  const classes = ["table-scroll", spec.stickyFirst ? "sticky-first" : "", spec.limitHeight ? "limit" : ""].filter(Boolean).join(" ");
  return `<div class="${classes}" data-table="${esc(spec.id)}">${tableInner(spec.id)}</div>`;
}

function sortedRows(entry) {
  const { spec, sort } = entry;
  const rows = [...spec.rows];
  if (!sort) return spec.defaultCompare ? rows.sort(spec.defaultCompare) : rows;
  const column = spec.columns.find((c) => c.key === sort.key);
  const sign = sort.dir === "ascending" ? 1 : -1;
  const value = (row) => column.sort(row);
  return rows.sort((a, b) => {
    const [x, y] = [value(a), value(b)];
    if (x === y) return spec.defaultCompare ? spec.defaultCompare(a, b) : 0;
    if (x === null || x === undefined) return 1; // missing values last, either direction
    if (y === null || y === undefined) return -1;
    return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * sign;
  });
}

function tableInner(id) {
  const entry = TABLES.get(id);
  const { spec, sort } = entry;
  const rows = sortedRows(entry);
  const cls = (c) => [c.numeric ? "num" : "", c.className ?? ""].filter(Boolean).join(" ");
  const header = spec.columns.map((c) => {
    const ariaSort = sort && sort.key === c.key ? sort.dir : "none";
    const label = c.sort
      ? `<button type="button" class="sort" data-sort="${esc(c.key)}">${c.label}${icon(ariaSort === "ascending" ? "chevron-up" : ariaSort === "descending" ? "chevron-down" : "chevrons")}</button>`
      : c.label;
    return `<th scope="col" class="${cls(c)}"${c.sort ? ` aria-sort="${ariaSort}"` : ""}>${label}</th>`;
  }).join("");
  const body = rows.map((row, index) => {
    const rank = spec.rank ? `<td class="num rank-cell"><span class="rank${index < 3 ? " top" : ""}">${index + 1}</span></td>` : "";
    const cells = spec.columns.map((c) => `<td class="${cls(c)}">${c.cell(row)}</td>`).join("");
    return `<tr class="${spec.rowClass ? spec.rowClass(row) : ""}" ${spec.rowAttrs ? spec.rowAttrs(row) : ""}>${rank}${cells}</tr>`;
  }).join("");
  const caption = spec.caption ? `<caption>${spec.caption}</caption>` : "";
  return `<table class="data">${caption}<thead><tr>${spec.rank ? `<th scope="col" class="num rank-cell">Rank</th>` : ""}${header}</tr></thead><tbody>${body}</tbody></table>`;
}

function wireTables(root) {
  root.querySelectorAll("[data-table]").forEach((container) => {
    if (container.dataset.wired) return;
    container.dataset.wired = "1";
    container.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-sort]");
      if (!button) return;
      event.stopPropagation();
      const entry = TABLES.get(container.dataset.table);
      const key = button.dataset.sort;
      const column = entry.spec.columns.find((c) => c.key === key);
      const first = column.firstDir ?? (column.numeric ? "descending" : "ascending");
      entry.sort = entry.sort && entry.sort.key === key
        ? { key, dir: entry.sort.dir === "ascending" ? "descending" : "ascending" }
        : { key, dir: first };
      container.innerHTML = tableInner(container.dataset.table);
      container.querySelector(`button[data-sort="${CSS.escape(key)}"]`).focus();
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

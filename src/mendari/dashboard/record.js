// SPDX-License-Identifier: Apache-2.0
// A case's input as a reader sees it, whatever pack it is for: a message as its text; any other
// record as its fields (chips), its lists (a table when they hold records) and its long texts.
// Everything in it is what someone wrote, so every value and key is escaped: shown as text, never
// run as markup. The decision views show stored inputs with it (`inputView`, pack.js), and the
// labelling screen a hidden case's (labelling.js).

import { esc } from "./ui.js";

export function recordHtml(input) {
  if (input?.message) {
    const m = input.message;
    return `${m.subject ? `<div class="mt-2"><b>${esc(m.subject)}</b></div>` : ""}<div class="message">${esc(m.body ?? "")}</div>`;
  }
  const fields = [], texts = [], lists = [];
  const shown = (v) => (v !== null && typeof v === "object" ? JSON.stringify(v) : String(v));
  for (const [key, value] of Object.entries(input ?? {})) {
    if (Array.isArray(value)) lists.push([key, value]);
    else if (value !== null && typeof value === "object") Object.entries(value).forEach(([k, v]) => fields.push([`${key}.${k}`, shown(v)]));
    else if (typeof value === "string" && value.length > 60) texts.push([key, value]);
    else fields.push([key, shown(value)]);
  }
  const listView = ([key, items]) => {
    if (!items.length) return `<div class="list-name">${esc(key)} <span class="muted">none</span></div>`;
    if (!items.every((i) => i !== null && typeof i === "object")) return `<div class="list-name">${esc(key)} <span>${items.map((i) => esc(shown(i))).join(", ")}</span></div>`;
    const columns = [...new Set(items.flatMap((i) => Object.keys(i)))];
    return `<div class="list-name">${esc(key)} <span class="muted">${items.length}</span></div><div class="table-wrap"><table class="compact">
      <tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>
      ${items.map((i) => `<tr>${columns.map((c) => `<td>${esc(i[c] === undefined ? "" : shown(i[c]))}</td>`).join("")}</tr>`).join("")}</table></div>`;
  };
  return `<div class="record">
    ${fields.length ? `<div class="chips">${fields.map(([k, v]) => `<span class="chip">${esc(k)} = ${esc(v)}</span>`).join("")}</div>` : ""}
    ${lists.map(listView).join("")}
    ${texts.map(([k, v]) => `<div class="list-name">${esc(k)}</div><div class="message">${esc(v)}</div>`).join("")}
  </div>`;
}

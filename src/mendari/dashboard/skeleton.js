// SPDX-License-Identifier: Apache-2.0
// Loading placeholders shaped like each view (title, intro, then its stats, cards or rows), so the
// page keeps its layout while data arrives instead of collapsing to one line and jumping back.
// Purely visual: the view is marked aria-busy while it loads, and the words say "Loading".

const bar = (kind) => `<div class="sk sk-${kind}"></div>`;
const repeat = (n, html) => html.repeat(n);
const rows = (n) => `<div class="sk-rows">${repeat(n, bar("row"))}</div>`;

const SHAPES = {
  overview: () => `<div class="sk-stats">${repeat(4, bar("stat"))}</div>${bar("strip")}${bar("chart")}`,
  review: () => `${bar("toolbar")}${repeat(3, bar("card"))}`,
  audit: () => `${bar("toolbar")}${rows(8)}`,
  monitoring: () => `${bar("toolbar")}<div class="sk-stats">${repeat(4, bar("stat"))}</div>${rows(5)}`,
  evals: () => `${bar("tabs")}${bar("toolbar")}${rows(6)}`,
  feedback: () => `${bar("toolbar")}${repeat(3, bar("card"))}`,
  card: () => `${repeat(3, `${bar("heading")}${repeat(3, bar("line"))}`)}`,
  try: () => `${bar("toolbar")}${bar("form")}`,
  patient: () => `${bar("toolbar")}${repeat(3, bar("card"))}`,
  access: () => `${bar("form")}`,
  admin: () => `${bar("tabs")}${bar("toolbar")}${rows(5)}`,
  labelling: () => `${rows(2)}${bar("form")}`,
};

export function skeleton(view) {
  const shape = SHAPES[view] ?? (() => rows(4));
  return `<div class="skeleton" aria-hidden="true">${bar("title")}${bar("intro")}${shape()}</div>
    <p class="sr-only">Loading…</p>`;
}

// Boot: theme, time zone, packs, then the first view. Imports every view so it registers.

import "./overview.js";
import "./review.js";
import "./audit.js";
import "./evals.js";
import "./feedback.js";
import "./card.js";
import "./try.js";
import { onHashChange, render } from "./router.js";
import { setupTheme } from "./theme.js";
import { $, api, DEFAULT_TIMEZONE, esc, focusHeading, loadPack, shortFormatFor, state, storageGet, storageSet, timeFormatFor, watchEdges } from "./ui.js";

async function boot() {
  setupTheme();
  $("#skip-link").addEventListener("click", () => focusHeading($("main h1") ?? $("main"), { scroll: false }));
  watchEdges($(".nav"));
  const health = await api("/api/health").catch(() => null);
  try {
    state.timeFormat = timeFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
    state.shortFormat = shortFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
    state.timezone = health?.timezone ?? DEFAULT_TIMEZONE;
  } catch { /* an unknown zone name: keep the default */ }
  state.packs = await api("/api/packs");
  const select = $("#pack-select");
  if (!state.packs.length) {
    $("#view").innerHTML = `<div class="panel empty">No Decision Packs found on the search path.</div>`;
    return;
  }
  select.innerHTML = state.packs.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} ${esc(p.version)}</option>`).join("");
  const saved = storageGet("oh.pack", state.packs[0].id);
  const initial = state.packs.some((p) => p.id === saved) ? saved : state.packs[0].id;
  select.value = initial;
  await loadPack(initial);
  select.addEventListener("change", async () => {
    storageSet("oh.pack", select.value);
    await loadPack(select.value);
    render();
  });
  window.addEventListener("hashchange", onHashChange);
  render();
}

boot().catch((error) => {
  $("#view").innerHTML = `<div class="panel">Could not reach the OpenHealth API: ${esc(error.message)}</div>`;
});

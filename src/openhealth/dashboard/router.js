// Views and routes ("#view" or "#view/param"): rendering, focus on navigation, the review badge.

import { wireCharts } from "./charts.js";
import { closeDrawer } from "./drawer.js";
import { wireTables } from "./table.js";
import { $, api, esc, focusHeading, state } from "./ui.js";

export const views = {};

// Routes are "#view" or "#view/param": "#audit/<decision id>" opens a record, "#evals/compare"
// picks an evaluation view.
export function route() {
  const [name = "overview", ...params] = (location.hash || "#overview").slice(1).split("/");
  return { view: views[name] ? name : "overview", params: params.filter(Boolean).map(decodeURIComponent) };
}

// A route change inside the current view (opening a record, switching a tab) updates that view in
// place, keeping its scroll and focus; anything else renders the new view.
export function onHashChange() {
  const { view, params } = route();
  if (view === state.view && views[view].update) return views[view].update(params);
  return render({ navigated: true });
}

// `navigated`: the user moved to another view, so it starts at the top with its heading focused
// (screen readers then announce the new view once, instead of reading the whole page). A view that
// opens at a section (e.g. "#card/policy") returns that section's heading instead.
export async function render({ navigated = false } = {}) {
  const { view, params } = route();
  state.view = view;
  closeDrawer({ restoreFocus: false, closedByUser: false });
  document.querySelectorAll(".nav a[data-view]").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === view);
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  keepActiveSectionInView();
  const main = $("#view");
  main.innerHTML = `<div class="empty">Loading…</div>`;
  let target = null;
  try {
    target = await views[view](main, params);
  } catch (error) {
    main.innerHTML = `<div class="panel error-state" role="alert"><h1>Could not load this view</h1>
      <p class="muted">${esc(error.message)}</p><button type="button" class="primary" id="retry">Try again</button></div>`;
    $("#retry").addEventListener("click", () => render({ navigated: true }));
  }
  if (target) focusHeading(target); // a link to a section lands there, on first load too
  else if (navigated) {
    window.scrollTo(0, 0);
    focusHeading(main.querySelector("h1"), { scroll: false });
  }
  refreshReviewCount();
}

// On a phone the section list scrolls sideways: the current section is scrolled into it, and edge
// shadows show that there is more to either side.
function keepActiveSectionInView() {
  const nav = $(".nav");
  const active = nav.querySelector("a.active");
  if (!active || nav.scrollWidth <= nav.clientWidth) return;
  const [a, n] = [active.getBoundingClientRect(), nav.getBoundingClientRect()];
  if (a.left < n.left) nav.scrollLeft += a.left - n.left - 8;
  else if (a.right > n.right) nav.scrollLeft += a.right - n.right + 8;
}

// Sorting, sticky edges and chart drawing all attach after a view puts its HTML in the page.
export const wire = (root) => {
  wireTables(root);
  wireCharts(root);
};

export async function refreshReviewCount() {
  try {
    const pending = await api(`/api/review?pack_id=${encodeURIComponent(state.packId)}&limit=500`);
    $("#review-count").textContent = pending.length || "";
    keepActiveSectionInView(); // the count changes the strip's width
  } catch { /* the badge is optional */ }
}

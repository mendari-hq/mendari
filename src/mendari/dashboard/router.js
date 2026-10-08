// SPDX-License-Identifier: Apache-2.0
// Views and routes ("#view" or "#view/param"): rendering, focus on navigation, the review badge.

import { wireCharts } from "./charts.js";
import { closeDrawer } from "./drawer.js";
import { skeleton } from "./skeleton.js";
import { wireTables } from "./table.js";
import { $, api, esc, focusHeading, state } from "./ui.js";

export const views = {};

// Routes are "#view" or "#view/param": "#audit/<decision id>" opens a record, "#evals/compare"
// picks an evaluation view, "#patient/<launch id>" the patient of that EHR launch. A browser that
// only launched (no one signed in) has the patient view and nothing else, still pinned to the
// window's own launch when the address no longer names it.
export function route() {
  const [name = "overview", ...params] = (location.hash || "#overview").slice(1).split("/");
  const named = params.filter(Boolean).map(decodeURIComponent);
  // Memberships mode, a person acting for no organization: the access page (access.js) and
  // nothing else, beside a launch's patient if the browser holds one; reached no other way.
  if (state.noAccess && (!state.launchOnly || name === "access")) return { view: "access", params: [state.noAccess] };
  if (state.launchOnly && (name !== "patient" || !named.length)) {
    return { view: "patient", params: state.launchPin ? [state.launchPin] : [] };
  }
  // A view someone may not use (`available()` says no: the administrators' Access view in claims or
  // open mode, or for anyone but an administrator) is not there: the overview, with no request.
  const shown = views[name] && name !== "access" && (views[name].available?.() ?? true);
  return { view: shown ? name : "overview", params: named };
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
// Renders run one after another, so the last one asked for is the one left on the page (a view
// that loads slowly can't paint over the view that was asked for after it).
let rendering = Promise.resolve();

export function render(options) {
  rendering = rendering.then(() => paint(options), () => paint(options));
  return rendering;
}

async function paint({ navigated = false } = {}) {
  const { view, params } = route();
  views[state.view]?.leave?.(); // e.g. the review queue sends overrides still in their undo window
  state.view = view;
  closeDrawer({ restoreFocus: false, closedByUser: false });
  document.querySelectorAll(".nav a[data-view]").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === view);
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  keepActiveSectionInView();
  const main = $("#view");
  main.setAttribute("aria-busy", "true");
  main.classList.remove("view-enter");
  main.innerHTML = skeleton(view);
  let target = null;
  try {
    target = await views[view](main, params);
  } catch (error) {
    main.innerHTML = `<div class="panel error-state" role="alert"><h1>Could not load this view</h1>
      <p class="muted">${esc(error.message)}</p><button type="button" class="primary" id="retry">Try again</button></div>`;
    $("#retry").addEventListener("click", () => render({ navigated: true }));
  } finally {
    main.removeAttribute("aria-busy");
  }
  // The content fades in where the skeleton was, rather than cutting over it. The reflow restarts
  // the animation when the class comes straight back (the same view rendered again).
  void main.offsetWidth;
  main.classList.add("view-enter");
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
  if (state.launchOnly || state.noAccess) return; // no review queue for a launch, or no access
  try {
    const pending = await api(`/api/review?pack_id=${encodeURIComponent(state.packId)}&limit=500`);
    $("#review-count").textContent = pending.length || "";
    keepActiveSectionInView(); // the count changes the strip's width
  } catch { /* the badge is optional */ }
}

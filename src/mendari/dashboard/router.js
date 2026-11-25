// SPDX-License-Identifier: Apache-2.0
// Views and routes ("#view" or "#view/param"): rendering, focus on navigation, the review badge.

import { wireCharts } from "./charts.js";
import { closeDrawer } from "./drawer.js";
import { wireSimulators } from "./report.js";
import { skeleton } from "./skeleton.js";
import { wireTables } from "./table.js";
import { $, api, esc, focusHeading, packLoaded, state } from "./ui.js";

export const views = {};

// Routes are "#view" or "#view/param": "#audit/<decision id>" opens a record, "#evals/compare"
// picks an evaluation view, "#patient/<launch id>" the patient of that EHR launch. A browser that
// only launched (no one signed in) has the patient view and nothing else, still pinned to the
// window's own launch when the address no longer names it.
const safeDecode = (s) => {
  try { return decodeURIComponent(s); } catch { return s; }
};

export function route() {
  const [name = "overview", ...params] = (location.hash || "#overview").slice(1).split("/");
  const named = params.filter(Boolean).map(safeDecode);
  // Memberships mode, a person acting for no organization: the access page (access.js) and
  // nothing else, beside a launch's patient if the browser holds one; reached no other way.
  if (state.noAccess && (!state.launchOnly || name === "access")) return { view: "access", params: [state.noAccess] };
  if (state.launchOnly && (name !== "patient" || !named.length)) {
    return { view: "patient", params: state.launchPin ? [state.launchPin] : [] };
  }
  // A labeller (a grader of written output), a registry reviewer (LP-19f), or both, and nothing
  // else: their screens, the first of them for any other address.
  const only = [state.labelOnly && state.written && "written", state.reviewOnly && "registry-review", state.storeOnly && "store"].filter(Boolean);
  if (only.length) return only.includes(name) ? { view: name, params: named } : { view: only[0], params: [] };
  // A view someone may not use (`available()` says no: the administrators' Access view in open
  // mode, or for anyone but an administrator who signed in) is not there: the overview, with no
  // request.
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
// Renders are sequenced and superseded: if a newer render is requested while an earlier one is
// waiting on the network, intermediate renders are skipped so rapid clicks land on the final view
// immediately and a slow response never paints over a newer route.
let renderSeq = 0;
let rendering = Promise.resolve();

export function render(options) {
  const seq = ++renderSeq;
  rendering = rendering.then(() => paint(options, seq), () => paint(options, seq));
  return rendering;
}

async function paint({ navigated = false } = {}, seq = renderSeq) {
  if (seq !== renderSeq) return; // superseded before starting
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
    if (!views[view].packless) await packLoaded(); // the skeleton shows meanwhile
    if (seq !== renderSeq) return;
    target = await views[view](main, params);
  } catch (error) {
    if (seq !== renderSeq) return;
    main.innerHTML = `<div class="panel error-state" role="alert"><h1>Could not load this view</h1>
      <p class="muted">${esc(error.message)}</p><button type="button" class="primary" id="retry">Try again</button></div>`;
    $("#retry").addEventListener("click", () => render({ navigated: true }));
  } finally {
    if (seq === renderSeq) main.removeAttribute("aria-busy");
  }
  if (seq !== renderSeq) return;
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

// Sorting, sticky edges, chart drawing and policy simulators attach after a view puts its HTML in the page.
export const wire = (root) => {
  wireTables(root);
  wireCharts(root);
  wireSimulators(root);
};

let reviewCountSeq = 0;

export async function refreshReviewCount() {
  if (state.launchOnly || state.noAccess || state.labelOnly || state.reviewOnly || state.storeOnly) return; // no review queue for these
  const packId = state.packId;
  const seq = ++reviewCountSeq;
  try {
    const [pending, summary] = await Promise.all([
      api(`/api/review?pack_id=${encodeURIComponent(packId)}&limit=500`),
      state.packs.length > 1 ? api("/api/review/summary").catch(() => null) : Promise.resolve(null),
    ]);
    if (seq !== reviewCountSeq || state.packId !== packId) return;
    $("#review-count").textContent = pending.length || "";
    keepActiveSectionInView(); // the count changes the strip's width
    if (Array.isArray(summary)) {
      state.packQueue = new Map(summary.map((row) => [row.pack_id, row]));
      updateCrossPackStat(summary, packId);
    }
  } catch { /* the badge is optional */ }
}

function updateCrossPackStat(summary, currentPackId) {
  const pill = $("#other-stat-pill");
  if (!pill) return;
  const others = summary.filter((row) => row.pack_id !== currentPackId && row.stat > 0);
  const totalStat = others.reduce((sum, row) => sum + row.stat, 0);
  if (!totalStat) {
    pill.hidden = true;
    return;
  }
  const first = others[0];
  const packName = state.packs.find((p) => p.id === first.pack_id)?.name ?? first.pack_id;
  pill.textContent = others.length === 1
    ? `${totalStat} stat in ${packName}`
    : `${totalStat} stat in other agents`;
  pill.title = `Open urgent review queue for ${packName}`;
  pill.onclick = () => {
    const select = $("#pack-select");
    if (select) {
      select.value = first.pack_id;
      select.dispatchEvent(new Event("change"));
    }
    location.hash = "#review";
  };
  pill.hidden = false;
}

// SPDX-License-Identifier: Apache-2.0
// Boot: theme, time zone, packs, then the first view. Imports every view so it registers.

import { chooseOrganization } from "./access.js";
import { acknowledgeNotice } from "./notice.js";
import "./overview.js";
import "./review.js";
import "./audit.js";
import "./evals.js";
import "./monitoring.js";
import "./feedback.js";
import "./card.js";
import "./try.js";
import "./patient.js";
import "./admin.js";
import "./system.js";
import "./registry.js";
import "./labelling.js";
import "./written.js";
import "./evalsets.js";
import "./store.js";
import "./agents.js";
import { defaultPack, packOptions } from "./pack.js";
import { setupPalette } from "./palette.js";
import { onHashChange, render, route, views } from "./router.js";
import { setupSettings } from "./settings.js";
import { skeleton } from "./skeleton.js";
import { setupTheme } from "./theme.js";
import { $, api, DEFAULT_TIMEZONE, esc, focusHeading, loadPack, roleName, shortFormatFor, state, storageGet, storageSet, timeFormatFor, watchEdges } from "./ui.js";

async function boot() {
  setupTheme();
  setupSettings();
  // The first view's skeleton shows while the packs load, not an empty page.
  $("#view").setAttribute("aria-busy", "true");
  $("#view").innerHTML = skeleton(route().view);
  $("#skip-link").addEventListener("click", () => focusHeading($("main h1") ?? $("main"), { scroll: false }));
  watchEdges($(".nav"));
  const health = await api("/api/health").catch(() => null);
  state.phiMode = health?.phi_mode ?? "off";
  state.backendServices = health?.backend_services ?? null; // the Access view's Clients tab
  state.checkpointKey = health?.checkpoint_key ?? null; // the audit trail's signed checkpoint
  state.evalsets = Boolean(health?.evalset_keys); // hidden evaluation sets are on (evalsets.js)
  state.registry = Boolean(health?.registry); // the pack registry is served here (registry.js)
  try {
    state.timeFormat = timeFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
    state.shortFormat = shortFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
    state.timezone = health?.timezone ?? DEFAULT_TIMEZONE;
  } catch { /* an unknown zone name: keep the default */ }
  // A SMART launch from the EHR leaves a session: the patient's view joins the sections. With
  // sign-in on and no one signed in, the launch is all this browser has: the patient view alone,
  // with the packs the launch names (the rest of the API would refuse it).
  // Who is signed in decides what this window may show and which organization it acts for: if
  // that cannot be read, nothing is shown (the error below), never a guess.
  const [session, me] = await Promise.all([api("/api/smart/session").catch(() => null), api("/api/auth/me")]);
  showSignedIn(me);
  // Memberships mode (ADR-0008): a person acting for no organization (none chosen yet, or theirs
  // ended) chooses one or asks to join one; one whose roles grant nothing is told so. Neither
  // has sections or packs, nor a notice to acknowledge (the server would refuse them all).
  const person = state.accounts === "memberships" && me?.signed_in && me.principal.kind === "user";
  state.store = Boolean(me?.store); // an organization's pack store is served here (store.js)
  if (person) {
    if (me.principal.org === null) state.noAccess = state.memberships.length ? "choose" : "none";
    else if (!me.principal.permissions.length) state.noAccess = "nothing";
    // A pack author and nothing else (LP-20f): the store's screens where it is served (LP-20j),
    // else the access page saying what the role is for.
    else if (me.principal.permissions.every((p) => p === "author")) {
      if (state.store) state.storeOnly = true;
      else state.noAccess = "author";
    }
    // A labeller (LP-11) or a reviewer of the pack registry (LP-19f), or both, and nothing
    // else: no packs to list, no notice, no review queue; just their screens. The review screen
    // where the server serves it (this organization is the registry's review organization), and
    // labelling where it does (below).
    else if (me.principal.permissions.every((p) => p === "label" || p === "registry_review")) {
      state.reviewOnly = me.principal.permissions.includes("registry_review") && Boolean(me.principal.registry_review_org);
      if (me.principal.permissions.includes("label")) state.noAccess = "label";
      else if (!state.reviewOnly) state.noAccess = "nothing";
    }
  }
  // Holding `label`, they label here where the deployment serves the labelling routes
  // (memberships mode with the evaluation-set keys, LP-11j): their counts say whether it does,
  // read once (they open no case). A labeller and nothing else then sees the labelling screen
  // alone (labelling.js); without the routes, the access page says labelling is not on here.
  state.labelling = Boolean(person && me.principal.org && me.principal.permissions.includes("label"))
    && await api("/api/labelling").then(() => true, () => false);
  // And they grade what an agent wrote (ADR-0025, written.js) where the deployment serves a
  // grader's routes (memberships mode under PHI mode encrypt) and a round holds work of
  // theirs, waiting or done: their counts say both, read once. Those routes need no key of
  // their own, so a labeller no round was ever assigned to sees what they saw before.
  state.written = Boolean(person && me.principal.org && me.principal.permissions.includes("label"))
    && await api("/api/written/labelling").then((counts) => counts.sets.length > 0, () => false);
  if (state.noAccess === "label" && (state.labelling || state.written)) [state.noAccess, state.labelOnly] = [null, true];
  if (state.noAccess === "label" && state.reviewOnly) state.noAccess = null; // no labelling here: reviewing
  // Labelling and reviewing the registry are not notice-gated.
  if (me?.signed_in && !state.noAccess && !state.labelOnly && !state.reviewOnly && !state.storeOnly) await acknowledgeNotice();
  state.smart = session?.active ? session : null;
  // An EHR launch in a browser whose person may do nothing here (no organization, roles that
  // grant nothing, or labelling alone): the launch's view, and, holding memberships, choosing
  // one of them.
  state.launchOnly = Boolean(state.smart && me?.mode === "sign-in" && (!me.signed_in || state.noAccess || state.labelOnly || state.reviewOnly || state.storeOnly));
  if (state.launchOnly && state.noAccess !== "choose") state.noAccess = null;
  if (state.launchOnly) [state.labelOnly, state.reviewOnly, state.storeOnly] = [false, false, false];
  showLaunch();
  showOrganization();
  showAdministration();
  showSystem();
  showEvalsets();
  showLabelling();
  showWritten();
  showRegistryReview();
  showStore();
  const pickPack = async (packId) => {
    if (!state.packs.some((p) => p.id === packId)) return;
    await switchPack(packId).catch(() => null); // the view says it (router.js), with Try again
    render();
  };
  setupPalette({ onSelectPack: (packId) => (packId === state.packId ? undefined : pickPack(packId)) });
  if ((state.noAccess || state.labelOnly || state.reviewOnly || state.storeOnly) && !state.launchOnly) {
    $(".pack-picker").hidden = true;
    // The docs stay; a labeller or a registry reviewer and nothing else keeps their screens'
    // links.
    const kept = [state.labelOnly && state.labelling && "labelling", state.labelOnly && state.written && "written", state.reviewOnly && "registry-review", state.storeOnly && "store"];
    document.querySelectorAll(".nav a[data-view]").forEach((a) => (a.hidden = !kept.includes(a.dataset.view)));
    syncNavGroups();
    window.addEventListener("hashchange", onHashChange);
    render();
    return;
  }
  syncNavGroups();
  state.packs = state.launchOnly ? state.smart.packs ?? [] : await api("/api/agents");
  const select = $("#pack-select");
  if (!state.packs.length) {
    $("#view").removeAttribute("aria-busy");
    $("#view").innerHTML = `<div class="panel empty">No governed agents found on the search path.</div>`;
    return;
  }
  // Grouped by where each pack comes from when more than one origin is served (pack.js).
  select.innerHTML = packOptions(state.packs);
  const known = (id) => state.packs.some((p) => p.id === id);
  const first = defaultPack(state.packs);
  const saved = storageGet("oh.pack", first);
  switchPack([packInAddress(), saved].find(known) ?? first); // a view that reads it waits (router.js)
  select.addEventListener("change", () => pickPack(select.value));
  // Back and Forward can cross a pack change. The address then names another pack than the one
  // shown, and the browser fires only popstate (hashchange needs everything but the hash to match).
  window.addEventListener("popstate", async () => {
    const wanted = packInAddress();
    if (wanted === state.packId || !known(wanted)) return;
    await switchPack(wanted).catch(() => null);
    render({ navigated: true });
  });
  window.addEventListener("hashchange", onHashChange);
  render();
}

// The agent travels in the address ("?agent=mendari/portal-message-agent#review"), so a shared or
// bookmarked link opens the agent it was copied from, and a record link opens over that agent's
// list. Without one, the agent last chosen here. Hash links keep the query, so no link builds it.
// The address said "?pack=" before: a link made then (a bookmark, a CDS card, an EHR launch) is
// still read, and the address is written as "?agent=" from there on.
function packInAddress() {
  const query = new URLSearchParams(location.search);
  return query.get("agent") ?? query.get("pack");
}

function switchPack(packId) {
  $("#pack-select").value = packId;
  storageSet("oh.pack", packId);
  history.replaceState(history.state, "", `?agent=${encodeURIComponent(packId).replaceAll("%2F", "/")}${location.hash}`);
  return loadPack(packId);
}

// The patient view's link names the launch the window shows ("#patient/<launch id>"): the one in
// the address when the EHR opened this window, else the browser's launch now. A launch alone sees
// no other section.
function showLaunch() {
  const link = $('.nav a[data-view="patient"]');
  link.hidden = !state.smart;
  const { view, params } = route();
  const pinned = (view === "patient" && params[0]) || state.smart?.launch_id;
  if (pinned) link.setAttribute("href", `#patient/${encodeURIComponent(pinned)}`);
  const choose = $('.nav a[data-view="access"]'); // memberships mode: a launch, and none chosen
  if (state.launchOnly) document.querySelectorAll(".nav a").forEach((a) => (a.hidden = a !== link && !(a === choose && state.noAccess)));
}

// With sign-in on, the bar names who is signed in (and their role) and offers signing out. In
// memberships mode, also the organization they act for (or that they act for none).
function showSignedIn(me) {
  state.me = me?.principal ?? null;
  state.accounts = me?.accounts ?? "claims";
  state.memberships = me?.memberships ?? [];
  const box = $("#signed-in");
  box.hidden = !me?.signed_in;
  if (!me?.signed_in) return;
  const { name, org } = me.principal;
  const roles = me.principal.roles.map(roleName).join(", "); // by their names, never their keys
  const held = state.memberships.find((m) => m.org === org);
  let shown = `${name} · ${roles}`;
  if (state.accounts === "memberships" && me.principal.kind === "user") {
    // The organization, unless the switcher shows it (several): the bar stays on one line.
    const named = state.memberships.length > 1 ? null : held?.name ?? org;
    shown = org === null ? `${name} · no organization` : [name, named, roles].filter(Boolean).join(" · ");
  }
  $("#signed-in-name").textContent = shown;
  $("#signed-in-name").title = `The audit trail records you as ${me.principal.actor}`;
  $("#sign-out").addEventListener("click", async () => {
    await api("/auth/logout", { method: "POST" }).catch(() => null);
    location.assign("/auth/login");
  });
}

// Memberships mode, a person in one of several organizations: switch between them, with the
// Switch button (choosing in the list alone changes nothing). The choice is this browser's;
// other windows still showing the one before are refused until reloaded.
function showOrganization() {
  if (state.accounts !== "memberships" || !state.me?.org || state.memberships.length < 2) return;
  const select = $("#org-select");
  select.replaceChildren(...state.memberships.map((m) => {
    const option = document.createElement("option");
    option.value = m.org;
    option.textContent = m.name; // text, never markup: a name is what an administrator typed
    return option;
  }));
  select.value = state.me.org;
  const form = $("#org-form");
  form.hidden = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const wanted = select.value;
    if (wanted === state.me.org) return;
    await chooseOrganization(wanted);
    select.value = state.me.org; // refused: the one still chosen
  });
}

// An administrator who signed in, in either accounts mode: their organization's API keys and
// Backend Services clients and, in memberships mode, its people (admin.js). Never in open mode.
function showAdministration() {
  $('.nav a[data-view="admin"]').hidden = state.launchOnly || Boolean(state.noAccess) || !views.admin.available();
}

// The deployment's operators (system.js): its checks, and the actions that needed a terminal.
function showSystem() {
  $('.nav a[data-view="system"]').hidden = state.launchOnly || Boolean(state.noAccess) || !views.system.available();
}

// An administrator who signed in, in memberships mode where hidden evaluation sets are on (LP-11n):
// their organization's sets (evalsets.js). Never a labeller, another role or claims mode.
function showEvalsets() {
  $('.nav a[data-view="evalsets"]').hidden = state.launchOnly || Boolean(state.noAccess) || !views.evalsets.available();
}

// Someone holding `label` where the labelling routes are served (LP-11m): the labelling screen.
function showLabelling() {
  $('.nav a[data-view="labelling"]').hidden = Boolean(state.noAccess) || !views.labelling.available();
}

// And where a grader's routes are served (ADR-0025): the grader's screen (written.js).
function showWritten() {
  $('.nav a[data-view="written"]').hidden = Boolean(state.noAccess) || !views.written.available();
}

// A reviewer of the pack registry, in its review organization, where the registry is served here
// (LP-19f): its review screen (registry.js).
function showRegistryReview() {
  $('.nav a[data-view="registry-review"]').hidden = state.launchOnly || Boolean(state.noAccess) || !views["registry-review"].available();
}

// A pack author or someone who may promote, where an organization's pack store is served (LP-20j):
// the store's screens (store.js).
function showStore() {
  $('.nav a[data-view="store"]').hidden = state.launchOnly || Boolean(state.noAccess) || !views.store.available();
}

// Hide each desktop pillar header whenever every link inside that pillar is hidden.
function syncNavGroups() {
  document.querySelectorAll(".nav-section-label[data-nav-group]").forEach((label) => {
    const group = label.dataset.navGroup;
    const links = [...document.querySelectorAll(`.nav a[data-nav-section="${group}"]`)];
    label.hidden = links.every((a) => a.hidden);
  });
}

boot().catch((error) => {
  $("#view").removeAttribute("aria-busy");
  $("#view").innerHTML = `<div class="panel">Could not reach the Mendari API: ${esc(error.message)}</div>`;
});

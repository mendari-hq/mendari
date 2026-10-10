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
import { defaultPack, packOptions } from "./pack.js";
import { onHashChange, render, route, views } from "./router.js";
import { setupSettings } from "./settings.js";
import { skeleton } from "./skeleton.js";
import { setupTheme } from "./theme.js";
import { $, api, DEFAULT_TIMEZONE, esc, focusHeading, loadPack, shortFormatFor, state, storageGet, storageSet, timeFormatFor, watchEdges } from "./ui.js";

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
  if (person) {
    if (me.principal.org === null) state.noAccess = state.memberships.length ? "choose" : "none";
    else if (!me.principal.permissions.length) state.noAccess = "nothing";
    // A labeller and nothing else (LP-11): no packs to list, no notice, no review queue; the
    // labelling screen comes with the routes it reads.
    else if (me.principal.permissions.every((p) => p === "label")) state.noAccess = "label";
  }
  if (me?.signed_in && !state.noAccess) await acknowledgeNotice();
  state.smart = session?.active ? session : null;
  // An EHR launch in a browser whose person may do nothing here (no organization, or roles that
  // grant nothing): the launch's view, and, holding memberships, choosing one of them.
  state.launchOnly = Boolean(state.smart && me?.mode === "sign-in" && (!me.signed_in || state.noAccess));
  if (state.launchOnly && state.noAccess !== "choose") state.noAccess = null;
  showLaunch();
  showOrganization();
  showAdministration();
  if (state.noAccess && !state.launchOnly) {
    $(".pack-picker").hidden = true;
    document.querySelectorAll(".nav a[data-view]").forEach((a) => (a.hidden = true)); // the docs stay
    window.addEventListener("hashchange", onHashChange);
    render();
    return;
  }
  state.packs = state.launchOnly ? state.smart.packs ?? [] : await api("/api/packs");
  const select = $("#pack-select");
  if (!state.packs.length) {
    $("#view").removeAttribute("aria-busy");
    $("#view").innerHTML = `<div class="panel empty">No Decision Packs found on the search path.</div>`;
    return;
  }
  // Grouped by where each pack comes from when more than one origin is served (pack.js).
  select.innerHTML = packOptions(state.packs);
  const known = (id) => state.packs.some((p) => p.id === id);
  const first = defaultPack(state.packs);
  const saved = storageGet("oh.pack", first);
  switchPack([packInAddress(), saved].find(known) ?? first); // a view that reads it waits (router.js)
  select.addEventListener("change", async () => {
    await switchPack(select.value);
    render();
  });
  // Back and Forward can cross a pack change. The address then names another pack than the one
  // shown, and the browser fires only popstate (hashchange needs everything but the hash to match).
  window.addEventListener("popstate", async () => {
    const wanted = packInAddress();
    if (wanted === state.packId || !known(wanted)) return;
    await switchPack(wanted);
    render({ navigated: true });
  });
  window.addEventListener("hashchange", onHashChange);
  render();
}

// The pack travels in the address ("?pack=mendari/referral-router#review"), so a shared or
// bookmarked link opens the pack it was copied from, and a record link opens over that pack's
// list. Without one, the pack last chosen here. Hash links keep the query, so no link builds it.
const packInAddress = () => new URLSearchParams(location.search).get("pack");

function switchPack(packId) {
  $("#pack-select").value = packId;
  storageSet("oh.pack", packId);
  history.replaceState(history.state, "", `?pack=${encodeURIComponent(packId).replaceAll("%2F", "/")}${location.hash}`);
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
  const { name, roles, org } = me.principal;
  const held = state.memberships.find((m) => m.org === org);
  let shown = `${name} · ${roles.join(", ")}`;
  if (state.accounts === "memberships" && me.principal.kind === "user") {
    // The organization, unless the switcher shows it (several): the bar stays on one line.
    const named = state.memberships.length > 1 ? null : held?.name ?? org;
    shown = org === null ? `${name} · no organization` : [name, named, roles.join(", ")].filter(Boolean).join(" · ");
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

boot().catch((error) => {
  $("#view").removeAttribute("aria-busy");
  $("#view").innerHTML = `<div class="panel">Could not reach the Mendari API: ${esc(error.message)}</div>`;
});

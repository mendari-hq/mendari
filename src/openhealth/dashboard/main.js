// Boot: theme, time zone, packs, then the first view. Imports every view so it registers.

import "./overview.js";
import "./review.js";
import "./audit.js";
import "./evals.js";
import "./feedback.js";
import "./card.js";
import "./try.js";
import "./patient.js";
import { onHashChange, render, route } from "./router.js";
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
  try {
    state.timeFormat = timeFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
    state.shortFormat = shortFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
    state.timezone = health?.timezone ?? DEFAULT_TIMEZONE;
  } catch { /* an unknown zone name: keep the default */ }
  // A SMART launch from the EHR leaves a session: the patient's view joins the sections.
  const [session, packs, me] = await Promise.all([
    api("/api/smart/session").catch(() => null), api("/api/packs"), api("/api/auth/me").catch(() => null),
  ]);
  showSignedIn(me);
  state.smart = session?.active ? session : null;
  $('.nav a[data-view="patient"]').hidden = !state.smart;
  state.packs = packs;
  const select = $("#pack-select");
  if (!state.packs.length) {
    $("#view").removeAttribute("aria-busy");
    $("#view").innerHTML = `<div class="panel empty">No Decision Packs found on the search path.</div>`;
    return;
  }
  select.innerHTML = state.packs.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} ${esc(p.version)}</option>`).join("");
  const known = (id) => state.packs.some((p) => p.id === id);
  const saved = storageGet("oh.pack", state.packs[0].id);
  await switchPack([packInAddress(), saved].find(known) ?? state.packs[0].id);
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

// The pack travels in the address ("?pack=openhealth/referral-router#review"), so a shared or
// bookmarked link opens the pack it was copied from, and a record link opens over that pack's
// list. Without one, the pack last chosen here. Hash links keep the query, so no link builds it.
const packInAddress = () => new URLSearchParams(location.search).get("pack");

async function switchPack(packId) {
  $("#pack-select").value = packId;
  storageSet("oh.pack", packId);
  history.replaceState(history.state, "", `?pack=${encodeURIComponent(packId).replaceAll("%2F", "/")}${location.hash}`);
  await loadPack(packId);
}

// With sign-in on, the bar names who is signed in (and their role) and offers signing out.
function showSignedIn(me) {
  state.me = me?.principal ?? null;
  const box = $("#signed-in");
  box.hidden = !me?.signed_in;
  if (!me?.signed_in) return;
  $("#signed-in-name").textContent = `${me.principal.name} · ${me.principal.roles.join(", ")}`;
  $("#sign-out").addEventListener("click", async () => {
    await api("/auth/logout", { method: "POST" }).catch(() => null);
    location.assign("/auth/login");
  });
}

boot().catch((error) => {
  $("#view").removeAttribute("aria-busy");
  $("#view").innerHTML = `<div class="panel">Could not reach the OpenHealth API: ${esc(error.message)}</div>`;
});

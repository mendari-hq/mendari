// This patient: opened by a SMART on FHIR launch from the EHR. The patient's banner, then the
// resources the current pack decides about (their messages, their referrals), each decided on
// request, with the decision shown in place (and, for those who may read the audit trail, one
// link away). Everything is read live from the EHR by the server, with the launch's token;
// nothing about the patient is kept in the browser.
//
// The window is pinned to its launch ("#patient/<launch id>"): the browser holds one launch at a
// time, so a later launch (another chart, maybe another patient) replaces it. A window whose
// launch was replaced says so, and never shows the other launch's patient: the wrong-patient
// safeguard. The launch is asked for again before every render and every decision, and the
// patient, items and decide requests carry the pin (`X-Mendari-Launch`): the server answers 409
// to a replaced one, so a launch that replaces this one between those requests shows nothing.

import { render, route, views } from "./router.js";
import { $, api, emptyState, esc, icon, may, outcomeBadge, priorityBadge, state, toast, toastError, whileBusy } from "./ui.js";

views.patient = async (main, params) => {
  let pinned = params[0] ?? null;
  if (pinned) state.launchPin = pinned;
  const session = state.smart || pinned ? await api("/api/smart/session") : { active: false };
  const now = session.active ? session : null;
  if (!now || (pinned && now.launch_id !== pinned)) {
    if (!now) state.smart = null;
    main.innerHTML = `<h1>This patient</h1>${emptyState(pinned ? (now ? REPLACED : ENDED) : NO_LAUNCH)}`;
    return;
  }
  state.smart = now;
  // Opened without a pin: the launch shown here is this window's from now on.
  if (!pinned && now.launch_id) state.launchPin = pinned = pin(now.launch_id);
  const pack = encodeURIComponent(state.packId);
  const options = pinnedTo(pinned);
  let patient, items;
  try {
    [patient, items] = await Promise.all([
      api("/api/smart/patient", options),
      api(`/api/smart/focus?pack_id=${pack}`, options),
    ]);
  } catch (error) {
    if (!error.launchEnded && !error.launchReplaced) throw error;
    if (error.launchEnded) state.smart = null;
    main.innerHTML = `<h1>This patient</h1>${emptyState(error.launchEnded ? ENDED : REPLACED)}`;
    return;
  }
  main.innerHTML = `
    <h1>This patient</h1>
    <p class="muted">From the EHR launch: what ${esc(packName())} decides about for this patient.</p>
    ${banner(patient)}
    ${items.length ? `<ul class="work-items">${items.map(workItem).join("")}</ul>` : emptyState({
      icon: "inbox", title: "Nothing to decide",
      text: `This patient has nothing ${esc(packName())} decides about. Choose another pack above.`,
    })}`;
  $("#smart-logout").addEventListener("click", logout);
  main.querySelectorAll("button[data-decide]").forEach((button) => button.addEventListener("click", () => decide(button, pinned)));
};

const NO_LAUNCH = {
  icon: "user", title: "No EHR launch",
  text: "Open Mendari from the EHR to see a patient's messages or referrals here.",
};
const ENDED = { icon: "clock", title: "This EHR launch ended", text: "Open Mendari again from the EHR." };
const REPLACED = {
  icon: "alert", title: "This window's launch was replaced",
  text: "A later launch from the EHR in this browser replaced the one this window opened, maybe for another patient. "
    + "Open Mendari again from the EHR to work on this window's patient.",
};

// The launch's requests name the window's launch, which the server checks (409 once replaced).
const pinnedTo = (pinned, options = {}) =>
  pinned ? { ...options, headers: { "content-type": "application/json", "x-mendari-launch": pinned } } : options;

// The window takes the launch it shows: its address and the section link name it from now on.
function pin(launchId) {
  const hash = `#patient/${encodeURIComponent(launchId)}`;
  history.replaceState(history.state, "", `${location.pathname}${location.search}${hash}`);
  $('.nav a[data-view="patient"]').setAttribute("href", hash);
  return launchId;
}

const packName = () => state.packs.find((p) => p.id === state.packId)?.name ?? state.packId;

function banner(patient) {
  const facts = [patient.gender, age(patient.birthDate)].filter(Boolean).map(esc);
  return `<section class="panel patient-banner" aria-label="Patient">
    <span class="patient-icon" aria-hidden="true">${icon("user")}</span>
    <div class="grow">
      <div class="patient-name">${esc(patient.name ?? "Unnamed patient")}</div>
      <div class="muted">${facts.join(" · ")}${state.smart.encounter ? ` · encounter <span class="mono">${esc(state.smart.encounter)}</span>` : ""}</div>
    </div>
    <button type="button" id="smart-logout">${icon("log-out")} End session</button>
  </section>`;
}

function workItem(item) {
  return `<li class="panel work-item" data-ref="${esc(item.ref)}">
    <div class="row">
      <b>${esc(item.title ?? item.ref)}</b>
      ${item.priority && item.priority !== "routine" ? priorityBadge(item.priority) : ""}
      <span class="grow"></span>
      ${item.date ? `<span class="muted">${esc(item.date.slice(0, 10))}</span>` : ""}
    </div>
    ${item.text ? `<p class="work-text">${esc(item.text)}</p>` : ""}
    <div class="row">
      <button type="button" class="primary" data-decide="${esc(item.ref)}">Decide</button>
      <div class="work-result" aria-live="polite"></div>
    </div>
  </li>`;
}

async function decide(button, pinned) {
  const result = button.parentElement.querySelector(".work-result");
  await whileBusy(button, "Deciding…", async () => {
    try {
      const now = await api("/api/smart/session"); // still this window's launch?
      if (!now.active || (pinned && now.launch_id !== pinned)) {
        render();
        return;
      }
      const { decision } = await api("/api/smart/decide", pinnedTo(pinned, {
        method: "POST",
        body: JSON.stringify({ pack_id: state.packId, focus: button.dataset.decide }),
      }));
      result.innerHTML = decided(decision);
    } catch (error) {
      if (error.launchReplaced) render(); // replaced since the check above: the view says so
      else toastError(error.message);
    }
  });
}

// The decision in place: the outcome, the recommendation and why. Those who may read the audit
// trail open the record from here; a launch alone reads its id, to quote to the organization's
// reviewers (who read the trail).
function decided(decision) {
  const reasons = (decision.reasons ?? []).filter(Boolean);
  const record = !state.launchOnly && may("read")
    ? `<a class="action-link" href="#audit/${encodeURIComponent(decision.id)}">Open the decision ${icon("arrow-right")}</a>`
    : "";
  return `<div class="decided-line">${outcomeBadge(decision.outcome)} → <b>${esc(decision.recommendation ?? "no recommendation")}</b>${record}</div>
    ${reasons.length ? `<ul class="reasons decided-reasons">${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    ${state.launchOnly ? `<p class="muted decided-id">Recorded in your organization's audit trail as <span class="mono">${esc(decision.id)}</span>.</p>` : ""}`;
}

async function logout() {
  await api("/api/smart/logout", { method: "POST" }).catch(() => null);
  state.smart = null;
  toast("The EHR session has ended.");
  if (state.launchOnly) return render(); // the patient view is all there is: it says so
  $('.nav a[data-view="patient"]').hidden = true;
  if (route().view === "patient") location.hash = "#overview";
  else render();
}

// Whole years, from the birth date (the viewer's own calendar is fine for a banner).
function age(birthDate) {
  if (!birthDate) return "";
  const born = new Date(`${birthDate.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(born.getTime())) return "";
  const now = new Date();
  const years = now.getFullYear() - born.getFullYear() - (now < new Date(now.getFullYear(), born.getMonth(), born.getDate()) ? 1 : 0);
  return `${years} years`;
}

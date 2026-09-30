// This patient: opened by a SMART on FHIR launch from the EHR. The patient's banner, then the
// resources the current pack decides about (their messages, their referrals), each decided on
// request, with the decision one link away. Everything is read live from the EHR by the server,
// with the launch's token; nothing about the patient is kept in the browser.

import { render, views } from "./router.js";
import { $, api, emptyState, esc, icon, outcomeBadge, priorityBadge, state, toast, toastError, whileBusy } from "./ui.js";

views.patient = async (main) => {
  if (!state.smart) {
    main.innerHTML = `<h1>This patient</h1>${emptyState({
      icon: "user", title: "No EHR launch",
      text: "Open OpenHealth Hub from the EHR to see a patient's messages or referrals here.",
    })}`;
    return;
  }
  const pack = encodeURIComponent(state.packId);
  const [patient, items] = await Promise.all([api("/api/smart/patient"), api(`/api/smart/focus?pack_id=${pack}`)]);
  main.innerHTML = `
    <h1>This patient</h1>
    <p class="muted">From the EHR launch: what ${esc(state.detail.manifest.name)} decides about for this patient.</p>
    ${banner(patient)}
    ${items.length ? `<ul class="work-items">${items.map(workItem).join("")}</ul>` : emptyState({
      icon: "inbox", title: "Nothing to decide",
      text: `This patient has nothing ${esc(state.detail.manifest.name)} decides about. Choose another pack above.`,
    })}`;
  $("#smart-logout").addEventListener("click", logout);
  main.querySelectorAll("button[data-decide]").forEach((button) => button.addEventListener("click", () => decide(button)));
};

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
      <span class="work-result" aria-live="polite"></span>
    </div>
  </li>`;
}

async function decide(button) {
  const result = button.parentElement.querySelector(".work-result");
  await whileBusy(button, "Deciding…", async () => {
    try {
      const { decision } = await api("/api/smart/decide", {
        method: "POST",
        body: JSON.stringify({ pack_id: state.packId, focus: button.dataset.decide }),
      });
      result.innerHTML = `${outcomeBadge(decision.outcome)} → <b>${esc(decision.recommendation ?? "no recommendation")}</b>
        <a class="action-link" href="#audit/${encodeURIComponent(decision.id)}">Open the decision ${icon("arrow-right")}</a>`;
    } catch (error) {
      toastError(error.message);
    }
  });
}

async function logout() {
  await api("/api/smart/logout", { method: "POST" }).catch(() => null);
  state.smart = null;
  $('.nav a[data-view="patient"]').hidden = true;
  toast("The EHR session has ended.");
  if (location.hash === "#patient") location.hash = "#overview";
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

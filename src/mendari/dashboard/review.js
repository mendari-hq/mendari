// Review queue: grouped by priority, stat first; resolving moves focus to the next card.

import { communityNotice, criteriaTable, inputView, isCommunity, originBadge, statusNotice } from "./pack.js";
import { refreshReviewCount, views } from "./router.js";
import { $, announce, api, emptyState, esc, focusHeading, icon, may, outcomeBadge, pct, PRIORITY_TEXT, priorityBadge, reducedMotion, signedIn, state, storageGet, storageSet, time, toast, toastError } from "./ui.js";

const PRIORITIES = ["stat", "asap", "urgent", "routine"];

views.review = async (main) => {
  const [items, stats] = await Promise.all([
    api(`/api/review?pack_id=${encodeURIComponent(state.packId)}&limit=100`),
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
  ]);
  const groups = PRIORITIES.map((p) => [p, items.filter((item) => item.priority === p)]).filter(([, g]) => g.length);
  main.innerHTML = `
    <h1>Review queue</h1>
    <p class="muted">Decisions waiting for a person, most urgent first.</p>
    ${communityNotice(state.detail)}
    ${statusNotice(state.detail)}
    <div class="row toolbar">
      <label for="reviewer">Reviewer</label>
      ${signedIn()
        ? `<input id="reviewer" value="${esc(state.me.actor)}" readonly title="Reviews are recorded for whoever is signed in, as the audit trail names them">`
        : `<input id="reviewer" value="${esc(storageGet("oh.reviewer", "sandbox-nurse"))}" maxlength="100" spellcheck="false" title="An identifier, without spaces: the audit trail keeps it">`}
      <span class="grow"></span>
      <span id="queue-counts" class="queue-counts" aria-live="polite">${queueCounts(items)}</span>
    </div>
    ${items.length < stats.pending_reviews ? `<p class="muted">Showing the first ${items.length} of ${stats.pending_reviews}, most urgent first.</p>` : ""}
    <div id="queue">
      ${groups.map(([priority, group]) => `<section class="review-group" data-priority="${priority}" aria-labelledby="group-${priority}">
        <h2 id="group-${priority}">${esc(PRIORITY_TEXT[priority])} <span class="count-plain">${group.length}</span></h2>
        ${group.map(reviewCard).join("")}
      </section>`).join("") || emptyQueue()}
    </div>`;
  $("#reviewer").addEventListener("change", (e) => storageSet("oh.reviewer", e.target.value.trim()));
  $("#queue").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-label]");
    if (button) resolveCard(button);
  });
};

const emptyQueue = () => emptyState({
  icon: "check", title: "Nothing waiting for review",
  text: "Decisions the policy sends to a person appear here, most urgent first.",
  action: { href: "#audit", label: "Open the audit trail" },
});

// A resolved card folds away (height and opacity, 200 ms) before the next one takes focus.
async function collapse(card) {
  if (!reducedMotion()) {
    const { height } = card.getBoundingClientRect();
    card.style.overflow = "hidden";
    const folded = { height: "0px", opacity: 0, paddingTop: "0px", paddingBottom: "0px", marginTop: "0px", borderWidth: "0px" };
    await card.animate([{ height: `${height}px`, opacity: 1 }, folded], { duration: 200, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" })
      .finished.catch(() => {});
  }
  card.remove();
}

function queueCounts(items) {
  if (!items.length) return "Nothing waiting";
  return PRIORITIES.map((p) => [p, items.filter((i) => i.priority === p).length]).filter(([, n]) => n)
    .map(([p, n]) => `<span class="badge p-${p}">${n} ${esc(PRIORITY_TEXT[p].toLowerCase())}</span>`).join(" ");
}

// Confirming the recommendation is one tap. Any other label is held for UNDO_SECONDS with an Undo
// button (focused), since the audit chain is append-only and a mis-tap on a phone would otherwise be
// a permanent override. Leaving the view or the page sends what is held, as if the time ran out.
const UNDO_SECONDS = 5;
const held = new Map(); // decision id -> send()

function resolveCard(button) {
  const card = button.closest(".review-card");
  const reviewer = $("#reviewer").value.trim();
  if (!reviewer) {
    $("#reviewer").focus();
    return toastError("Enter a reviewer name first");
  }
  const label = button.dataset.label;
  if (label === card.dataset.recommendation) return sendResolution(card, label, reviewer);
  holdResolution(card, button, label, reviewer);
}

function holdResolution(card, button, label, reviewer) {
  const labelButtons = card.querySelectorAll(".labels button");
  labelButtons.forEach((b) => (b.disabled = true));
  button.classList.add("held");
  const what = card.dataset.recommendation ? `Override to <b>${esc(label)}</b>` : `Label <b>${esc(label)}</b>`;
  const bar = document.createElement("div");
  bar.className = "undo-bar";
  bar.innerHTML = `<span>${what} in <span class="undo-seconds">${UNDO_SECONDS}</span> s</span>
    <button type="button" class="undo">Undo</button><span class="undo-time" aria-hidden="true"></span>`;
  card.querySelector(".labels").after(bar);
  let left = UNDO_SECONDS;
  const tick = setInterval(() => (bar.querySelector(".undo-seconds").textContent = String(--left)), 1000);
  const release = () => {
    clearInterval(tick);
    clearTimeout(timer);
    held.delete(card.dataset.id);
    bar.remove();
  };
  const send = (options) => {
    release();
    return sendResolution(card, label, reviewer, options);
  };
  const timer = setTimeout(send, UNDO_SECONDS * 1000);
  held.set(card.dataset.id, send);
  bar.querySelector(".undo").addEventListener("click", () => {
    release();
    button.classList.remove("held");
    labelButtons.forEach((b) => (b.disabled = false));
    button.focus();
    announce(`Cancelled: ${label} was not sent`);
  });
  bar.querySelector(".undo").focus();
  announce(`${card.dataset.recommendation ? "Override" : "Label"} ${label} sends in ${UNDO_SECONDS} seconds. Press Undo to cancel.`);
}

// Leaving the queue (another view, another pack) sends what is held; so does closing the page, with
// keepalive so the request outlives it.
views.review.leave = () => held.forEach((send) => send());
window.addEventListener("pagehide", () => held.forEach((send) => send({ keepalive: true })));

async function sendResolution(card, label, reviewer, { keepalive = false } = {}) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const hadFocus = card.contains(document.activeElement) || document.activeElement === document.body;
  try {
    const result = await api(`/api/review/${card.dataset.id}`, {
      method: "POST",
      body: JSON.stringify({ final_label: label, reviewer }),
      keepalive,
    });
    toast(result.human_override ? `Override recorded: ${label}` : `Confirmed: ${label}`);
    refreshReviewCount();
    if (!card.isConnected) return; // sent on the way out of the queue
    // Keyboard reviewers carry on where they were: the next card (or the one before) gets focus,
    // unless they have already moved on to another.
    const cards = [...document.querySelectorAll(".review-card")];
    const next = cards[cards.indexOf(card) + 1] ?? cards[cards.indexOf(card) - 1];
    const group = card.closest(".review-group");
    await collapse(card);
    if (!group.querySelector(".review-card")) group.remove();
    else group.querySelector(".count-plain").textContent = group.querySelectorAll(".review-card").length;
    const left = [...document.querySelectorAll(".review-card")].map((c) => ({ priority: c.dataset.priority }));
    $("#queue-counts").innerHTML = queueCounts(left);
    if (next) {
      if (hadFocus) focusHeading(next);
    } else {
      $("#queue").innerHTML = emptyQueue();
      if (hadFocus) focusHeading($("main h1"));
    }
  } catch (error) {
    toastError(error.message);
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
}

// The rule that made a decision urgent, in its own words ("rule possible-emergency: Possible
// emergency symptoms. Nurse triage now…" -> the sentence after the rule name).
const ruleReason = (record) => (record.reasons.find((r) => r.startsWith("rule ")) ?? "").replace(/^rule [^:]+:\s*/, "");

function reviewCard(item) {
  const record = item.record;
  const recommendation = item.recommendation;
  const labels = state.detail.labels;
  const title = recommendation ? `Recommended: ${recommendation}` : "No recommendation: the pack abstained";
  return `<article class="panel review-card${item.priority === "stat" ? " is-stat" : ""}" data-id="${esc(item.id)}" data-priority="${esc(item.priority)}"
      data-recommendation="${esc(recommendation ?? "")}"
      tabindex="-1" aria-labelledby="card-${esc(item.id)}">
    ${item.priority === "stat" ? `<div class="stat-banner">${icon("alert")}<span><b>Stat.</b> ${esc(ruleReason(record) || "Handle before anything else.")}</span></div>` : ""}
    <div class="row">
      <h3 class="card-title" id="card-${esc(item.id)}">${esc(title)}</h3>
      ${item.confidence !== null ? `<span class="muted">confidence ${pct(item.confidence)}</span>` : ""}
      <span class="grow"></span>
      ${isCommunity(state.detail) ? originBadge(state.detail) : ""}
      ${item.priority === "stat" ? "" : priorityBadge(item.priority)} ${outcomeBadge(item.outcome)}
      <span class="muted"><time datetime="${esc(item.created_at)}">${esc(time(item.created_at))}</time></span>
      <span class="mono muted">${esc(item.id.slice(0, 8))}</span>
    </div>
    ${inputView(record.input, { restricted: item.input_restricted })}
    <ul class="reasons">${record.reasons.filter((r) => item.priority !== "stat" || !r.startsWith("rule ")).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${criteriaTable(record.answers)}
    ${may("review") ? `<div class="labels">
      ${recommendation ? `<button type="button" class="primary" data-label="${esc(recommendation)}">${icon("check")} Confirm ${esc(recommendation)}</button>
        <span class="muted">or choose another label:</span>` : `<span class="muted">Choose the right label:</span>`}
      ${labels.filter((l) => l !== recommendation).map((l) => `<button type="button" data-label="${esc(l)}">${esc(l)}</button>`).join("")}
    </div>` : `<p class="muted">Resolving is for reviewers; your role can read the queue.</p>`}
  </article>`;
}

// SPDX-License-Identifier: Apache-2.0
// Review queue: grouped by priority, stat first; resolving moves focus to the next card.

import { actionsList, communityNotice, inputView, isCommunity, labelName, originBadge, statusNotice } from "./pack.js";
import { refreshReviewCount, views } from "./router.js";
import { $, announce, api, emptyState, esc, focusHeading, icon, may, outcomeBadge, pct, PRIORITY_TEXT, priorityBadge, reducedMotion, signedIn, state, storageGet, storageSet, time, toast, toastError } from "./ui.js";

const PRIORITIES = ["stat", "asap", "urgent", "routine"];

let keyHandler = null;

function stopKeys() {
  if (!keyHandler) return;
  document.removeEventListener("keydown", keyHandler);
  keyHandler = null;
}

const isTyping = (target) => Boolean(target?.closest?.("input, textarea, select, [contenteditable]"));

views.review = async (main) => {
  stopKeys();
  const [items, stats] = await Promise.all([
    api(`/api/review?pack_id=${encodeURIComponent(state.packId)}&limit=100`),
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
  ]);
  const known = new Set(PRIORITIES);
  const groups = PRIORITIES.map((p) => [p, items.filter((item) => item.priority === p)]).filter(([, g]) => g.length);
  const other = items.filter((item) => !known.has(item.priority));
  if (other.length) groups.push(["other", other]);
  let activePriority = "";
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
      <span id="queue-counts" class="queue-counts" aria-live="polite">${queueCounts(items, activePriority)}</span>
    </div>
    ${items.length < stats.pending_reviews ? `<p class="muted">Showing the first ${items.length} of ${stats.pending_reviews}, most urgent first.</p>` : ""}
    <div id="queue">
      ${groups.map(([priority, group]) => `<section class="review-group" data-priority="${esc(priority)}" aria-labelledby="group-${esc(priority)}">
        <h2 id="group-${esc(priority)}">${esc(PRIORITY_TEXT[priority] ?? "Other")} <span class="count-plain">${group.length}</span></h2>
        ${group.map(reviewCard).join("")}
      </section>`).join("") || emptyQueue()}
    </div>`;

  const applyFilter = (refocusPriority = null) => {
    const allGroups = [...main.querySelectorAll(".review-group")];
    if (activePriority && !allGroups.some((g) => g.dataset.priority === activePriority)) activePriority = "";
    allGroups.forEach((g) => (g.hidden = Boolean(activePriority && g.dataset.priority !== activePriority)));
    const left = [...main.querySelectorAll(".review-card")].map((c) => ({ priority: c.dataset.priority }));
    $("#queue-counts").innerHTML = queueCounts(left, activePriority);
    if (refocusPriority) {
      main.querySelector(`#queue-counts button[data-priority-filter="${CSS.escape(refocusPriority)}"]`)?.focus();
    }
  };

  $("#reviewer").addEventListener("change", (e) => storageSet("oh.reviewer", e.target.value.trim()));
  $("#queue-counts").addEventListener("click", (event) => {
    const pill = event.target.closest("button[data-priority-filter]");
    if (!pill) return;
    const p = pill.dataset.priorityFilter;
    activePriority = activePriority === p ? "" : p;
    applyFilter(p);
  });
  $("#queue").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-label]");
    if (button) resolveCard(button, applyFilter);
  });

  // Keyboard triage: J/K step through visible cards, C confirms the focused card's recommendation,
  // Z cancels a held override. Ignored when typing or inside a modal dialog.
  keyHandler = (event) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return; // taken (a G chord)
    if (document.querySelector("dialog[open], .drawer-open")) return;
    const key = event.key.toLowerCase();
    if (!["j", "k", "c", "z"].includes(key)) return;
    const visible = [...main.querySelectorAll(".review-group:not([hidden]) .review-card")];
    if (!visible.length) return;
    const current = document.activeElement?.closest?.(".review-card");
    const index = current ? visible.indexOf(current) : -1;
    if (key === "j" || key === "k") {
      event.preventDefault();
      const target = key === "j"
        ? visible[Math.min(index + 1, visible.length - 1)]
        : visible[Math.max(index <= 0 ? 0 : index - 1, 0)];
      if (target) focusHeading(target);
    } else if (key === "c") {
      const card = current ?? visible[0];
      const confirm = card?.querySelector(".labels button.primary:not(:disabled)");
      if (!confirm) return;
      event.preventDefault();
      confirm.click();
    } else if (key === "z") {
      const undo = current?.querySelector(".undo-bar .undo") ?? main.querySelector(".undo-bar .undo");
      if (!undo) return;
      event.preventDefault();
      undo.click();
    }
  };
  document.addEventListener("keydown", keyHandler);
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

function queueCounts(items, activePriority = "") {
  if (!items.length) return "Nothing waiting";
  const present = PRIORITIES.map((p) => [p, items.filter((i) => i.priority === p).length]).filter(([, n]) => n);
  if (present.length < 2 && !activePriority) {
    return present.map(([p, n]) => `<span class="badge p-${p}">${n} ${esc(PRIORITY_TEXT[p].toLowerCase())}</span>`).join(" ");
  }
  return present.map(([p, n]) =>
    `<button type="button" class="badge p-${p} priority-filter" data-priority-filter="${p}" aria-pressed="${activePriority === p}" title="Filter by ${esc(PRIORITY_TEXT[p].toLowerCase())}">${n} ${esc(PRIORITY_TEXT[p].toLowerCase())}</button>`).join(" ");
}

// Confirming the recommendation is one tap. Any other label is held for UNDO_SECONDS with an Undo
// button (focused), since the audit chain is append-only and a mis-tap on a phone would otherwise be
// a permanent override. Leaving the view or the page sends what is held, as if the time ran out.
const UNDO_SECONDS = 5;
const held = new Map(); // decision id -> send()

function resolveCard(button, onResolved) {
  const card = button.closest(".review-card");
  const reviewer = $("#reviewer").value.trim();
  if (!reviewer) {
    $("#reviewer").focus();
    return toastError("Enter a reviewer name first");
  }
  const label = button.dataset.label;
  // Confirming releases the held actions that are still checked, and a release is final: it
  // waits for Undo as an override does. Confirming with nothing to release is one tap, as before.
  if (label === card.dataset.recommendation && !releasing(card).length) return sendResolution(card, label, reviewer, { onResolved });
  holdResolution(card, button, label, reviewer, onResolved);
}

// The held actions a reviewer leaves checked (released on confirm) and unchecks (denied).
const releasing = (card) => [...card.querySelectorAll("input[data-release]:checked")].map((box) => box.dataset.release);
const refusing = (card) => [...card.querySelectorAll("input[data-release]:not(:checked)")].map((box) => box.dataset.release);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function holdResolution(card, button, label, reviewer, onResolved) {
  const labelButtons = card.querySelectorAll(".labels button");
  labelButtons.forEach((b) => (b.disabled = true));
  button.classList.add("held");
  const name = labelName(state.detail.primary, label);
  const confirming = label === card.dataset.recommendation;
  const what = confirming
    ? `Confirm <b>${esc(name)}</b> and release ${plural(releasing(card).length, "action")}`
    : card.dataset.recommendation ? `Override to <b>${esc(name)}</b>` : `Label <b>${esc(name)}</b>`;
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
    return sendResolution(card, label, reviewer, { ...options, onResolved });
  };
  const timer = setTimeout(send, UNDO_SECONDS * 1000);
  held.set(card.dataset.id, send);
  bar.querySelector(".undo").addEventListener("click", () => {
    release();
    button.classList.remove("held");
    labelButtons.forEach((b) => (b.disabled = false));
    button.focus();
    announce(`Cancelled: ${name} was not sent`);
  });
  bar.querySelector(".undo").focus();
  announce(`${confirming ? "Confirm" : card.dataset.recommendation ? "Override" : "Label"} ${name} sends in ${UNDO_SECONDS} seconds. Press Undo to cancel.`);
}

// Leaving the queue (another view, another pack, another organization: that one waits for them)
// sends what is held; so does closing the page, with keepalive so the request outlives it.
views.review.leave = () => {
  stopKeys();
  return Promise.all([...held.values()].map((send) => send()));
};
window.addEventListener("pagehide", () => held.forEach((send) => send({ keepalive: true })));

async function sendResolution(card, label, reviewer, { keepalive = false, onResolved } = {}) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const hadFocus = card.contains(document.activeElement) || document.activeElement === document.body;
  const primary = state.detail?.primary;
  const note = card.querySelector(".review-note textarea")?.value.trim() || null;
  const payload = note ? { final_label: label, reviewer, note } : { final_label: label, reviewer };
  // A held action the reviewer unchecked is denied while the label is confirmed (an override
  // denies them all, so it names none).
  const denied = label === card.dataset.recommendation ? refusing(card) : [];
  if (denied.length) payload.deny_actions = denied;
  try {
    const result = await api(`/api/review/${card.dataset.id}`, {
      method: "POST",
      body: JSON.stringify(payload),
      keepalive,
    });
    const name = primary ? labelName(primary, label) : label;
    const released = (result.actions ?? []).filter((a) => a.disposition === "released").length;
    const refused = (result.actions ?? []).length - released;
    const acted = result.actions?.length
      ? ` · ${[released ? `${plural(released, "action")} released` : "", refused ? `${plural(refused, "action")} denied` : ""].filter(Boolean).join(", ")}`
      : "";
    toast(`${result.human_override ? `Override recorded: ${name}` : `Confirmed: ${name}`}${acted}`);
    refreshReviewCount();
    if (!card.isConnected) return; // sent on the way out of the queue
    // Keyboard reviewers carry on where they were: the next card (or the one before) gets focus,
    // unless they have already moved on to another.
    const cards = [...document.querySelectorAll(".review-group:not([hidden]) .review-card")];
    const next = cards[cards.indexOf(card) + 1] ?? cards[cards.indexOf(card) - 1];
    const group = card.closest(".review-group");
    await collapse(card);
    if (group?.isConnected) {
      if (!group.querySelector(".review-card")) group.remove();
      else group.querySelector(".count-plain").textContent = group.querySelectorAll(".review-card").length;
    }
    if (onResolved) onResolved();
    else {
      const left = [...document.querySelectorAll(".review-card")].map((c) => ({ priority: c.dataset.priority }));
      const counts = $("#queue-counts");
      if (counts) counts.innerHTML = queueCounts(left);
    }
    const remaining = document.querySelectorAll(".review-card").length;
    if (next && next.isConnected) {
      if (hadFocus) focusHeading(next);
    } else if (!remaining) {
      const queue = $("#queue");
      if (queue) queue.innerHTML = emptyQueue();
      if (hadFocus) focusHeading($("main h1"));
    } else if (hadFocus) {
      const fallback = document.querySelector(".review-group:not([hidden]) .review-card");
      if (fallback) focusHeading(fallback);
    }
  } catch (error) {
    toastError(error.message);
    if (card.isConnected) card.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
}

// The rule that made a decision urgent, in its own words ("rule possible-emergency: Possible
// emergency symptoms. Nurse triage now…" -> the sentence after the rule name).
const ruleReason = (record) => (record.reasons.find((r) => r.startsWith("rule ")) ?? "").replace(/^rule [^:]+:\s*/, "");

function reviewCard(item) {
  const record = item.record;
  const recommendation = item.recommendation;
  const labels = state.detail.labels;
  const name = (label) => labelName(state.detail.primary, label);
  const title = recommendation ? `Recommended: ${name(recommendation)}` : "No recommendation: the agent abstained";
  const noteField = may("review") && state.phiMode !== "hash"
    ? `<details class="review-note"><summary>Add a note <span class="muted">(optional)</span></summary>
        <label class="sr-only" for="review-note-${esc(item.id)}">Review note</label>
        <textarea id="review-note-${esc(item.id)}" rows="2" maxlength="2000" placeholder="Context for the team (kept apart from the audit chain)"></textarea>
      </details>`
    : "";
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
      ${may("read") ? `<a class="action-link mono" href="#audit/${encodeURIComponent(item.id)}" title="Inspect decision ${esc(item.id)}">${esc(item.id.slice(0, 8))}</a>` : `<span class="mono muted">${esc(item.id.slice(0, 8))}</span>`}
    </div>
    ${inputView(record.input, { restricted: item.input_restricted })}
    <ul class="reasons">${record.reasons.filter((r) => item.priority !== "stat" || !r.startsWith("rule ")).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${item.actions?.length ? `<h4>Proposed actions</h4>
      ${item.actions.some((a) => a.disposition === "held") ? `<p class="muted">Confirming the recommendation releases the held actions; another label denies them.</p>` : ""}
      ${actionsList(item.actions, { choose: may("review") && Boolean(recommendation) })}` : ""}
    ${noteField}
    ${may("review") ? `<div class="labels">
      ${recommendation ? `<button type="button" class="primary" data-label="${esc(recommendation)}">${icon("check")} Confirm ${esc(name(recommendation))}</button>
        <span class="muted">or choose another label:</span>` : `<span class="muted">Choose the right label:</span>`}
      ${labels.filter((l) => l !== recommendation).map((l) => `<button type="button" data-label="${esc(l)}">${esc(name(l))}</button>`).join("")}
    </div>` : `<p class="muted">Resolving is for reviewers; your role can read the queue.</p>`}
  </article>`;
}

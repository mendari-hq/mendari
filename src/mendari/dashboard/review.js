// SPDX-License-Identifier: Apache-2.0
// Review queue: grouped by priority, stat first; resolving moves focus to the next card.
//
// A decision that carries what its agent wrote holds each text for the reviewer (ADR-0027): the
// card shows it, and takes a verdict on each, "Release as written" or "Withhold", before any
// label resolves the review. Its held actions are released by their ticks: one whose arguments
// can hold free text starts unticked, and withholding a text unticks them all. What the review
// did is listed under the queue afterwards, as the server answered it.

import { actionsList, inputView, labelName, NOT_RELEASED, statusNotice, textsList } from "./manifest.js";
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
    api(`/api/review?agent_id=${encodeURIComponent(state.agentId)}&limit=100`),
    api(`/api/stats?agent_id=${encodeURIComponent(state.agentId)}`),
  ]);
  const known = new Set(PRIORITIES);
  const groups = PRIORITIES.map((p) => [p, items.filter((item) => item.priority === p)]).filter(([, g]) => g.length);
  const other = items.filter((item) => !known.has(item.priority));
  if (other.length) groups.push(["other", other]);
  let activePriority = "";
  main.innerHTML = `
    <h1>Review queue</h1>
    <p class="muted">Decisions waiting for a person, most urgent first.</p>
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
    </div>
    <section id="resolved" aria-labelledby="resolved-title" hidden>
      <h2 id="resolved-title">What your reviews did</h2>
      <p class="muted">Each review you resolved here that held a text or an action: what it released, withheld or denied, as the audit trail recorded it. Newest first.</p>
      <ol class="resolved-list"></ol>
    </section>`;
  main.querySelectorAll(".review-card").forEach(syncCard);

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
  $("#queue").addEventListener("change", (event) => {
    const card = event.target.closest(".review-card");
    if (!card) return;
    if (event.target.matches("input[data-verdict]")) verdictChanged(card, event.target);
    else if (event.target.matches("input[data-release]")) syncCard(card);
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

// -- what the reviewer chose on a card (ADR-0027) -------------------------------------------------

const WITHHELD = "A text is withheld: no held action is released unless you tick it.";
const READY = "Every text has a verdict: you can resolve this review.";
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const waitsFor = (n) => `${n === 1 ? "1 text needs" : `${n} texts need`} a verdict before you can resolve this review.`;

// The verdict given to each held text of a card, by its output's name (null: none yet).
function verdicts(card) {
  const given = {};
  card.querySelectorAll("input[data-verdict]").forEach((radio) => {
    given[radio.dataset.verdict] ??= null;
    if (radio.checked) given[radio.dataset.verdict] = radio.value;
  });
  return given;
}
// The texts still without a verdict, in the order shown; the ticks of the held actions.
const waiting = (card) => Object.entries(verdicts(card)).filter(([, verdict]) => verdict === null).map(([name]) => name);
const ticks = (card) => [...card.querySelectorAll("input[data-release]")];

// A card in step with its verdicts and ticks. Until every held text has a verdict the labels
// are unavailable: `aria-disabled`, so the keyboard still reaches them, each described by the
// reason, which is on the card in words. While a text is withheld the card says that no held
// action is released without its tick. Each tick is described by what applies to it, and an
// unticked action says it will not be released.
function syncCard(card) {
  const given = Object.values(verdicts(card));
  const missing = given.filter((verdict) => verdict === null).length;
  const withheld = given.includes("withhold");
  card.dataset.waiting = String(missing);
  const gate = card.querySelector(".review-gate");
  if (gate) {
    gate.hidden = !missing;
    gate.textContent = missing ? waitsFor(missing) : "";
    card.querySelectorAll(".labels button").forEach((button) => {
      if (missing) {
        button.setAttribute("aria-disabled", "true");
        button.setAttribute("aria-describedby", gate.id);
      } else {
        button.removeAttribute("aria-disabled");
        button.removeAttribute("aria-describedby");
      }
    });
  }
  const note = card.querySelector(".withheld-note");
  if (note) {
    note.hidden = !withheld;
    note.textContent = withheld ? WITHHELD : "";
  }
  ticks(card).forEach((box) => {
    const row = box.closest("li");
    const off = row.querySelector(".release-off");
    off.hidden = box.checked;
    off.textContent = box.checked ? "" : NOT_RELEASED;
    const said = [row.querySelector(".release-why")?.id, withheld && note?.id, !box.checked && off.id].filter(Boolean);
    if (said.length) box.setAttribute("aria-describedby", said.join(" "));
    else box.removeAttribute("aria-describedby");
  });
}

// A text was given its verdict (`radio`: the one chosen), or another. A manifest does not say
// which action uses which text, so once a text is withheld no held action is released unless
// the review names it: each time a text is withheld every tick comes off, the reviewer's own
// too, and what is released after that, they ticked after that. Nothing is ticked for them
// again, whatever a verdict becomes. Said to a screen reader as it happens.
function verdictChanged(card, radio) {
  const wasWaiting = Number(card.dataset.waiting);
  const unticked = radio.value === "withhold" && ticks(card).length > 0;
  if (unticked) ticks(card).forEach((box) => (box.checked = false));
  syncCard(card);
  const missing = Number(card.dataset.waiting);
  const said = [missing !== wasWaiting && (missing ? waitsFor(missing) : READY), unticked && WITHHELD].filter(Boolean);
  if (said.length) announce(said.join(" "));
}

// What a label would send for a card, as the reviewer left it. Each held text's verdict. Then,
// when the label confirms the recommendation (any other denies every held action, so it names
// none): a tick releases, and names the action where confirming alone does not release it,
// because its arguments can hold free text or a text is withheld; a missing tick refuses by
// name what confirming alone would release, and leaves the rest unnamed, which is not released.
// So no action is ever both named and refused.
function choices(card, label) {
  const texts = verdicts(card);
  const byName = Object.values(texts).includes("withhold");
  const held = label === card.dataset.recommendation ? ticks(card) : [];
  const needsName = (box) => byName || "freeText" in box.dataset;
  const ids = (boxes) => boxes.map((box) => box.dataset.release);
  return {
    texts,
    released: held.filter((box) => box.checked).length,
    named: ids(held.filter((box) => box.checked && needsName(box))),
    denied: ids(held.filter((box) => !box.checked && !needsName(box))),
  };
}

// What the review does besides its label, in words: "release 2 texts and 1 action, withhold 1 text".
function doing({ texts, released }) {
  const given = Object.values(texts);
  const [release, withhold] = ["release", "withhold"].map((verdict) => given.filter((v) => v === verdict).length);
  const out = [release && plural(release, "text"), released && plural(released, "action")].filter(Boolean);
  return [out.length ? `release ${out.join(" and ")}` : "", withhold ? `withhold ${plural(withhold, "text")}` : ""].filter(Boolean).join(", ");
}

// Confirming the recommendation is one tap. Any other label is held for UNDO_SECONDS with an Undo
// button (focused), since the audit chain is append-only and a mis-tap on a phone would otherwise be
// a permanent override. Leaving the view or the page sends what is held, as if the time ran out.
const UNDO_SECONDS = 5;
const held = new Map(); // decision id -> send()

function resolveCard(button, onResolved) {
  const card = button.closest(".review-card");
  // Every held text has its verdict first: asked without, nothing is sent; the reason is said
  // and the first text that waits takes focus.
  const [first] = waiting(card);
  if (first) {
    card.querySelector(`input[data-verdict="${CSS.escape(first)}"]`).focus();
    return toastError(waitsFor(waiting(card).length));
  }
  const reviewer = $("#reviewer").value.trim();
  if (!reviewer) {
    $("#reviewer").focus();
    return toastError("Enter a reviewer name first");
  }
  const label = button.dataset.label;
  const chosen = choices(card, label);
  // A verdict is final, and so is a release: either waits for Undo as an override does.
  // Confirming with no text to judge and nothing to release is one tap, as before.
  const final = Object.keys(chosen.texts).length > 0 || chosen.released > 0;
  if (label === card.dataset.recommendation && !final) return sendResolution(card, label, reviewer, chosen, { onResolved });
  holdResolution(card, button, label, reviewer, chosen, onResolved);
}

// While a review waits for Undo or for the server's answer, its card's verdicts and ticks cannot
// change: what the bar says is what is sent. Undo, or a refusal, gives them back as they were.
function freeze(card, frozen) {
  card.querySelectorAll("input[data-verdict], input[data-release]").forEach((input) => (input.disabled = frozen));
}

function holdResolution(card, button, label, reviewer, chosen, onResolved) {
  const labelButtons = card.querySelectorAll(".labels button");
  labelButtons.forEach((b) => (b.disabled = true));
  freeze(card, true);
  button.classList.add("held");
  const name = labelName(state.detail.primary, label);
  const confirming = label === card.dataset.recommendation;
  const more = doing(chosen);
  const what = `${confirming ? "Confirm" : card.dataset.recommendation ? "Override to" : "Label"} <b>${esc(name)}</b>${more ? ` and ${esc(more)}` : ""}`;
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
    return sendResolution(card, label, reviewer, chosen, { ...options, onResolved });
  };
  const timer = setTimeout(send, UNDO_SECONDS * 1000);
  held.set(card.dataset.id, send);
  bar.querySelector(".undo").addEventListener("click", () => {
    release();
    button.classList.remove("held");
    labelButtons.forEach((b) => (b.disabled = false));
    freeze(card, false);
    button.focus();
    announce(`Cancelled: ${name} was not sent`);
  });
  bar.querySelector(".undo").focus();
  announce(`${confirming ? "Confirm" : card.dataset.recommendation ? "Override" : "Label"} ${name}${more ? ` and ${more}` : ""} sends in ${UNDO_SECONDS} seconds. Press Undo to cancel.`);
}

// Leaving the queue (another view, another agent, another organization: that one waits for them)
// sends what is held; so does closing the page, with keepalive so the request outlives it.
views.review.leave = () => {
  stopKeys();
  return Promise.all([...held.values()].map((send) => send()));
};
window.addEventListener("pagehide", () => held.forEach((send) => send({ keepalive: true })));

// What a resolved review did, as the server answered it, in words for a toast:
// ["2 texts released", "1 action released, 1 action denied"].
function outcomeWords(result) {
  const [texts, actions] = [result.outputs ?? [], result.actions ?? []];
  const count = (items, test) => items.filter(test).length;
  const said = (counts) => counts.filter(([n]) => n).map(([n, noun, verb]) => `${plural(n, noun)} ${verb}`).join(", ");
  return [
    said([[count(texts, (t) => t.verdict === "released"), "text", "released"], [count(texts, (t) => t.verdict !== "released"), "text", "withheld"]]),
    said([[count(actions, (a) => a.disposition === "released"), "action", "released"], [count(actions, (a) => a.disposition !== "released"), "action", "denied"]]),
  ].filter(Boolean);
}

// A decision's id as the queue shows it: a link to its record for whoever may read it.
const recordLink = (id) => (may("read")
  ? `<a class="action-link mono" href="#audit/${encodeURIComponent(id)}" title="Inspect decision ${esc(id)}">${esc(id.slice(0, 8))}</a>`
  : `<span class="mono muted">${esc(id.slice(0, 8))}</span>`);

// Under the queue, once a review that held a text or an action is resolved: each text with the
// verdict it got and each held action with what became of it and why, as the server answered.
// Never the texts themselves: the decision's record shows them to whoever may read them.
function listResolved(id, said, result) {
  const list = document.querySelector("#resolved .resolved-list");
  if (!list || !(result.outputs?.length || result.actions?.length)) return;
  const item = document.createElement("li");
  item.className = "panel resolved-item";
  item.dataset.id = id;
  item.innerHTML = `<div class="row"><b>${esc(said)}</b><span class="grow"></span>${recordLink(id)}</div>
    ${textsList((result.outputs ?? []).map((text) => ({ name: text.name, state: text.verdict })), { brief: true })}
    ${actionsList(result.actions, { brief: true })}`;
  list.prepend(item);
  $("#resolved").hidden = false;
}

async function sendResolution(card, label, reviewer, chosen, { keepalive = false, onResolved } = {}) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  freeze(card, true);
  const failed = card.querySelector(".review-error");
  if (failed) failed.hidden = true;
  const hadFocus = card.contains(document.activeElement) || document.activeElement === document.body;
  const primary = state.detail?.primary;
  const note = card.querySelector(".review-note textarea")?.value.trim() || null;
  const payload = note ? { final_label: label, reviewer, note } : { final_label: label, reviewer };
  if (Object.keys(chosen.texts).length) payload.texts = chosen.texts;
  if (chosen.named.length) payload.release_actions = chosen.named;
  if (chosen.denied.length) payload.deny_actions = chosen.denied;
  try {
    const result = await api(`/api/review/${card.dataset.id}`, {
      method: "POST",
      body: JSON.stringify(payload),
      keepalive,
    });
    const name = primary ? labelName(primary, label) : label;
    // What was done is what the server answered, never what was asked for.
    const said = result.human_override ? `Override recorded: ${name}` : `Confirmed: ${name}`;
    toast([said, ...outcomeWords(result)].join(" · "));
    refreshReviewCount();
    if (!card.isConnected) return; // sent on the way out of the queue
    listResolved(card.dataset.id, said, result);
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
    // Refused, or it never arrived: nothing was recorded, and nothing is shown as done. The
    // card says why, in place, and gives back every verdict and tick as the reviewer left it.
    const why = `Not recorded: ${error instanceof TypeError ? "the server could not be reached. Your choices are kept: try again." : error.message}`;
    if (!card.isConnected || !failed) return toastError(why);
    card.querySelectorAll("button").forEach((b) => {
      b.disabled = false;
      b.classList.remove("held");
    });
    freeze(card, false);
    failed.hidden = false; // shown, then filled: an alert is announced when its words change
    failed.textContent = why;
    if (hadFocus) card.focus({ preventScroll: true });
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
  const reviewing = may("review");
  // A held text waits for its verdict, and the labels wait with it (`syncCard`).
  const waits = (item.outputs ?? []).some((text) => text.state === "held");
  const noteField = reviewing && state.phiMode !== "hash"
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
      ${item.priority === "stat" ? "" : priorityBadge(item.priority)} ${outcomeBadge(item.outcome)}
      <span class="muted"><time datetime="${esc(item.created_at)}">${esc(time(item.created_at))}</time></span>
      ${recordLink(item.id)}
    </div>
    ${inputView(record.input, { restricted: item.input_restricted })}
    <ul class="reasons">${record.reasons.filter((r) => item.priority !== "stat" || !r.startsWith("rule ")).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${item.outputs?.length ? `<h4>What the agent wrote</h4>
      <p class="muted">${reviewing ? "Read each text and choose what becomes of it. " : ""}A released text may be sent or filed as written; a withheld one may not.</p>
      ${textsList(item.outputs, { choose: reviewing, id: item.id })}` : ""}
    ${item.actions?.length ? `<h4>Proposed actions</h4>
      ${item.actions.some((a) => a.disposition === "held") ? `<p class="muted">Confirming the recommendation releases the held actions that are ticked; another label denies them all.</p>
        <p class="callout warning withheld-note" id="withheld-${esc(item.id)}" hidden></p>` : ""}
      ${actionsList(item.actions, { choose: reviewing && Boolean(recommendation) })}` : ""}
    ${noteField}
    ${reviewing ? `<p class="field-error review-error" role="alert" hidden></p>
    ${waits ? `<p class="review-gate" id="gate-${esc(item.id)}" hidden></p>` : ""}
    <div class="labels">
      ${recommendation ? `<button type="button" class="primary" data-label="${esc(recommendation)}">${icon("check")} Confirm ${esc(name(recommendation))}</button>
        <span class="muted">or choose another label:</span>` : `<span class="muted">Choose the right label:</span>`}
      ${labels.filter((l) => l !== recommendation).map((l) => `<button type="button" data-label="${esc(l)}">${esc(name(l))}</button>`).join("")}
    </div>` : `<p class="muted">Resolving is for reviewers; your role can read the queue.</p>`}
  </article>`;
}

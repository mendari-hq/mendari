// SPDX-License-Identifier: Apache-2.0
// Feedback: the page, and the right / wrong controls with the optional right answer.

import { ratingCell } from "./audit.js";
import { criteriaTable, derivedPrimary, inputView } from "./pack.js";
import { render, views } from "./router.js";
import { TABLES } from "./table.js";
import { $, api, download, emptyState, esc, icon, outcomeBadge, packPath, priorityBadge, signedIn, stat, state, storageGet, storageSet, time, toastError, whileBusy } from "./ui.js";

views.feedback = async (main) => {
  const rating = storageGet("oh.feedback.rating", "down");
  const [items, stats] = await Promise.all([
    api(`/api/feedback?pack_id=${encodeURIComponent(state.packId)}${rating ? `&rating=${rating}` : ""}&limit=200`),
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
  ]);
  const f = stats.feedback;
  main.innerHTML = `
    <h1>Feedback</h1>
    <p class="muted">Rate decisions right or wrong. A wrong rating with the right answer becomes a test case.</p>
    <div class="stats-primary two">
      ${stat({ label: "Wrong on automated", value: String(f.down_on_auto), tone: f.down_on_auto ? "danger" : "", foot: "Automated, then rated wrong: confidently wrong" })}
      ${stat({ label: "Ready to become tests", value: String(f.down_with_expected), foot: "Rated wrong, with the right answer" })}
    </div>
    <div class="row toolbar">
      <label for="rating-filter">Show</label>
      <select id="rating-filter">
        <option value="down" ${rating === "down" ? "selected" : ""}>Wrong (${f.down})</option>
        <option value="up" ${rating === "up" ? "selected" : ""}>Right (${f.up})</option>
        <option value="" ${rating === "" ? "selected" : ""}>All (${f.up + f.down})</option>
      </select>
      <span class="grow"></span>
      <a class="button" id="feedback-export" href="${packPath()}/feedback/export?rating=down" download>${icon("download")} Download the wrong cases (JSONL)</a>
    </div>
    ${items.length ? items.map(feedbackCard).join("") : emptyState({
      icon: "message", title: "Nothing rated yet",
      text: "Rate a decision right or wrong in Try it, or in a record of the audit trail.",
      action: { href: "#try", label: "Try a decision" },
    })}`;
  // Memberships mode: fetched with the window's organization (claims mode: the browser saves it).
  $("#feedback-export").addEventListener("click", (event) => {
    if (state.accounts !== "memberships") return;
    event.preventDefault();
    download(event.currentTarget.getAttribute("href"));
  });
  $("#rating-filter").addEventListener("change", (e) => {
    storageSet("oh.feedback.rating", e.target.value);
    render();
  });
};

// A rating's note: kept apart from the audit chain, shown only to those who may read inputs.
function noteText(item) {
  if (item.note_status === "open") return `<p><b>Note:</b> ${esc(item.note)}</p>`;
  if (item.note_status === "sealed") return `<p class="muted">Note: only reviewers and admins can read it.</p>`;
  if (item.note_status === "erased") return `<p class="muted">Note: erased.</p>`;
  return "";
}

function feedbackCard(item) {
  const r = item.record;
  const wrong = item.rating === "down";
  return `<article class="panel feedback-card">
    <div class="row">
      <span class="badge ${wrong ? "s-degraded" : "s-ok"}">${icon(wrong ? "thumbs-down" : "thumbs-up")} ${wrong ? "Wrong" : "Right"}</span>
      ${outcomeBadge(r.outcome)} ${priorityBadge(r.priority)}
      <span>→ <b>${esc(r.recommendation ?? "no recommendation")}</b></span>
      <span class="grow"></span>
      <span class="muted"><time datetime="${esc(item.created_at)}">${esc(time(item.created_at))}</time>${item.reporter ? ` · ${esc(item.reporter)}` : ""}</span>
    </div>
    ${item.expected ? `<p class="right-answer">${icon("check")} <b>Right answer:</b> <code>${esc(item.expected.question)}</code> should be <b>${esc(item.expected.label)}</b>
      <span class="muted">(it said ${esc(r.answers[item.expected.question]?.label ?? "nothing")})</span></p>` : ""}
    ${noteText(item)}
    ${inputView(r.input, { restricted: item.input_restricted })}
    ${criteriaTable(r.answers, { evidence: false })}
    <a class="action-link" href="#audit/${encodeURIComponent(item.decision_id)}">Open the decision <span class="mono">${esc(item.decision_id.slice(0, 8))}</span> ${icon("arrow-right")}</a>
  </article>`;
}

// One tap records a rating, and it can be changed: the other button switches it (each person's
// latest rating counts; the audit trail keeps every one). "Wrong" then asks, optionally, what the
// answer should have been: with it, the case is a ready-made test (notes/ui-ux-review.md, decision 5).
// Signed in, ratings are recorded as the audit trail names the person; in open mode, the name given.
const reporter = () => (signedIn() ? state.me.actor : storageGet("oh.reviewer", "") || null);

// This person's current rating of a decision, from its hash-chained events.
export function myRating(events) {
  const mine = events.filter((e) => e.event === "feedback.recorded" && (e.payload.reporter ?? null) === reporter());
  const last = mine[mine.length - 1];
  return last ? { rating: last.payload.rating, expected: last.payload.expected ?? null } : null;
}

// The questions a thumbs-down can correct: the decision itself first, then (for a derived decision)
// each check. The choices are the labels the decision could have given, except the one it gave.
function correctable(record) {
  const derive = derivedPrimary();
  const questions = [record.primary, ...(derive ? derive.from : [])].filter((q) => record.answers[q]);
  return questions.map((q) => ({
    question: q,
    answer: record.answers[q].label,
    labels: Object.keys(record.answers[q].probabilities ?? {}).filter((l) => l !== record.answers[q].label),
  })).filter((q) => q.labels.length);
}

const answerChoices = (q) => q.labels.map((l) =>
  `<button type="button" data-expected-label="${esc(l)}" aria-pressed="false">${esc(l)}</button>`).join("");

export function feedbackControls(record, current = null) {
  const id = esc(record.id);
  const choices = correctable(record);
  const status = current ? ratedText(current) : "";
  return `<div class="fb" data-decision="${id}">
    <div class="fb-buttons" role="group" aria-label="Was this right?">
      <button type="button" class="fb-choice" data-rating="up" aria-pressed="${current?.rating === "up"}">${icon("thumbs-up")}<span>Right</span></button>
      <button type="button" class="fb-choice" data-rating="down" aria-pressed="${current?.rating === "down"}">${icon("thumbs-down")}<span>Wrong</span></button>
      <span class="fb-status muted" role="status">${esc(status)}</span>
    </div>
    <div class="fb-follow" hidden>
      <p><b>What should it have been?</b> <span class="muted">Optional. With the right answer, this case becomes a ready-made test.</span></p>
      ${choices.length > 1 ? `<label class="fb-field">Question <select class="fb-question">${choices.map((q) =>
        `<option value="${esc(q.question)}">${esc(q.question)}${q.question === record.primary ? " (the decision)" : ""} · answered ${esc(q.answer)}</option>`).join("")}</select></label>` : ""}
      ${choices.map((q, i) => `<div class="choices fb-labels" role="group" data-question="${esc(q.question)}"
        aria-label="Right answer for ${esc(q.question)}"${i ? " hidden" : ""}>${answerChoices(q)}</div>`).join("")}
      ${state.phiMode === "hash" ? "" : `<label class="fb-field" for="fb-note-${id}">Note <span class="muted">(optional)</span></label>
      <input id="fb-note-${id}" class="fb-note" maxlength="1000" placeholder="What was wrong?">`}
      <div class="row mt-3"><button type="button" class="primary fb-save">Save</button><button type="button" class="fb-skip">Skip</button></div>
    </div>
  </div>`;
}

function ratedText({ rating, expected }) {
  if (rating === "up") return "Recorded as right.";
  return expected ? `Recorded as wrong: ${expected.question} should be ${expected.label}.` : "Recorded as wrong.";
}

async function sendRating(box, body) {
  const before = box.querySelector('.fb-choice[aria-pressed="true"]')?.dataset.rating ?? null;
  const result = await api(`/api/audit/${box.dataset.decision}/feedback`, {
    method: "POST",
    body: JSON.stringify({ reporter: reporter(), ...body }),
  });
  updateAuditRow(box.dataset.decision, before, result.rating);
  box.querySelectorAll(".fb-choice").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.rating === result.rating)));
  box.querySelector(".fb-status").textContent = ratedText(result);
  return result;
}

// The audit table behind the drawer shows current ratings: this person's change moves one count.
// Only that cell is redrawn, so the row's button (where focus returns) stays in place.
function updateAuditRow(id, before, after) {
  const row = TABLES.get("audit")?.spec.rows.find((r) => r.id === id);
  if (!row || before === after) return;
  if (before) row[`feedback_${before}`] -= 1;
  row[`feedback_${after}`] += 1;
  const cell = document.querySelector(`#audit-table tr[data-id="${CSS.escape(id)}"] td.rated-cell`);
  if (cell) cell.innerHTML = ratingCell(row);
}

function closeFollowUp(box, { focus = true } = {}) {
  box.querySelector(".fb-follow").hidden = true;
  if (focus) box.querySelector('.fb-choice[data-rating="down"]').focus();
}

document.addEventListener("click", async (event) => {
  const box = event.target.closest(".fb");
  if (!box) return;
  const follow = box.querySelector(".fb-follow");
  const choice = event.target.closest("button.fb-choice");
  const label = event.target.closest("button[data-expected-label]");
  try {
    if (choice) {
      const pressed = choice.getAttribute("aria-pressed") === "true";
      if (choice.dataset.rating === "up") {
        closeFollowUp(box, { focus: false });
        if (!pressed) await sendRating(box, { rating: "up" });
      } else {
        if (!pressed) await sendRating(box, { rating: "down" });
        follow.hidden = false; // wrong: optionally say what it should have been
        (follow.querySelector("select, [data-expected-label], .fb-note") ?? follow.querySelector(".fb-save")).focus();
      }
    } else if (label) {
      const on = label.getAttribute("aria-pressed") !== "true";
      box.querySelectorAll("[data-expected-label]").forEach((b) => b.setAttribute("aria-pressed", String(b === label && on)));
    } else if (event.target.closest(".fb-skip")) {
      closeFollowUp(box);
    } else if (event.target.closest(".fb-save")) {
      const group = box.querySelector(".fb-labels:not([hidden])");
      const picked = group?.querySelector('[aria-pressed="true"]');
      // PHI mode hash keeps no free text, so there is no note field (the server would refuse one).
      const note = box.querySelector(".fb-note")?.value.trim() || null;
      const expected = picked ? { question: group.dataset.question, label: picked.dataset.expectedLabel } : undefined;
      if (expected || note) await whileBusy(event.target.closest(".fb-save"), "Saving…", () => sendRating(box, { rating: "down", note, expected }));
      closeFollowUp(box);
    }
  } catch (error) {
    toastError(error.message);
  }
});

// Another question in the follow-up shows its own labels; one answer is chosen at a time.
document.addEventListener("change", (event) => {
  const select = event.target.closest(".fb-question");
  if (!select) return;
  select.closest(".fb").querySelectorAll(".fb-labels").forEach((group) => {
    group.hidden = group.dataset.question !== select.value;
    group.querySelectorAll("[aria-pressed]").forEach((b) => b.setAttribute("aria-pressed", "false"));
  });
});

// SPDX-License-Identifier: Apache-2.0
// The labelling screen (`#labelling`, "Labelling" in the section list; LP-11m, ADR-0011): a person
// holding `label` (the `labeller` role, memberships mode with the evaluation-set keys) labels the
// cases of their organization's hidden evaluation sets, blind. It shows their own counts set by
// set (`GET /api/labelling`), then one case at a time (`GET /api/labelling/next`: the case's
// input and the set's questions with their labels, nothing else), and sends their answer
// (`POST /api/labelling/{assignment}`) before showing the next. It never shows anything the
// server did not send in those answers: no one else's label, no model's output, no intended
// label, no attribute of the case, and nothing chosen for them. A case is what its author wrote:
// every value escaped, shown as text, never run as markup (`recordHtml`, record.js). Nothing is
// kept in the browser's storage. A `422` names the question it refuses, shown beside that question; a
// `404` (the case was withdrawn or is no longer open) moves on to the next.

import { recordHtml } from "./record.js";
import { views } from "./router.js";
import { announce, api, emptyState, esc, focusHeading, slotText, state, toast, toastError, whileBusy } from "./ui.js";

const COUNTS = "/api/labelling";
const NEXT = "/api/labelling/next";
// More labels than this: a list box rather than a radio group, so the form stays short.
const MOST_RADIOS = 8;

views.labelling = async (main) => {
  main.innerHTML = `<h1>Labelling</h1>
    <p class="muted">Read each case and answer its questions. You label blind: you never see anyone
      else's label or what a model made of the case, and no one is told how yours compares.</p>
    <section class="panel" id="label-sets" aria-labelledby="label-sets-title"></section>
    <section class="panel label-case" id="label-case"></section>`;
  await refresh(main);
};
views.labelling.packless = true; // reads no pack: a labeller may hold none
// The person holds `label` and this deployment serves the labelling routes (main.js asks).
views.labelling.available = () => Boolean(state.labelling) && !state.launchOnly;

// The counts and the next case, read again (after each label, or a case no longer open).
async function refresh(main) {
  const [counts, shown] = await Promise.all([api(COUNTS), api(NEXT)]); // 204: null
  showSets(main, counts);
  showCase(main, shown, counts.remaining);
}

function showSets(main, counts) {
  const box = main.querySelector("#label-sets");
  const heading = `<h2 id="label-sets-title">Your sets</h2>`;
  if (!counts.sets.length) {
    box.innerHTML = `${heading}<p class="muted">No set has cases for you yet.</p>`;
    return;
  }
  const summary = counts.remaining ? `${waiting(counts.remaining)} in all.` : "All your cases are labelled.";
  // Sets by id only: a set's name is sealed, and a labeller needs none to label.
  const rows = counts.sets.map((s) => `<tr><td><code class="mono">${esc(s.set)}</code></td>
    <td class="num">${esc(s.open)}</td><td class="num">${esc(s.labelled)}</td></tr>`).join("");
  box.innerHTML = `${heading}<p class="muted">${esc(summary)}</p>
    <div class="table-scroll" data-table="label-sets"><table class="data">
      <caption>Your cases in each set</caption>
      <thead><tr><th scope="col">Set</th><th scope="col" class="num">Waiting</th><th scope="col" class="num">Labelled</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

const waiting = (n) => `${n} ${n === 1 ? "case waits" : "cases wait"} for you`;

// One case: its input, then a form with one group per question. Nothing waiting (a 204): said so.
function showCase(main, shown, remaining) {
  const box = main.querySelector("#label-case");
  if (!shown) {
    box.innerHTML = `${emptyState({
      icon: "check",
      title: "Nothing waits for you",
      text: "Every case assigned to you is labelled. New cases show here once an administrator assigns them.",
    })}`;
    const button = Object.assign(document.createElement("button"), { type: "button", id: "label-again", className: "button", textContent: "Check again" });
    box.querySelector(".empty-state").append(button);
    button.addEventListener("click", (event) =>
      whileBusy(event.currentTarget, "Checking…", () => again(main)));
    return;
  }
  const questions = Object.entries(shown.questions);
  box.innerHTML = `<h2>Case to label</h2>
    <p class="muted">${esc(waiting(remaining))}, this one included.</p>
    <div class="case-input">${recordHtml(shown.input)}</div>
    <form class="label-form" id="label-form" novalidate>
      ${questions.map(([question, labels], i) => questionHtml(question, labels, i)).join("")}
      <p class="field-error" id="label-form-error" role="alert" hidden></p>
      <div class="row"><button type="submit" class="primary">Save label</button></div>
    </form>`;
  const form = box.querySelector("#label-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await whileBusy(form.querySelector("button[type=submit]"), "Saving…", () => send(main, form, shown, questions));
  });
}

function questionHtml(question, labels, i) {
  const name = esc(slotText(question));
  const error = `<p class="field-error" id="q-${i}-error" hidden></p>`;
  if (labels.length > MOST_RADIOS) {
    return `<div class="label-question" data-question="${esc(question)}">
      <label for="q-${i}" class="label-question-name">${name}</label>
      <select id="q-${i}" name="q-${i}" aria-describedby="q-${i}-error">
        <option value="">Choose a label</option>
        ${labels.map((label) => `<option value="${esc(label)}">${esc(label)}</option>`).join("")}
      </select>${error}
    </div>`;
  }
  return `<fieldset class="label-question" data-question="${esc(question)}" aria-describedby="q-${i}-error">
    <legend class="label-question-name">${name}</legend>
    <div class="label-options">${labels.map((label) => `<label class="label-option">
      <input type="radio" name="q-${i}" value="${esc(label)}"> <span>${esc(label)}</span></label>`).join("")}
    </div>${error}
  </fieldset>`;
}

// What the form holds: each question with the label chosen, or null where none is.
function chosen(form, questions) {
  const data = new FormData(form);
  return questions.map(([question], i) => [question, data.get(`q-${i}`) || null]);
}

async function send(main, form, shown, questions) {
  clearErrors(form);
  const answers = chosen(form, questions);
  const missing = answers.filter(([, label]) => label === null).map(([question]) => question);
  if (missing.length) {
    showErrors(form, questions, new Map(missing.map((q) => [q, "Choose a label for this question."])));
    return;
  }
  try {
    const { remaining } = await api(`${COUNTS}/${encodeURIComponent(shown.assignment)}`, {
      method: "POST",
      body: JSON.stringify({ labels: Object.fromEntries(answers) }),
    });
    toast(remaining ? `Label saved. ${waiting(remaining)}.` : "Label saved. Nothing else waits for you.");
    await again(main);
  } catch (error) {
    if (error.status === 422) {
      const named = namedQuestions(error.message, questions);
      if (named.size) showErrors(form, questions, named);
      else showFormError(form, error.message);
    } else if (error.status === 404) {
      // Withdrawn, or no longer open (its set was frozen): nothing to save; the next one.
      toast("That case is no longer yours to label. Here is your next one.");
      await again(main);
    } else {
      toastError(error.message);
    }
  }
}

// The counts and the next case again, the reader taken to it (its heading, or the empty state's).
async function again(main) {
  await refresh(main);
  const box = main.querySelector("#label-case");
  focusHeading(box.querySelector("h2"));
}

// The questions a 422 names (the server names questions, never a value: core/evalsets.py
// `_label_problem`), each with what to do; empty when it names none of them.
function namedQuestions(detail, questions) {
  const words = new Set(detail.split(/[\s,:]+/).map((w) => w.replace(/^labels\./, "")));
  const unanswered = detail.includes("unanswered");
  return new Map(questions.filter(([question]) => words.has(question)).map(([question]) => [
    question, unanswered ? "Choose a label for this question." : "Choose one of this question's labels.",
  ]));
}

function showErrors(form, questions, errors) {
  let first = null;
  questions.forEach(([question], i) => {
    if (!errors.has(question)) return;
    const message = form.querySelector(`#q-${i}-error`);
    message.textContent = errors.get(question);
    message.hidden = false;
    form.querySelectorAll(`[name="q-${i}"]`).forEach((input) => input.setAttribute("aria-invalid", "true"));
    first ??= form.querySelector(`[name="q-${i}"]`);
  });
  first?.focus();
  announce([...errors.keys()].map((q) => `${slotText(q)}: ${errors.get(q)}`).join(" "), { error: true });
}

function showFormError(form, message) {
  const box = form.querySelector("#label-form-error");
  box.textContent = message;
  box.hidden = false;
}

function clearErrors(form) {
  form.querySelectorAll(".field-error").forEach((p) => {
    p.hidden = true;
    p.textContent = "";
  });
  form.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
}

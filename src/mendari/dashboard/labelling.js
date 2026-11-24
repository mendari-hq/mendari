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
//
// The same screen grades what an agent wrote (`#written`, written.js; ADR-0025): `labellingScreen`
// takes the routes and the words. There the answer also holds the text to grade (`written`) and
// the rubric's own words for each criterion and level (`guidance`), which are shown as sent:
// the text above what the agent was given, each criterion's instructions under its name and
// each level's anchor beside its choice.

import { recordHtml } from "./record.js";
import { views } from "./router.js";
import { announce, api, emptyState, esc, focusHeading, slotText, state, toast, toastError, whileBusy } from "./ui.js";

// More labels than this: a list box rather than a radio group, so the form stays short.
const MOST_RADIOS = 8;

// Labelling hidden evaluation sets: the routes, and every word the screen says.
const HIDDEN = {
  base: "/api/labelling",
  title: "Labelling",
  lede: `Read each case and answer its questions. You label blind: you never see anyone
      else's label or what a model made of the case, and no one is told how yours compares.`,
  setsTitle: "Your sets",
  noSets: "No set has cases for you yet.",
  allDone: "All your cases are labelled.",
  caption: "Your cases in each set",
  set: "Set",
  done: "Labelled",
  waiting: (n) => `${n} ${n === 1 ? "case waits" : "cases wait"} for you`,
  nothing: "Every case assigned to you is labelled. New cases show here once an administrator assigns them.",
  heading: "Case to label",
  save: "Save label",
  saved: "Label saved.",
  choose: "Choose a label",
  missing: "Choose a label for this question.",
  invalid: "Choose one of this question's labels.",
  gone: "That case is no longer yours to label. Here is your next one.",
  prefix: /^labels\./,
};

// The screen in which `kind` (the routes and the words, as `HIDDEN`) is worked through.
export async function labellingScreen(main, kind = HIDDEN) {
  main.innerHTML = `<h1>${esc(kind.title)}</h1>
    <p class="muted">${esc(kind.lede)}</p>
    <section class="panel" id="label-sets" aria-labelledby="label-sets-title"></section>
    <section class="panel label-case" id="label-case"></section>`;
  await refresh(main, kind);
}

views.labelling = (main) => labellingScreen(main, HIDDEN);
views.labelling.packless = true; // reads no pack: a labeller may hold none
// The person holds `label` and this deployment serves the labelling routes (main.js asks).
views.labelling.available = () => Boolean(state.labelling) && !state.launchOnly;

// The counts and the next case, read again (after each label, or a case no longer open).
async function refresh(main, kind) {
  const [counts, shown] = await Promise.all([api(kind.base), api(`${kind.base}/next`)]); // 204: null
  showSets(main, counts, kind);
  showCase(main, shown, counts.remaining, kind);
}

function showSets(main, counts, kind) {
  const box = main.querySelector("#label-sets");
  if (!box) return;
  const heading = `<h2 id="label-sets-title">${esc(kind.setsTitle)}</h2>`;
  if (!counts.sets.length) {
    box.innerHTML = `${heading}<p class="muted">${esc(kind.noSets)}</p>`;
    return;
  }
  const summary = counts.remaining ? `${kind.waiting(counts.remaining)} in all.` : kind.allDone;
  // Sets by id only: a set's name is sealed, and a labeller needs none to label.
  const rows = counts.sets.map((s) => `<tr><td><code class="mono">${esc(s.set)}</code></td>
    <td class="num">${esc(s.open)}</td><td class="num">${esc(s.labelled)}</td></tr>`).join("");
  box.innerHTML = `${heading}<p class="muted">${esc(summary)}</p>
    <div class="table-scroll" data-table="label-sets"><table class="data">
      <caption>${esc(kind.caption)}</caption>
      <thead><tr><th scope="col">${esc(kind.set)}</th><th scope="col" class="num">Waiting</th><th scope="col" class="num">${esc(kind.done)}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

// One case: its input, then a form with one group per question. Nothing waiting (a 204): said so.
// A written output (`shown.written`) is shown first, as text, above what the agent was given.
function showCase(main, shown, remaining, kind) {
  const box = main.querySelector("#label-case");
  if (!box) return;
  if (!shown) {
    box.innerHTML = `${emptyState({ icon: "check", title: "Nothing waits for you", text: kind.nothing })}`;
    const button = Object.assign(document.createElement("button"), { type: "button", id: "label-again", className: "button", textContent: "Check again" });
    box.querySelector(".empty-state").append(button);
    button.addEventListener("click", (event) =>
      whileBusy(event.currentTarget, "Checking…", () => again(main, kind)));
    return;
  }
  const questions = Object.entries(shown.questions);
  const written = shown.written ? `<h3>${esc(slotText(shown.written.name))}</h3>
    <p class="muted">${esc(shown.written.description)}</p>
    <div class="message" id="written-text">${esc(shown.written.text)}</div>
    <h3>What the agent was given</h3>` : "";
  box.innerHTML = `<h2>${esc(kind.heading)}</h2>
    <p class="muted">${esc(kind.waiting(remaining))}, this one included.</p>
    ${written}
    <div class="case-input">${recordHtml(shown.input)}</div>
    <form class="label-form" id="label-form" novalidate>
      ${questions.map(([question, labels], i) => questionHtml(question, labels, i, kind, shown.guidance?.[question])).join("")}
      <p class="field-error" id="label-form-error" role="alert" hidden></p>
      <div class="row"><button type="submit" class="primary">${esc(kind.save)}</button></div>
    </form>`;
  const form = box.querySelector("#label-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await whileBusy(form.querySelector("button[type=submit]"), "Saving…", () => send(main, form, shown, questions, kind));
  });
}

// One question. With a rubric's `guidance` (a criterion's own words): its name as a person reads
// it, its instructions under the name, and each level's anchor beside its choice.
function questionHtml(question, labels, i, kind, guidance) {
  const name = esc(guidance?.display || slotText(question));
  const error = `<p class="field-error" id="q-${i}-error" hidden></p>`;
  const asked = guidance ? `<p class="muted" id="q-${i}-asked">${esc(guidance.instructions)}</p>` : "";
  const described = guidance ? `q-${i}-asked q-${i}-error` : `q-${i}-error`;
  const anchor = (label) => (guidance?.levels?.[label] ? ` <span class="muted">${esc(guidance.levels[label])}</span>` : "");
  if (labels.length > MOST_RADIOS) {
    return `<div class="label-question" data-question="${esc(question)}">
      <label for="q-${i}" class="label-question-name">${name}</label>${asked}
      <select id="q-${i}" name="q-${i}" aria-describedby="${described}">
        <option value="">${esc(kind.choose)}</option>
        ${labels.map((label) => `<option value="${esc(label)}">${esc(label)}</option>`).join("")}
      </select>${error}
    </div>`;
  }
  return `<fieldset class="label-question" data-question="${esc(question)}" aria-describedby="${described}">
    <legend class="label-question-name">${name}</legend>${asked}
    <div class="label-options">${labels.map((label) => `<label class="label-option">
      <input type="radio" name="q-${i}" value="${esc(label)}"> <span>${esc(label)}</span>${anchor(label)}</label>`).join("")}
    </div>${error}
  </fieldset>`;
}

// What the form holds: each question with the label chosen, or null where none is.
function chosen(form, questions) {
  const data = new FormData(form);
  return questions.map(([question], i) => [question, data.get(`q-${i}`) || null]);
}

async function send(main, form, shown, questions, kind) {
  clearErrors(form);
  const answers = chosen(form, questions);
  const missing = answers.filter(([, label]) => label === null).map(([question]) => question);
  if (missing.length) {
    showErrors(form, questions, new Map(missing.map((q) => [q, kind.missing])));
    return;
  }
  try {
    const { remaining } = await api(`${kind.base}/${encodeURIComponent(shown.assignment)}`, {
      method: "POST",
      body: JSON.stringify({ labels: Object.fromEntries(answers) }),
    });
    toast(remaining ? `${kind.saved} ${kind.waiting(remaining)}.` : `${kind.saved} Nothing else waits for you.`);
    await again(main, kind);
  } catch (error) {
    if (error.status === 422) {
      const named = namedQuestions(error.message, questions, kind);
      if (named.size) showErrors(form, questions, named);
      else showFormError(form, error.message);
    } else if (error.status === 404) {
      // Withdrawn, or no longer open (its set was frozen): nothing to save; the next one.
      toast(kind.gone);
      await again(main, kind);
    } else {
      toastError(error.message);
    }
  }
}

// The counts and the next case again, the reader taken to it (its heading, or the empty state's).
async function again(main, kind) {
  await refresh(main, kind);
  const box = main.querySelector("#label-case");
  if (box?.isConnected) focusHeading(box.querySelector("h2"));
}

// The questions a 422 names (the server names questions, never a value: core/evalsets.py
// `_label_problem`, core/written.py `_grades_problem`), each with what to do; empty when it
// names none of them.
function namedQuestions(detail, questions, kind) {
  const words = new Set(detail.split(/[\s,:]+/).map((w) => w.replace(kind.prefix, "")));
  const unanswered = detail.includes("unanswered") || detail.includes("ungraded");
  return new Map(questions.filter(([question]) => words.has(question)).map(([question]) => [
    question, unanswered ? kind.missing : kind.invalid,
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

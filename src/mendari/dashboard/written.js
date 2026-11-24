// SPDX-License-Identifier: Apache-2.0
// Written output (ADR-0025). Two things, both over `/api/written`:
//
// - The grader's screen (`#written`, "Written output" in the section list): a person holding
//   `label` grades, blind, what an agent wrote, against the agent's rubric. It is the labelling
//   screen (labelling.js) with a grader's words: the text to grade, what the agent was given,
//   and each criterion with its instructions and each level's anchor. It never shows a judge's
//   grade or anyone else's: the server sends neither.
// - The rounds panel (`roundsPanel`, the Evaluations view's "Written output" tab): each round
//   of the agent with its state and counts and, once closed, its report: per criterion,
//   clinicians' pass share, the judge's agreement with clinicians (over the levels, and on pass
//   or fail) beside clinician with clinician, the judge's pass share beside the clinicians' on
//   the same outputs, what it could not grade, the state (validated, not validated,
//   insufficient evidence) with its reasons, and the judge's share for ungraded outputs only
//   where validated; otherwise "withheld". A round's `note` is shown as sent: one left closing
//   (with how to finish it), or a stored report that is not the audit trail's, which the
//   server does not send. Read-only: rounds are made, assigned and closed on the command line.

import { labellingScreen } from "./labelling.js";
import { views } from "./router.js";
import { api, emptyState, esc, figureHtml, slotText, state, time } from "./ui.js";

const GRADING = {
  base: "/api/written/labelling",
  title: "Written output",
  lede: `Read each text and grade it against each criterion. You grade blind: you never see a
      model judge's grade or anyone else's, and no one is told how yours compares.`,
  setsTitle: "Your rounds",
  noSets: "No round has texts for you yet.",
  allDone: "All your texts are graded.",
  caption: "Your texts in each round",
  set: "Round",
  done: "Graded",
  waiting: (n) => `${n} ${n === 1 ? "text waits" : "texts wait"} for you`,
  nothing: "Every text assigned to you is graded. New texts show here once a round is assigned to you.",
  heading: "Text to grade",
  save: "Save grades",
  saved: "Grades saved.",
  choose: "Choose a level",
  missing: "Choose a level for this criterion.",
  invalid: "Choose one of this criterion's levels.",
  gone: "That text is no longer yours to grade. Here is your next one.",
  prefix: /^grades\./,
};

views.written = (main) => labellingScreen(main, GRADING);
views.written.packless = true; // reads no agent: a grader may hold none
// The person holds `label`, this deployment serves a grader's routes and a round holds work of
// theirs (main.js asks).
views.written.available = () => Boolean(state.written) && !state.launchOnly;

const STATE_WORDS = { open: "Open: being graded", closing: "Closing: not finished", closed: "Closed" };

// The rounds of one agent, into `box`; each closed one's report under it.
export async function roundsPanel(box, packId) {
  let rounds;
  try {
    rounds = await api(`/api/written/rounds?pack_id=${encodeURIComponent(packId)}`);
  } catch (error) {
    // Not served here (it needs memberships mode and PHI mode encrypt), or not this reader's.
    box.innerHTML = emptyState({
      icon: "history", title: "No rounds here",
      text: "This deployment serves no rounds of written output to grade. `mendari written eval` reports a run from a grades file on the command line.",
    });
    return;
  }
  if (box.dataset.tab !== "written") return; // the reader moved on
  if (!rounds.length) {
    box.innerHTML = emptyState({
      icon: "history", title: "No round yet",
      text: "A round is made from a run on the command line (`mendari written round create`), graded here by your organization's graders, and reported once it is closed.",
    });
    return;
  }
  box.innerHTML = rounds.map(roundHtml).join("");
}

function roundHtml(round) {
  const head = `<h2>Rubric ${esc(slotText(round.rubric))}</h2>
    <p class="muted">${esc(STATE_WORDS[round.state] ?? round.state)} · made ${esc(time(round.created_at))} ·
      ${esc(round.items)} texts kept to grade · ${esc(round.graded)} grades given · ${esc(round.open)} still waiting</p>
    <p class="muted">Round <code class="mono">${esc(round.id)}</code></p>
    ${round.note ? `<p data-round-note><strong>${esc(round.note)}</strong></p>` : ""}`;
  if (!round.report) {
    // Open: nothing is compared yet. Closing, or a report that is not the audit trail's: the
    // note above says which, and no figure is shown.
    const why = round.state === "open" ? "No report yet: nothing is compared, for anyone, until the round is closed." : "No report is shown.";
    return `<section class="panel" data-round="${esc(round.id)}">${head}<p>${esc(why)}</p></section>`;
  }
  return `<section class="panel" data-round="${esc(round.id)}">${head}${reportHtml(round.report)}</section>`;
}

const kappaText = (k) => {
  if (!k || k.estimate === null || k.estimate === undefined) return "not estimable";
  if (k.interval?.status !== "ok") return `${k.estimate.toFixed(2)} (interval not estimable)`;
  return `${k.estimate.toFixed(2)} (${k.interval.low.toFixed(2)} to ${k.interval.high.toFixed(2)})`;
};

function reportHtml(report) {
  const judge = report.judge;
  const notes = [
    judge.canned ? "Canned grades: a demonstration only. Nothing may be concluded from them." : null,
    report.simulated_graders ? "The graders are simulated, not clinicians. Nothing may be concluded from this report." : null,
    judge.same_model_as_agent ? "The judge runs on the same model as the agent it grades: models favour their own text." : null,
  ].filter(Boolean);
  const floors = report.floors;
  const seeds = report.seeds;
  const rows = Object.entries(report.criteria).map(([key, c]) => {
    const j = c.judge_agreement;
    const missed = c.judge_errors;
    return `<tr data-criterion="${esc(key)}">
    <th scope="row">${esc(slotText(key))}${c.must_pass ? ` <span class="chip">must not fail</span>` : ""}</th>
    <td>${figureHtml(c.clinicians.passing)}</td>
    <td>${esc(kappaText(c.clinician_agreement.kappa))}</td>
    <td>${esc(kappaText(j.kappa))}<br>on pass or fail: ${esc(kappaText(j.pass_fail_kappa))}</td>
    <td>clinicians ${figureHtml(j.passing.clinicians)}<br>judge ${figureHtml(j.passing.judge)}${j.symmetry.leaning
      ? `<br><span class="chip">the judge leans ${esc(j.symmetry.leaning)}</span>` : ""}</td>
    <td>${j.failures_found ? figureHtml(j.failures_found) : "n/a"}</td>
    <td>${esc(missed.paired.k)} of ${esc(missed.paired.n)} graded by clinicians<br>${esc(missed.ungraded.k)} of ${esc(missed.ungraded.n)} others</td>
    <td><strong>${esc(c.validation.state)}</strong>${c.validation.reasons.length
      ? `<ul>${c.validation.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}</td>
    <td>${c.judged ? `${figureHtml(c.judged.passing)}<br>it could not grade ${esc(c.judged.could_not_grade)} more` : "withheld"}</td>
  </tr>`;
  }).join("");
  return `${notes.map((n) => `<p><strong>${esc(n)}</strong></p>`).join("")}
    <p class="muted">Judge ${esc(judge.model ?? "not named")} · ${esc(report.cases.with_output)} outputs,
      ${esc(report.cases.judged)} judged, ${esc(report.cases.graded)} graded by clinicians
      (${esc(report.cases.graded_twice)} by two or more).
      Floors: kappa's lower bound at least ${esc(floors.agreement)}, over the levels and on pass or fail; on a
      must-not-fail criterion, failures found at least ${esc(floors.must_pass_recall)}; ${esc(floors.min_paired)} outputs
      graded by both, ${esc(floors.min_each_side)} on each side of the pass line; the judge may fail to grade at most
      ${esc(floors.max_judge_errors)} of the outputs. Bootstrap seed ${esc(seeds.bootstrap)}, ${esc(seeds.resamples)} resamples.</p>
    <div class="table-scroll" data-table="written-report"><table class="data">
      <caption>The judge's agreement with clinicians, criterion by criterion</caption>
      <thead><tr><th scope="col">Criterion</th><th scope="col">Clinicians: passing</th>
        <th scope="col">Clinician with clinician (kappa)</th><th scope="col">Judge with clinicians (kappa)</th>
        <th scope="col">Passing, on the outputs both graded</th>
        <th scope="col">Failures the judge found</th><th scope="col">The judge could not grade</th>
        <th scope="col">Judge</th>
        <th scope="col">Judged, ungraded outputs: passing</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

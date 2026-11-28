// SPDX-License-Identifier: Apache-2.0
// Agent-generic rendering: inputs, providers and what labels lead to.

import { chartHtml } from "./charts.js";
import { recordHtml } from "./record.js";
import { esc, may, num, pct, signedIn, slotText, state } from "./ui.js";

// An agent's words for its questions and labels (spec §4.3 `display`, `names`), else the key as
// words, spelled as the server spells it (core/spec.py `readable`). An agent's names are its
// author's claim, so the code stays one click away: `named` puts it beside the name, shown only
// once someone presses "Show codes" (`codesToggle`).
export const readable = (key) => {
  const words = String(key).replaceAll("_", " ").trim();
  return words.slice(0, 1).toUpperCase() + words.slice(1);
};
// Own keys only: a label may be named like an object's built-in (`constructor`).
const own = (object, key) => (object && Object.hasOwn(object, key) ? object[key] : undefined);
const question = (key) => own(state.detail.manifest.questions, key);
export const questionName = (key) => question(key)?.display ?? readable(key);
export const labelName = (key, label) => own(question(key)?.names, label) ?? readable(label);
// The space is the code's own, so a name and its code never run together.
export const named = (name, code) => `${esc(name)}<span class="code-ref"> <code>${esc(code)}</code></span>`;

// A toggle button keeps its name; whether it is on is its pressed state (WAI-ARIA).
export const codesToggle = () => `<button type="button" class="linklike" data-codes aria-pressed="false">Show codes</button>`;

// "Show codes" shows every code under `scope` beside its name, and hides them again.
export function wireCodes(scope) {
  scope.querySelectorAll("[data-codes]").forEach((button) => button.addEventListener("click", () => {
    button.setAttribute("aria-pressed", String(scope.classList.toggle("show-codes")));
  }));
}

// A decision's reasons in words, for the main view. A rule's reason is the agent author's own
// sentence; the engine's reasons are said plainly, keeping what a person must know (which answer
// is missing, that safety checks may be incomplete, what a refused input lacks). Only reasons the
// engine alone needs are left to the decision record: a threshold that was met. Anything else not
// recognised is shown as written, never dropped.
export function plainReasons(r) {
  const plain = (r.reasons ?? []).map((reason) => plainReason(reason, r)).filter((x) => x !== null);
  return [...new Set(plain)];
}

const ENGINE_ONLY = /^confidence [\d.]+ >= auto threshold [\d.]+$/;

function plainReason(reason, r) {
  const rule = r.rule ? `rule ${r.rule}: ` : null;  // the rule's name may hold anything, a colon too
  if (rule && reason.startsWith(rule)) return reason.slice(rule.length);
  if (ENGINE_ONLY.test(reason)) return null;
  const refused = reason.match(/^input rejected: ([\s\S]+)$/);
  if (refused) return `The input was refused: ${refused[1]}`;
  const failed = reason.match(/^provider failure \([^)]*\); ([\s\S]+)$/);
  if (failed) {
    const missing = Object.keys(r.errors ?? {}).map(questionName);
    return `${missing.length ? `No answer for ${missing.join(", ")}` : "Part of the engine could not answer"}; ${failed[1]}, so a person checks.`;
  }
  const unanswered = reason.match(/^no answer for '(.+?)' /);
  if (unanswered) return `No answer for ${questionName(unanswered[1])}, so a person decides.`;
  const hold = reason.match(/^automation is on hold: ([\s\S]+)$/);
  if (hold) return /^hold [0-9a-f]+$/.test(hold[1]) ? "Automation is on hold for this agent, so a person checks." : `Automation is on hold: ${hold[1]}`;
  const never = reason.match(/^policy never automates '(.+)'$/);
  if (never) return `${labelName(r.primary, never[1])} is never automated: a person checks.`;
  const approved = reason.match(/^risk level \d+ AUTO permitted by governance override \(([\s\S]+)\)$/);
  if (approved) return `Automation at this agent's risk level was approved (${approved[1]}).`;
  const said = [
    [/ < review threshold /, "Not sure enough to recommend, so a person decides."],
    [/ is below the auto threshold /, "Not sure enough to act alone, so a person checks."],
    [/^automation requires /, "This deployment's safety screen is not one this agent automates with, so a person checks."],
    [/^risk level \d+ requires human review$/, "This agent's risk level needs a person to check."],
  ];
  return said.find(([pattern]) => pattern.test(reason))?.[1] ?? reason;
}

// One question's answer as the engine gave it, for the decision record: each label's probability,
// the provider and an ordinal's expected score. `own`: the chosen agent made it, so its words and
// its policy's tones apply; another agent's answer gets neither.
export function answerChart(id, question, a, { own = true } = {}) {
  const words = own ? questionText(question) : slotText(question);
  const rows = Object.entries(a.probabilities ?? {}).sort((x, y) => y[1] - x[1])
    .map(([label, value]) => ({ label, value, chosen: label === a.label, tone: own ? labelTone(question, label) : "" }));
  return `<h4>${esc(words)} <span class="muted"><span class="mono">${esc(question)}</span> · ${providerWords(a.provider)}${a.value !== undefined ? ` · expected score ${num(a.value, 2)}` : ""}</span></h4>
    ${rows.length ? chartHtml({
      id: `answer-${id}-${question}`,
      type: "bars",
      ariaLabel: `${words} ${rows.map((row) => `${row.label} ${pct(row.value)}`).join(", ")}. Chosen: ${a.label}.`,
      rows,
    }) : `<p class="muted">${esc(a.label)}</p>`}
    ${a.evidence?.length ? `<div class="chips mt-2">${a.evidence.map((e) => `<span class="chip">${esc(e)}</span>`).join("")}</div>` : ""}`;
}

export const originBadge = (agent) => (agent?.origin_label
  ? `<span class="badge origin">${esc(agent.origin_label)}</span>`
  : "");

// What the agent is (`status_label`, derived from its card.provenance, never written by the agent):
// a synthetic example, and not clinically reviewed, wherever the agent is shown.
export const statusNotice = (agent) => (agent?.status_label
  ? `<p class="callout warning status-notice"><b>${esc(agent.status_label)}</b></p>`
  : "");

// The automation holds in force (GET /api/holds) that cover this agent: its own, or every agent's.
export const holdsFor = (holds, agent) => (holds ?? []).filter((h) =>
  (h.agent_id == null || h.agent_id === agent.id) && (h.agent_digest == null || h.agent_digest === agent.digest));

// `holds` null: they could not be read, which is said, never taken for none.
export const holdNotice = (holds, agent) => {
  if (holds === null) {
    return `<p class="callout warning holds-unread"><b>The automation holds could not be read</b>, so this page cannot say
      whether one covers this agent. Reload it to try again.</p>`;
  }
  return holdsFor(holds, agent).length
    ? `<p class="callout danger hold-notice"><b>Automation is on hold.</b> A person reviews every decision this agent would
      have automated, with the same recommendation and priority. See the Decision Card.</p>`
    : "";
};

// The picker's groups, in this order; one group only, and the picker is a plain list. An agent of
// an origin not listed here is in a group of its own at the end, so none is ever left out.
const ORIGIN_GROUPS = [
  ["maintained", "Maintained"],
  ["hub", "From a registry"],
  ["store", "From this organization's store"],
  ["local", "Local agents"],
];

export function agentOptions(agents) {
  const option = (p) => `<option value="${esc(p.id)}">${esc([p.name, p.version].filter(Boolean).join(" "))}</option>`;
  const originOf = (p) => p.origin ?? "maintained";
  const listed = new Set(ORIGIN_GROUPS.map(([origin]) => origin));
  const others = [...new Set(agents.map(originOf).filter((origin) => !listed.has(origin)))];
  const groups = [...ORIGIN_GROUPS, ...others.map((origin) => [origin, origin])]
    .map(([origin, label]) => [label, agents.filter((p) => originOf(p) === origin)])
    .filter(([, members]) => members.length);
  if (groups.length < 2) return agents.map(option).join("");
  return groups.map(([label, members]) => `<optgroup label="${esc(label)}">${members.map(option).join("")}</optgroup>`).join("");
}

// The agent a first visit opens on: the first maintained one, else the first served.
export const defaultAgent = (agents) => (agents.find((p) => p.origin === "maintained") ?? agents[0]).id;

// Agents whose input is a patient message keep a message-shaped UI.
export const isMessageAgent = () => Boolean(state.detail.input_schema?.properties?.message);

// `restricted`: PHI mode `encrypt` kept the input, but this role may not read it (the server says).
export function inputView(input, { restricted = false } = {}) {
  if (restricted) return `<div class="message muted">Input kept encrypted (PHI mode): reviewers and administrators can read it, and each read is logged.</div>`;
  if (!input) {
    const why = state.phiMode === "off" ? "the agent keeps only its hash (audit.store_input)" : "PHI mode keeps only its hash";
    return `<div class="message muted">Input not stored: ${why}.</div>`;
  }
  return recordHtml(input);
}

// A question in words: the first sentence of its instructions, which its author writes for people
// ("Is the patient aged 18 to 75 on the screening date?"); the code follows as secondary text.
export function questionText(question) {
  const instructions = state.detail.manifest.questions[question]?.instructions ?? "";
  return instructions.match(/^.*?[?.](?=\s|$)/)?.[0] ?? slotText(question);
}

export const providerWords = (p) => `<span title="${esc(p.variant)}${p.model ? ` · ${esc(p.model)}` : ""}">${esc(providerName(p))}</span>`;

// A critical label in words: "Verdict: block", "Urgency: urgent" (the codes stay in tooltips).
export const criticalText = (question, label) => `${slotText(question)}: ${label}`;

// Plain words for variant names ("rules_and_llm" -> "Rules + LLM"); the raw configuration stays in
// the row's tooltip.
const VARIANT_WORDS = { rules: "Rules", llm: "LLM" };

export const variantText = (variant) => variant.split("_and_").map((part) => VARIANT_WORDS[part] ?? part.replaceAll("_", " ")).join(" + ");

const REMOTE_VARIANTS = new Set(["llm"]);

// A provider in words, with the model where one runs: "Rules + model-2", "model-2",
// "Rules". A remote part is named by its model, so nothing is said twice; models of other
// variants (a check's members) follow in brackets.
export function providerName(p) {
  if (!p) return "–";
  const models = remoteModels(p.model, p.type).map(modelText);
  let used = 0;
  const words = p.variant.split("_and_").map((part) =>
    (REMOTE_VARIANTS.has(part) && models[used] ? models[used++] : VARIANT_WORDS[part] ?? part.replaceAll("_", " "))).join(" + ");
  return used < models.length ? `${words} (${models.slice(used).join(", ")})` : words;
}

const COMPOSITES = new Set(["check"]);

// The remote models behind a provider ("rules:rules@1a2b + llm:vendor/model-2" ->
// ["vendor/model-2"]); local fingerprints ("rules@1a2b") are not a deployment choice. Only a
// composite's model is "member:model" parts (joined by " + "), as leaderboard.config_label
// reads it; any other model is one id, colons and all ("vendor.model-v1:0", "model:8b").
export const remoteModels = (model, type) => (COMPOSITES.has(type)
  ? (model ?? "").split(" + ").map((part) => (part.includes(":") ? part.slice(part.indexOf(":") + 1) : part))
  : [model ?? ""])
  .filter((m) => m && !m.includes("@"));

// A model as its id says it, without the gateway's vendor prefix ("vendor/model-2" -> "model-2"),
// whoever makes it.
function modelText(id) {
  return id.split("/").pop();
}

// What choosing this label leads to under the agent's own policy: a block rule is danger; a review
// rule, or a primary label the policy never automates, is warning. Other labels get no tone.
export function labelTone(question, label) {
  const policy = state.detail.manifest.policy;
  const rules = policy.rules.filter((rule) => rule.when.question === question && rule.when.labels?.includes(label));
  if (rules.some((rule) => rule.outcome === "BLOCK")) return "block";
  if (rules.some((rule) => rule.outcome === "HUMAN_REVIEW")) return "review";
  if (question === policy.primary && policy.labels?.[label]?.allow_auto === false) return "review"; // never automated
  return "";
}

// -- proposed actions (spec §13) -------------------------------------------------------------------
// What an agent asked to have done, and what may be done with each: released (the caller may run
// it), held (it waits for the decision's reviewer), blocked or denied. Mendari runs none.
const DISPOSITION_TEXT = { released: "Released", held: "Held", blocked: "Blocked", denied: "Denied" };
const DISPOSITION_TONE = { released: "s-ok", held: "s-warning", blocked: "s-degraded", denied: "s-degraded" };
const ACTION_REASON = {
  with_decision: "its effect needs only a decision that is not blocked",
  automated: "released with the automated decision",
  confirmed: "the reviewer confirmed the decision",
  awaiting_review: "waits for this decision's reviewer",
  undeclared: "the agent's manifest declares no such action",
  invalid_arguments: "its arguments do not match its schema",
  composite_member: "proposed by a member of a composite, which is not taken",
  agent_changed: "another version of the agent made this decision",
  decision_blocked: "the decision is blocked",
  no_recommendation: "there is no recommendation for a reviewer to confirm",
  no_reviewer: "it needs a person, and this decision was not sent to one",
  not_automated: "the agent may not automate now",
  earlier_refusal: "an earlier action on this decision was refused",
  review_closed: "asked for after the review was resolved",
  overridden: "the reviewer overrode the recommendation",
  reviewer_denied: "the reviewer refused it",
  // Free text is released by name (ADR-0027): its arguments can hold free text, or a text of
  // the decision was withheld, and the review that confirmed the decision did not name it.
  not_named: "not released: the review did not name it",
};
const ARGUMENTS_STATUS = {
  sealed: "Arguments kept sealed: reviewers and administrators can read them, and each read is logged.",
  erased: "Arguments erased.",
  missing: "Arguments missing: verify the chain with --notes.",
  not_kept: "Arguments not kept: only their hash is.",
};

export const effectBadge = (effect) => (effect
  ? `<span class="chip" title="effect: ${esc(effect)}">${esc(effect)}</span>`
  : `<span class="chip" title="the agent's manifest declares no such action">undeclared</span>`);

export const dispositionBadge = (disposition) =>
  `<span class="badge ${DISPOSITION_TONE[disposition] ?? "s-degraded"}" title="${esc(disposition)}">${esc(DISPOSITION_TEXT[disposition] ?? disposition)}</span>`;

export const actionReason = (reason) => ACTION_REASON[reason] ?? reason;

// An action by the name its manifest declares; an undeclared one has none the chain keeps, and
// the name the agent gave it is shown only with its arguments, to whoever may read them.
export const actionName = (action) => (action.name
  ? named(readable(action.name), action.name)
  : `An action the agent did not declare${action.proposed_name ? ` (<code>${esc(action.proposed_name)}</code>)` : ""}`);

function actionArguments(action) {
  if (action.arguments_status !== "open") return `<p class="muted">${esc(ARGUMENTS_STATUS[action.arguments_status] ?? "Arguments not shown.")}</p>`;
  const given = Object.entries(action.arguments ?? {});
  if (!given.length) return `<p class="muted">No arguments.</p>`;
  return `<dl class="facts">${given.map(([key, value]) =>
    `<dt>${esc(key)}</dt><dd class="break-anywhere">${esc(typeof value === "string" ? value : JSON.stringify(value))}</dd>`).join("")}</dl>`;
}

// `choose`: a reviewer picks which held actions confirming releases, each by its tick
// (`releaseChoice`). `brief`: what became of each, without its arguments (a review's answer
// has none).
export function actionsList(actions, { choose = false, brief = false } = {}) {
  if (!actions?.length) return "";
  return `<ul class="actions-list" aria-label="Proposed actions">${actions.map((action) => `<li data-action="${esc(action.id)}" data-disposition="${esc(action.disposition)}">
    <div class="row"><b>${actionName(action)}</b> ${effectBadge(action.effect)} ${dispositionBadge(action.disposition)}
      <span class="muted">${esc(actionReason(action.reason))}</span></div>
    ${brief ? "" : actionArguments(action)}
    ${choose && action.disposition === "held" ? releaseChoice(action) : ""}
  </li>`).join("")}</ul>`;
}

// What an unticked held action is told (the review view keeps it in step with the tick), and
// why one starts unticked.
export const NOT_RELEASED = "Will not be released.";
const FREE_TEXT = "Its arguments can hold free text: read them before releasing.";

// A held action's tick. Ticked, the action is released when the reviewer confirms the
// decision. An action whose arguments can hold free text (`free_text`, ADR-0027) starts
// unticked and says why: nobody releases what they have not read. Any other starts ticked, as
// it always did. The tick is described by what it is told, so a screen reader says it too.
function releaseChoice(action) {
  const id = esc(action.id);
  const free = action.free_text === true;
  return `<div class="release-choice">
      <label class="pick"><input type="checkbox" data-release="${id}"${free ? ` data-free-text aria-describedby="why-${id} off-${id}"` : " checked"}> Release when I confirm</label>
      <span class="release-off" id="off-${id}"${free ? "" : " hidden"}>${free ? NOT_RELEASED : ""}</span>
    </div>
    ${free ? `<p class="muted release-why" id="why-${id}">${FREE_TEXT}</p>` : ""}`;
}

// -- held texts (ADR-0027) ------------------------------------------------------------------------
// What an agent wrote for a decision that went to a person. Each text is held until the review
// gives it a verdict: released (it may be sent or filed as written) or withheld (it may not).
// A text is what its agent wrote: it is shown as text, every character escaped, with its line
// breaks, and never run as markup.
const TEXT_STATE = { held: "Held", released: "Released", withheld: "Withheld" };
const TEXT_TONE = { held: "s-warning", released: "s-ok", withheld: "s-degraded" };
const VERDICTS = [["release", "Release as written"], ["withhold", "Withhold"]];

export const textStateBadge = (stands) =>
  `<span class="badge ${TEXT_TONE[stands] ?? "s-degraded"}" title="${esc(stands)}">${esc(TEXT_STATE[stands] ?? stands)}</span>`;

// A text by the name its manifest declares for the output.
export const textName = (name) => named(readable(name), name);

// Whoever reads this decision reads what it keeps: anyone under PHI mode off, else someone
// signed in who may read inputs. A text still sealed for them is one the deployment's keys do
// not open.
const readsInputs = () => state.phiMode === "off" || (signedIn() && may("read_input"));

// Why a held text is not on the screen, in words.
function textUnshown(status) {
  if (status === "sealed") {
    return readsInputs()
      ? "Kept sealed under a key this deployment does not hold."
      : "Kept sealed: reviewers and administrators can read it, and each read is logged.";
  }
  if (status === "erased") return "Erased, with what this decision kept of its input.";
  if (status === "missing") return "Missing: verify the chain with --notes.";
  if (status !== "not_kept") return "Not shown here.";
  // Kept only where the decision's input is: never under PHI mode hash, nor for an agent that
  // keeps only its input's hash.
  return state.phiMode === "hash"
    ? "Not kept here: PHI mode hash keeps no free text."
    : "Not kept here: this agent keeps only a hash of its input (audit.store_input).";
}

// The text in a box of its own that scrolls (a text can be 8,000 characters), in the tab order
// so the keyboard scrolls it; or why there is none. `choose`: the verdict is still owed.
function textBody(text, choose) {
  if (text.text_status === "open" && typeof text.text === "string") {
    return `<div class="message text-body" tabindex="0" role="group" aria-label="${esc(readable(text.name))}: the text">${esc(text.text)}</div>`;
  }
  return `<p class="muted text-missing">${esc(textUnshown(text.text_status))}${choose ? " Read the draft where your own system shows it, then give your verdict." : ""}</p>`;
}

// The two verdicts of one text, neither chosen: the reviewer says which.
const verdictChoice = (id, name) => `<div class="label-options">${VERDICTS.map(([verdict, words]) =>
  `<label class="label-option"><input type="radio" name="verdict-${esc(id)}-${esc(name)}" value="${verdict}" data-verdict="${esc(name)}"> <span>${words}</span></label>`).join("")}</div>`;

// A decision's texts (`outputs`: each one's `name`, `state` and, where it is shown, `text`).
// `choose`: a reviewer gives each held text its verdict (`id`: the decision's, which names
// each group). `described`: the chosen agent made the decision, so its manifest says what each
// output is. `brief`: where each stands and no more (a review's answer has no text).
export function textsList(outputs, { choose = false, id = "", described = true, brief = false } = {}) {
  if (!outputs?.length) return "";
  return `<ul class="texts-list" aria-label="Texts the agent wrote">${outputs.map((text) => {
    const asked = choose && text.state === "held";
    const head = `<span class="row"><b>${textName(text.name)}</b> ${textStateBadge(text.state)}</span>`;
    const what = described ? own(state.detail?.manifest?.outputs, text.name)?.description : null;
    const body = brief ? "" : `${what ? `<p class="muted">${esc(what)}</p>` : ""}${textBody(text, asked)}`;
    return `<li data-output="${esc(text.name)}" data-state="${esc(text.state)}">${asked
      ? `<fieldset class="text-verdict"><legend>${head}</legend>${body}${verdictChoice(id, text.name)}</fieldset>`
      : `${head}${body}`}</li>`;
  }).join("")}</ul>`;
}

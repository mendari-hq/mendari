// SPDX-License-Identifier: Apache-2.0
// Pack-generic rendering: inputs, criteria tables, providers and what labels lead to.

import { chartHtml } from "./charts.js";
import { esc, num, pct, slotText, state } from "./ui.js";

// Where a pack comes from (`origin`, from /api/packs): set by where the deployment found it, never
// by its manifest. A community pack is not clinically reviewed and never automates
// (core/origin.py); every view that shows a pack says so.
export const isCommunity = (pack) => pack?.origin === "community";

// A pack's words for its questions and labels (spec 0.2 `display`, `names`), else the key as
// words, spelled as the server spells it (core/spec.py `readable`). A pack's names are its
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

// A decision's reasons in words, for the main view. A rule's reason is the pack author's own
// sentence; the engine's reasons are said plainly, keeping what a person must know (which answer
// is missing, that safety checks may be incomplete, what a refused input lacks). Only reasons the
// engine alone needs are left to the decision record: a threshold that was met, and a derived
// decision's table rows (the criteria show them). Anything else not recognised is shown as
// written, never dropped.
export function plainReasons(r) {
  const plain = (r.reasons ?? []).map((reason) => plainReason(reason, r)).filter((x) => x !== null);
  return [...new Set(plain)];
}

const ENGINE_ONLY = /^confidence [\d.]+ >= auto threshold [\d.]+$/;

function plainReason(reason, r) {
  const rule = r.rule ? `rule ${r.rule}: ` : null;  // the rule's name may hold anything, a colon too
  if (rule && reason.startsWith(rule)) return reason.slice(rule.length);
  if (ENGINE_ONLY.test(reason)) return null;
  if (derivedPrimary() && (state.detail.labels ?? []).some((label) => reason.startsWith(`${label}: `))) return null;
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
  if (hold) return /^hold [0-9a-f]+$/.test(hold[1]) ? "Automation is on hold for this pack, so a person checks." : `Automation is on hold: ${hold[1]}`;
  const never = reason.match(/^policy never automates '(.+)'$/);
  if (never) return `${labelName(r.primary, never[1])} is never automated: a person checks.`;
  const approved = reason.match(/^risk level \d+ AUTO permitted by governance override \(([\s\S]+)\)$/);
  if (approved) return `Automation at this pack's risk level was approved (${approved[1]}).`;
  const said = [
    [/ < review threshold /, "Not sure enough to recommend, so a person decides."],
    [/ is below the auto threshold /, "Not sure enough to act alone, so a person checks."],
    [/^automation requires /, "This deployment's safety screen is not one this pack automates with, so a person checks."],
    [/^risk level \d+ requires human review$/, "This pack's risk level needs a person to check."],
    [/^a community pack .* never blocks on its own/, "A community pack never blocks on its own: a person decides."],
    [/^a community pack .* recommends and never automates$/, "A community pack only recommends: a person checks."],
  ];
  return said.find(([pattern]) => pattern.test(reason))?.[1] ?? reason;
}

// One question's answer as the engine gave it, for the decision record: each label's probability,
// the provider and an ordinal's expected score.
export function answerChart(id, question, a, answers) {
  const rows = Object.entries(a.probabilities ?? {}).sort((x, y) => y[1] - x[1])
    .map(([label, value]) => ({ label, value, chosen: label === a.label, tone: labelTone(question, label, answers) }));
  return `<h4>${esc(questionText(question))} <span class="muted"><span class="mono">${esc(question)}</span> · ${providerWords(a.provider)}${a.value !== undefined ? ` · expected score ${num(a.value, 2)}` : ""}</span></h4>
    ${rows.length ? chartHtml({
      id: `answer-${id}-${question}`,
      type: "bars",
      ariaLabel: `${questionText(question)} ${rows.map((row) => `${row.label} ${pct(row.value)}`).join(", ")}. Chosen: ${a.label}.`,
      rows,
    }) : `<p class="muted">${esc(a.label)}</p>`}
    ${a.evidence?.length ? `<div class="chips mt-2">${a.evidence.map((e) => `<span class="chip">${esc(e)}</span>`).join("")}</div>` : ""}`;
}

export const originBadge = (pack) => (pack?.origin_label
  ? `<span class="badge origin${isCommunity(pack) ? " origin-community" : ""}">${esc(pack.origin_label)}</span>`
  : "");

// What the pack is (`status_label`, derived from its card.provenance, never written by the pack):
// a synthetic example, and not clinically reviewed, wherever the pack is shown.
export const statusNotice = (pack) => (pack?.status_label
  ? `<p class="callout warning status-notice"><b>${esc(pack.status_label)}</b></p>`
  : "");

// The automation holds in force (GET /api/holds) that cover this pack: its own, or every pack's.
export const holdsFor = (holds, pack) => (holds ?? []).filter((h) =>
  (h.pack_id == null || h.pack_id === pack.id) && (h.pack_digest == null || h.pack_digest === pack.digest));

export const holdNotice = (holds, pack) => (holdsFor(holds, pack).length
  ? `<p class="callout danger hold-notice"><b>Automation is on hold.</b> A person reviews every decision this pack would
      have automated, with the same recommendation and priority. See the Decision Card.</p>`
  : "");

export const communityNotice = (pack) => (isCommunity(pack)
  ? `<div class="callout community-notice"><b>${esc(pack.origin_label)}.</b> An outside developer contributed this pack,
      and Mendari has not clinically reviewed it. It recommends and never automates: a person confirms every decision.</div>`
  : "");

// The picker's groups, in this order; one group only, and the picker is a plain list.
const ORIGIN_GROUPS = [
  ["maintained", "Maintained"],
  ["hub", "From a registry"],
  ["local", "Local packs"],
  ["community", "Community · not clinically reviewed"],
];

// A community pack's option says so too: a closed picker shows only the chosen option's text, and
// a community pack may share a word or two of its name with another.
export function packOptions(packs) {
  const option = (p) => `<option value="${esc(p.id)}">${esc([p.name, p.version, isCommunity(p) ? "(community)" : ""].filter(Boolean).join(" "))}</option>`;
  const groups = ORIGIN_GROUPS.map(([origin, label]) => [label, packs.filter((p) => (p.origin ?? "maintained") === origin)])
    .filter(([, members]) => members.length);
  if (groups.length < 2) return packs.map(option).join("");
  return groups.map(([label, members]) => `<optgroup label="${esc(label)}">${members.map(option).join("")}</optgroup>`).join("");
}

// The pack a first visit opens on: the first maintained one, never a community pack while any
// other is served (packs are listed by id, so `community/…` would often come first).
export const defaultPack = (packs) =>
  (packs.find((p) => p.origin === "maintained") ?? packs.find((p) => !isCommunity(p)) ?? packs[0]).id;

// Packs whose input is a patient message (the inbox) keep a message-shaped UI.
export const isMessagePack = () => Boolean(state.detail.input_schema?.properties?.message);

// `restricted`: PHI mode `encrypt` kept the input, but this role may not read it (the server says).
export function inputView(input, { restricted = false } = {}) {
  if (restricted) return `<div class="message muted">Input kept encrypted (PHI mode): reviewers and administrators can read it, and each read is logged.</div>`;
  if (!input) {
    const why = state.phiMode === "off" ? "the pack keeps only its hash (audit.store_input)" : "PHI mode keeps only its hash";
    return `<div class="message muted">Input not stored: ${why}.</div>`;
  }
  if (input.message) {
    const m = input.message;
    return `${m.subject ? `<div class="mt-2"><b>${esc(m.subject)}</b></div>` : ""}<div class="message">${esc(m.body ?? "")}</div>`;
  }
  const fields = [], texts = [], lists = [];
  const shown = (v) => (v !== null && typeof v === "object" ? JSON.stringify(v) : String(v));
  for (const [key, value] of Object.entries(input)) {
    if (Array.isArray(value)) lists.push([key, value]);
    else if (value !== null && typeof value === "object") Object.entries(value).forEach(([k, v]) => fields.push([`${key}.${k}`, shown(v)]));
    else if (typeof value === "string" && value.length > 60) texts.push([key, value]);
    else fields.push([key, shown(value)]);
  }
  const listView = ([key, items]) => {
    if (!items.length) return `<div class="list-name">${esc(key)} <span class="muted">none</span></div>`;
    if (!items.every((i) => i !== null && typeof i === "object")) return `<div class="list-name">${esc(key)} <span>${items.map((i) => esc(shown(i))).join(", ")}</span></div>`;
    const columns = [...new Set(items.flatMap((i) => Object.keys(i)))];
    return `<div class="list-name">${esc(key)} <span class="muted">${items.length}</span></div><div class="table-wrap"><table class="compact">
      <tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>
      ${items.map((i) => `<tr>${columns.map((c) => `<td>${esc(i[c] === undefined ? "" : shown(i[c]))}</td>`).join("")}</tr>`).join("")}</table></div>`;
  };
  return `<div class="record">
    ${fields.length ? `<div class="chips">${fields.map(([k, v]) => `<span class="chip">${esc(k)} = ${esc(v)}</span>`).join("")}</div>` : ""}
    ${lists.map(listView).join("")}
    ${texts.map(([k, v]) => `<div class="list-name">${esc(k)}</div><div class="message">${esc(v)}</div>`).join("")}
  </div>`;
}

const TONE_BADGE = { block: "o-BLOCK", review: "o-HUMAN_REVIEW", neutral: "o-ABSTAIN" };

// For a derived primary (e.g. eligibility), the member answers are what a reviewer checks:
// one row per criterion, with what that answer does in the decision table.
export function derivedPrimary() {
  const primary = state.detail.manifest.questions[state.detail.primary];
  return primary && primary.derive ? primary.derive : null;
}

// What a member's answer does in the table, given every member's answer (for `when` rows).
function memberEffect(derive, member, label, answers) {
  for (const row of derive.table) {
    if (row.else !== undefined && row.else !== null) return null;
    if (row.in && !row.in.includes(member)) continue;
    const gateOpen = !row.when || Object.entries(row.when).every(([m, labels]) => labels.includes(answers[m]?.label));
    if (gateOpen && rowLabels(row.any).includes(label)) return row.then;
  }
  return null;
}

// The criteria of a derived decision. `plain` (the main result view): each criterion, answer and
// effect by name, the codes one click away; otherwise (the record, the review queue) the engine's
// view, with confidence, source and evidence.
export function criteriaTable(answers, { evidence = true, plain = false } = {}) {
  const derive = derivedPrimary();
  if (!derive || !answers) return "";
  const questions = state.detail.manifest.questions;
  if (plain) {
    const primary = state.detail.primary;
    const rows = derive.from.map((member) => {
      const a = answers[member];
      const effect = a ? memberEffect(derive, member, a.label, answers) : null;
      const tone = effect ? labelTone(primary, effect, answers) || "neutral" : "";
      return `<tr class="${effect ? `hit hit-${tone}` : ""}">
        <td class="criterion">${named(questionName(member), member)}</td>
        <td>${a ? `<b>${named(labelName(member, a.label), a.label)}</b>` : `<span class="muted">no answer</span>`}</td>
        <td class="mono">${a && a.confidence !== null && a.confidence !== undefined ? pct(a.confidence, 0) : ""}</td>
        <td>${effect ? `<span class="badge ${TONE_BADGE[tone]}">→ ${named(labelName(primary, effect), effect)}</span>` : ""}</td>
      </tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="compact criteria">
      <tr><th>Criterion</th><th>Answer</th><th>Sure</th><th>Effect</th></tr>${rows}</table></div>`;
  }
  const rows = derive.from.map((member) => {
    const a = answers[member];
    const effect = a ? memberEffect(derive, member, a.label, answers) : null;
    const tone = effect ? labelTone(state.detail.primary, effect, answers) || "neutral" : "";
    return `<tr class="${effect ? `hit hit-${tone}` : ""}">
      <td class="criterion" title="${esc(questions[member]?.instructions ?? "")}">${esc(questionText(member))} <span class="mono muted">${esc(member)}</span></td>
      <td>${a ? `<b>${esc(a.label)}</b>` : `<span class="muted">no answer</span>`}</td>
      <td class="mono">${a ? pct(a.confidence, 0) : ""}</td>
      <td>${effect ? `<span class="badge ${TONE_BADGE[tone]}">→ ${esc(effect)}</span>` : ""}</td>
      <td class="muted">${a ? providerWords(a.provider) : ""}</td>
      ${evidence ? `<td class="evidence">${a && a.evidence.length ? esc(a.evidence.join("; ")) : ""}</td>` : ""}
    </tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="compact criteria">
    <tr><th>Criterion</th><th>Answer</th><th>Conf.</th><th>Effect</th><th>Source</th>${evidence ? "<th>Evidence</th>" : ""}</tr>${rows}</table></div>`;
}

// A question in words: the first sentence of its instructions, which the pack writes for people
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
const VARIANT_WORDS = { rules: "Rules", llm: "LLM", jev: "Jev", naive_bayes: "Naive Bayes", cascade: "Cascade", vote: "Vote", criteria: "Criteria", dosing: "Dosing" };

export const variantText = (variant) => variant.split("_and_").map((part) => VARIANT_WORDS[part] ?? part.replaceAll("_", " ")).join(" + ");

const REMOTE_VARIANTS = new Set(["jev", "llm"]);

// A provider in words, with the model where one runs: "Rules + model-2", "Jev 1.13.0",
// "Naive Bayes". A remote part is named by its model, so nothing is said twice; models of other
// variants (a cascade, a vote) follow in brackets.
export function providerName(p) {
  if (!p) return "–";
  const models = remoteModels(p.model, p.type).map(modelText);
  let used = 0;
  const words = p.variant.split("_and_").map((part) =>
    (REMOTE_VARIANTS.has(part) && models[used] ? models[used++] : VARIANT_WORDS[part] ?? part.replaceAll("_", " "))).join(" + ");
  return used < models.length ? `${words} (${models.slice(used).join(", ")})` : words;
}

const COMPOSITES = new Set(["any_of", "cascade", "vote"]);

// The remote models behind a provider ("rules:rules@1a2b + llm:vendor/model-2" ->
// ["vendor/model-2"]); local fingerprints ("rules@1a2b") are not a deployment choice. Only a
// composite's model is "member:model" parts (" + ", or " → " for a cascade), as
// leaderboard.config_label reads it; any other model is one id, colons and all
// ("vendor.model-v1:0", "model:8b").
export const remoteModels = (model, type) => (COMPOSITES.has(type)
  ? (model ?? "").split(/ \+ | → /).map((part) => (part.includes(":") ? part.slice(part.indexOf(":") + 1) : part))
  : [model ?? ""])
  .filter((m) => m && !m.includes("@"));

// A model as its id says it, without the gateway's vendor prefix ("vendor/model-2" -> "model-2"),
// whoever makes it; Jev's own versions read as the variant does ("jev-1.13.0" -> "Jev 1.13.0").
function modelText(id) {
  const name = id.split("/").pop();
  const jev = name.match(/^jev-(\d[\w.-]*)$/);
  return jev ? `Jev ${jev[1]}` : name;
}

// `any` / `all` hold a label or a list of labels.
export const rowLabels = (value) => (value === null || value === undefined ? [] : Array.isArray(value) ? value : [value]);

// What choosing this label leads to under the pack's own policy: a block rule is danger; a review
// rule, or a primary label the policy never automates, is warning. A criterion of a derived
// decision takes the tone of what that answer would make the decision (the Effect column of the
// criteria table). Other labels get no tone.
export function labelTone(question, label, answers) {
  const policy = state.detail.manifest.policy;
  const rules = policy.rules.filter((rule) => rule.when.question === question && rule.when.labels?.includes(label));
  if (rules.some((rule) => rule.outcome === "BLOCK")) return "block";
  if (rules.some((rule) => rule.outcome === "HUMAN_REVIEW")) return "review";
  if (question === policy.primary && policy.labels?.[label]?.allow_auto === false) return "review"; // never automated
  const derive = derivedPrimary();
  const effect = derive?.from.includes(question) ? memberEffect(derive, question, label, answers) : null;
  return effect ? labelTone(state.detail.primary, effect, answers) : "";
}

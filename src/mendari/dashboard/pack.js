// Pack-generic rendering: inputs, criteria tables, providers and what labels lead to.

import { esc, pct, slotText, state } from "./ui.js";

// Where a pack comes from (`origin`, from /api/packs): set by where the deployment found it, never
// by its manifest. A community pack is not clinically reviewed and never automates
// (core/origin.py); every view that shows a pack says so.
export const isCommunity = (pack) => pack?.origin === "community";

export const originBadge = (pack) => (pack?.origin_label
  ? `<span class="badge origin${isCommunity(pack) ? " origin-community" : ""}">${esc(pack.origin_label)}</span>`
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

export function packOptions(packs) {
  const option = (p) => `<option value="${esc(p.id)}">${esc([p.name, p.version].filter(Boolean).join(" "))}</option>`;
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

export function criteriaTable(answers, { evidence = true } = {}) {
  const derive = derivedPrimary();
  if (!derive || !answers) return "";
  const questions = state.detail.manifest.questions;
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

// A provider in words, with the model where one runs: "Rules + Claude Sonnet 5", "Jev 1.13.0",
// "Naive Bayes". A remote part is named by its model, so nothing is said twice; models of other
// variants (a cascade, a vote) follow in brackets.
export function providerName(p) {
  if (!p) return "–";
  const models = remoteModels(p.model).map(modelText);
  let used = 0;
  const words = p.variant.split("_and_").map((part) =>
    (REMOTE_VARIANTS.has(part) && models[used] ? models[used++] : VARIANT_WORDS[part] ?? part.replaceAll("_", " "))).join(" + ");
  return used < models.length ? `${words} (${models.slice(used).join(", ")})` : words;
}

// The remote models behind a provider ("rules:rules@1a2b + llm:anthropic/claude-sonnet-5" ->
// ["anthropic/claude-sonnet-5"]); local fingerprints ("rules@1a2b") are not a deployment choice.
export const remoteModels = (model) => (model ?? "").split(" + ")
  .map((part) => (part.includes(":") ? part.slice(part.indexOf(":") + 1) : part))
  .filter((m) => m && !m.includes("@"));

function modelText(id) {
  const name = id.split("/").pop();
  const claude = name.match(/^claude-([a-z]+)-(\d+(?:\.\d+)*)$/);
  if (claude) return `Claude ${claude[1][0].toUpperCase()}${claude[1].slice(1)} ${claude[2]}`;
  const jev = name.match(/^jev-(.+)$/);
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

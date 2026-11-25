// SPDX-License-Identifier: Apache-2.0
// Try it: a message form or a record form generated from the input schema, or FHIR input where the
// pack maps FHIR; the result.

import { feedbackControls } from "./feedback.js";
import { schemaForm, wireSchemaForm } from "./form.js";
import { codesToggle, isMessagePack, labelName, named, plainReasons, questionName, statusNotice, wireCodes } from "./pack.js";
import { refreshReviewCount, views, wire } from "./router.js";
import { guardrailTrace } from "./trace.js";
import { $, api, copyText, emptyState, esc, focusHeading, icon, may, packPath, pct, PRIORITY_TEXT, state, storageGet, storageSet, toast, toastError, whileBusy } from "./ui.js";

const SAMPLES = [
  { label: "Refill", subject: "Refill request", body: "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks" },
  { label: "Chest pain", subject: "", body: "Having tight chest pressure and I'm sweating a lot right now." },
  { label: "Bill (es)", subject: "", body: "Recibí una factura de $120 y no entiendo por qué.", language: "es", channel: "sms" },
  { label: "Lab result", subject: "Lab results", body: "My A1c came back at 7.8. What does that mean? Do I need to change anything?" },
  { label: "Outside records", subject: "", body: "I had an MRI done at Riverside Imaging. Can you get the images and the report sent over?" },
  { label: "Vague", subject: "", body: "hello?" },
];

views.try = async (main) => {
  if (!may("decide")) {
    main.innerHTML = `<h1>Try it</h1>${emptyState({
      icon: "user", title: "Trying the agent is for reviewers",
      text: "Decisions made here are recorded, so your role can read them but not make them.",
    })}`;
    return;
  }
  return isMessagePack() ? tryMessage(main) : tryRecord(main);
};

// What the chosen pack takes besides its own input, as the API reference offers it
// (server/api_docs.py): FHIR where it has a FHIR mapping.
const takesFhir = () => Boolean(state.detail.manifest?.inputs?.fhir);

// The input switch: each way the pack's input can be given, the one chosen last first.
const MODE_NAMES = { form: "Form", json: "JSON", fhir: "FHIR" };

function rememberedMode(modes) {
  const stored = storageGet("oh.try.mode", "form");
  return modes.includes(stored) ? stored : "form";
}

const modeSwitch = (modes) => `<div class="row try-mode"><span class="try-mode-label" id="input-label">Input</span>
  <div class="segmented" role="group" aria-label="Edit the input as">
    ${modes.map((m) => `<button type="button" data-mode="${m}">${MODE_NAMES[m]}</button>`).join("")}</div></div>`;

const pressMode = (main, mode) =>
  main.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));

// FHIR input: pasted JSON, checked to parse before it is sent; a refusal stays under it.
const fhirBox = () => `<div id="fhir-input" class="sf-field">
    <label for="fhir-json">FHIR R4 JSON</label>
    <span class="sf-hint" id="fhir-hint">A Bundle, one resource, or a CDS Hooks prefetch object: the agent's FHIR mapping turns it into the agent's input. Synthetic data only.</span>
    <textarea id="fhir-json" class="code" spellcheck="false" aria-describedby="fhir-hint fhir-error"></textarea>
    <p class="error" id="fhir-error" role="alert" hidden></p>
  </div>`;

function fhirError(message) {
  const error = $("#fhir-error");
  error.textContent = message;
  error.hidden = !message;
  if (message) $("#fhir-json").setAttribute("aria-invalid", "true");
  else $("#fhir-json").removeAttribute("aria-invalid");
}

const shadowChecked = () => Boolean($("#try-shadow")?.checked);
const shadowHeaders = (shadow) => (shadow ? { "content-type": "application/json", "x-mendari-shadow": "1" } : undefined);

function decideFhir(button) {
  let input;
  try {
    input = JSON.parse($("#fhir-json").value);
  } catch (error) {
    fhirError(`Not valid JSON: ${error.message}`);
    return $("#fhir-json").focus();
  }
  if (!isObject(input)) {
    fhirError("FHIR input is one JSON object: a Bundle, a resource, or a CDS Hooks prefetch object.");
    return $("#fhir-json").focus();
  }
  fhirError("");
  const targetPack = state.packId;
  const endpoint = `${packPath()}/fhir/decide`;
  const shadow = shadowChecked();
  return whileBusy(button, "Deciding…", async () => {
    try {
      const answer = await api(endpoint, { method: "POST", headers: shadowHeaders(shadow), body: JSON.stringify(input) });
      if (state.packId !== targetPack || !$("#result")) return;
      showResult(answer.decision, { task: answer.task, request: { endpoint, body: input, shadow } });
    } catch (error) {
      if (state.packId === targetPack && $("#fhir-error")) fhirError(error.message);
    }
  });
}

// The pack's own input, decided.
function send(button, input) {
  const targetPack = state.packId;
  const endpoint = `${packPath()}/decide`;
  const shadow = shadowChecked();
  return whileBusy(button, "Deciding…", async () => {
    try {
      const decision = await api(endpoint, { method: "POST", headers: shadowHeaders(shadow), body: JSON.stringify(input) });
      if (state.packId !== targetPack || !$("#result")) return;
      showResult(decision, { request: { endpoint, body: input, shadow } });
    } catch (error) {
      toastError(error.message);
    }
  });
}

function wireSubmitKey(element, trigger) {
  element?.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      trigger();
    }
  });
}

function curlCommand({ endpoint, body, shadow = false }) {
  const url = `${location.origin}${endpoint}`;
  const json = JSON.stringify(body).replaceAll("'", "'\\''");
  const extra = shadow ? " \\\n  -H 'X-Mendari-Shadow: 1'" : "";
  return `curl -X POST '${url}' \\\n  -H 'Content-Type: application/json'${extra} \\\n  -d '${json}'`;
}

// Packs whose input is a structured record: edit JSON, starting from the pack's own examples.
async function tryRecord(main) {
  const examples = await api(`${packPath()}/examples`);
  const expected = (e) => Object.entries(e.expect).filter(([k]) => k !== "answers").map(([k, v]) => `${k} ${Array.isArray(v) ? v.join("/") : v}`).join(", ");
  const modes = ["form", "json", ...(takesFhir() ? ["fhir"] : [])];
  main.innerHTML = `
    <h1>Try it</h1>
    <p class="muted">Synthetic records only; decisions here are recorded. Start from one of the agent's tests and change any field.</p>
    ${statusNotice(state.detail)}
    <div class="panel">
      <div class="row mb-3" id="example-row">
        <label for="example">Example</label>
        <select id="example" class="grow">${examples.length ? "" : `<option value="">No behaviour tests</option>`}${examples.map((e, i) =>
          `<option value="${i}">${esc(e.name)}</option>`).join("")}</select>
      </div>
      <p class="muted" id="expected"></p>
      ${modeSwitch(modes)}
      <form id="record-form" class="sf" aria-labelledby="input-label"></form>
      <div id="record-json"><label for="record" class="sr-only">Input (JSON)</label>
        <textarea id="record" class="code" spellcheck="false"></textarea></div>
      ${modes.includes("fhir") ? fhirBox() : ""}
      <div class="row mt-3">
        <label class="shadow-toggle"><input type="checkbox" id="try-shadow"> <span>Shadow mode</span></label>
        <span class="grow"></span>
        <button type="button" class="primary" id="decide">Decide</button></div>
    </div>
    <div id="result"></div>`;
  // One record, two views: a form generated from the pack's input schema (form.js), or its JSON.
  // Switching carries the record across; the choice is remembered. FHIR input is its own.
  const schema = state.detail.input_schema;
  const formBox = $("#record-form");
  let form;
  let mode = rememberedMode(modes);
  let recordView = mode === "fhir" ? "form" : mode; // the record's view while FHIR is shown
  const load = (record) => {
    form = schemaForm(schema, record, "try");
    formBox.innerHTML = form.html;
  };
  const showMode = () => {
    formBox.hidden = mode !== "form";
    $("#record-json").hidden = mode !== "json";
    if ($("#fhir-input")) $("#fhir-input").hidden = mode !== "fhir";
    $("#example-row").hidden = mode === "fhir";
    $("#expected").hidden = mode === "fhir";
    pressMode(main, mode);
  };
  const switchTo = (next) => {
    if (next === mode) return;
    const from = mode === "fhir" ? recordView : mode;
    if (next !== "fhir" && next !== from) {
      if (next === "json") $("#record").value = JSON.stringify(form.value(), null, 2);
      else {
        try {
          load(JSON.parse($("#record").value));
        } catch (error) {
          return toastError(`Not valid JSON: ${error.message}. Fix it to switch back to the form.`);
        }
      }
    }
    mode = next;
    if (next !== "fhir") recordView = next;
    storageSet("oh.try.mode", mode);
    showMode();
  };
  wireSchemaForm(formBox, () => form);
  formBox.addEventListener("submit", (event) => event.preventDefault()); // Enter in a field doesn't decide
  main.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => switchTo(b.dataset.mode)));
  // The picker and the input always show the same example, starting with the first.
  const choose = (index) => {
    const example = examples[index];
    load(example?.input ?? {});
    $("#record").value = JSON.stringify(example?.input ?? {}, null, 2);
    $("#expected").textContent = example && expected(example) ? `The agent's test expects: ${expected(example)}.` : "";
  };
  const replayMeta = state.replayDraft?.packId === state.packId ? state.replayDraft : null;
  const replay = replayMeta?.input ?? null;
  state.replayDraft = null;
  if (replay && isObject(replay)) {
    if (mode === "fhir") mode = recordView;
    load(replay);
    $("#record").value = JSON.stringify(replay, null, 2);
    const orig = replayMeta?.original;
    const origNote = orig?.outcome
      ? ` (original: ${orig.outcome}${orig.recommendation ? ` · ${orig.recommendation}` : ""})`
      : "";
    $("#expected").textContent = `Loaded from the audit trail${origNote}: edit any field and decide to compare.`;
    toast("Loaded case from audit trail");
  } else {
    choose(0);
  }
  showMode();
  $("#example").addEventListener("change", (e) => choose(Number(e.target.value)));
  const run = (button) => {
    if (mode === "fhir") return decideFhir(button);
    let input;
    if (mode === "form") {
      if (!formBox.reportValidity()) return; // the browser points at the field that needs fixing
      input = form.value();
    } else {
      try {
        input = JSON.parse($("#record").value);
      } catch (error) {
        return toastError(`Not valid JSON: ${error.message}`);
      }
    }
    return send(button, input);
  };
  $("#decide").addEventListener("click", (e) => run(e.currentTarget));
  wireSubmitKey($("#record"), () => $("#decide").click());
  wireSubmitKey($("#fhir-json"), () => $("#decide").click());
}

// Whether an object schema takes `key`: it declares it, or it is open (no
// `additionalProperties: false`). A schema not given (none for that object) is open.
const takes = (schema, key) => !schema || Boolean(schema.properties?.[key]) || schema.additionalProperties !== false;

// The message form's fields the pack's input schema takes: a closed schema refuses anything else
// (one may take a message body and subject only), so the form shows and sends
// only these.
function messageFields() {
  const root = state.detail.input_schema ?? {};
  const message = root.properties?.message;
  const patient = root.properties?.patient;
  const hasPatient = takes(root, "patient");
  return {
    subject: takes(message, "subject"),
    channel: takes(message, "channel"),
    language: hasPatient && takes(patient, "preferred_language"),
    age: hasPatient && takes(patient, "age_band"),
  };
}

async function tryMessage(main) {
  const fields = messageFields();
  const modes = ["form", ...(takesFhir() ? ["fhir"] : [])];
  main.innerHTML = `
    <h1>Try it</h1>
    <p class="muted">Synthetic messages only; decisions here are recorded.</p>
    ${statusNotice(state.detail)}
    <div class="panel">
      ${modes.length > 1 ? modeSwitch(modes) : ""}
      <div id="message-form">
      <div class="choices samples mb-3" role="group" aria-label="Sample messages"><span class="muted">Samples</span>
        ${SAMPLES.map((s, i) => `<button type="button" data-sample="${i}">${esc(s.label)}</button>`).join("")}</div>
      ${fields.subject ? `<label for="subject">Subject</label><input id="subject" class="field-full">` : ""}
      <label for="body">Message</label><textarea id="body"></textarea>
      </div>
      ${modes.length > 1 ? fhirBox() : ""}
      <div class="row mt-3">
        <span class="row" id="message-options">
        ${fields.channel ? `<label>Channel <select id="channel"><option>portal</option><option>sms</option><option>email</option></select></label>` : ""}
        ${fields.language ? `<label>Language <select id="language"><option>en</option><option>es</option></select></label>` : ""}
        ${fields.age ? `<label>Age band <select id="age"><option value="">unknown</option><option>0-17</option><option>18-39</option><option>40-64</option><option>65+</option></select></label>` : ""}
        </span>
        <span class="grow"></span>
        <label class="shadow-toggle"><input type="checkbox" id="try-shadow"> <span>Shadow mode</span></label>
        <button class="primary" id="decide">Decide</button>
      </div>
    </div>
    <div id="result"></div>`;
  let mode = rememberedMode(modes);
  const showMode = () => {
    $("#message-form").hidden = mode !== "form";
    $("#message-options").hidden = mode !== "form";
    if ($("#fhir-input")) $("#fhir-input").hidden = mode !== "fhir";
    pressMode(main, mode);
  };
  main.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    storageSet("oh.try.mode", mode);
    showMode();
  }));
  const replayMeta = state.replayDraft?.packId === state.packId ? state.replayDraft : null;
  const replay = replayMeta?.input ?? null;
  state.replayDraft = null;
  if (replay && isObject(replay)) {
    if (mode === "fhir") mode = "form";
    const msg = isObject(replay.message) ? replay.message : {};
    const pat = isObject(replay.patient) ? replay.patient : {};
    if (typeof msg.body === "string") $("#body").value = msg.body;
    if (fields.subject && typeof msg.subject === "string") $("#subject").value = msg.subject;
    if (fields.channel && typeof msg.channel === "string") $("#channel").value = msg.channel;
    if (fields.language && typeof pat.preferred_language === "string") $("#language").value = pat.preferred_language;
    if (fields.age && typeof pat.age_band === "string") $("#age").value = pat.age_band;
    const orig = replayMeta?.original;
    const origNote = orig?.outcome ? ` (was ${orig.outcome}${orig.recommendation ? ` · ${orig.recommendation}` : ""})` : "";
    toast(`Loaded case from audit trail${origNote}`);
  }
  showMode();
  main.querySelectorAll("button[data-sample]").forEach((b) => b.addEventListener("click", () => {
    const s = SAMPLES[Number(b.dataset.sample)];
    $("#body").value = s.body;
    if (fields.subject) $("#subject").value = s.subject;
    if (fields.channel) $("#channel").value = s.channel || "portal";
    if (fields.language) $("#language").value = s.language || "en";
  }));
  $("#decide").addEventListener("click", (e) => {
    if (mode === "fhir") return decideFhir(e.currentTarget);
    const message = { body: $("#body").value };
    if (fields.channel) message.channel = $("#channel").value;
    if (fields.subject && $("#subject").value.trim()) message.subject = $("#subject").value.trim();
    const input = { message };
    if (fields.language || fields.age) {
      const patient = {};
      if (fields.language) patient.preferred_language = $("#language").value;
      if (fields.age && $("#age").value) patient.age_band = $("#age").value;
      input.patient = patient;
    }
    return send(e.currentTarget, input);
  });
  wireSubmitKey($("#body"), () => $("#decide").click());
  wireSubmitKey($("#fhir-json"), () => $("#decide").click());
}

// The outcome in words, with the priority when it isn't routine: "Blocked · urgent".
const RESULT_WORDS = { AUTO: "Automated", HUMAN_REVIEW: "Sent to review", ABSTAIN: "Abstained: a person decides", BLOCK: "Blocked" };

const RESULT_ICON = { AUTO: "check", HUMAN_REVIEW: "clock", ABSTAIN: "clock", BLOCK: "alert" };

const resultHeadline = (r) => `${RESULT_WORDS[r.outcome] ?? r.outcome}${r.priority && r.priority !== "routine" ? ` · ${PRIORITY_TEXT[r.priority].toLowerCase()}` : ""}`;

// Show a new result: scrolled into view, its headline focused (and so announced). `extra`: the
// FHIR Task (`task`) and the request made (`request`).
function showResult(result, extra = {}) {
  const box = $("#result");
  if (!box) return;
  box.innerHTML = decisionView(result, extra);
  wire(box);
  wireCodes(box.querySelector(".result"));
  if (extra.request) {
    box.querySelector("#copy-curl")?.addEventListener("click", () =>
      copyText(curlCommand(extra.request), "cURL copied"));
  }
  focusHeading($("#result-title"));
  refreshReviewCount();
}

// The result in words (LP-05b): names, not codes (the codes one click away), the reasons a person
// reads, and each question's answer. The engine's detail (each label's probability, the
// providers, thresholds and timings, the reasons as the engine gave them) is in the decision
// record, one link away.
function decisionView(r, { task = null, request = null } = {}) {
  const urgent = r.outcome === "BLOCK" || r.priority === "stat";
  const why = plainReasons(r);
  // Every question the pack asks: an answer by name, or that none came (a provider failed).
  const asked = [...new Set([...Object.keys(r.answers), ...Object.keys(r.errors ?? {})])];
  const answers = `<dl class="answers">${asked.map((q) => {
    const a = r.answers[q];
    return `<div>
      <dt>${named(questionName(q), q)}</dt>
      <dd>${a ? `<b>${named(labelName(q, a.label), a.label)}</b>${a.confidence !== null && a.confidence !== undefined ? ` <span class="muted">${pct(a.confidence)} sure</span>` : ""}` : `<span class="muted">no answer</span>`}</dd>
    </div>`;
  }).join("")}</dl>`;
  // The confidence is the primary answer's: said only of the label the engine chose, not of one a
  // rule set instead.
  const chosen = r.recommendation && r.recommendation === r.answers[r.primary]?.label;
  const certainty = !r.recommendation ? "" : chosen && r.confidence !== null ? ` · ${pct(r.confidence)} sure` : r.rule ? " · set by the agent's rule" : "";
  const headIcon = RESULT_ICON[r.outcome];
  return `<section class="panel result" aria-labelledby="result-title">
    <div class="result-head ${urgent ? "tone-solid" : `tone-${esc(r.outcome)}`}">
      <h2 id="result-title">${icon(headIcon)}${esc(resultHeadline(r))}</h2>
      <p>${r.recommendation ? `${named(questionName(r.primary), r.primary)}: <b>${named(labelName(r.primary, r.recommendation), r.recommendation)}</b>` : "No recommendation"}${certainty}</p>
    </div>
    ${guardrailTrace(r)}
    ${r.fallback ? `<div class="callout ${r.outcome === "BLOCK" ? "danger" : "neutral"}"><b>Instead:</b> ${esc(r.fallback)}</div>` : ""}
    ${why.length ? `<h3>Why</h3><ul class="reasons">${why.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${asked.length ? `<h3>Answers</h3>${answers}` : ""}
    <p class="result-more">${codesToggle()} ·
      <a class="action-link" href="#audit/${encodeURIComponent(r.id)}">Engine detail in the decision record <span class="mono">${esc(r.id.slice(0, 8))}</span></a>${request ? ` · <button type="button" class="link-button" id="copy-curl">${icon("copy")} Copy as cURL</button>` : ""}</p>
    ${task ? `<details><summary>The FHIR Task for the work queue</summary><pre class="json" tabindex="0">${esc(JSON.stringify(task, null, 2))}</pre></details>` : ""}
    <h3>Was this right?</h3>${feedbackControls(r)}
  </section>`;
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

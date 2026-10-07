// Try it: a message form or a record form generated from the input schema; the result.

import { feedbackControls } from "./feedback.js";
import { schemaForm, wireSchemaForm } from "./form.js";
import { codesToggle, communityNotice, criteriaTable, derivedPrimary, isMessagePack, labelName, named, plainReasons, questionName, statusNotice, wireCodes } from "./pack.js";
import { refreshReviewCount, views, wire } from "./router.js";
import { $, api, emptyState, esc, focusHeading, icon, may, packPath, pct, PRIORITY_TEXT, state, storageGet, storageSet, toastError, whileBusy } from "./ui.js";

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
      icon: "user", title: "Trying the pack is for reviewers",
      text: "Decisions made here are recorded, so your role can read them but not make them.",
    })}`;
    return;
  }
  return isMessagePack() ? tryMessage(main) : tryRecord(main);
};

// Packs whose input is a structured record: edit JSON, starting from the pack's own examples.
async function tryRecord(main) {
  const examples = await api(`${packPath()}/examples`);
  const expected = (e) => Object.entries(e.expect).filter(([k]) => k !== "answers").map(([k, v]) => `${k} ${Array.isArray(v) ? v.join("/") : v}`).join(", ");
  main.innerHTML = `
    <h1>Try it</h1>
    <p class="muted">Synthetic records only; decisions here are recorded. Start from one of the pack's tests and change any field.</p>
    ${communityNotice(state.detail)}
    ${statusNotice(state.detail)}
    <div class="panel">
      <div class="row mb-3">
        <label for="example">Example</label>
        <select id="example" class="grow">${examples.length ? "" : `<option value="">No behaviour tests</option>`}${examples.map((e, i) =>
          `<option value="${i}">${esc(e.name)}</option>`).join("")}</select>
      </div>
      <p class="muted" id="expected"></p>
      <div class="row try-mode"><span class="try-mode-label" id="input-label">Input</span>
        <div class="segmented" role="group" aria-label="Edit the input as">
          <button type="button" data-mode="form">Form</button><button type="button" data-mode="json">JSON</button></div></div>
      <form id="record-form" class="sf" aria-labelledby="input-label"></form>
      <div id="record-json"><label for="record" class="sr-only">Input (JSON)</label>
        <textarea id="record" class="code" spellcheck="false"></textarea></div>
      <div class="row mt-3"><span class="grow"></span><button type="button" class="primary" id="decide">Decide</button></div>
    </div>
    <div id="result"></div>`;
  // One record, two views: a form generated from the pack's input schema (form.js), or its JSON.
  // Switching carries the record across; the choice is remembered.
  const schema = state.detail.input_schema;
  const formBox = $("#record-form");
  let form;
  let mode = storageGet("oh.try.mode", "form") === "json" ? "json" : "form";
  const load = (record) => {
    form = schemaForm(schema, record, "try");
    formBox.innerHTML = form.html;
  };
  const showMode = () => {
    formBox.hidden = mode !== "form";
    $("#record-json").hidden = mode !== "json";
    main.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  };
  const switchTo = (next) => {
    if (next === mode) return;
    if (next === "json") $("#record").value = JSON.stringify(form.value(), null, 2);
    else {
      try {
        load(JSON.parse($("#record").value));
      } catch (error) {
        return toastError(`Not valid JSON: ${error.message}. Fix it to switch back to the form.`);
      }
    }
    mode = next;
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
    $("#expected").textContent = example && expected(example) ? `The pack's test expects: ${expected(example)}.` : "";
  };
  choose(0);
  showMode();
  $("#example").addEventListener("change", (e) => choose(Number(e.target.value)));
  $("#decide").addEventListener("click", (e) => {
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
    return whileBusy(e.currentTarget, "Deciding…", async () => {
      try {
        showResult(await api(`${packPath()}/decide`, { method: "POST", body: JSON.stringify(input) }));
      } catch (error) {
        toastError(error.message);
      }
    });
  });
}

// Whether an object schema takes `key`: it declares it, or it is open (no
// `additionalProperties: false`). A schema not given (none for that object) is open.
const takes = (schema, key) => !schema || Boolean(schema.properties?.[key]) || schema.additionalProperties !== false;

// The message form's fields the pack's input schema takes: a closed schema refuses anything else
// (the community scaffold's takes a message body and subject only), so the form shows and sends
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
  main.innerHTML = `
    <h1>Try it</h1>
    <p class="muted">Synthetic messages only; decisions here are recorded.</p>
    ${communityNotice(state.detail)}
    ${statusNotice(state.detail)}
    <div class="panel">
      <div class="choices samples mb-3" role="group" aria-label="Sample messages"><span class="muted">Samples</span>
        ${SAMPLES.map((s, i) => `<button type="button" data-sample="${i}">${esc(s.label)}</button>`).join("")}</div>
      ${fields.subject ? `<label for="subject">Subject</label><input id="subject" class="field-full">` : ""}
      <label for="body">Message</label><textarea id="body"></textarea>
      <div class="row mt-3">
        ${fields.channel ? `<label>Channel <select id="channel"><option>portal</option><option>sms</option><option>email</option></select></label>` : ""}
        ${fields.language ? `<label>Language <select id="language"><option>en</option><option>es</option></select></label>` : ""}
        ${fields.age ? `<label>Age band <select id="age"><option value="">unknown</option><option>0-17</option><option>18-39</option><option>40-64</option><option>65+</option></select></label>` : ""}
        <span class="grow"></span>
        <button class="primary" id="decide">Decide</button>
      </div>
    </div>
    <div id="result"></div>`;
  main.querySelectorAll("button[data-sample]").forEach((b) => b.addEventListener("click", () => {
    const s = SAMPLES[Number(b.dataset.sample)];
    $("#body").value = s.body;
    if (fields.subject) $("#subject").value = s.subject;
    if (fields.channel) $("#channel").value = s.channel || "portal";
    if (fields.language) $("#language").value = s.language || "en";
  }));
  $("#decide").addEventListener("click", (e) => {
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
    return whileBusy(e.currentTarget, "Deciding…", async () => {
      try {
        showResult(await api(`${packPath()}/decide`, { method: "POST", body: JSON.stringify(input) }));
      } catch (error) {
        toastError(error.message);
      }
    });
  });
}

// The outcome in words, with the priority when it isn't routine: "Blocked · urgent".
const RESULT_WORDS = { AUTO: "Automated", HUMAN_REVIEW: "Sent to review", ABSTAIN: "Abstained: a person decides", BLOCK: "Blocked" };

const RESULT_ICON = { AUTO: "check", HUMAN_REVIEW: "clock", ABSTAIN: "clock", BLOCK: "alert" };

const resultHeadline = (r) => `${RESULT_WORDS[r.outcome] ?? r.outcome}${r.priority && r.priority !== "routine" ? ` · ${PRIORITY_TEXT[r.priority].toLowerCase()}` : ""}`;

// Show a new result: scrolled into view, its headline focused (and so announced).
function showResult(result) {
  const box = $("#result");
  box.innerHTML = decisionView(result);
  wire(box);
  wireCodes(box.querySelector(".result"));
  focusHeading($("#result-title"));
  refreshReviewCount();
}

// The result in words (LP-05b): names, not codes (the codes one click away), the reasons a person
// reads, and each question's answer. The engine's detail (each label's probability, the
// providers, thresholds and timings, the reasons as the engine gave them) is in the decision
// record, one link away.
function decisionView(r) {
  const urgent = r.outcome === "BLOCK" || r.priority === "stat";
  const why = plainReasons(r);
  // Every question the pack asks: an answer by name, or that none came (a provider failed).
  const asked = [...new Set([...Object.keys(r.answers), ...Object.keys(r.errors ?? {})])];
  const answers = derivedPrimary()
    ? criteriaTable(r.answers, { plain: true })
    : `<dl class="answers">${asked.map((q) => {
      const a = r.answers[q];
      return `<div>
        <dt>${named(questionName(q), q)}</dt>
        <dd>${a ? `<b>${named(labelName(q, a.label), a.label)}</b>${a.confidence !== null && a.confidence !== undefined ? ` <span class="muted">${pct(a.confidence)} sure</span>` : ""}` : `<span class="muted">no answer</span>`}</dd>
      </div>`;
    }).join("")}</dl>`;
  // The confidence is the primary answer's: said only of the label the engine chose, not of one a
  // rule set instead.
  const chosen = r.recommendation && r.recommendation === r.answers[r.primary]?.label;
  const certainty = !r.recommendation ? "" : chosen && r.confidence !== null ? ` · ${pct(r.confidence)} sure` : r.rule ? " · set by the pack's rule" : "";
  return `<section class="panel result" aria-labelledby="result-title">
    <div class="result-head ${urgent ? "tone-solid" : `tone-${esc(r.outcome)}`}">
      <h2 id="result-title">${icon(RESULT_ICON[r.outcome])}${esc(resultHeadline(r))}</h2>
      <p>${r.recommendation ? `${named(questionName(r.primary), r.primary)}: <b>${named(labelName(r.primary, r.recommendation), r.recommendation)}</b>` : "No recommendation"}${certainty}</p>
    </div>
    ${r.fallback ? `<div class="callout ${r.outcome === "BLOCK" ? "danger" : "neutral"}"><b>Instead:</b> ${esc(r.fallback)}</div>` : ""}
    ${why.length ? `<h3>Why</h3><ul class="reasons">${why.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${asked.length ? `<h3>Answers</h3>${answers}` : ""}
    <p class="result-more">${codesToggle()} ·
      <a class="action-link" href="#audit/${encodeURIComponent(r.id)}">Engine detail in the decision record <span class="mono">${esc(r.id.slice(0, 8))}</span></a></p>
    <h3>Was this right?</h3>${feedbackControls(r)}
  </section>`;
}

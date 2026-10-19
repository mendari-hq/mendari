// SPDX-License-Identifier: Apache-2.0
// Audit trail: the decisions table and each record in the drawer; the signed daily checkpoint
// (LP-21k, public) and, for administrators, the pack's trail exported as FHIR NDJSON.

import { closeDrawer, showDrawer } from "./drawer.js";
import { feedbackControls, myRating } from "./feedback.js";
import { answerChart, communityNotice, criteriaTable, derivedPrimary, inputView, labelName, statusNotice } from "./pack.js";
import { route, views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, api, download, esc, fileSlug, icon, may, num, outcomeBadge, OUTCOMES, outcomeText, pct, priorityBadge, saveFile, shortTime, state, storageGet, storageSet, time, toastError, today, whileBusy, zoneShort } from "./ui.js";

const PRIORITY_RANK = { stat: 0, asap: 1, urgent: 2, routine: 3 };

const EVENT_TEXT = {
  "decision.created": "Decision recorded",
  "review.resolved": "Review resolved",
  "outcome.recorded": "Outcome recorded",
  "feedback.recorded": "Feedback recorded",
  "input.read": "Input or notes read (PHI mode)",
  "input.purged": "Input deleted (retention)",
  "input.erased": "Input erased",
  "pas.submitted": "Sent for prior authorization",
  "note.erased": "Note erased",
  "card.feedback": "Card feedback from the EHR",
};

// A note is kept apart from the chain and shown only to those who may read inputs.
const NOTE_STATUS = {
  sealed: "Note (only reviewers and admins can read it)",
  erased: "Note (erased)",
  missing: "Note (missing: verify the chain with --notes)",
};

views.audit = async (main, params) => {
  const outcome = storageGet("oh.audit.outcome", "");
  main.innerHTML = `
    <h1>Audit trail</h1>
    <p class="muted">Every decision and what happened next, in a tamper-evident chain.</p>
    ${communityNotice(state.detail)}
    ${statusNotice(state.detail)}
    <div class="row toolbar">
      <label for="outcome-filter">Outcome</label>
      <select id="outcome-filter"><option value="">All</option>${OUTCOMES.map((o) => `<option value="${o}" ${o === outcome ? "selected" : ""}>${esc(outcomeText(o))}</option>`).join("")}</select>
      <span class="grow"></span>
      ${may("admin") ? `<button type="button" id="export-fhir">${icon("download")} Export as FHIR</button>` : ""}
      ${may("admin") && state.accounts !== "memberships" ? `<button id="verify">Verify chain</button>
      <span id="verify-result"></span>` : ""}
    </div>
    <p class="muted checkpoint" id="checkpoint"></p>
    <div class="panel" id="audit-table"></div>`;
  const load = async () => {
    const filter = $("#outcome-filter").value;
    storageSet("oh.audit.outcome", filter);
    const rows = await api(`/api/audit?pack_id=${encodeURIComponent(state.packId)}&limit=100${filter ? `&outcome=${filter}` : ""}`);
    const table = $("#audit-table");
    // Another filter chosen meanwhile, or the view left: this answer is stale.
    if (!table || $("#outcome-filter")?.value !== filter) return;
    table.innerHTML = rows.length ? auditTable(rows) : `<div class="empty">No decisions recorded.</div>`;
    wire(table);
  };
  // Rows open their record (the first cell's button does the same from the keyboard).
  $("#audit-table").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row) location.hash = `#audit/${encodeURIComponent(row.dataset.id)}`;
  });
  $("#outcome-filter").addEventListener("change", load);
  $("#verify")?.addEventListener("click", async () => {
    const result = await api("/api/audit/verify");
    $("#verify-result").innerHTML = result.ok
      ? `<span class="badge s-ok">verified · ${result.events} events</span>`
      : `<span class="badge s-degraded">broken at event ${result.broken_at}</span>`;
  });
  // Fetched, not linked, so the request carries the window's organization (`download`, ui.js).
  $("#export-fhir")?.addEventListener("click", (event) => whileBusy(event.currentTarget, "Exporting…", () => download(
    `/api/audit/export/fhir?pack_id=${encodeURIComponent(state.packId)}`,
    `audit-${fileSlug(state.packId)}-${today()}.ndjson`,
  )));
  showCheckpoint(); // not waited for: the table does not depend on it
  await load();
  if (params[0]) openRecord(params[0]);
};

// The newest signed daily checkpoint (LP-21k, public): the whole chain's head on a day, signed
// with the deployment's Ed25519 key, which a copy kept elsewhere later checks the chain against
// (`mendari audit verify --against`). Without a key, or before the first signing, one quiet line.
// Without a key (`/api/health` says so) nothing is asked: the route would only say 404.
async function showCheckpoint() {
  let found = null;
  let missing = "not set up on this deployment";
  if (state.checkpointKey) {
    try {
      found = await api("/api/audit/checkpoint");
    } catch (error) {
      missing = error.status === 404 ? "none signed yet" : "could not be read";
    }
  }
  const line = $("#checkpoint");
  if (!line) return; // the view was left meanwhile
  if (!found) {
    line.textContent = `Signed daily checkpoint: ${missing}.`;
    return;
  }
  const s = found.statement;
  line.innerHTML = `${icon("shield-check")} Signed daily checkpoint for <b>${esc(s.date)}</b>: the whole chain up to
    <span class="mono" title="${esc(s.head)}">${esc(s.head.slice(0, 12))}…</span>, signed with key
    <span class="mono" title="public key ${esc(found.public_key)}">${esc(found.key_id)}</span>.
    <button type="button" id="checkpoint-save">${icon("download")} Save a copy</button>`;
  $("#checkpoint-save").addEventListener("click", () => saveFile(
    new Blob([JSON.stringify(found, null, 2)], { type: "application/json" }),
    `checkpoint-${fileSlug(s.date)}.json`,
  ));
}

views.audit.update = (params) => (params[0] ? openRecord(params[0]) : closeDrawer());

// Current ratings of a decision, read-only (rating happens in the record's drawer).
export function ratingCell(r) {
  const parts = [];
  if (r.feedback_up) parts.push(`<span class="rated up">${icon("thumbs-up")}${r.feedback_up > 1 ? r.feedback_up : ""}</span>`);
  if (r.feedback_down) parts.push(`<span class="rated down">${icon("thumbs-down")}${r.feedback_down > 1 ? r.feedback_down : ""}</span>`);
  const words = [r.feedback_up && `${r.feedback_up} right`, r.feedback_down && `${r.feedback_down} wrong`].filter(Boolean).join(", ");
  return parts.length ? `${parts.join(" ")}<span class="sr-only">${words}</span>` : `<span class="muted" aria-label="not rated">–</span>`;
}

function auditTable(rows) {
  const reviewed = (r) => (r.review_status === "resolved" ? `${r.final_label}${r.human_override ? " (override)" : ""}` : r.review_status.replace("_", " "));
  // The reviewer's label, and what a downstream system then saw happen ("nurse · observed physician").
  const review = (r) => (r.observed_label ? `${reviewed(r)} · observed ${r.observed_label}` : reviewed(r));
  return tableHtml({
    id: "audit",
    caption: `<span class="sr-only">Decisions, newest first. Open one to see its record.</span>`,
    rows,
    limitHeight: true,
    stickyFirst: true,
    rowClass: () => "clickable",
    rowAttrs: (r) => `data-id="${esc(r.id)}"`,
    columns: [
      {
        key: "time", label: `Time (${esc(zoneShort())})`, sort: (r) => r.created_at, firstDir: "descending",
        // The name starts with what the button shows (WCAG 2.5.3, label in name), then says more.
        cell: (r) => `<button type="button" class="linklike open-record" data-id="${esc(r.id)}"><time datetime="${esc(r.created_at)}" title="${esc(time(r.created_at))}">${esc(shortTime(r.created_at))}</time><span class="sr-only">: open decision ${esc(r.id.slice(0, 8))}, ${esc(time(r.created_at))}</span></button>`,
      },
      { key: "outcome", label: "Outcome", sort: (r) => OUTCOMES.indexOf(r.outcome), cell: (r) => outcomeBadge(r.outcome) },
      { key: "recommendation", label: "Recommendation", sort: (r) => r.recommendation ?? "", cell: (r) => esc(r.recommendation ?? "–") },
      { key: "priority", label: "Priority", sort: (r) => PRIORITY_RANK[r.priority] ?? 9, firstDir: "ascending", cell: (r) => priorityBadge(r.priority) },
      { key: "confidence", label: "Confidence", numeric: true, sort: (r) => r.confidence, cell: (r) => pct(r.confidence) },
      { key: "latency", label: "Latency, ms", numeric: true, sort: (r) => r.latency_ms, cell: (r) => num(r.latency_ms, 2) },
      { key: "review", label: "Review", sort: review, cell: (r) => esc(review(r)) },
      { key: "rated", label: "Rated", className: "rated-cell", sort: (r) => r.feedback_down + r.feedback_up / 1000, cell: ratingCell },
      { key: "id", label: "ID", cell: (r) => `<span class="mono">${esc(r.id.slice(0, 8))}</span>` },
    ],
  });
}

let recordAsked = 0; // the latest record asked for: an older answer, arriving after it, is dropped

async function openRecord(id) {
  const opener = document.querySelector(`button.open-record[data-id="${CSS.escape(id)}"]`) ?? document.activeElement;
  const asked = ++recordAsked;
  let found;
  try {
    found = await api(`/api/audit/${encodeURIComponent(id)}`);
  } catch (error) {
    if (asked !== recordAsked) return;
    toastError(error.message);
    location.hash = "#audit";
    return;
  }
  // A record opened after this one was asked for, or the drawer closed meanwhile: the address
  // names what is shown, so a slow answer never replaces a newer drawer.
  if (asked !== recordAsked || route().params[0] !== id) return;
  const r = found.record;
  const shown = showDrawer({
    // What happened, in words ("Human review · nurse"); the id is in the body.
    title: `${outcomeText(r.outcome)}${r.recommendation && r.recommendation !== r.outcome.toLowerCase() ? ` · ${labelName(r.primary ?? state.detail.primary, r.recommendation)}` : ""}`,
    opener,
    // Closing the drawer (button, Escape, backdrop) goes back to the list's URL.
    onClose: () => {
      if (route().params.length) location.hash = "#audit";
    },
    body: `
      <div class="row">${outcomeBadge(r.outcome)} ${priorityBadge(r.priority)}
        <span>→ <b>${r.recommendation ? `${esc(labelName(r.primary ?? state.detail.primary, r.recommendation))}</b> <span class="mono muted">${esc(r.recommendation)}</span>` : "no recommendation</b>"}</span>
        ${r.confidence !== null && r.confidence !== undefined ? `<span class="muted">confidence ${pct(r.confidence)}</span>` : ""}</div>
      <p class="muted mt-2"><time datetime="${esc(r.created_at)}">${esc(time(r.created_at))}</time> · <span class="mono break-anywhere">${esc(id)}</span></p>
      ${inputView(r.input, { restricted: found.input_restricted })}
      ${r.fallback ? `<p class="mt-3"><b>Fallback:</b> ${esc(r.fallback)}</p>` : ""}
      ${r.reasons?.length ? `<ul class="reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${derivedPrimary() ? `<h3>Criteria</h3>${criteriaTable(r.answers)}` : ""}
      ${r.data_coverage ? coveragePanel(id, r.data_coverage) : ""}
      <h3>Engine detail</h3>
      <p class="muted">Each answer's probabilities and provider, as the engine gave them${r.latency_ms !== undefined ? `; ${num(r.latency_ms, 2)} ms` : ""}.</p>
      ${Object.entries(r.answers ?? {}).map(([q, a]) => answerChart(id, q, a, r.answers)).join("")}
      ${may("review") ? `<h3>Was this right?</h3>${feedbackControls(r, myRating(found.events))}` : ""}
      <h3>Events</h3>${timeline(found.events)}
      <details><summary>Full record (JSON)</summary><pre class="json">${esc(JSON.stringify(r, null, 2))}</pre></details>`,
  });
  wire(shown.querySelector(".drawer-body"));  // this drawer's charts, not one still closing
}

// What a FHIR decision looked for in the record and found (its data-coverage record, LP-23d):
// per mapped field and list, the resources read and the values found, as counts, and what could
// not be read, by reason. Counts and the pack's own names only, never a value; never a share.
const readReasons = (counts) => {
  const parts = Object.entries(counts ?? {}).filter(([, n]) => n > 0).map(([reason, n]) => `${esc(reason)} ${num(n, 0)}`);
  return parts.length ? parts.join(", ") : `<span class="muted">none</span>`;
};
const readFrom = (resources) => Object.entries(resources).map(([kind, n]) => `${esc(kind)} ${num(n, 0)}`).join(", ");
const codings = (found) => {
  const { other = 0, ...declared } = found ?? {};
  const inDeclared = Object.values(declared).reduce((sum, n) => sum + n, 0);
  if (!inDeclared && !other) return `<span class="muted">–</span>`;
  return `${num(inDeclared, 0)} in declared systems, ${num(other, 0)} other`;
};

function coveragePanel(id, coverage) {
  const rows = [
    { name: `Focus: ${esc(coverage.focus.resource)}`, from: readFrom({ [coverage.focus.resource]: coverage.focus.resources }), found: null, codings: null, unreadable: coverage.focus.unreadable },
    ...Object.entries(coverage.fields ?? {}).map(([name, f]) => ({ name: `<span class="mono">${esc(name)}</span>`, from: readFrom(f.resources ?? {}), found: f.values, codings: f.codings, unreadable: f.unreadable })),
    ...Object.entries(coverage.lists ?? {}).map(([name, l]) => ({ name: `<span class="mono">${esc(name)}</span> (list)`, from: readFrom({ [l.resource]: l.resources }), found: l.items, codings: null, unreadable: l.unreadable })),
  ];
  const total = readReasons(coverage.unreadable);
  return `<section class="coverage" aria-labelledby="coverage-${esc(id)}">
    <h3 id="coverage-${esc(id)}">What it read</h3>
    <p class="muted">From the FHIR record, for each field and list the pack maps: counts only, never a value. Could not read, in all: ${total}.</p>
    ${tableHtml({
      id: `coverage-table-${id}`,
      caption: `<span class="sr-only">What the decision read from the FHIR record, by field</span>`,
      rows,
      columns: [
        { key: "name", label: "Field", cell: (row) => row.name },
        { key: "from", label: "Resources read", cell: (row) => row.from },
        { key: "found", label: "Values found", numeric: true, cell: (row) => (row.found === null ? `<span class="muted">–</span>` : num(row.found, 0)) },
        { key: "codings", label: "Codings", cell: (row) => (row.codings === null ? `<span class="muted">–</span>` : codings(row.codings)) },
        { key: "unreadable", label: "Could not read", cell: (row) => readReasons(row.unreadable) },
      ],
    })}
  </section>`;
}

// An event's note, when it has one: its text for those who may read it, else what became of it.
function noteLine(e) {
  if (!e.note_status) return "";
  if (e.note_status === "open") return `<p class="event-note"><b>Note:</b> ${esc(e.note)}</p>`;
  return `<p class="event-note muted">${esc(NOTE_STATUS[e.note_status] ?? "Note")}</p>`;
}

// The hash chain as an ordered list: what happened, when, and each event's hash.
function timeline(events) {
  return `<ol class="timeline">${events.map((e) => `<li>
    <div class="row"><b>${esc(EVENT_TEXT[e.event] ?? e.event)}</b>
      <span class="muted"><time datetime="${esc(e.created_at)}">${esc(time(e.created_at))}</time></span>
      <span class="grow"></span><span class="mono muted" title="hash ${esc(e.hash)}">#${e.seq} · ${esc(e.hash.slice(0, 12))}…</span></div>
    ${noteLine(e)}
    <details><summary>Payload</summary><pre class="json">${esc(JSON.stringify(e.payload, null, 2))}</pre></details>
  </li>`).join("")}</ol>`;
}

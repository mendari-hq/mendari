// Audit trail: the decisions table and each record in the drawer.

import { closeDrawer, showDrawer } from "./drawer.js";
import { feedbackControls, myRating } from "./feedback.js";
import { criteriaTable, derivedPrimary, inputView } from "./pack.js";
import { route, views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, api, esc, icon, may, num, outcomeBadge, OUTCOMES, outcomeText, pct, priorityBadge, shortTime, state, storageGet, storageSet, time, toastError, zoneShort } from "./ui.js";

const PRIORITY_RANK = { stat: 0, asap: 1, urgent: 2, routine: 3 };

const EVENT_TEXT = {
  "decision.created": "Decision recorded",
  "review.resolved": "Review resolved",
  "outcome.recorded": "Outcome recorded",
  "feedback.recorded": "Feedback recorded",
  "input.read": "Input read (PHI mode)",
  "input.purged": "Input deleted (retention)",
  "pas.submitted": "Sent for prior authorization",
};

views.audit = async (main, params) => {
  const outcome = storageGet("oh.audit.outcome", "");
  main.innerHTML = `
    <h1>Audit trail</h1>
    <p class="muted">Every decision and what happened next, in a tamper-evident chain.</p>
    <div class="row toolbar">
      <label for="outcome-filter">Outcome</label>
      <select id="outcome-filter"><option value="">All</option>${OUTCOMES.map((o) => `<option value="${o}" ${o === outcome ? "selected" : ""}>${esc(outcomeText(o))}</option>`).join("")}</select>
      <span class="grow"></span>
      <button id="verify">Verify chain</button>
      <span id="verify-result"></span>
    </div>
    <div class="panel" id="audit-table"></div>`;
  const load = async () => {
    const filter = $("#outcome-filter").value;
    storageSet("oh.audit.outcome", filter);
    const rows = await api(`/api/audit?pack_id=${encodeURIComponent(state.packId)}&limit=100${filter ? `&outcome=${filter}` : ""}`);
    $("#audit-table").innerHTML = rows.length ? auditTable(rows) : `<div class="empty">No decisions recorded.</div>`;
    wire($("#audit-table"));
  };
  // Rows open their record (the first cell's button does the same from the keyboard).
  $("#audit-table").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row) location.hash = `#audit/${encodeURIComponent(row.dataset.id)}`;
  });
  $("#outcome-filter").addEventListener("change", load);
  $("#verify").addEventListener("click", async () => {
    const result = await api("/api/audit/verify");
    $("#verify-result").innerHTML = result.ok
      ? `<span class="badge s-ok">verified · ${result.events} events</span>`
      : `<span class="badge s-degraded">broken at event ${result.broken_at}</span>`;
  });
  await load();
  if (params[0]) openRecord(params[0]);
};

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
  const review = (r) => (r.review_status === "resolved" ? `${r.final_label}${r.human_override ? " (override)" : ""}` : r.review_status.replace("_", " "));
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
        cell: (r) => `<button type="button" class="linklike open-record" data-id="${esc(r.id)}" aria-label="Open decision ${esc(r.id.slice(0, 8))}, ${esc(time(r.created_at))}"><time datetime="${esc(r.created_at)}" title="${esc(time(r.created_at))}">${esc(shortTime(r.created_at))}</time></button>`,
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

async function openRecord(id) {
  const opener = document.querySelector(`button.open-record[data-id="${CSS.escape(id)}"]`) ?? document.activeElement;
  let found;
  try {
    found = await api(`/api/audit/${encodeURIComponent(id)}`);
  } catch (error) {
    toastError(error.message);
    location.hash = "#audit";
    return;
  }
  const r = found.record;
  showDrawer({
    // What happened, in words ("Human review · nurse"); the id is in the body.
    title: `${outcomeText(r.outcome)}${r.recommendation && r.recommendation !== r.outcome.toLowerCase() ? ` · ${r.recommendation}` : ""}`,
    opener,
    // Closing the drawer (button, Escape, backdrop) goes back to the list's URL.
    onClose: () => {
      if (route().params.length) location.hash = "#audit";
    },
    body: `
      <div class="row">${outcomeBadge(r.outcome)} ${priorityBadge(r.priority)}
        <span>→ <b>${esc(r.recommendation ?? "no recommendation")}</b></span>
        ${r.confidence !== null && r.confidence !== undefined ? `<span class="muted">confidence ${pct(r.confidence)}</span>` : ""}</div>
      <p class="muted mt-2"><time datetime="${esc(r.created_at)}">${esc(time(r.created_at))}</time> · <span class="mono break-anywhere">${esc(id)}</span></p>
      ${inputView(r.input, { restricted: found.input_restricted })}
      ${r.fallback ? `<p class="mt-3"><b>Fallback:</b> ${esc(r.fallback)}</p>` : ""}
      ${r.reasons?.length ? `<ul class="reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${derivedPrimary() ? `<h3>Criteria</h3>${criteriaTable(r.answers)}` : ""}
      ${may("review") ? `<h3>Was this right?</h3>${feedbackControls(r, myRating(found.events))}` : ""}
      <h3>Events</h3>${timeline(found.events)}
      <details><summary>Full record (JSON)</summary><pre class="json">${esc(JSON.stringify(r, null, 2))}</pre></details>`,
  });
}

// The hash chain as an ordered list: what happened, when, and each event's hash.
function timeline(events) {
  return `<ol class="timeline">${events.map((e) => `<li>
    <div class="row"><b>${esc(EVENT_TEXT[e.event] ?? e.event)}</b>
      <span class="muted"><time datetime="${esc(e.created_at)}">${esc(time(e.created_at))}</time></span>
      <span class="grow"></span><span class="mono muted" title="hash ${esc(e.hash)}">#${e.seq} · ${esc(e.hash.slice(0, 12))}…</span></div>
    <details><summary>Payload</summary><pre class="json">${esc(JSON.stringify(e.payload, null, 2))}</pre></details>
  </li>`).join("")}</ol>`;
}

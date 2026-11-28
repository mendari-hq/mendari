// SPDX-License-Identifier: Apache-2.0
// Audit trail: the decisions table and each record in the drawer; the signed daily checkpoint
// (LP-21k, public) and, for administrators, the agent's trail exported as FHIR NDJSON.

import { closeDrawer, showDrawer, updateDrawer } from "./drawer.js";
import { feedbackControls, myRating } from "./feedback.js";
import { actionName, actionReason, actionsList, answerChart, dispositionBadge, effectBadge, inputView, labelName, statusNotice, textName, textsList, textStateBadge } from "./manifest.js";
import { route, views, wire } from "./router.js";
import { drawerSkeleton } from "./skeleton.js";
import { tableHtml } from "./table.js";
import { guardrailTrace } from "./trace.js";
import { $, api, copyText, download, esc, fileSlug, icon, legible, may, num, outcomeBadge, OUTCOMES, outcomeText, pct, PRIORITY_TEXT, priorityBadge, saveFile, shortTime, state, storageGet, storageSet, time, toastError, today, whileBusy, WHOLE_DECISION_ID, zoneShort } from "./ui.js";

const PRIORITY_RANK = { stat: 0, asap: 1, urgent: 2, routine: 3 };

const EVENT_TEXT = {
  "decision.created": "Decision recorded",
  "review.resolved": "Review resolved",
  "outcome.recorded": "Outcome recorded",
  "feedback.recorded": "Feedback recorded",
  "input.read": "Input or notes read (PHI mode)",
  "input.purged": "Input deleted (retention)",
  "input.erased": "Input erased",
  "note.erased": "Note erased",
  "card.feedback": "Card feedback from the EHR",
  "action.proposed": "Action proposed",
  "action.resolved": "Action released or denied by review",
  "output.held": "Text held for review",
  "output.resolved": "Text released or withheld by review",
};

// A note is kept apart from the chain and shown only to those who may read inputs.
const NOTE_STATUS = {
  sealed: "Note (only reviewers and admins can read it)",
  erased: "Note (erased)",
  missing: "Note (missing: verify the chain with --notes)",
};

// The trail is listed a page at a time, newest first (the route pages by `limit` and `offset`).
// The filters search the decisions listed, and the count says how much of the trail that is.
const PAGE = 100;


let auditRows = [];
let visibleIds = [];

views.audit = async (main, params) => {
  const outcome = storageGet("oh.audit.outcome", "");
  const agentId = state.agentId;
  auditRows = [];
  visibleIds = [];
  // How many decisions the trail holds, by outcome, to say how much is listed (null when that
  // could not be read); the rows the route has given (the next page's offset); and whether the
  // last page was full, so that older decisions may be left.
  const counts = api(`/api/stats?agent_id=${encodeURIComponent(agentId)}`).catch(() => null);
  let total = null;
  let given = 0;
  let full = false;
  let loads = 0; // listings started: an older page asked for under an earlier one is dropped
  main.innerHTML = `
    <h1>Audit trail</h1>
    <p class="muted">Every decision and what happened next, in a tamper-evident chain.</p>
    ${statusNotice(state.detail)}
    <div class="row toolbar">
      <label for="outcome-filter">Outcome</label>
      <select id="outcome-filter"><option value="">All</option>${OUTCOMES.map((o) => `<option value="${o}" ${o === outcome ? "selected" : ""}>${esc(outcomeText(o))}</option>`).join("")}</select>
      <label for="priority-filter">Priority</label>
      <select id="priority-filter"><option value="">All</option>${Object.entries(PRIORITY_TEXT).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
      <label for="audit-search" class="sr-only">Filter decisions</label>
      <input id="audit-search" type="search" class="audit-search" placeholder="Search ID, label, reviewer…" autocomplete="off">
      <button type="button" id="audit-reset" hidden>Reset</button>
      <span id="audit-count" class="muted audit-count" aria-live="polite"></span>
      <span class="grow"></span>
      ${may("admin") ? `<button type="button" id="export-fhir">${icon("download")} Export as FHIR</button>` : ""}
      ${may("operate") ? `<button type="button" id="verify">Verify chain</button>
      <span id="verify-result"></span>` : ""}
    </div>
    <p class="muted checkpoint" id="checkpoint"></p>
    <div class="panel" id="audit-table"></div>
    <div class="row mt-3" id="audit-more" hidden><button type="button" id="audit-older">Show older decisions</button></div>`;

  // Older decisions are left to list; what is listed, when not all: "the newest 100 of 250 decisions".
  const more = () => full && (total === null || auditRows.length < total);
  const listedText = () => `the newest ${auditRows.length}${total === null ? "" : ` of ${total}`} decisions`;

  const paintTable = () => {
    const table = $("#audit-table");
    if (!table) return;
    const priority = $("#priority-filter")?.value ?? "";
    const query = ($("#audit-search")?.value ?? "").trim().toLowerCase();
    const filtered = auditRows.filter((r) => {
      if (priority && r.priority !== priority) return false;
      if (!query) return true;
      const hay = `${r.id} ${r.recommendation ?? ""} ${r.final_label ?? ""} ${r.observed_label ?? ""} ${r.reviewed_by ?? ""} ${r.reviewer ?? ""} ${r.review_status ?? ""} ${r.priority ?? ""} ${r.outcome ?? ""}`.toLowerCase();
      return hay.includes(query);
    });
    visibleIds = filtered.map((r) => r.id);
    const active = Boolean(priority || query);
    const partial = more();
    const reset = $("#audit-reset");
    if (reset) reset.hidden = !active;
    const count = $("#audit-count");
    if (count) {
      const listed = partial ? listedText() : auditRows.length;
      count.textContent = active ? `Showing ${filtered.length} of ${listed}` : partial ? `Showing ${listed}` : "";
    }
    const none = !auditRows.length ? "No decisions recorded."
      : partial ? `No decisions among the newest ${auditRows.length} match the current filter.`
      : "No decisions match the current filter.";
    table.innerHTML = filtered.length ? auditTable(filtered) : `<div class="empty">${none}</div>`;
    wire(table);
    $("#audit-more").hidden = !partial;
  };

  const listing = (filter, offset) => api(`/api/audit?agent_id=${encodeURIComponent(agentId)}&limit=${PAGE}${offset ? `&offset=${offset}` : ""}${filter ? `&outcome=${filter}` : ""}`);
  // Another agent or filter chosen meanwhile, or this view left (or shown anew): an answer is stale.
  const shownTable = $("#audit-table");
  const stale = (filter) => !shownTable.isConnected || state.agentId !== agentId || $("#outcome-filter")?.value !== filter;

  const load = async () => {
    const filter = $("#outcome-filter").value;
    const asked = ++loads;
    storageSet("oh.audit.outcome", filter);
    try {
      const [rows, stats] = await Promise.all([listing(filter, 0), counts]);
      if (asked !== loads || stale(filter)) return;
      auditRows = rows;
      given = rows.length;
      full = rows.length === PAGE;
      total = (filter ? stats?.outcomes?.[filter] : stats?.decisions) ?? null;
      paintTable();
    } catch (error) {
      if (state.agentId === agentId) toastError(error.message);
    }
  };

  // The next page, after what is listed. Decisions made meanwhile move the pages down, so one
  // already listed can come again: it is listed once.
  const loadOlder = async () => {
    const filter = $("#outcome-filter").value;
    const asked = loads;
    try {
      const rows = await listing(filter, given);
      if (asked !== loads || stale(filter)) return;
      const listed = new Set(auditRows.map((r) => r.id));
      const added = rows.filter((r) => !listed.has(r.id));
      auditRows = [...auditRows, ...added];
      given += rows.length;
      full = rows.length === PAGE;
      paintTable();
      // The keyboard goes on from the first older decision the filters show.
      const first = added.find((r) => visibleIds.includes(r.id));
      if (first) $(`#audit-table button.open-record[data-id="${CSS.escape(first.id)}"]`)?.focus();
    } catch (error) {
      if (state.agentId === agentId) toastError(error.message);
    }
  };
  // Rows open their record (the first cell's button does the same from the keyboard).
  $("#audit-table").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row) location.hash = `#audit/${encodeURIComponent(row.dataset.id)}`;
  });
  $("#outcome-filter").addEventListener("change", load);
  $("#priority-filter").addEventListener("change", paintTable);
  $("#audit-search").addEventListener("input", (event) => {
    const query = event.currentTarget.value.trim();
    if (WHOLE_DECISION_ID.test(query)) {
      event.currentTarget.value = ""; // it found its decision: the list is not filtered by it
      location.hash = `#audit/${encodeURIComponent(query.toLowerCase())}`;
    }
    paintTable();
  });
  $("#audit-older").addEventListener("click", (event) => whileBusy(event.currentTarget, "Loading…", loadOlder));
  $("#audit-reset").addEventListener("click", () => {
    $("#priority-filter").value = "";
    $("#audit-search").value = "";
    paintTable();
    $("#audit-search").focus();
  });
  $("#verify")?.addEventListener("click", (event) => whileBusy(event.currentTarget, "Verifying…", async () => {
    try {
      const result = await api("/api/audit/verify");
      const out = $("#verify-result");
      if (!out?.isConnected) return;
      out.innerHTML = result.ok
        ? `<span class="badge s-ok">verified · ${esc(result.events)} events</span>`
        : `<span class="badge s-degraded">broken at event ${esc(result.broken_at)}</span>`;
    } catch (error) {
      toastError(error.message);
    }
  }));
  // Fetched, not linked, so the request carries the window's organization (`download`, ui.js).
  $("#export-fhir")?.addEventListener("click", (event) => whileBusy(event.currentTarget, "Exporting…", () => download(
    `/api/audit/export/fhir?agent_id=${encodeURIComponent(state.agentId)}`,
    `audit-${fileSlug(state.agentId)}-${today()}.ndjson`,
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
  const onClose = () => {
    if (route().params.length) location.hash = "#audit";
  };
  // Show a drawer skeleton at once so clicking a row feels instantaneous.
  showDrawer({ title: `Decision ${id.slice(0, 8)}`, opener, onClose, body: drawerSkeleton(), busy: true });
  let found;
  try {
    found = await api(`/api/audit/${encodeURIComponent(id)}`);
  } catch (error) {
    if (asked !== recordAsked || route().view !== "audit" || route().params[0] !== id) return;
    closeDrawer({ restoreFocus: true, closedByUser: false });
    toastError(error.message);
    location.hash = "#audit";
    return;
  }
  // A record opened after this one was asked for, or the drawer closed meanwhile: the address
  // names what is shown, so a slow answer never replaces a newer drawer.
  if (asked !== recordAsked || route().params[0] !== id) return;
  const r = found.record;
  // The agent that made it: its row's (`agent_id`), as the record names it (`agent.id`). Opened
  // by its id alone (the palette, a bare `#audit/<id>`), it can be another agent's than the one
  // chosen: its codes are then shown as they are, never in the chosen agent's words, and it is
  // not replayed into the chosen agent's Try it.
  const recordAgent = found.agent_id ?? r.agent?.id;
  const sameAgent = recordAgent === state.agentId;
  const recLabel = (code) => (sameAgent ? labelName(r.primary ?? state.detail.primary, code) : code);
  const at = visibleIds.indexOf(id);
  const prevId = at > 0 ? visibleIds[at - 1] : null;
  const nextId = at >= 0 && at < visibleIds.length - 1 ? visibleIds[at + 1] : null;
  const canReplay = sameAgent && may("decide") && !found.input_restricted && r.input !== null && r.input !== undefined;
  const headActions = `
    ${visibleIds.length > 1 && at >= 0 ? `<button type="button" class="icon-button" data-step="${esc(prevId ?? "")}" aria-label="Previous decision ([)" title="Previous decision ([)"${prevId ? "" : " disabled"}>${icon("chevron-left")}</button>
      <button type="button" class="icon-button" data-step="${esc(nextId ?? "")}" aria-label="Next decision (])" title="Next decision (])"${nextId ? "" : " disabled"}>${icon("chevron-right")}</button>` : ""}
    ${canReplay ? `<button type="button" class="icon-button" data-replay aria-label="Replay in Try it" title="Replay this input in Try it">${icon("rotate-ccw")}</button>` : ""}
    <button type="button" class="icon-button" data-copy-id aria-label="Copy decision ID" title="Copy decision ID">${icon("copy")}</button>`;
  const shown = updateDrawer({
    // What happened, in words ("Human review · nurse"); the id is in the body.
    title: `${outcomeText(r.outcome)}${r.recommendation && r.recommendation !== r.outcome.toLowerCase() ? ` · ${recLabel(r.recommendation)}` : ""}`,
    headActions,
    onKeydown: (event) => {
      if (event.target?.closest?.("input, textarea, select")) return;
      if (event.key === "[" && prevId) {
        event.preventDefault();
        location.hash = `#audit/${encodeURIComponent(prevId)}`;
      } else if (event.key === "]" && nextId) {
        event.preventDefault();
        location.hash = `#audit/${encodeURIComponent(nextId)}`;
      }
    },
    body: `
      <div class="row">${outcomeBadge(r.outcome)} ${priorityBadge(r.priority)}
        <span>→ <b>${r.recommendation ? `${esc(recLabel(r.recommendation))}</b> <span class="mono muted">${esc(r.recommendation)}</span>` : "no recommendation</b>"}</span>
        ${r.confidence !== null && r.confidence !== undefined ? `<span class="muted">confidence ${pct(r.confidence)}</span>` : ""}</div>
      <p class="muted mt-2"><time datetime="${esc(r.created_at)}">${esc(time(r.created_at))}</time> · <span class="mono break-anywhere">${esc(id)}</span></p>
      ${sameAgent ? "" : recordAgentNote(recordAgent)}
      ${guardrailTrace(r)}
      ${inputView(r.input, { restricted: found.input_restricted })}
      ${r.fallback ? `<p class="mt-3"><b>Fallback:</b> ${esc(r.fallback)}</p>` : ""}
      ${r.reasons?.length ? `<ul class="reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${r.data_coverage ? coveragePanel(id, r.data_coverage, r.ehr_declaration) : ""}
      <h3>Engine detail</h3>
      <p class="muted">Each answer's probabilities and provider, as the engine gave them${r.latency_ms !== undefined ? `; ${num(r.latency_ms, 2)} ms` : ""}.</p>
      ${Object.entries(r.answers ?? {}).map(([q, a]) => answerChart(id, q, a, { own: sameAgent })).join("")}
      ${may("review") ? `<h3>Was this right?</h3>${feedbackControls(r, myRating(found.events))}` : ""}
      ${writtenSection(found, sameAgent)}
      ${found.actions?.length ? `<h3>Actions</h3>
        <p class="muted">What the agent asked to have done. Mendari authorizes each and runs none.</p>
        ${actionsList(found.actions)}` : ""}
      <h3>Events</h3>${timeline(found.events)}
      ${eraseLink(id)}
      <details><summary>Full record (JSON)</summary><pre class="json">${esc(JSON.stringify(r, null, 2))}</pre></details>`,
  });
  if (!shown) return;
  shown.querySelector("[data-copy-id]")?.addEventListener("click", () => copyText(id, "Decision ID copied"));
  shown.querySelector("[data-replay]")?.addEventListener("click", () => {
    state.replayDraft = {
      agentId: state.agentId,
      input: r.input,
      original: { id: r.id, outcome: r.outcome, recommendation: r.recommendation, primary: r.primary },
    };
    closeDrawer({ restoreFocus: false, closedByUser: false });
    location.hash = "#try";
  });
  shown.querySelectorAll("[data-step]").forEach((btn) => btn.addEventListener("click", () => {
    if (btn.dataset.step) location.hash = `#audit/${encodeURIComponent(btn.dataset.step)}`;
  }));
  // Chosen as the picker chooses it: the address keeps `#audit/<id>`, so the record opens again
  // over that agent's trail.
  shown.querySelector("[data-record-agent]")?.addEventListener("click", (event) => {
    const select = $("#agent-select");
    select.value = event.currentTarget.dataset.recordAgent;
    select.dispatchEvent(new Event("change"));
  });
  wire(shown.querySelector(".drawer-body"));  // this drawer's charts, not one still closing
}

// Whose decision it is, when not the chosen agent's; where that agent is served here, a way to
// show the decision with it (its words, its Try it).
function recordAgentNote(agentId) {
  const served = state.agents.find((a) => a.id === agentId);
  return `<p class="callout neutral record-agent">Made by another agent, <b>${esc(served?.name ?? agentId)}</b>
    <span class="mono">${esc(agentId)}</span>: its labels show as codes here.
    ${served ? `<button type="button" data-record-agent="${esc(agentId)}">Show it with ${esc(served.name)}</button>` : ""}</p>`;
}

// Where what a decision keeps of its input is erased, on a patient's request or for a case
// recorded by mistake: the System view's form (system.js), with this decision's id in it. For
// those who may erase and have that view, where the deployment keeps inputs at all.
const eraseLink = (id) => (may("admin") && state.phiMode === "encrypt" && views.system?.available?.()
  ? `<p class="mt-3"><a href="#system/erase/${encodeURIComponent(id)}" data-erase>Erase what this decision keeps of its input…</a></p>`
  : "");

// What a FHIR decision looked for in the record and found (its data-coverage record, LP-23d):
// per mapped field and list, the resources read and the values found, as counts, and what could
// not be read, by reason. A list of several resource types has a row per source, and a list
// item's key that could not read something a row of its own. Under the table: the prefetch keys
// that could not be read (with the HTTP status, a number) or that the EHR does not offer, the
// linked reads, and the declaration of what the EHR does not offer, when one was read under.
// Counts and the agent's own names only, never a value; never a share.
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
// What a field could not read, and the formats of the notes it could not read, when it says.
const notRead = (record) => {
  const formats = Object.entries(record.unread_formats ?? {}).map(([label, n]) => `${esc(label)} ${num(n, 0)}`);
  return `${readReasons(record.unreadable)}${formats.length ? ` <span class="muted">(formats: ${formats.join(", ")})</span>` : ""}`;
};
const NOT_OFFERED = `<span class="muted">not offered (declared)</span>`;

function listRows(name, list) {
  if (!list.sources) {
    return [{ name: `<span class="mono">${esc(name)}</span> (list)`, from: readFrom({ [list.resource]: list.resources }), found: list.items, codings: null, unreadable: notRead(list) }];
  }
  const rows = [{ name: `<span class="mono">${esc(name)}</span> (list)`, from: null, found: list.items, codings: null, unreadable: notRead(list) }];
  for (const [kind, source] of Object.entries(list.sources)) {
    const from = `<span class="mono">${esc(name)}</span> ← ${esc(kind)}`;
    rows.push({ name: from, from: source.not_offered ? NOT_OFFERED : readFrom({ [kind]: source.resources }), found: source.items, codings: null, unreadable: notRead(source) });
    for (const [key, field] of Object.entries(source.fields ?? {})) {
      if (!field.unreadable) continue;
      rows.push({ name: `<span class="mono">${esc(name)}.${esc(key)}</span> ← ${esc(kind)}`, from: null, found: field.values, codings: field.codings, unreadable: notRead(field) });
    }
  }
  return rows;
}

function prefetchLine(prefetch) {
  const entries = Object.entries(prefetch ?? {});
  const failed = entries.filter(([, p]) => !p.not_offered).map(([key, p]) => `<span class="mono">${esc(key)}</span>${p.resource ? ` (${esc(p.resource)})` : ""}: ${esc(p.reason)}${p.status ? `, HTTP ${esc(String(p.status))}` : ""}`);
  const offered = entries.filter(([, p]) => p.not_offered).map(([key, p]) => `<span class="mono">${esc(key)}</span> (${esc(p.resource)})`);
  return [
    failed.length ? `<p class="muted mt-2">Prefetch reads that failed: ${failed.join("; ")}.</p>` : "",
    offered.length ? `<p class="muted mt-2">Not offered by this EHR, as the site declared, so not asked for: ${offered.join("; ")}.</p>` : "",
  ].join("");
}

function linksLine(links) {
  if (!links) return "";
  const read = links.read ?? {};
  const budget = links.budget ?? {};
  const statuses = Object.entries(links.statuses ?? {}).map(([status, n], at) => `${num(n, 0)} ${at ? "" : "failed "}with HTTP ${esc(status)}`);
  return `<p class="muted mt-2">Linked reads: notes ${num(read.notes ?? 0, 0)}, followed references ${num(read.follows ?? 0, 0)}; a decision reads at most ${num(budget.notes ?? 0, 0)} notes and ${num(budget.follows ?? 0, 0)} references${statuses.length ? `. Of the reads, ${statuses.join(", ")}` : ""}.</p>`;
}

function declarationLine(declared) {
  if (!declared) return "";
  return `<p class="muted mt-2">Read under the site's declaration of what this EHR does not offer: version <span class="mono">${esc(declared.version)}</span>, attested by ${esc(declared.attested_by)} (${esc(declared.reference)}), SHA-256 <span class="mono break-anywhere">${esc(declared.sha256)}</span>. A person decides.</p>`;
}

function coveragePanel(id, coverage, declared) {
  const rows = [
    { name: `Focus: ${esc(coverage.focus.resource)}`, from: readFrom({ [coverage.focus.resource]: coverage.focus.resources }), found: null, codings: null, unreadable: readReasons(coverage.focus.unreadable) },
    ...Object.entries(coverage.fields ?? {}).map(([name, f]) => ({ name: `<span class="mono">${esc(name)}</span>`, from: readFrom(f.resources ?? {}), found: f.values, codings: f.codings, unreadable: notRead(f) })),
    ...Object.entries(coverage.lists ?? {}).flatMap(([name, l]) => listRows(name, l)),
  ];
  const total = readReasons(coverage.unreadable);
  const dash = `<span class="muted">–</span>`;
  return `<section class="coverage" aria-labelledby="coverage-${esc(id)}">
    <h3 id="coverage-${esc(id)}">What it read</h3>
    <p class="muted">From the FHIR record, for each field and list the agent maps: counts only, never a value. Could not read, in all: ${total}.</p>
    ${tableHtml({
      id: `coverage-table-${id}`,
      caption: `<span class="sr-only">What the decision read from the FHIR record, by field</span>`,
      rows,
      columns: [
        { key: "name", label: "Field", cell: (row) => row.name },
        { key: "from", label: "Resources read", cell: (row) => (row.from === null ? dash : row.from) },
        { key: "found", label: "Values found", numeric: true, cell: (row) => (row.found === null || row.found === undefined ? dash : num(row.found, 0)) },
        { key: "codings", label: "Codings", cell: (row) => (row.codings === null ? dash : codings(row.codings)) },
        { key: "unreadable", label: "Could not read", cell: (row) => row.unreadable },
      ],
    })}
    ${prefetchLine(coverage.prefetch)}${linksLine(coverage.links)}${declarationLine(declared)}
  </section>`;
}

// What the agent wrote for a decision (ADR-0027). The texts a person was to read, each where it
// stands and shown to whoever may read it; or, for a decision no person was to read (it
// automated, or was blocked), their names alone: the record says each was `not_released`, and
// there is no text anywhere to show.
function writtenSection(found, sameAgent) {
  if (found.outputs?.length) {
    return `<h3>What the agent wrote</h3>
      <p class="muted">Each text was held for a person to read. A released text may be sent or filed as written; a withheld one may not.</p>
      ${textsList(found.outputs, { described: sameAgent })}`;
  }
  const unread = Object.entries(found.record.written ?? {})
    .filter(([, text]) => text?.state === "not_released")
    .map(([name]) => ({ name, state: "not_released" }));
  if (!unread.length) return "";
  return `<h3>What the agent wrote</h3>
    <p class="muted">No person was to read this decision, so what its agent wrote was returned to nobody and is kept nowhere.</p>
    ${textsList(unread, { brief: true })}`;
}

// What an event's note is, where it is not a person's note: a proposed action's arguments and a
// held text, each kept apart from the chain as inputs are.
const KEPT = { "action.proposed": "Arguments", "output.held": "Text" };

// An event's note, when it has one: its text for those who may read it, else what became of it.
function noteLine(e) {
  if (!e.note_status) return "";
  const what = KEPT[e.event] ?? "Note";
  if (e.note_status !== "open") return `<p class="event-note muted">${esc((NOTE_STATUS[e.note_status] ?? "Note").replace(/^Note/, what))}</p>`;
  // A held text is shown once, whole, under its name: the timeline does not repeat it.
  if (what === "Text") return `<p class="event-note muted">Its text is shown above, under “What the agent wrote”.</p>`;
  return `<p class="event-note"><b>${what}:</b> ${legible(what === "Note" ? e.note : keptArguments(e.note))}</p>`;
}

// A proposed action's kept note is `{name, arguments}`: the arguments, as JSON.
function keptArguments(note) {
  try {
    return JSON.stringify(JSON.parse(note).arguments ?? {});
  } catch {
    return note;
  }
}

// An action event in a line: which action, its effect, and what may be done with it.
function actionLine(e) {
  if (!e.event.startsWith("action.")) return "";
  const p = e.payload;
  return `<p class="event-note">${actionName(p)} ${effectBadge(p.effect)} ${dispositionBadge(p.disposition)}
    <span class="muted">${esc(actionReason(p.reason))}${p.reviewer ? ` · ${esc(p.reviewer)}` : ""}</span></p>`;
}

// A held text's event in a line: which text, where it stood then, and who gave its verdict.
function textLine(e) {
  if (!e.event.startsWith("output.")) return "";
  const p = e.payload;
  return `<p class="event-note">${textName(p.output)} ${textStateBadge(p.verdict ?? "held")}${p.reviewer ? ` <span class="muted">${esc(p.reviewer)}</span>` : ""}</p>`;
}

// The hash chain as an ordered list: what happened, when, and each event's hash.
function timeline(events) {
  return `<ol class="timeline">${events.map((e) => `<li>
    <div class="row"><b>${esc(EVENT_TEXT[e.event] ?? e.event)}</b>
      <span class="muted"><time datetime="${esc(e.created_at)}">${esc(time(e.created_at))}</time></span>
      <span class="grow"></span><span class="mono muted" title="hash ${esc(e.hash)}">#${e.seq} · ${esc(e.hash.slice(0, 12))}…</span></div>
    ${actionLine(e)}${textLine(e)}${noteLine(e)}
    <details><summary>Payload</summary><pre class="json">${esc(JSON.stringify(e.payload, null, 2))}</pre></details>
  </li>`).join("")}</ol>`;
}

"use strict";

// ---- utilities -------------------------------------------------------------------------------

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (v, d = 1) => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(d)}%`);
const num = (v, d = 3) => (v === null || v === undefined ? "n/a" : Number(v).toFixed(d));
// Times and days are shown in the deployment's time zone (from /api/health, default US Eastern),
// whatever the viewer's own clock says; the zone's short name (EDT, EST) is part of every time.
const DEFAULT_TIMEZONE = "America/New_York";
const time = (iso) => (iso ? state.timeFormat.format(new Date(iso)) : "");
const timeFormatFor = (timeZone) => new Intl.DateTimeFormat("en-US", {
  timeZone, year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
});
function zoneName() {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: state.timezone, timeZoneName: "longGeneric" }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? state.timezone;
  } catch {
    return state.timezone;
  }
}
const OUTCOMES = ["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK"];
// Plain words first; the exact code stays one hover away (docs/ui-ux-review.md, decision 1).
const OUTCOME_TEXT = { AUTO: "Automated", HUMAN_REVIEW: "Human review", ABSTAIN: "Abstained", BLOCK: "Blocked" };
const PRIORITY_TEXT = { stat: "Stat", asap: "ASAP", urgent: "Urgent", routine: "Routine" };
const outcomeText = (o) => OUTCOME_TEXT[o] ?? o;
const outcomeBadge = (o) => `<span class="badge o-${esc(o)}" title="${esc(o)}">${esc(outcomeText(o))}</span>`;
const priorityBadge = (p) => `<span class="badge p-${esc(p)}" title="priority: ${esc(p)}">${esc(PRIORITY_TEXT[p] ?? p)}</span>`;
// Icons come from the sprite in index.html; they are decorative, so the text beside them names them.
const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error((body && (body.detail || body.error)) || response.statusText);
  return body;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function storageGet(key, fallback) {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode: fine */ }
}

// ---- state & routing -------------------------------------------------------------------------

const state = { packs: [], packId: null, detail: null, timezone: DEFAULT_TIMEZONE, timeFormat: timeFormatFor(DEFAULT_TIMEZONE) };
const packPath = () => `/api/packs/${state.packId}`;
const views = {};

async function loadPack(packId) {
  state.packId = packId;
  state.detail = await api(packPath());
}

// `navigated`: the user moved to another view, so it starts at the top with its heading focused
// (screen readers then announce the new view once, instead of reading the whole page).
async function render({ navigated = false } = {}) {
  const view = (location.hash || "#overview").slice(1);
  document.querySelectorAll(".nav a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  const main = $("#view");
  main.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    await (views[view] || views.overview)(main);
  } catch (error) {
    main.innerHTML = `<div class="panel">Could not load this view: ${esc(error.message)}</div>`;
  }
  if (navigated) {
    window.scrollTo(0, 0);
    const heading = main.querySelector("h1");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  }
  refreshReviewCount();
}

const scrollBehavior = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

// Disable a button while its request runs, so a slow provider can't be submitted twice.
async function whileBusy(button, busyText, task) {
  const text = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = busyText;
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = text;
  }
}

// ---- pack-generic rendering ------------------------------------------------------------------

// Packs whose input is a patient message (the inbox) keep a message-shaped UI.
const isMessagePack = () => Boolean(state.detail.input_schema?.properties?.message);

function inputView(input) {
  if (!input) return `<div class="message">(input not stored: audit.store_input = hash)</div>`;
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

// For a derived primary (e.g. eligibility), the member answers are what a reviewer checks:
// one row per criterion, with what that answer does in the decision table.
function derivedPrimary() {
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

function criteriaTable(answers, { evidence = true } = {}) {
  const derive = derivedPrimary();
  if (!derive || !answers) return "";
  const questions = state.detail.manifest.questions;
  const rows = derive.from.map((member) => {
    const a = answers[member];
    const effect = a ? memberEffect(derive, member, a.label, answers) : null;
    return `<tr class="${effect ? "hit" : ""}">
      <td title="${esc(questions[member]?.instructions ?? "")}"><code>${esc(member)}</code></td>
      <td>${a ? `<b>${esc(a.label)}</b>` : `<span class="muted">no answer</span>`}</td>
      <td class="mono">${a ? pct(a.confidence, 0) : ""}</td>
      <td>${effect ? `<span class="badge s-warning">→ ${esc(effect)}</span>` : ""}</td>
      <td class="muted">${a ? esc(a.provider.variant) : ""}</td>
      ${evidence ? `<td class="evidence">${a && a.evidence.length ? esc(a.evidence.join("; ")) : ""}</td>` : ""}
    </tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="compact criteria">
    <tr><th>Criterion</th><th>Answer</th><th>Conf.</th><th>Effect</th><th>Source</th>${evidence ? "<th>Evidence</th>" : ""}</tr>${rows}</table></div>`;
}

async function refreshReviewCount() {
  try {
    const pending = await api(`/api/review?pack_id=${encodeURIComponent(state.packId)}&limit=500`);
    $("#review-count").textContent = pending.length || "";
  } catch { /* the badge is optional */ }
}

// ---- overview --------------------------------------------------------------------------------

views.overview = async (main) => {
  const [stats, chain] = await Promise.all([
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
    api("/api/audit/verify"),
  ]);
  const d = state.detail;
  const total = stats.decisions;
  const autoShare = total ? stats.outcomes.AUTO / total : null;
  main.innerHTML = `
    <h1>${esc(d.name)}</h1>
    <p class="muted">${esc(d.id)} · v${esc(d.version)} · risk level ${d.risk.level} (${esc(d.risk.category)}) ·
      providers ${Object.entries(d.active_providers).map(([s, p]) => `${esc(s)}=<b>${esc(p.variant)}</b>`).join(", ")}</p>
    <div class="tiles">
      ${tile("Decisions", total)}
      ${tile("Automated", pct(autoShare), `${stats.outcomes.AUTO} decisions`)}
      ${tile("Waiting for review", stats.pending_reviews, "human review or abstained")}
      ${tile("Human override rate", pct(stats.human_override_rate), `${stats.reviews_resolved} reviews`)}
      ${tile("Observed accuracy", pct(stats.observed_accuracy), "vs. final human labels")}
      ${tile("Latency p95", stats.latency_ms.p95 === null ? "n/a" : `${num(stats.latency_ms.p95, 2)} ms`)}
      ${tile("Cost / decision", stats.cost_usd.per_decision === null ? "n/a" : `$${num(stats.cost_usd.per_decision, 5)}`)}
      ${tile("Audit chain", chain.ok ? "verified" : "BROKEN", `${chain.events} events`)}
      ${tile("Feedback", `${icon("thumbs-up")}<span class="sr-only">Right:</span> ${stats.feedback.up} <span class="muted">·</span> ${icon("thumbs-down")}<span class="sr-only">Wrong:</span> ${stats.feedback.down}`,
        `${stats.feedback.down_on_auto} wrong on automated decisions`, "", { html: true })}
    </div>
    <div class="panel">
      <h3>Outcome mix</h3>
      ${stackBar(stats.outcomes, total)}
    </div>
    <div class="panel">
      <h3>Daily volume <span class="muted">· days in ${esc(zoneName())}</span></h3>
      ${stats.daily.length ? `<div class="table-wrap"><table>
        <tr><th>Date</th>${OUTCOMES.map((o) => `<th title="${o}">${esc(outcomeText(o))}</th>`).join("")}<th class="mix">Mix</th></tr>
        ${stats.daily.map((day) => {
          const n = OUTCOMES.reduce((sum, o) => sum + day[o], 0);
          return `<tr><td>${esc(day.date)}</td>${OUTCOMES.map((o) => `<td>${day[o]}</td>`).join("")}<td>${stackBar(day, n, false)}</td></tr>`;
        }).join("")}
      </table></div>` : `<div class="empty">No decisions yet. Use <a href="#try">Try it</a>, the API, or the sandbox worker.</div>`}
    </div>`;
};

// `html`: the value is trusted markup built here (icons with numbers), not data to escape.
function tile(label, value, sub = "", kind = "", { html = false } = {}) {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${kind}">${html ? value : esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
}

function stackBar(counts, total, legend = true) {
  const parts = OUTCOMES.map((o) => (total ? `<span class="bg-${o}" style="width:${(100 * counts[o]) / total}%" title="${outcomeText(o)}: ${counts[o]}"></span>` : "")).join("");
  const keys = legend ? `<div class="legend">${OUTCOMES.map((o) => `<span><i class="bg-${o}"></i>${esc(outcomeText(o))} ${counts[o]} (${pct(total ? counts[o] / total : null)})</span>`).join("")}</div>` : "";
  return `<div class="stack">${parts}</div>${keys}`;
}

// ---- review queue ----------------------------------------------------------------------------

views.review = async (main) => {
  const items = await api(`/api/review?pack_id=${encodeURIComponent(state.packId)}&limit=100`);
  const labels = state.detail.labels;
  main.innerHTML = `
    <h1>Review queue</h1>
    <p class="muted">Decisions held for human review or abstained, stat first. Choosing a label other than the recommendation records a human override.</p>
    <div class="row panel">
      <label for="reviewer">Reviewer</label>
      <input id="reviewer" value="${esc(storageGet("oh.reviewer", "sandbox-nurse"))}" autocomplete="name">
      <span class="muted">${items.length} pending</span>
    </div>
    <div id="queue" class="mt-4">
      ${items.length ? items.map((item) => reviewCard(item, labels)).join("") : `<div class="panel empty">Nothing waiting for review.</div>`}
    </div>`;
  $("#reviewer").addEventListener("change", (e) => storageSet("oh.reviewer", e.target.value.trim()));
  main.querySelectorAll("button[data-label]").forEach((button) =>
    button.addEventListener("click", async () => {
      const card = button.closest(".review-card");
      const reviewer = $("#reviewer").value.trim();
      if (!reviewer) return toast("Enter a reviewer name first");
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        const result = await api(`/api/review/${card.dataset.id}`, {
          method: "POST",
          body: JSON.stringify({ final_label: button.dataset.label, reviewer }),
        });
        card.remove();
        toast(result.human_override ? `Override recorded → ${button.dataset.label}` : `Confirmed → ${button.dataset.label}`);
        refreshReviewCount();
      } catch (error) {
        toast(error.message);
        card.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    }),
  );
};

function reviewCard(item, labels) {
  const record = item.record;
  const recommendation = item.recommendation;
  return `<article class="panel review-card" data-id="${esc(item.id)}">
    <div class="row">
      ${priorityBadge(item.priority)} ${outcomeBadge(item.outcome)}
      <span class="muted">${esc(time(item.created_at))}</span>
      <span class="grow"></span>
      <span class="mono muted">${esc(item.id.slice(0, 8))}</span>
    </div>
    ${inputView(record.input)}
    <div>Recommendation: <b>${esc(recommendation ?? "none (abstained)")}</b>
      ${item.confidence !== null ? `<span class="muted">· confidence ${pct(item.confidence)}</span>` : ""}</div>
    <ul class="reasons">${record.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${criteriaTable(record.answers)}
    <div class="labels">${labels.map((label) =>
      `<button data-label="${esc(label)}" class="${label === recommendation ? "recommended" : ""}">${label === recommendation ? `${icon("check")}<span class="sr-only">Recommended:</span> ` : ""}${esc(label)}</button>`).join("")}
    </div>
  </article>`;
}

// ---- audit trail -----------------------------------------------------------------------------

views.audit = async (main) => {
  const outcome = storageGet("oh.audit.outcome", "");
  main.innerHTML = `
    <h1>Audit trail</h1>
    <p class="muted">Every decision is recorded once; reviews and outcomes are appended as hash-chained events.</p>
    <div class="row panel">
      <label for="outcome-filter">Outcome</label>
      <select id="outcome-filter"><option value="">All</option>${OUTCOMES.map((o) => `<option value="${o}" ${o === outcome ? "selected" : ""}>${esc(outcomeText(o))}</option>`).join("")}</select>
      <span class="grow"></span>
      <button id="verify">Verify chain</button>
      <span id="verify-result"></span>
    </div>
    <div class="panel table-wrap" id="audit-table"></div>
    <div id="audit-detail"></div>`;
  const load = async () => {
    const filter = $("#outcome-filter").value;
    storageSet("oh.audit.outcome", filter);
    const rows = await api(`/api/audit?pack_id=${encodeURIComponent(state.packId)}&limit=100${filter ? `&outcome=${filter}` : ""}`);
    $("#audit-table").innerHTML = rows.length ? `<table>
      <tr><th>Time</th><th>Outcome</th><th>Recommendation</th><th>Priority</th><th>Confidence</th><th>Latency</th><th>Review</th><th>ID</th><th>Right?</th></tr>
      ${rows.map((r) => `<tr class="clickable" data-id="${esc(r.id)}">
        <td><button type="button" class="linklike open-record" aria-label="Open decision ${esc(r.id.slice(0, 8))}">${esc(time(r.created_at))}</button></td>
        <td>${outcomeBadge(r.outcome)}</td><td>${esc(r.recommendation ?? "-")}</td>
        <td>${priorityBadge(r.priority)}</td><td>${pct(r.confidence)}</td><td>${num(r.latency_ms, 2)} ms</td>
        <td>${esc(r.review_status === "resolved" ? `${r.final_label}${r.human_override ? " (override)" : ""}` : r.review_status.replace("_", " "))}</td>
        <td class="mono">${esc(r.id.slice(0, 8))}</td><td>${feedbackControls(r.id, true)}</td></tr>`).join("")}
    </table>` : `<div class="empty">No decisions recorded.</div>`;
    $("#audit-table").querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => showRecord(tr.dataset.id)));
  };
  $("#outcome-filter").addEventListener("change", load);
  $("#verify").addEventListener("click", async () => {
    const result = await api("/api/audit/verify");
    $("#verify-result").innerHTML = result.ok
      ? `<span class="badge s-ok">verified · ${result.events} events</span>`
      : `<span class="badge s-degraded">broken at event ${result.broken_at}</span>`;
  });
  await load();
};

async function showRecord(id) {
  const record = await api(`/api/audit/${id}`);
  $("#audit-detail").innerHTML = `<div class="panel">
    <div class="row"><h3 tabindex="-1" class="break-anywhere">Decision ${esc(id)}</h3><span class="grow"></span>${outcomeBadge(record.outcome)}</div>
    <h3>Events (hash chain)</h3>
    <div class="table-wrap"><table><tr><th>#</th><th>Event</th><th>At</th><th>Hash</th><th>Payload</th></tr>
      ${record.events.map((e) => `<tr><td>${e.seq}</td><td>${esc(e.event)}</td><td>${esc(time(e.created_at))}</td>
        <td class="mono">${esc(e.hash.slice(0, 16))}…</td><td class="mono break-anywhere">${esc(JSON.stringify(e.payload))}</td></tr>`).join("")}
    </table></div>
    ${derivedPrimary() ? `<h3>Criteria</h3>${criteriaTable(record.record.answers)}` : ""}
    <h3>Was this right?</h3>${feedbackControls(id)}
    <h3>Record</h3><pre class="json">${esc(JSON.stringify(record.record, null, 2))}</pre></div>`;
  $("#audit-detail").scrollIntoView({ behavior: scrollBehavior() });
  $("#audit-detail h3").focus({ preventScroll: true });
}

// ---- evaluations -----------------------------------------------------------------------------

views.evals = async (main) => {
  const reports = await api(`/api/evals?pack_id=${encodeURIComponent(state.packId)}`);
  const datasets = state.detail.datasets;
  const preselected = datasets.find((d) => d.includes("local-site")) ?? datasets[0];
  main.innerHTML = `
    <h1>Evaluations</h1>
    <p class="muted">Run the pack on a labelled dataset. Anything other than the benchmark dataset is compared against the published benchmark (local validation).</p>
    <div class="row panel">
      <label for="dataset">Dataset</label>
      <select id="dataset">${datasets.map((d) => `<option ${d === preselected ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
      <button class="primary" id="run-eval">Run evaluation</button>
      <span class="grow"></span>
      <label for="history" class="sr-only">Report shown</label>
      <select id="history"><option value="">Published benchmark</option>${reports.map((r, i) =>
        `<option value="${i}">${esc(time(r.created_at))} · ${esc(r.dataset.name)} (n=${r.dataset.n})</option>`).join("")}</select>
    </div>
    <div id="report"></div>
    ${state.detail.leaderboard ? leaderboardView(state.detail.leaderboard) : ""}`;
  const show = (report) => ($("#report").innerHTML = report ? reportView(report) : `<div class="panel empty">No benchmark published for this pack.</div>`);
  show(state.detail.benchmark);
  $("#history").addEventListener("change", (e) => show(e.target.value === "" ? state.detail.benchmark : reports[Number(e.target.value)]));
  $("#run-eval").addEventListener("click", (e) =>
    whileBusy(e.currentTarget, "Running…", async () => {
      try {
        show(await api(`${packPath()}/evals`, { method: "POST", body: JSON.stringify({ dataset: $("#dataset").value }) }));
      } catch (error) {
        toast(error.message);
      }
    }),
  );
};

function leaderboardView(board) {
  const groups = {};
  for (const row of board.rows) (groups[row.dataset] ??= []).push(row);
  const critical = (row) => row.critical.filter((c) => c.n).map((c) =>
    `${row.critical.length > 1 ? `${c.question}=${c.label} ` : ""}${c.detected}/${c.n} (${c.auto_missed} automated)`).join("; ") || "n/a";
  const ms = (v) => (v < 10 ? `${v.toFixed(2)} ms` : `${Math.round(v).toLocaleString()} ms`);
  return `<div class="panel"><h2>Provider comparison</h2>
    <p class="muted">Every configuration ran on the same records (updated ${esc(time(board.updated_at))}). Remote models are named because they are a deployment choice.</p>
    ${Object.entries(groups).map(([dataset, rows]) => `<h3>${esc(dataset)} <span class="muted">n=${Math.max(...rows.map((r) => r.n))}</span></h3>
      <div class="table-wrap"><table class="board">
        <tr><th>Configuration</th><th>Accuracy</th><th>Macro-F1</th><th>ECE</th><th>Automated</th><th>False automation</th><th>Critical detected</th><th>p50</th><th>Cost / decision</th><th>Errors</th></tr>
        ${rows.sort((a, b) => (b.accuracy ?? 0) - (a.accuracy ?? 0)).map((r) => `<tr><td>${esc(r.config)}</td><td>${pct(r.accuracy)}</td><td>${num(r.macro_f1)}</td>
          <td>${num(r.ece)}</td><td>${pct(r.auto_rate)}</td><td>${pct(r.false_automation_rate)}</td><td>${esc(critical(r))}</td>
          <td class="mono">${ms(r.latency_p50_ms)}</td><td class="mono">$${r.cost_per_decision_usd.toFixed(5)}</td>
          <td title="${esc(Object.entries(r.error_codes ?? {}).map(([c, n]) => `${c}: ${n}`).join(", "))}">${r.errors}</td></tr>`).join("")}
      </table></div>${otherQuestions(rows)}`).join("")}
  </div>`;
}

// Accuracy / ECE of the non-primary questions whose results differ between configurations.
function otherQuestions(rows) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r.questions ?? {})))].sort();
  const varying = keys.filter((k) => new Set(rows.map((r) => JSON.stringify(r.questions?.[k] ?? null))).size > 1);
  if (!varying.length) return "";
  const cell = (m) => (m ? `${pct(m.accuracy)} / ${num(m.ece)}` : "n/a");
  return `<p class="muted mt-3">Other questions (accuracy / ECE)</p><div class="table-wrap"><table class="board">
    <tr><th>Configuration</th>${varying.map((k) => `<th>${esc(k)}</th>`).join("")}</tr>
    ${rows.map((r) => `<tr><td>${esc(r.config)}</td>${varying.map((k) => `<td>${cell(r.questions?.[k])}</td>`).join("")}</tr>`).join("")}
  </table></div>`;
}

function reportView(r) {
  const q = r.questions[r.primary];
  const d = r.decisions;
  const critical = d.critical.map((c) => tile(`Critical ${c.question}=${c.label}`, `${c.detected}/${c.n}`, `${c.auto_missed} automated`)).join("");
  return `
    <div class="panel">
      <div class="row"><b>${esc(r.dataset.name)}</b><span class="muted">n=${r.dataset.n} · ${esc(time(r.created_at))} ·
        ${Object.entries(r.providers).map(([s, p]) => `${esc(s)}=${esc(p.variant)}`).join(", ")}</span></div>
    </div>
    ${r.comparison ? comparisonView(r.comparison) : ""}
    <div class="tiles mt-4">
      ${tile(`${r.primary} accuracy`, pct(q.accuracy), `macro-F1 ${num(q.macro_f1)}`)}
      ${tile("Calibration (ECE)", num(q.calibration.ece), `Brier ${num(q.brier)}`)}
      ${tile("Automated", pct(d.outcome_rates.AUTO), `accuracy ${pct(d.automation.accuracy)}`)}
      ${tile("False automation rate", pct(d.automation.false_automation_rate), `of ${d.automation.n} automated`)}
      ${tile("Abstention", pct(d.abstention_rate))}
      ${tile("Expected override rate", pct(d.review.expected_override_rate), `of ${d.review.n} reviewed`)}
      ${critical}
      ${tile("Latency p50 / p95", `${num(r.latency_ms.p50, 2)} / ${num(r.latency_ms.p95, 2)}`, "ms")}
      ${tile("Cost / decision", r.cost.per_decision_usd === null ? "unknown" : `$${num(r.cost.per_decision_usd, 5)}`)}
    </div>
    <div class="grid-2">
      <div class="panel"><h3>Reliability</h3>${reliability(q.calibration.bins)}</div>
      <div class="panel"><h3>Threshold sweep</h3><table>
        <tr><th>Automate at ≥</th><th>Coverage</th><th>Accuracy</th><th>Errors</th></tr>
        ${r.threshold_sweep.map((t) => `<tr><td>${t.threshold.toFixed(2)}</td><td>${pct(t.coverage)}</td><td>${pct(t.accuracy)}</td><td>${t.errors}</td></tr>`).join("")}
      </table></div>
    </div>
    <div class="panel table-wrap"><h3>Per label</h3><table>
      <tr><th>Label</th><th>Precision</th><th>Recall</th><th>F1</th><th>n</th></tr>
      ${Object.entries(q.per_label).map(([label, m]) => `<tr><td>${esc(label)}</td><td>${pct(m.precision)}</td><td>${pct(m.recall)}</td><td>${num(m.f1)}</td><td>${m.support}</td></tr>`).join("")}
    </table></div>
    <div class="panel table-wrap"><h3>Subgroups</h3><table>
      <tr><th>Subgroup</th><th>n</th><th>Accuracy</th><th>Automated</th><th>Abstained</th><th>False automation</th></tr>
      ${Object.entries(r.subgroups).flatMap(([attr, values]) => Object.entries(values).map(([value, m]) =>
        `<tr><td>${esc(attr)}=${esc(value)}</td><td>${m.n}</td><td>${pct(m.accuracy)}</td><td>${pct(m.auto_rate)}</td><td>${pct(m.abstention_rate)}</td><td>${pct(m.false_automation_rate)}</td></tr>`)).join("")}
    </table></div>`;
}

function comparisonView(c) {
  if (c.status === "unknown") return `<div class="panel">${esc(c.reason)}</div>`;
  const label = { ok: "No degradation", warning: "Localized degradation", degraded: "Performance degradation detected" }[c.status];
  return `<div class="panel">
    <div class="row"><span class="badge s-${esc(c.status)}">${esc(label)}</span>
      <span>Public benchmark <b>${pct(c.baseline.accuracy)}</b> → local <b>${pct(c.local.accuracy)}</b>
      (${(c.delta * 100).toFixed(1)} pts; tolerance ${(c.tolerance * 100).toFixed(1)})</span></div>
    ${c.findings.length ? `<div class="mt-2">${c.findings.map((f) => `<div class="finding"><b>${esc(f.subject)}</b>:
      ${f.kind === "critical" ? `${f.local} of ${f.n}` : f.kind === "calibration" ? `${num(f.baseline)} → ${num(f.local)}` : `${pct(f.baseline)} → ${pct(f.local)} <span class="muted">(n=${f.n})</span>`}</div>`).join("")}</div>` : ""}
  </div>`;
}

function reliability(bins) {
  const size = 240, pad = 28, span = size - 2 * pad;
  const x = (v) => pad + v * span, y = (v) => size - pad - v * span;
  const total = bins.reduce((s, b) => s + b.n, 0) || 1;
  const dots = bins.map((b) => `<circle cx="${x(b.confidence)}" cy="${y(b.accuracy)}" r="${3 + 9 * Math.sqrt(b.n / total)}"
      fill="var(--chart-1)" fill-opacity="0.75"><title>${b.low.toFixed(1)}–${b.high.toFixed(1)}: n=${b.n}, confidence ${pct(b.confidence)}, accuracy ${pct(b.accuracy)}</title></circle>`).join("");
  return `<svg viewBox="0 0 ${size} ${size}" width="100%" class="chart-small" role="img" aria-label="Reliability diagram">
    <rect x="${pad}" y="${pad}" width="${span}" height="${span}" fill="none" stroke="var(--color-border)"/>
    <line x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}" stroke="var(--color-text-muted)" stroke-dasharray="4 4"/>
    ${dots}
    <text x="${pad}" y="${size - 8}">0</text><text x="${size - pad - 6}" y="${size - 8}">1</text>
    <text x="${size / 2 - 26}" y="${size - 8}">confidence</text>
    <text x="4" y="${pad + 4}">1</text><text x="4" y="${size / 2}">acc</text>
  </svg><p class="muted">Dots on the dashed line are perfectly calibrated; dot size is bin volume.</p>`;
}

// ---- feedback view ---------------------------------------------------------------------------

views.feedback = async (main) => {
  const rating = storageGet("oh.feedback.rating", "down");
  const [items, stats] = await Promise.all([
    api(`/api/feedback?pack_id=${encodeURIComponent(state.packId)}${rating ? `&rating=${rating}` : ""}&limit=200`),
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
  ]);
  main.innerHTML = `
    <h1>Feedback</h1>
    <p class="muted">Thumbs up (right) and thumbs down (wrong) from anyone using the system. Thumbs-down cases are where the pack is wrong:
      download them, fill in the right answers, and they become tests and evaluation data for the next version.</p>
    <div class="tiles">
      ${tile("Right", stats.feedback.up, "thumbs up")}
      ${tile("Wrong", stats.feedback.down, "thumbs down")}
      ${tile("Wrong on automated", stats.feedback.down_on_auto, "confidently wrong: automated, then rated wrong")}
    </div>
    <div class="row panel">
      <label for="rating-filter">Show</label>
      <select id="rating-filter">
        <option value="down" ${rating === "down" ? "selected" : ""}>Thumbs down</option>
        <option value="up" ${rating === "up" ? "selected" : ""}>Thumbs up</option>
        <option value="" ${rating === "" ? "selected" : ""}>All</option>
      </select>
      <span class="grow"></span>
      <a class="button" href="${packPath()}/feedback/export?rating=down" download>${icon("download")} Download thumbs-down cases (JSONL)</a>
    </div>
    ${items.length ? items.map(feedbackCard).join("") : `<div class="panel empty">No feedback yet. Rate decisions as right or wrong in Try it or the Audit trail.</div>`}`;
  $("#rating-filter").addEventListener("change", (e) => {
    storageSet("oh.feedback.rating", e.target.value);
    render();
  });
};

function feedbackCard(item) {
  const r = item.record;
  const urgency = r.answers.urgency ? ` · urgency ${esc(r.answers.urgency.label)}` : "";
  return `<article class="panel">
    <div class="row">
      <span class="badge ${item.rating === "down" ? "s-degraded" : "s-ok"}">${item.rating === "down" ? `${icon("thumbs-down")} Wrong` : `${icon("thumbs-up")} Right`}</span>
      ${outcomeBadge(r.outcome)} ${priorityBadge(r.priority)}
      <span>→ <b>${esc(r.recommendation ?? "no recommendation")}</b>${urgency}</span>
      <span class="grow"></span>
      <span class="muted">${esc(time(item.created_at))}${item.reporter ? ` · ${esc(item.reporter)}` : ""}</span>
    </div>
    ${inputView(r.input)}
    ${criteriaTable(r.answers, { evidence: false })}
    ${item.note ? `<p><b>Note:</b> ${esc(item.note)}</p>` : ""}
    <span class="mono muted break-anywhere">decision ${esc(item.decision_id)}</span>
  </article>`;
}

// ---- decision card ---------------------------------------------------------------------------

// `any` / `all` hold a label or a list of labels.
const rowLabels = (value) => (value === null || value === undefined ? [] : Array.isArray(value) ? value : [value]);

function deriveRow(row) {
  const among = row.in ? `of ${row.in.map((m) => `<code>${esc(m)}</code>`).join(", ")}` : "member";
  const codes = (labels) => labels.map((l) => `<code>${esc(l)}</code>`).join(" or ");
  const gate = row.when ? `, when ${Object.entries(row.when).map(([m, labels]) => `<code>${esc(m)}</code> is ${codes(labels)}`).join(" and ")}` : "";
  if (row.any !== null && row.any !== undefined) return `any ${among} is ${codes(rowLabels(row.any))}${gate} → <code>${esc(row.then)}</code>`;
  if (row.all !== null && row.all !== undefined) return `every ${among} is ${codes(rowLabels(row.all))}${gate} → <code>${esc(row.then)}</code>`;
  return `otherwise → <code>${esc(row.else)}</code>`;
}

function questionPanel(key, q, primary) {
  const kind = q.type && q.type !== "choice" ? ` <span class="chip">${esc(q.type)}</span>` : "";
  const derive = q.derive ? `<p>Derived from ${q.derive.from.map((x) => `<code>${esc(x)}</code>`).join(", ")}
      (first matching row wins; members treated as independent):</p>
    <ol>${q.derive.table.map((row) => `<li>${deriveRow(row)}</li>`).join("")}</ol>` : "";
  const labels = q.type === "score" ? Object.keys(q.levels).sort((a, b) => q.levels[a].score - q.levels[b].score) : Object.keys(q.criteria);
  const score = (label) => (q.type === "score" ? `<td class="mono">${esc(q.levels[label].score)}</td>` : "");
  return `<div class="panel"><h3>Question <code>${esc(key)}</code>${primary ? " (primary)" : ""}${kind}</h3>
    <p class="muted">${esc(q.instructions)}</p>${derive}
    <table>${labels.map((label) => `<tr><td><code>${esc(label)}</code></td>${score(label)}<td>${esc(q.criteria[label])}</td></tr>`).join("")}</table></div>`;
}

function conditionText(when) {
  const parts = [];
  if (when.min_probability !== null && when.min_probability !== undefined) parts.push(`P(${esc(when.question)} ∈ [${when.labels.map(esc).join(", ")}]) ≥ ${esc(when.min_probability)}`);
  else if (when.labels.length) parts.push(`${esc(when.question)} ∈ [${when.labels.map(esc).join(", ")}]`);
  if (when.min_score !== null && when.min_score !== undefined) parts.push(`${esc(when.question)} expected score ≥ ${esc(when.min_score)}`);
  if (when.max_score !== null && when.max_score !== undefined) parts.push(`${esc(when.question)} expected score ≤ ${esc(when.max_score)}`);
  const confidence = when.min_confidence ? ` with p ≥ ${when.min_confidence}` : "";
  return parts.join(" and ") + confidence;
}

views.card = async (main) => {
  const d = state.detail;
  const m = d.manifest;
  const p = m.policy;
  main.innerHTML = `
    <h1>${esc(m.name)}</h1>
    <p class="muted">${esc(m.id)} · v${esc(m.version)} · ${esc(m.license)} · ${esc(m.publisher.name)} (${esc(m.publisher.type)})</p>
    <p>${esc(m.summary)}</p>
    <div class="tiles">
      ${tile("Risk level", m.risk.level, m.risk.category)}
      ${tile("Automation threshold", p.thresholds.auto.toFixed(2), `review ≥ ${p.thresholds.review.toFixed(2)}`)}
      ${tile("CDS Hooks service", d.cds_service ?? "none", m.cds_hooks ? `hook: ${m.cds_hooks.hook}` : "", "ident")}
    </div>
    <div class="panel"><b>Digest</b> <span class="mono break-anywhere">${esc(d.digest)}</span></div>
    <div class="grid-2">
      <div class="panel"><h3>Intended use</h3><p>${esc(m.card.intended_use)}</p>
        <h3>Users</h3><ul>${m.card.intended_users.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>
        <h3>Population</h3><p>${esc(m.card.patient_population)}</p>
        <h3>Why risk level ${m.risk.level}</h3><p class="muted">${esc(m.risk.rationale)}</p></div>
      <div class="panel"><h3>Out of scope</h3><ul>${m.card.out_of_scope.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>
        <h3>Known failure modes</h3><ul>${m.card.known_failure_modes.map((u) => `<li>${esc(u)}</li>`).join("")}</ul></div>
    </div>
    ${Object.entries(m.questions).map(([key, q]) => questionPanel(key, q, key === p.primary)).join("")}
    <div class="panel"><h3>Policy</h3><ul>
      ${p.rules.map((r) => `<li><b>${esc(r.name)}</b>: ${conditionText(r.when)}
        ⇒ ${outcomeBadge(r.outcome)} ${r.recommendation ? `→ ${esc(r.recommendation)}` : ""} ${r.priority ? priorityBadge(r.priority) : ""}<br><span class="muted">${esc(r.reason)}</span></li>`).join("")}
      ${Object.entries(p.labels).map(([label, l]) => `<li><code>${esc(label)}</code>: ${l.allow_auto ? `automated at ≥ ${l.auto_threshold ?? p.thresholds.auto}` : "never automated"}</li>`).join("")}
      ${Object.entries(p.automation_requires ?? {}).map(([slot, variants]) => `<li>Automated only while <code>${esc(slot)}</code> is ${variants.map((v) => `<code>${esc(v)}</code>`).join(" or ")}; otherwise a person reviews</li>`).join("")}
      <li>Fallback: ${esc(p.fallback)}</li></ul></div>
    <div class="panel table-wrap"><h3>Providers</h3><table>
      <tr><th>Slot</th><th>Answers</th><th>Active</th><th>Variants</th></tr>
      ${Object.entries(m.providers).map(([slot, s]) => `<tr><td>${esc(slot)}</td><td>${s.answers.map(esc).join(", ")}</td>
        <td><b>${esc(d.active_providers[slot].variant)}</b> <span class="muted mono">${esc(d.active_providers[slot].model ?? "")}</span></td>
        <td>${Object.entries(s.variants).map(([name, v]) => `${esc(name)} (${esc(v.type)})`).join(", ")}</td></tr>`).join("")}
    </table></div>
    <div class="grid-2">
      <div class="panel"><h3>Input schema</h3><pre class="json">${esc(JSON.stringify(d.input_schema, null, 2))}</pre></div>
      <div class="panel"><h3>Output schema</h3><pre class="json">${esc(JSON.stringify(d.output_schema, null, 2))}</pre></div>
    </div>`;
};

// ---- try it ----------------------------------------------------------------------------------

const SAMPLES = [
  { label: "Refill", subject: "Refill request", body: "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks" },
  { label: "Chest pain", subject: "", body: "Having tight chest pressure and I'm sweating a lot right now." },
  { label: "Bill (es)", subject: "", body: "Recibí una factura de $120 y no entiendo por qué.", language: "es", channel: "sms" },
  { label: "Lab result", subject: "Lab results", body: "My A1c came back at 7.8. What does that mean? Do I need to change anything?" },
  { label: "Outside records", subject: "", body: "I had an MRI done at Riverside Imaging. Can you get the images and the report sent over?" },
  { label: "Vague", subject: "", body: "hello?" },
];

views.try = async (main) => (isMessagePack() ? tryMessage(main) : tryRecord(main));

// Packs whose input is a structured record: edit JSON, starting from the pack's own examples.
async function tryRecord(main) {
  const examples = await api(`${packPath()}/examples`);
  const expected = (e) => Object.entries(e.expect).filter(([k]) => k !== "answers").map(([k, v]) => `${k} ${Array.isArray(v) ? v.join("/") : v}`).join(", ");
  main.innerHTML = `
    <h1>Try it</h1>
    <p class="muted">Synthetic records only. Decisions made here are recorded in the audit trail and review queue.
      The examples are the pack's behaviour tests.</p>
    <div class="panel">
      <div class="row mb-3">
        <label for="example">Example</label>
        <select id="example" class="grow">${examples.length ? "" : `<option value="">No behaviour tests</option>`}${examples.map((e, i) =>
          `<option value="${i}">${esc(e.name)}</option>`).join("")}</select>
      </div>
      <p class="muted" id="expected"></p>
      <label for="record">Input (JSON)</label>
      <textarea id="record" class="code" spellcheck="false"></textarea>
      <div class="row mt-3"><span class="grow"></span><button class="primary" id="decide">Decide</button></div>
    </div>
    <div id="result"></div>`;
  // The picker and the input always show the same example, starting with the first.
  const choose = (index) => {
    const example = examples[index];
    $("#record").value = JSON.stringify(example?.input ?? {}, null, 2);
    $("#expected").textContent = example && expected(example) ? `The pack's test expects: ${expected(example)}.` : "";
  };
  choose(0);
  $("#example").addEventListener("change", (e) => choose(Number(e.target.value)));
  $("#decide").addEventListener("click", (e) => {
    let input;
    try {
      input = JSON.parse($("#record").value);
    } catch (error) {
      return toast(`Not valid JSON: ${error.message}`);
    }
    return whileBusy(e.currentTarget, "Deciding…", async () => {
      try {
        $("#result").innerHTML = decisionView(await api(`${packPath()}/decide`, { method: "POST", body: JSON.stringify(input) }));
        refreshReviewCount();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

async function tryMessage(main) {
  main.innerHTML = `
    <h1>Try it</h1>
    <p class="muted">Synthetic messages only. Decisions made here are recorded in the audit trail and review queue.</p>
    <div class="panel">
      <div class="chips mb-3">${SAMPLES.map((s, i) => `<button data-sample="${i}">${esc(s.label)}</button>`).join("")}</div>
      <label for="subject">Subject</label><input id="subject" class="field-full">
      <label for="body">Message</label><textarea id="body"></textarea>
      <div class="row mt-3">
        <label>Channel <select id="channel"><option>portal</option><option>sms</option><option>email</option></select></label>
        <label>Language <select id="language"><option>en</option><option>es</option></select></label>
        <label>Age band <select id="age"><option value="">unknown</option><option>0-17</option><option>18-39</option><option>40-64</option><option>65+</option></select></label>
        <span class="grow"></span>
        <button class="primary" id="decide">Decide</button>
      </div>
    </div>
    <div id="result"></div>`;
  main.querySelectorAll("button[data-sample]").forEach((b) => b.addEventListener("click", () => {
    const s = SAMPLES[Number(b.dataset.sample)];
    $("#subject").value = s.subject; $("#body").value = s.body;
    $("#channel").value = s.channel || "portal"; $("#language").value = s.language || "en";
  }));
  $("#decide").addEventListener("click", (e) => {
    const message = { body: $("#body").value, channel: $("#channel").value };
    if ($("#subject").value.trim()) message.subject = $("#subject").value.trim();
    const patient = { preferred_language: $("#language").value };
    if ($("#age").value) patient.age_band = $("#age").value;
    return whileBusy(e.currentTarget, "Deciding…", async () => {
      try {
        const result = await api(`${packPath()}/decide`, { method: "POST", body: JSON.stringify({ message, patient }) });
        $("#result").innerHTML = decisionView(result);
        refreshReviewCount();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

function answerBars(question, a) {
  return `<h3>${esc(question)} <span class="muted mono">${esc(a.provider.variant)}${a.provider.model ? ` · ${esc(a.provider.model)}` : ""}${a.value !== undefined ? ` · expected score ${num(a.value, 2)}` : ""}</span></h3>
    <div class="bars">${Object.entries(a.probabilities).sort((x, y) => y[1] - x[1]).map(([label, p]) =>
      `<span>${esc(label)}</span><div class="bar ${label === a.label ? "chosen" : ""}"><span style="width:${(p * 100).toFixed(1)}%"></span></div><span class="mono">${pct(p)}</span>`).join("")}</div>
    ${a.evidence.length ? `<div class="chips mt-2">${a.evidence.map((e) => `<span class="chip">${esc(e)}</span>`).join("")}</div>` : ""}`;
}

function decisionView(r) {
  const derive = derivedPrimary();
  const primary = r.answers[r.primary];
  const answers = derive && primary
    ? `<h3>Criteria</h3>${criteriaTable(r.answers)}${answerBars(r.primary, primary)}
      <details><summary>Probabilities for every criterion</summary>
        ${Object.entries(r.answers).filter(([q]) => q !== r.primary).map(([q, a]) => answerBars(q, a)).join("")}</details>`
    : Object.entries(r.answers).map(([q, a]) => answerBars(q, a)).join("");
  return `<div class="panel">
    <div class="row">${outcomeBadge(r.outcome)} ${priorityBadge(r.priority)}
      <span>→ <b>${esc(r.recommendation ?? "no recommendation")}</b></span>
      ${r.confidence !== null ? `<span class="muted">confidence ${pct(r.confidence)}</span>` : ""}
      <span class="grow"></span><span class="muted">${num(r.latency_ms, 2)} ms</span>
      <a class="mono" href="#audit" title="${esc(r.id)}">${esc(r.id.slice(0, 8))}</a></div>
    ${r.fallback ? `<p class="mt-3"><b>Fallback:</b> ${esc(r.fallback)}</p>` : ""}
    <ul class="reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    ${answers}
    <h3>Was this right?</h3>${feedbackControls(r.id)}
  </div>`;
}

// ---- feedback (thumbs up / down) -------------------------------------------------------------

function feedbackControls(decisionId, compact = false) {
  const note = compact ? "" : `<input class="fb-note" maxlength="1000" placeholder="What was wrong? (optional)" aria-label="What was wrong? (optional)">`;
  return `<div class="fb ${compact ? "compact" : ""}" data-decision="${esc(decisionId)}">${note}
    <button type="button" data-rating="up" title="Right">${icon("thumbs-up")}<span class="${compact ? "sr-only" : ""}">Right</span></button>
    <button type="button" data-rating="down" title="Wrong">${icon("thumbs-down")}<span class="${compact ? "sr-only" : ""}">Wrong</span></button></div>`;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-rating]");
  if (!button) return;
  event.stopPropagation();
  const box = button.closest(".fb");
  const note = box.querySelector(".fb-note");
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await api(`/api/audit/${box.dataset.decision}/feedback`, {
      method: "POST",
      body: JSON.stringify({
        rating: button.dataset.rating,
        reporter: storageGet("oh.reviewer", "") || null,
        note: note && note.value.trim() ? note.value.trim() : null,
      }),
    });
    box.innerHTML = `<span class="fb-done">${button.dataset.rating === "up" ? `${icon("thumbs-up")} Recorded as right` : `${icon("thumbs-down")} Recorded as wrong`}</span>`;
    toast(button.dataset.rating === "up" ? "Thanks for the feedback" : "Thumbs down recorded. It's in the Feedback list.");
  } catch (error) {
    toast(error.message);
    box.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
}, true);

// ---- theme -----------------------------------------------------------------------------------

// index.html resolves the theme before first paint; this keeps it in sync with the toggle, and with
// the system setting while "System" is chosen.
const THEME_CHOICES = ["system", "light", "dark"];
const systemDark = matchMedia("(prefers-color-scheme: dark)");
const themeChoice = () => {
  const choice = storageGet("oh.theme", "system");
  return THEME_CHOICES.includes(choice) ? choice : "system";
};

function applyTheme() {
  const choice = themeChoice();
  const dark = choice === "dark" || (choice === "system" && systemDark.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === choice)));
}

function setupTheme() {
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => {
    storageSet("oh.theme", b.dataset.themeChoice);
    applyTheme();
  }));
  systemDark.addEventListener("change", applyTheme);
  applyTheme();
}

// ---- boot ------------------------------------------------------------------------------------

async function boot() {
  setupTheme();
  const health = await api("/api/health").catch(() => null);
  try {
    state.timeFormat = timeFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
    state.timezone = health?.timezone ?? DEFAULT_TIMEZONE;
  } catch { /* an unknown zone name: keep the default */ }
  state.packs = await api("/api/packs");
  const select = $("#pack-select");
  if (!state.packs.length) {
    $("#view").innerHTML = `<div class="panel empty">No Decision Packs found on the search path.</div>`;
    return;
  }
  select.innerHTML = state.packs.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} ${esc(p.version)}</option>`).join("");
  const saved = storageGet("oh.pack", state.packs[0].id);
  const initial = state.packs.some((p) => p.id === saved) ? saved : state.packs[0].id;
  select.value = initial;
  await loadPack(initial);
  select.addEventListener("change", async () => {
    storageSet("oh.pack", select.value);
    await loadPack(select.value);
    render();
  });
  window.addEventListener("hashchange", () => render({ navigated: true }));
  render();
}

boot().catch((error) => {
  $("#view").innerHTML = `<div class="panel">Could not reach the OpenHealth API: ${esc(error.message)}</div>`;
});

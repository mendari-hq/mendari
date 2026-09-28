"use strict";

// ---- utilities -------------------------------------------------------------------------------

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (v, d = 1) => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(d)}%`);
const num = (v, d = 3) => (v === null || v === undefined ? "n/a" : Number(v).toFixed(d));
const time = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const OUTCOMES = ["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK"];
const outcomeBadge = (o) => `<span class="badge o-${esc(o)}">${esc(o.replace("_", " "))}</span>`;
const priorityBadge = (p) => `<span class="badge p-${esc(p)}">${esc(p)}</span>`;

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

const state = { packs: [], packId: null, detail: null };
const packPath = () => `/api/packs/${state.packId}`;
const views = {};

async function loadPack(packId) {
  state.packId = packId;
  state.detail = await api(packPath());
}

async function render() {
  const view = (location.hash || "#overview").slice(1);
  document.querySelectorAll(".nav a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  const main = $("#view");
  main.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    await (views[view] || views.overview)(main);
  } catch (error) {
    main.innerHTML = `<div class="panel">Could not load this view: ${esc(error.message)}</div>`;
  }
  refreshReviewCount();
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
      ${tile("Automated", pct(autoShare), `${stats.outcomes.AUTO} AUTO`)}
      ${tile("Waiting for review", stats.pending_reviews, "HUMAN_REVIEW + ABSTAIN")}
      ${tile("Human override rate", pct(stats.human_override_rate), `${stats.reviews_resolved} reviews`)}
      ${tile("Observed accuracy", pct(stats.observed_accuracy), "vs. final human labels")}
      ${tile("Latency p95", stats.latency_ms.p95 === null ? "n/a" : `${num(stats.latency_ms.p95, 2)} ms`)}
      ${tile("Cost / decision", stats.cost_usd.per_decision === null ? "n/a" : `$${num(stats.cost_usd.per_decision, 5)}`)}
      ${tile("Audit chain", chain.ok ? "verified" : "BROKEN", `${chain.events} events`)}
    </div>
    <div class="panel">
      <h3 style="margin-top:0">Outcome mix</h3>
      ${stackBar(stats.outcomes, total)}
    </div>
    <div class="panel">
      <h3 style="margin-top:0">Daily volume</h3>
      ${stats.daily.length ? `<div class="table-wrap"><table>
        <tr><th>Date</th>${OUTCOMES.map((o) => `<th>${esc(o)}</th>`).join("")}<th style="width:40%">Mix</th></tr>
        ${stats.daily.map((day) => {
          const n = OUTCOMES.reduce((sum, o) => sum + day[o], 0);
          return `<tr><td>${esc(day.date)}</td>${OUTCOMES.map((o) => `<td>${day[o]}</td>`).join("")}<td>${stackBar(day, n, false)}</td></tr>`;
        }).join("")}
      </table></div>` : `<div class="empty">No decisions yet. Use <a href="#try">Try it</a>, the API, or the sandbox worker.</div>`}
    </div>`;
};

function tile(label, value, sub = "", kind = "") {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${kind}">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
}

function stackBar(counts, total, legend = true) {
  const parts = OUTCOMES.map((o) => (total ? `<span class="bg-${o}" style="width:${(100 * counts[o]) / total}%" title="${o}: ${counts[o]}"></span>` : "")).join("");
  const keys = legend ? `<div class="legend">${OUTCOMES.map((o) => `<span><i class="bg-${o}"></i>${esc(o)} ${counts[o]} (${pct(total ? counts[o] / total : null)})</span>`).join("")}</div>` : "";
  return `<div class="stack">${parts}</div>${keys}`;
}

// ---- review queue ----------------------------------------------------------------------------

views.review = async (main) => {
  const items = await api(`/api/review?pack_id=${encodeURIComponent(state.packId)}&limit=100`);
  const labels = state.detail.labels;
  main.innerHTML = `
    <h1>Review queue</h1>
    <p class="muted">HUMAN_REVIEW and ABSTAIN decisions, stat first. Choosing a label other than the recommendation records a human override.</p>
    <div class="row panel">
      <label for="reviewer">Reviewer</label>
      <input id="reviewer" value="${esc(storageGet("oh.reviewer", "sandbox-nurse"))}" autocomplete="name">
      <span class="muted">${items.length} pending</span>
    </div>
    <div id="queue" style="margin-top:14px">
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
  const message = (record.input && record.input.message) || {};
  const recommendation = item.recommendation;
  return `<article class="panel review-card" data-id="${esc(item.id)}">
    <div class="row">
      ${priorityBadge(item.priority)} ${outcomeBadge(item.outcome)}
      <span class="muted">${esc(time(item.created_at))}</span>
      <span class="grow"></span>
      <span class="mono muted">${esc(item.id.slice(0, 8))}</span>
    </div>
    ${message.subject ? `<div style="margin-top:8px"><b>${esc(message.subject)}</b></div>` : ""}
    <div class="message">${esc(message.body ?? "(input not stored: audit.store_input = hash)")}</div>
    <div>Recommendation: <b>${esc(recommendation ?? "none (abstained)")}</b>
      ${item.confidence !== null ? `<span class="muted">· confidence ${pct(item.confidence)}</span>` : ""}</div>
    <ul class="reasons">${record.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    <div class="labels">${labels.map((label) =>
      `<button data-label="${esc(label)}" class="${label === recommendation ? "recommended" : ""}">${label === recommendation ? "✓ " : ""}${esc(label)}</button>`).join("")}
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
      <select id="outcome-filter"><option value="">All</option>${OUTCOMES.map((o) => `<option ${o === outcome ? "selected" : ""}>${o}</option>`).join("")}</select>
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
      <tr><th>Time</th><th>Outcome</th><th>Recommendation</th><th>Priority</th><th>Confidence</th><th>Latency</th><th>Review</th><th>ID</th></tr>
      ${rows.map((r) => `<tr class="clickable" data-id="${esc(r.id)}">
        <td>${esc(time(r.created_at))}</td><td>${outcomeBadge(r.outcome)}</td><td>${esc(r.recommendation ?? "-")}</td>
        <td>${priorityBadge(r.priority)}</td><td>${pct(r.confidence)}</td><td>${num(r.latency_ms, 2)} ms</td>
        <td>${esc(r.review_status === "resolved" ? `${r.final_label}${r.human_override ? " (override)" : ""}` : r.review_status.replace("_", " "))}</td>
        <td class="mono">${esc(r.id.slice(0, 8))}</td></tr>`).join("")}
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
    <div class="row"><h3 style="margin:0">Decision ${esc(id)}</h3><span class="grow"></span>${outcomeBadge(record.outcome)}</div>
    <h3>Events (hash chain)</h3>
    <table><tr><th>#</th><th>Event</th><th>At</th><th>Hash</th><th>Payload</th></tr>
      ${record.events.map((e) => `<tr><td>${e.seq}</td><td>${esc(e.event)}</td><td>${esc(time(e.created_at))}</td>
        <td class="mono">${esc(e.hash.slice(0, 16))}…</td><td class="mono">${esc(JSON.stringify(e.payload))}</td></tr>`).join("")}
    </table>
    <h3>Record</h3><pre class="json">${esc(JSON.stringify(record.record, null, 2))}</pre></div>`;
  $("#audit-detail").scrollIntoView({ behavior: "smooth" });
}

// ---- evaluations -----------------------------------------------------------------------------

views.evals = async (main) => {
  const reports = await api(`/api/evals?pack_id=${encodeURIComponent(state.packId)}`);
  const datasets = state.detail.datasets;
  main.innerHTML = `
    <h1>Evaluations</h1>
    <p class="muted">Run the pack on a labelled dataset. Anything other than the benchmark dataset is compared against the published benchmark (local validation).</p>
    <div class="row panel">
      <label for="dataset">Dataset</label>
      <select id="dataset">${datasets.map((d) => `<option ${d === "local-site-example" ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
      <button class="primary" id="run-eval">Run evaluation</button>
      <span class="grow"></span>
      <select id="history"><option value="">Published benchmark</option>${reports.map((r, i) =>
        `<option value="${i}">${esc(time(r.created_at))} · ${esc(r.dataset.name)} (n=${r.dataset.n})</option>`).join("")}</select>
    </div>
    <div id="report"></div>`;
  const show = (report) => ($("#report").innerHTML = report ? reportView(report) : `<div class="panel empty">No benchmark published for this pack.</div>`);
  show(state.detail.benchmark);
  $("#history").addEventListener("change", (e) => show(e.target.value === "" ? state.detail.benchmark : reports[Number(e.target.value)]));
  $("#run-eval").addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Running…";
    try {
      show(await api(`${packPath()}/evals`, { method: "POST", body: JSON.stringify({ dataset: $("#dataset").value }) }));
    } catch (error) {
      toast(error.message);
    } finally {
      e.target.disabled = false;
      e.target.textContent = "Run evaluation";
    }
  });
};

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
    <div class="tiles" style="margin-top:14px">
      ${tile(`${r.primary} accuracy`, pct(q.accuracy), `macro-F1 ${num(q.macro_f1)}`)}
      ${tile("Calibration (ECE)", num(q.calibration.ece), `Brier ${num(q.brier)}`)}
      ${tile("Automated", pct(d.outcome_rates.AUTO), `accuracy ${pct(d.automation.accuracy)}`)}
      ${tile("False automation rate", pct(d.automation.false_automation_rate), `of ${d.automation.n} AUTO`)}
      ${tile("Abstention", pct(d.abstention_rate))}
      ${tile("Expected override rate", pct(d.review.expected_override_rate), `of ${d.review.n} reviewed`)}
      ${critical}
      ${tile("Latency p50 / p95", `${num(r.latency_ms.p50, 2)} / ${num(r.latency_ms.p95, 2)}`, "ms")}
      ${tile("Cost / decision", r.cost.per_decision_usd === null ? "unknown" : `$${num(r.cost.per_decision_usd, 5)}`)}
    </div>
    <div class="grid-2">
      <div class="panel"><h3 style="margin-top:0">Reliability</h3>${reliability(q.calibration.bins)}</div>
      <div class="panel"><h3 style="margin-top:0">Threshold sweep</h3><table>
        <tr><th>Auto at ≥</th><th>Coverage</th><th>Accuracy</th><th>Errors</th></tr>
        ${r.threshold_sweep.map((t) => `<tr><td>${t.threshold.toFixed(2)}</td><td>${pct(t.coverage)}</td><td>${pct(t.accuracy)}</td><td>${t.errors}</td></tr>`).join("")}
      </table></div>
    </div>
    <div class="panel table-wrap"><h3 style="margin-top:0">Per label</h3><table>
      <tr><th>Label</th><th>Precision</th><th>Recall</th><th>F1</th><th>n</th></tr>
      ${Object.entries(q.per_label).map(([label, m]) => `<tr><td>${esc(label)}</td><td>${pct(m.precision)}</td><td>${pct(m.recall)}</td><td>${num(m.f1)}</td><td>${m.support}</td></tr>`).join("")}
    </table></div>
    <div class="panel table-wrap"><h3 style="margin-top:0">Subgroups</h3><table>
      <tr><th>Subgroup</th><th>n</th><th>Accuracy</th><th>AUTO</th><th>Abstain</th><th>False automation</th></tr>
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
    ${c.findings.length ? `<div style="margin-top:8px">${c.findings.map((f) => `<div class="finding"><b>${esc(f.subject)}</b>:
      ${f.kind === "critical" ? `${f.local} of ${f.n}` : f.kind === "calibration" ? `${num(f.baseline)} → ${num(f.local)}` : `${pct(f.baseline)} → ${pct(f.local)} <span class="muted">(n=${f.n})</span>`}</div>`).join("")}</div>` : ""}
  </div>`;
}

function reliability(bins) {
  const size = 240, pad = 28, span = size - 2 * pad;
  const x = (v) => pad + v * span, y = (v) => size - pad - v * span;
  const total = bins.reduce((s, b) => s + b.n, 0) || 1;
  const dots = bins.map((b) => `<circle cx="${x(b.confidence)}" cy="${y(b.accuracy)}" r="${3 + 9 * Math.sqrt(b.n / total)}"
      fill="var(--accent)" fill-opacity="0.75"><title>${b.low.toFixed(1)}–${b.high.toFixed(1)}: n=${b.n}, confidence ${pct(b.confidence)}, accuracy ${pct(b.accuracy)}</title></circle>`).join("");
  return `<svg viewBox="0 0 ${size} ${size}" width="100%" style="max-width:320px" role="img" aria-label="Reliability diagram">
    <rect x="${pad}" y="${pad}" width="${span}" height="${span}" fill="none" stroke="var(--border)"/>
    <line x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}" stroke="var(--muted)" stroke-dasharray="4 4"/>
    ${dots}
    <text x="${pad}" y="${size - 8}">0</text><text x="${size - pad - 6}" y="${size - 8}">1</text>
    <text x="${size / 2 - 26}" y="${size - 8}">confidence</text>
    <text x="4" y="${pad + 4}">1</text><text x="4" y="${size / 2}">acc</text>
  </svg><p class="muted">Dots on the dashed line are perfectly calibrated; dot size is bin volume.</p>`;
}

// ---- decision card ---------------------------------------------------------------------------

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
      ${tile("AUTO threshold", p.thresholds.auto.toFixed(2), `review ≥ ${p.thresholds.review.toFixed(2)}`)}
      ${tile("CDS Hooks service", d.cds_service ?? "none", m.cds_hooks ? `hook: ${m.cds_hooks.hook}` : "", "ident")}
    </div>
    <div class="panel"><b>Digest</b> <span class="mono">${esc(d.digest)}</span></div>
    <div class="grid-2">
      <div class="panel"><h3 style="margin-top:0">Intended use</h3><p>${esc(m.card.intended_use)}</p>
        <h3>Users</h3><ul>${m.card.intended_users.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>
        <h3>Population</h3><p>${esc(m.card.patient_population)}</p>
        <h3>Why risk level ${m.risk.level}</h3><p class="muted">${esc(m.risk.rationale)}</p></div>
      <div class="panel"><h3 style="margin-top:0">Out of scope</h3><ul>${m.card.out_of_scope.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>
        <h3>Known failure modes</h3><ul>${m.card.known_failure_modes.map((u) => `<li>${esc(u)}</li>`).join("")}</ul></div>
    </div>
    ${Object.entries(m.questions).map(([key, q]) => `<div class="panel"><h3 style="margin-top:0">Question <code>${esc(key)}</code>${key === p.primary ? " (primary)" : ""}</h3>
      <p class="muted">${esc(q.instructions)}</p><table>${Object.entries(q.criteria).map(([label, text]) => `<tr><td><code>${esc(label)}</code></td><td>${esc(text)}</td></tr>`).join("")}</table></div>`).join("")}
    <div class="panel"><h3 style="margin-top:0">Policy</h3><ul>
      ${p.rules.map((r) => `<li><b>${esc(r.name)}</b>: ${esc(r.when.question)} ∈ [${r.when.labels.map(esc).join(", ")}] with p ≥ ${r.when.min_confidence}
        ⇒ ${outcomeBadge(r.outcome)} ${r.recommendation ? `→ ${esc(r.recommendation)}` : ""} ${r.priority ? priorityBadge(r.priority) : ""}<br><span class="muted">${esc(r.reason)}</span></li>`).join("")}
      ${Object.entries(p.labels).map(([label, l]) => `<li><code>${esc(label)}</code>: ${l.allow_auto ? `AUTO at ≥ ${l.auto_threshold ?? p.thresholds.auto}` : "never automated"}</li>`).join("")}
      <li>Fallback: ${esc(p.fallback)}</li></ul></div>
    <div class="panel table-wrap"><h3 style="margin-top:0">Providers</h3><table>
      <tr><th>Slot</th><th>Answers</th><th>Active</th><th>Variants</th></tr>
      ${Object.entries(m.providers).map(([slot, s]) => `<tr><td>${esc(slot)}</td><td>${s.answers.map(esc).join(", ")}</td>
        <td><b>${esc(d.active_providers[slot].variant)}</b> <span class="muted mono">${esc(d.active_providers[slot].model ?? "")}</span></td>
        <td>${Object.entries(s.variants).map(([name, v]) => `${esc(name)} (${esc(v.type)})`).join(", ")}</td></tr>`).join("")}
    </table></div>
    <div class="grid-2">
      <div class="panel"><h3 style="margin-top:0">Input schema</h3><pre class="json">${esc(JSON.stringify(d.input_schema, null, 2))}</pre></div>
      <div class="panel"><h3 style="margin-top:0">Output schema</h3><pre class="json">${esc(JSON.stringify(d.output_schema, null, 2))}</pre></div>
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

views.try = async (main) => {
  main.innerHTML = `
    <h1>Try it</h1>
    <p class="muted">Synthetic messages only. Decisions made here are recorded in the audit trail and review queue.</p>
    <div class="panel">
      <div class="chips" style="margin-bottom:10px">${SAMPLES.map((s, i) => `<button data-sample="${i}">${esc(s.label)}</button>`).join("")}</div>
      <label for="subject">Subject</label><input id="subject" style="width:100%;margin:4px 0 10px">
      <label for="body">Message</label><textarea id="body"></textarea>
      <div class="row" style="margin-top:10px">
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
  $("#decide").addEventListener("click", async () => {
    const message = { body: $("#body").value, channel: $("#channel").value };
    if ($("#subject").value.trim()) message.subject = $("#subject").value.trim();
    const patient = { preferred_language: $("#language").value };
    if ($("#age").value) patient.age_band = $("#age").value;
    try {
      const result = await api(`${packPath()}/decide`, { method: "POST", body: JSON.stringify({ message, patient }) });
      $("#result").innerHTML = decisionView(result);
      refreshReviewCount();
    } catch (error) {
      toast(error.message);
    }
  });
};

function decisionView(r) {
  return `<div class="panel">
    <div class="row">${outcomeBadge(r.outcome)} ${priorityBadge(r.priority)}
      <span>→ <b>${esc(r.recommendation ?? "no recommendation")}</b></span>
      ${r.confidence !== null ? `<span class="muted">confidence ${pct(r.confidence)}</span>` : ""}
      <span class="grow"></span><span class="muted">${num(r.latency_ms, 2)} ms</span>
      <a class="mono" href="#audit" title="${esc(r.id)}">${esc(r.id.slice(0, 8))}</a></div>
    ${r.fallback ? `<p style="margin-top:10px"><b>Fallback:</b> ${esc(r.fallback)}</p>` : ""}
    <ul class="reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    ${Object.entries(r.answers).map(([question, a]) => `<h3>${esc(question)} <span class="muted mono">${esc(a.provider.variant)} · ${esc(a.provider.model ?? "")}</span></h3>
      <div class="bars">${Object.entries(a.probabilities).sort((x, y) => y[1] - x[1]).map(([label, p]) =>
        `<span>${esc(label)}</span><div class="bar ${label === a.label ? "chosen" : ""}"><span style="width:${(p * 100).toFixed(1)}%"></span></div><span class="mono">${pct(p)}</span>`).join("")}</div>
      ${a.evidence.length ? `<div class="chips" style="margin-top:8px">${a.evidence.map((e) => `<span class="chip">${esc(e)}</span>`).join("")}</div>` : ""}`).join("")}
  </div>`;
}

// ---- boot ------------------------------------------------------------------------------------

async function boot() {
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
  window.addEventListener("hashchange", render);
  render();
}

boot().catch((error) => {
  $("#view").innerHTML = `<div class="panel">Could not reach the OpenHealth API: ${esc(error.message)}</div>`;
});

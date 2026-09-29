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
const shortTime = (iso) => (iso ? state.shortFormat.format(new Date(iso)) : "");
const timeFormatFor = (timeZone) => new Intl.DateTimeFormat("en-US", {
  timeZone, year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
});
const shortFormatFor = (timeZone) => new Intl.DateTimeFormat("en-US", {
  timeZone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
});
// "ET" for US Eastern: right in both EDT and EST, unlike the seasonal short name.
function zoneShort() {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: state.timezone, timeZoneName: "shortGeneric" }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? state.timezone;
  } catch {
    return state.timezone;
  }
}
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

const state = {
  packs: [], packId: null, detail: null, view: null,
  timezone: DEFAULT_TIMEZONE, timeFormat: timeFormatFor(DEFAULT_TIMEZONE), shortFormat: shortFormatFor(DEFAULT_TIMEZONE),
};
const packPath = () => `/api/packs/${state.packId}`;
const views = {};

async function loadPack(packId) {
  state.packId = packId;
  state.detail = await api(packPath());
}

// Routes are "#view" or "#view/param": "#audit/<decision id>" opens a record, "#evals/compare"
// picks an evaluation view.
function route() {
  const [name = "overview", ...params] = (location.hash || "#overview").slice(1).split("/");
  return { view: views[name] ? name : "overview", params: params.filter(Boolean).map(decodeURIComponent) };
}

// A route change inside the current view (opening a record, switching a tab) updates that view in
// place, keeping its scroll and focus; anything else renders the new view.
function onHashChange() {
  const { view, params } = route();
  if (view === state.view && views[view].update) return views[view].update(params);
  return render({ navigated: true });
}

// `navigated`: the user moved to another view, so it starts at the top with its heading focused
// (screen readers then announce the new view once, instead of reading the whole page).
async function render({ navigated = false } = {}) {
  const { view, params } = route();
  state.view = view;
  closeDrawer({ restoreFocus: false });
  document.querySelectorAll(".nav a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  const main = $("#view");
  main.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    await views[view](main, params);
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

// Sorting, sticky edges and chart drawing all attach after a view puts its HTML in the page.
const wire = (root) => {
  wireTables(root);
  wireCharts(root);
};

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

// ---- drawer ------------------------------------------------------------------------------------

// One dialog at a time, beside the page (full screen on phones). The page behind is inert, Tab stays
// inside, Escape or the close button closes it, and focus returns to what opened it.
let drawer = null;

function showDrawer({ title, body, opener, onClose }) {
  closeDrawer({ restoreFocus: false });
  const root = document.createElement("div");
  root.className = "drawer-root";
  root.innerHTML = `<div class="drawer-backdrop" data-close></div>
    <section class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <header class="drawer-head"><h2 id="drawer-title" tabindex="-1">${esc(title)}</h2>
        <button type="button" class="icon-button" data-close aria-label="Close">${icon("x")}</button></header>
      <div class="drawer-body">${body}</div>
    </section>`;
  document.body.append(root);
  document.querySelectorAll(".topbar, .shell").forEach((el) => (el.inert = true));
  document.body.classList.add("drawer-open");
  drawer = { root, opener, onClose };
  root.addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) closeDrawer();
  });
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDrawer();
    } else if (event.key === "Tab") {
      keepFocusInside(event, root.querySelector(".drawer"));
    }
  });
  root.querySelector("#drawer-title").focus();
}

function closeDrawer({ restoreFocus = true } = {}) {
  if (!drawer) return;
  const { root, opener, onClose } = drawer;
  drawer = null;
  root.remove();
  document.querySelectorAll(".topbar, .shell").forEach((el) => (el.inert = false));
  document.body.classList.remove("drawer-open");
  if (restoreFocus && opener?.isConnected) opener.focus();
  onClose?.();
}

function keepFocusInside(event, container) {
  const focusable = [...container.querySelectorAll("a[href], button:not([disabled]), input, select, textarea, summary")]
    .filter((el) => el.getClientRects().length);
  if (!focusable.length) return;
  const [first, last] = [focusable[0], focusable[focusable.length - 1]];
  const inside = container.contains(document.activeElement) && document.activeElement.id !== "drawer-title";
  if (event.shiftKey && (!inside || document.activeElement === first)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
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
      ${stats.daily.length ? dailyChart(stats.daily) : `<div class="empty">No decisions yet. Use <a href="#try">Try it</a>, the API, or the sandbox worker.</div>`}
    </div>`;
  wire(main);
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

// Decisions per day, by outcome: the last 30 days in the deployment's time zone. Days without
// decisions are real zeros, but the chart starts at the first decision, so a new deployment's
// history is never drawn as a run of empty days.
const CHART_DAYS = 30;

const addDays = (day, n) => {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, date + n)).toISOString().slice(0, 10);
};
const dayLabel = (day, long = false) => new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC", month: "short", day: "numeric", ...(long ? { weekday: "short", year: "numeric" } : {}),
}).format(new Date(`${day}T00:00:00Z`));
function localToday() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: state.timezone, year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(new Date()).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function dailyChart(daily) {
  const byDay = new Map(daily.map((d) => [d.date, d]));
  const last = [localToday(), daily[daily.length - 1].date].sort().pop();
  const first = [daily[0].date, addDays(last, 1 - CHART_DAYS)].sort().pop();
  const categories = [];
  for (let day = first; day <= last; day = addDays(day, 1)) {
    categories.push({ axis: dayLabel(day), title: dayLabel(day, true), values: Object.fromEntries(OUTCOMES.map((o) => [o, byDay.get(day)?.[o] ?? 0])) });
  }
  const totalOf = (values) => OUTCOMES.reduce((sum, o) => sum + values[o], 0);
  const peak = categories.reduce((best, cat) => (totalOf(cat.values) > totalOf(best.values) ? cat : best));
  return chartHtml({
    id: "daily",
    type: "columns",
    ariaLabel: `Decisions per day, ${categories[0].axis} to ${categories[categories.length - 1].axis}, peak ${totalOf(peak.values)} on ${peak.axis}`,
    categories,
    series: OUTCOMES.map((o) => ({ key: o, label: outcomeText(o), swatch: o })),
    legend: OUTCOMES.map((o) => ({ swatch: `bg-${o}`, label: outcomeText(o) })),
    note: daily[0].date < first
      ? `The last ${CHART_DAYS} days. The table lists every day with decisions.`
      : `Since the first decision on ${esc(categories[0].axis)}. Nothing is drawn before it.`,
    data: {
      rows: [...daily].reverse(),
      limitHeight: true,
      columns: [
        { key: "date", label: "Date", sort: (d) => d.date, cell: (d) => esc(dayLabel(d.date, true)) },
        ...OUTCOMES.map((o) => ({ key: o, label: `<span title="${o}">${esc(outcomeText(o))}</span>`, numeric: true, sort: (d) => d[o], cell: (d) => String(d[o]) })),
        { key: "total", label: "Total", numeric: true, sort: (d) => totalOf(d), cell: (d) => String(totalOf(d)) },
      ],
    },
  });
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

const PRIORITY_RANK = { stat: 0, asap: 1, urgent: 2, routine: 3 };
const EVENT_TEXT = {
  "decision.created": "Decision recorded",
  "review.resolved": "Review resolved",
  "outcome.recorded": "Outcome recorded",
  "feedback.recorded": "Feedback recorded",
};

views.audit = async (main, params) => {
  const outcome = storageGet("oh.audit.outcome", "");
  main.innerHTML = `
    <h1>Audit trail</h1>
    <p class="muted">Every decision is recorded once; reviews, outcomes and feedback are appended as hash-chained events. Open a decision to see its record.</p>
    <div class="row panel">
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
    toast(error.message);
    location.hash = "#audit";
    return;
  }
  const r = found.record;
  showDrawer({
    title: `Decision ${id.slice(0, 8)}`,
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
      ${inputView(r.input)}
      ${r.fallback ? `<p class="mt-3"><b>Fallback:</b> ${esc(r.fallback)}</p>` : ""}
      ${r.reasons?.length ? `<ul class="reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${derivedPrimary() ? `<h3>Criteria</h3>${criteriaTable(r.answers)}` : ""}
      <h3>Was this right?</h3>${feedbackControls(id)}
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

// ---- evaluations -----------------------------------------------------------------------------

const evalsState = { reports: [] };
const EVAL_TABS = [
  ["compare", "Provider comparison"],
  ["report", "Benchmark report"],
  ["run", "Run on a dataset"],
  ["history", "History"],
];
const evalTabs = () => EVAL_TABS.filter(([key]) => key !== "compare" || state.detail.leaderboard);

views.evals = async (main, params) => {
  evalsState.reports = await api(`/api/evals?pack_id=${encodeURIComponent(state.packId)}`);
  main.innerHTML = `
    <h1>Evaluations</h1>
    <p class="muted">How the pack performs on labelled data: configurations compared on the same records, the published benchmark, and local validation on your own datasets.</p>
    <nav class="segmented tabs" aria-label="Evaluation views">${evalTabs().map(([key, label]) =>
      `<a href="#evals/${key}" data-tab="${key}">${esc(label)}</a>`).join("")}</nav>
    <div id="evals-body"></div>`;
  showEvalsTab(params[0]);
};
views.evals.update = (params) => showEvalsTab(params[0]);

function showEvalsTab(requested) {
  const tabs = evalTabs();
  const tab = tabs.some(([key]) => key === requested) ? requested : tabs[0][0];
  document.querySelectorAll(".tabs a[data-tab]").forEach((a) => {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  const body = $("#evals-body");
  const empty = (text) => `<div class="panel empty">${text}</div>`;
  if (tab === "compare") {
    body.innerHTML = leaderboardView(state.detail.leaderboard);
    wireLeaderboard(body, state.detail.leaderboard);
  } else if (tab === "report") {
    body.innerHTML = state.detail.benchmark ? reportView(state.detail.benchmark) : empty("No benchmark published for this pack.");
  } else if (tab === "run") {
    const datasets = state.detail.datasets;
    const preselected = datasets.find((d) => d.includes("local-site")) ?? datasets[0];
    body.innerHTML = `
      <div class="row panel">
        <label for="dataset">Dataset</label>
        <select id="dataset">${datasets.map((d) => `<option ${d === preselected ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
        <button class="primary" id="run-eval">Run evaluation</button>
      </div>
      <p class="muted mt-3">Anything other than the benchmark dataset is compared against the published benchmark (local validation).</p>
      <div id="run-report">${empty("Choose a dataset and run it; the report appears here.")}</div>`;
    $("#run-eval").addEventListener("click", (e) =>
      whileBusy(e.currentTarget, "Running…", async () => {
        try {
          const report = await api(`${packPath()}/evals`, { method: "POST", body: JSON.stringify({ dataset: $("#dataset").value }) });
          evalsState.reports.unshift(report);
          $("#run-report").innerHTML = reportView(report);
          wire($("#run-report"));
        } catch (error) {
          toast(error.message);
        }
      }),
    );
  } else {
    const reports = evalsState.reports;
    body.innerHTML = reports.length ? `
      <div class="row panel">
        <label for="history">Report</label>
        <select id="history">${reports.map((r, i) => `<option value="${i}">${esc(time(r.created_at))} · ${esc(r.dataset.name)} (n=${r.dataset.n})</option>`).join("")}</select>
      </div>
      <div id="history-report">${reportView(reports[0])}</div>` : empty("No evaluations have been run on this deployment yet. Use “Run on a dataset”.");
    $("#history")?.addEventListener("change", (e) => {
      $("#history-report").innerHTML = reportView(reports[Number(e.target.value)]);
      wire($("#history-report"));
    });
  }
  wire(body);
}

// ---- leaderboard -----------------------------------------------------------------------------

// Plain words for variant names ("rules_and_llm" -> "Rules + LLM"); the raw configuration stays in
// the row's tooltip.
const VARIANT_WORDS = { rules: "Rules", llm: "LLM", jev: "Jev", naive_bayes: "Naive Bayes", cascade: "Cascade", vote: "Vote", criteria: "Criteria", dosing: "Dosing" };
const variantText = (variant) => variant.split("_and_").map((part) => VARIANT_WORDS[part] ?? part.replaceAll("_", " ")).join(" + ");

// The remote models behind a provider ("rules:rules@1a2b + llm:anthropic/claude-sonnet-5" ->
// ["anthropic/claude-sonnet-5"]); local fingerprints ("rules@1a2b") are not a deployment choice.
const remoteModels = (model) => (model ?? "").split(" + ")
  .map((part) => (part.includes(":") ? part.slice(part.indexOf(":") + 1) : part))
  .filter((m) => m && !m.includes("@"));

function modelText(id) {
  const name = id.split("/").pop();
  const claude = name.match(/^claude-([a-z]+)-(\d+(?:\.\d+)*)$/);
  if (claude) return `Claude ${claude[1][0].toUpperCase()}${claude[1].slice(1)} ${claude[2]}`;
  const jev = name.match(/^jev-(.+)$/);
  return jev ? `Jev ${jev[1]}` : name;
}

const sameProvider = (a, b) => a?.variant === b?.variant && remoteModels(a?.model).join() === remoteModels(b?.model).join();
const isDeployed = (row) => Object.keys(row.providers).every((slot) => sameProvider(row.providers[slot], state.detail.active_providers[slot]));

// Safety first: the worst detection rate across the critical labels, then accuracy, then cost.
const safetyScore = (row) => Math.min(...row.critical.filter((c) => c.n).map((c) => c.detected / c.n), 1);
const safetyFirst = (a, b) => safetyScore(b) - safetyScore(a) || (b.accuracy ?? 0) - (a.accuracy ?? 0) || a.cost_per_decision_usd - b.cost_per_decision_usd;

function boardDatasets(board) {
  const datasets = [...new Set(board.rows.map((r) => r.dataset))];
  const saved = storageGet(`oh.board.${state.packId}`, "");
  const holdout = datasets.find((d) => d.endsWith("_holdout"));
  const benchmark = state.detail.manifest.evaluation?.benchmark_dataset;
  const current = datasets.includes(saved) ? saved : holdout ?? (datasets.includes(benchmark) ? benchmark : datasets[0]);
  return { datasets, current };
}

function leaderboardView(board) {
  const { datasets, current } = boardDatasets(board);
  const n = (d) => Math.max(...board.rows.filter((r) => r.dataset === d).map((r) => r.n));
  return `<div class="panel">
    <h2>Provider comparison</h2>
    <p class="muted">Every configuration ran on the same records (updated ${esc(time(board.updated_at))}). Ranked safety-first: the share of critical cases caught, then accuracy, then cost. Sort by any column.</p>
    <div class="choices" role="group" aria-label="Dataset">${datasets.map((d) =>
      `<button type="button" data-board-dataset="${esc(d)}" aria-pressed="${d === current}">${esc(d)} <span class="muted">n=${n(d)}</span></button>`).join("")}</div>
    <div id="board" class="mt-3">${boardTables(board, current)}</div>
  </div>`;
}

function wireLeaderboard(root, board) {
  root.querySelectorAll("[data-board-dataset]").forEach((button) => button.addEventListener("click", () => {
    const dataset = button.dataset.boardDataset;
    storageSet(`oh.board.${state.packId}`, dataset);
    root.querySelectorAll("[data-board-dataset]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    $("#board", root).innerHTML = boardTables(board, dataset);
    wire($("#board", root));
  }));
}

function boardTables(board, dataset) {
  const rows = board.rows.filter((r) => r.dataset === dataset);
  const n = Math.max(...rows.map((r) => r.n));
  const same = (value) => rows.length > 1 && new Set(rows.map((r) => JSON.stringify(value(r)))).size === 1;
  const slots = [...new Set(rows.flatMap((r) => Object.keys(r.providers)))];
  const varying = slots.filter((slot) => !same((r) => [r.providers[slot]?.variant, remoteModels(r.providers[slot]?.model)]));
  const constantSlots = slots.filter((slot) => !varying.includes(slot));
  const ms = (v) => (v < 10 ? v.toFixed(2) : Math.round(v).toLocaleString());
  const metrics = [
    { key: "accuracy", label: "Accuracy", value: (r) => r.accuracy, text: (v) => pct(v) },
    { key: "macro_f1", label: "Macro-F1", value: (r) => r.macro_f1, text: (v) => num(v) },
    { key: "ece", label: "ECE", value: (r) => r.ece, text: (v) => num(v), firstDir: "ascending" },
    { key: "auto_rate", label: "Automated", value: (r) => r.auto_rate, text: (v) => pct(v) },
    { key: "false_automation", label: "False automation", value: (r) => r.false_automation_rate, text: (v) => pct(v), firstDir: "ascending" },
    { key: "p50", label: "p50, ms", value: (r) => r.latency_p50_ms, text: ms, firstDir: "ascending" },
    { key: "cost", label: "Cost / decision, $", value: (r) => r.cost_per_decision_usd, text: (v) => v.toFixed(5), firstDir: "ascending" },
    { key: "errors", label: "Errors", value: (r) => r.errors, text: (v) => String(v), firstDir: "ascending" },
  ];
  const constantMetrics = metrics.filter((m) => same(m.value));
  const criticalKeys = [...new Set(rows.flatMap((r) => r.critical.map((c) => `${c.question}=${c.label}`)))];

  const configCell = (r) => {
    const parts = varying.map((slot) => {
      const p = r.providers[slot];
      const models = remoteModels(p?.model).map((m) => `<span class="chip" title="${esc(m)}">${esc(modelText(m))}</span>`).join(" ");
      return `<span>${varying.length > 1 ? `<span class="muted">${esc(slot)}:</span> ` : ""}${esc(variantText(p?.variant ?? "–"))}</span> ${models}`;
    });
    return `<div class="config" title="${esc(r.config)}">${parts.join("<br>")}${isDeployed(r) ? ` <span class="badge deployed">Deployed</span>` : ""}</div>`;
  };
  const criticalCell = (r, key) => {
    const c = r.critical.find((x) => `${x.question}=${x.label}` === key);
    if (!c || !c.n) return `<span class="muted">–</span>`;
    const missed = c.detected < c.n;
    return `<span class="${missed ? "miss" : ""}">${c.detected}/${c.n}</span>
      <span class="meter${missed ? " miss" : ""}" aria-hidden="true"><span style="width:${(100 * c.detected) / c.n}%"></span></span>
      ${c.auto_missed ? `<span class="miss">${c.auto_missed} automated</span>` : ""}`;
  };
  const columns = [
    { key: "config", label: "Configuration", className: "config-cell", sort: (r) => r.config, cell: configCell },
    ...criticalKeys.map((key) => ({
      key: `critical:${key}`, label: `${key.split("=").map((part) => `<code>${esc(part)}</code>`).join(" = ")} caught`, numeric: true, className: "critical",
      sort: (r) => { const c = r.critical.find((x) => `${x.question}=${x.label}` === key); return c && c.n ? c.detected / c.n : null; },
      cell: (r) => criticalCell(r, key),
    })),
    ...metrics.filter((m) => !constantMetrics.includes(m)).map((m) => ({
      key: m.key, label: m.label, numeric: true, firstDir: m.firstDir, sort: m.value,
      cell: (r) => (m.value(r) === null || m.value(r) === undefined ? "n/a" : m.text(m.value(r))),
    })),
  ];
  // What every row shares is said once, above the table, instead of repeated in cells.
  const notes = [
    ...constantSlots.map((slot) => `all rows: ${esc(slot)} = ${esc(variantText(rows[0].providers[slot].variant))}`),
    ...constantMetrics.map((m) => `${esc(m.label)} ${m.value(rows[0]) === null ? "n/a" : m.text(m.value(rows[0]))} in every configuration`),
  ];
  return (notes.length ? `<p class="table-note">${notes.join(" · ")}</p>` : "") + tableHtml({
    id: `board-${dataset}`,
    caption: `<b>${esc(dataset)}</b> · n=${n}`,
    rows,
    rank: true,
    stickyFirst: true,
    defaultCompare: safetyFirst,
    rowClass: (r) => (isDeployed(r) ? "deployed" : ""),
    columns,
  }) + costChart(rows, varying, dataset) + otherQuestions(rows, varying);
}

// A configuration in plain words ("Rules + Jev Claude Sonnet 5"), for the slots that differ.
const configName = (row, varying) => varying.map((slot) => [variantText(row.providers[slot]?.variant ?? "–"), ...remoteModels(row.providers[slot]?.model).map(modelText)].join(" ")).join(", ") || row.config;

const costTick = (v) => (v === 0 ? "$0" : `$${v.toFixed(Math.max(0, -Math.round(Math.log10(v))))}`);

// The trade-off behind a deployment choice: what a decision costs against how often it is right.
// Diamonds are configurations that missed a critical case; the deployed one is ringed.
function costChart(rows, varying, dataset) {
  const usable = rows.filter((r) => [r.accuracy, r.cost_per_decision_usd].every((v) => v !== null && v !== undefined));
  if (usable.length < 2 || new Set(usable.map((r) => r.cost_per_decision_usd)).size < 2) return "";
  const missed = (r) => safetyScore(r) < 1;
  const accuracies = usable.map((r) => r.accuracy);
  const costs = usable.map((r) => r.cost_per_decision_usd);
  return `<h3 class="chart-title">Cost against accuracy</h3>${chartHtml({
    id: `cost-${dataset}`,
    type: "scatter",
    ariaLabel: `Cost against accuracy for ${usable.length} configurations on ${dataset}: accuracy from ${pct(Math.min(...accuracies))} to ${pct(Math.max(...accuracies))}, cost from $${Math.min(...costs).toFixed(5)} to $${Math.max(...costs).toFixed(5)} per decision.`,
    x: { kind: "log", title: "Cost per decision (log scale)", format: costTick },
    y: { kind: "rate", title: "Accuracy" },
    points: usable.map((r) => ({
      x: r.cost_per_decision_usd, y: r.accuracy, ring: isDeployed(r), shape: missed(r) ? "diamond" : "circle", tone: missed(r) ? "danger" : "c1",
      title: `${configName(r, varying)}${isDeployed(r) ? " (deployed)" : ""}`,
      rows: [["Accuracy", pct(r.accuracy)], ["Cost / decision", `$${r.cost_per_decision_usd.toFixed(5)}`], ...(missed(r) ? [["Critical cases", "some missed"]] : [])],
    })),
    legend: [
      ...(usable.some((r) => !missed(r)) ? [{ swatch: "dot bg-c1", label: usable.some(missed) ? "Caught every critical case" : "Configuration" }] : []),
      ...(usable.some(missed) ? [{ swatch: "diamond bg-danger", label: "Missed a critical case" }] : []),
      ...(usable.some(isDeployed) ? [{ swatch: "ring", label: "Deployed" }] : []),
    ],
    data: {
      rows: usable,
      defaultCompare: (a, b) => a.cost_per_decision_usd - b.cost_per_decision_usd || b.accuracy - a.accuracy,
      columns: [
        { key: "config", label: "Configuration", sort: (r) => configName(r, varying), cell: (r) => esc(configName(r, varying)) },
        { key: "cost", label: "Cost / decision, $", numeric: true, sort: (r) => r.cost_per_decision_usd, cell: (r) => r.cost_per_decision_usd.toFixed(5) },
        { key: "accuracy", label: "Accuracy", numeric: true, sort: (r) => r.accuracy, cell: (r) => pct(r.accuracy) },
      ],
    },
  })}`;
}

// Accuracy of the non-primary questions that differ between configurations, weak cells marked.
function otherQuestions(rows, varying) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r.questions ?? {})))].sort();
  const changing = keys.filter((k) => new Set(rows.map((r) => JSON.stringify(r.questions?.[k] ?? null))).size > 1);
  if (!changing.length) return "";
  const shade = (acc) => (acc === null || acc === undefined ? "" : acc < 0.8 ? "cell-bad" : acc < 0.9 ? "cell-warn" : "");
  const name = (r) => configName(r, varying);
  return `<details class="mt-3"><summary>Per-check accuracy</summary>
    <p class="muted mt-2">Accuracy of each check; ECE in the tooltip. Amber below 90 %, red below 80 %.</p>
    ${tableHtml({
      id: `checks-${rows[0].dataset}`,
      caption: "",
      rows: [...rows].sort(safetyFirst),
      stickyFirst: true,
      columns: [
        { key: "config", label: "Configuration", className: "config-cell", sort: name, cell: (r) => `<span title="${esc(r.config)}">${esc(name(r))}</span>` },
        ...changing.map((k) => ({
          key: k, label: `<code>${esc(k)}</code>`, numeric: true, sort: (r) => r.questions?.[k]?.accuracy,
          cell: (r) => {
            const m = r.questions?.[k];
            return m ? `<span class="${shade(m.accuracy)}" title="ECE ${num(m.ece)}">${pct(m.accuracy)}</span>` : "n/a";
          },
        })),
      ],
    })}</details>`;
}

// ---- evaluation report -------------------------------------------------------------------------

function reportView(r) {
  const q = r.questions[r.primary];
  const d = r.decisions;
  const critical = d.critical.map((c) => tile(`Critical ${c.question}=${c.label}`, `${c.detected}/${c.n}`, `${c.auto_missed} automated`)).join("");
  const id = `${r.id ?? r.created_at}`.replace(/[^a-zA-Z0-9-]/g, "");
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
      <div class="panel"><h3>Reliability</h3>${reliabilityChart(q.calibration, id)}</div>
      <div class="panel"><h3>Threshold sweep</h3>${sweepChart(r.threshold_sweep, r.policy?.thresholds?.auto, id)}</div>
    </div>
    <div class="panel"><h3>Per label</h3>${tableHtml({
      id: `labels-${id}`, caption: "",
      rows: Object.entries(q.per_label).map(([label, m]) => ({ label, ...m })),
      columns: [
        { key: "label", label: "Label", sort: (m) => m.label, cell: (m) => esc(m.label) },
        { key: "precision", label: "Precision", numeric: true, sort: (m) => m.precision, cell: (m) => pct(m.precision) },
        { key: "recall", label: "Recall", numeric: true, sort: (m) => m.recall, cell: (m) => pct(m.recall) },
        { key: "f1", label: "F1", numeric: true, sort: (m) => m.f1, cell: (m) => num(m.f1) },
        { key: "support", label: "n", numeric: true, sort: (m) => m.support, cell: (m) => String(m.support) },
      ],
    })}${confusionView(q.confusion, id)}</div>
    <div class="panel"><h3>Subgroups</h3>
      <p class="muted">Grouped by attribute, lowest accuracy first. Groups under 20 cases are marked: their numbers move a lot with each case.</p>
      ${subgroupsView(r.subgroups, id)}</div>`;
}

const SMALL_N = 20;

function subgroupsView(subgroups, id) {
  return Object.entries(subgroups).map(([attr, values]) => {
    const rows = Object.entries(values).map(([value, m]) => ({ value, ...m }))
      .sort((a, b) => (a.accuracy ?? 1) - (b.accuracy ?? 1));
    const worst = rows[0];
    return `<details class="group"><summary><b>${esc(attr)}</b>
      <span class="muted">${rows.length} groups · lowest ${pct(worst.accuracy)} (${esc(worst.value)}, n=${worst.n})</span></summary>
      ${tableHtml({
        id: `sub-${id}-${attr}`, caption: "", rows,
        columns: [
          { key: "value", label: esc(attr), sort: (m) => m.value, cell: (m) => `${esc(m.value)}${m.n < SMALL_N ? ` <span class="badge o-ABSTAIN">small n</span>` : ""}` },
          { key: "n", label: "n", numeric: true, sort: (m) => m.n, cell: (m) => String(m.n) },
          {
            key: "accuracy", label: "Accuracy", numeric: true, sort: (m) => m.accuracy, firstDir: "ascending",
            cell: (m) => `${pct(m.accuracy)}${m.accuracy === null || m.accuracy === undefined ? "" : `<span class="meter" aria-hidden="true"><span style="width:${100 * m.accuracy}%"></span></span>`}`,
          },
          { key: "auto", label: "Automated", numeric: true, sort: (m) => m.auto_rate, cell: (m) => pct(m.auto_rate) },
          { key: "abstain", label: "Abstained", numeric: true, sort: (m) => m.abstention_rate, cell: (m) => pct(m.abstention_rate) },
          { key: "false_auto", label: "False automation", numeric: true, sort: (m) => m.false_automation_rate, cell: (m) => pct(m.false_automation_rate) },
        ],
      })}</details>`;
  }).join("");
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

// Calibration: each dot is a confidence bin; on the diagonal, confidence equals accuracy.
function reliabilityChart(calibration, id) {
  const bins = calibration.bins;
  if (!bins.length) return `<div class="empty">No labelled decisions.</div>`;
  const cases = bins.reduce((sum, b) => sum + b.n, 0);
  const overconfident = bins.filter((b) => b.accuracy < b.confidence).length;
  const gap = (b) => (b.accuracy - b.confidence) * 100;
  return chartHtml({
    id: `reliability-${id}`,
    type: "scatter",
    ariaLabel: `Reliability diagram: ${bins.length} confidence bins over ${cases} cases, expected calibration error ${num(calibration.ece)}. ${overconfident} of ${bins.length} bins sit below the diagonal, where the pack is more confident than accurate.`,
    square: true,
    diagonal: true,
    sizeBy: true,
    x: { kind: "rate", title: "Mean confidence in the bin", from: 0 },
    y: { kind: "rate", title: "Accuracy in the bin", from: 0 },
    points: bins.map((b) => ({
      x: b.confidence, y: b.accuracy, weight: b.n,
      title: `Confidence ${b.low.toFixed(1)}–${b.high.toFixed(1)}`,
      rows: [["Cases", String(b.n)], ["Mean confidence", pct(b.confidence)], ["Accuracy", pct(b.accuracy)]],
    })),
    legend: [{ swatch: "dot bg-c1", label: "Confidence bin (area = cases)" }, { swatch: "diag", label: "Perfectly calibrated" }],
    data: {
      rows: bins,
      columns: [
        { key: "bin", label: "Confidence", cell: (b) => `${b.low.toFixed(1)}–${b.high.toFixed(1)}` },
        { key: "n", label: "n", numeric: true, cell: (b) => String(b.n) },
        { key: "confidence", label: "Mean confidence", numeric: true, cell: (b) => pct(b.confidence) },
        { key: "accuracy", label: "Accuracy", numeric: true, cell: (b) => pct(b.accuracy) },
        { key: "gap", label: "Gap, pts", numeric: true, cell: (b) => `${gap(b) > 0 ? "+" : ""}${gap(b).toFixed(1)}` },
      ],
    },
  });
}

// Coverage and accuracy as the automation threshold rises, with the pack's own threshold marked.
function sweepChart(sweep, autoThreshold, id) {
  if (!sweep?.length) return `<div class="empty">No labelled decisions.</div>`;
  const [first, last] = [sweep[0], sweep[sweep.length - 1]];
  const lastAccuracy = [...sweep].reverse().find((t) => t.accuracy !== null && t.accuracy !== undefined)?.accuracy;
  const threshold = (t) => t.toFixed(2);
  return chartHtml({
    id: `sweep-${id}`,
    type: "lines",
    ariaLabel: `Threshold sweep: raising the automation threshold from ${threshold(first.threshold)} to ${threshold(last.threshold)} moves coverage from ${pct(first.coverage)} to ${pct(last.coverage)} and accuracy from ${pct(first.accuracy)} to ${pct(lastAccuracy)}.${typeof autoThreshold === "number" ? ` The pack automates at ${threshold(autoThreshold)} and above.` : ""}`,
    x: { title: "Automate when confidence is at least", format: threshold },
    series: [{ key: "coverage", label: "Coverage", swatch: "c1" }, { key: "accuracy", label: "Accuracy", swatch: "c2", dashed: true }],
    legend: [{ swatch: "line c1", label: "Coverage (share automated)" }, { swatch: "line c2 dashed", label: "Accuracy of those" }],
    marker: typeof autoThreshold === "number" ? { x: autoThreshold, label: `Pack automates at ${threshold(autoThreshold)}` } : null,
    points: sweep.map((t) => ({ x: t.threshold, title: `Automate at ≥ ${threshold(t.threshold)}`, values: { coverage: t.coverage, accuracy: t.accuracy }, extra: [["Errors", String(t.errors)]] })),
    data: {
      rows: sweep,
      columns: [
        { key: "threshold", label: "Automate at ≥", numeric: true, cell: (t) => threshold(t.threshold) },
        { key: "coverage", label: "Coverage", numeric: true, cell: (t) => pct(t.coverage) },
        { key: "accuracy", label: "Accuracy", numeric: true, cell: (t) => pct(t.accuracy) },
        { key: "errors", label: "Errors", numeric: true, cell: (t) => String(t.errors) },
      ],
    },
  });
}

// Where the errors cluster: true label down the side, the pack's answer across the top. Only
// mistakes are shaded (amber, red from a tenth of the row), so they stand out from the diagonal.
function confusionView(confusion, id) {
  const truths = Object.keys(confusion ?? {});
  if (!truths.length) return "";
  const answers = [...new Set([...truths, ...truths.flatMap((t) => Object.keys(confusion[t]))])];
  const rows = truths.map((truth) => ({ truth, counts: confusion[truth], total: Object.values(confusion[truth]).reduce((a, b) => a + b, 0) }));
  return `<details class="mt-3"><summary>Confusion matrix</summary>
    ${tableHtml({
      id: `confusion-${id}`,
      caption: "Rows are the true label, columns the pack's answer. Mistakes are shaded; hover for the share of the row.",
      rows,
      stickyFirst: true,
      columns: [
        { key: "truth", label: "True label", sort: (row) => row.truth, cell: (row) => `<b>${esc(row.truth)}</b> <span class="muted">n=${row.total}</span>` },
        ...answers.map((answer) => ({
          key: `answer:${answer}`, label: esc(answer), numeric: true,
          cell: (row) => {
            const n = row.counts[answer] ?? 0;
            const share = row.total ? n / row.total : 0;
            const shade = answer === row.truth || !n ? "" : share >= 0.1 ? "cell-bad" : "cell-warn";
            return `<span class="${shade}" title="${esc(pct(share))} of ${esc(row.truth)}">${n}</span>`;
          },
        })),
      ],
    })}</details>`;
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
        wire($("#result"));
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
        wire($("#result"));
        refreshReviewCount();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

// What choosing this label leads to, from the pack's own policy: a block rule is danger, a review
// rule is warning. Labels the policy doesn't act on get no tone.
function labelTone(question, label) {
  const rules = state.detail.manifest.policy.rules.filter((rule) => rule.when.question === question && rule.when.labels?.includes(label));
  if (rules.some((rule) => rule.outcome === "BLOCK")) return "block";
  return rules.some((rule) => rule.outcome === "HUMAN_REVIEW") ? "review" : "";
}

function answerBars(id, question, a) {
  const rows = Object.entries(a.probabilities).sort((x, y) => y[1] - x[1])
    .map(([label, value]) => ({ label, value, chosen: label === a.label, tone: labelTone(question, label) }));
  return `<h3>${esc(question)} <span class="muted mono">${esc(a.provider.variant)}${a.provider.model ? ` · ${esc(a.provider.model)}` : ""}${a.value !== undefined ? ` · expected score ${num(a.value, 2)}` : ""}</span></h3>
    ${chartHtml({
      id: `answer-${id}-${question}`,
      type: "bars",
      ariaLabel: `${question}: ${rows.map((r) => `${r.label} ${pct(r.value)}`).join(", ")}. Chosen: ${a.label}.`,
      rows,
    })}
    ${a.evidence.length ? `<div class="chips mt-2">${a.evidence.map((e) => `<span class="chip">${esc(e)}</span>`).join("")}</div>` : ""}`;
}

function decisionView(r) {
  const derive = derivedPrimary();
  const primary = r.answers[r.primary];
  const answers = derive && primary
    ? `<h3>Criteria</h3>${criteriaTable(r.answers)}${answerBars(r.id, r.primary, primary)}
      <details><summary>Probabilities for every criterion</summary>
        ${Object.entries(r.answers).filter(([q]) => q !== r.primary).map(([q, a]) => answerBars(r.id, q, a)).join("")}</details>`
    : Object.entries(r.answers).map(([q, a]) => answerBars(r.id, q, a)).join("");
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
    state.shortFormat = shortFormatFor(health?.timezone ?? DEFAULT_TIMEZONE);
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
  window.addEventListener("hashchange", onHashChange);
  render();
}

boot().catch((error) => {
  $("#view").innerHTML = `<div class="panel">Could not reach the OpenHealth API: ${esc(error.message)}</div>`;
});

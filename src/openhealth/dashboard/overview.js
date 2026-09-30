// Overview: what is deployed, the numbers people act on, outcome mix and decisions per day.

import { chartHtml } from "./charts.js";
import { providerWords } from "./pack.js";
import { views, wire } from "./router.js";
import { api, esc, icon, money, num, OUTCOMES, outcomeText, pct, slotText, stat, state, zoneName } from "./ui.js";

views.overview = async (main) => {
  const [stats, chain] = await Promise.all([
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
    api("/api/audit/verify"),
  ]);
  const d = state.detail;
  const total = stats.decisions;
  main.innerHTML = `
    <h1>${esc(d.name)}</h1>
    ${packHeader(d)}
    ${total ? `
      ${primaryStats(stats)}
      ${secondaryStats(stats, chain)}
      <div class="panel">
        <h2>Outcome mix</h2>
        ${stackBar(stats.outcomes, total)}
      </div>
      <div class="panel">
        <h2>Decisions per day <span class="muted">· days in ${esc(zoneName())}</span></h2>
        ${dailyChart(stats.daily)}
      </div>` : `
      <div class="panel empty-state">
        <h2>No decisions yet</h2>
        <p class="muted">Decisions appear here as soon as this pack makes one: from Try it, the API, a CDS Hooks call or the sandbox worker.</p>
        <p class="row"><a class="button primary" href="#try">Try it</a><a class="button" href="/docs">API docs</a></p>
      </div>`}`;
  wire(main);
};

function packHeader(d) {
  const deployed = Object.entries(d.active_providers);
  return `<div class="pack-header">
    <p class="pack-risk"><span class="badge risk">Risk level ${d.risk.level}</span> <span class="muted">${esc(d.risk.category)}</span></p>
    <p class="deployed"><span class="muted">Deployed</span> ${deployed.map(([slot, p]) =>
      `<span class="deployed-slot">${esc(slotText(slot))}: <b>${providerWords(p)}</b></span>`).join(`<span class="muted" aria-hidden="true">·</span>`)}</p>
    <p class="mono muted">${esc(d.id)} v${esc(d.version)} · ${deployed.map(([slot, p]) => `${esc(slot)}=${esc(p.variant)}`).join(" · ")}</p>
  </div>`;
}

// The four numbers people act on: how much is automated, what waits for a person, and whether
// people agree with the pack (overrides, observed accuracy), each with what it is out of.
function primaryStats(stats) {
  const total = stats.decisions;
  const waiting = stats.pending_reviews;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  return `<div class="stats-primary">
    ${stat({ label: "Automated", value: pct(stats.outcomes.AUTO / total), foot: `${stats.outcomes.AUTO} of ${plural(total, "decision")}` })}
    ${stat({
      label: "Waiting for review", value: String(waiting), tone: waiting ? "warning" : "",
      foot: waiting ? `<a href="#review">Open the review queue ${icon("arrow-right")}</a>` : "Nothing waiting",
    })}
    ${stat({ label: "Human override rate", value: pct(stats.human_override_rate), foot: stats.reviews_resolved ? `of ${plural(stats.reviews_resolved, "review")}` : "No reviews yet" })}
    ${stat({ label: "Observed accuracy", value: pct(stats.observed_accuracy), foot: stats.labelled ? `against ${plural(stats.labelled, "human label")}` : "No human labels yet" })}
  </div>`;
}

// Operational context, one row: volume, speed, cost, the audit chain and feedback.
function secondaryStats(stats, chain) {
  const f = stats.feedback;
  return `<div class="stats-secondary">
    ${stat({ size: "small", label: "Decisions", value: stats.decisions.toLocaleString() })}
    ${stat({ size: "small", label: "Latency p95", value: stats.latency_ms.p95 === null ? "n/a" : `${num(stats.latency_ms.p95, 2)} ms` })}
    ${stat({ size: "small", label: "Cost / decision", value: money(stats.cost_usd.per_decision) })}
    ${stat({
      size: "small", label: "Audit chain", html: true, tone: chain.ok ? "" : "danger",
      value: chain.ok ? `${icon("shield-check")} Verified` : `${icon("shield-x")} Broken at event ${esc(chain.broken_at)}`,
      foot: `${chain.events.toLocaleString()} events`,
    })}
    ${stat({
      size: "small", label: "Feedback", html: true,
      value: `${icon("thumbs-up")}<span class="sr-only">Right:</span> ${f.up} <span class="muted" aria-hidden="true">·</span> ${icon("thumbs-down")}<span class="sr-only">Wrong:</span> ${f.down}`,
      foot: `<a href="#feedback">${f.down_on_auto} wrong on automated</a>`,
    })}
  </div>`;
}

function stackBar(counts, total) {
  const parts = OUTCOMES.map((o) => (total ? `<span class="bg-${o}" style="width:${(100 * counts[o]) / total}%" title="${outcomeText(o)}: ${counts[o]}"></span>` : "")).join("");
  const keys = `<div class="legend">${OUTCOMES.map((o) => `<span><i class="bg-${o}"></i>${esc(outcomeText(o))} ${counts[o]} (${pct(total ? counts[o] / total : null)})</span>`).join("")}</div>`;
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
  const span = `${categories[0].axis} to ${categories[categories.length - 1].axis}`;
  return chartHtml({
    id: "daily",
    type: "columns",
    ariaLabel: totalOf(peak.values) ? `Decisions per day, ${span}, peak ${totalOf(peak.values)} on ${peak.axis}` : `Decisions per day, ${span}: none`,
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

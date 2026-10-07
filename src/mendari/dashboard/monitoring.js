// SPDX-License-Identifier: Apache-2.0
// Monitoring: the deployed pack over time against its benchmark: alerts first, then the last
// 7 days (observed accuracy, overrides, confidence drift, automated decisions rated wrong), then
// week by week. Computed by the server (evals/monitoring.py) for this organization.

import { chartHtml, wireCharts } from "./charts.js";
import { communityNotice } from "./pack.js";
import { render, views } from "./router.js";
import { tableHtml } from "./table.js";
import { $, api, emptyState, esc, figureHtml, icon, money, share, shown, stat, state, storageGet, storageSet } from "./ui.js";

const PERIODS = [30, 90, 180];
// A calendar day of the period ("2026-09-30" -> "Sep 30"); the server counts weeks in UTC days.
const day = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const ALERT_TITLES = {
  accuracy: "Accuracy below the benchmark",
  drift: "Confidence drift",
  automated_wrong: "Automated and rated wrong",
  overrides: "Overrides doubled",
};

views.monitoring = async (main) => {
  const days = Number(storageGet("oh.monitoring.days", "30"));
  const report = await api(`/api/monitoring?pack_id=${encodeURIComponent(state.packId)}&days=${days}`);
  const week = report.last_7_days;
  const bench = report.benchmark;
  main.innerHTML = `
    <h1>Monitoring</h1>
    <p class="muted">${esc(state.detail.manifest.name)} in use, against its benchmark. Alerts need enough evidence to fire.</p>
    ${communityNotice(state.detail)}
    <div class="row toolbar">
      <label for="monitoring-days">Period</label>
      <select id="monitoring-days">${PERIODS.map((d) => `<option value="${d}" ${d === days ? "selected" : ""}>${d} days</option>`).join("")}</select>
    </div>
    ${alerts(report.alerts)}
    <div class="stats-primary">
      ${stat({ label: "Observed accuracy, 7 days", value: figureHtml(week.figures.accuracy), html: true,
        foot: `of the labelled decisions · benchmark ${esc(shown(bench.figures.accuracy))}`,
        tone: report.alerts.some((a) => a.kind === "accuracy") ? "danger" : "" })}
      ${stat({ label: "Human overrides, 7 days", value: figureHtml(week.figures.overrides), html: true, foot: "of the reviewed decisions" })}
      ${stat({ label: "Confidence drift (PSI)", value: week.drift_psi === null ? "n/a" : week.drift_psi.toFixed(2),
        foot: "0.25 or more is a real shift", tone: report.alerts.some((a) => a.kind === "drift") ? "warning" : "" })}
      ${stat({ label: "Automated, rated wrong", value: String(week.automated_rated_wrong), foot: "last 7 days",
        tone: week.automated_rated_wrong ? "danger" : "" })}
    </div>
    <h2>Week by week</h2>
    ${report.weeks.some((w) => w.decisions) ? `${trendChart(report)}${weeksTable(report.weeks)}` : emptyState({
      icon: "clock", title: `No decisions in the last ${days} days`,
      text: "Monitoring fills in as the pack decides and people review, record outcomes or rate its answers.",
      action: { href: "#try", label: "Try it" },
    })}`;
  wireCharts(main);
  $("#monitoring-days").addEventListener("change", (event) => {
    storageSet("oh.monitoring.days", event.target.value);
    render();
  });
};

function alerts(list) {
  if (!list.length) return `<p class="monitor-ok">${icon("check")} No alerts in the last 7 days.</p>`;
  return list.map((a) => `<div class="callout ${a.severity === "high" ? "danger" : "warning"}" role="status">
    ${icon("alert")} <b>${esc(ALERT_TITLES[a.kind] ?? a.kind)}:</b> ${esc(a.message)}</div>`).join("");
}

// Observed accuracy and the override rate, week by week; weeks without evidence leave a gap. Until
// two weeks have one or the other, a line has nothing to join, so a sentence says what fills it.
function trendChart(report) {
  const weeks = report.weeks;
  const known = (figure) => share(figure) !== null;
  if (weeks.filter((w) => known(w.figures.accuracy) || known(w.figures.overrides)).length < 2) {
    return `<p class="muted">The trend appears once decisions in two weeks have been reviewed, re-routed downstream or rated.</p>`;
  }
  const label = (i) => (Number.isInteger(i) && weeks[i] ? day(weeks[i].to) : "");
  const bench = report.benchmark.figures.accuracy;
  return chartHtml({
    id: "monitoring-trend",
    type: "lines",
    ariaLabel: `Observed accuracy and the human override rate for each of the last ${weeks.length} weeks.`,
    x: { title: "Week ending", format: label },
    series: [{ key: "accuracy", label: "Observed accuracy", swatch: "c1" }, { key: "overrides", label: "Overrides", swatch: "c2", dashed: true }],
    directLabels: true,
    legend: [{ swatch: "line c1", label: "Observed accuracy" }, { swatch: "line c2 dashed", label: "Overrides" }],
    note: `Observed accuracy compares the pack's recommendation with what a person or a downstream system said${bench ? `; the benchmark is ${esc(shown(bench))}` : ""}.`,
    points: weeks.map((w, i) => ({
      x: i, title: `${day(w.from)} to ${day(w.to)}`,
      values: { accuracy: share(w.figures.accuracy), overrides: share(w.figures.overrides) },
      text: { accuracy: shown(w.figures.accuracy), overrides: shown(w.figures.overrides) },
      extra: [["Decisions", String(w.decisions)]],
    })),
  });
}

function weeksTable(weeks) {
  return tableHtml({
    id: "monitoring-weeks",
    caption: `<span class="sr-only">Each week of the period, oldest first.</span>`,
    rows: weeks,
    columns: [
      { key: "week", label: "Week", sort: (w) => w.from, cell: (w) => `<span class="nowrap">${esc(day(w.from))} – ${esc(day(w.to))}</span>` },
      { key: "decisions", label: "Decisions", numeric: true, sort: (w) => w.decisions, cell: (w) => String(w.decisions) },
      { key: "automated", label: "Automated", numeric: true, sort: (w) => share(w.figures.automated), cell: (w) => figureHtml(w.figures.automated) },
      { key: "overrides", label: "Overrides", numeric: true, sort: (w) => share(w.figures.overrides), cell: (w) => figureHtml(w.figures.overrides) },
      { key: "accuracy", label: "Observed accuracy", numeric: true, sort: (w) => share(w.figures.accuracy), cell: (w) => figureHtml(w.figures.accuracy) },
      { key: "cost", label: "Cost / decision", numeric: true, sort: (w) => w.cost_per_decision, cell: (w) => money(w.cost_per_decision) },
    ],
  });
}

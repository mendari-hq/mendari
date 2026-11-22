// SPDX-License-Identifier: Apache-2.0
// Monitoring: the served version of the pack over time, against its benchmark only where that
// measured this build with the providers that run here: alerts first, then the last 7 days
// (reviewed accuracy, overrides, confidence drift, automated decisions rated wrong), then week
// by week, operational health included (provider errors, abstentions, how long 95% of decisions
// took). Computed by the server (evals/monitoring.py) for this organization.

import { chartHtml } from "./charts.js";
import { communityNotice } from "./pack.js";
import { render, views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, api, emptyState, esc, figureHtml, icon, money, share, shown, stat, state, storageGet, storageSet } from "./ui.js";

const PERIODS = [30, 90, 180];
// A calendar day of the period ("2026-09-30" -> "Sep 30"); the server counts weeks in UTC days.
const day = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
// Why the benchmark is not compared (`benchmark.reason`).
const NOT_COMPARED = {
  from_registry: "the agent is from a registry, and the benchmark it carries is its publisher's upload, never a reference",
  no_benchmark: "the agent has no benchmark",
  another_build: "the benchmark measured another build of the agent",
  other_providers: "the benchmark ran other providers or models than this deployment",
  providers_unknown: "which providers and models run here is not known",
  other_policy: "the benchmark measured the agent under other governance (an approval in force here and not there, or the reverse)",
};
const ALERT_TITLES = {
  accuracy: "Accuracy below the benchmark",
  drift: "Confidence drift",
  automated_wrong: "Automated and rated wrong",
  overrides: "Overrides doubled",
  provider_errors: "Provider errors rose",
  abstention: "Abstentions rose",
  latency: "Decisions slowed down",
};
// A decision's time: in ms under a second ("45 ms", "1.4 ms"), else in seconds ("1.5 s").
const took = (ms) => {
  if (ms === null || ms === undefined) return "n/a";
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  return ms >= 10 ? `${Math.round(ms)} ms` : `${ms.toFixed(1)} ms`;
};

views.monitoring = async (main) => {
  const storedDays = Number(storageGet("oh.monitoring.days", "30"));
  const days = PERIODS.includes(storedDays) ? storedDays : 30;
  const packId = state.packId;
  const report = await api(`/api/monitoring?pack_id=${encodeURIComponent(packId)}&days=${days}`);
  if (state.packId !== packId) return;
  const week = report.last_7_days;
  const bench = report.benchmark;
  const psi = week.drift_psi;
  const psiPct = psi === null ? 0 : Math.min(100, Math.max(0, (psi / 0.5) * 100));
  const psiGauge = psi === null ? "" : `<span class="psi-gauge${psi >= 0.25 ? " is-high" : ""}" aria-hidden="true"><span class="psi-fill" style="width:${psiPct.toFixed(1)}%"></span><span class="psi-mark"></span></span>`;
  main.innerHTML = `
    <h1>Monitoring</h1>
    <p class="muted">${esc(state.detail.manifest.name)} in use${bench.comparable ? ", against its benchmark" : ""}. Alerts need enough evidence to fire.</p>
    ${communityNotice(state.detail)}
    <div class="row toolbar">
      <label for="monitoring-days">Period</label>
      <select id="monitoring-days">${PERIODS.map((d) => `<option value="${d}" ${d === days ? "selected" : ""}>${d} days</option>`).join("")}</select>
    </div>
    ${alerts(report.alerts)}
    ${comparedNote(report)}
    <div class="stats-primary">
      ${stat({ label: "Reviewed accuracy, 7 days", value: figureHtml(week.figures.reviewed_accuracy), html: true,
        foot: bench.comparable
          ? `recommendations a reviewer kept · benchmark ${esc(shown(bench.figures.reviewed_accuracy))}`
          : "recommendations a reviewer kept",
        tone: alerted(report, "reviewed_accuracy") ? "danger" : "" })}
      ${bench.comparable && bench.automated_accuracy !== null && bench.figures.automated_accuracy ? stat({
        label: "Automated accuracy, 7 days", value: figureHtml(week.figures.automated_accuracy), html: true,
        foot: `outcomes recorded downstream · benchmark ${esc(shown(bench.figures.automated_accuracy))}`,
        tone: alerted(report, "automated_accuracy") ? "danger" : "" }) : ""}
      ${stat({ label: "Human overrides, 7 days", value: figureHtml(week.figures.overrides), html: true, foot: "of the reviewed decisions" })}
      ${stat({ label: "Confidence drift (PSI)", value: psi === null ? "n/a" : `${psi.toFixed(2)}${psiGauge}`, html: true,
        foot: bench.comparable ? "0.25 or more is a real shift" : "needs a comparable benchmark",
        tone: report.alerts.some((a) => a.kind === "drift") ? "warning" : "" })}
      ${stat({ label: "Automated, rated wrong", value: String(week.automated_rated_wrong), foot: "last 7 days",
        tone: week.automated_rated_wrong ? "danger" : "" })}
    </div>
    <h2>Week by week</h2>
    ${report.weeks.some((w) => w.decisions) ? `${trendChart(report)}${weeksTable(report.weeks)}` : emptyState({
      icon: "clock", title: `No decisions${othersOf(report) ? " of this version" : ""} in the last ${days} days`,
      text: "Monitoring fills in as the agent decides and people review, record outcomes or rate its answers.",
      action: { href: "#try", label: "Try it" },
    })}`;
  wire(main);
  $("#monitoring-days").addEventListener("change", (event) => {
    storageSet("oh.monitoring.days", event.target.value);
    render();
  });
};

// An accuracy alert of one stratum (`reviewed_accuracy`, `automated_accuracy`).
const alerted = (report, stratum) => report.alerts.some((a) => a.kind === "accuracy" && a.stratum === stratum);
const othersOf = (report) => Object.values(report.other_versions ?? {}).reduce((sum, n) => sum + n, 0);

// What the view compares: the benchmark, or why not; and the other versions' decisions it leaves
// out.
function comparedNote(report) {
  const bench = report.benchmark;
  const others = othersOf(report);
  const parts = [];
  if (!bench.comparable) {
    parts.push(`Not compared with the benchmark: ${esc(NOT_COMPARED[bench.reason] ?? bench.reason)}. Only the checks that need no benchmark run: automated decisions rated wrong, overrides that doubled, and provider errors, abstentions and latency against the week before.`);
  }
  if (others) {
    parts.push(others === 1
      ? "1 decision of another version of the agent is not counted."
      : `${others} decisions of other versions of the agent are not counted.`);
  }
  return parts.length ? `<p class="muted">${parts.join(" ")}</p>` : "";
}

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
    return `<p class="muted">The trend appears once decisions in two weeks have been reviewed or re-routed downstream.</p>`;
  }
  const label = (i) => (Number.isInteger(i) && weeks[i] ? day(weeks[i].to) : "");
  return chartHtml({
    id: "monitoring-trend",
    type: "lines",
    ariaLabel: `Observed accuracy and the human override rate for each of the last ${weeks.length} weeks.`,
    x: { title: "Week ending", format: label },
    series: [{ key: "accuracy", label: "Observed accuracy", swatch: "c1" }, { key: "overrides", label: "Overrides", swatch: "c2", dashed: true }],
    directLabels: true,
    legend: [{ swatch: "line c1", label: "Observed accuracy" }, { swatch: "line c2 dashed", label: "Overrides" }],
    note: "Observed accuracy compares the agent's recommendation with what a person or a downstream system said.",
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
      { key: "awaiting", label: "Awaiting review", numeric: true, sort: (w) => w.awaiting_review, cell: (w) => String(w.awaiting_review) },
      { key: "refused", label: "Refused inputs", numeric: true, sort: (w) => w.refused, cell: (w) => String(w.refused) },
      { key: "errors", label: "Provider errors", numeric: true, sort: (w) => share(w.figures.provider_errors), cell: (w) => figureHtml(w.figures.provider_errors) },
      { key: "abstained", label: "Abstained", numeric: true, sort: (w) => share(w.figures.abstained), cell: (w) => figureHtml(w.figures.abstained) },
      { key: "p95", label: "Time (p95)", numeric: true, sort: (w) => w.latency_ms.p95, cell: (w) => took(w.latency_ms.p95) },
      // Unknown when any decision's cost is: never an average of the known ones.
      { key: "cost", label: "Cost / decision", numeric: true, sort: (w) => w.cost_per_decision, cell: (w) => (w.cost_unknown ? "unknown" : money(w.cost_per_decision)) },
    ],
  });
}

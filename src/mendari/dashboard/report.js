// SPDX-License-Identifier: Apache-2.0
// Evaluation report: stats, reliability, threshold sweep, per label, confusion, subgroups.

import { chartHtml } from "./charts.js";
import { criticalText } from "./pack.js";
import { tableHtml } from "./table.js";
import { esc, money, num, pct, slotText, stat, time } from "./ui.js";

export function reportView(r) {
  const q = r.questions[r.primary];
  const d = r.decisions;
  // Safety first: accuracy, every critical label's catch rate (danger when anything is missed) and
  // false automation lead; calibration, rates, speed and cost follow.
  const critical = d.critical.map((c) => stat({
    label: `${criticalText(c.question, c.label)} caught`, value: `${c.detected}/${c.n}`, tone: c.detected < c.n || c.auto_missed ? "danger" : "",
    foot: c.auto_missed ? `<b>${c.auto_missed} automated</b>` : "none automated",
  })).join("");
  const small = (label, value, foot = "") => stat({ size: "small", label, value, foot: esc(foot) });
  const id = `${r.id ?? r.created_at}`.replace(/[^a-zA-Z0-9-]/g, "");
  return `
    <div class="report-meta">
      <div class="row"><b>${esc(r.dataset.name)}</b><span class="muted">n=${r.dataset.n} · ${esc(time(r.created_at))} ·
        ${Object.entries(r.providers).map(([s, p]) => `${esc(s)}=${esc(p.variant)}`).join(", ")}</span></div>
    </div>
    ${r.comparison ? comparisonView(r.comparison) : ""}
    <div class="stats-primary">
      ${stat({ label: `${slotText(r.primary)} accuracy`, value: pct(q.accuracy), foot: `macro-F1 ${num(q.macro_f1)} · n=${q.n}` })}
      ${critical}
      ${stat({
        label: "False automation", value: pct(d.automation.false_automation_rate), tone: d.automation.false_automation_rate ? "danger" : "",
        foot: d.automation.n ? `of ${d.automation.n} automated` : "nothing automated",
      })}
    </div>
    <div class="stats-secondary">
      ${small("Calibration (ECE)", num(q.calibration.ece), `Brier ${num(q.brier)}`)}
      ${small("Automated", pct(d.outcome_rates.AUTO), `accuracy ${pct(d.automation.accuracy)}`)}
      ${small("Abstention", pct(d.abstention_rate))}
      ${small("Expected override", pct(d.review.expected_override_rate), `of ${d.review.n} reviewed`)}
      ${small("Latency p50 / p95", `${num(r.latency_ms.p50, 2)} / ${num(r.latency_ms.p95, 2)} ms`)}
      ${small("Cost / decision", r.cost.per_decision_usd === null ? "unknown" : money(r.cost.per_decision_usd))}
    </div>
    <div class="grid-2">
      <div class="panel"><h2>Reliability</h2>${reliabilityChart(q.calibration, id)}</div>
      <div class="panel"><h2>Threshold sweep</h2>${sweepChart(r.threshold_sweep, r.policy?.thresholds?.auto, d.outcome_rates.AUTO === 0, id)}</div>
    </div>
    <div class="panel"><h2>Per label</h2>${tableHtml({
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
    <div class="panel"><h2>Subgroups</h2>
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

// What each automation threshold would allow: the share of cases at or above it and their accuracy,
// with the pack's threshold marked. A what-if: risk-level caps and review rules still apply, so at
// risk level 1-3 a pack may automate nothing at all.
function sweepChart(sweep, autoThreshold, nothingAutomated, id) {
  if (!sweep?.length) return `<div class="empty">No labelled decisions.</div>`;
  const [first, last] = [sweep[0], sweep[sweep.length - 1]];
  const lastAccuracy = [...sweep].reverse().find((t) => t.accuracy !== null && t.accuracy !== undefined)?.accuracy;
  const threshold = (t) => t.toFixed(2);
  return chartHtml({
    id: `sweep-${id}`,
    type: "lines",
    ariaLabel: `Threshold sweep: raising the automation threshold from ${threshold(first.threshold)} to ${threshold(last.threshold)} moves coverage from ${pct(first.coverage)} to ${pct(last.coverage)} and accuracy from ${pct(first.accuracy)} to ${pct(lastAccuracy)}.${typeof autoThreshold === "number" ? ` The pack's automation threshold is ${threshold(autoThreshold)}.` : ""}`,
    x: { title: "Automate when confidence is at least", format: threshold },
    series: [{ key: "coverage", label: "Coverage", swatch: "c1" }, { key: "accuracy", label: "Accuracy", swatch: "c2", dashed: true }],
    directLabels: true,
    legend: [{ swatch: "line c1", label: "Coverage" }, { swatch: "line c2 dashed", label: "Accuracy" }],
    marker: typeof autoThreshold === "number" ? { x: autoThreshold, label: `Automation threshold ${threshold(autoThreshold)}` } : null,
    note: `Coverage is the share of cases at or above the threshold; accuracy is of those cases.${nothingAutomated
      ? " Nothing was automated in this run: the policy sent every case to a person. The curves show what each threshold would allow." : ""}`,
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
  // "none" is the evaluation's column for cases without an answer, unless the pack has a label of that name.
  const answerLabel = (answer) => (answer === "none" && !truths.includes("none") ? `<span title="none">No answer</span>` : esc(answer));
  const rows = truths.map((truth) => ({ truth, counts: confusion[truth], total: Object.values(confusion[truth]).reduce((a, b) => a + b, 0) }));
  return `<details class="mt-3"><summary>Confusion matrix</summary>
    ${tableHtml({
      id: `confusion-${id}`,
      caption: "Rows are the true label, columns the pack's answer. Mistakes are shaded: amber, or red when they are a tenth of the row or more.",
      rows,
      stickyFirst: true,
      columns: [
        { key: "truth", label: "True label", sort: (row) => row.truth, cell: (row) => `<b>${esc(row.truth)}</b> <span class="muted">n=${row.total}</span>` },
        ...answers.map((answer) => ({
          key: `answer:${answer}`, label: answerLabel(answer), numeric: true,
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

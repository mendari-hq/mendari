// SPDX-License-Identifier: Apache-2.0
// Evaluation report: stats, reliability, threshold sweep, per label, confusion, subgroups.

import { chartHtml } from "./charts.js";
import { criticalText } from "./pack.js";
import { tableHtml } from "./table.js";
import { copyText, esc, evidenceMark, figureHtml, icon, money, num, share, shown, slotText, stat, storageGet, time } from "./ui.js";

// A report's run: its id, time and latency (mendari.eval/2), or a mendari.eval/1 report's own.
export const runOf = (r) => r.run ?? r;

// What a report says of its primary question when none of its cases is labelled for it.
const UNLABELLED = { macro_f1: null, brier: null, calibration: { ece: null, bins: [] }, per_label: {}, confusion: {} };

export function reportView(r) {
  const q = r.questions[r.primary] ?? UNLABELLED;
  const d = r.decisions;
  // Every share of cases is a figure the server worked out (mendari.figures/1): k of n with its
  // exact interval, as the card shows it. A stored report it could not be read from shows n/a.
  const f = r.figures ?? {};
  // Safety first: accuracy, every critical label's catch rate (danger when anything is missed) and
  // false automation lead; calibration, rates, speed and cost follow.
  const critical = d.critical.map((c, i) => {
    const figure = f.critical?.[i];
    return stat({
      label: `${criticalText(c.question, c.label)} caught`, html: true,
      value: figureHtml(figure?.detected), tone: c.detected < c.n || c.auto_missed ? "danger" : "",
      foot: `${c.auto_missed ? `<b>${c.auto_missed} automated</b>` : "none automated"}${evidenceMark(figure?.insufficient_evidence, f.min_positives)}`,
    });
  }).join("");
  const small = (label, value, foot = "") => stat({ size: "small", label, value, html: true, foot });
  const run = runOf(r);
  const id = `${run.id ?? run.created_at}`.replace(/[^a-zA-Z0-9-]/g, "");
  return `
    <div class="report-meta">
      <div class="row"><b>${esc(r.dataset.name)}</b><span class="muted">n=${r.dataset.n} · ${esc(time(run.created_at))} ·
        ${Object.entries(r.providers).map(([s, p]) => `${esc(s)}=${esc(p.variant)}`).join(", ")}</span></div>
    </div>
    ${r.comparison ? comparisonView(r.comparison, f.comparison, f.min_positives) : ""}
    <div class="stats-primary">
      ${stat({ label: `${slotText(r.primary)} accuracy`, value: figureHtml(f.accuracy), html: true, foot: `macro-F1 ${num(q.macro_f1)}` })}
      ${critical}
      ${stat({
        label: "False automation", value: figureHtml(f.false_automation), html: true,
        tone: (f.false_automation ? f.false_automation.k : d.automation.false_automation_rate) ? "danger" : "",
        foot: "automated with a wrong answer, of the labelled cases",
      })}
    </div>
    <div class="stats-secondary">
      ${small("Calibration (ECE)", num(q.calibration.ece), `Brier ${num(q.brier)}`)}
      ${small("Automated", figureHtml(f.outcomes?.AUTO), automatedFoot(f, d))}
      ${small("Abstention", figureHtml(f.outcomes?.ABSTAIN))}
      ${small("Expected override", figureHtml(f.expected_override), "of the reviewed cases")}
      ${small("Latency p50 / p95", `${num(run.latency_ms?.p50, 2)} / ${num(run.latency_ms?.p95, 2)} ms`)}
      ${small("Cost / decision", r.cost.per_decision_usd === null ? "unknown" : money(r.cost.per_decision_usd))}
    </div>
    <div class="grid-2">
      <div class="panel"><h2>Reliability</h2>${reliabilityChart(q.calibration, f.calibration ?? [], id)}</div>
      <div class="panel"><h2>Threshold sweep</h2>${sweepChart(r.threshold_sweep, f.sweep ?? [], r.policy?.thresholds?.auto, d.outcome_rates.AUTO === 0, id)}${thresholdSimulator(r.threshold_sweep, f.sweep ?? [], r.policy?.thresholds?.auto, id)}</div>
    </div>
    <div class="panel"><h2>Per label</h2>${tableHtml({
      id: `labels-${id}`, caption: "",
      rows: Object.entries(q.per_label).map(([label, m]) => ({ label, ...m, figures: f.per_label?.[label] ?? {} })),
      columns: [
        { key: "label", label: "Label", sort: (m) => m.label, cell: (m) => esc(m.label) },
        { key: "precision", label: "Precision", numeric: true, sort: (m) => share(m.figures.precision), cell: (m) => figureHtml(m.figures.precision) },
        { key: "recall", label: "Recall", numeric: true, sort: (m) => share(m.figures.recall), cell: (m) => figureHtml(m.figures.recall) },
        { key: "f1", label: "F1", numeric: true, sort: (m) => m.f1, cell: (m) => num(m.f1) },
        { key: "support", label: "n", numeric: true, sort: (m) => m.support, cell: (m) => String(m.support) },
      ],
    })}${confusionView(q.confusion, f.confusion ?? {}, id)}</div>
    <div class="panel"><h2>Subgroups</h2>
      <p class="muted">Grouped by attribute, lowest accuracy first. Groups under 20 cases are marked: their numbers move a lot with each case.</p>
      ${subgroupsView(r.subgroups, f.subgroups ?? {}, id)}</div>`;
}

// Of what was automated, how much was right: "nothing automated" only when nothing was.
function automatedFoot(f, d) {
  const correct = f.automated_correct;
  if (correct === null || correct === undefined) return "correct n/a";
  if (correct.n) return `correct ${esc(shown(correct))}`;
  return (f.outcomes?.AUTO?.k ?? d.outcome_counts.AUTO ?? 0) ? "correct: none of them labelled" : "nothing automated";
}

const SMALL_N = 20;

function subgroupsView(subgroups, figures, id) {
  return Object.entries(subgroups).map(([attr, values]) => {
    const rows = Object.entries(values).map(([value, m]) => ({ value, ...m, figures: figures[attr]?.[value] ?? {} }))
      .sort((a, b) => (share(a.figures.accuracy) ?? a.accuracy ?? 1) - (share(b.figures.accuracy) ?? b.accuracy ?? 1));
    const worst = rows[0];
    const lowest = worst?.figures.accuracy ? ` · lowest ${esc(shown(worst.figures.accuracy))} (${esc(worst.value)})` : "";
    return `<details class="group"><summary><b>${esc(attr)}</b>
      <span class="muted">${rows.length} groups${lowest}</span></summary>
      ${tableHtml({
        id: `sub-${id}-${attr}`, caption: "", rows,
        columns: [
          { key: "value", label: esc(attr), sort: (m) => m.value, cell: (m) => `${esc(m.value)}${m.n < SMALL_N ? ` <span class="badge o-ABSTAIN">small n</span>` : ""}` },
          { key: "n", label: "n", numeric: true, sort: (m) => m.n, cell: (m) => String(m.n) },
          {
            key: "accuracy", label: "Accuracy", numeric: true, sort: (m) => share(m.figures.accuracy), firstDir: "ascending",
            cell: (m) => {
              const part = share(m.figures.accuracy);
              return `${figureHtml(m.figures.accuracy)}${part === null ? "" : `<span class="meter" aria-hidden="true"><span style="width:${100 * part}%"></span></span>`}`;
            },
          },
          { key: "auto", label: "Automated", numeric: true, sort: (m) => share(m.figures.automation), cell: (m) => figureHtml(m.figures.automation) },
          { key: "abstain", label: "Abstained", numeric: true, sort: (m) => share(m.figures.abstention), cell: (m) => figureHtml(m.figures.abstention) },
        ],
      })}</details>`;
  }).join("");
}

// A local validation: the benchmark against this run, and where it degrades. A subgroup the
// benchmark lacks is compared with the benchmark overall, said as such.
function comparisonView(c, figures, min) {
  if (c.status === "unknown") return `<div class="panel">${esc(c.reason)}</div>`;
  const label = { ok: "No degradation", warning: "Localized degradation", degraded: "Performance degradation detected" }[c.status];
  const finding = (item, figure) => {
    if (item.kind === "critical") return `${figureHtml(figure?.automated)} positives${evidenceMark(figure?.insufficient_evidence, min)}`;
    if (item.kind === "calibration") return `${num(item.baseline)} → ${num(item.local)}`;
    return `${figure?.overall ? "benchmark overall " : ""}${figureHtml(figure?.baseline)} → ${figureHtml(figure?.local)}`;
  };
  return `<div class="panel">
    <div class="row"><span class="badge s-${esc(c.status)}">${esc(label)}</span>
      <span>Public benchmark <b>${figureHtml(figures?.baseline)}</b> → local <b>${figureHtml(figures?.local)}</b>
      (${(c.delta * 100).toFixed(1)} pts; tolerance ${(c.tolerance * 100).toFixed(1)} pts)</span></div>
    ${c.findings.length ? `<div class="mt-2">${c.findings.map((item, i) => `<div class="finding"><b>${esc(item.subject)}</b>:
      ${finding(item, figures?.findings?.[i])}</div>`).join("")}</div>` : ""}
  </div>`;
}

// Calibration: each dot is a confidence bin; on the diagonal, confidence equals accuracy.
function reliabilityChart(calibration, figures, id) {
  const bins = calibration.bins;
  if (!bins.length) return `<div class="empty">No labelled decisions.</div>`;
  const cases = bins.reduce((sum, b) => sum + b.n, 0);
  const overconfident = bins.filter((b) => b.accuracy < b.confidence).length;
  const gap = (b) => (b.accuracy - b.confidence) * 100;
  const correct = (i) => shown(figures[i]);
  return chartHtml({
    id: `reliability-${id}`,
    type: "scatter",
    ariaLabel: `Reliability diagram: ${bins.length} confidence bins over ${cases} cases, expected calibration error ${num(calibration.ece)}. ${overconfident} of ${bins.length} bins sit below the diagonal, where the agent is more confident than accurate.`,
    square: true,
    diagonal: true,
    sizeBy: true,
    x: { kind: "rate", title: "Mean confidence in the bin", from: 0 },
    y: { kind: "rate", title: "Accuracy in the bin", from: 0 },
    points: bins.map((b, i) => ({
      x: b.confidence, y: b.accuracy, weight: b.n,
      title: `Confidence ${b.low.toFixed(1)}–${b.high.toFixed(1)}`,
      rows: [["Mean confidence", num(b.confidence)], ["Correct", correct(i)]],
    })),
    legend: [{ swatch: "dot bg-c1", label: "Confidence bin (area = cases)" }, { swatch: "diag", label: "Perfectly calibrated" }],
    data: {
      rows: bins.map((b, i) => ({ ...b, index: i })),
      columns: [
        { key: "bin", label: "Confidence", cell: (b) => `${b.low.toFixed(1)}–${b.high.toFixed(1)}` },
        { key: "confidence", label: "Mean confidence", numeric: true, cell: (b) => num(b.confidence) },
        { key: "correct", label: "Correct", numeric: true, cell: (b) => figureHtml(figures[b.index]) },
        { key: "gap", label: "Gap, pts", numeric: true, cell: (b) => `${gap(b) > 0 ? "+" : ""}${gap(b).toFixed(1)}` },
      ],
    },
  });
}

// What each automation threshold would allow: how many cases are at or above it and how many of
// those are right, with the pack's threshold marked. A what-if: risk-level caps and review rules
// still apply, so at risk level 1-3 a pack may automate nothing at all.
function sweepChart(sweep, figures, autoThreshold, nothingAutomated, id) {
  if (!sweep?.length) return `<div class="empty">No labelled decisions.</div>`;
  const rows = sweep.map((t, i) => ({ ...t, automated: figures[i]?.automated ?? null, correct: figures[i]?.correct ?? null }));
  const [first, last] = [rows[0], rows[rows.length - 1]];
  const threshold = (t) => t.toFixed(2);
  return chartHtml({
    id: `sweep-${id}`,
    type: "lines",
    ariaLabel: `Threshold sweep: raising the automation threshold from ${threshold(first.threshold)} to ${threshold(last.threshold)} automates ${shown(first.automated)} cases at first and ${shown(last.automated)} at last; of those, ${shown(first.correct)} and ${shown(last.correct)} are correct.${typeof autoThreshold === "number" ? ` The agent's automation threshold is ${threshold(autoThreshold)}.` : ""}`,
    x: { title: "Automate when confidence is at least", format: threshold },
    series: [{ key: "coverage", label: "Would be automated", swatch: "c1" }, { key: "accuracy", label: "Of those, correct", swatch: "c2", dashed: true }],
    directLabels: true,
    legend: [{ swatch: "line c1", label: "Would be automated" }, { swatch: "line c2 dashed", label: "Of those, correct" }],
    marker: typeof autoThreshold === "number" ? { x: autoThreshold, label: `Automation threshold ${threshold(autoThreshold)}` } : null,
    note: `Would be automated: the cases at or above the threshold, of the scored ones; correct: of those.${nothingAutomated
      ? " Nothing was automated in this run: the policy sent every case to a person. The curves show what each threshold would allow." : ""}`,
    points: rows.map((t) => ({
      x: t.threshold, title: `Automate at ≥ ${threshold(t.threshold)}`,
      values: { coverage: share(t.automated), accuracy: share(t.correct) },
      text: { coverage: shown(t.automated), accuracy: shown(t.correct) },
    })),
    data: {
      rows,
      columns: [
        { key: "threshold", label: "Automate at ≥", numeric: true, cell: (t) => threshold(t.threshold) },
        { key: "automated", label: "Would be automated", numeric: true, cell: (t) => figureHtml(t.automated) },
        { key: "correct", label: "Of those, correct", numeric: true, cell: (t) => figureHtml(t.correct) },
      ],
    },
  });
}

// Interactive Policy Threshold "What-If" Simulator: lets clinicians and governance leads slide
// across the server's precomputed sweep points (with exact Clopper-Pearson intervals) to see
// projected automation, human review load, accuracy, and copyable YAML policy config.
const SIMULATORS = new Map();

function thresholdSimulator(sweep, figures, autoThreshold, id) {
  if (!sweep?.length || sweep.length < 2) return "";
  const rows = sweep.map((t, i) => ({
    threshold: t.threshold,
    automated: figures[i]?.automated ?? null,
    correct: figures[i]?.correct ?? null,
  }));
  const current = typeof autoThreshold === "number" ? autoThreshold : null;
  let initial = 0;
  if (current !== null) {
    let bestDiff = Infinity;
    rows.forEach((row, idx) => {
      const diff = Math.abs(row.threshold - current);
      if (diff < bestDiff) { bestDiff = diff; initial = idx; }
    });
  }
  const simId = `sim-${id}`;
  SIMULATORS.set(simId, { rows, current });
  return `<section class="simulator" data-simulator="${esc(simId)}" aria-labelledby="${esc(simId)}-title">
    <div class="row sim-head">
      <h3 id="${esc(simId)}-title">${icon("sliders")} Policy threshold simulator</h3>
      <span class="grow"></span>
      <button type="button" class="link-button" data-sim-copy>${icon("copy")} Copy policy YAML</button>
    </div>
    <div class="sim-control">
      <label for="${esc(simId)}-range">Automate when confidence is at least</label>
      <input type="range" id="${esc(simId)}-range" min="0" max="${rows.length - 1}" step="1" value="${initial}">
      <output for="${esc(simId)}-range" class="sim-value mono" data-sim-threshold>${rows[initial].threshold.toFixed(2)}</output>
    </div>
    <div class="sim-metrics" data-sim-metrics aria-live="polite"></div>
  </section>`;
}

export function wireSimulators(root) {
  // Drop entries for simulators no longer in the document so repeated navigation never leaks.
  for (const key of SIMULATORS.keys()) {
    if (!document.querySelector(`[data-simulator="${CSS.escape(key)}"]`)) SIMULATORS.delete(key);
  }
  root.querySelectorAll("[data-simulator]").forEach((section) => {
    if (section.dataset.wired) return;
    section.dataset.wired = "1";
    const entry = SIMULATORS.get(section.dataset.simulator);
    if (!entry) return;
    const range = section.querySelector('input[type="range"]');
    const out = section.querySelector("[data-sim-threshold]");
    const metrics = section.querySelector("[data-sim-metrics]");
    const copyBtn = section.querySelector("[data-sim-copy]");
    const paint = () => {
      const row = entry.rows[Number(range.value)] ?? entry.rows[0];
      const th = row.threshold.toFixed(2);
      const delta = entry.current !== null ? row.threshold - entry.current : null;
      const deltaText = delta === null ? ""
        : Math.abs(delta) < 0.001 ? " (agent default)"
          : ` (${delta > 0 ? "+" : ""}${delta.toFixed(2)} vs default)`;
      out.textContent = `${th}${deltaText}`;
      const autoN = row.automated?.n ?? 0;
      const autoK = row.automated?.k ?? 0;
      const reviewK = Math.max(0, autoN - autoK);
      const autoShare = share(row.automated);
      const storedMins = Number(storageGet("oh.roi.mins", "4"));
      const minsPerCase = Number.isFinite(storedMins) ? Math.min(60, Math.max(0.5, storedMins)) : 4;
      const hrsPer1k = autoShare !== null ? ((autoShare * 1000 * minsPerCase) / 60).toFixed(1) : "n/a";
      const wrongAuto = row.correct && row.correct.n ? row.correct.n - row.correct.k : 0;
      metrics.innerHTML = `
        <div class="sim-metric"><span class="muted">Would automate</span><b>${figureHtml(row.automated)}</b></div>
        <div class="sim-metric"><span class="muted">Sent to review</span><b>${reviewK} of ${autoN}</b></div>
        <div class="sim-metric"><span class="muted">Automated correct</span><b>${figureHtml(row.correct)}</b></div>
        <div class="sim-metric"><span class="muted">Est. clinician time</span><b>${hrsPer1k === "n/a" ? "n/a" : `${hrsPer1k} hrs / 1k cases`}</b>${wrongAuto ? `<span class="sim-warn">${wrongAuto} wrong in sweep</span>` : ""}</div>`;
    };
    range.addEventListener("input", paint);
    copyBtn?.addEventListener("click", () => {
      const row = entry.rows[Number(range.value)] ?? entry.rows[0];
      copyText(`policy:\n  thresholds:\n    auto: ${row.threshold.toFixed(2)}\n`, "Policy YAML copied");
    });
    paint();
  });
}

// Where the errors cluster: true label down the side, the pack's answer across the top. Only
// mistakes are shaded (amber, red from a tenth of the row), so they stand out from the diagonal.
function confusionView(confusion, figures, id) {
  const truths = Object.keys(confusion ?? {});
  if (!truths.length) return "";
  const answers = [...new Set([...truths, ...truths.flatMap((t) => Object.keys(confusion[t]))])];
  // "none" is the evaluation's column for cases without an answer, unless the pack has a label of that name.
  const answerLabel = (answer) => (answer === "none" && !truths.includes("none") ? `<span title="none">No answer</span>` : esc(answer));
  const rows = truths.map((truth) => ({ truth, counts: confusion[truth], total: Object.values(confusion[truth]).reduce((a, b) => a + b, 0) }));
  return `<details class="mt-3"><summary>Confusion matrix</summary>
    ${tableHtml({
      id: `confusion-${id}`,
      caption: "Rows are the true label, columns the agent's answer. Mistakes are shaded: amber, or red when they are a tenth of the row or more.",
      rows,
      stickyFirst: true,
      columns: [
        { key: "truth", label: "True label", sort: (row) => row.truth, cell: (row) => `<b>${esc(row.truth)}</b> <span class="muted">n=${row.total}</span>` },
        ...answers.map((answer) => ({
          key: `answer:${answer}`, label: answerLabel(answer), numeric: true,
          cell: (row) => {
            const n = row.counts[answer] ?? 0;
            const part = row.total ? n / row.total : 0;
            const shade = answer === row.truth || !n ? "" : part >= 0.1 ? "cell-bad" : "cell-warn";
            const title = n ? ` title="${esc(shown(figures[row.truth]?.[answer]))} of ${esc(row.truth)} answered ${esc(answer)}"` : "";
            return `<span class="${shade}"${title}>${n}</span>`;
          },
        })),
      ],
    })}</details>`;
}

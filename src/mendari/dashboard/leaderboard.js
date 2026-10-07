// SPDX-License-Identifier: Apache-2.0
// Provider comparison: the safety-first leaderboard, cost against accuracy, per-check accuracy.

import { chartHtml } from "./charts.js";
import { criticalText, providerName, remoteModels, variantText } from "./pack.js";
import { wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, esc, num, pct, slotText, state, storageGet, storageSet, time } from "./ui.js";

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

export function leaderboardView(board) {
  const { datasets, current } = boardDatasets(board);
  const n = (d) => Math.max(...board.rows.filter((r) => r.dataset === d).map((r) => r.n));
  return `<div class="panel">
    <h2>Provider comparison</h2>
    <p class="muted">Same records for every configuration (updated ${esc(time(board.updated_at))}). Ranked by critical cases caught, then accuracy, then cost.</p>
    <div class="choices" role="group" aria-label="Dataset">${datasets.map((d) =>
      `<button type="button" data-board-dataset="${esc(d)}" aria-pressed="${d === current}">${esc(d)} <span class="muted">n=${n(d)}</span></button>`).join("")}</div>
    <div id="board" class="mt-3">${boardTables(board, current)}</div>
  </div>`;
}

export function wireLeaderboard(root, board) {
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

  // A row measured on another version of the pack says so: its automation followed that policy.
  const measuredOn = (r) => (r.pack_version && r.pack_version !== state.detail.version
    ? `<br><span class="muted">measured on ${esc(r.pack_version)}</span>` : "");
  const configCell = (r) => {
    const parts = varying.map((slot) =>
      `<span>${varying.length > 1 ? `<span class="muted">${esc(slotText(slot))}:</span> ` : ""}${esc(providerName(r.providers[slot]))}</span>`);
    return `<div class="config" title="${esc(r.config)}">${parts.join("<br>")}${isDeployed(r) ? ` <span class="badge deployed">Deployed</span>` : ""}${measuredOn(r)}</div>`;
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
      key: `critical:${key}`, label: `<span title="${esc(key)}">${esc(criticalText(...key.split("=")))} caught</span>`, numeric: true, className: "critical",
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

// A configuration in plain words ("Rules + Jev 1.13.0"), for the slots that differ.
const configName = (row, varying) => varying.map((slot) =>
  `${varying.length > 1 ? `${slotText(slot)}: ` : ""}${providerName(row.providers[slot])}`).join(", ") || row.config;

const costTick = (v) => (v === 0 ? "$0" : `$${v.toFixed(Math.max(0, -Math.round(Math.log10(v))))}`);

// The trade-off behind a deployment choice: what a decision costs against how often it is right.
// Diamonds are configurations that missed a critical case; the deployed one is ringed.
function costChart(rows, varying, dataset) {
  const usable = rows.filter((r) => [r.accuracy, r.cost_per_decision_usd].every((v) => v !== null && v !== undefined));
  if (usable.length < 2 || new Set(usable.map((r) => r.cost_per_decision_usd)).size < 2) return "";
  const missed = (r) => safetyScore(r) < 1;
  const accuracies = usable.map((r) => r.accuracy);
  const costs = usable.map((r) => r.cost_per_decision_usd);
  // Named on the chart: the deployed configuration, the leaderboard's first (safety first), and every
  // one that no other beats on both cost and accuracy (the frontier worth choosing from). The rest
  // are named in their tooltips.
  const beaten = (r) => usable.some((o) => o !== r && o.cost_per_decision_usd <= r.cost_per_decision_usd && o.accuracy >= r.accuracy
    && (o.cost_per_decision_usd < r.cost_per_decision_usd || o.accuracy > r.accuracy));
  const first = [...usable].sort(safetyFirst)[0];
  const named = (r) => isDeployed(r) || r === first || !beaten(r);
  return `<h3 class="chart-title">Cost against accuracy</h3>${chartHtml({
    id: `cost-${dataset}`,
    type: "scatter",
    ariaLabel: `Cost against accuracy for ${usable.length} configurations on ${dataset}: accuracy from ${pct(Math.min(...accuracies))} to ${pct(Math.max(...accuracies))}, cost from $${Math.min(...costs).toFixed(5)} to $${Math.max(...costs).toFixed(5)} per decision.`,
    x: { kind: "log", title: "Cost per decision (log scale)", format: costTick },
    y: { kind: "rate", title: "Accuracy" },
    dodge: true,
    points: usable.map((r) => ({
      x: r.cost_per_decision_usd, y: r.accuracy, ring: isDeployed(r), shape: missed(r) ? "diamond" : "circle", tone: missed(r) ? "danger" : "c1",
      label: named(r) ? configName(r, varying) : undefined,
      title: `${configName(r, varying)}${isDeployed(r) ? " (deployed)" : ""}`,
      rows: [
        ["Accuracy", pct(r.accuracy)],
        ["Cost / decision", `$${r.cost_per_decision_usd.toFixed(5)}`],
        ...r.critical.filter((c) => c.n).map((c) => [`${criticalText(c.question, c.label)} caught`, `${c.detected}/${c.n}`]),
      ],
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

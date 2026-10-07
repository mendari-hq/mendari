// SPDX-License-Identifier: Apache-2.0
// Provider comparison: the leaderboard in the one ranking (mendari.rank/1, written by the server),
// cost against accuracy, per-check accuracy. Every share of cases is a figure the server worked
// out (mendari.figures/1); this view only formats and orders what it is given.

import { chartHtml } from "./charts.js";
import { criticalText, providerName, remoteModels, variantText } from "./pack.js";
import { wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, esc, evidenceMark, figureHtml, num, share, shown, slotText, state, storageGet, storageSet, time } from "./ui.js";

const sameProvider = (a, b) => a?.variant === b?.variant && remoteModels(a?.model).join() === remoteModels(b?.model).join();

const isDeployed = (row) => Object.keys(row.providers).every((slot) => sameProvider(row.providers[slot], state.detail.active_providers[slot]));

const criticalKey = (c) => `${c.question}=${c.label}`;

// Any critical label with a case it did not catch (a diamond on the cost chart).
const missed = (row) => row.figures.critical.some((c) => c.detected.k < c.detected.n);

// The set of records shown first: the one remembered (by its label, or by its dataset's name as
// before), a held-out set, the benchmark, or the first.
function boardGroups(board) {
  const groups = board.groups.map((group, index) => ({ ...group, index }));
  const saved = storageGet(`oh.board.${state.packId}`, "");
  const benchmark = state.detail.manifest.evaluation?.benchmark_dataset;
  const current = groups.find((g) => g.label === saved) ?? groups.find((g) => g.dataset === saved)
    ?? groups.find((g) => g.dataset.endsWith("_holdout")) ?? groups.find((g) => g.dataset === benchmark) ?? groups[0];
  return { groups, current };
}

export function leaderboardView(board) {
  const { groups, current } = boardGroups(board);
  if (!current) return `<div class="panel"><h2>Provider comparison</h2><p class="muted">No configurations compared yet.</p></div>`;
  const min = board.ranking.min_positives;
  return `<div class="panel">
    <h2>Provider comparison</h2>
    <p class="muted">Each set of records is compared on its own (updated ${esc(time(board.updated_at))}). Ranked (${esc(board.ranking.method)}) by the critical cases caught first:
      fewest automated, then the worst critical label's detection, then false automation, accuracy and cost; never by accuracy alone.
      Only configurations measured on this version of the pack rank, and only where a critical label has at least ${esc(min)} positives.</p>
    <div class="choices" role="group" aria-label="Records">${groups.map((g) =>
      `<button type="button" data-board-group="${g.index}" aria-pressed="${g === current}" title="${esc(g.about.join("; "))}">${esc(g.label)}</button>`).join("")}</div>
    <div id="board" class="mt-3">${boardTables(board, current)}</div>
  </div>`;
}

export function wireLeaderboard(root, board) {
  root.querySelectorAll("[data-board-group]").forEach((button) => button.addEventListener("click", () => {
    const group = { ...board.groups[Number(button.dataset.boardGroup)], index: Number(button.dataset.boardGroup) };
    storageSet(`oh.board.${state.packId}`, group.label);
    root.querySelectorAll("[data-board-group]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    $("#board", root).innerHTML = boardTables(board, group);
    wire($("#board", root));
  }));
}

function boardTables(board, group) {
  const rows = board.rows.filter((r) => r.group === group.index); // in the ranking's order
  const min = board.ranking.min_positives;
  const same = (value) => rows.length > 1 && new Set(rows.map((r) => JSON.stringify(value(r)))).size === 1;
  const slots = [...new Set(rows.flatMap((r) => Object.keys(r.providers)))];
  const varying = slots.filter((slot) => !same((r) => [r.providers[slot]?.variant, remoteModels(r.providers[slot]?.model)]));
  const constantSlots = slots.filter((slot) => !varying.includes(slot));
  const ms = (v) => (v === null || v === undefined ? "n/a" : v < 10 ? v.toFixed(2) : Math.round(v).toLocaleString());
  const cost = (v) => (v === null || v === undefined ? "unknown" : v.toFixed(5));
  // A metric is a figure (shown as k of n) or a number; `value` is what the cell shows, compared
  // between rows to say once what every row shares.
  const metrics = [
    { key: "accuracy", label: "Accuracy", figure: (r) => r.figures.accuracy },
    { key: "macro_f1", label: "Macro-F1", value: (r) => r.macro_f1, text: (v) => num(v) },
    { key: "ece", label: "ECE", value: (r) => r.ece, text: (v) => num(v), firstDir: "ascending" },
    { key: "auto_rate", label: "Automated", figure: (r) => r.figures.automation },
    { key: "false_automation", label: "False automation", figure: (r) => r.figures.false_automation, firstDir: "ascending" },
    { key: "p50", label: "p50, ms", value: (r) => r.latency_p50_ms, text: ms, firstDir: "ascending" },
    { key: "cost", label: "Cost / decision, $", value: (r) => r.cost_per_decision_usd, text: cost, firstDir: "ascending" },
    { key: "errors", label: "Errors", value: (r) => r.errors, text: (v) => String(v), firstDir: "ascending" },
  ].map((m) => (m.figure
    ? { ...m, value: (r) => m.figure(r), sort: (r) => share(m.figure(r)), html: (r) => figureHtml(m.figure(r)), words: (r) => shown(m.figure(r)) }
    : { ...m, sort: m.value, html: (r) => esc(m.text(m.value(r))), words: (r) => m.text(m.value(r)) }));
  const constantMetrics = metrics.filter((m) => same(m.value));
  const criticalKeys = [...new Set(rows.flatMap((r) => r.figures.critical.map(criticalKey)))];

  const configCell = (r) => {
    const parts = varying.map((slot) =>
      `<span>${varying.length > 1 ? `<span class="muted">${esc(slotText(slot))}:</span> ` : ""}${esc(providerName(r.providers[slot]))}</span>`);
    const standing = r.standing_text ? `<br><span class="standing">${esc(r.standing_text)}</span>` : "";
    return `<div class="config" title="${esc(r.config)}">${parts.join("<br>")}${isDeployed(r) ? ` <span class="badge deployed">Deployed</span>` : ""}${standing}</div>`;
  };
  const criticalCell = (r, key) => {
    const c = r.figures.critical.find((x) => criticalKey(x) === key);
    if (!c) return `<span class="muted">–</span>`;
    const miss = c.detected.k < c.detected.n;
    return `<span class="${miss ? "miss" : ""}">${figureHtml(c.detected)}</span>
      <span class="meter${miss ? " miss" : ""}" aria-hidden="true"><span style="width:${100 * share(c.detected)}%"></span></span>
      ${c.auto_missed ? `<span class="miss">${c.auto_missed} automated</span>` : ""}${evidenceMark(c.insufficient_evidence, min)}`;
  };
  const columns = [
    { key: "config", label: "Configuration", className: "config-cell", sort: (r) => r.config, cell: configCell },
    ...criticalKeys.map((key) => ({
      key: `critical:${key}`, label: `<span title="${esc(key)}">${esc(criticalText(...key.split("=")))} caught</span>`, numeric: true, className: "critical",
      sort: (r) => share(r.figures.critical.find((x) => criticalKey(x) === key)?.detected),
      cell: (r) => criticalCell(r, key),
    })),
    ...metrics.filter((m) => !constantMetrics.includes(m)).map((m) => ({
      key: m.key, label: m.label, numeric: true, firstDir: m.firstDir, sort: m.sort, cell: m.html,
    })),
  ];
  // What every row shares is said once, above the table, instead of repeated in cells.
  const notes = [
    ...constantSlots.map((slot) => `all rows: ${esc(slot)} = ${esc(variantText(rows[0].providers[slot].variant))}`),
    ...constantMetrics.map((m) => `${esc(m.label)} ${esc(m.words(rows[0]))} in every configuration`),
  ];
  const withoutCounts = rows.some((r) => r.figures.accuracy === null)
    ? `<p class="table-note">Accuracy and false automation are n/a on a row recorded before leaderboards kept the counts they are of.</p>` : "";
  return (notes.length ? `<p class="table-note">${notes.join(" · ")}</p>` : "") + tableHtml({
    id: `board-${group.index}`,
    caption: `<b>${esc(group.dataset)}</b> · ${esc(group.about.join("; "))}`,
    rows,
    rank: (r) => r.rank,
    unranked: (r) => r.standing_text ?? "",
    stickyFirst: true,
    rowClass: (r) => (isDeployed(r) ? "deployed" : ""),
    columns,
  }) + withoutCounts + costChart(rows, varying, group) + otherQuestions(rows, varying, group);
}

// A configuration in plain words ("Rules + Jev 1.13.0"), for the slots that differ.
const configName = (row, varying) => varying.map((slot) =>
  `${varying.length > 1 ? `${slotText(slot)}: ` : ""}${providerName(row.providers[slot])}`).join(", ") || row.config;

const costTick = (v) => (v === 0 ? "$0" : `$${v.toFixed(Math.max(0, -Math.round(Math.log10(v))))}`);

// The trade-off behind a deployment choice: what a decision costs against how often it is right.
// Diamonds are configurations that missed a critical case; the deployed one is ringed. Only rows
// whose accuracy is known (k of n) are drawn.
function costChart(rows, varying, group) {
  const usable = rows.filter((r) => share(r.figures.accuracy) !== null && r.cost_per_decision_usd !== null && r.cost_per_decision_usd !== undefined);
  if (usable.length < 2 || new Set(usable.map((r) => r.cost_per_decision_usd)).size < 2) return "";
  const accuracy = (r) => share(r.figures.accuracy);
  // Named on the chart: the deployed configuration, the first in the ranking, and every one that no
  // other beats on both cost and accuracy (the frontier worth choosing from). The rest are named in
  // their tooltips.
  const beaten = (r) => usable.some((o) => o !== r && o.cost_per_decision_usd <= r.cost_per_decision_usd && accuracy(o) >= accuracy(r)
    && (o.cost_per_decision_usd < r.cost_per_decision_usd || accuracy(o) > accuracy(r)));
  const named = (r) => isDeployed(r) || r.rank === 1 || !beaten(r);
  return `<h3 class="chart-title">Cost against accuracy</h3>${chartHtml({
    id: `cost-${group.index}`,
    type: "scatter",
    ariaLabel: `Cost against accuracy for ${usable.length} configurations on ${group.label}, from $${Math.min(...usable.map((r) => r.cost_per_decision_usd)).toFixed(5)} to $${Math.max(...usable.map((r) => r.cost_per_decision_usd)).toFixed(5)} per decision. Each configuration's accuracy is in its own label.`,
    x: { kind: "log", title: "Cost per decision (log scale)", format: costTick },
    y: { kind: "rate", title: "Accuracy" },
    dodge: true,
    points: usable.map((r) => ({
      x: r.cost_per_decision_usd, y: accuracy(r), ring: isDeployed(r), shape: missed(r) ? "diamond" : "circle", tone: missed(r) ? "danger" : "c1",
      label: named(r) ? configName(r, varying) : undefined,
      title: `${configName(r, varying)}${isDeployed(r) ? " (deployed)" : ""}`,
      rows: [
        ["Accuracy", shown(r.figures.accuracy)],
        ["Cost / decision", `$${r.cost_per_decision_usd.toFixed(5)}`],
        ...r.figures.critical.map((c) => [`${criticalText(c.question, c.label)} caught`, shown(c.detected)]),
      ],
    })),
    legend: [
      ...(usable.some((r) => !missed(r)) ? [{ swatch: "dot bg-c1", label: usable.some(missed) ? "Caught every critical case" : "Configuration" }] : []),
      ...(usable.some(missed) ? [{ swatch: "diamond bg-danger", label: "Missed a critical case" }] : []),
      ...(usable.some(isDeployed) ? [{ swatch: "ring", label: "Deployed" }] : []),
    ],
    data: {
      rows: usable,
      defaultCompare: (a, b) => a.cost_per_decision_usd - b.cost_per_decision_usd || accuracy(b) - accuracy(a),
      columns: [
        { key: "config", label: "Configuration", sort: (r) => configName(r, varying), cell: (r) => esc(configName(r, varying)) },
        { key: "cost", label: "Cost / decision, $", numeric: true, sort: (r) => r.cost_per_decision_usd, cell: (r) => r.cost_per_decision_usd.toFixed(5) },
        { key: "accuracy", label: "Accuracy", numeric: true, sort: accuracy, cell: (r) => figureHtml(r.figures.accuracy) },
      ],
    },
  })}`;
}

// Accuracy of the non-primary questions that differ between configurations, weak cells marked.
function otherQuestions(rows, varying, group) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r.questions ?? {})))].sort();
  const changing = keys.filter((k) => new Set(rows.map((r) => JSON.stringify(r.questions?.[k] ?? null))).size > 1);
  if (!changing.length) return "";
  const shade = (part) => (part === null ? "" : part < 0.8 ? "cell-bad" : part < 0.9 ? "cell-warn" : "");
  const name = (r) => configName(r, varying);
  return `<details class="mt-3"><summary>Per-check accuracy</summary>
    <p class="muted mt-2">Accuracy of each check; ECE in the tooltip. Amber when under 9 in 10 cases are right, red under 8 in 10.</p>
    ${tableHtml({
      id: `checks-${group.index}`,
      caption: "",
      rows,
      stickyFirst: true,
      columns: [
        { key: "config", label: "Configuration", className: "config-cell", sort: name, cell: (r) => `<span title="${esc(r.config)}">${esc(name(r))}</span>` },
        ...changing.map((k) => ({
          key: k, label: `<code>${esc(k)}</code>`, numeric: true, sort: (r) => share(r.figures.questions?.[k]),
          cell: (r) => {
            const m = r.questions?.[k];
            const figure = r.figures.questions?.[k];
            return m ? `<span class="${shade(share(figure))}" title="ECE ${num(m.ece)}">${figureHtml(figure)}</span>` : "n/a";
          },
        })),
      ],
    })}</details>`;
}

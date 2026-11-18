// SPDX-License-Identifier: Apache-2.0
// Overview: what is deployed, the numbers people act on, outcome mix and decisions per day.

import { chartHtml, sparkline } from "./charts.js";
import { holdNotice, originBadge, providerWords, statusNotice } from "./pack.js";
import { views, wire } from "./router.js";
import { $, api, emptyState, esc, figureHtml, icon, may, money, num, OUTCOMES, outcomeText, shown, slotText, stat, state, storageGet, storageSet, zoneName } from "./ui.js";

views.overview = async (main) => {
  const [stats, holds] = await Promise.all([
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
    api("/api/holds").catch(() => null),
  ]);
  const d = state.detail;
  const total = stats.decisions;
  const period = PERIODS.some(([key]) => key === storageGet("oh.period")) ? storageGet("oh.period") : "30";
  main.innerHTML = `
    ${packHeader(d, holds)}
    ${total ? `
      <div class="period-row">
        <h2>Decision activity</h2>
        <div class="segmented compact" role="group" aria-label="Reporting period">${PERIODS.map(([key, label]) =>
          `<button type="button" data-period="${key}" aria-pressed="${key === period}">${label}</button>`).join("")}</div>
      </div>
      <div id="period-figures">${periodFigures(stats, period)}</div>
      <div class="panel">
        <h2>Decisions per day <span class="muted">· days in ${esc(zoneName())}</span></h2>
        ${dailyView(stats.daily)}
      </div>` : `
      ${emptyState({
        icon: "inbox", title: "No decisions yet",
        text: `Decisions appear here as soon as this pack makes one: from Try it, <a href="/docs">the API</a>, a CDS Hooks call or the sandbox worker.`,
        action: { href: "#try", label: "Try it" },
      })}`}`;
  main.querySelector(".period-row")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-period]");
    if (!button) return;
    storageSet("oh.period", button.dataset.period);
    main.querySelectorAll("[data-period]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    $("#period-figures").innerHTML = periodFigures(stats, button.dataset.period);
    wireStack(main);
  });
  wire(main);
  wireStack(main);
};

// ---- the period: 7 days, 30 days or everything, each compared with the one before it ------------

const PERIODS = [["7", "7 days"], ["30", "30 days"], ["all", "All"]];
const ratio = (part, whole) => (whole ? part / whole : null);

function periodFigures(stats, period) {
  // The period's figures come from the server (stats.periods: the same local days as below);
  // the trend lines are drawn from the daily counts.
  const { days, from, now, before } = stats.periods[period];
  const today = localToday();
  const scope = days ? `last ${days} days` : "all time";
  // The trend lines run over the period (at most the last 30 days), from the first decision on.
  const trendFrom = [stats.daily[0]?.date ?? today, from ?? addDays(today, -29)].sort().pop();
  const byDate = new Map(stats.daily.filter((d) => d.date >= trendFrom).map((d) => [d.date, d]));
  const trendDays = [];
  for (let day = trendFrom; day <= today; day = addDays(day, 1)) {
    const counts = byDate.get(day);
    trendDays.push({ AUTO: counts?.AUTO ?? 0, decisions: OUTCOMES.reduce((n, o) => n + (counts?.[o] ?? 0), 0) });
  }
  return `${primaryStats(stats, now, before, scope, days, trendDays)}
    ${secondaryStats(stats, now, before, days, trendDays)}
    <div class="panel">
      <h2>Outcome mix <span class="muted">· ${esc(scope)}</span></h2>
      ${now.decisions ? stackBar(now) : `<p class="muted">No decisions in the ${esc(scope)}.</p>`}
    </div>`;
}

// The window before, beside the figure: its own k of n, never a difference of two shares (which
// has no interval and, on few cases, reads as a large swing). Nothing when it had no cases.
function previous(figure, days) {
  if (!days || !figure || !figure.n) return "";
  return `<div class="stat-change">previous ${days} days: ${esc(shown(figure))}</div>`;
}

// The four numbers people act on: how much is automated, what waits for a person (now, whatever
// the period), and whether people agree with the pack (overrides, observed accuracy), each with
// what it is out of and the window before it.
function primaryStats(stats, now, before, scope, days, trendDays) {
  const waiting = stats.pending_reviews;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const autoTrend = sparkline(trendDays.map((day) => ratio(day.AUTO, day.decisions)));
  return `<div class="stats-primary">
    ${stat({
      label: "Automated", value: figureHtml(now.figures.automated), html: true, trend: autoTrend,
      foot: now.decisions ? `of the ${plural(now.decisions, "decision")}, ${scope}` : `No decisions, ${scope}`,
      change: previous(before?.figures.automated, days),
    })}
    ${stat({
      label: "Waiting for review", value: String(waiting), tone: waiting ? "warning" : "",
      foot: waiting ? `<a href="#review">Open the review queue ${icon("arrow-right")}</a>` : "Nothing waiting",
    })}
    ${stat({
      label: "Human override rate", value: figureHtml(now.figures.overrides), html: true,
      foot: now.reviewed ? `of the ${plural(now.reviewed, "review")}, ${scope}` : `No reviews, ${scope}`,
      change: previous(before?.figures.overrides, days),
    })}
    ${stat({
      label: "Observed accuracy", value: figureHtml(now.figures.accuracy), html: true,
      foot: now.labelled ? `against ${plural(now.labelled, "human label")}, ${scope}` : `No human labels, ${scope}`,
      change: previous(before?.figures.accuracy, days),
    })}
  </div>`;
}

// Operational context, one row: volume (for the period), speed, cost, the audit chain and feedback.
function secondaryStats(stats, now, before, days, trendDays) {
  const f = stats.feedback;
  return `<div class="stats-secondary">
    ${stat({
      size: "small", label: "Decisions", value: now.decisions.toLocaleString(),
      trend: sparkline(trendDays.map((day) => day.decisions), { zero: true }),
      change: days && before?.decisions ? `<div class="stat-change">previous ${days} days: ${before.decisions.toLocaleString()}</div>` : "",
    })}
    ${stat({ size: "small", label: "Latency p95", value: stats.latency_ms.p95 === null ? "n/a" : `${num(stats.latency_ms.p95, 2)} ms`, foot: "all time" })}
    ${stat({ size: "small", label: "Cost / decision", value: money(stats.cost_usd.per_decision), foot: "all time" })}
    ${may("admin") && state.accounts !== "memberships" ? stat({
      size: "small", label: "Audit chain", html: true,
      value: `<a href="#audit" class="action-link">Audit trail ${icon("arrow-right")}</a>`,
      foot: "Open to verify integrity",
    }) : ""}
    ${stat({
      size: "small", label: "Feedback", html: true,
      value: `${icon("thumbs-up")}<span class="sr-only">Right:</span> ${f.up} <span class="muted" aria-hidden="true">·</span> ${icon("thumbs-down")}<span class="sr-only">Wrong:</span> ${f.down}`,
      foot: `<a href="#feedback">${f.down_on_auto} wrong on automated</a>`,
    })}
  </div>`;
}

// The outcome mix: segments wide enough carry their name and count inside the bar; the legend
// gives each outcome as k of n with its interval, and shows only the segments without room (it
// stays in the page for screen readers either way).
function stackBar(now) {
  const total = now.decisions;
  const parts = OUTCOMES.filter((o) => now[o]).map((o) => `<span class="bg-${o}" data-outcome="${o}" style="width:${(100 * now[o]) / total}%" title="${outcomeText(o)}: ${esc(shown(now.figures.outcomes[o]))}">
      <span class="stack-label" aria-hidden="true">${esc(outcomeText(o))} ${now[o]}</span></span>`).join("");
  const keys = `<div class="legend stack-legend">${OUTCOMES.map((o) => `<span data-outcome="${o}"><i class="bg-${o}"></i>${esc(outcomeText(o))} ${figureHtml(now.figures.outcomes[o])}</span>`).join("")}</div>`;
  return `<div class="stack">${parts}</div>${keys}`;
}

// Which segments have room for their label: measured, and again whenever the bar changes width.
function wireStack(root) {
  const stack = root.querySelector(".stack");
  if (!stack || stack.dataset.wired) return;
  stack.dataset.wired = "1";
  const legend = stack.nextElementSibling;
  const observer = new ResizeObserver(() => (stack.isConnected ? fit() : observer.disconnect()));
  const fit = () => {
    for (const segment of stack.querySelectorAll("[data-outcome]")) {
      const label = segment.querySelector(".stack-label");
      const fits = label.scrollWidth + 16 <= segment.clientWidth;
      segment.classList.toggle("labelled", fits);
      legend.querySelector(`[data-outcome="${segment.dataset.outcome}"]`).classList.toggle("sr-only", fits);
    }
  };
  observer.observe(stack);
  fit();
}

function packHeader(d, holds) {
  const deployed = Object.entries(d.active_providers);
  return `<header class="pack-header">
    <div class="pack-identity">
      <div><h1>${esc(d.name)}</h1>
        <p class="pack-risk">${originBadge(d)} <span class="badge risk">Risk level ${esc(d.risk.level)}</span> <span class="muted">${esc(d.risk.category)}</span></p>
      </div>
      <a href="#card" class="button">Decision Card ${icon("arrow-right")}</a>
    </div>
    ${statusNotice(d)}
    ${holds === null ? '<p class="callout warning" role="status">Could not check automation holds.</p>' : holdNotice(holds, d)}
    ${withoutModel(d.without_model ?? [])}
    <details class="deployment-details">
      <summary>Deployment details <span class="muted">v${esc(d.version)}</span></summary>
      <p class="deployed"><span class="muted">Deployed</span> ${deployed.map(([slot, p]) =>
      `<span class="deployed-slot">${esc(slotText(slot))}: <b>${providerWords(p)}</b></span>`).join(`<span class="muted" aria-hidden="true">·</span>`)}</p>
      <p class="mono muted">${esc(d.id)} v${esc(d.version)} · ${deployed.map(([slot, p]) => `${esc(slot)}=${esc(p.variant)}`).join(" · ")}</p>
    </details>
  </header>`;
}

// Slots on the pack's offline floor although it offers a model there (e.g. no Jev set up).
function withoutModel(slots) {
  if (!slots.length) return "";
  return `<div class="callout neutral" role="note"><b>Running without Jev or an LLM:</b> ${slots.map((slot) => esc(slotText(slot))).join(", ")}.</div>`;
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

// Under a week of history a chart is mostly empty space (one day was a lone 36 px column in an
// 1,100 px panel), so each day with decisions gets a line instead.
const MIN_CHART_DAYS = 7;

function dailyView(daily) {
  const span = daily.length ? (Date.parse(localToday()) - Date.parse(daily[0].date)) / 86_400_000 + 1 : 0;
  if (span >= MIN_CHART_DAYS) return dailyChart(daily);
  const line = (day) => {
    const total = OUTCOMES.reduce((sum, o) => sum + day[o], 0);
    const parts = OUTCOMES.filter((o) => day[o]).map((o) => `${day[o]} ${esc(outcomeText(o).toLowerCase())}`);
    return `<li><b>${esc(dayLabel(day.date, true))}</b> <span>${total} decision${total === 1 ? "" : "s"}</span>
      <span class="muted">${parts.join(" · ")}</span></li>`;
  };
  return `<ul class="day-summary">${[...daily].reverse().map(line).join("")}</ul>
    <p class="chart-note">A chart appears once there are ${MIN_CHART_DAYS} days of history.</p>`;
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

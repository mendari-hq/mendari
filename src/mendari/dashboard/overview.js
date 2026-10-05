// Overview: what is deployed, the numbers people act on, outcome mix and decisions per day.

import { chartHtml, sparkline } from "./charts.js";
import { originBadge, providerWords } from "./pack.js";
import { views, wire } from "./router.js";
import { $, api, emptyState, esc, icon, may, money, num, OUTCOMES, outcomeText, pct, slotText, stat, state, storageGet, storageSet, zoneName } from "./ui.js";

views.overview = async (main) => {
  const [stats, chain] = await Promise.all([
    api(`/api/stats?pack_id=${encodeURIComponent(state.packId)}`),
    // Verifying the chain reads every event: administrators only (the server says the same).
    may("admin") ? api("/api/audit/verify") : null,
  ]);
  const d = state.detail;
  const total = stats.decisions;
  const period = PERIODS.some(([key]) => key === storageGet("oh.period")) ? storageGet("oh.period") : "30";
  main.innerHTML = `
    <h1>${esc(d.name)}</h1>
    ${packHeader(d)}
    ${total ? `
      <div class="period-row">
        <span class="muted" id="period-name">Period</span>
        <div class="segmented compact" role="group" aria-labelledby="period-name">${PERIODS.map(([key, label]) =>
          `<button type="button" data-period="${key}" aria-pressed="${key === period}">${label}</button>`).join("")}</div>
      </div>
      <div id="period-figures">${periodFigures(stats, chain, period)}</div>
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
    $("#period-figures").innerHTML = periodFigures(stats, chain, button.dataset.period);
    wireStack(main);
  });
  wire(main);
  wireStack(main);
};

// ---- the period: 7 days, 30 days or everything, each compared with the one before it ------------

const PERIODS = [["7", "7 days"], ["30", "30 days"], ["all", "All"]];
const REVIEW_COUNTS = ["reviewed", "overridden", "labelled", "agreed"];

// Totals over the days from `from` to `to` (inclusive; null for no bound), from stats.daily.
function totalsBetween(daily, from, to) {
  const sums = Object.fromEntries([...OUTCOMES, ...REVIEW_COUNTS].map((k) => [k, 0]));
  for (const day of daily) {
    if ((from && day.date < from) || (to && day.date > to)) continue;
    for (const key of Object.keys(sums)) sums[key] += day[key] ?? 0;
  }
  sums.decisions = OUTCOMES.reduce((sum, o) => sum + sums[o], 0);
  return sums;
}

const ratio = (part, whole) => (whole ? part / whole : null);

function periodFigures(stats, chain, period) {
  const today = localToday();
  const days = period === "all" ? null : Number(period);
  const from = days ? addDays(today, 1 - days) : null;
  const now = totalsBetween(stats.daily, from, null);
  const before = days ? totalsBetween(stats.daily, addDays(from, -days), addDays(from, -1)) : null;
  const scope = days ? `last ${days} days` : "all time";
  // The trend lines run over the period (at most the last 30 days), from the first decision on.
  const trendFrom = [stats.daily[0].date, from ?? addDays(today, -29)].sort().pop();
  const trendDays = [];
  for (let day = trendFrom; day <= today; day = addDays(day, 1)) trendDays.push(totalsBetween(stats.daily, day, day));
  return `${primaryStats(stats, now, before, scope, days, trendDays)}
    ${secondaryStats(stats, chain, now, before, days, trendDays)}
    <div class="panel">
      <h2>Outcome mix <span class="muted">· ${esc(scope)}</span></h2>
      ${now.decisions ? stackBar(now, now.decisions) : `<p class="muted">No decisions in the ${esc(scope)}.</p>`}
    </div>`;
}

// A change against the previous period, in words: points for a rate, percent for a count. Nothing
// when the previous period had nothing to compare with.
function change(current, previous, days, { rate = true } = {}) {
  if (!days || current === null || previous === null || previous === undefined) return "";
  const previousText = `vs previous ${days} days`;
  if (rate) {
    const points = (current - previous) * 100;
    if (Math.abs(points) < 0.05) return `<div class="stat-change">No change ${previousText}</div>`;
    return `<div class="stat-change">${points > 0 ? "+" : "−"}${Math.abs(points).toFixed(1)} pts ${previousText}</div>`;
  }
  if (!previous) return "";
  const percent = ((current - previous) / previous) * 100;
  if (Math.abs(percent) < 0.5) return `<div class="stat-change">No change ${previousText}</div>`;
  return `<div class="stat-change">${percent > 0 ? "+" : "−"}${Math.abs(percent).toFixed(0)}% ${previousText}</div>`;
}

// The four numbers people act on: how much is automated, what waits for a person (now, whatever
// the period), and whether people agree with the pack (overrides, observed accuracy), each with
// what it is out of and how it moved.
function primaryStats(stats, now, before, scope, days, trendDays) {
  const waiting = stats.pending_reviews;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const automated = ratio(now.AUTO, now.decisions);
  const overrides = ratio(now.overridden, now.reviewed);
  const accuracy = ratio(now.agreed, now.labelled);
  const autoTrend = sparkline(trendDays.map((day) => ratio(day.AUTO, day.decisions)));
  return `<div class="stats-primary">
    ${stat({
      label: "Automated", value: pct(automated), trend: autoTrend,
      foot: now.decisions ? `${now.AUTO} of ${plural(now.decisions, "decision")}, ${scope}` : `No decisions, ${scope}`,
      change: change(automated, before && ratio(before.AUTO, before.decisions), days),
    })}
    ${stat({
      label: "Waiting for review", value: String(waiting), tone: waiting ? "warning" : "",
      foot: waiting ? `<a href="#review">Open the review queue ${icon("arrow-right")}</a>` : "Nothing waiting",
    })}
    ${stat({
      label: "Human override rate", value: pct(overrides),
      foot: now.reviewed ? `of ${plural(now.reviewed, "review")}, ${scope}` : `No reviews, ${scope}`,
      change: change(overrides, before && ratio(before.overridden, before.reviewed), days),
    })}
    ${stat({
      label: "Observed accuracy", value: pct(accuracy),
      foot: now.labelled ? `against ${plural(now.labelled, "human label")}, ${scope}` : `No human labels, ${scope}`,
      change: change(accuracy, before && ratio(before.agreed, before.labelled), days),
    })}
  </div>`;
}

// Operational context, one row: volume (for the period), speed, cost, the audit chain and feedback.
function secondaryStats(stats, chain, now, before, days, trendDays) {
  const f = stats.feedback;
  return `<div class="stats-secondary">
    ${stat({
      size: "small", label: "Decisions", value: now.decisions.toLocaleString(),
      trend: sparkline(trendDays.map((day) => day.decisions), { zero: true }),
      change: change(now.decisions, before?.decisions, days, { rate: false }),
    })}
    ${stat({ size: "small", label: "Latency p95", value: stats.latency_ms.p95 === null ? "n/a" : `${num(stats.latency_ms.p95, 2)} ms`, foot: "all time" })}
    ${stat({ size: "small", label: "Cost / decision", value: money(stats.cost_usd.per_decision), foot: "all time" })}
    ${chain ? stat({
      size: "small", label: "Audit chain", html: true, tone: chain.ok ? "" : "danger",
      value: chain.ok ? `${icon("shield-check")} Verified` : `${icon("shield-x")} Broken at event ${esc(chain.broken_at)}`,
      foot: `${chain.events.toLocaleString()} events`,
    }) : ""}
    ${stat({
      size: "small", label: "Feedback", html: true,
      value: `${icon("thumbs-up")}<span class="sr-only">Right:</span> ${f.up} <span class="muted" aria-hidden="true">·</span> ${icon("thumbs-down")}<span class="sr-only">Wrong:</span> ${f.down}`,
      foot: `<a href="#feedback">${f.down_on_auto} wrong on automated</a>`,
    })}
  </div>`;
}

// The outcome mix: segments wide enough carry their name and share inside the bar; the legend
// shows only the rest (it stays in the page for screen readers either way).
function stackBar(counts, total) {
  const share = (o) => pct(total ? counts[o] / total : null);
  const parts = OUTCOMES.filter((o) => counts[o]).map((o) => `<span class="bg-${o}" data-outcome="${o}" style="width:${(100 * counts[o]) / total}%" title="${outcomeText(o)}: ${counts[o]}">
      <span class="stack-label" aria-hidden="true">${esc(outcomeText(o))} ${share(o)}</span></span>`).join("");
  const keys = `<div class="legend stack-legend">${OUTCOMES.map((o) => `<span data-outcome="${o}"><i class="bg-${o}"></i>${esc(outcomeText(o))} ${counts[o]} (${share(o)})</span>`).join("")}</div>`;
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

function packHeader(d) {
  const deployed = Object.entries(d.active_providers);
  return `<div class="pack-header">
    <p class="pack-risk">${originBadge(d)} <span class="badge risk">Risk level ${d.risk.level}</span> <span class="muted">${esc(d.risk.category)}</span></p>
    <p class="deployed"><span class="muted">Deployed</span> ${deployed.map(([slot, p]) =>
      `<span class="deployed-slot">${esc(slotText(slot))}: <b>${providerWords(p)}</b></span>`).join(`<span class="muted" aria-hidden="true">·</span>`)}</p>
    <p class="mono muted">${esc(d.id)} v${esc(d.version)} · ${deployed.map(([slot, p]) => `${esc(slot)}=${esc(p.variant)}`).join(" · ")}</p>
    ${withoutModel(d.without_model ?? [])}
  </div>`;
}

// Slots on the pack's offline floor although it offers a model there (e.g. no Jev set up).
function withoutModel(slots) {
  if (!slots.length) return "";
  return `<div class="callout neutral" role="note"><b>Running without a model:</b> ${slots.map((slot) => esc(slotText(slot))).join(", ")}.
    This deployment uses the pack's offline readers there; the Decision Card compares them with the model readers.</div>`;
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

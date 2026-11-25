// SPDX-License-Identifier: Apache-2.0
// Overview: what is deployed, the numbers people act on, outcome mix and decisions per day.

import { adoptionPath } from "./adoption.js";
import { chartHtml, sparkline } from "./charts.js";
import { holdNotice, originBadge, providerWords, statusNotice } from "./manifest.js";
import { render, views, wire } from "./router.js";
import { setupChecks, setupPanel } from "./system.js";
import { $, actionDialog, api, copyText, download, emptyState, esc, figureHtml, fileSlug, focusHeading, icon, may, money, num, OUTCOMES, outcomeText, agentPath, shown, slotText, stat, state, storageGet, storageSet, toast, toastError, today, whileBusy, zoneName } from "./ui.js";

views.overview = async (main) => {
  const agentId = state.agentId;
  const [stats, chain, holds, brief, setup] = await Promise.all([
    api(`/api/stats?agent_id=${encodeURIComponent(agentId)}`),
    // Verifying the chain reads every event: the deployment's operators alone (the server says
    // the same; ADR-0008): an administrator of the organization that operates it.
    may("operate") ? api("/api/audit/verify") : null,
    api("/api/holds").catch(() => []),
    api(`${agentPath()}/validation-brief?days=30`).catch(() => null),
    // A new deployment's operator sees what is left to set up first (system.js).
    setupChecks(),
  ]);
  if (state.agentId !== agentId) return;
  const d = state.detail;
  const total = stats.decisions;
  const period = PERIODS.some(([key]) => key === storageGet("oh.period")) ? storageGet("oh.period") : "30";
  const lens = LENSES.some(([key]) => key === storageGet("oh.lens")) ? storageGet("oh.lens") : "governance";
  main.innerHTML = `
    ${setupPanel(setup)}
    <section class="agent-hero agent-header" aria-label="Agent overview">
      <div class="agent-hero-top">
        <div class="agent-hero-title">
          <p class="agent-risk">${originBadge(d)} <span class="badge risk">Risk level ${d.risk.level}</span> <span class="muted">${esc(d.risk.category)}</span></p>
          <h1>${esc(d.name)}</h1>
        </div>
        <div class="agent-hero-actions">
          ${may("decide") ? `<a class="button primary" href="#try">${icon("play")} Try it</a>` : ""}
          <a class="button" href="#evals">${icon("bar-chart")} Evaluations</a>
          ${may("review") ? `<a class="button" href="#review">${icon("clipboard-check")} Review queue${stats.pending_reviews ? ` (${stats.pending_reviews})` : ""}</a>` : ""}
          <a class="button" href="#card">${icon("file-text")} Decision Card</a>
        </div>
      </div>
      ${agentHeader(d, holds)}
    </section>
    ${adoptionPath(stats, brief, d)}
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
        text: `Decisions appear here as soon as this agent makes one: from Try it, <a href="/docs">the API</a>, a CDS Hooks call or the sandbox worker.`,
        action: { href: "#try", label: "Try it" },
      })}`}
    ${brief ? commandCenter(brief, d, lens) : ""}`;
  main.querySelector(".period-row")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-period]");
    if (!button) return;
    storageSet("oh.period", button.dataset.period);
    main.querySelectorAll("[data-period]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    $("#period-figures").innerHTML = periodFigures(stats, chain, button.dataset.period);
    wireStack(main);
  });
  if (brief) wireCommandCenter(main, brief, d);
  // A step of the adoption path that names a section of this view (the command center) goes
  // there, on the lens where its action is, instead of following "#cc-title" as a route.
  main.querySelector(".adoption")?.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-section]");
    if (!link) return;
    event.preventDefault();
    main.querySelector('[data-lens="governance"][aria-pressed="false"]')?.click();
    focusHeading(main.querySelector(link.getAttribute("href")));
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
  // The period's figures come from the server (stats.periods: the same local days as below);
  // the trend lines are drawn from the daily counts.
  const { days, from, now, before } = stats.periods[period];
  const currentDay = today();
  const scope = days ? `last ${days} days` : "all time";
  // The trend lines run over the period (at most the last 30 days), from the first decision on.
  const trendDays = [];
  if (stats.daily.length) {
    const trendFrom = [stats.daily[0].date, from ?? addDays(currentDay, -29)].sort().pop();
    for (let day = trendFrom; day <= currentDay; day = addDays(day, 1)) trendDays.push(totalsBetween(stats.daily, day, day));
  }
  return `${primaryStats(stats, now, before, scope, days, trendDays)}
    ${secondaryStats(stats, chain, now, before, days, trendDays)}
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
// the period), and whether people agree with the agent (overrides, observed accuracy), each with
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
function secondaryStats(stats, chain, now, before, days, trendDays) {
  const f = stats.feedback;
  return `<div class="stats-secondary">
    ${stat({
      size: "small", label: "Decisions", value: now.decisions.toLocaleString(),
      trend: sparkline(trendDays.map((day) => day.decisions), { zero: true }),
      change: days && before?.decisions ? `<div class="stat-change">previous ${days} days: ${before.decisions.toLocaleString()}</div>` : "",
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

function agentHeader(d, holds) {
  const deployed = Object.entries(d.active_providers);
  return `<div class="agent-header-meta">
    ${statusNotice(d)}
    ${holdNotice(holds, d)}
    <p class="deployed"><span class="muted">Deployed</span> ${deployed.map(([slot, p]) =>
      `<span class="deployed-slot">${esc(slotText(slot))}: <b>${providerWords(p)}</b></span>`).join(`<span class="muted" aria-hidden="true">·</span>`)}</p>
    <p class="mono muted">${esc(d.id)} v${esc(d.version)} · ${deployed.map(([slot, p]) => `${esc(slot)}=${esc(p.variant)}`).join(" · ")} · <a href="https://mendari-agent-hub.vercel.app" target="_blank" rel="noopener noreferrer">Agent Hub ${icon("external")}</a></p>
    ${withoutModel(d.without_model ?? [])}
  </div>`;
}

// Slots on the agent's offline floor although it offers a model there (e.g. no LLM set up).
function withoutModel(slots) {
  if (!slots.length) return "";
  return `<div class="callout neutral" role="note"><b>Running without a model:</b> ${slots.map((slot) => esc(slotText(slot))).join(", ")}.
    This deployment uses the agent's offline readers there; the Decision Card compares them with the model readers.</div>`;
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

// Under a week of history a chart is mostly empty space (one day was a lone 36 px column in an
// 1,100 px panel), so each day with decisions gets a line instead.
const MIN_CHART_DAYS = 7;

function dailyView(daily) {
  if (!daily.length) return `<p class="muted">No daily counts yet.</p>`;
  const span = (Date.parse(today()) - Date.parse(daily[0].date)) / 86_400_000 + 1;
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
  const last = [today(), daily[daily.length - 1].date].sort().pop();
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

// ---- Command center: shadow pilot & validation brief, closed-loop overrides, EHR integration ----

const LENSES = [
  ["governance", "Governance & ROI"],
  ["improvement", "Closed-loop improvement"],
  ["ehr", "EHR integration"],
];

// A gate with nothing to measure is "no_data": said in words, in the neutral badge, never a pass.
const GATE_TONE = { pass: "s-ok", warn: "s-warning", fail: "s-degraded" };
const GATE_LABEL = { pass: "Pass", warn: "Review", fail: "Action needed", no_data: "Not enough data" };

function commandCenter(brief, detail, lens) {
  return `<section class="panel command-center" aria-labelledby="cc-title">
    <div class="row cc-head">
      <h2 id="cc-title">Deployment readiness &amp; operations</h2>
      <span class="grow"></span>
      <div class="segmented compact" role="group" aria-label="Workspace lens">${LENSES.map(([key, label]) =>
        `<button type="button" data-lens="${key}" aria-pressed="${key === lens}">${esc(label)}</button>`).join("")}</div>
    </div>
    <div id="cc-body">${lensBody(brief, detail, lens)}</div>
  </section>`;
}

function lensBody(brief, detail, lens) {
  if (lens === "improvement") return improvementLens(brief, detail);
  if (lens === "ehr") return ehrLens(detail);
  return governanceLens(brief);
}

function governanceLens(brief) {
  const sp = brief.shadow_pilot;
  const vol = brief.volume;
  const mins = Number(storageGet("oh.roi.mins", "3.5")) || 3.5;
  const hours = ((brief.roi.weekly_auto_cases_projected * mins) / 60).toFixed(1);
  const shadowBar = sp.active
    ? `<div class="callout ok cc-shadow"><div class="row"><span>${icon("eye")} <b>Shadow pilot active.</b> Automation is held for clinician comparison while tracking what would automate (${vol.would_automate} of ${vol.total} cases so far).</span><span class="grow"></span>
        ${may("approve") ? `<button type="button" id="shadow-stop" data-hold="${esc(sp.hold_id)}">End shadow pilot</button>` : ""}</div></div>`
    : `<div class="callout neutral cc-shadow"><div class="row"><span>${icon("eye")} <b>7-day shadow mode pilot:</b> score live traffic alongside clinicians with zero automation, then download the readiness brief: your organization's own counts, computed here and not signed.</span><span class="grow"></span>
        ${may("approve") ? `<button type="button" class="primary" id="shadow-start">Start shadow pilot</button>` : ""}</div></div>`;
  const gates = `<ul class="gate-list">${brief.gates.map((g) => `<li class="gate-row">
      <span class="badge ${GATE_TONE[g.status] ?? "o-ABSTAIN"}">${esc(GATE_LABEL[g.status] ?? g.status)}</span>
      <span class="gate-label"><b>${esc(g.label)}</b></span>
      <span class="muted">${esc(g.detail)}</span>
    </li>`).join("")}</ul>`;
  return `${shadowBar}
    <div class="cc-roi-row">
      <div class="cc-metric"><span class="muted">Automation-ready</span><b>${figureHtml(brief.figures.automation_readiness)}</b></div>
      <div class="cc-metric"><span class="muted">Clinician agreement</span><b>${figureHtml(brief.figures.agreement)}</b></div>
      <div class="cc-metric"><span class="muted">Projected time reclaimed</span><b id="roi-hours">${esc(hours)} hrs / week</b>
        <label class="cc-mins muted">at <select id="roi-mins" aria-label="Minutes per case">${[2, 3.5, 5, 8].map((m) => `<option value="${m}" ${m === mins ? "selected" : ""}>${m} min/case</option>`).join("")}</select></label></div>
    </div>
    ${gates}
    <div class="row mt-3">
      <button type="button" id="brief-download">${icon("download")} Download Validation Brief (.md)</button>
      <button type="button" id="packet-download" title="Everything a governance committee reads about this agent, as one page to print: what is known, and what nobody has stated">${icon("download")} Committee packet</button>
      <button type="button" id="brief-copy">${icon("copy")} Copy executive summary</button>
      <span class="grow"></span>
      <a class="action-link" href="#card/approval">Governance &amp; holds ${icon("arrow-right")}</a>
    </div>`;
}

function improvementLens(brief, detail) {
  const clusters = brief.override_clusters ?? [];
  const name = detail.id.split("/").pop();
  const cli = `mendari eval ${detail.id} --dataset ${name}-overrides.jsonl`;
  const table = clusters.length
    ? `<div class="table-wrap mt-3"><table class="compact">
        <thead><tr><th scope="col">Agent recommended</th><th scope="col">Clinician chose</th><th scope="col" class="num">Overrides</th><th scope="col" class="num">Of overrides</th></tr></thead>
        <tbody>${clusters.map((c) => `<tr><td><code>${esc(c.recommended)}</code></td><td><code>${esc(c.chosen)}</code></td><td class="num">${c.count}</td><td class="num">${c.count} of ${brief.volume.overrides}</td></tr>`).join("")}</tbody>
      </table></div>`
    : `<p class="muted mt-3">No human overrides recorded in the last ${brief.window_days} days. As clinicians override decisions in the Review queue, recurring disagreement patterns cluster here for targeted recalibration.</p>`;
  return `<p class="muted mt-2">Close the loop from clinician overrides to measurable agent improvements: export overridden cases as a local validation set, test a recalibrated threshold or stronger provider, and compare before promotion.</p>
    ${table}
    <div class="row mt-3">
      ${may("admin") ? `<button type="button" class="primary" id="export-overrides">${icon("download")} Export overrides (JSONL)</button>` : ""}
      <button type="button" id="copy-recal">${icon("copy")} Copy eval CLI</button>
      <a class="action-link" href="#evals">Open Evaluations ${icon("arrow-right")}</a>
    </div>
    <pre class="json mt-2">${esc(cli)}</pre>`;
}

function ehrLens(detail) {
  const origin = location.origin;
  const rows = [
    ["REST decide (live)", `POST ${origin}${agentPath()}/decide`],
    ["Shadow mode header", `X-Mendari-Shadow: 1`],
    ["FHIR R4 $decide", `POST ${origin}/fhir/$decide`],
    ...(detail.cds_service ? [["CDS Hooks 2.0 service", `POST ${origin}/cds-services/${detail.cds_service}`]] : []),
    ["CDS Hooks discovery", `GET ${origin}/cds-services`],
    ["SMART App Launch", `${origin}/smart/launch`],
  ];
  return `<p class="muted mt-2">Connect an EHR workqueue, CDS Hooks service, or SMART App Launch directly to this deployment. Send <code>X-Mendari-Shadow: 1</code> during a pilot to hold automation while recording full comparison metrics.</p>
    <div class="ehr-list mt-3">${rows.map(([label, value], idx) => `<div class="ehr-row">
      <span class="ehr-label muted">${esc(label)}</span>
      <code class="break-anywhere">${esc(value)}</code>
      <button type="button" class="icon-button" data-copy-ehr="${idx}" aria-label="Copy ${esc(label)}" title="Copy">${icon("copy")}</button>
    </div>`).join("")}</div>`;
}

function wireCommandCenter(main, brief, detail) {
  const section = main.querySelector(".command-center");
  if (!section) return;
  const bindBody = () => {
    const targetAgent = state.agentId;
    section.querySelector("#shadow-start")?.addEventListener("click", (event) => {
      const btn = event.currentTarget;
      whileBusy(btn, "Starting…", async () => {
        try {
          await api("/api/holds", {
            method: "POST",
            body: JSON.stringify({ agent_id: targetAgent, reason: "shadow pilot: 7-day validation against clinicians" }),
          });
          toast("Shadow pilot started: automation held for clinician comparison");
          if (btn.isConnected && state.agentId === targetAgent) render();
        } catch (error) {
          toastError(error.message);
        }
      });
    });
    section.querySelector("#shadow-stop")?.addEventListener("click", (event) => {
      const btn = event.currentTarget;
      const holdId = btn.dataset.hold;
      actionDialog({
        title: "End shadow pilot?",
        body: "<p>Ending the shadow pilot lifts its automation hold and allows live automation again within this agent's risk policy.</p>",
        action: "End shadow pilot",
        danger: true,
        run: async () => {
          await api(`/api/holds/${encodeURIComponent(holdId)}`, { method: "DELETE" });
          toast("Shadow pilot ended");
          if (btn.isConnected && state.agentId === targetAgent) render();
        },
      });
    });
    section.querySelector("#roi-mins")?.addEventListener("change", (event) => {
      const m = Number(event.target.value) || 3.5;
      storageSet("oh.roi.mins", String(m));
      const out = section.querySelector("#roi-hours");
      if (out) out.textContent = `${((brief.roi.weekly_auto_cases_projected * m) / 60).toFixed(1)} hrs / week`;
    });
    section.querySelector("#brief-download")?.addEventListener("click", (event) => {
      const m = Number(storageGet("oh.roi.mins", "3.5")) || 3.5;
      whileBusy(event.currentTarget, "Building…", () =>
        download(`${agentPath()}/validation-brief?days=30&minutes_per_case=${m}&format=markdown`, `${fileSlug(state.agentId)}-validation-brief.md`));
    });
    section.querySelector("#packet-download")?.addEventListener("click", (event) =>
      whileBusy(event.currentTarget, "Building…", () =>
        download(`${agentPath()}/committee-packet?format=html`, `${fileSlug(state.agentId)}-committee-packet.html`)));
    section.querySelector("#brief-copy")?.addEventListener("click", () => {
      const mins = Number(storageGet("oh.roi.mins", "3.5")) || 3.5;
      const hours = ((brief.roi.weekly_auto_cases_projected * mins) / 60).toFixed(1);
      const summary = `${brief.agent.name} (${brief.agent.id} v${brief.agent.version}) — ${brief.volume.total} local cases (last ${brief.window_days}d), agreement ${shown(brief.figures.agreement)}, automation-ready ${shown(brief.figures.automation_readiness)}, ${brief.volume.down_on_auto} rated wrong of ${brief.volume.automated} automated, ~${hours} clinician hrs/wk reclaimable.`;
      copyText(summary, "Executive summary copied");
    });
    section.querySelector("#export-overrides")?.addEventListener("click", (event) =>
      whileBusy(event.currentTarget, "Exporting…", () =>
        download(`${agentPath()}/feedback/export?source=overrides`, `${fileSlug(state.agentId)}-overrides.jsonl`)
          .catch((err) => toastError(err.message))));
    section.querySelector("#copy-recal")?.addEventListener("click", () => {
      const name = detail.id.split("/").pop();
      copyText(`mendari eval ${detail.id} --dataset ${name}-overrides.jsonl`, "Command copied");
    });
    const ehrValues = [
      `POST ${location.origin}${agentPath()}/decide`,
      "X-Mendari-Shadow: 1",
      `POST ${location.origin}/fhir/$decide`,
      ...(detail.cds_service ? [`POST ${location.origin}/cds-services/${detail.cds_service}`] : []),
      `GET ${location.origin}/cds-services`,
      `${location.origin}/smart/launch`,
    ];
    section.querySelectorAll("[data-copy-ehr]").forEach((btn) =>
      btn.addEventListener("click", () => copyText(ehrValues[Number(btn.dataset.copyEhr)], "Copied")));
  };

  section.querySelectorAll("[data-lens]").forEach((button) =>
    button.addEventListener("click", () => {
      const next = button.dataset.lens;
      storageSet("oh.lens", next);
      section.querySelectorAll("[data-lens]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      $("#cc-body", section).innerHTML = lensBody(brief, detail, next);
      bindBody();
    }));
  bindBody();
}

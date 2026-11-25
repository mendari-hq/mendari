// SPDX-License-Identifier: Apache-2.0
// Agents: one place to see the agents this organization uses, and to find and add more.
// - Our agents (#agents): every agent the organization is served, with where it came from, its
//   version, whether it acts on its own now and what stands in the way, and the service it is
//   connected to (connection.js); then the agents that were added and are not in use yet, each
//   with its next step and who takes it. The actions are the agent store's own (store.js):
//   evaluate, start serving (promote), stop serving, withdraw.
// - Find agents (#agents/hub): the deployment's agent registry (the Agent Hub, or a site's
//   own: the System view's "Agent registry" check names the same one), searched; an agent's listing
//   (#agents/hub/<namespace>/<name>): what the registry checked, apart from what the publisher
//   claims, its recalls, whether evaluations are published, its Decision Card as text; and adding
//   a version to the organization, which the server verifies first (server/hub_routes.py).
// Adding is not approving: an added agent answers nothing until someone else starts serving it,
// and never acts on its own until the organization approves it. Every word that comes from the
// registry, a publisher or a person is escaped; a Decision Card is shown as text.

import { connectionDialog, connectionWords } from "./connection.js";
import { holdsFor, originBadge, agentOptions } from "./manifest.js";
import { views, wire } from "./router.js";
import { $, actionDialog, api, emptyState, esc, focusHeading, icon, may, signedIn, state, time, toast, toastError } from "./ui.js";

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
const short = (digest) => esc(String(digest ?? "").replace("sha256:", "").slice(0, 12));
const digestCode = (digest) => `<code title="${esc(digest)}">${short(digest)}</code>`;
const hubLink = (id) => `#agents/hub/${String(id).split("/").map(encodeURIComponent).join("/")}`;
const orgName = () => state.memberships?.find((m) => m.org === state.me?.org)?.name ?? state.me?.org ?? "your organization";
// A time a registry gave, written as this page writes times; what is not a time is shown as the
// text it is (a registry's answer never stops the page).
const when = (value) => (Number.isNaN(Date.parse(value)) ? String(value ?? "") : time(value));

const TABS = [
  { key: "ours", label: "Our agents", href: "#agents" },
  { key: "hub", label: "Find agents", href: "#agents/hub" },
];

views.agents = async (main, params = []) => {
  const [first, namespace, name] = params;
  if (first === "hub" && namespace && name) return showListing(main, `${namespace}/${name}`);
  const current = first === "hub" ? TABS[1] : TABS[0];
  main.innerHTML = `<h1>Agents</h1>
    <p class="prose lead">The agents ${esc(orgName())} uses, and the agent registry to find more in. Adding an agent never lets it act on
      its own: someone else starts serving it, and your organization approves it before it decides anything alone.</p>
    <nav class="segmented tabs" aria-label="Agents views">${TABS
      .map((t) => `<a href="${t.href}" data-tab="${t.key}"${t === current ? ' aria-current="page"' : ""}>${esc(t.label)}</a>`)
      .join("")}</nav>
    <section id="agents-body" aria-busy="true"></section>`;
  const body = $("#agents-body");
  try {
    if (current.key === "hub") await showHub(body);
    else await showOurs(body);
  } catch (error) {
    body.innerHTML = failed("This could not be read", error.message);
    body.querySelector("[data-retry]")?.addEventListener("click", () => views.agents(main, params));
  } finally {
    body.removeAttribute("aria-busy");
  }
  wire(body);
};

views.agents.agentless = true;
views.agents.leave = () => document.querySelectorAll("dialog[data-admin]:not([data-busy])").forEach((d) => d.close());

const failed = (title, text) => `<div class="panel error-state" role="alert"><h2>${esc(title)}</h2>
  <p class="muted">${esc(text)}</p><button type="button" class="primary" data-retry>Try again</button></div>`;

// -- our agents ----------------------------------------------------------------------------------

async function load() {
  const author = Boolean(state.store) && may("author");
  const promoter = Boolean(state.store) && may("promote");
  const [agents, hub, connections, holds, registry, governance, lineages, jobs, promotions] = await Promise.all([
    api("/api/agents"),
    api("/api/hub"),
    api("/api/connections"),
    api("/api/holds"),
    api("/api/registry-approvals"),
    api("/api/governance"),
    author ? api("/api/store/agents") : null,
    author ? api("/api/store/jobs") : null,
    promoter ? api("/api/store/promotions") : null,
  ]);
  // The agent picker follows what is served now: one that started being served here is there to
  // choose without reloading the page.
  state.agents = agents;
  const picker = $("#agent-select");
  if (picker && agents.length) {
    picker.innerHTML = agentOptions(agents);
    if (agents.some((a) => a.id === state.agentId)) picker.value = state.agentId;
  }
  return { agents, hub, connections, holds, registry, governance, lineages, jobs, promotions, author, promoter };
}

async function showOurs(body) {
  const ctx = await load();
  body.innerHTML = `${servedHtml(ctx)}${waitingHtml(ctx)}${findMore(ctx)}`;
  // After an action the lists are read again, and the keyboard keeps its place: on the control
  // that follows (`next`, a selector) when it is there, else on the heading of its list.
  const again = async (next, heading) => {
    if (!body.isConnected) return;
    await showOurs(body);
    wire(body);
    const control = next ? body.querySelector(next) : null;
    if (control) control.focus();
    else focusHeading(body.querySelector(heading), { scroll: false });
  };
  const quoted = (value) => CSS.escape(value);
  body.querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () => openAgent(button.dataset.open, button.dataset.view)));
  body.querySelectorAll("[data-connect]").forEach((button) => button.addEventListener("click", () =>
    connectionDialog(button.dataset.connect, button.dataset.name).then(() => again(`[data-connect="${quoted(button.dataset.connect)}"]`, "#agents-served"))));
  body.querySelectorAll("[data-stop]").forEach((button) => button.addEventListener("click", () =>
    stopServing(button.dataset.stop, button.dataset.name).then((done) => done && again(null, "#agents-served"))));
  body.querySelectorAll("[data-evaluate]").forEach((button) => button.addEventListener("click", () =>
    evaluate(button.dataset.evaluate, button.dataset.name).then((done) => done && again(`[data-serve="${quoted(button.dataset.evaluate)}"]`, "#agents-waiting"))));
  body.querySelectorAll("[data-serve]").forEach((button) => button.addEventListener("click", () => {
    const version = waiting(ctx).find((v) => v.digest === button.dataset.serve);
    return startServing(version).then((done) => done && again(`[data-agent="${quoted(version.agent_id)}"] [data-open]`, "#agents-served"));
  }));
  body.querySelectorAll("[data-withdraw]").forEach((button) => button.addEventListener("click", () =>
    withdraw(button.dataset.withdraw, button.dataset.name).then((done) => done && again(null, "#agents-waiting"))));
}

// Choose an agent in the page's picker and open one of its views.
function openAgent(id, view) {
  const picker = $("#agent-select");
  if (picker && picker.value !== id) {
    picker.value = id;
    picker.dispatchEvent(new Event("change"));
  }
  location.hash = `#${view}`;
}

// Whether a served agent acts on its own now, and what stands in the way: each of the engine's own
// conditions (core/decision.py, core/policy.py), read from what the page already asks.
function standing(agent, ctx) {
  const why = [];
  const level = agent.risk?.level ?? 0;
  if (level >= 4) why.push("At risk level 4 a person reviews every decision.");
  if (holdsFor(ctx.holds, agent).length) why.push("Automation is on hold.");
  const promotion = ctx.promotions?.promotions.find((p) => p.agent_id === agent.id && !p.retired_at);
  if (promotion && !promotion.automate) why.push("It is served to recommend only.");
  if (agent.origin === "hub" && !ctx.registry.approvals.some((a) => a.agent_id === agent.id && a.agent_digest === agent.digest && !a.revoked_at)) {
    why.push("Your organization has not approved this exact version yet.");
  }
  const approved = ctx.governance.approvals.some((a) => a.agent_id === agent.id && a.agent_digest === agent.digest && !a.revoked_at && new Date(a.expires_at) > new Date());
  if (level >= 2 && level < 4 && !approved) why.push(`At risk level ${level} it needs your organization's approval to act on its own.`);
  const connection = ctx.connections.agents.find((c) => c.agent_id === agent.id);
  if (connection?.applies && connection.canned && signedIn()) why.push("It is not connected to its service, so it answers from canned examples.");
  return why.length
    ? { label: "A person decides every case", tone: "s-warning", why }
    : { label: "Acts on its own within its policy", tone: "s-ok", why: [] };
}

function servedHtml(ctx) {
  const cards = ctx.agents.map((agent, index) => {
    const found = standing(agent, ctx);
    const connection = ctx.connections.agents.find((c) => c.agent_id === agent.id);
    const words = connectionWords(connection);
    const own = connection?.source === "organization" ? connection.connection : null;
    const promotion = ctx.promotions?.promotions.find((p) => p.agent_id === agent.id && !p.retired_at);
    const selected = agent.id === state.agentId;
    return `<article class="panel agent-card" aria-labelledby="agent-${index}" data-agent="${esc(agent.id)}">
      <div class="row agent-head">
        <h3 id="agent-${index}">${esc(agent.name)}</h3>
        ${originBadge(agent)}
        <span class="badge ${found.tone}" data-standing>${esc(found.label)}</span>
      </div>
      <p class="mono muted">${esc(agent.id)} v${esc(agent.version)} · ${digestCode(agent.digest)}${selected ? " · shown in the other views" : ""}</p>
      ${found.why.length ? `<ul class="agent-why">${found.why.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>` : ""}
      <dl class="facts">
        <dt>Connection</dt><dd><span class="badge ${words.tone}">${esc(words.text)}</span>${
          own ? ` <span class="muted">to <code class="break-anywhere">${esc(own.origin)}</code>, set ${esc(time(own.set_at))} by ${esc(own.set_by)}</span>` : ""}</dd>
        ${promotion ? `<dt>Served since</dt><dd>${esc(time(promotion.created_at))} <span class="muted">(${esc(promotion.reference)})</span></dd>` : ""}
      </dl>
      <div class="row agent-actions">
        <button type="button" class="primary" data-open="${esc(agent.id)}" data-view="overview">Open ${icon("arrow-right")}</button>
        <button type="button" data-open="${esc(agent.id)}" data-view="card">Decision Card</button>
        ${connection?.applies ? `<button type="button" data-connect="${esc(agent.id)}" data-name="${esc(agent.name)}">Connection…</button>` : ""}
        ${promotion && ctx.promoter ? `<button type="button" data-stop="${esc(promotion.id)}" data-name="${esc(agent.name)}">Stop serving…</button>` : ""}
      </div>
    </article>`;
  }).join("");
  return `<h2 id="agents-served">In use</h2>
    <p class="muted">The agents that answer requests made in ${esc(orgName())}.</p>
    ${cards || emptyState({ icon: "package", title: "No agent is in use", text: "Find one in the agent registry, under Find agents.", level: 3 })}`;
}

// The versions of the organization's store that are not served: what an author sees of them, and
// what someone who may start serving them does.
function waiting(ctx) {
  const found = new Map();
  for (const lineage of ctx.lineages ?? []) {
    for (const v of lineage.versions) if (["draft", "retired"].includes(v.state)) found.set(v.digest, { ...v });
  }
  for (const v of ctx.promotions?.versions ?? []) found.set(v.digest, { ...(found.get(v.digest) ?? {}), ...v, candidate: true });
  const done = (digest) => (ctx.jobs ?? []).filter((j) => !j.agent && j.digest === digest && j.state === "done").map((j) => j.report_id);
  return [...found.values()].map((v) => ({ ...v, reports: v.reports ?? done(v.digest) }))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
}

function step({ done, current, title, text, action = "" }, index) {
  return `<li class="adoption-step ${done ? "done" : "todo"}${current ? " current" : ""}"${current ? ' aria-current="step"' : ""}>
    <span class="adoption-mark" aria-hidden="true">${done ? icon("check") : index + 1}</span>
    <div class="adoption-body"><p class="adoption-title"><b>${esc(title)}</b>
      <span class="sr-only">${done ? "(done)" : current ? "(next step)" : "(to do)"}</span></p>
      <p class="muted">${text}</p></div>
    ${action}
  </li>`;
}

function provenance(v) {
  const p = v.pulled;
  if (!p) return v.created_at ? `<p class="muted">Made ${esc(time(v.created_at))} in the agent store.</p>` : "";
  const counter = p.accepted === true ? ", and the registry's counter-signature" : "";
  return `<p class="muted">Added from <span class="break-anywhere">${esc(p.registry)}</span> on ${esc(when(v.created_at ?? p.pulled_at))}.
    Verified here before it was kept: the archive, the publisher's signature (key <span class="mono">${esc(p.key_id)}</span>)${counter}.</p>`;
}

function waitingHtml(ctx) {
  if (!state.store) return "";
  if (!ctx.author && !ctx.promoter) {
    return `<h2 id="agents-waiting">Added, not in use yet</h2>
      <p class="muted">Shown to agent authors, who add and evaluate agents, and to those who may start serving them (Governance, Administrator).</p>`;
  }
  const versions = waiting(ctx);
  if (!versions.length) {
    return `<h2 id="agents-waiting">Added, not in use yet</h2>
      <p class="muted">Nothing is waiting. An agent added from the registry shows here until someone starts serving it.</p>`;
  }
  const cards = versions.map((v, index) => {
    const name = `${v.agent_id} ${v.version}`;
    const evaluated = v.reports.length > 0;
    const fromHub = v.source === "hub";
    const serve = !evaluated
      ? ""
      : v.may_promote === false
        ? '<span class="muted adoption-action">Someone other than you: you added it.</span>'
        : ctx.promoter
          ? `<button type="button" class="primary adoption-action" data-serve="${esc(v.digest)}">Start serving…</button>`
          : '<span class="muted adoption-action">Governance or an administrator.</span>';
    const steps = [
      { done: true, title: fromHub ? "Added and verified" : "Made in the agent store",
        text: v.valid === false ? "It does not validate: it cannot be served as it is." : "It is kept for your organization and answers no request yet." },
      { done: evaluated, current: !evaluated, title: "Evaluate it here",
        text: evaluated ? `${v.reports.length} evaluation${v.reports.length === 1 ? "" : "s"} of this exact version ${v.reports.length === 1 ? "is" : "are"} kept.`
          : "Run it on a labelled dataset, so whoever starts serving it has your own figures to rely on.",
        action: evaluated ? "" : ctx.author
          ? `<button type="button" class="primary adoption-action" data-evaluate="${esc(v.digest)}" data-name="${esc(name)}">Evaluate…</button>`
          : '<span class="muted adoption-action">An agent author.</span>' },
      { done: false, current: evaluated, title: "Start serving it",
        text: "Someone who did not add it decides that your organization uses it. It then answers requests, and a person still decides every case.",
        action: serve },
      { done: false, title: "Approve it to act on its own",
        text: "Once it is served: on its Decision Card, your organization approves this exact version. Until then it never automates." },
    ];
    return `<article class="panel agent-card" aria-labelledby="waiting-${index}" data-waiting="${esc(v.digest)}">
      <div class="row agent-head">
        <h3 id="waiting-${index}"><code>${esc(v.agent_id)}</code> ${esc(v.version)}</h3>
        <span class="badge">${v.state === "retired" ? "No longer served" : "Not in use yet"}</span>
        ${digestCode(v.digest)}
      </div>
      ${provenance(v)}
      <ol class="adoption-steps">${steps.map(step).join("")}</ol>
      <div class="row agent-actions">
        ${ctx.author ? `<a class="button" href="#store/version/${encodeURIComponent(v.digest)}">Files, tests and Decision Card</a>` : ""}
        <button type="button" data-connect="${esc(v.agent_id)}" data-name="${esc(name)}">Connection…</button>
        ${ctx.author && v.state === "draft" ? `<button type="button" data-withdraw="${esc(v.digest)}" data-name="${esc(name)}">Withdraw…</button>` : ""}
      </div>
    </article>`;
  }).join("");
  return `<h2 id="agents-waiting">Added, not in use yet</h2>
    <p class="muted">Kept for ${esc(orgName())} and answering nothing. Each shows its next step and who takes it.</p>${cards}`;
}

function findMore(ctx) {
  const where = ctx.hub.registry ? `on <span class="break-anywhere">${esc(ctx.hub.registry)}</span>` : "in the agent registry";
  return `<p class="mt-4"><a class="button" href="#agents/hub">${icon("search")} Find an agent ${where}</a></p>`;
}

async function evaluate(digest, name) {
  let version;
  try {
    version = await api(`/api/store/versions/${encodeURIComponent(digest)}`);
  } catch (error) {
    toastError(error.message);
    return null;
  }
  const datasets = (version.checks?.datasets ?? []).map((d) => d.dataset).filter(Boolean);
  if (!datasets.length) {
    await actionDialog({
      title: "This agent carries no dataset",
      body: `<p>${esc(name)} declares no labelled dataset to evaluate on. Upload one of your own in the Agent store, under Results, and run a job there.</p>`,
      action: "Open the Agent store",
      run: async () => { location.hash = "#store/results"; },
    });
    return null;
  }
  return actionDialog({
    title: `Evaluate ${name}?`,
    body: `<p>The agent decides every case of the dataset here, on this server, and the figures are kept for your organization.
        Nothing is sent to the registry.</p>
      <div class="agent-fields"><label for="evaluate-dataset">Dataset</label>
      <select id="evaluate-dataset" name="labelled">${datasets.map((d) => `<option>${esc(d)}</option>`).join("")}</select></div>
      <p class="muted" data-progress aria-live="polite"></p>`,
    action: "Evaluate",
    run: async (form) => {
      const progress = form.querySelector("[data-progress]");
      let job = await post("/api/store/jobs", { version: digest, dataset: { name: form.labelled.value } });
      const id = encodeURIComponent(job.id);
      for (let i = 0; i < 2000 && (job.state === "queued" || job.state === "running"); i += 1) {
        progress.textContent = `${job.done} of ${job.total} cases decided…`;
        job = await post(`/api/store/jobs/${id}/advance`, {});
      }
      if (job.state !== "done") throw new Error(`The evaluation stopped: ${job.error ?? job.state}.`);
      toast("Evaluated: its report is kept");
      return job;
    },
  });
}

function startServing(v) {
  if (!v) return Promise.resolve(null);
  const fromHub = v.source === "hub";
  return actionDialog({
    title: `Start serving ${v.agent_id} ${v.version}?`,
    body: `<p>From now on this version answers every request for this agent made in <b>${esc(orgName())}</b>, by people, keys, clients and
        the EHR, and in no other organization. The version served before it, if any, stops.</p>
      <p>${fromHub
        ? "It still never acts on its own: a person decides every case until your organization approves this exact version on its Decision Card."
        : "It acts on its own only where its risk level and your organization's approvals allow."}</p>
      <div class="agent-fields"><fieldset><legend>Evaluations relied on</legend>${v.reports.map((r, i) =>
        `<label><input type="checkbox" name="reports" value="${esc(r)}"${i === 0 ? " checked" : ""}> Report <code>${esc(String(r).slice(0, 8))}</code></label>`).join("")}</fieldset>
      <label for="serve-reference">Reference (where this decision is recorded)</label>
      <input id="serve-reference" name="reference" required minlength="3" maxlength="500" placeholder="e.g. AI committee minutes, 2026-11-23, item 2">
      <label><input type="checkbox" name="recommend"> Recommend only: this version never acts on its own, whatever is approved later</label></div>`,
    action: "Start serving",
    run: (form) => {
      const reports = [...form.querySelectorAll('input[name="reports"]:checked')].map((input) => input.value);
      const reference = form.reference.value.trim();
      if (!reports.length) throw new Error("Choose at least one evaluation to rely on.");
      if (reference.length < 3) throw new Error("Type where this decision is recorded.");
      return post("/api/store/promotions", { version: v.digest, reports, reference, automate: !form.recommend.checked });
    },
  }).then((made) => {
    if (made) toast("Now serving: it shows under In use");
    return made;
  });
}

function stopServing(promotionId, name) {
  return actionDialog({
    title: `Stop serving ${name}?`,
    body: `<p>${esc(orgName())} is then served no version of this agent until another is started. Its decisions and its audit trail stay.</p>`,
    action: "Stop serving",
    danger: true,
    run: () => post(`/api/store/promotions/${encodeURIComponent(promotionId)}/retire`, {}),
  });
}

function withdraw(digest, name) {
  return actionDialog({
    title: `Withdraw ${name}?`,
    body: "<p>It is taken out of the list and can no longer be evaluated or served. Its record stays in the audit trail; its files are freed after 30 days.</p>",
    action: "Withdraw",
    danger: true,
    run: () => api(`/api/store/versions/${encodeURIComponent(digest)}`, { method: "DELETE" }),
  });
}

// -- the agent registry --------------------------------------------------------------------------

const ADD_REASON = {
  no_registry: "No agent registry is set on this deployment, so there is nothing to search. Whoever runs it names one (MENDARI_REGISTRY_URL); the System view's Agent registry check says how.",
  no_store: "You can read what the registry lists here. Adding an agent from this page is off on this deployment: it needs the agent store (MENDARI_AGENT_STORE=on, with people managed as memberships). Whoever runs it turns that on.",
};

// Why this person cannot add from here, or null when they can.
function cannotAdd(hub) {
  if (!hub.add.available) return ADD_REASON[hub.add.reason] ?? "Adding an agent is off on this deployment.";
  if (!may("author")) return "Adding an agent needs the Agent author role. An administrator gives it under Access, Members (to themself too).";
  return null;
}

const riskBadge = (risk) => (Number.isInteger(risk?.level)
  ? `<span class="badge risk" title="Declared by the publisher">Risk ${esc(risk.level)} (declared)</span>` : "");
const publisherBadge = (type) => (type ? `<span class="badge" title="Set by the registry's reviewers for this publisher">${esc(type)} publisher</span>` : "");
const recallBadge = (recall) => (recall ? `<span class="badge o-BLOCK">${recall.deleted ? "Deleted" : "Recalled"}</span>` : "");

function installedWords(installed) {
  if (!installed) return "";
  if (installed.state === "served") return `Your organization uses version ${esc(installed.version)}.`;
  return `Your organization added version ${esc(installed.version)}; it is not in use yet.`;
}

async function showHub(body) {
  const hub = await api("/api/hub");
  if (!hub.registry) {
    body.innerHTML = emptyState({ icon: "search", title: "No agent registry is set", text: esc(ADD_REASON.no_registry), level: 2 });
    return;
  }
  const blocked = cannotAdd(hub);
  body.innerHTML = `<form class="row hub-search" id="hub-search" role="search" novalidate>
      <label class="sr-only" for="hub-q">Search the agent registry</label>
      <input id="hub-q" name="q" type="search" maxlength="200" autocomplete="off" placeholder="Search by name, purpose or question" class="grow">
      <button type="submit" class="primary">${icon("search")} Search</button>
    </form>
    <p class="muted">Searching <span class="break-anywhere">${esc(hub.registry)}</span>. What it lists is the registry's and each publisher's own words.</p>
    ${blocked ? `<p class="callout neutral" id="hub-cannot-add">${esc(blocked)}</p>` : ""}
    <div id="hub-results" aria-live="polite" aria-busy="true"></div>`;
  const results = $("#hub-results");
  let latest = 0;
  const search = async (q) => {
    const seq = ++latest;
    results.setAttribute("aria-busy", "true");
    results.innerHTML = '<p class="muted">Searching…</p>';
    try {
      const found = await api(`/api/hub/search?q=${encodeURIComponent(q)}`);
      if (seq !== latest || !results.isConnected) return;
      results.innerHTML = resultsHtml(found, q);
    } catch (error) {
      if (seq !== latest || !results.isConnected) return;
      results.innerHTML = failed("The registry could not be read", error.message);
      results.querySelector("[data-retry]").addEventListener("click", () => search(q));
    } finally {
      if (seq === latest) results.removeAttribute("aria-busy");
    }
  };
  $("#hub-search").addEventListener("submit", (event) => {
    event.preventDefault();
    search(event.target.q.value.trim());
  });
  await search("");
}

function resultsHtml(found, q) {
  if (!found.agents.length) {
    return emptyState({
      icon: "search",
      title: q ? "No agent matches" : "The registry lists no agent yet",
      text: q ? "Try fewer or other words." : "Nothing is published there yet.",
      level: 2,
    });
  }
  const cards = found.agents.map((a, index) => `<article class="panel agent-card" aria-labelledby="hub-${index}" data-hub="${esc(a.id)}">
      <div class="row agent-head">
        <h3 id="hub-${index}"><a href="${hubLink(a.id)}">${esc(a.name ?? a.id)}</a></h3>
        ${recallBadge(a.recall)}${publisherBadge(a.checked.publisher_type)}${riskBadge(a.claimed.risk)}
      </div>
      <p class="mono muted">${esc(a.id)} v${esc(a.version)}</p>
      <p class="prose">${esc(a.summary ?? "")}</p>
      <p class="muted">${esc(a.checked.status_label ?? "Not clinically reviewed.")}${a.standards.length ? ` · ${a.standards.map(esc).join(" · ")}` : ""}</p>
      ${a.installed ? `<p class="callout neutral">${installedWords(a.installed)}</p>` : ""}
    </article>`).join("");
  return `<p class="muted">${found.agents.length} agent${found.agents.length === 1 ? "" : "s"}${found.more ? ", the first of more: narrow the search" : ""}.</p>${cards}`;
}

// A Decision Card as the registry rendered it, to read as text: the backslashes Markdown puts
// before punctuation are taken out, and nothing else is changed. It is never parsed: the page
// sets it as text.
const plainCard = (markdown) => String(markdown).replace(/\\([!-/:-@[-`{-~])/g, "$1");

function reviewWords(review, declared) {
  if (review.status !== "reviewed") return "Not reviewed by the registry.";
  if (review.matches_declared === false) return `Reviewed: the registry found risk level ${esc(review.risk_level)}, not the ${esc(declared)} the publisher declares.`;
  return `Reviewed by the registry at risk level ${esc(review.risk_level)}.`;
}

async function showListing(main, agentId) {
  main.innerHTML = `<p><a href="#agents/hub">← Find agents</a></p><h1>${esc(agentId)}</h1><section id="agents-body" aria-busy="true"><p class="muted">Reading the registry…</p></section>`;
  const body = $("#agents-body");
  let hub;
  let found;
  try {
    [hub, found] = await Promise.all([api("/api/hub"), api(`/api/hub/agents/${agentId.split("/").map(encodeURIComponent).join("/")}`)]);
  } catch (error) {
    body.removeAttribute("aria-busy");
    body.innerHTML = failed(error.status === 404 ? "The registry lists no such agent" : "The registry could not be read", error.message);
    body.querySelector("[data-retry]").addEventListener("click", () => showListing(main, agentId));
    return focusHeading(main.querySelector("h1"), { scroll: false });
  }
  body.removeAttribute("aria-busy");
  const a = found.agent;
  const recalls = found.recalled.length
    ? `<section class="panel" id="hub-recalled"><h2>Recalled versions</h2><ul>${found.recalled.map((r) =>
        `<li><b>${esc(r.version)}</b>: ${r.deleted ? "deleted" : `recalled (${esc(r.severity)})`}${r.reason ? `. ${esc(r.reason)}` : ""}</li>`).join("")}</ul>
        <p class="muted">A recalled version is not added, and one already in use stops acting on its own.</p></section>`
    : "";
  if (!a) {
    body.innerHTML = `<p class="callout danger"><b>Every version of this agent is recalled.</b> It cannot be added.</p>${recalls}`;
    return focusHeading(main.querySelector("h1"), { scroll: false });
  }
  main.querySelector("h1").textContent = a.name ?? agentId;
  const c = a.checked;
  const evaluations = a.evaluations.published
    ? `<p>${a.evaluations.published} evaluation${a.evaluations.published === 1 ? "" : "s"} published by the publisher for this version:</p>
       <ul>${a.evaluations.listed.map((e) => `<li>${esc(e.dataset ?? "a dataset")}${e.cases != null ? `, ${esc(e.cases)} cases` : ""}${e.models.length ? `, with ${e.models.map(esc).join(", ")}` : ""}${e.rerun ? `. ${esc(e.rerun)}` : ""}</li>`).join("")}</ul>
       <p class="muted">The figures, with their intervals, are on the registry's own page. They are the publisher's: evaluate it on your own cases too.</p>`
    : `<p class="muted">The publisher has published no evaluation of this version. Evaluate it on your own cases before relying on it.</p>`;
  body.innerHTML = `<p class="mono muted">${esc(agentId)} v${esc(a.version)}${a.published_at ? ` · published ${esc(when(a.published_at))}` : ""}</p>
    ${a.recall ? `<p class="callout danger"><b>${a.recall.deleted ? "Deleted from the registry" : `Recalled (${esc(a.recall.severity)})`}.</b> ${esc(a.recall.reason ?? "")} It cannot be added.</p>` : ""}
    <p class="prose lead">${esc(a.summary ?? "")}</p>
    <p class="callout warning status-notice"><b>${esc(c.status_label ?? "Not clinically reviewed.")}</b></p>
    <section class="panel" id="hub-add" aria-labelledby="hub-add-title"><h2 id="hub-add-title">Add to ${esc(orgName())}</h2>${addHtml(found, hub)}
      <div id="hub-add-result" aria-live="polite"></div></section>
    <div class="grid-2 hub-facts">
      <section class="panel" id="hub-checked"><h2>Checked by the registry</h2>
        <dl class="facts">
          <dt>Signature</dt><dd>${c.signature.verified ? "Verified when it was published" : "Not verified"}${c.signature.key_id ? `, key <span class="mono break-anywhere">${esc(c.signature.key_id)}</span>` : ""}. Checked again here when you add it.</dd>
          <dt>Counter-signature</dt><dd>${c.countersigned ? "Signed by the registry itself" : "None: this registry signs nothing itself"}</dd>
          <dt>Publisher</dt><dd>${c.publisher_type ? `${esc(c.publisher_type)}, as the registry's reviewers set it` : "Not checked by the registry"}</dd>
          <dt>Review</dt><dd>${reviewWords(c.review, a.claimed.risk.level)}</dd>
          <dt>Digest</dt><dd><code class="break-anywhere">${esc(c.digest ?? "")}</code></dd>
        </dl></section>
      <section class="panel" id="hub-claimed"><h2>Claimed by the publisher</h2>
        <p class="muted">What the publisher says of the agent. The registry has not checked it.</p>
        <dl class="facts">
          <dt>Publisher</dt><dd>${esc(a.claimed.publisher.name ?? "Not stated")}${a.claimed.publisher.type ? ` (${esc(a.claimed.publisher.type)}, self-declared)` : ""}</dd>
          <dt>Risk level</dt><dd>${a.claimed.risk.level != null ? `${esc(a.claimed.risk.level)}${a.claimed.risk.category ? `, ${esc(a.claimed.risk.category)}` : ""}` : "Not stated"}</dd>
          <dt>Licence</dt><dd>${esc(a.claimed.license ?? "Not stated")}</dd>
          <dt>Standards</dt><dd>${a.standards.length ? a.standards.map(esc).join(", ") : "None stated"}</dd>
        </dl></section>
    </div>
    <section class="panel" id="hub-evaluations"><h2>Evaluations</h2>${evaluations}</section>
    ${recalls}
    <section class="panel"><h2>Decision Card</h2>
      <p class="muted">Rendered by the registry from the agent's files, and shown here as text.</p>
      <pre id="hub-card" class="registry-card" tabindex="0"></pre></section>`;
  $("#hub-card").textContent = a.card_markdown ? plainCard(a.card_markdown) : "The registry rendered no card for this version.";
  $("#hub-add-button")?.addEventListener("click", () => add(agentId, a, found));
  wire(body);
  return focusHeading(main.querySelector("h1"), { scroll: false });
}

function addHtml(found, hub) {
  const a = found.agent;
  // What the organization has of this agent already: this very version (nothing to add), or
  // another one (said, and this one can still be added).
  const has = found.installed
    ? `<p class="callout neutral">${installedWords(found.installed)} <a href="#agents">See it under Our agents.</a></p>` : "";
  if (found.installed && (found.installed.digest === a.checked.digest || found.installed.version === a.version)) return has;
  if (a.recall) return `${has}<p class="muted">A recalled version is never added.</p>`;
  const blocked = cannotAdd(hub);
  if (blocked) return `${has}<p class="callout neutral">${esc(blocked)}</p>`;
  return `${has}<p>Adding ${found.installed ? "this version" : "it"} downloads it, checks it against the registry's record and the publisher's
      signature, and keeps it for your organization. It answers nothing until someone else starts serving it, and never acts on its own until
      your organization approves it.</p>
    <button type="button" class="primary" id="hub-add-button">Add version ${esc(a.version)}…</button>`;
}

// The store's own refusals come with more than words: a changed signing key names both keys, and
// files that read as a person's details come with where (never what).
async function pull(ref, acceptNewKey) {
  try {
    return { ok: true, body: await post("/api/store/pulls", { ref, accept_new_key: acceptNewKey }) };
  } catch (error) {
    if (error.body?.key_changed || error.body?.phi) return { ok: false, body: error.body };
    throw error;
  }
}

async function add(agentId, a, found) {
  const ref = `${agentId}@${a.version}`;
  const result = $("#hub-add-result");
  const ask = (acceptNewKey, keys) => actionDialog({
    title: acceptNewKey ? "Accept this publisher's new key and add?" : `Add ${ref}?`,
    body: acceptNewKey
      ? `<p>This version is signed with a key your organization has not seen from this publisher before:</p>
         <dl class="facts"><dt>New key</dt><dd class="mono break-anywhere">${esc(keys.key_id)}</dd>
           <dt>Seen before</dt><dd class="mono break-anywhere">${keys.pinned.length ? keys.pinned.map(esc).join(", ") : "none recorded"}</dd></dl>
         <p>A publisher changes its key now and then, and so does someone pretending to be it. Check the new key with the publisher before you accept it.</p>`
      : `<p>This server downloads <b>${esc(ref)}</b> from the registry and checks it before keeping it for <b>${esc(orgName())}</b>.</p>
         <p>It is then kept and <b>not in use</b>: it answers no request until someone other than you starts serving it.</p>`,
    action: acceptNewKey ? "Accept the key and add" : "Add",
    run: () => pull(ref, acceptNewKey),
  });
  let answer = await ask(false);
  if (answer && !answer.ok && answer.body.key_changed) answer = await ask(true, answer.body.key_changed);
  if (!answer) return;
  if (!answer.ok && answer.body.phi) {
    const rows = answer.body.phi.findings.map((f) => `<li>${esc(f.kind)} in <code>${esc(f.file)}</code>${f.line ? `, line ${esc(f.line)}` : ""}</li>`).join("");
    result.innerHTML = `<div class="callout danger" role="alert"><p><b>Not added: some of its files read as a person's details.</b> Where each was found (never what):</p><ul>${rows}</ul></div>`;
    return;
  }
  if (!answer.ok) {
    result.innerHTML = `<p class="callout danger" role="alert">${esc(answer.body.detail ?? "It was not added.")}</p>`;
    return;
  }
  $("#hub-add-button")?.remove();
  result.innerHTML = `<div class="callout ok" role="status"><p><b>Added and verified.</b> ${esc(ref)} is kept for ${esc(orgName())}, and is not in use yet.</p>
        <p>Next: evaluate it here, then someone other than you starts serving it. <a href="#agents">See it under Our agents.</a></p></div>`;
  toast("Added: not in use yet");
  found.installed = { version: a.version, digest: answer.body.digest, state: answer.body.state };
}

// SPDX-License-Identifier: Apache-2.0
// Decision Card: the agent as a document, sections linked as routes ("#card/policy").

import { cardConnection } from "./connection.js";
import { effectBadge, holdsFor, originBadge, providerWords, statusNotice } from "./manifest.js";
import { render, views } from "./router.js";
import { $, actionDialog, api, copyText, esc, focusHeading, icon, may, outcomeBadge, priorityBadge, shortTime, signedIn, slotText, state, time, toast, toastError, whileBusy } from "./ui.js";

function questionPanel(key, q, primary) {
  const kind = q.type && q.type !== "choice" ? ` <span class="chip">${esc(q.type)}</span>` : "";
  const labels = q.type === "score" ? Object.keys(q.levels).sort((a, b) => q.levels[a].score - q.levels[b].score) : Object.keys(q.criteria);
  const score = (label) => (q.type === "score" ? `<td class="mono">${esc(q.levels[label].score)}</td>` : "");
  return `<div class="panel question"><h3>Question <code>${esc(key)}</code>${primary ? " (the decision)" : ""}${kind}</h3>
    <p class="muted">${esc(q.instructions)}</p>
    <table><thead><tr><th scope="col">Label</th>${q.type === "score" ? `<th scope="col">Score</th>` : ""}<th scope="col">Means</th></tr></thead>
      <tbody>${labels.map((label) => `<tr><td><code>${esc(label)}</code></td>${score(label)}<td>${esc(q.criteria[label])}</td></tr>`).join("")}</tbody></table></div>`;
}

// A policy condition in words: "the chance that urgency is urgent is at least 0.35".
function conditionText(when) {
  const q = `<code>${esc(when.question)}</code>`;
  const labels = (when.labels ?? []).map((l) => `<code>${esc(l)}</code>`).join(" or ");
  const known = (v) => v !== null && v !== undefined;
  const parts = [];
  if (known(when.min_probability)) parts.push(`the chance that ${q} is ${labels} is at least ${esc(when.min_probability)}`);
  else if (labels) parts.push(`${q} is ${labels}`);
  if (known(when.min_score)) parts.push(`the expected score of ${q} is at least ${esc(when.min_score)}`);
  if (known(when.max_score)) parts.push(`the expected score of ${q} is at most ${esc(when.max_score)}`);
  const confidence = when.min_confidence ? `, with confidence at least ${esc(when.min_confidence)}` : "";
  return parts.join(" and ") + confidence;
}

// Whether this deployment automates at all, and when. The thresholds say when a decision may be
// automated; the caps in core/policy.py (risk level, required providers) can rule it out entirely,
// and at levels 2-3 only the organization's approval of this exact version lifts the risk cap. An
// agent from a registry also needs its organization's approval of its digest (`registry`), and
// never automates while a recall its last pull recorded stands (core/decision.py `_origin_holds`).
function automationText(d, governance, registry) {
  const p = d.manifest.policy;
  const code = (v) => `<code>${esc(v)}</code>`;
  const review = `A person reviews from confidence ${p.thresholds.review.toFixed(2)}; below that it abstains.`;
  if (d.risk.level >= 4) return `Never: at risk level 4 a person reviews every decision. ${review}`;
  if (registry?.recalled) return `Not while its registry's recall stands: a person reviews every decision. ${review}`;
  if (registry && !registry.active) {
    return `Not without an approval: this agent came from a registry, so a person reviews every decision until your organization approves this exact version (see Automation approval, below). ${review}`;
  }
  if (d.risk.level >= 2 && !governance.active) {
    return `Not without an approval: at risk level ${d.risk.level} a person reviews every decision unless your organization approves automation for this version (see Automation approval, below). ${review}`;
  }
  const unmet = Object.entries(p.automation_requires ?? {}).filter(([slot, allowed]) => !allowed.includes(d.active_providers[slot]?.variant));
  if (unmet.length) {
    return `Not with the deployed providers: automation needs ${unmet.map(([slot, allowed]) =>
      `${code(slot)} = ${allowed.map(code).join(" or ")} (deployed: ${code(d.active_providers[slot]?.variant ?? "none")})`).join(" and ")}. ${review}`;
  }
  const labels = Object.values(p.labels);
  const higher = labels.some((l) => l.allow_auto && (l.auto_threshold ?? 0) > p.thresholds.auto);
  const never = labels.some((l) => !l.allow_auto);
  const exceptions = [higher && "some labels need more", never && "some are never automated"].filter(Boolean).join(", ");
  const approved = governance.active ? `Approved by ${esc(governance.active.approver)}: ` : "";
  return `${approved}At confidence ${p.thresholds.auto.toFixed(2)} or more${exceptions ? ` (${exceptions}; see Policy)` : ""}. ${review}`;
}

// Where this agent runs: one row each time its version, providers or release changed somewhere.
// In memberships mode they are the deployment operator's (null: not served, ADR-0008).
function deploymentsSection(rows) {
  if (rows === null) return `<p class="prose muted">Kept by the deployment's operator: where and how this agent runs is recorded on the server, not shown to each organization.</p>`;
  if (!rows.length) return `<p class="prose muted">Not recorded yet: a server records itself the first time it uses the agent.</p>`;
  const providers = (map) => Object.entries(map).map(([slot, variant]) => `<code>${esc(slot)}</code> ${esc(variant)}`).join(", ");
  return `<div class="panel table-wrap"><table>
    <caption class="sr-only">Deployments of this agent, newest first</caption>
    <thead><tr><th scope="col">Since</th><th scope="col">Environment</th><th scope="col">Version</th><th scope="col">Providers</th><th scope="col">Release</th></tr></thead>
    <tbody>${rows.map((r) => `<tr><td class="nowrap"><time datetime="${esc(r.started_at)}" title="${esc(time(r.started_at))}">${esc(shortTime(r.started_at))}</time></td>
      <td>${esc(r.environment)}</td>
      <td>v${esc(r.agent_version)} <span class="mono muted">${esc(r.agent_digest.slice(7, 19))}</span></td>
      <td>${providers(r.providers)}</td><td class="mono">${esc(r.release)}</td></tr>`).join("")}</tbody>
  </table></div>`;
}

const CARD_SECTIONS = [
  ["facts", "At a glance"],
  ["use", "Intended use"],
  ["questions", "Questions"],
  ["policy", "Policy"],
  ["actions", "Actions"],
  ["approval", "Automation approval"],
  ["deployments", "Deployments"],
  ["providers", "Providers"],
  ["connection", "Connection"],
  ["schemas", "Schemas"],
];

// The deployment's records are its operator's (ADR-0008). In memberships mode its operators
// alone read them (`operate`); in claims mode the server says whose they are: every
// organization's, unless the deployment names the one that operates it, and then a 404 for the
// others. Null: not this window's to read, and the card says so.
const deploymentRecords = (query) => (state.accounts === "memberships" && !may("operate")
  ? null
  : api(`/api/deployments?${query}`).catch((error) => (error.status === 404 ? null : Promise.reject(error))));

// The card is a document: a table of contents whose links are routes ("#card/policy"), prose at a
// readable width, facts as a definition list, and the machine-facing schemas folded away.
views.card = async (main, params) => {
  const d = state.detail;
  const m = d.manifest;
  const p = m.policy;
  const query = `agent_id=${encodeURIComponent(state.agentId)}`;
  const [governance, deployments, holds, registry] = await Promise.all([
    api(`/api/governance?${query}`),
    deploymentRecords(query),
    api("/api/holds"),
    fromRegistry(d) ? api(`/api/registry-approvals?${query}`) : null,
  ]);
  const list = (items) => `<ul>${items.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>`;
  main.innerHTML = `
    <h1>${esc(m.name)}</h1>
    <p class="prose lead">${esc(m.summary)}</p>
    ${statusNotice(d)}
    <nav class="toc" aria-label="On this page">${CARD_SECTIONS.filter(([key]) => key !== "actions" || d.actions?.length).map(([key, title]) => `<a href="#card/${key}">${esc(title)}</a>`).join("")}</nav>

    <section class="card-section" id="card-facts" aria-labelledby="card-facts-title">
      <h2 id="card-facts-title">At a glance</h2>
      <dl class="facts">
        <dt>Source</dt><dd>${originBadge(d)}</dd>
        <dt>Risk level</dt><dd><span class="badge risk">${m.risk.level}</span> ${esc(m.risk.category)}</dd>
        <dt>Decides</dt><dd><code>${esc(p.primary)}</code>: ${d.labels.map((l) => `<code>${esc(l)}</code>`).join(", ")}</dd>
        <dt>Automates</dt><dd>${automationText(d, governance, registry)}</dd>
        <dt>CDS Hooks</dt><dd>${d.cds_service ? `service <code>${esc(d.cds_service)}</code> on <code>${esc(m.cds_hooks.hook)}</code>` : "none"}</dd>
        <dt>Publisher</dt><dd>${esc(m.publisher.name)} <span class="muted">(${esc(m.publisher.type)}, self-declared)</span></dd>
        <dt>Version</dt><dd><span class="mono">${esc(m.id)}</span> v${esc(m.version)} · ${esc(m.license)}</dd>
        <dt>Digest</dt><dd class="digest"><code class="break-anywhere">${esc(d.digest)}</code>
          <button type="button" class="icon-button" data-copy="${esc(d.digest)}" aria-label="Copy the digest">${icon("copy")}</button></dd>
      </dl>
    </section>

    <section class="card-section prose" id="card-use" aria-labelledby="card-use-title">
      <h2 id="card-use-title">Intended use</h2>
      <p>${esc(m.card.intended_use)}</p>
      <h3>Users</h3>${list(m.card.intended_users)}
      <h3>Population</h3><p>${esc(m.card.patient_population)}</p>
      <h3>Out of scope</h3>${list(m.card.out_of_scope)}
      <h3>Known failure modes</h3>${list(m.card.known_failure_modes)}
      <h3>Why risk level ${m.risk.level}</h3><p>${esc(m.risk.rationale)}</p>
    </section>

    <section class="card-section" id="card-questions" aria-labelledby="card-questions-title">
      <h2 id="card-questions-title">Questions</h2>
      ${Object.entries(m.questions).map(([key, q]) => questionPanel(key, q, key === p.primary)).join("")}
    </section>

    <section class="card-section" id="card-policy" aria-labelledby="card-policy-title">
      <h2 id="card-policy-title">Policy</h2>
      <p class="prose muted">Rules are checked in order and the first that matches decides; otherwise the confidence thresholds do.
        An automated outcome is then held for a person if its label is never automated, a provider failed, a required provider
        isn't deployed, or the risk level forbids it.</p>
      <ol class="rules">${p.rules.map((r) => `<li>
        <div class="row"><b>${esc(r.name)}</b> ${outcomeBadge(r.outcome)} ${r.priority ? priorityBadge(r.priority) : ""}
          ${r.recommendation ? `<span>→ <code>${esc(r.recommendation)}</code></span>` : ""}</div>
        <p>When ${conditionText(r.when)}.</p>
        <p class="muted">${esc(r.reason)}</p></li>`).join("")}</ol>
      <h3>Labels</h3>
      <ul>${Object.entries(p.labels).map(([label, l]) => `<li><code>${esc(label)}</code>: ${l.allow_auto ? `needs confidence ${l.auto_threshold ?? p.thresholds.auto} or more to be automated` : "never automated; a person always reviews"}</li>`).join("") || "<li>Every label follows the thresholds above.</li>"}
        ${Object.entries(p.automation_requires ?? {}).map(([slot, variants]) => `<li>Automated only while <code>${esc(slot)}</code> is ${variants.map((v) => `<code>${esc(v)}</code>`).join(" or ")}; otherwise a person reviews.</li>`).join("")}</ul>
      <div class="callout neutral"><b>When it can't decide:</b> ${esc(p.fallback)}</div>
    </section>

    ${d.actions?.length ? `<section class="card-section" id="card-actions" aria-labelledby="card-actions-title">
      <h2 id="card-actions-title">Actions</h2>
      <p class="prose muted">What the agent may ask to have done, besides answer. Mendari authorizes each action and runs none:
        the caller runs an action only when it was released, and every proposal and its release or denial is in the audit trail.
        An action the agent proposes that is not listed here is blocked, and its decision goes to a person.</p>
      <div class="panel table-wrap"><table>
        <thead><tr><th scope="col">Action</th><th scope="col">Does</th><th scope="col">Effect</th><th scope="col">Released</th><th scope="col">Arguments</th></tr></thead>
        <tbody>${d.actions.map((a) => `<tr><td><code>${esc(a.name)}</code></td>
          <td>${esc(a.description)}</td>
          <td>${effectBadge(a.effect)}</td>
          <td>${esc(a.released)}</td>
          <td>${a.arguments === null ? `<span class="muted">not checked</span>` : a.arguments.map((key) => `<code>${esc(key)}</code>`).join(", ") || `<span class="muted">none taken</span>`}</td></tr>`).join("")}</tbody>
      </table></div>
    </section>` : ""}

    <section class="card-section" id="card-approval" aria-labelledby="card-approval-title">
      <h2 id="card-approval-title">Automation approval</h2>
      ${registry ? registrySection(registry) : ""}
      ${approvalSection(governance, d, registry)}
      ${holdSection(holds, d)}
    </section>

    <section class="card-section" id="card-deployments" aria-labelledby="card-deployments-title">
      <h2 id="card-deployments-title">Deployments</h2>
      ${deploymentsSection(deployments)}
    </section>

    <section class="card-section" id="card-providers" aria-labelledby="card-providers-title">
      <h2 id="card-providers-title">Providers</h2>
      <div class="panel table-wrap"><table>
        <thead><tr><th scope="col">Slot</th><th scope="col">Answers</th><th scope="col">Deployed</th><th scope="col">Variants</th></tr></thead>
        <tbody>${Object.entries(m.providers).map(([slot, sp]) => `<tr><td>${esc(slotText(slot))} <span class="mono muted">${esc(slot)}</span></td>
          <td>${sp.answers.map((a) => `<code>${esc(a)}</code>`).join(", ")}</td>
          <td><b>${providerWords(d.active_providers[slot])}</b> <span class="mono muted">${esc(d.active_providers[slot].variant)}</span></td>
          <td>${Object.entries(sp.variants).map(([name, v]) => `<code>${esc(name)}</code> <span class="muted">${esc(v.type)}</span>`).join(", ")}</td></tr>`).join("")}</tbody>
      </table></div>
    </section>

    <section class="card-section" id="card-connection" aria-labelledby="card-connection-title">
      <h2 id="card-connection-title">Connection</h2>
      <p class="prose muted">The service that answers for this agent in your organization.</p>
      <div id="card-connection-body" aria-live="polite"><p class="muted">Reading…</p></div>
    </section>

    <section class="card-section" id="card-schemas" aria-labelledby="card-schemas-title">
      <h2 id="card-schemas-title">Schemas</h2>
      <details><summary>Input schema (JSON)</summary><pre class="json">${esc(JSON.stringify(d.input_schema, null, 2))}</pre></details>
      <details><summary>Output schema (JSON)</summary><pre class="json">${esc(JSON.stringify(d.output_schema, null, 2))}</pre></details>
    </section>`;
  wireApproval(main);
  if (registry) wireRegistry(main, registry, d);
  wireHolds(main, holds, d);
  cardConnection(state.agentId); // read apart: the card never waits for it (connection.js)
  main.querySelector("[data-copy]").addEventListener("click", (event) =>
    copyText(event.currentTarget.dataset.copy, "Digest copied", "Copying isn't available here: select the digest and copy it"));
  return cardSection(params[0]);
};

// What the agent's risk level allows, the approval in force, and (for people who may approve)
// approving or revoking. An approval is for this exact agent: a new version needs a new one.
function approvalSection(g, d, registry) {
  if (g.risk_level >= 4) return `<p class="prose">Risk level 4: never automated. No approval can change that.</p>`;
  if (!g.approvable) {
    return registry
      ? `<p class="prose">Risk level ${g.risk_level}: its risk level needs no approval besides the registry approval above.</p>`
      : `<p class="prose">Risk level ${g.risk_level}: automates within its thresholds. No approval is needed.</p>`;
  }
  const heading = registry ? `<h3>Approval for risk level ${g.risk_level}</h3>` : "";
  const active = g.active
    ? `<div class="callout neutral"><b>Automation approved</b> by ${esc(g.active.approver)} (${esc(g.active.reference)}),
        until ${esc(time(g.active.expires_at))}.
        ${may("approve") ? `<button type="button" data-revoke="${esc(g.active.id)}">Revoke</button>` : ""}</div>`
    : `<p class="prose">Risk level ${g.risk_level}: a person reviews every decision unless your organization approves automation
        for this exact version (digest <span class="mono">${esc(g.digest.slice(7, 19))}</span>).</p>`;
  const form = !g.active && may("approve") ? `<form class="approve-form" id="approve-form">
      <label for="approval-reference">Reference</label>
      <input id="approval-reference" required minlength="3" placeholder="e.g. AI committee minutes, 2026-10-01, item 4">
      ${signedIn() ? "" : `<label for="approval-approver">Approver</label><input id="approval-approver" required maxlength="100" pattern="\\S+" spellcheck="false" title="An identifier, without spaces: the audit trail keeps it">`}
      <label for="approval-days">For</label>
      <select id="approval-days"><option value="90">90 days</option><option value="180" selected>180 days</option><option value="365">1 year</option></select>
      <button type="submit" class="primary">Approve automation</button>
    </form>` : "";
  return heading + active + form;
}

function wireApproval(main) {
  main.querySelector("#approve-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    whileBusy(button, "Approving…", async () => {
      try {
        await api("/api/governance", {
          method: "POST",
          body: JSON.stringify({
            agent_id: state.agentId, reference: $("#approval-reference").value, days: Number($("#approval-days").value),
            approver: $("#approval-approver")?.value || null,
          }),
        });
        toast("Automation approved for this version");
        if (main.isConnected) render();
      } catch (error) {
        toastError(error.message);
      }
    });
  });
  main.querySelector("[data-revoke]")?.addEventListener("click", (event) => {
    const button = event.currentTarget;
    whileBusy(button, "Revoking…", async () => {
      try {
        await api(`/api/governance/${encodeURIComponent(button.dataset.revoke)}`, { method: "DELETE" });
        toast("Approval revoked: a person reviews again");
        if (main.isConnected) render();
      } catch (error) {
        toastError(error.message);
      }
    });
  });
}

// An agent pulled from a registry (origin `hub`; LP-18d, ADR-0012 §6) never automates, and its own
// rules never block alone, until your organization approves its exact digest
// (GET/POST /api/registry-approvals, DELETE /api/registry-approvals/{id}). The approval is keyed
// on the agent and its digest, so it lives on the agent's card: what is approved, by whom, when and
// why; approving (a reference, then a dialog saying what it allows and what it never lifts) and
// revoking for people with `approve`.
const fromRegistry = (d) => d.origin === "hub";

const shortDigest = (digest) => String(digest ?? "").replace(/^sha256:/, "").slice(0, 12);

function registrySection(r) {
  const pulled = r.pulled ?? {};
  const recorded = (value) => (value ? esc(value) : `<span class="muted">not recorded</span>`);
  const facts = `<dl class="facts">
      <dt>Registry</dt><dd class="break-anywhere">${recorded(pulled.registry)}</dd>
      <dt>Namespace</dt><dd>${recorded(pulled.namespace)}</dd>
      <dt>Signing key</dt><dd class="mono break-anywhere">${recorded(pulled.key_id)}</dd>
      <dt>Pulled</dt><dd>${pulled.pulled_at ? `<time datetime="${esc(pulled.pulled_at)}">${esc(time(pulled.pulled_at))}</time>` : recorded(null)}</dd>
      <dt>Digest served</dt><dd><code class="break-anywhere">${esc(r.digest)}</code></dd>
    </dl>`;
  const recall = r.recalled
    ? `<div class="callout danger"><b>Recalled by its registry</b> when this version was pulled. It never automates while
        the recall stands, approved or not.</div>`
    : "";
  const a = r.active;
  const status = a
    ? `<div class="callout neutral" id="registry-active"><b>Approved for automation</b> by ${esc(a.approver)} (${esc(a.reference)}),
        ${esc(time(a.created_at))}, for digest <span class="mono">${esc(shortDigest(a.agent_digest))}</span>, until revoked.
        ${may("approve") ? `<button type="button" data-registry-revoke="${esc(a.id)}">Revoke</button>` : ""}</div>`
    : `<p class="prose"><b>Not approved here.</b> A person reviews every decision this agent's policy would automate, and every
        case its own rules would block, until your organization approves this exact version
        (digest <span class="mono">${esc(shortDigest(r.digest))}</span>).</p>`;
  const form = !a && may("approve") ? `<form class="approve-form" id="registry-form">
      <label for="registry-reference">Reference</label>
      <input id="registry-reference" required minlength="3" maxlength="300" placeholder="e.g. AI committee minutes, 2026-10-01, item 4">
      ${signedIn() ? "" : `<label for="registry-approver">Approver</label><input id="registry-approver" required maxlength="100" pattern="\\S+" spellcheck="false" title="An identifier, without spaces: the audit trail keeps it">`}
      <button type="submit" class="primary">Approve this version…</button>
    </form>` : "";
  return `<div id="registry-approval">
    <h3>Agent from a registry</h3>
    ${facts}${recall}${status}${form}
    ${registryHistory(r.approvals ?? [], r.digest)}
  </div>`;
}

// Every approval of this agent in your organization, newest first: of this digest or another,
// in force or revoked.
function registryHistory(rows, digest) {
  if (!rows.length) return "";
  const when = (iso) => `<time datetime="${esc(iso)}" title="${esc(time(iso))}">${esc(shortTime(iso))}</time>`;
  const status = (row) => {
    if (row.revoked_at) return `Revoked by ${esc(row.revoked_by)}, ${when(row.revoked_at)}`;
    return row.agent_digest === digest ? "In force" : "Another version, not served here";
  };
  return `<div class="panel table-wrap mt-2"><table>
    <caption class="sr-only">Approvals of this agent from a registry, newest first</caption>
    <thead><tr><th scope="col">Approved</th><th scope="col">Digest</th><th scope="col">By</th><th scope="col">Reference</th><th scope="col">Status</th></tr></thead>
    <tbody>${rows.map((row) => `<tr><td class="nowrap">${when(row.created_at)}</td>
      <td class="mono">${esc(shortDigest(row.agent_digest))}</td><td>${esc(row.approver)}</td>
      <td>${esc(row.reference)}</td><td>${status(row)}</td></tr>`).join("")}</tbody>
  </table></div>`;
}

function wireRegistry(main, r, d) {
  main.querySelector("#registry-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const reference = $("#registry-reference").value;
    const approver = $("#registry-approver")?.value || null;
    const registry = r.pulled?.registry;
    const approved = await actionDialog({
      title: "Approve this agent from a registry?",
      body: `<p>Your organization approves <b>${esc(d.manifest.name)}</b> <span class="mono">${esc(r.agent_id)}</span> v${esc(d.manifest.version)},
          digest <code class="break-anywhere">${esc(r.digest)}</code>${registry ? `, from ${esc(registry)}` : ""}, to automate under its own policy.</p>
        <p>Once approved, what this agent's policy automates is no longer sent to a person because the agent came from a
          registry, and its own rules' blocks take effect without a person.</p>
        <p>It does not lift a recall by its registry, an automation hold, or its risk level's limits (levels 2 and 3 also need
          their own approval). It covers this digest only, lasts until revoked, and is kept in the audit trail with your reference.</p>`,
      action: "Approve",
      run: () => api("/api/registry-approvals", {
        method: "POST",
        body: JSON.stringify({ agent_id: r.agent_id, agent_digest: r.digest, reference, approver }),
      }),
    });
    if (!approved) return;
    toast("Approved for this version");
    if (main.isConnected) render();
  });
  main.querySelector("[data-registry-revoke]")?.addEventListener("click", (event) => {
    const button = event.currentTarget;
    whileBusy(button, "Revoking…", async () => {
      try {
        await api(`/api/registry-approvals/${encodeURIComponent(button.dataset.registryRevoke)}`, { method: "DELETE" });
        toast("Approval revoked: a person reviews again");
        if (main.isConnected) render();
      } catch (error) {
        toastError(error.message);
      }
    });
  });
}

// Automation holds (AuditStore.hold_automation): stopping automation for this agent at once,
// without a redeploy, and lifting it. A hold set from the command line covers every
// organization and is lifted there. Holding is one click (it only lowers automation); lifting
// raises it again, so it is asked about first, as every other change that takes a safeguard away.
function holdSection(holds, d) {
  const covering = holdsFor(holds, d);
  // A hold of the deployment's comes without its reason and setter in memberships mode: the
  // operator's (ADR-0008).
  const why = (h) => (h.reason === undefined
    ? `set by the deployment's operator, ${esc(time(h.set_at))}.`
    : `${esc(h.reason)}. Set by ${esc(h.set_by)}, ${esc(time(h.set_at))}.`);
  const listed = covering.map((h) => `<div class="callout danger"><b>Automation on hold</b>
      (${h.agent_id ? "this agent" : "every agent"}${h.org ? "" : ", every organization"}): ${why(h)}
      ${may("approve") && h.org ? `<button type="button" data-lift="${esc(h.id)}">Lift</button>` : ""}</div>`).join("");
  const form = may("approve") ? `<form class="approve-form" id="hold-form">
      <label for="hold-reason">Reason</label>
      <input id="hold-reason" required maxlength="200" placeholder="e.g. investigating a missed emergency">
      <button type="submit">Hold automation for this agent</button>
    </form>` : "";
  return listed + form;
}

// What lifting `hold` does, in the words of the dialog that asks first: the pack decides on its
// own again, and what still stops it from automating stays.
function liftText(hold, d) {
  const who = hold?.agent_id
    ? `<b>${esc(d.manifest.name)}</b> decides on its own again`
    : `Every agent of your organization decides on its own again, <b>${esc(d.manifest.name)}</b> included`;
  return `<p>${who}: what its policy automates is no longer sent to a person because of this hold.</p>
    <p>Another hold, a recall by its registry, an approval it still needs and its risk level's limits keep applying.
      The lift is kept in the audit trail.</p>`;
}

function wireHolds(main, holds, d) {
  main.querySelector("#hold-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    whileBusy(button, "Holding…", async () => {
      try {
        await api("/api/holds", { method: "POST", body: JSON.stringify({ agent_id: state.agentId, reason: $("#hold-reason").value }) });
        toast("Automation held: a person reviews every decision");
        if (main.isConnected) render();
      } catch (error) {
        toastError(error.message);
      }
    });
  });
  for (const button of main.querySelectorAll("[data-lift]")) {
    button.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.lift;
      const lifted = await actionDialog({
        title: "Lift this automation hold?",
        body: liftText(holds.find((h) => h.id === id), d),
        action: "Lift",
        danger: true,
        run: () => api(`/api/holds/${encodeURIComponent(id)}`, { method: "DELETE" }),
      });
      if (!lifted) return;
      toast("Hold lifted");
      if (main.isConnected) render();
    });
  }
}

views.card.update = (params) => focusHeading(cardSection(params[0]) ?? $("main h1"));

const cardSection = (key) => (key ? $(`#card-${CSS.escape(key)} h2`) : null);

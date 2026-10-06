// Decision Card: the pack as a document, sections linked as routes ("#card/policy").

import { communityNotice, isCommunity, originBadge, providerWords, rowLabels, statusNotice } from "./pack.js";
import { render, views } from "./router.js";
import { $, api, esc, focusHeading, icon, may, outcomeBadge, priorityBadge, shortTime, signedIn, slotText, state, time, toast, toastError } from "./ui.js";

function deriveRow(row) {
  const among = row.in ? `of ${row.in.map((m) => `<code>${esc(m)}</code>`).join(", ")}` : "member";
  const codes = (labels) => labels.map((l) => `<code>${esc(l)}</code>`).join(" or ");
  const gate = row.when ? `, when ${Object.entries(row.when).map(([m, labels]) => `<code>${esc(m)}</code> is ${codes(labels)}`).join(" and ")}` : "";
  if (row.any !== null && row.any !== undefined) return `any ${among} is ${codes(rowLabels(row.any))}${gate} → <code>${esc(row.then)}</code>`;
  if (row.all !== null && row.all !== undefined) return `every ${among} is ${codes(rowLabels(row.all))}${gate} → <code>${esc(row.then)}</code>`;
  return `otherwise → <code>${esc(row.else)}</code>`;
}

function questionPanel(key, q, primary) {
  const kind = q.type && q.type !== "choice" ? ` <span class="chip">${esc(q.type)}</span>` : "";
  const derive = q.derive ? `<p>Derived from ${q.derive.from.map((x) => `<code>${esc(x)}</code>`).join(", ")}
      (first matching row wins; members treated as independent):</p>
    <ol>${q.derive.table.map((row) => `<li>${deriveRow(row)}</li>`).join("")}</ol>` : "";
  const labels = q.type === "score" ? Object.keys(q.levels).sort((a, b) => q.levels[a].score - q.levels[b].score) : Object.keys(q.criteria);
  const score = (label) => (q.type === "score" ? `<td class="mono">${esc(q.levels[label].score)}</td>` : "");
  return `<div class="panel question"><h3>Question <code>${esc(key)}</code>${primary ? " (the decision)" : ""}${kind}</h3>
    <p class="muted">${esc(q.instructions)}</p>${derive}
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
// and at levels 2-3 only the organization's approval of this exact version lifts the risk cap.
function automationText(d, governance) {
  const p = d.manifest.policy;
  const code = (v) => `<code>${esc(v)}</code>`;
  const review = `A person reviews from confidence ${p.thresholds.review.toFixed(2)}; below that it abstains.`;
  if (isCommunity(d)) return `Never: a community pack recommends, and a person confirms every decision. ${review}`;
  if (d.risk.level >= 4) return `Never: at risk level 4 a person reviews every decision. ${review}`;
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

// Where this pack runs: one row each time its version, providers or release changed somewhere.
function deploymentsSection(rows) {
  if (!rows.length) return `<p class="prose muted">Not recorded yet: a server records itself the first time it uses the pack.</p>`;
  const providers = (map) => Object.entries(map).map(([slot, variant]) => `<code>${esc(slot)}</code> ${esc(variant)}`).join(", ");
  return `<div class="panel table-wrap"><table>
    <caption class="sr-only">Deployments of this pack, newest first</caption>
    <thead><tr><th scope="col">Since</th><th scope="col">Environment</th><th scope="col">Version</th><th scope="col">Providers</th><th scope="col">Release</th></tr></thead>
    <tbody>${rows.map((r) => `<tr><td class="nowrap"><time datetime="${esc(r.started_at)}" title="${esc(time(r.started_at))}">${esc(shortTime(r.started_at))}</time></td>
      <td>${esc(r.environment)}</td>
      <td>v${esc(r.pack_version)} <span class="mono muted">${esc(r.pack_digest.slice(7, 19))}</span></td>
      <td>${providers(r.providers)}</td><td class="mono">${esc(r.release)}</td></tr>`).join("")}</tbody>
  </table></div>`;
}

const CARD_SECTIONS = [
  ["facts", "At a glance"],
  ["use", "Intended use"],
  ["questions", "Questions"],
  ["policy", "Policy"],
  ["approval", "Automation approval"],
  ["deployments", "Deployments"],
  ["providers", "Providers"],
  ["schemas", "Schemas"],
];

// The card is a document: a table of contents whose links are routes ("#card/policy"), prose at a
// readable width, facts as a definition list, and the machine-facing schemas folded away.
views.card = async (main, params) => {
  const d = state.detail;
  const m = d.manifest;
  const p = m.policy;
  const query = `pack_id=${encodeURIComponent(state.packId)}`;
  const [governance, deployments] = await Promise.all([api(`/api/governance?${query}`), api(`/api/deployments?${query}`)]);
  const list = (items) => `<ul>${items.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>`;
  main.innerHTML = `
    <h1>${esc(m.name)}</h1>
    <p class="prose lead">${esc(m.summary)}</p>
    ${communityNotice(d)}
    ${statusNotice(d)}
    <nav class="toc" aria-label="On this page">${CARD_SECTIONS.map(([key, title]) => `<a href="#card/${key}">${esc(title)}</a>`).join("")}</nav>

    <section class="card-section" id="card-facts" aria-labelledby="card-facts-title">
      <h2 id="card-facts-title">At a glance</h2>
      <dl class="facts">
        <dt>Source</dt><dd>${originBadge(d)}</dd>
        <dt>Risk level</dt><dd><span class="badge risk">${m.risk.level}</span> ${esc(m.risk.category)}</dd>
        <dt>Decides</dt><dd><code>${esc(p.primary)}</code>: ${d.labels.map((l) => `<code>${esc(l)}</code>`).join(", ")}</dd>
        <dt>Automates</dt><dd>${automationText(d, governance)}</dd>
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
        isn't deployed, the risk level forbids it, or the pack is a community pack.</p>
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

    <section class="card-section" id="card-approval" aria-labelledby="card-approval-title">
      <h2 id="card-approval-title">Automation approval</h2>
      ${approvalSection(governance, d)}
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

    <section class="card-section" id="card-schemas" aria-labelledby="card-schemas-title">
      <h2 id="card-schemas-title">Schemas</h2>
      <details><summary>Input schema (JSON)</summary><pre class="json">${esc(JSON.stringify(d.input_schema, null, 2))}</pre></details>
      <details><summary>Output schema (JSON)</summary><pre class="json">${esc(JSON.stringify(d.output_schema, null, 2))}</pre></details>
    </section>`;
  wireApproval(main);
  main.querySelector("[data-copy]").addEventListener("click", async (event) => {
    try {
      await navigator.clipboard.writeText(event.currentTarget.dataset.copy);
      toast("Digest copied");
    } catch {
      toastError("Copying isn't available here: select the digest and copy it");
    }
  });
  return cardSection(params[0]);
};

// What the pack's risk level allows, the approval in force, and (for people who may approve)
// approving or revoking. An approval is for this exact pack: a new version needs a new one.
function approvalSection(g, d) {
  if (isCommunity(d)) return `<p class="prose">A community pack is never automated, and no approval can change that: a person confirms every decision.</p>`;
  if (g.risk_level >= 4) return `<p class="prose">Risk level 4: never automated. No approval can change that.</p>`;
  if (!g.approvable) return `<p class="prose">Risk level ${g.risk_level}: automates within its thresholds. No approval is needed.</p>`;
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
  return active + form;
}

function wireApproval(main) {
  main.querySelector("#approve-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/governance", {
        method: "POST",
        body: JSON.stringify({
          pack_id: state.packId, reference: $("#approval-reference").value, days: Number($("#approval-days").value),
          approver: $("#approval-approver")?.value || null,
        }),
      });
      toast("Automation approved for this version");
      render();
    } catch (error) {
      toastError(error.message);
    }
  });
  main.querySelector("[data-revoke]")?.addEventListener("click", async (event) => {
    try {
      await api(`/api/governance/${encodeURIComponent(event.currentTarget.dataset.revoke)}`, { method: "DELETE" });
      toast("Approval revoked: a person reviews again");
      render();
    } catch (error) {
      toastError(error.message);
    }
  });
}

views.card.update = (params) => focusHeading(cardSection(params[0]) ?? $("main h1"));

const cardSection = (key) => (key ? $(`#card-${CSS.escape(key)} h2`) : null);

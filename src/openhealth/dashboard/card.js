// Decision Card: the pack as a document, sections linked as routes ("#card/policy").

import { providerWords, rowLabels } from "./pack.js";
import { views } from "./router.js";
import { $, esc, focusHeading, icon, outcomeBadge, priorityBadge, slotText, state, toast, toastError } from "./ui.js";

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
// automated; the caps in core/policy.py (risk level, required providers) can rule it out entirely.
function automationText(d) {
  const p = d.manifest.policy;
  const code = (v) => `<code>${esc(v)}</code>`;
  const review = `A person reviews from confidence ${p.thresholds.review.toFixed(2)}; below that it abstains.`;
  if (d.risk.level >= 4) return `Never: at risk level 4 a person reviews every decision. ${review}`;
  if (d.risk.level >= 2) {
    return `Not here: at risk level ${d.risk.level} a person reviews every decision (only a recorded governance approval can lift that, and this server takes none). ${review}`;
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
  return `At confidence ${p.thresholds.auto.toFixed(2)} or more${exceptions ? ` (${exceptions}; see Policy)` : ""}. ${review}`;
}

const CARD_SECTIONS = [
  ["facts", "At a glance"],
  ["use", "Intended use"],
  ["questions", "Questions"],
  ["policy", "Policy"],
  ["providers", "Providers"],
  ["schemas", "Schemas"],
];

// The card is a document: a table of contents whose links are routes ("#card/policy"), prose at a
// readable width, facts as a definition list, and the machine-facing schemas folded away.
views.card = async (main, params) => {
  const d = state.detail;
  const m = d.manifest;
  const p = m.policy;
  const list = (items) => `<ul>${items.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>`;
  main.innerHTML = `
    <h1>${esc(m.name)}</h1>
    <p class="prose lead">${esc(m.summary)}</p>
    <nav class="toc" aria-label="On this page">${CARD_SECTIONS.map(([key, title]) => `<a href="#card/${key}">${esc(title)}</a>`).join("")}</nav>

    <section class="card-section" id="card-facts" aria-labelledby="card-facts-title">
      <h2 id="card-facts-title">At a glance</h2>
      <dl class="facts">
        <dt>Risk level</dt><dd><span class="badge risk">${m.risk.level}</span> ${esc(m.risk.category)}</dd>
        <dt>Decides</dt><dd><code>${esc(p.primary)}</code>: ${d.labels.map((l) => `<code>${esc(l)}</code>`).join(", ")}</dd>
        <dt>Automates</dt><dd>${automationText(d)}</dd>
        <dt>CDS Hooks</dt><dd>${d.cds_service ? `service <code>${esc(d.cds_service)}</code> on <code>${esc(m.cds_hooks.hook)}</code>` : "none"}</dd>
        <dt>Publisher</dt><dd>${esc(m.publisher.name)} <span class="muted">(${esc(m.publisher.type)})</span></dd>
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

views.card.update = (params) => focusHeading(cardSection(params[0]) ?? $("main h1"));

const cardSection = (key) => (key ? $(`#card-${CSS.escape(key)} h2`) : null);

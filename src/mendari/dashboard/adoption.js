// SPDX-License-Identifier: Apache-2.0
// The adoption path: the six steps from finding an agent to running it on evidence, at the top of
// the Overview. Each step's state comes from what the page already reads (the agent's detail, its
// decision counts and the validation brief's gates); nothing here is stored or computed anew.

import { esc, icon } from "./ui.js";

const MIN_PILOT_CASES = 30; // the validation brief's pilot minimum (core/validation_brief.py)

const gate = (brief, key) => (brief?.gates ?? []).find((g) => g.key === key);
// A gate is "pass", "warn", "fail" or "no_data" (nothing to measure): only a pass is a pass.
const passed = (g) => g?.status === "pass";
const failed = (g) => g?.status === "fail";

// What local validation has shown. A pilot can show that clinicians agree; how the agent does
// alone is measured only once something is automated, so until then the safety gate has no data:
// the step is done on agreement (and no safety failure), and says what is still unmeasured.
function validation(safety, agreement) {
  if (!safety || !agreement) return "Evaluate it on your organization's labelled cases and compare with the publisher's evaluations.";
  if (failed(safety)) return safety.detail;
  if (!passed(agreement)) return agreement.detail;
  return passed(safety) ? "Safe on your cases, and clinicians agree with it." : `Clinicians agree with it. ${safety.detail}`;
}

const SOURCE = {
  hub: "Pulled from a registry and verified. Its Hub page shows the publisher's signed evaluations and sites' evidence.",
  maintained: "Bundled with Mendari.",
  local: "Your organization's own agent, served from this deployment.",
  store: "From your organization's agent store, promoted by someone who did not write it.",
  community: "A community agent: it recommends, and a person confirms every decision.",
};

// [state, evidence, action] for each step; state is "done", "todo" or "blocked".
function steps(stats, brief, detail) {
  const total = stats?.decisions ?? 0;
  const pilot = brief?.shadow_pilot ?? {};
  const pilotCases = brief?.volume?.total ?? 0;
  const safety = gate(brief, "safety");
  const agreement = gate(brief, "agreement");
  const governance = gate(brief, "governance");
  const drift = gate(brief, "drift");
  return [
    {
      key: "source",
      title: "Know where it came from",
      state: detail?.origin ? "done" : "todo",
      evidence: SOURCE[detail?.origin] ?? "Its source is shown on its Decision Card.",
      action: { href: "#card", label: "Decision Card" },
    },
    {
      key: "try",
      title: "Try it on synthetic cases",
      state: total > 0 ? "done" : "todo",
      evidence: total > 0
        ? `${total} decision${total === 1 ? "" : "s"} made so far.`
        : "See what it decides, and why, before it touches a workflow.",
      action: { href: "#try", label: "Try it" },
    },
    {
      key: "shadow",
      title: "Shadow it on real workflows",
      state: pilot.active && pilotCases >= MIN_PILOT_CASES ? "done" : "todo",
      evidence: pilot.active
        ? `Shadow pilot running: ${pilotCases} of the ${MIN_PILOT_CASES} cases the brief asks for, automation held while clinicians decide.`
        : "Run it beside clinicians with automation held, so its answers are compared with theirs before it acts alone.",
      // The pilot is run from the command center below (a section of this view, not a route);
      // without the brief there is none, and holding automation on the Decision Card is the way.
      action: brief
        ? { href: "#cc-title", section: true, label: pilot.active ? "Shadow pilot" : "Start a shadow pilot" }
        : { href: "#card/approval", label: "Hold automation" },
    },
    {
      key: "validate",
      title: "Validate it on your own cases",
      state: passed(agreement) && safety && !failed(safety) ? "done" : "todo",
      // What still stands in the way, or what was measured (and what was not).
      evidence: validation(safety, agreement),
      action: { href: "#evals", label: "Evaluations" },
    },
    {
      key: "approve",
      title: "Approve it",
      state: passed(governance) ? "done" : "todo",
      evidence: governance?.detail ?? "Record who approved this exact version, and for what.",
      action: { href: "#card/approval", label: "Governance" },
    },
    {
      key: "monitor",
      title: "Monitor it and share back",
      // Quiet monitoring is only evidence once the agent has been validated on real cases.
      state: passed(safety) && passed(agreement) && passed(drift) && total > 0 ? "done" : "todo",
      evidence: drift?.detail
        ? `${drift.detail} Then share signed counts with the registry, so the next hospital starts from evidence.`
        : "Watch drift and overrides, and share signed counts so the next hospital starts from evidence.",
      action: { href: "#monitoring", label: "Monitoring" },
    },
  ];
}

export function adoptionPath(stats, brief, detail) {
  const list = steps(stats, brief, detail);
  const next = list.findIndex((step) => step.state !== "done");
  const doneCount = list.filter((step) => step.state === "done").length;
  return `<section class="panel adoption" aria-labelledby="adoption-title">
    <div class="row adoption-head">
      <h2 id="adoption-title">Adoption path</h2>
      <span class="muted">${doneCount} of ${list.length} steps done</span>
    </div>
    <ol class="adoption-steps">${list.map((step, index) => {
      const current = index === next;
      return `<li class="adoption-step ${step.state}${current ? " current" : ""}" ${current ? 'aria-current="step"' : ""} data-step="${step.key}">
        <span class="adoption-mark" aria-hidden="true">${step.state === "done" ? icon("check") : index + 1}</span>
        <div class="adoption-body">
          <p class="adoption-title"><b>${esc(step.title)}</b>
            <span class="sr-only">${step.state === "done" ? "(done)" : current ? "(next step)" : "(to do)"}</span></p>
          <p class="muted">${esc(step.evidence)}</p>
        </div>
        <a class="button${current ? " primary" : ""} adoption-action" href="${step.action.href}"${step.action.section ? " data-section" : ""}>${esc(step.action.label)}</a>
      </li>`;
    }).join("")}</ol>
  </section>`;
}

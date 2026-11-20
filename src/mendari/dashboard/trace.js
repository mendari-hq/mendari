// SPDX-License-Identifier: Apache-2.0
// Visual guardrail trace: turns a decision's outcome, rule, confidence, errors and holds into a
// four-stage pipeline strip so clinicians and governance leads immediately see which layer acted.

import { esc, icon, outcomeText, pct } from "./ui.js";

function stagesOf(record) {
  const reasons = record.reasons ?? [];
  const errors = Object.keys(record.errors ?? {});
  const rejected = reasons.some((r) => r.startsWith("input rejected:"));
  const shadow = reasons.some((r) => r.includes("shadow mode:") || r.includes("shadow pilot:"));
  const held = reasons.some((r) => r.startsWith("automation is on hold:"));
  const riskCap = reasons.some((r) => r.includes("risk level") || r.includes("community pack") || r.includes("registry"));
  const providerCap = reasons.some((r) => r.includes("automation requires"));

  // Stage 1: Input & schema gate
  const inputStage = rejected
    ? { label: "Input gate", value: "Rejected", tone: "danger", icon: "shield-x" }
    : { label: "Input gate", value: "Valid", tone: "ok", icon: "check" };

  // Stage 2: Model / providers
  let modelStage;
  if (rejected) {
    modelStage = { label: "Providers", value: "Skipped", tone: "muted", icon: "clock" };
  } else if (errors.length) {
    modelStage = { label: "Providers", value: `${errors.length} failed`, tone: "warning", icon: "alert" };
  } else if (record.confidence !== null && record.confidence !== undefined) {
    modelStage = { label: "Providers", value: `${pct(record.confidence)} sure`, tone: "ok", icon: "activity" };
  } else {
    modelStage = { label: "Providers", value: "Answered", tone: "ok", icon: "activity" };
  }

  // Stage 3: Policy & safety rules
  let policyStage;
  if (rejected) {
    policyStage = { label: "Safety policy", value: "Blocked", tone: "danger", icon: "shield-x" };
  } else if (record.rule) {
    policyStage = {
      label: "Safety rule",
      value: record.rule,
      tone: record.outcome === "BLOCK" || record.priority === "stat" ? "danger" : "warning",
      icon: "alert",
    };
  } else if (record.outcome === "ABSTAIN") {
    policyStage = { label: "Thresholds", value: "Below review", tone: "muted", icon: "clock" };
  } else if (reasons.some((r) => r.includes(">= auto threshold"))) {
    policyStage = { label: "Thresholds", value: "Auto-ready", tone: "ok", icon: "check" };
  } else {
    policyStage = { label: "Thresholds", value: "Needs review", tone: "warning", icon: "clock" };
  }

  // Stage 4: Governance, holds & shadow mode
  let gateStage;
  if (shadow) {
    gateStage = { label: "Governance", value: "Shadow pilot", tone: "warning", icon: "eye" };
  } else if (held) {
    gateStage = { label: "Governance", value: "Held", tone: "warning", icon: "lock" };
  } else if (riskCap) {
    gateStage = { label: "Governance", value: "Risk / approval gate", tone: "warning", icon: "lock" };
  } else if (providerCap) {
    gateStage = { label: "Governance", value: "Model required", tone: "warning", icon: "lock" };
  } else if (record.outcome === "AUTO") {
    gateStage = { label: "Governance", value: "Cleared", tone: "ok", icon: "shield-check" };
  } else {
    gateStage = { label: "Outcome", value: outcomeText(record.outcome), tone: record.outcome === "BLOCK" ? "danger" : "muted", icon: record.outcome === "BLOCK" ? "shield-x" : "check" };
  }

  return [inputStage, modelStage, policyStage, gateStage];
}

export function guardrailTrace(record) {
  if (!record) return "";
  const stages = stagesOf(record);
  return `<ol class="trace" aria-label="Decision guardrail trace">
    ${stages.map((s, i) => `<li class="trace-step tone-${esc(s.tone)}">
      <span class="trace-k"><span class="trace-num" aria-hidden="true">0${i + 1}</span>${icon(s.icon)}<span>${esc(s.label)}</span></span>
      <span class="trace-v">${esc(s.value)}</span>
    </li>`).join("")}
  </ol>`;
}

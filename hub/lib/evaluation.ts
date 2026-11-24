// SPDX-License-Identifier: Apache-2.0
// The evaluation page's words (app/agents/[namespace]/[name]/evaluation/page.tsx): what the
// registry's answer (mendari.agent-evaluation/1, lib/registry.ts) says, in sentences a hospital's
// IT lead or a clinician can read. Nothing here works out a number: every figure is the
// registry's k of n with its interval, printed by lib/figures.ts. tests/evaluation-words.spec.ts
// holds each sentence.
import { shown } from "./figures";
import type {
  AgentEvaluation,
  Comparison,
  EvaluatedConfiguration,
  EvaluatedDataset,
  EvaluationClaimed,
  Measure,
  NotShown,
} from "./registry";

const whole = new Intl.NumberFormat("en-US");

export function counted(count: number, one: string, many = `${one}s`): string {
  return `${whole.format(count)} ${count === 1 ? one : many}`;
}

// What the cases were built from, as a signed record states it (the manifest's card.provenance).
export const PROVENANCE_WORDS: Record<string, string> = {
  synthetic: "Synthetic: written or generated for testing. No patient's record is in it.",
  de_identified: "De-identified records, as the publisher's manifest states.",
  site_records: "Records from a care site, as the publisher's manifest states.",
};

export function provenanceWords(dataset: EvaluatedDataset, agentIsSynthetic: boolean): string {
  if (dataset.provenance) return PROVENANCE_WORDS[dataset.provenance] ?? "Not stated.";
  return agentIsSynthetic
    ? "Not stated in the signed record. The agent's own manifest marks its data as synthetic."
    : "Not stated in the signed record.";
}

// "600 synthetic cases", "600 cases".
export function casesWords(dataset: EvaluatedDataset, agentIsSynthetic: boolean): string {
  const synthetic = dataset.provenance === "synthetic" || (dataset.provenance === null && agentIsSynthetic);
  return counted(dataset.n, synthetic ? "synthetic case" : "case");
}

// The order's measures, as a column says them, with which way is better.
export const MEASURE_WORDS: Record<Measure, { name: string; better: string }> = {
  critical_automated: { name: "Critical cases automated", better: "fewer is better" },
  critical_detection: { name: "Critical cases detected", better: "more is better" },
  false_automation: { name: "Wrong answers automated", better: "fewer is better" },
  accuracy: { name: "Correct answers", better: "more is better" },
};

export const ORDER_WORDS =
  "The order is fixed and the same for every agent: fewest critical cases automated, then most critical cases detected, " +
  "then fewest wrong answers automated, then most correct answers. Cost never decides it.";

function labelOf(dataset: EvaluatedDataset, id: string | null): string {
  return dataset.configurations.find((configuration) => configuration.id === id)?.label ?? "a configuration";
}

// What the comparison of one dataset's configurations says: a headline, the sentences behind
// it, whether a configuration is named as having done best, and how the box reads: `ok` only
// when the one that did best let no critical case through, `warn` when even it did.
export type ComparisonWords = {
  headline: string;
  detail: string[];
  best: string | null;
  tone: "ok" | "warn" | "neutral";
};

function decided(measure: Measure, comparison: Comparison, second: string): string {
  const figures = comparison.figures;
  if (!figures) return "";
  const [one, other] = [shown(figures.first), shown(figures.second)];
  switch (measure) {
    case "critical_automated":
      return `It automated ${one} of the critical cases; ${second} automated ${other}.`;
    case "critical_detection": {
      const of = figures.first.question ? ` (${figures.first.question}=${figures.first.label})` : "";
      return `It detected ${one} of the critical cases${of}; ${second} detected ${other}.`;
    }
    case "false_automation":
      return `It automated a wrong answer in ${one} cases; ${second} in ${other}.`;
    case "accuracy":
      return `It gave the right answer in ${one} cases; ${second} in ${other}.`;
  }
}

// A run in which a provider failed sent those cases to a person: its figures are partly of the
// failure, and the comparison says so beside whatever it says.
function failureWords(dataset: EvaluatedDataset): string[] {
  return dataset.configurations
    .filter((configuration) => (configuration.claimed.failed_cases ?? 0) > 0)
    .map(
      (configuration) =>
        `A provider failed on ${whole.format(configuration.claimed.failed_cases ?? 0)} of the ${counted(dataset.n, "case")} ` +
        `in ${configuration.label}'s run. Each went to a person, so its figures are partly of the failure.`,
    );
}

export function comparisonWords(dataset: EvaluatedDataset): ComparisonWords {
  const words = compared(dataset);
  return { ...words, detail: [...words.detail, ...failureWords(dataset)] };
}

function compared(dataset: EvaluatedDataset): ComparisonWords {
  const comparison = dataset.comparison;
  const many = dataset.configurations.length;
  // Whether any configuration let a critical case through: no box reads as reassuring then.
  const anyAutomated = dataset.configurations.some((configuration) => configuration.critical_automated > 0);
  const tone = anyAutomated ? "warn" : "neutral";
  switch (comparison.status) {
    case "one_configuration":
      return {
        headline: "One configuration was published, so there is nothing to compare it with.",
        detail: ["Its figures are below. A publisher that runs the agent in more than one way can publish each."],
        best: null,
        tone,
      };
    case "no_critical_label":
      return {
        headline: "No configuration is named as best: this agent names no critical label.",
        detail: [
          "The comparison puts safety first, and safety here means the cases a miss is costly on. The agent's " +
            "manifest lists none, so the figures below are side by side and in name order.",
        ],
        best: null,
        tone,
      };
    case "insufficient_evidence":
      return {
        headline: "Too few critical cases to say which configuration works best.",
        detail: [
          "A configuration is named first only when a critical label has at least 100 positive cases in the dataset. " +
            "The figures below still stand, with the wide intervals that few cases give.",
        ],
        best: null,
        tone,
      };
    case "tied": {
      const tied = comparison.tied.map((id) => labelOf(dataset, id));
      return {
        headline: `No configuration is ahead: ${tied.join(" and ")} did the same on every measure compared.`,
        detail: [ORDER_WORDS],
        best: null,
        tone,
      };
    }
    case "ordered": {
      const leader = dataset.configurations.find((configuration) => configuration.id === comparison.first);
      const first = labelOf(dataset, comparison.first);
      const second = labelOf(dataset, comparison.second);
      const measure = comparison.decided_by;
      const apart = comparison.separated === true;
      const through = leader?.critical_automated ?? 0;
      const detail = [
        measure ? decided(measure, comparison, second) : "",
        apart
          ? "The two intervals do not overlap, so the difference is unlikely to be chance."
          : "The two intervals overlap: with this many cases, the difference could be chance.",
        many > 2 ? `The other ${counted(many - 2, "configuration")} ${many - 2 === 1 ? "follows" : "follow"} in the same order.` : "",
        ORDER_WORDS,
      ].filter(Boolean);
      // Best is relative: when even the first let critical cases through, the headline says so.
      const still = through
        ? `, and it still let ${whole.format(through)} of ${counted(leader?.critical_cases ?? 0, "critical case")} through with no person reading ${through === 1 ? "it" : "them"}`
        : "";
      return {
        headline: apart
          ? `${first} did best on this dataset${still}.`
          : `${first} is listed first, but it is not clearly ahead of ${second}${still}.`,
        detail,
        best: apart ? comparison.first : null,
        tone: through ? "warn" : apart ? "ok" : tone,
      };
    }
  }
}

// Which configurations miss critical cases, one sentence each (none for an agent with no
// critical label).
export function criticalWords(configuration: EvaluatedConfiguration): string | null {
  const { critical_cases: cases, critical_missed: missed, critical_automated: automated } = configuration;
  if (!cases) return null;
  const all = cases === 1 ? "the one critical case" : `all ${counted(cases, "critical case")}`;
  const detected =
    missed === 0
      ? `${configuration.label} detected ${all}`
      : `${configuration.label} did not detect ${whole.format(missed)} of ${counted(cases, "critical case")}`;
  const decidedAlone =
    automated === 0
      ? "none was decided automatically"
      : `${whole.format(automated)} of ${whole.format(cases)} ${automated === 1 ? "was" : "were"} decided automatically, with no person reading ${automated === 1 ? "it" : "them"}`;
  return `${detected}; ${decidedAlone}.`;
}

// Whether the registry ran a configuration again itself.
export const RERUN_WORDS: Record<string, string> = {
  matched: "Re-run by the registry: it counted the same.",
  calls_a_model: "Not re-run: it calls a model, which the registry does not. These counts are the publisher's signed word.",
};

// What a configuration answers with: its models, or that it asks none.
export function modelWords(configuration: EvaluatedConfiguration): string {
  const asks = Object.values(configuration.providers).some((provider) => configuration.rerun === "calls_a_model" && provider.model);
  if (!configuration.models.length) return "No model reported";
  return `${asks ? "Model" : "Answered by"}: ${configuration.models.join(", ")}`;
}

function milliseconds(value: number): string {
  return value < 10 ? `${value.toFixed(2)} ms` : `${whole.format(Math.round(value))} ms`;
}

// The publisher's own measurements of a run, never a count the registry checked.
export function claimedWords(claimed: EvaluationClaimed): string {
  const speed = [
    claimed.latency_ms_p50 == null ? null : `half of the decisions took under ${milliseconds(claimed.latency_ms_p50)}`,
    claimed.latency_ms_p95 == null ? null : `19 in 20 under ${milliseconds(claimed.latency_ms_p95)}`,
  ].filter(Boolean);
  const cost =
    claimed.cost_usd_per_decision == null
      ? "cost not reported"
      : `USD ${claimed.cost_usd_per_decision.toFixed(4)} per decision`;
  return `${speed.length ? speed.join(", ") : "speed not reported"}; ${cost}`;
}

// The cases a provider failed on (there the engine sends the case to a person); null for a
// record that does not state it.
export function failedWords(claimed: EvaluationClaimed, n: number): string | null {
  if (claimed.failed_cases == null) return null;
  if (claimed.failed_cases === 0) return `No provider failure in the ${counted(n, "case")}.`;
  return `A provider failed on ${whole.format(claimed.failed_cases)} of ${counted(n, "case")}: each went to a person, so those cases say nothing of this configuration.`;
}

// How far the registry checked the counts itself: it runs again what it can run, which is every
// configuration that calls no model.
export function rerunSummary(configurations: EvaluatedConfiguration[]): string {
  const total = configurations.length;
  const rerun = configurations.filter((configuration) => configuration.rerun === "matched").length;
  const rest = total - rerun;
  if (!rest) {
    return total === 1
      ? "The registry ran the configuration again itself and counted the same, which shows the counts are real, not that the dataset is hard."
      : "The registry ran every configuration again itself and counted the same, which shows the counts are real, not that the dataset is hard.";
  }
  const unchecked =
    rest === 1
      ? "calls a model the registry does not call, so its counts rest on the publisher's signature"
      : "call a model the registry does not call, so their counts rest on the publisher's signature";
  if (!rerun) {
    return total === 1
      ? `The registry did not run it again: it ${unchecked}.`
      : `The registry ran none of them again: each calls a model the registry does not call, so their counts rest on the publisher's signature.`;
  }
  return (
    `The registry ran ${whole.format(rerun)} of ${counted(total, "configuration")} again itself and counted the same; ` +
    `the other ${rest === 1 ? "one" : whole.format(rest)} ${unchecked}.`
  );
}

// Why a stored evaluation is not shown.
export const NOT_SHOWN_WORDS: Record<NotShown["reason"], string> = {
  record: "the stored record is no longer the one that was signed",
  signature: "its signature does not hold",
  key: "it is signed with a key that is not one of this publisher's",
  key_revoked: "its key was revoked after it signed this",
};

// Every verified evaluation shown, across the datasets.
export function shownConfigurations(evaluation: AgentEvaluation): EvaluatedConfiguration[] {
  return evaluation.datasets.flatMap((dataset) => dataset.configurations);
}

// One sentence for the top of the page: who ran what, on what.
export function summaryWords(evaluation: AgentEvaluation): string {
  const agent = evaluation.agent;
  const publisher = evaluation.publisher.display_name || evaluation.publisher.namespace;
  if (evaluation.state === "recalled" || !agent) {
    return "Every version of this agent is recalled, or this one is. A recalled version is not evaluated here: do not use it.";
  }
  if (evaluation.state === "none") {
    return `Its publisher, ${publisher}, has published no evaluation of version ${agent.version}. There is nothing here to weigh yet.`;
  }
  if (evaluation.state === "not_verified") {
    return `An evaluation of version ${agent.version} was published, but none of it can be verified, so none of its numbers are shown.`;
  }
  const configurations = new Set(shownConfigurations(evaluation).map((configuration) => configuration.label)).size;
  const [only] = evaluation.datasets;
  const on =
    evaluation.datasets.length === 1 && only
      ? casesWords(only, agent.synthetic)
      : counted(evaluation.datasets.length, "dataset");
  return (
    `Its publisher, ${publisher}, ran ${counted(configurations, "configuration")} of version ${agent.version} on ${on} ` +
    "and published the results as signed counts. This page shows those counts, and what they do and do not tell you."
  );
}

// The Markdown a README takes: the badge, linked to this page.
export function badgeMarkdown(badgeUrl: string, pageUrl: string): string {
  return `[![Mendari evaluation](${badgeUrl})](${pageUrl})`;
}

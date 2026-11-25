// SPDX-License-Identifier: Apache-2.0
// lib/evaluation.ts words the registry's answer for the evaluation page. The page's tests read
// most of its sentences where they are shown (evaluation.spec.ts, evaluation-states.spec.ts);
// these hold the ones no seeded or recorded registry answer reaches: a singular, a figure the
// publisher did not report, several configurations none of which the registry ran again.
import { expect, test } from "@playwright/test";

import {
  badgeMarkdown,
  claimedWords,
  comparisonWords,
  counted,
  criticalWords,
  failedWords,
  modelWords,
  provenanceWords,
  rerunSummary,
} from "../lib/evaluation";
import type { Comparison, EvaluatedConfiguration, EvaluatedDataset } from "../lib/registry";

function configuration(changes: Partial<EvaluatedConfiguration>): EvaluatedConfiguration {
  return {
    id: "0".repeat(64),
    configuration: { reader: "agent" },
    label: "reader=agent",
    providers: { reader: { variant: "agent", type: "agent", model: "a-model" } },
    models: ["a-model"],
    engine: "0.1.0",
    record_format: "mendari.publisher-evaluation/1",
    signed: { key_id: "ed25519:0000000000000000", at: "2026-11-23T12:00:00Z", verified: true },
    rerun: "calls_a_model",
    figures: { format: "mendari.figures/1", n: 600, primary: "request", critical: [], accuracy: null, false_automation: null, outcomes: {} },
    critical_cases: 114,
    critical_missed: 0,
    critical_automated: 0,
    tokens: { input: 0, output: 0 },
    claimed: {},
    position: null,
    ...changes,
  };
}

// Three configurations, the first two told apart on the critical cases they automated.
function ordered(changes: Partial<Comparison>): EvaluatedDataset {
  const figure = (k: number) => ({ k, n: 114, low: 0, high: 0.1 });
  return {
    name: "test",
    sha256: "0".repeat(64),
    n: 600,
    provenance: "synthetic",
    labelled_by: [],
    critical: [],
    configurations: ["a", "b", "c"].map((id) => configuration({ id, label: `reader=${id}` })),
    comparison: {
      method: "mendari.rank/1",
      measures: ["critical_automated", "critical_detection", "false_automation", "accuracy"],
      status: "ordered",
      first: "a",
      second: "b",
      tied: [],
      decided_by: "critical_automated",
      figures: { first: figure(0), second: figure(14) },
      separated: true,
      ...changes,
    },
  } as EvaluatedDataset;
}

test("a configuration is named as having done best only when the registry names it", () => {
  // Alone in the first band: named, and its row can be marked.
  const alone = comparisonWords(ordered({ first_band: ["a"], best: "a" }));
  expect(alone.headline).toBe("reader=a did best on this dataset.");
  expect(alone.best).toBe("a");
  expect(alone.tone).toBe("ok");

  // Clearly ahead of the second, and a third the counts cannot tell from it: none is named.
  const close = comparisonWords(ordered({ first_band: ["a", "c"], best: null }));
  expect(close.headline).toBe(
    "No configuration is clearly best on this dataset: reader=a is listed first, but it is not clearly ahead of reader=c.",
  );
  expect(close.best).toBeNull();
  expect(close.tone).toBe("neutral");
  expect(close.detail).toContain("The two intervals do not overlap, so the difference is unlikely to be chance.");
  expect(close.detail).toContain(
    "reader=a is still not named as best: what separates it from reader=c is inside their intervals, and could be chance.",
  );

  // Several in the first band: each is named beside the first.
  const several = comparisonWords(ordered({ separated: false, first_band: ["a", "b", "c"], best: null }));
  expect(several.headline).toBe(
    "No configuration is clearly best on this dataset: reader=a is listed first, but it is not clearly ahead of reader=b or reader=c.",
  );
  expect(several.detail).toContain("The two intervals overlap: with this many cases, the difference could be chance.");
  expect(several.detail.join(" ")).not.toContain("still not named");

  // The registry's word is taken only for the configuration it lists first.
  expect(comparisonWords(ordered({ first_band: ["a"], best: "b" })).best).toBeNull();
});

test("a tie of several names each, and none as best", () => {
  const tied = comparisonWords(ordered({ status: "tied", first: null, second: null, tied: ["a", "b", "c"], first_band: ["a", "b", "c"], best: null }));
  expect(tied.headline).toBe(
    "No configuration is clearly best: reader=a, reader=b and reader=c did the same on every measure compared.",
  );
  expect(tied.best).toBeNull();
});

test("counts say what they count, in the singular and the plural", () => {
  expect(counted(1, "case")).toBe("1 case");
  expect(counted(0, "case")).toBe("0 cases");
  expect(counted(12500, "case")).toBe("12,500 cases");
});

test("critical cases, one sentence for each configuration", () => {
  expect(criticalWords(configuration({}))).toBe("reader=agent detected all 114 critical cases; none was decided automatically.");
  expect(criticalWords(configuration({ critical_missed: 1, critical_automated: 1 }))).toBe(
    "reader=agent did not detect 1 of 114 critical cases; 1 of 114 was decided automatically, with no person reading it.",
  );
  expect(criticalWords(configuration({ critical_cases: 1, critical_missed: 0, critical_automated: 0 }))).toBe(
    "reader=agent detected the one critical case; none was decided automatically.",
  );
  expect(criticalWords(configuration({ critical_cases: 0 }))).toBeNull(); // the agent names no critical label
});

test("how far the registry checked the counts itself", () => {
  const model = configuration({});
  const rules = configuration({ rerun: "matched" });
  expect(rerunSummary([rules])).toContain("The registry ran the configuration again itself and counted the same");
  expect(rerunSummary([rules, rules])).toContain("The registry ran every configuration again itself and counted the same");
  expect(rerunSummary([model])).toBe(
    "The registry did not run it again: it calls a model the registry does not call, so its counts rest on the publisher's signature.",
  );
  expect(rerunSummary([model, model])).toBe(
    "The registry ran none of them again: each calls a model the registry does not call, so their counts rest on the publisher's signature.",
  );
  expect(rerunSummary([rules, model, model])).toBe(
    "The registry ran 1 of 3 configurations again itself and counted the same; the other 2 call a model the registry " +
      "does not call, so their counts rest on the publisher's signature.",
  );
});

test("what the publisher measured, and what it did not report", () => {
  expect(claimedWords({})).toBe("speed not reported; cost not reported");
  expect(claimedWords({ latency_ms_p50: 0.049, latency_ms_p95: null, cost_usd_per_decision: 0 })).toBe(
    "half of the decisions took under 0.05 ms; USD 0.0000 per decision",
  );
  expect(failedWords({}, 600)).toBeNull(); // nothing claimed: nothing said
  expect(failedWords({ failed_cases: 0 }, 600)).toBe("No provider failure in the 600 cases.");
  expect(failedWords({ failed_cases: 1 }, 1)).toContain("A provider failed on 1 of 1 case:");
  expect(modelWords(configuration({ models: [] }))).toBe("No model reported");
  expect(modelWords(configuration({}))).toBe("Model: a-model");
});

test("where the cases come from, as a signed record states it", () => {
  const dataset = { provenance: "de_identified" } as EvaluatedDataset;
  expect(provenanceWords(dataset, false)).toBe("De-identified records, as the publisher's manifest states.");
  expect(provenanceWords({ ...dataset, provenance: "site_records" }, false)).toContain("Records from a care site");
  expect(provenanceWords({ ...dataset, provenance: null }, false)).toBe("Not stated in the signed record.");
});

test("the README's line is the badge, linked to the page", () => {
  expect(badgeMarkdown("https://registry.example/v1/agents/a/b/badge.svg", "https://hub.example/agents/a/b/evaluation")).toBe(
    "[![Mendari evaluation](https://registry.example/v1/agents/a/b/badge.svg)](https://hub.example/agents/a/b/evaluation)",
  );
});

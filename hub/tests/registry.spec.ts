// SPDX-License-Identifier: Apache-2.0
import { expect, test } from "@playwright/test";
import { RegistryUnavailable, siteEvidence, type EvidenceGroup } from "../lib/registry";

const counts = { k: 3, n: 5, low: 0.1, high: 0.95 };
const report = { report: "report-1", site: "site-1", site_name: "First site" };
const insufficient = { status: "insufficient evidence", method: "random effects", needs_sites: 3 };
const group: EvidenceGroup = {
  version: "1.0.0", digest: "digest-1", configuration: { router: "rules" }, remote_consent: false,
  sites: 1, cases: 5, unreported_tickets: 0, withdrawn_tickets: 0, superseded_reports: 0, tests_run: 0,
  left_out: [],
  figures: [{
    figure: "accuracy", label: "Accuracy", sites: [{ ...report, ...counts, insufficient_evidence: false }],
    sites_pooled: 1, cases_pooled: 5, pooled: { ...counts, status: "ok", method: "fixed" },
    random_effects: insufficient, homogeneity: insufficient, warning: null,
  }],
};
const response = { format: "mendari.site-evidence/1", pack: "demo/router", note: "", alpha: 0.05 };

async function read(body: unknown, status = 200) {
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json(body, { status });
  try {
    return await siteEvidence("demo", "router");
  } finally {
    globalThis.fetch = fetch;
  }
}

test("site evidence accepts extra registry fields, insufficient results and an empty registry response", async () => {
  const parsed = await read({ ...response, newer_field: true, groups: [{ ...group, future: {} }] });
  expect(parsed?.groups[0]).toEqual(group);
  expect(await read({ ...response, groups: [] })).toMatchObject({ groups: [] });
  expect(await read({}, 404)).toBeNull();
});

test("malformed nested evidence and evidence for another pack are unavailable", async () => {
  const figure = group.figures[0]!;
  const broken = [
    null,
    { ...group, configuration: null },
    { ...group, left_out: [null] },
    { ...group, figures: [{ ...figure, sites: [{ ...report, ...counts, site_name: {} }] }] },
    { ...group, figures: [{ ...figure, pooled: { ...figure.pooled, k: 6 } }] },
    { ...group, figures: [{ ...figure, random_effects: { status: "ok", method: "random effects" } }] },
  ];
  for (const entry of broken) {
    await expect(read({ ...response, groups: [entry] })).rejects.toBeInstanceOf(RegistryUnavailable);
  }
  await expect(read({ ...response, pack: "another/pack", groups: [] })).rejects.toBeInstanceOf(RegistryUnavailable);
});

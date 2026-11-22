// SPDX-License-Identifier: Apache-2.0
// The registry API (src/mendari/hub/registry.py), read on the server at request time.

const BASE = (process.env.REGISTRY_URL ?? "http://127.0.0.1:8100").replace(/\/$/, "");

export type Risk = { level: number; category: string };

import type { Figure } from "./figures";

// What the registry checked itself at publish (ADR-0012), beside every version.
export type Checked = {
  signature: { algorithm: string; key_id: string; verified: boolean };
  archive_sha256: string;
  digest: string;
  engine: "bounded" | "maintained" | null; // null: published before the registry recorded it
  engine_release?: string | null; // the registry's release whose bundled agents chose the engine
  card: "rendered" | "not_rendered";
  status_label: string | null;
  // The newest review of this version by the registry's reviewers, if any.
  review:
    | { status: "unreviewed" }
    | { status: "reviewed"; risk_level: number; matches_declared: boolean | null; reviewer: string; reviewed_at: string; note: string };
  publisher_type: string | null; // the namespace's, set by the registry after checking who publishes
  // The registry's own evaluation on the publisher's test set: a regression check, never a ranking.
  regression?: Regression | null;
  // The registry's counter-signature on the version (a signed mendari.registry-acceptance/1
  // record), written when it became listed. null: the registry had no signing key then.
  accepted?: Acceptance | null;
};

export type Acceptance = { format: string; pack: string; version: string; digest: string; archive_sha256: string; publisher_key_id: string; key_id: string; at: string; signature: string };

// The counter-signature in a sentence. The page states what the registry recorded; a pull checks
// the signature itself, against a key shipped with the client or pinned for the registry.
export function acceptanceWords(accepted: Acceptance | null | undefined): string {
  return accepted ? "Counter-signed by the registry" : "Not counter-signed";
}

export type Regression = {
  label: string;
  status: "measured" | "no_test_set" | "needs_remote" | "failed";
  reason?: string | null;
  compared_with?: string | null;
  regressions?: string[] | null;
  test_set?: { name: string | null; n: number | null } | null;
  accuracy?: Figure | null;
};

const REGRESSION_STATUS: Record<string, string> = {
  no_test_set: "Not run: the agent names no test set",
  needs_remote: "Not run: its default providers call a model, which the registry does not",
  failed: "Not run: the check could not finish",
};

// The regression check in a sentence. Its figure is the registry's own, on the publisher's set.
export function regressionWords(check: Regression | null | undefined, shown: (f: Figure | null | undefined) => string): string {
  if (!check) return "None: published before the registry ran the check";
  if (check.status !== "measured") return REGRESSION_STATUS[check.status] ?? check.status;
  const on = check.test_set?.name ? ` on its set ${check.test_set.name}` : "";
  const against = check.compared_with
    ? check.regressions?.length
      ? `; against ${check.compared_with}: ${check.regressions.join("; ")}`
      : `; against ${check.compared_with}: no regression`
    : "; nothing earlier to compare with";
  return `Accuracy ${shown(check.accuracy)}${on}${against}`;
}

// What the publisher says about the pack, unchecked.
export type Claimed = {
  publisher: { name: string; type: string } | null;
  risk: { level: number; category: string; declared_by: "publisher"; confirmed?: boolean | null };
  license: string | null;
  summary: string | null;
};

// What the registry works out on read beside a version (mendari.figures/1). It shows no figure
// it did not compute itself, so these stay null until it computes one.
export type VersionFigures = { format: string; benchmark: Figure | null; leaderboard: null };

export type PackSummary = {
  id: string;
  namespace: string;
  name: string;
  version: string;
  published_at: string;
  pack_name: string;
  summary: string;
  license: string;
  risk: Risk;
  standards: string[];
  questions: string[];
  primary: string;
  digest: string;
  key_id: string;
  archive_sha256: string;
  publisher_type: string | null;
  claimed_publisher: { name: string; type: string };
  status_label?: string;
  checked: Checked;
  claimed: Claimed;
  figures?: VersionFigures;
};

// A recall: the signed record (hub/yank.py), checked by the registry before it stored it, and a
// deletion, if the version's archive was removed.
export type Yank = {
  format: string;
  pack: string;
  version: string;
  reason: string;
  severity: "low" | "medium" | "high" | "critical";
  signer: "publisher" | "registry";
  key_id: string;
  at: string;
};
export type Deleted = { reason: string; at: string };
export type Recalled = { version: string; yank: Yank | null; deleted: Deleted | null };

// The card the registry rendered from the pack's own files; null for a version published before.
export type PackVersion = PackSummary & { card_markdown: string | null; yank?: Yank | null; deleted?: Deleted | null };

// `latest` is the newest version that is not recalled: null when every version is.
export type PackDetail = { id: string; versions: string[]; recalled?: Recalled[]; latest: PackVersion | null };

export type Namespace = {
  name: string;
  display_name: string;
  publisher_type: string;
  created_at: string;
  keys: { key_id: string; created_at: string }[];
  packs: string[];
};

export class RegistryUnavailable extends Error {}

async function get<T>(path: string): Promise<T | null> {
  const bypassSecret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  const headers: Record<string, string> = bypassSecret ? { "x-vercel-protection-bypass": bypassSecret } : {};
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      cache: "no-store",
      redirect: "error",
      headers,
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    throw new RegistryUnavailable("The registry cannot be reached right now.");
  }
  if (response.status === 404) return null;
  if (!response.ok) throw new RegistryUnavailable(`The registry answered HTTP ${response.status}.`);
  try {
    return (await response.json()) as T;
  } catch {
    throw new RegistryUnavailable("The registry returned an unreadable response.");
  }
}

export type SearchFilters = { q?: string; risk?: string; standard?: string; publisher_type?: string };

export async function search(filters: SearchFilters): Promise<PackSummary[]> {
  const params = new URLSearchParams(
    Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1])),
  );
  return (await get<PackSummary[]>(`/v1/search?${params}`)) ?? [];
}

export const pack = (namespace: string, name: string) =>
  get<PackDetail>(`/v1/packs/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`);

export const packVersion = (namespace: string, name: string, version: string) =>
  get<PackVersion>(
    `/v1/packs/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/${encodeURIComponent(version)}`,
  );

export const namespace = (name: string) => get<Namespace>(`/v1/namespaces/${encodeURIComponent(name)}`);

export const RISK_WORDS: Record<number, string> = {
  0: "Administrative",
  1: "Workflow",
  2: "Clinical support, human review by default",
  3: "Clinical support, governance needed to automate",
  4: "Never automated",
};

export const ENGINE_WORDS: Record<string, string> = {
  bounded: "The engine for agents from a registry: RE2 patterns, input limits and a 1 s CPU budget per decision",
  maintained: "The maintained engine: a copy of an agent the registry's own release bundles",
};

// What the registry's review says of a version, in a sentence.
export function reviewWords(review: Checked["review"], declared: number): string {
  if (review.status !== "reviewed") return "Not reviewed by the registry: the risk level is the publisher's own";
  const day = review.reviewed_at.slice(0, 10);
  const found =
    review.matches_declared === true
      ? `risk level ${review.risk_level} confirmed`
      : review.matches_declared === false
        ? `risk level ${review.risk_level} found, where the publisher declared ${declared}`
        : `risk level ${review.risk_level} found`;
  return `Reviewed by the registry on ${day}: ${found}${review.note ? `. ${review.note}` : ""}`;
}

// The declared risk level, with what a review found of it.
export function riskWords(risk: Claimed["risk"], review: Checked["review"]): string {
  if (risk.confirmed === true) return "declared by the publisher, confirmed by the registry's review";
  if (risk.confirmed === false && review.status === "reviewed") {
    return `declared by the publisher; the registry's review found level ${review.risk_level}`;
  }
  return "declared by the publisher, not confirmed by a review";
}

// Site evidence (mendari.site-evidence/1, LP-22g): sites' signed counts of a pack, worked out by
// the registry from counts it verified. Shown apart from everything else on a pack's page.
export type SiteFigure = Figure & { report: string; site: string; site_name: string; insufficient_evidence: boolean };
export type Pooled = Partial<Figure> & { method: string; status: string; needs_sites?: number };
export type RandomEffects = { method: string; status: string; estimate?: number; low?: number; high?: number; needs_sites?: number };
export type Homogeneity = { method?: string; status: string; p?: number; differ?: boolean; seed?: number; needs_sites?: number };
export type EvidenceFigure = {
  figure: string;
  label: string;
  sites: SiteFigure[];
  sites_pooled: number;
  cases_pooled: number;
  pooled: Pooled;
  random_effects: RandomEffects;
  homogeneity: Homogeneity;
  warning: string | null;
};
export type EvidenceGroup = {
  version: string;
  digest: string;
  configuration: Record<string, string>;
  remote_consent: boolean;
  sites: number;
  cases: number;
  left_out: { report: string; site: string; site_name: string; reason: string }[];
  unreported_tickets: number;
  withdrawn_tickets: number;
  superseded_reports: number;
  figures: EvidenceFigure[];
  tests_run: number;
};
export type SiteEvidence = { format: string; pack: string; note: string; alpha: number; groups: EvidenceGroup[] };

export const siteEvidence = (namespace: string, name: string) =>
  get<SiteEvidence>(`/v1/packs/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/site-evidence`);

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
  engine: "bounded" | "maintained";
  engine_release?: string | null; // the registry's release whose bundled agents chose the engine
  card: "rendered";
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

export type Acceptance = { format: string; agent: string; version: string; digest: string; archive_sha256: string; publisher_key_id: string; key_id: string; at: string; signature: string };

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
  if (!check) return "None: the registry ran no check when it was published";
  if (check.status !== "measured") return REGRESSION_STATUS[check.status] ?? check.status;
  const on = check.test_set?.name ? ` on its set ${check.test_set.name}` : "";
  const against = check.compared_with
    ? check.regressions?.length
      ? `; against ${check.compared_with}: ${check.regressions.join("; ")}`
      : `; against ${check.compared_with}: no regression`
    : "; nothing earlier to compare with";
  return `Accuracy ${shown(check.accuracy)}${on}${against}`;
}

// What the publisher says about the agent, unchecked.
export type Claimed = {
  publisher: { name: string; type: string } | null;
  risk: { level: number; category: string; declared_by: "publisher"; confirmed?: boolean | null };
  license: string | null;
  summary: string | null;
};

// What the registry works out on read beside a version (mendari.figures/1). It shows no figure
// it did not compute itself, so these stay null until it computes one.
export type VersionFigures = { format: string; benchmark: Figure | null; leaderboard: null };

export type AgentSummary = {
  id: string;
  namespace: string;
  name: string;
  version: string;
  published_at: string;
  agent_name: string;
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
  agent: string;
  version: string;
  reason: string;
  severity: "low" | "medium" | "high" | "critical";
  signer: "publisher" | "registry";
  key_id: string;
  at: string;
};
export type Deleted = { reason: string; at: string };
export type Recalled = { version: string; yank: Yank | null; deleted: Deleted | null };

// The card the registry rendered from the agent's own files.
export type AgentVersion = AgentSummary & { card_markdown: string; yank?: Yank | null; deleted?: Deleted | null };

// `latest` is the newest version that is not recalled: null when every version is.
export type AgentDetail = { id: string; versions: string[]; recalled?: Recalled[]; latest: AgentVersion | null };

export type Namespace = {
  name: string;
  display_name: string;
  publisher_type: string;
  created_at: string;
  // `revoked_at`: when the registry's operator revoked the key, null while it is live.
  keys: { key_id: string; created_at: string; revoked_at: string | null }[];
  agents: string[];
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

export async function search(filters: SearchFilters): Promise<AgentSummary[]> {
  const params = new URLSearchParams(
    Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1])),
  );
  return (await get<AgentSummary[]>(`/v1/search?${params}`)) ?? [];
}

// An agent's reads, under the registry's /v1/agents/<namespace>/<name>.
const agentPath = (namespace: string, name: string) =>
  `/v1/agents/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`;

export const agent = (namespace: string, name: string) => get<AgentDetail>(agentPath(namespace, name));

export const agentVersion = (namespace: string, name: string, version: string) =>
  get<AgentVersion>(`${agentPath(namespace, name)}/${encodeURIComponent(version)}`);

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

// Site evidence (mendari.site-evidence/1, LP-22g): sites' signed counts of an agent, worked out by
// the registry from counts it verified. Shown apart from everything else on an agent's page.
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
export type SiteEvidence = { format: string; agent: string; note: string; alpha: number; groups: EvidenceGroup[] };

export const siteEvidence = (namespace: string, name: string) =>
  get<SiteEvidence>(`${agentPath(namespace, name)}/site-evidence`);

// Publisher evaluations (mendari.publisher-evaluations/1, ADR-0020): the publisher's signed
// evaluations of one version, one per configuration and public dataset. Every share is the
// registry's, from counts; latency, cost and models are the publisher's own word.
export type EvaluationCritical = {
  question: string;
  label: string;
  detected: Figure;
  automated_misses: number;
  automated: Figure;
  insufficient_evidence: boolean;
};
export type EvaluationFigures = {
  format: string;
  n: number;
  primary: string;
  critical: EvaluationCritical[];
  accuracy: Figure | null;
  false_automation: Figure | null;
  outcomes: Record<string, { count: number; share: Figure | null }>;
};
// The publisher's own figures of a run, never a count: `failed_cases` (the cases a provider
// failed on) in every record shown; none of them for a record that no longer verifies.
export type EvaluationClaimed = {
  latency_ms_p50?: number | null;
  latency_ms_p95?: number | null;
  cost_usd_per_decision?: number | null;
  failed_cases?: number;
};
export type PublisherEvaluation = {
  id: string;
  configuration: Record<string, string>;
  // n and sha256 are null for a record that no longer verifies: nothing it says is shown.
  dataset: { name: string; n: number | null; sha256: string | null };
  rerun: "matched" | "calls_a_model";
  rerun_words: string;
  key_id: string;
  models: string[];
  claimed: EvaluationClaimed;
  created_at: string;
  // "key revoked" or "not verified": listed, left out of the comparison.
  excluded: string | null;
  signature: { key_id: string; verified: boolean; problem: string | null };
  figures: EvaluationFigures | null;
};
export type PublisherEvaluations = {
  format: string;
  agent: string;
  version: string;
  note: string;
  evaluations: PublisherEvaluation[];
  superseded: number;
};

// null: no such version.
export const publisherEvaluations = (namespace: string, name: string, version: string) =>
  get<PublisherEvaluations>(`${agentPath(namespace, name)}/${encodeURIComponent(version)}/evaluations`);

// An agent's published evaluation (mendari.agent-evaluation/1, src/mendari/hub/evaluation_page.py):
// what the evaluation page shows. The registry works it out on every read from the publisher's
// signed evaluations that still verify; this site only prints it.
export type EvaluationState = "evaluated" | "none" | "not_verified" | "recalled";
export type ComparisonStatus = "ordered" | "tied" | "one_configuration" | "no_critical_label" | "insufficient_evidence";
export type Measure = "critical_automated" | "critical_detection" | "false_automation" | "accuracy";
export type MeasuredFigure = Figure & { question?: string; label?: string };
export type Comparison = {
  method: string;
  measures: Measure[];
  status: ComparisonStatus;
  first: string | null;
  second: string | null;
  tied: string[];
  decided_by: Measure | null;
  figures: { first: MeasuredFigure; second: MeasuredFigure } | null;
  separated: boolean | null; // whether the two intervals keep apart; false: it could be chance
  // The first configuration and every other the counts cannot tell from it, in the order shown;
  // and the one that did best, which the registry names only when it is alone there (ADR-0020,
  // amended 2026-11-24).
  first_band: string[];
  best: string | null;
};
export type EvaluatedConfiguration = {
  id: string; // the signed record's id: the SHA-256 of its canonical JSON
  configuration: Record<string, string>;
  label: string; // "slot=variant, slot=variant"
  providers: Record<string, { variant: string; type: string; model: string | null }>;
  models: string[];
  engine: string; // the Mendari release that ran it, as the publisher states
  record_format: string;
  signed: { key_id: string; at: string; verified: true };
  rerun: "matched" | "calls_a_model";
  figures: EvaluationFigures;
  critical_cases: number;
  critical_missed: number;
  critical_automated: number;
  tokens: { input: number; output: number };
  claimed: EvaluationClaimed;
  position: number | null; // its place in the comparison's order; null where nothing is ordered
};
export type EvaluatedDataset = {
  name: string;
  sha256: string;
  n: number;
  provenance: "synthetic" | "de_identified" | "site_records" | null; // null: the manifest does not say
  labelled_by: string[]; // the publisher's own words
  critical: { question: string; label: string; n: number }[];
  comparison: Comparison;
  configurations: EvaluatedConfiguration[];
};
export type NotShown = {
  id: string;
  configuration: string;
  dataset: string;
  key_id: string;
  reason: "record" | "key" | "signature" | "key_revoked";
};
export type BadgeWords = { state: string; label: string; message: string; tone: "info" | "warn" | "bad" | "plain" };
export type AgentEvaluation = {
  format: string;
  id: string;
  state: EvaluationState;
  note: string;
  publisher: { namespace: string; display_name: string; publisher_type: string };
  versions: { shown: string | null; latest: string | null; evaluated: string[] };
  agent: {
    name: string | null;
    version: string;
    digest: string | null;
    published_at: string | null;
    status_label: string | null;
    synthetic: boolean;
    primary: string | null;
  } | null; // null: recalled
  datasets: EvaluatedDataset[];
  not_shown: NotShown[];
  superseded: number;
  links: { page: string | null };
  badge: BadgeWords;
};

// null: no such agent (or version).
export const agentEvaluation = (namespace: string, name: string, version?: string) =>
  get<AgentEvaluation>(
    `${agentPath(namespace, name)}/evaluation${version ? `?version=${encodeURIComponent(version)}` : ""}`,
  );

// The badge as the registry serves it (an SVG of words and numbers, src/mendari/hub/badge.py), for
// this site to show as an image. null when it cannot be read as one: the page shows none.
export async function agentBadge(namespace: string, name: string): Promise<string | null> {
  const bypassSecret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  const headers: Record<string, string> = bypassSecret ? { "x-vercel-protection-bypass": bypassSecret } : {};
  try {
    const response = await fetch(`${BASE}${agentPath(namespace, name)}/badge.svg`, {
      cache: "no-store",
      redirect: "error",
      headers,
      signal: AbortSignal.timeout(10_000),
    });
    const type = response.headers.get("content-type") ?? "";
    if (!response.ok || !type.startsWith("image/svg+xml")) return null;
    const image = await response.text();
    return image.length <= 8_000 && image.startsWith("<svg ") ? image : null;
  } catch {
    return null;
  }
}

// The registry's address as a README can use it: REGISTRY_PUBLIC_URL when the site reaches the
// registry at an address the public cannot (a container's name), else REGISTRY_URL when it is
// https or the site itself is opened on this machine. null: the page gives no badge address.
export function publicRegistry(siteIsLocal: boolean): string | null {
  const given = (process.env.REGISTRY_PUBLIC_URL ?? "").replace(/\/$/, "");
  if (given) return /^https:\/\/[^\s"'<>()]+$/.test(given) || siteIsLocal ? given : null;
  return BASE.startsWith("https://") || siteIsLocal ? BASE : null;
}

export const badgePath = (namespace: string, name: string) => `${agentPath(namespace, name)}/badge.svg`;

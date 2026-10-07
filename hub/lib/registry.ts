// SPDX-License-Identifier: Apache-2.0
// The registry API (src/mendari/hub/registry.py), read on the server at request time.

const BASE = (process.env.REGISTRY_URL ?? "http://127.0.0.1:8100").replace(/\/$/, "");

export type Risk = { level: number; category: string };

import type { Figure } from "./figures";

export type Benchmark = { dataset: string | null; n: number | null; accuracy: number | null } | null;

// A leaderboard as the registry serves it beside a version (mendari.figures/1): ranked against the
// version's own digest, by set of records, each row with its figures. Null when malformed.
export type ServedRow = {
  config: string;
  dataset: string;
  n: number;
  pack_version: string;
  macro_f1?: number | null;
  ece?: number | null;
  latency_p50_ms?: number | null;
  cost_per_decision_usd?: number | null;
  rank: number | null;
  standing: string;
  standing_text: string | null;
  group: number;
  figures: {
    accuracy: Figure | null;
    false_automation: Figure | null;
    automation: Figure | null;
    critical: { question: string; label: string; detected: Figure; auto_missed: number; insufficient_evidence: boolean }[];
    critical_named?: boolean; // the pack has several critical labels: name each, as the card does
  };
};

export type BoardFigures = {
  ranking: { method: string; min_positives: number };
  groups: { dataset: string; sha256: string | null; n: number; about: string[]; label: string }[];
  rows: ServedRow[];
};

// What the registry works out on read beside a version; absent from an older registry.
export type VersionFigures = { format: string; benchmark: Figure | null; leaderboard: BoardFigures | null };

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
  // Derived by the registry from the manifest's card.provenance; absent on versions published before it.
  status_label?: string;
  benchmark: Benchmark;
  figures?: VersionFigures;
};

export type PackVersion = PackSummary & {
  card_markdown: string | null;
  leaderboard: Record<string, unknown> | null; // as the publisher sent it; the Hub shows figures.leaderboard
};

export type PackDetail = { id: string; versions: string[]; latest: PackVersion };

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
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { cache: "no-store" });
  } catch {
    throw new RegistryUnavailable("The registry cannot be reached right now.");
  }
  if (response.status === 404) return null;
  if (!response.ok) throw new RegistryUnavailable(`The registry answered HTTP ${response.status}.`);
  return (await response.json()) as T;
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

// The registry API (src/mendari/hub/registry.py), read on the server at request time.

const BASE = (process.env.REGISTRY_URL ?? "http://127.0.0.1:8100").replace(/\/$/, "");

export type Risk = { level: number; category: string };

export type Benchmark = { dataset: string | null; n: number | null; accuracy: number | null } | null;

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
  benchmark: Benchmark;
};

export type LeaderboardRow = {
  config: string;
  dataset: string;
  n: number;
  accuracy: number | null;
  macro_f1: number | null;
  ece: number | null;
  auto_rate: number | null;
  false_automation_rate: number | null;
  latency_p50_ms: number | null;
  cost_per_decision_usd: number | null;
  pack_version: string;
};

export type PackVersion = PackSummary & {
  card_markdown: string | null;
  leaderboard: { dataset?: string; rows?: LeaderboardRow[]; [key: string]: unknown } | null;
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

export const percent = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined ? "n/a" : `${(value * 100).toFixed(digits)}%`;

export const RISK_WORDS: Record<number, string> = {
  0: "Administrative",
  1: "Workflow",
  2: "Clinical support, human review by default",
  3: "Clinical support, governance needed to automate",
  4: "Never automated",
};

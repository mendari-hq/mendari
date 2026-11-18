// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";
import { RISK_WORDS, search, type PackSummary, type SearchFilters } from "@/lib/registry";

export const dynamic = "force-dynamic";

type Params = Record<string, string | string[] | undefined>;

// Next exposes repeated query keys as arrays. A shared search URL should select exactly the
// value the form displays, and only the public search fields should reach the registry.
function field(params: Params, name: string): string {
  const value = params[name];
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

const PUBLISHERS = ["verified", "institutional", "community"];

export default async function Home({ searchParams }: { searchParams: Promise<Params> }) {
  const params = await searchParams;
  const filters: SearchFilters = {
    q: field(params, "q"),
    risk: field(params, "risk"),
    standard: field(params, "standard"),
    publisher_type: field(params, "publisher_type"),
  };
  const invalidRisk = Boolean(filters.risk && !/^[0-4]$/.test(filters.risk));
  const filtered = Object.values(filters).some(Boolean);
  const packs = invalidRisk ? [] : await search(filters);
  return (
    <>
      <header className="catalog-intro">
        <h1>Decision Packs</h1>
        <p className="lead">
          Browse signed healthcare decision logic. Read each pack’s Decision Card and validate it
          on your own data before relying on it.
        </p>
      </header>
      <div className="catalog">
        <form className="search" role="search" action="/" aria-labelledby="filters-heading">
          <h2 id="filters-heading">Find a pack</h2>
          <label>
            Search
            <input name="q" type="search" defaultValue={filters.q} placeholder="Name or keyword" />
          </label>
          <label>
            Risk level
            <select name="risk" defaultValue={filters.risk} aria-invalid={invalidRisk || undefined}
              aria-describedby={invalidRisk ? "risk-error" : undefined}>
              <option value="">Any risk level</option>
              {invalidRisk && <option value={filters.risk}>Unrecognized level</option>}
              {Object.entries(RISK_WORDS).map(([level, meaning]) => (
                <option key={level} value={level}>{level} · {meaning}</option>
              ))}
            </select>
          </label>
          {invalidRisk && <p className="form-error" id="risk-error">Choose a risk level from 0 to 4, or any risk level.</p>}
          <label>
            Standard
            <select name="standard" defaultValue={filters.standard}>
              <option value="">Any standard</option>
              <option value="FHIR">FHIR R4</option>
              <option value="CDS Hooks">CDS Hooks</option>
              {filters.standard && !["FHIR", "CDS Hooks"].includes(filters.standard) &&
                <option value={filters.standard}>{filters.standard}</option>}
            </select>
          </label>
          <label>
            Publisher
            <select name="publisher_type" defaultValue={filters.publisher_type}>
              <option value="">Any publisher</option>
              {PUBLISHERS.map((kind) => (
                <option key={kind} value={kind}>{kind.charAt(0).toUpperCase() + kind.slice(1)}</option>
              ))}
              {filters.publisher_type && !PUBLISHERS.includes(filters.publisher_type) &&
                <option value={filters.publisher_type}>{filters.publisher_type}</option>}
            </select>
          </label>
          <button type="submit">Search</button>
        </form>
        <section className="catalog-results" aria-labelledby="results-heading">
          <div className="results-heading">
            <h2 id="results-heading">{filtered ? "Search results" : "All packs"}
              {!invalidRisk && <span className="result-count">{packs.length}</span>}
            </h2>
            {filtered && <Link href="/" className="reset">Clear filters</Link>}
          </div>
          {packs.length ? (
            <ul className="results" aria-label="Packs">
              {packs.map((item) => <PackCard key={item.id} item={item} />)}
            </ul>
          ) : (
            <div className="empty">
              <p>{invalidRisk ? "Update the risk level to search." : filtered ? "No pack matches these filters." : "No packs are published yet."}</p>
            </div>
          )}
        </section>
      </div>
    </>
  );
}

function PackCard({ item }: { item: PackSummary }) {
  return (
    <li className="card">
      <div className="card-meta">
        <span className="mono muted">{item.namespace}</span>
        <span className="mono muted">v{item.version}</span>
      </div>
      <h3><Link href={`/packs/${item.namespace}/${item.name}`}>{item.pack_name} <span className="card-arrow" aria-hidden="true">→</span></Link></h3>
      <p>{item.summary}</p>
      {item.standards.length > 0 && <div className="row standards">
        {item.standards.map((standard) => <span className="chip" key={standard}>{standard}</span>)}
      </div>}
      <div className="row card-status">
        {item.checked.publisher_type && <span className={`badge ${item.checked.publisher_type}`}>{item.checked.publisher_type} publisher</span>}
        <span className={`badge risk-${item.claimed.risk.level}`} title={`${RISK_WORDS[item.claimed.risk.level]}, declared by the publisher`}>
          Risk {item.claimed.risk.level} (declared)
        </span>
      </div>
    </li>
  );
}

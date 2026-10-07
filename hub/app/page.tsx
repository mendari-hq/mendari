// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";
import { shown } from "@/lib/figures";
import { RISK_WORDS, search, type PackSummary } from "@/lib/registry";

export const dynamic = "force-dynamic";

type Params = { q?: string; risk?: string; standard?: string; publisher_type?: string };

export default async function Home({ searchParams }: { searchParams: Promise<Params> }) {
  const filters = await searchParams;
  const packs = await search(filters);
  const filtered = Boolean(filters.q || filters.risk || filters.standard || filters.publisher_type);
  return (
    <>
      <h1>Decision Packs</h1>
      <p className="lead">
        Healthcare decisions packaged with their evidence: a Decision Card, a benchmark, a provider
        leaderboard and a signed archive you can validate on your own data.
      </p>
      <form className="search" role="search" action="/">
        <label>
          Search
          <input name="q" type="search" defaultValue={filters.q ?? ""} placeholder="referral, prior authorization…" />
        </label>
        <label>
          Risk level
          <select name="risk" defaultValue={filters.risk ?? ""}>
            <option value="">Any</option>
            {[0, 1, 2, 3, 4].map((level) => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
        </label>
        <label>
          Standard
          <select name="standard" defaultValue={filters.standard ?? ""}>
            <option value="">Any</option>
            <option value="FHIR">FHIR R4</option>
            <option value="CDS Hooks">CDS Hooks</option>
          </select>
        </label>
        <label>
          Publisher
          <select name="publisher_type" defaultValue={filters.publisher_type ?? ""}>
            <option value="">Any</option>
            <option value="verified">Verified</option>
            <option value="institutional">Institutional</option>
            <option value="community">Community</option>
          </select>
        </label>
        <button type="submit">Search</button>
      </form>
      {packs.length ? (
        <ul className="results" aria-label="Packs">
          {packs.map((item) => <PackCard key={item.id} item={item} />)}
        </ul>
      ) : (
        <p className="empty">{filtered ? "No pack matches these filters." : "No packs are published yet."}</p>
      )}
    </>
  );
}

function PackCard({ item }: { item: PackSummary }) {
  return (
    <li className="card">
      <div className="row">
        <h3><Link href={`/packs/${item.namespace}/${item.name}`}>{item.pack_name}</Link></h3>
        <span className="mono muted">{item.id}@{item.version}</span>
        <span className="grow" />
        {item.publisher_type ? <span className={`badge ${item.publisher_type}`}>{item.publisher_type}</span> : null}
        <span className={`badge risk-${item.risk.level}`} title={RISK_WORDS[item.risk.level]}>Risk {item.risk.level}</span>
      </div>
      <p>{item.summary}</p>
      <div className="row">
        {item.standards.map((standard) => <span className="chip" key={standard}>{standard}</span>)}
        <span className="grow" />
        {item.figures?.benchmark ? (
          <span className="muted">Benchmark accuracy {shown(item.figures.benchmark)}</span>
        ) : null}
      </div>
    </li>
  );
}

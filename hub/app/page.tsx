// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";
import { RISK_WORDS, search, type PackSummary } from "@/lib/registry";

export const dynamic = "force-dynamic";

type Params = { q?: string; risk?: string; standard?: string; publisher_type?: string };

export default async function Home({ searchParams }: { searchParams: Promise<Params> }) {
  const filters = await searchParams;
  const packs = await search(filters);
  const filtered = Boolean(filters.q || filters.risk || filters.standard || filters.publisher_type);
  const filterKey = `${filters.q ?? ""}|${filters.risk ?? ""}|${filters.standard ?? ""}|${filters.publisher_type ?? ""}`;
  const verifiedCount = packs.filter(
    (item) => item.checked.publisher_type === "verified" || item.checked.publisher_type === "institutional",
  ).length;
  const standardsCount = new Set(packs.flatMap((item) => item.standards)).size;

  return (
    <>
      <section className="hub-hero">
        <div className="wrap">
          <div className="hero-grid">
            <div className="hero-copy">
              <p className="eyebrow">
                <span className="eyebrow-dot" aria-hidden="true" />
                Signed Decision Pack Registry
              </p>
              <h1>Decision Packs</h1>
              <p className="lead">
                Healthcare decisions packaged as signed archives, each with a Decision Card the registry
                rendered from the pack&apos;s own files. Validate a pack on your own data before you rely on it.
              </p>
              <dl className="hero-metrics" aria-label="Registry summary">
                <div className="hero-metric">
                  <dt>{filtered ? "Matching packs" : "Published packs"}</dt>
                  <dd>{packs.length}</dd>
                </div>
                <div className="hero-metric">
                  <dt>Verified publishers</dt>
                  <dd>{verifiedCount}</dd>
                </div>
                <div className="hero-metric">
                  <dt>Standards covered</dt>
                  <dd>{standardsCount}</dd>
                </div>
              </dl>
            </div>
            <div className="hero-cli" aria-label="Install and verify a Decision Pack">
              <p className="terminal-bar">
                <span>Quick install</span>
                <span>Ed25519 verified</span>
              </p>
              <pre>
                <code>
                  <span className="prompt" aria-hidden="true">$ </span>pip install mendari{"\n"}
                  <span className="prompt" aria-hidden="true">$ </span>mendari pull mendari/referral-router{"\n"}
                  <span className="prompt" aria-hidden="true">$ </span>mendari eval mendari/referral-router{"\n"}
                  <span className="comment">    --dataset your-cases.jsonl</span>
                </code>
              </pre>
            </div>
          </div>

          <div className="search-shell">
            <form key={filterKey} className="search" role="search" action="/">
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
              <div className="search-actions">
                <button type="submit">Search</button>
                {filtered ? <Link href="/" className="clear-filters">Clear</Link> : null}
              </div>
            </form>
          </div>
        </div>
      </section>

      <section className="wrap catalog-section">
        {packs.length ? (
          <>
            <div className="catalog-bar">
              <h2 className="sr-only">Published packs</h2>
              <p className="catalog-count">
                <strong>{packs.length}</strong> {packs.length === 1 ? "pack" : "packs"}{filtered ? " matching filters" : " available"}
              </p>
              <p className="muted catalog-hint">
                Risk levels are declared by publishers until reviewed by the registry.
              </p>
            </div>
            <ul className="results" aria-label="Packs">
              {packs.map((item) => <PackCard key={item.id} item={item} />)}
            </ul>
          </>
        ) : (
          <div className="empty-state">
            <p className="empty">
              {filtered ? (
                <>No pack matches these filters. <Link href="/">Clear filters</Link></>
              ) : (
                "No packs are published yet."
              )}
            </p>
          </div>
        )}
      </section>

      <div className="wrap federated-wrap">
        <section className="federated-banner" aria-labelledby="federated-heading">
          <div className="federated-copy">
            <p className="section-kicker">Zero-PHI multi-site validation</p>
            <h2 id="federated-heading">Independent multi-site validation, without sharing PHI</h2>
            <p className="federated-text">
              Verified health systems evaluate packs on their own chart-reviewed cases and publish
              cryptographically signed counts (never patient records). Open any pack to inspect its
              multi-site forest plot and homogeneity tests.
            </p>
          </div>
          <div className="federated-cmd">
            <p className="terminal-bar">
              <span>Share site counts</span>
              <span>No PHI leaves site</span>
            </p>
            <pre>
              <code>
                <span className="prompt" aria-hidden="true">$ </span>mendari eval &lt;pack&gt; --dataset your-cases.jsonl --share
              </code>
            </pre>
          </div>
        </section>
      </div>
    </>
  );
}

function PackCard({ item }: { item: PackSummary }) {
  return (
    <li className="card">
      <div className="card-top">
        <div className="card-heading">
          <h3><Link href={`/packs/${item.namespace}/${item.name}`}>{item.pack_name}</Link></h3>
          <p className="card-id mono muted">{item.id}@{item.version}</p>
        </div>
        <div className="card-badges">
          {item.checked.publisher_type ? (
            <span className={`badge ${item.checked.publisher_type}`}>{item.checked.publisher_type}</span>
          ) : null}
          <span
            className={`badge risk-${item.claimed.risk.level}`}
            title={`${RISK_WORDS[item.claimed.risk.level]}, declared by the publisher`}
          >
            Risk {item.claimed.risk.level} (declared)
          </span>
        </div>
      </div>
      <p className="card-summary">{item.summary}</p>
      <div className="card-footer">
        <div className="card-tags">
          {item.standards.map((standard) => <span className="chip" key={standard}>{standard}</span>)}
          {item.primary ? (
            <span className="card-outcome muted">
              Decides <code>{item.primary}</code>
            </span>
          ) : null}
        </div>
        <span className="card-cta" aria-hidden="true">Inspect →</span>
      </div>
    </li>
  );
}

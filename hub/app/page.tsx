// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";
import { RegistryUnavailable, RISK_WORDS, search, type PackSummary } from "@/lib/registry";

import { SearchBar } from "./search-bar";

export const dynamic = "force-dynamic";

type Params = { q?: string; risk?: string; standard?: string; publisher_type?: string };

const PUBLISHER_BADGE = new Set(["verified", "institutional", "community"]);

export default async function Home({ searchParams }: { searchParams: Promise<Params> }) {
  const filters = await searchParams;
  let packs: PackSummary[] = [];
  let unavailable = false;
  try {
    packs = await search(filters);
  } catch (error) {
    if (error instanceof RegistryUnavailable) unavailable = true;
    else throw error;
  }
  const filtered = Boolean(filters.q || filters.risk || filters.standard || filters.publisher_type);
  const filterKey = `${filters.q ?? ""}|${filters.risk ?? ""}|${filters.standard ?? ""}|${filters.publisher_type ?? ""}`;
  const verifiedPublishers = new Set(
    packs
      .filter((item) => item.checked.publisher_type === "verified" || item.checked.publisher_type === "institutional")
      .map((item) => item.namespace),
  ).size;
  const standardsCount = new Set(packs.flatMap((item) => item.standards)).size;

  return (
    <>
      <section className="hub-hero">
        <div className="wrap">
          <div className="hero-grid">
            <div className="hero-copy">
              <p className="eyebrow">
                <span className="eyebrow-dot" aria-hidden="true" />
                Mendari Agent Hub · Signed Registry
              </p>
              <h1>Healthcare AI Agents</h1>
              <p className="lead">
                Browse vetted healthcare AI agents with published evals, deterministic risk caps, and multi-site hospital evidence. Builders wrap any LLM, LangGraph, or HTTP agent in Mendari and publish signed scorecards here.
              </p>
              <dl className="hero-metrics" aria-label="Registry summary">
                <div className="hero-metric">
                  <dt>{filtered ? "Matching agents" : "Published agents"}</dt>
                  <dd>{unavailable ? "—" : packs.length}</dd>
                </div>
                <div className="hero-metric">
                  <dt>Verified publishers</dt>
                  <dd>{unavailable ? "—" : verifiedPublishers}</dd>
                </div>
                <div className="hero-metric">
                  <dt>EHR standards</dt>
                  <dd>{unavailable ? "—" : standardsCount}</dd>
                </div>
              </dl>
            </div>
            <div className="hero-cli" aria-label="Pull and shadow-test an agent or publish your own">
              <p className="terminal-bar">
                <span>Pull to shadow-test · or publish yours</span>
                <span>Ed25519 verified</span>
              </p>
              <pre>
                <code>
                  <span className="comment"># Hospital IT: pull &amp; shadow-test locally</span>{"\n"}
                  <span className="prompt" aria-hidden="true">$ </span>mendari pull mendari/portal-message-agent{"\n"}
                  <span className="prompt" aria-hidden="true">$ </span>mendari eval mendari/portal-message-agent --dataset your-cases.jsonl{"\n"}
                  <span className="comment"># AI Builders: wrap an agent &amp; publish scorecard</span>{"\n"}
                  <span className="prompt" aria-hidden="true">$ </span>mendari new --agent my-agent --id my-org/my-agent{"\n"}
                  <span className="prompt" aria-hidden="true">$ </span>mendari push my-agent --key keys/publisher.key
                </code>
              </pre>
            </div>
          </div>

          <div className="search-shell">
            <SearchBar
              filterKey={filterKey}
              q={filters.q}
              risk={filters.risk}
              standard={filters.standard}
              publisherType={filters.publisher_type}
              filtered={filtered}
            />
          </div>
        </div>
      </section>

      <section className="wrap catalog-section">
        {unavailable ? (
          <div className="empty-state" role="alert">
            <p className="section-kicker">Registry temporarily unreachable</p>
            <p className="empty">
              The Hub couldn&apos;t load the live catalog right now. You can still pull known agents via the CLI above, or{" "}
              <Link href="/">reload the catalog</Link>.
            </p>
          </div>
        ) : packs.length ? (
          <>
            <div className="catalog-bar">
              <h2 className="sr-only">Published agents</h2>
              <p className="catalog-count">
                <strong>{packs.length}</strong> {packs.length === 1 ? "agent" : "agents"}{filtered ? " matching filters" : " available"}
              </p>
              <p className="muted catalog-hint">
                Built-in <code>mendari/*</code> entries are tagged as starter demos; risk levels are declared by publishers until reviewed.
              </p>
            </div>
            <ul className="results" aria-label="Agents">
              {packs.map((item) => <PackCard key={item.id} item={item} />)}
            </ul>
          </>
        ) : (
          <div className="empty-state">
            <p className="empty">
              {filtered ? (
                <>No agent matches these filters. <Link href="/">Clear filters</Link></>
              ) : (
                "No agents are published yet."
              )}
            </p>
          </div>
        )}
      </section>

      <div className="wrap federated-wrap">
        <section className="federated-banner" aria-labelledby="federated-heading">
          <div className="federated-copy">
            <p className="section-kicker">Zero-PHI multi-site hospital evidence</p>
            <h2 id="federated-heading">Publish local site evals back — without sharing PHI</h2>
            <p className="federated-text">
              Verified health systems shadow-test agents on their own chart-reviewed cases and publish
              cryptographically signed counts (never patient records), so the next hospital starts from
              real-world evidence instead of vendor claims. Open any agent to inspect its forest plot.
            </p>
          </div>
          <div className="federated-cmd">
            <p className="terminal-bar">
              <span>Publish local site eval</span>
              <span>No PHI leaves site</span>
            </p>
            <pre>
              <code>
                <span className="prompt" aria-hidden="true">$ </span>mendari eval &lt;agent-id&gt; --dataset your-cases.jsonl --share
              </code>
            </pre>
          </div>
        </section>
      </div>
    </>
  );
}

function PackCard({ item }: { item: PackSummary }) {
  const starterLabel =
    item.namespace === "mendari"
      ? item.name === "portal-message-agent"
        ? "Demo agent"
        : "Starter template"
      : null;

  const publisherBadgeClass =
    item.checked.publisher_type && PUBLISHER_BADGE.has(item.checked.publisher_type)
      ? item.checked.publisher_type
      : null;

  return (
    <li className="card">
      <div className="card-top">
        <div className="card-heading">
          <h3>
            <Link href={`/packs/${encodeURIComponent(item.namespace)}/${encodeURIComponent(item.name)}`}>
              {item.pack_name}
            </Link>
          </h3>
          <p className="card-id mono muted">{item.id}@{item.version}</p>
        </div>
        <div className="card-badges">
          {starterLabel ? <span className="badge demo-tag">{starterLabel}</span> : null}
          {item.checked.publisher_type ? (
            <span className={publisherBadgeClass ? `badge ${publisherBadgeClass}` : "badge"}>
              {item.checked.publisher_type}
            </span>
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
        <span className="card-cta" aria-hidden="true">Inspect evals →</span>
      </div>
    </li>
  );
}

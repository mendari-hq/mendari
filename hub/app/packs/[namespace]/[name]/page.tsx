import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import { pack, percent, reports, RISK_WORDS, type LeaderboardRow } from "@/lib/registry";

export const dynamic = "force-dynamic";

type Params = { namespace: string; name: string };

// The card's own headings sit under this page's "Decision Card" (h2), so they start at h3.
const CARD_HEADINGS = { h1: "h3", h2: "h4", h3: "h5", h4: "h6" } as const;

export async function generateMetadata({ params }: { params: Promise<Params> }): Promise<Metadata> {
  const { namespace, name } = await params;
  return { title: `${namespace}/${name}` };
}

export default async function PackPage({ params }: { params: Promise<Params> }) {
  const { namespace, name } = await params;
  const [detail, shared] = await Promise.all([pack(namespace, name), reports(namespace, name)]);
  if (!detail) notFound();
  const latest = detail.latest;
  const rows: LeaderboardRow[] = Array.isArray(latest.leaderboard?.rows) ? latest.leaderboard.rows : [];
  return (
    <>
      <p className="muted"><Link href="/">Decision Packs</Link> / <Link href={`/publishers/${namespace}`}>{namespace}</Link></p>
      <h1>{latest.pack_name}</h1>
      <p className="lead">{latest.summary}</p>
      <div className="row">
        {latest.publisher_type ? <span className={`badge ${latest.publisher_type}`}>{latest.publisher_type} publisher</span> : null}
        <span className={`badge risk-${latest.risk.level}`}>Risk {latest.risk.level}: {RISK_WORDS[latest.risk.level]}</span>
        {latest.standards.map((standard) => <span className="chip" key={standard}>{standard}</span>)}
      </div>
      <section className="facts" aria-label="At a glance">
        <Fact label="Latest version" value={latest.version} />
        <Fact label="Benchmark accuracy" value={percent(latest.benchmark?.accuracy)} foot={latest.benchmark ? `${latest.benchmark.n} cases, ${latest.benchmark.dataset}` : undefined} />
        <Fact label="Decides" value={latest.primary} foot={`${latest.questions.length} questions`} />
        <Fact label="License" value={latest.license} />
      </section>
      <h2>Use it</h2>
      <code className="pull">pip install mendari &amp;&amp; mendari pull {detail.id}</code>
      <p className="muted">
        Pulls verify the archive against its signature ({latest.key_id}) and the pack digest{" "}
        <span className="mono">{latest.digest.slice(0, 19)}…</span>, then cache it for{" "}
        <code>mendari eval {detail.id} --dataset your-cases.jsonl</code>.
      </p>
      {rows.length ? <Leaderboard rows={rows} /> : null}
      <h2>Site reports</h2>
      {shared && shared.sites.length ? (
        <div className="table-wrap">
          <table>
            <caption className="muted">How the pack did on sites&apos; own data (metrics only; no cases leave a site).</caption>
            <thead>
              <tr><th>Site</th><th>Version</th><th className="num">Cases</th><th className="num">Accuracy</th><th className="num">Automated</th><th className="num">Calibration (ECE)</th></tr>
            </thead>
            <tbody>
              {shared.sites.map((site) => (
                <tr key={site.id}>
                  <td>{site.site}</td><td className="mono">{site.version}</td><td className="num">{site.n ?? "n/a"}</td>
                  <td className="num">{percent(site.accuracy)}</td><td className="num">{percent(site.automation_rate)}</td>
                  <td className="num">{site.ece == null ? "n/a" : site.ece.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">No site has shared an evaluation yet: <code>mendari eval {detail.id} --dataset … --share &quot;Your site&quot;</code>.</p>
      )}
      <h2>Versions</h2>
      <p>{detail.versions.map((version) => <span key={version} className="chip mono">{version}</span>)}</p>
      {latest.card_markdown ? (
        <>
          <h2>Decision Card</h2>
          <article className="markdown">
            <Markdown components={CARD_HEADINGS}>{latest.card_markdown}</Markdown>
          </article>
        </>
      ) : null}
    </>
  );
}

function Fact({ label, value, foot }: { label: string; value: string; foot?: string }) {
  return (
    <div className="fact">
      <div className="fact-label">{label}</div>
      <div className="fact-value">{value}</div>
      {foot ? <div className="muted">{foot}</div> : null}
    </div>
  );
}

function Leaderboard({ rows }: { rows: LeaderboardRow[] }) {
  const datasets = Array.from(new Set(rows.map((row) => row.dataset)));
  return (
    <>
      <h2>Provider leaderboard</h2>
      <p className="muted">Every configuration on every dataset, ranked by accuracy. Open a dataset to compare.</p>
      {datasets.map((dataset, index) => {
        const ranked = rows.filter((row) => row.dataset === dataset).sort((a, b) => (b.accuracy ?? -1) - (a.accuracy ?? -1));
        return (
          <details key={dataset} open={index === 0}>
            <summary><b>{dataset}</b> <span className="muted">n={ranked[0]?.n}</span></summary>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Configuration</th><th className="num">Accuracy</th><th className="num">Macro-F1</th>
                    <th className="num">ECE</th><th className="num">Automated</th><th className="num">p50 ms</th><th className="num">Cost / decision</th>
                  </tr>
                </thead>
                <tbody>
                  {ranked.map((row) => (
                    <tr key={`${dataset} ${row.config}`}>
                      <td>{row.config}</td>
                      <td className="num">{percent(row.accuracy)}</td>
                      <td className="num">{row.macro_f1 == null ? "n/a" : row.macro_f1.toFixed(3)}</td>
                      <td className="num">{row.ece == null ? "n/a" : row.ece.toFixed(3)}</td>
                      <td className="num">{percent(row.auto_rate)}</td>
                      <td className="num">{row.latency_p50_ms == null ? "n/a" : row.latency_p50_ms.toFixed(1)}</td>
                      <td className="num">{row.cost_per_decision_usd == null ? "n/a" : `$${row.cost_per_decision_usd.toPrecision(2)}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        );
      })}
    </>
  );
}

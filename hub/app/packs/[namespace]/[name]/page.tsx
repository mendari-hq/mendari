// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import { shown } from "@/lib/figures";
import { pack, RISK_WORDS, type BoardFigures } from "@/lib/registry";

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
  const detail = await pack(namespace, name);
  if (!detail) notFound();
  const latest = detail.latest;
  const board = latest.figures?.leaderboard ?? null; // ranked and worked out by the registry
  return (
    <>
      <p className="muted"><Link href="/">Decision Packs</Link> / <Link href={`/publishers/${namespace}`}>{namespace}</Link></p>
      <h1>{latest.pack_name}</h1>
      <p className="lead">{latest.summary}</p>
      <p className="notice"><strong>{latest.status_label ?? "Not clinically reviewed."}</strong></p>
      <div className="row">
        {latest.publisher_type ? <span className={`badge ${latest.publisher_type}`}>{latest.publisher_type} publisher</span> : null}
        <span className={`badge risk-${latest.risk.level}`}>Risk {latest.risk.level}: {RISK_WORDS[latest.risk.level]}</span>
        {latest.standards.map((standard) => <span className="chip" key={standard}>{standard}</span>)}
      </div>
      <section className="facts" aria-label="At a glance">
        <Fact label="Latest version" value={latest.version} />
        <Fact
          label="Benchmark accuracy"
          value={shown(latest.figures?.benchmark)}
          foot={typeof latest.benchmark?.dataset === "string" ? `on ${latest.benchmark.dataset}` : undefined}
        />
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
      {board?.groups.length ? <Leaderboard board={board} /> : null}
      <h2>Site reports</h2>
      <p className="muted">
        A site can share how this pack did on its own data (metrics only; no case leaves the site):{" "}
        <code>mendari eval {detail.id} --dataset … --share &quot;Your site&quot;</code>. Shared reports are not
        shown yet: until sites sign what they share, anyone with a publisher token could submit one under
        any site name.
      </p>
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

function Leaderboard({ board }: { board: BoardFigures }) {
  return (
    <>
      <h2>Provider leaderboard</h2>
      <p className="muted">
        Each set of records is compared on its own, ranked ({board.ranking.method}) by the critical cases caught
        first, never by accuracy alone. Only configurations measured on this version of the pack rank, and only
        where a critical label has at least {board.ranking.min_positives} positives.
      </p>
      {board.groups.map((group, index) => {
        // Each row keyed by its place in the served order (unique, and fixed by the registry).
        const rows = board.rows.map((row, position) => ({ row, position })).filter(({ row }) => row.group === index);
        return (
          <details key={`${group.label} ${index}`} open={index === 0}>
            <summary><b>{group.dataset}</b> <span className="muted">{group.about.join("; ")}</span></summary>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="num">Rank</th><th>Configuration</th><th>Critical detected</th>
                    <th className="num">False automation</th><th className="num">Accuracy</th><th className="num">Automated</th>
                    <th className="num">Macro-F1</th><th className="num">ECE</th><th className="num">p50 ms</th><th className="num">Cost / decision</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ row, position }) => (
                    <tr key={position}>
                      <td className="num">{row.rank ?? <span title={row.standing_text ?? ""}>–</span>}</td>
                      <td>{row.config}{row.standing_text ? <span className="standing">{row.standing_text}</span> : null}</td>
                      <td>
                        {row.figures.critical.length ? row.figures.critical.map((c) => (
                          <div key={`${c.question}=${c.label}`}>
                            {(row.figures.critical_named ?? row.figures.critical.length > 1) ? `${c.question}=${c.label} ` : ""}
                            {shown(c.detected)} ({c.auto_missed} automated)
                            {c.insufficient_evidence ? <span className="evidence-mark">insufficient evidence</span> : null}
                          </div>
                        )) : "n/a"}
                      </td>
                      <td className="num">{shown(row.figures.false_automation)}</td>
                      <td className="num">{shown(row.figures.accuracy)}</td>
                      <td className="num">{shown(row.figures.automation)}</td>
                      <td className="num">{typeof row.macro_f1 === "number" ? row.macro_f1.toFixed(3) : "n/a"}</td>
                      <td className="num">{typeof row.ece === "number" ? row.ece.toFixed(3) : "n/a"}</td>
                      <td className="num">{typeof row.latency_p50_ms === "number" ? row.latency_p50_ms.toFixed(1) : "n/a"}</td>
                      <td className="num">{typeof row.cost_per_decision_usd === "number" ? `$${row.cost_per_decision_usd.toPrecision(2)}` : "unknown"}</td>
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

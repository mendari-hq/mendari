// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ENGINE_WORDS, pack, REVIEW_WORDS, RISK_WORDS } from "@/lib/registry";

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
  const { checked, claimed } = latest;
  const risk = claimed.risk;
  return (
    <>
      <p className="muted"><Link href="/">Decision Packs</Link> / <Link href={`/publishers/${namespace}`}>{namespace}</Link></p>
      <h1>{latest.pack_name}</h1>
      <p className="lead">{latest.summary}</p>
      <p className="notice"><strong>{checked.status_label ?? "Not clinically reviewed."}</strong></p>
      <div className="row">
        {checked.publisher_type ? <span className={`badge ${checked.publisher_type}`}>{checked.publisher_type} publisher</span> : null}
        <span className={`badge risk-${risk.level}`} title="Declared by the publisher">Risk {risk.level} (declared): {RISK_WORDS[risk.level]}</span>
        {latest.standards.map((standard) => <span className="chip" key={standard}>{standard}</span>)}
      </div>
      <section className="facts" aria-label="At a glance">
        <Fact label="Latest version" value={latest.version} />
        <Fact label="Decides" value={latest.primary} foot={`${latest.questions.length} questions`} />
      </section>

      <section className="group checked" aria-labelledby="checked-heading">
        <h2 id="checked-heading">Checked by the registry</h2>
        <dl>
          <Item term="Signature">
            {checked.signature.algorithm}, key <span className="mono">{checked.signature.key_id}</span>,
            {checked.publisher_type ? ` a key of this ${checked.publisher_type} publisher's,` : null} verified at publish
          </Item>
          <Item term="Archive"><span className="mono break">sha256 {checked.archive_sha256}</span></Item>
          <Item term="Pack digest"><span className="mono break">{checked.digest}</span></Item>
          <Item term="Engine">{checked.engine ? ENGINE_WORDS[checked.engine] : "Not recorded (published before the registry recorded it)"}</Item>
          <Item term="Decision Card">
            {checked.card === "rendered"
              ? "Rendered by the registry from the files the digest covers; the uploaded card is not shown"
              : "Not shown: published before the registry rendered cards"}
          </Item>
          <Item term="Review">{REVIEW_WORDS[checked.review.status] ?? checked.review.status}</Item>
          <Item term="Figures">None yet: the registry shows no figure it did not compute itself</Item>
        </dl>
      </section>

      <section className="group claimed" aria-labelledby="claimed-heading">
        <h2 id="claimed-heading">Claimed by the publisher</h2>
        <p className="muted">What the pack says about itself. The registry has not checked it.</p>
        <dl>
          {claimed.publisher ? (
            <Item term="Publisher">{claimed.publisher.name} ({claimed.publisher.type}, self-declared)</Item>
          ) : null}
          <Item term="Risk level">{risk.level}, {risk.category}: declared by the publisher, not confirmed by a review</Item>
          <Item term="Licence">{claimed.license}</Item>
          <Item term="Summary">{claimed.summary}</Item>
        </dl>
      </section>

      <h2>Use it</h2>
      <code className="pull">pip install mendari &amp;&amp; mendari pull {detail.id}</code>
      <p className="muted">
        Pulls verify the archive against its signature ({checked.signature.key_id}) and the pack digest{" "}
        <span className="mono">{checked.digest.slice(0, 19)}…</span>, then cache it for{" "}
        <code>mendari eval {detail.id} --dataset your-cases.jsonl</code>.
      </p>
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
          <p className="muted">Rendered by the registry from the pack&apos;s own files. What it says is the publisher&apos;s.</p>
          <article className="markdown">
            {/* Publisher text: no raw HTML, and only safe link schemes (react-markdown's default). */}
            <Markdown components={CARD_HEADINGS} remarkPlugins={[remarkGfm]} skipHtml>
              {latest.card_markdown}
            </Markdown>
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

function Item({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div className="item">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

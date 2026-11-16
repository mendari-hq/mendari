// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { shown } from "@/lib/figures";
import {
  acceptanceWords,
  ENGINE_WORDS,
  pack,
  type Recalled,
  regressionWords,
  reviewWords,
  RISK_WORDS,
  riskWords,
  siteEvidence,
  type SiteEvidence,
} from "@/lib/registry";

import { EvidenceGroupView } from "./forest";

export const dynamic = "force-dynamic";

type Params = { namespace: string; name: string };

// The card's own headings sit under this page's "Decision Card" (h2), so they start at h3.
// No image is shown: a publisher's picture could pass for a badge, and would tell its server
// who reads the page.
const CARD_ELEMENTS = { h1: "h3", h2: "h4", h3: "h5", h4: "h6", img: () => null } as const;

export async function generateMetadata({ params }: { params: Promise<Params> }): Promise<Metadata> {
  const { namespace, name } = await params;
  return { title: `${namespace}/${name}` };
}

export default async function PackPage({ params }: { params: Promise<Params> }) {
  const { namespace, name } = await params;
  const detail = await pack(namespace, name);
  if (!detail) notFound();
  const recalled = detail.recalled ?? [];
  const latest = detail.latest;
  if (!latest) {
    return (
      <>
        <p className="muted"><Link href="/">Decision Packs</Link> / <Link href={`/publishers/${namespace}`}>{namespace}</Link></p>
        <h1>{detail.id}</h1>
        <p className="notice"><strong>Every version of this pack is recalled. Do not use it.</strong></p>
        <Recalls recalled={recalled} />
      </>
    );
  }
  const evidence = await siteEvidence(namespace, name);
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
            {checked.signature.algorithm}, key <span className="mono">{checked.signature.key_id}</span> of this
            namespace, verified at publish
          </Item>
          <Item term="Publisher type">
            {checked.publisher_type ?? "Not set"}: set by the registry&apos;s reviewers for this namespace (as it stands now)
          </Item>
          <Item term="Archive"><span className="mono break">sha256 {checked.archive_sha256}</span></Item>
          <Item term="Pack digest"><span className="mono break">{checked.digest}</span></Item>
          <Item term="Engine">
            {checked.engine ? ENGINE_WORDS[checked.engine] : "Not recorded (published before the registry recorded it)"}
            {checked.engine_release ? ` (registry release ${checked.engine_release})` : null}
          </Item>
          <Item term="Decision Card">
            {checked.card === "rendered"
              ? "Rendered by the registry from the files the digest covers; the uploaded card is not shown"
              : "Not shown: published before the registry rendered cards"}
          </Item>
          <Item term="Review">{reviewWords(checked.review, risk.level)}</Item>
          <Item term="Regression check">
            <span className="regression-label">{checked.regression?.label ?? "publisher's own test set: regression check, not a ranking"}</span>
            {" "}{regressionWords(checked.regression, shown)}
          </Item>
          <Item term="Counter-signature">
            {acceptanceWords(checked.accepted)}
            {checked.accepted ? <>, key <span className="mono">{checked.accepted.key_id}</span>, {checked.accepted.at}</> : null}
          </Item>
        </dl>
      </section>

      <section className="group claimed" aria-labelledby="claimed-heading">
        <h2 id="claimed-heading">Claimed by the publisher</h2>
        <p className="muted">What the pack says about itself. The registry has not checked it.</p>
        <dl>
          {claimed.publisher ? (
            <Item term="Publisher">{claimed.publisher.name} ({claimed.publisher.type}, self-declared)</Item>
          ) : null}
          <Item term="Risk level">{risk.level}, {risk.category}: {riskWords(risk, checked.review)}</Item>
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
      <SiteEvidenceSection evidence={evidence} version={latest.version} packId={detail.id} />
      <h2>Versions</h2>
      <p>{detail.versions.map((version) => <span key={version} className="chip mono">{version}</span>)}</p>
      <Recalls recalled={recalled} />
      {latest.card_markdown ? (
        <>
          <h2>Decision Card</h2>
          <p className="muted">Rendered by the registry from the pack&apos;s own files. What it says is the publisher&apos;s.</p>
          <article className="markdown">
            {/* Publisher text: no raw HTML, and only safe link schemes (react-markdown's default). */}
            <Markdown components={CARD_ELEMENTS} remarkPlugins={[remarkGfm]} skipHtml>
              {latest.card_markdown}
            </Markdown>
          </article>
        </>
      ) : null}
    </>
  );
}

// Site evidence (LP-22h): sites' own data, shared as signed counts, for the version shown. Its
// own section, apart from what the registry checked and from any rank (ADR-0017).
function SiteEvidenceSection({ evidence, version, packId }: { evidence: SiteEvidence | null; version: string; packId: string }) {
  const groups = (evidence?.groups ?? []).filter((group) => group.version === version);
  const sites = groups.reduce((total, group) => total + group.sites, 0);
  const cases = groups.reduce((total, group) => total + group.cases, 0);
  return (
    <section className="group evidence" aria-labelledby="evidence-heading">
      <h2 id="evidence-heading">
        Site evidence{groups.length ? `: ${sites} ${sites === 1 ? "site" : "sites"}, ${cases} cases` : ""}
      </h2>
      <p className="muted">
        These are sites&apos; own data, shared as signed counts. They are not part of any hidden-set result or
        rank. A ticket binds a dataset before its run, not how its cases were chosen.
      </p>
      {groups.length ? (
        groups.map((group) => <EvidenceGroupView key={`${group.digest}:${JSON.stringify(group.configuration)}:${group.remote_consent}`} group={group} />)
      ) : (
        <p>
          No site has shared signed counts for version {version} yet. A verified site shares how this pack did on its
          own data with <code>mendari eval {packId} --dataset … --share</code>; no case leaves the site.
        </p>
      )}
    </section>
  );
}

// Recalled versions, each with the record's reason (publisher text, shown as text) and who
// signed it. Still pullable by exact version; never the latest, never in search.
function Recalls({ recalled }: { recalled: Recalled[] }) {
  if (!recalled.length) return null;
  return (
    <section className="group recalled" aria-labelledby="recalled-heading">
      <h2 id="recalled-heading">Recalled versions</h2>
      <p className="muted">A recalled version is not the latest and is not searched. Do not automate with it.</p>
      <ul>
        {recalled.map(({ version, yank, deleted }) => (
          <li key={version}>
            <span className="mono">{version}</span>
            {yank ? (
              <>
                {" "}
                <span className={`badge severity-${yank.severity}`}>{yank.severity}</span> recalled by{" "}
                {yank.signer === "publisher" ? "its publisher" : "the registry"} on {yank.at.slice(0, 10)}, signed
                with <span className="mono">{yank.key_id}</span>: {yank.reason}
              </>
            ) : null}
            {deleted ? <> Deleted on {deleted.at.slice(0, 10)}: {deleted.reason}</> : null}
          </li>
        ))}
      </ul>
    </section>
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

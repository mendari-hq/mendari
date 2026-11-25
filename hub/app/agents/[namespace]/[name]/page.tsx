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
  publisherEvaluations,
  type PublisherEvaluations,
  type Recalled,
  RegistryUnavailable,
  regressionWords,
  reviewWords,
  RISK_WORDS,
  riskWords,
  siteEvidence,
  type SiteEvidence,
} from "@/lib/registry";

import { RegistryUnavailablePage } from "@/app/registry-unavailable";

import { CopyCommand } from "./copy-command";
import { PublishedEvaluationsSection } from "./evaluations";
import { EvidenceGroupView } from "./forest";

export const dynamic = "force-dynamic";

type Params = { namespace: string; name: string };

// The card's own headings sit under this page's "Decision Card" (h2), so they start at h3.
// No image is shown: a publisher's picture could pass for a badge, and would tell its server
// who reads the page.
const CARD_ELEMENTS = { h1: "h3", h2: "h4", h3: "h5", h4: "h6", img: () => null } as const;
const PUBLISHER_BADGE = new Set(["verified", "institutional", "community"]);

export async function generateMetadata({ params }: { params: Promise<Params> }): Promise<Metadata> {
  const { namespace, name } = await params;
  return { title: `${namespace}/${name}` };
}

export default async function PackPage({ params }: { params: Promise<Params> }) {
  const { namespace, name } = await params;
  const [packResult, evidenceResult] = await Promise.allSettled([
    pack(namespace, name),
    siteEvidence(namespace, name),
  ]);
  if (packResult.status === "rejected") {
    if (packResult.reason instanceof RegistryUnavailable) {
      return <RegistryUnavailablePage retry={`/agents/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`} />;
    }
    throw packResult.reason;
  }
  const detail = packResult.value;
  if (!detail) notFound();
  const recalled = detail.recalled ?? [];
  const latest = detail.latest;
  if (!latest) {
    return (
      <div className="wrap page-body">
        <nav className="breadcrumb muted" aria-label="Breadcrumb">
          <Link href="/">Agent Hub</Link>
          <span className="breadcrumb-sep" aria-hidden="true">/</span>
          <Link href={`/publishers/${encodeURIComponent(namespace)}`}>{namespace}</Link>
        </nav>
        <section className="pack-hero pack-hero-danger">
          <h1>{detail.id}</h1>
          <p className="notice notice-danger"><strong>Every version of this agent is recalled. Do not use it.</strong></p>
        </section>
        <Recalls recalled={recalled} />
      </div>
    );
  }
  // One read, for the version shown.
  let evaluations: PublisherEvaluations | null = null;
  let evaluationsUnavailable = false;
  try {
    evaluations = await publisherEvaluations(namespace, name, latest.version);
  } catch {
    evaluationsUnavailable = true;
  }
  const evidence = evidenceResult.status === "fulfilled" ? evidenceResult.value : null;
  const evidenceError = evidenceResult.status === "rejected";
  const versionGroups = (evidence?.groups ?? []).filter((group) => group.version === latest.version);
  const totalSites = versionGroups.reduce((total, group) => total + group.sites, 0);
  const totalSiteCases = versionGroups.reduce((total, group) => total + group.cases, 0);
  const anySiteDiff = versionGroups.some((group) =>
    group.figures.some((fig) => fig.homogeneity?.differ === true),
  );
  const siteSummary = totalSites
    ? `${totalSites} ${totalSites === 1 ? "site" : "sites"} · ${totalSiteCases.toLocaleString()} cases`
    : "None yet";
  const siteFoot = totalSites
    ? anySiteDiff
      ? "Site differences noted — inspect below"
      : "Signed zero-PHI hospital counts"
    : "Publish with mendari eval --share";
  const starterLabel =
    namespace === "mendari"
      ? name === "portal-message-agent"
        ? "Demo agent"
        : "Starter template"
      : null;
  const { checked, claimed } = latest;
  const risk = claimed.risk;
  const publisherBadgeClass =
    checked.publisher_type && PUBLISHER_BADGE.has(checked.publisher_type)
      ? checked.publisher_type
      : null;
  const reviewPill =
    checked.review.status === "reviewed"
      ? {
          tone: checked.review.matches_declared === false ? "warn" : "ok",
          text:
            checked.review.matches_declared === false
              ? `Reviewed · Risk ${checked.review.risk_level} found`
              : `Reviewed · Risk ${checked.review.risk_level}`,
        }
      : { tone: "neutral", text: "Unreviewed" };
  const regressionPill =
    checked.regression?.status === "measured"
      ? checked.regression.regressions?.length
        ? { tone: "warn", text: `${checked.regression.regressions.length} regression(s) flagged` }
        : { tone: "ok", text: "Regression check passed" }
      : { tone: "neutral", text: "Regression not measured" };
  return (
    <div className="wrap page-body">
      <nav className="breadcrumb muted" aria-label="Breadcrumb">
        <Link href="/">Agent Hub</Link>
        <span className="breadcrumb-sep" aria-hidden="true">/</span>
        <Link href={`/publishers/${encodeURIComponent(namespace)}`}>{namespace}</Link>
      </nav>

      <header className="pack-hero">
        <div className="pack-hero-meta row">
          <span className="mono pack-id-pill">{detail.id}@{latest.version}</span>
          {starterLabel ? <span className="badge demo-tag">{starterLabel}</span> : null}
          {checked.publisher_type ? (
            <span className={publisherBadgeClass ? `badge ${publisherBadgeClass}` : "badge"}>
              {checked.publisher_type} publisher
            </span>
          ) : null}
          <span className={`badge risk-${risk.level}`} title="Declared by the publisher">
            Risk {risk.level} (declared): {RISK_WORDS[risk.level]}
          </span>
        </div>
        <h1>{latest.pack_name}</h1>
        <p className="lead">{latest.summary}</p>
        <p className="notice"><strong>{checked.status_label ?? "Not clinically reviewed."}</strong></p>
        {latest.standards.length ? (
          <div className="pack-hero-standards row">
            {latest.standards.map((standard) => <span className="chip" key={standard}>{standard}</span>)}
          </div>
        ) : null}
      </header>

      <ul className="trust-strip" aria-label="Verification summary">
        <li className="trust-pill trust-pill-ok">
          <span className="trust-k">Signature</span>
          <span className="trust-v">{checked.signature.algorithm} verified</span>
        </li>
        <li className={`trust-pill trust-pill-${reviewPill.tone}`}>
          <span className="trust-k">Registry review</span>
          <span className="trust-v">{reviewPill.text}</span>
        </li>
        <li className={`trust-pill trust-pill-${regressionPill.tone}`}>
          <span className="trust-k">Test set</span>
          <span className="trust-v">{regressionPill.text}</span>
        </li>
        <li className={`trust-pill trust-pill-${checked.accepted ? "ok" : "neutral"}`}>
          <span className="trust-k">Counter-signature</span>
          <span className="trust-v">{checked.accepted ? "Registry signed" : "None recorded"}</span>
        </li>
      </ul>

      <section className="facts" aria-label="At a glance">
        <Fact
          label="Latest version"
          value={latest.version}
          foot={`${detail.versions.length} ${detail.versions.length === 1 ? "version" : "versions"} published`}
        />
        <Fact label="Agent output" value={latest.primary} foot={`${latest.questions.length} governed questions`} />
        <Fact label="Hospital site evals" value={siteSummary} foot={siteFoot} />
      </section>

      <section className="install-card" aria-labelledby="use-heading">
        <div className="install-head">
          <h2 id="use-heading">Try &amp; shadow-test this agent</h2>
          <span className="install-tag mono">Signature &amp; digest verified on pull</span>
        </div>
        <CopyCommand command={`pip install mendari && mendari pull ${detail.id}`} />
        <p className="install-note">
          Pulls verify the signed archive (<span className="mono">{checked.signature.key_id}</span>) and digest{" "}
          <span className="mono">{checked.digest.slice(0, 19)}…</span>, then cache it for local shadow evaluation with{" "}
          <code>mendari eval {detail.id} --dataset your-cases.jsonl</code> — add <code>--share</code> to publish zero-PHI site counts back to the Hub.
        </p>
      </section>

      <div className="trust-grid">
        <section className="group checked" aria-labelledby="checked-heading">
          <div className="group-head">
            <h2 id="checked-heading">Checked by the registry</h2>
            <span className="group-tag group-tag-ok">Verified at publish</span>
          </div>
          <dl>
            <Item term="Signature">
              {checked.signature.algorithm}, key <span className="mono">{checked.signature.key_id}</span> of this
              namespace, verified at publish
            </Item>
            <Item term="Publisher type">
              {checked.publisher_type ?? "Not set"}: set by the registry&apos;s reviewers for this namespace (as it stands now)
            </Item>
            <Item term="Archive"><span className="mono break">sha256 {checked.archive_sha256}</span></Item>
            <Item term="Agent digest"><span className="mono break">{checked.digest}</span></Item>
            <Item term="Engine">
              {ENGINE_WORDS[checked.engine]}
              {checked.engine_release ? ` (registry release ${checked.engine_release})` : null}
            </Item>
            <Item term="Decision Card">
              Rendered by the registry from the files the digest covers; the uploaded card is not shown
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
          <div className="group-head">
            <h2 id="claimed-heading">Claimed by the publisher</h2>
            <span className="group-tag group-tag-warn">Publisher declared</span>
          </div>
          <p className="muted group-sub">What the publisher says about the agent. The registry has not checked it.</p>
          <dl>
            {claimed.publisher ? (
              <Item term="Publisher">{claimed.publisher.name} ({claimed.publisher.type}, self-declared)</Item>
            ) : null}
            <Item term="Risk level">{risk.level}, {risk.category}: {riskWords(risk, checked.review)}</Item>
            <Item term="Licence">{claimed.license}</Item>
            <Item term="Summary">{claimed.summary}</Item>
          </dl>
        </section>
      </div>

      <PublishedEvaluationsSection
        evaluations={evaluations}
        unavailable={evaluationsUnavailable}
        version={latest.version}
        packId={detail.id}
      />

      <SiteEvidenceSection evidence={evidence} evidenceError={evidenceError} version={latest.version} packId={detail.id} />

      <section className="panel versions-panel" aria-labelledby="versions-heading">
        <div className="group-head">
          <h2 id="versions-heading">Versions</h2>
          <span className="muted small-meta">{detail.versions.length} available</span>
        </div>
        <div className="row">{detail.versions.map((version) => <span key={version} className="chip mono">{version}</span>)}</div>
      </section>

      <Recalls recalled={recalled} />

      <section className="card-doc" aria-labelledby="decision-card-heading">
        <div className="card-doc-head">
          <h2 id="decision-card-heading">Decision Card</h2>
          <p className="muted">Rendered by the registry from the agent&apos;s own files. What it says is the publisher&apos;s.</p>
        </div>
        <article className="markdown">
          {/* Publisher text: no raw HTML, and only safe link schemes (react-markdown's default). */}
          <Markdown components={CARD_ELEMENTS} remarkPlugins={[remarkGfm]} skipHtml>
            {latest.card_markdown}
          </Markdown>
        </article>
      </section>
    </div>
  );
}

// Site evidence (LP-22h): sites' own data, shared as signed counts, for the version shown. Its
// own section, apart from what the registry checked and from any rank (ADR-0017).
function SiteEvidenceSection({
  evidence,
  evidenceError,
  version,
  packId,
}: {
  evidence: SiteEvidence | null;
  evidenceError: boolean;
  version: string;
  packId: string;
}) {
  const groups = (evidence?.groups ?? []).filter((group) => group.version === version);
  const sites = groups.reduce((total, group) => total + group.sites, 0);
  const cases = groups.reduce((total, group) => total + group.cases, 0);
  return (
    <section className="group evidence" aria-labelledby="evidence-heading">
      <div className="group-head">
        <h2 id="evidence-heading">
          Site evidence{groups.length ? `: ${sites} ${sites === 1 ? "site" : "sites"}, ${cases} cases` : ""}
        </h2>
        <span className="group-tag group-tag-accent">Zero-PHI counts</span>
      </div>
      <p className="muted group-sub">
        These are sites&apos; own data, shared as signed counts. They are not part of any hidden-set result or
        rank. A ticket binds a dataset before its run, not how its cases were chosen.
      </p>
      {evidenceError ? (
        <p className="muted">Site evidence is temporarily unavailable. Reload in a moment to check again.</p>
      ) : groups.length ? (
        groups.map((group) => <EvidenceGroupView key={`${group.digest}:${JSON.stringify(group.configuration)}:${group.remote_consent}`} group={group} />)
      ) : (
        <p className="evidence-empty">
          No site has shared signed counts for version {version} yet. A verified site shares how this agent did on its
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
      <div className="group-head">
        <h2 id="recalled-heading">Recalled versions</h2>
        <span className="group-tag group-tag-danger">Do not automate</span>
      </div>
      <p className="muted group-sub">A recalled version is not the latest and is not searched. Do not automate with it.</p>
      <ul className="recall-list">
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
      {foot ? <div className="fact-foot muted">{foot}</div> : null}
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

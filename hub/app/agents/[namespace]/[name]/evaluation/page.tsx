// SPDX-License-Identifier: Apache-2.0
// An agent's evaluation page: what its publisher's signed evaluations say, for someone deciding
// whether to try the agent (a hospital's IT lead, a clinician), not for a statistician. A stable
// address a publisher links from a README, beside the badge the registry serves.
//
// Everything shown is the registry's answer (mendari.agent-evaluation/1): it checks each signed
// record again on every read and works out every figure from the counts. This page words it
// (lib/evaluation.ts) and prints the figures (lib/figures.ts). A record that does not verify is
// listed as such, with none of its numbers. Publisher text (who labelled the cases, an agent's
// name) is rendered as text.
import type { Metadata } from "next";
import { headers } from "next/headers";
import Link from "next/link";
import { notFound } from "next/navigation";

import { RegistryUnavailablePage } from "@/app/registry-unavailable";
import {
  badgeMarkdown,
  casesWords,
  claimedWords,
  comparisonWords,
  counted,
  criticalWords,
  failedWords,
  MEASURE_WORDS,
  modelWords,
  NOT_SHOWN_WORDS,
  provenanceWords,
  RERUN_WORDS,
  rerunSummary,
  shownConfigurations,
  summaryWords,
} from "@/lib/evaluation";
import { type Figure, shown } from "@/lib/figures";
import {
  type AgentEvaluation,
  agentBadge,
  agentEvaluation,
  badgePath,
  type EvaluatedConfiguration,
  type EvaluatedDataset,
  publicRegistry,
  RegistryUnavailable,
} from "@/lib/registry";

import { CopyCommand } from "../copy-command";

export const dynamic = "force-dynamic";

type Params = { namespace: string; name: string };
type Search = { version?: string | string[] };

const VERSION = /^(?=.{1,64}$)\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;
const HOST = /^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::\d{1,5})?$/;
const PUBLISHER_BADGE = new Set(["verified", "institutional", "community"]);
// The page's header, as the agent's page styles its own (globals.css), and when recalled.
const HERO = "agent-hero";
const HERO_DANGER = "agent-hero-danger";
// What the engine did with a case, as someone outside the project says it.
const OUTCOMES: [string, string, string][] = [
  ["AUTO", "Decided automatically", "the agent's answer was acted on with no person reading the case"],
  ["HUMAN_REVIEW", "Sent to a person", "staff decide, with the agent's suggestion in front of them"],
  ["ABSTAIN", "No suggestion made", "the agent was not sure enough to suggest anything"],
  ["BLOCK", "Blocked", "a rule stopped the case"],
];

export async function generateMetadata({ params }: { params: Promise<Params> }): Promise<Metadata> {
  const { namespace, name } = await params;
  return { title: `Evaluation of ${namespace}/${name}` };
}

function local(host: string): boolean {
  const name = host.replace(/:\d+$/, "").replace(/^\[|\]$/g, "").toLowerCase();
  return name === "localhost" || name.endsWith(".localhost") || name === "127.0.0.1" || name === "::1";
}

export default async function EvaluationPage({
  params,
  searchParams,
}: {
  params: Promise<Params>;
  searchParams: Promise<Search>;
}) {
  const { namespace, name } = await params;
  const asked = (await searchParams).version;
  const version = typeof asked === "string" ? asked : undefined;
  if (asked !== undefined && (version === undefined || !VERSION.test(version))) notFound();
  const agentHref = `/agents/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`;
  const here = `${agentHref}/evaluation`;
  let evaluation: AgentEvaluation | null;
  try {
    evaluation = await agentEvaluation(namespace, name, version);
  } catch (error) {
    if (error instanceof RegistryUnavailable) return <RegistryUnavailablePage retry={here} />;
    throw error;
  }
  if (!evaluation) notFound();
  const agent = evaluation.agent;
  const latest = evaluation.versions.latest;
  const older = Boolean(agent && latest && agent.version !== latest);
  // The badge is the newest version's: shown here when this page is of that version.
  const image = older ? null : await agentBadge(namespace, name);
  const host = (await headers()).get("host") ?? "";
  const site = HOST.test(host) ? `${local(host) ? "http" : "https"}://${host}` : null;
  const registry = site ? publicRegistry(local(host)) : null;
  const markdown = site && registry ? badgeMarkdown(`${registry}${badgePath(namespace, name)}`, `${site}${here}`) : null;
  const publisherType = evaluation.publisher.publisher_type;
  const title = agent?.name || evaluation.id;
  return (
    <div className="wrap page-body eval-page">
      <nav className="breadcrumb muted" aria-label="Breadcrumb">
        <Link href="/">Agent Hub</Link>
        <span className="breadcrumb-sep" aria-hidden="true">/</span>
        <Link href={`/publishers/${encodeURIComponent(namespace)}`}>{namespace}</Link>
        <span className="breadcrumb-sep" aria-hidden="true">/</span>
        <Link href={agentHref}>{name}</Link>
      </nav>

      <header className={[HERO, evaluation.state === "recalled" ? HERO_DANGER : ""].join(" ").trim()}>
        <div className="agent-hero-meta row">
          <span className="mono agent-id-pill">
            {evaluation.id}
            {agent ? `@${agent.version}` : ""}
          </span>
          <span className={PUBLISHER_BADGE.has(publisherType) ? `badge ${publisherType}` : "badge"}>
            {publisherType} publisher
          </span>
          <span className="badge">Publisher&apos;s own evaluation</span>
        </div>
        <h1>Evaluation of {title}</h1>
        <p className="lead">{summaryWords(evaluation)}</p>
        {agent?.status_label ? (
          <p className="notice">
            <strong>{agent.status_label}</strong>
          </p>
        ) : null}
        {older && agent && latest ? (
          <p className="eval-older">
            You are reading version {agent.version}. The newest version is {latest}:{" "}
            <Link href={here}>read its evaluation</Link>.
          </p>
        ) : null}
      </header>

      {evaluation.state === "evaluated" && agent ? (
        <>
          <AtAGlance evaluation={evaluation} />
          <WhatWasEvaluated evaluation={evaluation} namespace={namespace} />
          {evaluation.datasets.map((dataset) => (
            <Compared
              key={`${dataset.name}:${dataset.sha256}`}
              dataset={dataset}
              synthetic={agent.synthetic}
              several={evaluation.datasets.length > 1}
            />
          ))}
        </>
      ) : (
        <Nothing evaluation={evaluation} here={here} agentHref={agentHref} />
      )}

      {evaluation.state !== "recalled" ? <Signatures evaluation={evaluation} namespace={namespace} /> : null}

      {evaluation.state === "evaluated" && agent ? (
        <Limits evaluation={evaluation} agentHref={agentHref} />
      ) : null}

      <section className="panel eval-next" aria-labelledby="next-heading">
        <h2 id="next-heading">Read next</h2>
        <ul className="eval-links">
          <li>
            <Link href={`${agentHref}#decision-card-heading`}>The agent&apos;s Decision Card</Link>: what it is for, who
            it is for, its risk level, its policy and its known failure modes, rendered by the registry from the
            agent&apos;s own files.
          </li>
          <li>
            <Link href={agentHref}>The agent&apos;s page</Link>: what the registry checked itself, what other sites
            measured on their own cases, and how to pull the agent.
          </li>
          <li>
            <Link href={`/publishers/${encodeURIComponent(namespace)}`}>The publisher&apos;s page</Link>: its signing
            keys, and whether the registry has checked who it is.
          </li>
        </ul>
      </section>

      {image ? (
        <section className="panel eval-badge" aria-labelledby="badge-heading">
          <h2 id="badge-heading">The badge</h2>
          <p className="muted group-sub">
            A publisher can put this in a README. It says the same as this page, in one line, and the registry
            draws it from the same signed records each time it is shown: words and whole numbers only.
          </p>
          {/* The registry's own image, as data: nothing in it runs, and it loads nothing. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            className="eval-badge-image"
            src={`data:image/svg+xml;base64,${Buffer.from(image, "utf8").toString("base64")}`}
            alt={`${evaluation.badge.label}: ${evaluation.badge.message}`}
            height={20}
          />
          {markdown ? (
            <>
              <p className="muted eval-badge-how">Markdown for a README, linked to this page:</p>
              <CopyCommand command={markdown} prompt={false} />
            </>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

// "549 of 600 [89.0-93.6%]", the interval kept on one line when the cell wraps.
function Shown({ figure }: { figure: Figure | null | undefined }) {
  const text = shown(figure);
  const at = text.indexOf(" [");
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)} <span className="eval-interval">{text.slice(at + 1)}</span>
    </>
  );
}

// A sentence that holds figures: each interval ("[0.7-2.8%]") stays on one line.
function Sentence({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\[[^[\]]*\])/).map((part, index) =>
        index % 2 ? (
          <span className="eval-nowrap" key={index}>
            {part}
          </span>
        ) : (
          part
        ),
      )}
    </>
  );
}

function Pill({ tone, term, children }: { tone: "ok" | "warn" | "neutral"; term: string; children: React.ReactNode }) {
  return (
    <li className={`trust-pill trust-pill-${tone}`}>
      <span className="trust-k">{term}</span>
      <span className="trust-v">{children}</span>
    </li>
  );
}

function AtAGlance({ evaluation }: { evaluation: AgentEvaluation }) {
  const configurations = shownConfigurations(evaluation);
  const total = configurations.length + evaluation.not_shown.length;
  const [only] = evaluation.datasets;
  const synthetic = evaluation.agent?.synthetic ?? false;
  const data =
    evaluation.datasets.length === 1 && only ? casesWords(only, synthetic) : counted(evaluation.datasets.length, "dataset");
  const dataIsSynthetic = evaluation.datasets.every(
    (dataset) => dataset.provenance === "synthetic" || (dataset.provenance === null && synthetic),
  );
  const critical = configurations.filter((configuration) => configuration.critical_cases > 0);
  const worst = critical.reduce<EvaluatedConfiguration | null>(
    (most, configuration) => (most && most.critical_automated >= configuration.critical_automated ? most : configuration),
    null,
  );
  const least = Math.min(...critical.map((configuration) => configuration.critical_automated));
  const rerun = configurations.filter((configuration) => configuration.rerun === "matched").length;
  return (
    <ul className="trust-strip" aria-label="Evaluation at a glance">
      <Pill tone={evaluation.not_shown.length ? "warn" : "ok"} term="Signatures">
        {configurations.length} of {counted(total, "record")} verified
      </Pill>
      <Pill tone={dataIsSynthetic ? "warn" : "neutral"} term="Data">
        {data}
      </Pill>
      <Pill tone={worst && worst.critical_automated > 0 ? "warn" : worst ? "ok" : "neutral"} term="Critical cases automated">
        {worst
          ? `${least < worst.critical_automated ? `${least} to ` : ""}${worst.critical_automated} of ${worst.critical_cases}`
          : "No critical label named"}
      </Pill>
      <Pill tone={rerun === configurations.length ? "ok" : "neutral"} term="Re-run by the registry">
        {rerun} of {counted(configurations.length, "configuration")}
      </Pill>
    </ul>
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

function WhatWasEvaluated({ evaluation, namespace }: { evaluation: AgentEvaluation; namespace: string }) {
  const agent = evaluation.agent;
  if (!agent) return null;
  const engines = [...new Set(shownConfigurations(evaluation).map((configuration) => configuration.engine))];
  return (
    <section className="group eval-what" aria-labelledby="what-heading">
      <div className="group-head">
        <h2 id="what-heading">What was evaluated, and on what data</h2>
        <span className="group-tag group-tag-accent">From the signed records</span>
      </div>
      <dl>
        <Item term="Agent">
          {agent.name ?? evaluation.id}, version {agent.version}
          {agent.published_at ? `, published ${agent.published_at.slice(0, 10)}` : ""}.{" "}
          {agent.digest ? (
            <>
              Exactly this build: <span className="mono break">{agent.digest}</span>
            </>
          ) : null}
        </Item>
        <Item term="Who ran it">
          Its publisher, <Link href={`/publishers/${encodeURIComponent(namespace)}`}>{evaluation.publisher.display_name || namespace}</Link>
          , on its own machines. Nobody else chose the cases.
        </Item>
        {evaluation.datasets.map((dataset) => (
          <Item key={`${dataset.name}:${dataset.sha256}`} term={evaluation.datasets.length > 1 ? `Dataset ${dataset.name}` : "Dataset"}>
            <span className="mono">{dataset.name}</span>: {counted(dataset.n, "case")}, a file inside the agent&apos;s signed
            archive (<span className="mono break">sha256 {dataset.sha256.slice(0, 16)}…</span>), so anyone who pulls the
            agent has the same cases.
            <span className="eval-line">
              <strong>Where the cases come from.</strong> {provenanceWords(dataset, agent.synthetic)}
            </span>
            <span className="eval-line">
              <strong>Who labelled them.</strong>{" "}
              {dataset.labelled_by.length
                ? `${dataset.labelled_by.join("; ")} (as stated by the publisher)`
                : "Not stated by the publisher."}
            </span>
            <span className="eval-line">
              <strong>What counts as critical.</strong>{" "}
              {dataset.critical.length
                ? dataset.critical
                    .map((label) => `${label.question}=${label.label} (${label.n} of the ${dataset.n} cases)`)
                    .join("; ")
                : "The agent names no critical label."}
            </span>
          </Item>
        ))}
        <Item term="Engine">{`Mendari ${engines.join(", ")}, as stated by the publisher.`}</Item>
      </dl>
    </section>
  );
}

function Compared({ dataset, synthetic, several }: { dataset: EvaluatedDataset; synthetic: boolean; several: boolean }) {
  const words = comparisonWords(dataset);
  const comparison = dataset.comparison;
  const heading = `compare-${dataset.name}`;
  const named = dataset.critical.length !== 1;
  const only = dataset.critical.length === 1 ? dataset.critical[0] : undefined;
  const criticalHead = only ? ` (${only.question}=${only.label})` : "";
  const flagged = dataset.configurations.map(criticalWords);
  const primary = dataset.configurations[0]?.figures.primary;
  // The comparison's four measures, as the columns name them (and, on a phone, each figure).
  const measures = [
    { ...MEASURE_WORDS.critical_detection, name: `${MEASURE_WORDS.critical_detection.name}${criticalHead}` },
    { ...MEASURE_WORDS.critical_automated, name: `${MEASURE_WORDS.critical_automated.name}${criticalHead}` },
    MEASURE_WORDS.false_automation,
    { ...MEASURE_WORDS.accuracy, name: `${MEASURE_WORDS.accuracy.name}${primary ? ` (${primary})` : ""}` },
  ];
  return (
    <section className="group eval-compare" aria-labelledby={heading}>
      <div className="group-head">
        <h2 id={heading}>How the configurations compare{several ? ` on ${dataset.name}` : ""}</h2>
        <span className="group-tag group-tag-warn">Publisher signed</span>
      </div>
      <p className="muted group-sub">
        A configuration is one way of running the agent: which model or which rules answer its questions. Every row
        below is the same {casesWords(dataset, synthetic)}, so the rows can be compared. Each figure is a count of
        cases; the range in brackets is where the true rate plausibly lies (a 95% interval), and it is wide when the
        cases are few.
      </p>

      <div className={`eval-verdict eval-verdict-${words.tone}`} role="note" aria-label="What the comparison shows">
        <p className="eval-verdict-head">{words.headline}</p>
        {words.detail.map((sentence) => (
          <p key={sentence}>
            <Sentence text={sentence} />
          </p>
        ))}
      </div>

      {flagged.some(Boolean) ? (
        <ul className="eval-critical" aria-label="Critical cases, by configuration">
          {dataset.configurations.map((configuration, index) =>
            flagged[index] ? (
              <li key={configuration.id} className={configuration.critical_automated ? "eval-flag" : undefined}>
                {flagged[index]}
              </li>
            ) : null,
          )}
        </ul>
      ) : null}

      {/* On a phone each row stacks, each figure under its column's name (globals.css
          .eval-stack); the roles keep it a table to a screen reader either way. */}
      <div className="table-wrap">
        <table className="eval-stack" role="table">
          <caption className="sr-only">
            Each configuration on the {dataset.n} cases of {dataset.name}, in the comparison&apos;s order
          </caption>
          <thead role="rowgroup">
            <tr role="row">
              <th scope="col" role="columnheader">
                Configuration
              </th>
              {measures.map((measure) => (
                <th scope="col" role="columnheader" className="num" key={measure.name}>
                  {measure.name}
                  <span className="eval-sub">{measure.better}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody role="rowgroup">
            {dataset.configurations.map((configuration) => {
              const figures = configuration.figures;
              const first = comparison.status === "ordered" && comparison.first === configuration.id;
              return (
                <tr role="row" key={configuration.id} className={words.best === configuration.id ? "eval-best" : undefined}>
                  <th scope="row" role="rowheader">
                    <span className="eval-name">
                      <span className="mono">{configuration.label}</span>
                      {first ? (
                        <span className={words.tone === "ok" ? "badge eval-first" : "badge"}>
                          {words.best ? "Did best on this data" : "Listed first"}
                        </span>
                      ) : null}
                    </span>
                    <span className="eval-sub">{modelWords(configuration)}</span>
                    <span className="eval-sub">{RERUN_WORDS[configuration.rerun] ?? configuration.rerun}</span>
                  </th>
                  <td role="cell" className="num" data-label={measures[0]?.name}>
                    {figures.critical.length
                      ? figures.critical.map((item) => (
                          <span className="eval-line" key={`${item.question}=${item.label}`}>
                            {named ? `${item.question}=${item.label}: ` : ""}
                            <Shown figure={item.detected} />
                            {item.insufficient_evidence ? <span className="eval-sub">too few cases to be sure</span> : null}
                          </span>
                        ))
                      : "No critical label"}
                  </td>
                  <td role="cell" className="num" data-label={measures[1]?.name}>
                    {figures.critical.length
                      ? figures.critical.map((item) => (
                          <span
                            className={`eval-line${item.automated_misses ? " eval-warn" : ""}`}
                            key={`${item.question}=${item.label}`}
                          >
                            {named ? `${item.question}=${item.label}: ` : ""}
                            <Shown figure={item.automated} />
                          </span>
                        ))
                      : "No critical label"}
                  </td>
                  <td role="cell" className="num" data-label={measures[2]?.name}>
                    <Shown figure={figures.false_automation} />
                  </td>
                  <td role="cell" className="num" data-label={measures[3]?.name}>
                    <Shown figure={figures.accuracy} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <h3 className="eval-h3">What happened to the {dataset.n} cases</h3>
      <div className="table-wrap">
        <table className="eval-stack" role="table">
          <caption className="sr-only">What the engine did with the {dataset.n} cases, by configuration</caption>
          <thead role="rowgroup">
            <tr role="row">
              <th scope="col" role="columnheader">
                Configuration
              </th>
              {OUTCOMES.map(([key, label]) => (
                <th scope="col" role="columnheader" className="num" key={key}>
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody role="rowgroup">
            {dataset.configurations.map((configuration) => (
              <tr role="row" key={configuration.id}>
                <th scope="row" role="rowheader">
                  <span className="mono">{configuration.label}</span>
                </th>
                {OUTCOMES.map(([key, label]) => (
                  <td role="cell" className="num" key={key} data-label={label}>
                    <Shown figure={configuration.figures.outcomes[key]?.share} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="eval-hints">
        {OUTCOMES.map(([key, label, meaning]) => (
          <span key={key}>
            <strong>{label}</strong>: {meaning}.{" "}
          </span>
        ))}
        The share decided automatically is the automation rate.
      </p>

      <h3 className="eval-h3">Speed, cost and failures, as the publisher measured them</h3>
      <p className="muted group-sub">
        These are the publisher&apos;s own measurements on its own machines, over the same {dataset.n} cases. The
        registry cannot check them.
      </p>
      <ul className="eval-claimed">
        {dataset.configurations.map((configuration) => {
          const failed = failedWords(configuration.claimed, dataset.n);
          return (
            <li key={configuration.id}>
              <span className="mono">{configuration.label}</span>: {claimedWords(configuration.claimed)}.{" "}
              {failed ? <span className={configuration.claimed.failed_cases ? "eval-warn" : undefined}>{failed}</span> : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Nothing({ evaluation, here, agentHref }: { evaluation: AgentEvaluation; here: string; agentHref: string }) {
  const agent = evaluation.agent;
  const others = evaluation.versions.evaluated.filter((version) => version !== agent?.version);
  if (evaluation.state === "recalled" || !agent) {
    return (
      <section className="group recalled" aria-labelledby="none-heading">
        <div className="group-head">
          <h2 id="none-heading">Recalled</h2>
          <span className="group-tag group-tag-danger">Do not automate</span>
        </div>
        <p className="notice notice-danger">
          <strong>This version is recalled. Do not use it.</strong>
        </p>
        <p className="eval-after">
          <Link href={agentHref}>The agent&apos;s page</Link> says who recalled it and why.
        </p>
      </section>
    );
  }
  return (
    <section className="group eval-none" aria-labelledby="none-heading">
      <div className="group-head">
        <h2 id="none-heading">
          {evaluation.state === "not_verified" ? "Nothing here can be verified" : "No evaluation published"}
        </h2>
        <span className={`group-tag ${evaluation.state === "not_verified" ? "group-tag-danger" : "group-tag-warn"}`}>
          {evaluation.state === "not_verified" ? "Not verified" : "Nothing to show"}
        </span>
      </div>
      {evaluation.state === "not_verified" ? (
        <p className="evidence-empty">
          An evaluation of version {agent.version} was published, but the registry cannot verify any of it today, so
          this page shows none of its numbers. What was found is listed under Signatures, below.
        </p>
      ) : (
        <p className="evidence-empty">
          The publisher has not published how version {agent.version} did on any dataset. That says nothing good or
          bad about the agent: read its Decision Card, and evaluate it on your own cases before relying on it.
        </p>
      )}
      {others.length ? (
        <p className="eval-after">
          {others.length === 1 ? "An earlier version has" : "Earlier versions have"} a published evaluation:{" "}
          {others.map((version, index) => (
            <span key={version}>
              {index ? ", " : ""}
              <Link href={`${here}?version=${encodeURIComponent(version)}`}>{version}</Link>
            </span>
          ))}
          . It is of that version, not of this one.
        </p>
      ) : null}
      <p className="eval-after muted">
        Publishers: <code>mendari push {evaluation.id} --key publisher.key --evaluate DATASET</code> evaluates every
        configuration on one of the agent&apos;s own datasets and publishes each, signed.
      </p>
    </section>
  );
}

function Signatures({ evaluation, namespace }: { evaluation: AgentEvaluation; namespace: string }) {
  const verified = evaluation.datasets.flatMap((dataset) =>
    dataset.configurations.map((configuration) => ({ configuration, dataset: dataset.name })),
  );
  const total = verified.length + evaluation.not_shown.length;
  if (!total) return null;
  const publisher = evaluation.publisher.display_name || namespace;
  return (
    <section className={`group ${evaluation.not_shown.length ? "recalled" : "checked"}`} aria-labelledby="signature-heading">
      <div className="group-head">
        <h2 id="signature-heading">Signatures</h2>
        <span className={`group-tag ${evaluation.not_shown.length ? "group-tag-danger" : "group-tag-ok"}`}>
          {verified.length} of {total} verified
        </span>
      </div>
      <p className="muted group-sub">
        Each result is a record its publisher signed with a private key. The registry checks every signature again
        each time this page is opened, against <Link href={`/publishers/${encodeURIComponent(namespace)}`}>the keys {publisher} registered</Link>.
        A signature shows who stated the counts and that nobody changed them since. It does not show that the counts
        are right.
      </p>
      <ul className="eval-records">
        {verified.map(({ configuration, dataset }) => (
          <li key={configuration.id}>
            <span className="badge verified">Signature verified</span>{" "}
            <span className="mono">{configuration.label}</span> on <span className="mono">{dataset}</span>: signed{" "}
            {configuration.signed.at.slice(0, 10)} with key <span className="mono">{configuration.signed.key_id}</span> of{" "}
            {publisher}. Record <span className="mono break">{configuration.id.slice(0, 16)}…</span>
          </li>
        ))}
        {evaluation.not_shown.map((record) => (
          <li key={record.id} className="eval-unverified">
            <span className="badge severity-high">Not verified</span> <span className="mono">{record.configuration}</span>{" "}
            on <span className="mono">{record.dataset}</span>, key <span className="mono">{record.key_id}</span>:{" "}
            {NOT_SHOWN_WORDS[record.reason] ?? "it does not verify"}. None of its numbers are shown, and it takes no
            part in the comparison or the badge.
          </li>
        ))}
      </ul>
      {evaluation.superseded ? (
        <p className="eval-after muted">
          {counted(evaluation.superseded, "earlier evaluation")} of this version {evaluation.superseded === 1 ? "was" : "were"}{" "}
          replaced by a newer one of the same configuration and dataset, and {evaluation.superseded === 1 ? "is" : "are"} not
          shown. The registry keeps every one.
        </p>
      ) : null}
    </section>
  );
}

function Limits({ evaluation, agentHref }: { evaluation: AgentEvaluation; agentHref: string }) {
  const configurations = shownConfigurations(evaluation);
  const synthetic = evaluation.agent?.synthetic ?? false;
  return (
    <section className="group claimed" aria-labelledby="limits-heading">
      <div className="group-head">
        <h2 id="limits-heading">What these numbers do not show</h2>
        <span className="group-tag group-tag-warn">Read before relying on them</span>
      </div>
      <ul className="eval-limits">
        <li>
          <strong>How the agent will do on your patients.</strong> These are the publisher&apos;s cases
          {synthetic ? ", and they are synthetic: written for testing, not taken from care" : ""}. Your messages, your
          population and your workflows differ. Evaluate the agent on your own cases, in shadow, before it decides
          anything: <code>mendari eval {evaluation.id} --dataset your-cases.jsonl</code>.
        </li>
        <li>
          <strong>An independent test.</strong> The publisher chose the dataset and published it, so a configuration
          can be tuned to it. {rerunSummary(configurations)}
        </li>
        <li>
          <strong>A ranking of agents.</strong> The comparison is between ways of running this one agent, on the same
          cases. It says nothing about another agent.
        </li>
        <li>
          <strong>Speed and cost at your site.</strong> Those are the publisher&apos;s own measurements, on its machines
          and its model prices.
        </li>
        <li>
          <strong>Clinical review.</strong> A count of right answers is not a review by a clinician, and the registry
          records no clinical review of any agent.
        </li>
      </ul>
      <p className="eval-after">
        Stronger evidence, where there is some, is on <Link href={`${agentHref}#evidence-heading`}>the agent&apos;s page</Link>:
        counts that hospitals measured on their own cases and signed.
      </p>
    </section>
  );
}

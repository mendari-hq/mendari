// SPDX-License-Identifier: Apache-2.0
// Published evaluations (ADR-0020): the publisher's signed evaluations of the version shown, one
// row per configuration, apart from what the registry checked and from site evidence. Every
// share is k of n with its exact interval, as the registry worked it out from the counts; no row
// is ranked or marked best. Latency, cost and models are the publisher's own word, labelled so.
import { type Figure, shown } from "@/lib/figures";
import type { PublisherEvaluation, PublisherEvaluations } from "@/lib/registry";

function configurationWords(configuration: Record<string, string>): string {
  return Object.entries(configuration)
    .map(([slot, variant]) => `${slot}=${variant}`)
    .join(", ");
}

const STATUS: Record<string, string> = {
  matched: "Re-run by the registry: matched",
  calls_a_model: "Signed by the publisher; not re-run: calls a model",
};

// The publisher's own figures, never a count: "p50 0.05 ms, p95 0.06 ms; USD 0.0000 per decision".
function claimedWords(claimed: PublisherEvaluation["claimed"]): string {
  const ms = (value: number | null | undefined) => (value == null ? null : `${value} ms`);
  const latency = [
    ms(claimed.latency_ms_p50) ? `p50 ${ms(claimed.latency_ms_p50)}` : null,
    ms(claimed.latency_ms_p95) ? `p95 ${ms(claimed.latency_ms_p95)}` : null,
  ].filter(Boolean);
  const cost =
    claimed.cost_usd_per_decision == null
      ? "cost not reported"
      : `USD ${claimed.cost_usd_per_decision.toFixed(4)} per decision`;
  return `${latency.length ? latency.join(", ") : "latency not reported"}; ${cost}`;
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

function criticalLabel(item: { question: string; label: string }): string {
  return `${item.question}=${item.label}`;
}

// With one critical label (the usual case) its name is in the column's header, not in each cell.
function Row({ evaluation, named }: { evaluation: PublisherEvaluation; named: boolean }) {
  const figures = evaluation.figures;
  return (
    <tr>
      <th scope="row">
        <span className="mono">{configurationWords(evaluation.configuration)}</span>
        <span className="eval-sub muted">
          {evaluation.models.length ? `Models: ${evaluation.models.join(", ")}` : "No model reported"}
        </span>
        <span className="eval-sub muted">As reported by the publisher: {claimedWords(evaluation.claimed)}</span>
      </th>
      <td className="eval-status">{STATUS[evaluation.rerun] ?? evaluation.rerun_words}</td>
      <td className="num">
        {figures?.critical.length
          ? figures.critical.map((item) => (
              <span className="eval-line" key={criticalLabel(item)}>
                {named ? `${criticalLabel(item)}: ` : ""}
                <Shown figure={item.detected} />
                {item.insufficient_evidence ? " (insufficient evidence)" : ""}
              </span>
            ))
          : "No critical label"}
      </td>
      <td className="num">
        {figures?.critical.length
          ? figures.critical.map((item) => (
              <span className="eval-line" key={criticalLabel(item)}>
                {named ? `${criticalLabel(item)}: ` : ""}
                <Shown figure={item.automated} />
              </span>
            ))
          : "n/a"}
      </td>
      <td className="num">
        <Shown figure={figures?.accuracy} />
      </td>
      <td className="num">
        <Shown figure={figures?.false_automation} />
      </td>
    </tr>
  );
}

export function PublishedEvaluationsSection({
  evaluations,
  unavailable,
  version,
  packId,
}: {
  evaluations: PublisherEvaluations | null;
  unavailable: boolean;
  version: string;
  packId: string;
}) {
  const listed = evaluations?.evaluations ?? [];
  const compared = listed.filter((evaluation) => !evaluation.excluded);
  const excluded = listed.filter((evaluation) => evaluation.excluded);
  const datasets = [...new Set(compared.map((e) => `${e.dataset.name} (${e.dataset.n} cases)`))];
  const primary = compared[0]?.figures?.primary;
  const critical = compared[0]?.figures?.critical ?? [];
  const named = critical.length !== 1;
  const only = critical.length === 1 ? critical[0] : undefined;
  const criticalHead = only ? ` (${criticalLabel(only)})` : "";
  return (
    <section className="group published-evals" aria-labelledby="published-evaluations-heading">
      <div className="group-head">
        <h2 id="published-evaluations-heading">Published evaluations</h2>
        <span className="group-tag group-tag-warn">Publisher signed</span>
      </div>
      <p className="muted group-sub">
        The publisher&apos;s own evaluations of version {version}, one row per configuration, on its own public
        dataset{datasets.length ? `: ${datasets.join(", ")}` : ""}. A configuration can be tuned to a set its
        publisher chose: site evidence and hidden sets are stronger evidence. The rows are not ranked.
      </p>
      {unavailable ? (
        <p className="muted">Published evaluations are temporarily unavailable. Reload in a moment to check again.</p>
      ) : evaluations === null ? (
        <p className="evidence-empty">This registry does not list publisher evaluations.</p>
      ) : compared.length ? (
        <div className="table-wrap">
          <table>
            <caption className="sr-only">Published evaluations of version {version}, one row per configuration</caption>
            <thead>
              <tr>
                <th scope="col">Configuration (latency and cost as reported by the publisher)</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">Critical detection{criticalHead}</th>
                <th scope="col" className="num">Automated misses{criticalHead}</th>
                <th scope="col" className="num">Accuracy{primary ? ` (${primary})` : ""}</th>
                <th scope="col" className="num">False automation</th>
              </tr>
            </thead>
            <tbody>
              {compared.map((evaluation) => (
                <Row key={evaluation.id} evaluation={evaluation} named={named} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="evidence-empty">
          The publisher has published no evaluation of version {version}. A publisher signs one per configuration
          with <code>mendari eval {packId} --dataset test --publish --key publisher.key</code>.
        </p>
      )}
      {excluded.length ? (
        <ul className="muted evidence-left-out">
          {excluded.map((evaluation) => (
            <li key={evaluation.id}>
              Left out of the comparison: <span className="mono">{configurationWords(evaluation.configuration)}</span>,
              signed with <span className="mono">{evaluation.key_id}</span>
              {evaluation.excluded === "key revoked" ? ", a key revoked since" : `, ${evaluation.excluded}`}.
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

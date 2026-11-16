// SPDX-License-Identifier: Apache-2.0
// Site evidence (LP-22h): one figure's sites and pooled estimates, as a forest plot drawn on the
// server (inline SVG, no script) and the same numbers as a table for screen readers. Every share
// is k of n through lib/figures, as the registry worked it out; the plot only places them.
import { shown } from "@/lib/figures";
import type { EvidenceFigure, EvidenceGroup } from "@/lib/registry";

const WIDTH = 640;
const LABEL = 200; // the left column, for the sites' names
const ROW = 22;
const PAD = 8;

function x(share: number): number {
  return LABEL + PAD + Math.max(0, Math.min(1, share)) * (WIDTH - LABEL - 2 * PAD);
}

// A model's estimate per 100 cases, its interval in brackets: never a bare percentage.
function per100(value: number | undefined): string {
  return value === undefined ? "n/a" : (value * 100).toFixed(1);
}

export function pooledWords(figure: EvidenceFigure): string {
  const pooled = figure.pooled;
  if (pooled.status !== "ok" || pooled.k === undefined || pooled.n === undefined) return "insufficient evidence";
  return shown({ k: pooled.k, n: pooled.n, low: pooled.low ?? null, high: pooled.high ?? null });
}

export function randomWords(figure: EvidenceFigure): string {
  const re = figure.random_effects;
  if (re.status !== "ok") return `insufficient evidence (needs ${re.needs_sites ?? 3} sites)`;
  return `${per100(re.estimate)} per 100 [${per100(re.low)}-${per100(re.high)}%]`;
}

export function Forest({ figure }: { figure: EvidenceFigure }) {
  const rows = figure.sites.length + 2;
  const height = rows * ROW + 2 * PAD + 16;
  const pooled = figure.pooled;
  const re = figure.random_effects;
  const label = `${figure.label}: ${figure.sites.length} sites; pooled ${pooledWords(figure)}`;
  return (
    <svg className="forest" viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-label={label} width="100%">
      {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
        <g key={tick}>
          <line className="forest-grid" x1={x(tick)} x2={x(tick)} y1={PAD} y2={height - PAD - 14} />
          <text className="forest-axis" x={x(tick)} y={height - PAD} textAnchor="middle">
            {tick * 100}
          </text>
        </g>
      ))}
      <text className="forest-axis" x={WIDTH - PAD} y={height - PAD} textAnchor="end">
        per 100 cases
      </text>
      {figure.sites.map((site, i) => {
        const y = PAD + i * ROW + ROW / 2;
        const share = site.n ? site.k / site.n : 0;
        return (
          <g key={site.report}>
            <text className="forest-label" x={0} y={y + 4}>
              {site.site_name.length > 28 ? `${site.site_name.slice(0, 27)}…` : site.site_name}
            </text>
            {site.low !== null && site.high !== null ? (
              <line className="forest-interval" x1={x(site.low)} x2={x(site.high)} y1={y} y2={y} />
            ) : null}
            <rect className="forest-point" x={x(share) - 4} y={y - 4} width={8} height={8} />
          </g>
        );
      })}
      {pooled.status === "ok" && pooled.k !== undefined && pooled.n ? (
        <g>
          <text className="forest-label" x={0} y={PAD + figure.sites.length * ROW + ROW / 2 + 4}>
            Pooled (summed counts)
          </text>
          <polygon
            className="forest-pooled"
            points={[
              [x(pooled.low ?? 0), PAD + figure.sites.length * ROW + ROW / 2],
              [x(pooled.k / pooled.n), PAD + figure.sites.length * ROW + 4],
              [x(pooled.high ?? 1), PAD + figure.sites.length * ROW + ROW / 2],
              [x(pooled.k / pooled.n), PAD + (figure.sites.length + 1) * ROW - 4],
            ]
              .map((p) => p.join(","))
              .join(" ")}
          />
        </g>
      ) : null}
      {re.status === "ok" && re.estimate !== undefined ? (
        <g>
          <text className="forest-label" x={0} y={PAD + (figure.sites.length + 1) * ROW + ROW / 2 + 4}>
            Random effects
          </text>
          <line
            className="forest-interval"
            x1={x(re.low ?? 0)}
            x2={x(re.high ?? 1)}
            y1={PAD + (figure.sites.length + 1) * ROW + ROW / 2}
            y2={PAD + (figure.sites.length + 1) * ROW + ROW / 2}
          />
          <circle className="forest-random" cx={x(re.estimate)} cy={PAD + (figure.sites.length + 1) * ROW + ROW / 2} r={4} />
        </g>
      ) : null}
    </svg>
  );
}

export function EvidenceTable({ figure }: { figure: EvidenceFigure }) {
  return (
    <div className="table-wrap">
      <table>
        <caption className="muted">{figure.label}</caption>
        <thead>
          <tr>
            <th scope="col">Site</th>
            <th scope="col">Cases</th>
          </tr>
        </thead>
        <tbody>
          {figure.sites.map((site) => (
            <tr key={site.report}>
              <th scope="row">{site.site_name}</th>
              <td>
                {shown(site)}
                {site.insufficient_evidence ? " (insufficient evidence)" : ""}
              </td>
            </tr>
          ))}
          <tr>
            <th scope="row">Pooled (fixed effect, summed counts)</th>
            <td>{pooledWords(figure)}</td>
          </tr>
          <tr>
            <th scope="row">Random effects (DerSimonian-Laird)</th>
            <td>{randomWords(figure)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

export function EvidenceGroupView({ group }: { group: EvidenceGroup }) {
  const configuration = Object.entries(group.configuration)
    .map(([slot, variant]) => `${slot}=${variant}`)
    .join(", ");
  return (
    <div className="evidence-group">
      <h3>
        {group.sites} {group.sites === 1 ? "site" : "sites"}, {group.cases} cases
        {group.remote_consent ? " (a configuration with remote providers, consented)" : ""}
      </h3>
      <p className="muted">
        Configuration <span className="mono">{configuration}</span>. {group.unreported_tickets} unreported{" "}
        {group.unreported_tickets === 1 ? "ticket" : "tickets"}, {group.withdrawn_tickets} withdrawn,{" "}
        {group.superseded_reports} superseded {group.superseded_reports === 1 ? "report" : "reports"}.{" "}
        {group.tests_run} homogeneity {group.tests_run === 1 ? "test" : "tests"} run, each at 0.05.
      </p>
      {group.left_out.length ? (
        <ul className="muted">
          {group.left_out.map((item) => (
            <li key={item.report}>
              Not pooled: {item.site_name}, {item.reason}.
            </li>
          ))}
        </ul>
      ) : null}
      {group.figures.map((figure) => (
        <figure key={figure.figure} className="evidence-figure">
          <figcaption>{figure.label}</figcaption>
          {figure.warning ? <p className="notice">{figure.warning}</p> : null}
          <Forest figure={figure} />
          <EvidenceTable figure={figure} />
        </figure>
      ))}
    </div>
  );
}

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
const PAD_RIGHT = 24; // keeps the "100" tick label inside the viewBox

function x(share: number): number {
  return LABEL + PAD + Math.max(0, Math.min(1, share)) * (WIDTH - LABEL - PAD - PAD_RIGHT);
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
  const pooled = figure.pooled;
  const re = figure.random_effects;
  const showPooled = pooled.status === "ok" && pooled.k !== undefined && Boolean(pooled.n);
  const showRandom = re.status === "ok" && re.estimate !== undefined;
  const pooledRow = figure.sites.length;
  const randomRow = figure.sites.length + (showPooled ? 1 : 0);
  const rows = figure.sites.length + (showPooled ? 1 : 0) + (showRandom ? 1 : 0);
  const axisBottom = PAD + rows * ROW;
  const height = axisBottom + 36;
  const label = `${figure.label}: ${figure.sites.length} sites; pooled ${pooledWords(figure)}`;
  return (
    <div className="forest-frame">
      <svg className="forest" viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-label={label} width="100%">
        {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
          <g key={tick}>
            <line className="forest-grid" x1={x(tick)} x2={x(tick)} y1={PAD} y2={axisBottom} />
            <text className="forest-axis" x={x(tick)} y={axisBottom + 14} textAnchor="middle">
              {tick * 100}
            </text>
          </g>
        ))}
        <text className="forest-axis" x={x(0.5)} y={axisBottom + 30} textAnchor="middle">
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
              {site.n > 0 ? <rect className="forest-point" x={x(share) - 4} y={y - 4} width={8} height={8} rx={2} /> : null}
            </g>
          );
        })}
        {showPooled && pooled.k !== undefined && pooled.n ? (
          <g>
            <text className="forest-label forest-label-strong" x={0} y={PAD + pooledRow * ROW + ROW / 2 + 4}>
              Pooled (summed counts)
            </text>
            <polygon
              className="forest-pooled"
              points={[
                [x(pooled.low ?? 0), PAD + pooledRow * ROW + ROW / 2],
                [x(pooled.k / pooled.n), PAD + pooledRow * ROW + 4],
                [x(pooled.high ?? 1), PAD + pooledRow * ROW + ROW / 2],
                [x(pooled.k / pooled.n), PAD + (pooledRow + 1) * ROW - 4],
              ]
                .map((p) => p.join(","))
                .join(" ")}
            />
          </g>
        ) : null}
        {showRandom && re.estimate !== undefined ? (
          <g>
            <text className="forest-label forest-label-strong" x={0} y={PAD + randomRow * ROW + ROW / 2 + 4}>
              Random effects
            </text>
            <line
              className="forest-interval"
              x1={x(re.low ?? 0)}
              x2={x(re.high ?? 1)}
              y1={PAD + randomRow * ROW + ROW / 2}
              y2={PAD + randomRow * ROW + ROW / 2}
            />
            <circle className="forest-random" cx={x(re.estimate)} cy={PAD + randomRow * ROW + ROW / 2} r={4} />
          </g>
        ) : null}
      </svg>
    </div>
  );
}

export function EvidenceTable({ figure }: { figure: EvidenceFigure }) {
  return (
    <div className="table-wrap">
      <table>
        <caption className="sr-only">{figure.label}</caption>
        <thead>
          <tr>
            <th scope="col">Site</th>
            <th scope="col" className="num">Cases</th>
          </tr>
        </thead>
        <tbody>
          {figure.sites.map((site) => (
            <tr key={site.report}>
              <th scope="row">{site.site_name}</th>
              <td className="num">
                {shown(site)}
                {site.insufficient_evidence ? " (insufficient evidence)" : ""}
              </td>
            </tr>
          ))}
          <tr className="summary-row">
            <th scope="row">Pooled (fixed effect, summed counts)</th>
            <td className="num">{pooledWords(figure)}</td>
          </tr>
          <tr className="summary-row">
            <th scope="row">Random effects (DerSimonian-Laird)</th>
            <td className="num">{randomWords(figure)}</td>
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
      <div className="evidence-group-head">
        <h3>
          {group.sites} {group.sites === 1 ? "site" : "sites"}, {group.cases} cases
          {group.remote_consent ? " (a configuration with remote providers, consented)" : ""}
        </h3>
        <p className="muted evidence-meta">
          Configuration <span className="mono">{configuration}</span>. {group.unreported_tickets} unreported{" "}
          {group.unreported_tickets === 1 ? "ticket" : "tickets"}, {group.withdrawn_tickets} withdrawn,{" "}
          {group.superseded_reports} superseded {group.superseded_reports === 1 ? "report" : "reports"}.{" "}
          {group.tests_run} homogeneity {group.tests_run === 1 ? "test" : "tests"} run, each at 0.05.
        </p>
      </div>
      {group.left_out.length ? (
        <ul className="muted evidence-left-out">
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

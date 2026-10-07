// SPDX-License-Identifier: Apache-2.0
// Figures (mendari.figures/1): k of n with its exact interval, worked out by the registry
// (src/mendari/evals/figures.py) and only printed here, as the card and the command line print
// them (render.shown in Python), digit for digit: tests/figures.spec.ts checks this against what
// Python prints (tests/fixtures/shown.json in the repository).

export type Figure = { k: number; n: number; low: number | null; high: number | null };

// x with d decimals as Python's f"{x:.{d}f}": toFixed rounds an exact binary tie up (98.25 to
// "98.3"), Python to the even digit ("98.2"). The double's own digits (toFixed(100)) show a tie.
function fixed(x: number, d: number): string {
  const [whole = "", frac = ""] = x.toFixed(100).split(".");
  if (frac[d] === "5" && /^0*$/.test(frac.slice(d + 1))) {
    const last = d ? frac[d - 1] : whole[whole.length - 1];
    if (Number(last) % 2 === 0) return d ? `${whole}.${frac.slice(0, d)}` : whole;
  }
  return x.toFixed(d);
}

const BOUND_DIGITS = 4;
const UNDER = `<${(10 ** -BOUND_DIGITS).toFixed(BOUND_DIGITS)}`;
const OVER = `>${(100 - 10 ** -BOUND_DIGITS).toFixed(BOUND_DIGITS)}`;

// A bound as a percentage, with the digits it needs (up to 4) never to read 0 or 100 when it is not.
export function percent(value: number): string {
  let digits = 1;
  while (
    digits < BOUND_DIGITS && value > 0 && value < 1
    && [(0).toFixed(digits), (100).toFixed(digits)].includes(fixed(value * 100, digits))
  ) digits++;
  return fixed(value * 100, digits);
}

// "98.3-99.8": a bound strictly inside (0, 100) never reads as either.
function bounds(p: Figure): string {
  let low = percent(p.low ?? 0);
  let high = percent(p.high ?? 0);
  if (Number(low) === 0 && p.k > 0) low = UNDER;
  else if (Number(low) === 100) low = OVER;
  if (Number(high) === 100 && p.k < p.n) high = OVER;
  else if (Number(high) === 0) high = UNDER;
  return `${low}-${high}`;
}

// "596 of 600 [98.3-99.8%]"; "no cases" when n is 0; "n/a" when what it is of is not known (or an
// older registry that serves no figures).
export function shown(p: Figure | null | undefined): string {
  if (p === null || p === undefined) return "n/a";
  if (!p.n || p.low === null || p.high === null) return "no cases";
  return `${p.k} of ${p.n} [${bounds(p)}%]`;
}

// k / n, for ordering only (never shown as a number).
export const share = (p: Figure | null | undefined): number | null => (p && p.n ? p.k / p.n : null);

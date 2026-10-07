# SPDX-License-Identifier: Apache-2.0
"""Writes tests/fixtures/shown.json: what `mendari.evals.render` prints for a figure, for the
dashboard's and the Hub's ports of it to be checked against (LP-10), so every surface prints
the same text for the same k of n.

    uv run python scripts/shown_fixture.py          # rewrite the fixture
    uv run python scripts/shown_fixture.py --check  # exit 1 when it is not current

- `percent`: [value, text] for a bound as `render.percent` prints it: seeded values, values
  near 0 and 1, and every value whose percentage is an exact binary tie at 1 to 4 digits
  (Python rounds those half to even; JavaScript's toFixed rounds them up).
- `shown`: [k, n, low, high, text], the figure as the server serves it (`figures.of`: the
  unrounded interval of k of n) and as `render.shown` prints it: every k for n up to 60, edge
  cases up to n = 10^9, and seeded pairs.

Floats are written as Python's repr, which JSON readers in Python and JavaScript read back to
the same double.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from decimal import Decimal
from pathlib import Path

from mendari.evals import figures
from mendari.evals.render import percent, shown

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "shown.json"
SEED = 20261007


def _ties() -> list[float]:
    """Values whose percentage, as the double value x 100, lies exactly halfway at 1 to 4
    digits. Past 1 digit, `percent` only prints more digits below 0.05% and above 99.95%."""
    found = []
    for digits in range(1, 5):
        scale = 10**digits
        odd: list[int] | range = range(1, 200 * scale, 2)  # twice / (2 x scale) ends in 5
        if digits > 1:
            odd = [t for t in odd if t < scale // 10 or t > 1999 * scale // 10]
        for twice in odd:
            text = Decimal(twice) / (2 * scale)
            value = float(text / 100)
            if Decimal(value * 100) == text:
                found.append(value)
    return sorted(set(found))


def _percent_cases() -> list[list[object]]:
    rng = random.Random(SEED)
    values = [rng.random() for _ in range(400)]
    values += [0.0, 1.0, 1e-9, 4.9e-7, 5e-7, 5.1e-7, 1e-5, 4.99e-5, 5e-5, 0.999995, 0.9999995]
    values += [1 - 1e-9, 0.99949, 0.9995, 0.99951, 0.0005, 0.00049, 0.00051]
    values += _ties()
    return [[value, percent(value)] for value in sorted(set(values))]


def _pairs() -> list[tuple[int, int]]:
    pairs = {(k, n) for n in range(0, 61) for k in range(0, n + 1)}
    for n in (100, 200, 300, 598, 600, 1000, 5000, 60000, 10**6, 10**7, 10**9):
        pairs |= {(k, n) for k in (0, 1, 2, 3, n // 3, n // 2, n - 3, n - 2, n - 1, n)}
    rng = random.Random(SEED)
    for _ in range(400):
        n = rng.randint(61, 100_000)
        pairs.add((rng.randint(0, n), n))
    return sorted(pairs, key=lambda p: (p[1], p[0]))


def _shown_cases() -> list[list[object]]:
    cases: list[list[object]] = []
    for k, n in _pairs():
        figure = figures.of(k, n)
        cases.append([k, n, figure["low"], figure["high"], shown(figure)])
    return cases


def fixture() -> str:
    data = {
        "about": "render.percent and render.shown, for the dashboard's and the Hub's ports "
        "(scripts/shown_fixture.py writes it)",
        "percent": _percent_cases(),
        "shown": _shown_cases(),
        "none": shown(None),
    }
    return json.dumps(data, indent=0, allow_nan=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when it is not current")
    args = parser.parse_args(argv)
    text = fixture()
    if args.check:
        if not FIXTURE.is_file() or FIXTURE.read_text() != text:
            print(f"{FIXTURE.relative_to(ROOT)} is not current: run scripts/shown_fixture.py")
            return 1
        print(f"ok  {FIXTURE.relative_to(ROOT)} is current")
        return 0
    FIXTURE.write_text(text)
    print(f"wrote {FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

// SPDX-License-Identifier: Apache-2.0
// lib/figures.ts prints a figure as the card and the command line do (render.shown in Python):
// checked against what Python prints for 2,401 figures and 601 bounds, with the exact rounding
// ties among them (tests/fixtures/shown.json, written by scripts/shown_fixture.py).
import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { percent, shown } from "../lib/figures";

type Fixture = { percent: [number, string][]; shown: [number, number, number | null, number | null, string][]; none: string };

const fixture = JSON.parse(readFileSync("../tests/fixtures/shown.json", "utf8")) as Fixture;

test("the Hub prints a figure as the card does", () => {
  const wrong: unknown[] = [];
  for (const [value, text] of fixture.percent) {
    if (percent(value) !== text) wrong.push(["percent", value, percent(value), text]);
  }
  for (const [k, n, low, high, text] of fixture.shown) {
    const got = shown({ k, n, low, high });
    if (got !== text) wrong.push(["shown", k, n, got, text]);
  }
  expect(shown(null)).toBe(fixture.none);
  expect(wrong).toEqual([]);
});

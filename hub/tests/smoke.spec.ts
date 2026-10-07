// SPDX-License-Identifier: Apache-2.0
import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

const PACKS = [
  "AI Guard",
  "Clinical Trial Prescreener",
  "Patient Inbox Router",
  "Prior Authorization Preflight",
  "Referral Router",
];

// The referral pack's version as it ships: the registry the tests run against publishes it.
// `npm test` runs in hub/.
function shippedVersion(pack: string): string {
  const version = /^version: (.+)$/m.exec(readFileSync(`../packs/${pack}/pack.yaml`, "utf8"))?.[1];
  if (!version) throw new Error(`no version in packs/${pack}/pack.yaml`);
  return version;
}

function watchConsole(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

test("the home page lists every published pack", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Decision Packs");
  await expect(page.getByRole("list", { name: "Packs" }).getByRole("heading", { level: 3 })).toHaveText(PACKS);
  await expect(page.locator(".badge.verified")).toHaveCount(5);
  // Each benchmark is k of n with its interval (worked out by the registry), never a bare rate.
  await expect(page.getByText(/^Benchmark accuracy \d+ of \d+ \[[<\d.]+-[>\d.]+%\]$/)).toHaveCount(5);
  expect(errors).toEqual([]);
});

test("search narrows by words, risk level and standard", async ({ page }) => {
  await page.goto("/?q=referral+specialty");
  await expect(page.locator(".results h3")).toHaveText(["Referral Router"]);
  await page.goto("/?risk=2");
  await expect(page.locator(".results h3")).toHaveText(["AI Guard", "Clinical Trial Prescreener"]);
  await page.goto("/?standard=CDS+Hooks&q=authorization");
  await expect(page.locator(".results h3")).toHaveText(["Prior Authorization Preflight"]);
  await page.goto("/?q=nothing-matches-this");
  await expect(page.getByText("No pack matches these filters.")).toBeVisible();
});

test("a pack page has the facts, the pull command, the leaderboard and the card", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Referral Router" }).click();
  await expect(page).toHaveURL(/\/packs\/mendari\/referral-router$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Referral Router");
  await expect(page.locator(".pull")).toHaveText("pip install mendari && mendari pull mendari/referral-router");
  await expect(page.locator(".fact").first()).toContainText(shippedVersion("referral-router"));
  await expect(page.locator(".badge.verified")).toHaveText("verified publisher");
  await expect(page.locator(".fact").nth(1).locator(".fact-value")).toHaveText(/^\d+ of \d+ \[[<\d.]+-[>\d.]+%\]$/);
  const board = page.locator("details").first();
  await expect(board).toHaveAttribute("open", "");
  await expect(board.locator("tbody tr").first()).toBeVisible();
  // The shipped rows were measured on earlier versions: none ranks, and each says why.
  await expect(board.locator("tbody tr").first().locator("td").first()).toHaveText("\u2013");
  await expect(board.locator(".standing").first()).toContainText("its pack digest not recorded");
  await expect(page.getByRole("heading", { name: "Decision Card", exact: true })).toBeVisible();
  // No share of cases is a bare percentage: each is k of n with its interval. The card's own
  // text (thresholds, units) is left out.
  const bare = await page.locator("main").evaluate((main) => {
    const clone = main.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("article.markdown").forEach((article) => article.remove());
    return (clone.textContent ?? "").replace(/\[[^\]]*%\]/g, "").match(/.{0,30}\d\s?%.{0,10}/g) ?? [];
  });
  expect(bare).toEqual([]);
  expect(errors).toEqual([]);
});

test("a publisher page lists its packs and signing keys", async ({ page }) => {
  await page.goto("/publishers/mendari");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Mendari reference packs");
  await expect(page.getByText("Verified publisher")).toBeVisible();
  await expect(page.locator("ul").first().getByRole("link")).toHaveCount(5);
  await expect(page.locator("li.mono")).toHaveCount(1);
});

test("an unknown pack is a 404 page", async ({ page }) => {
  const response = await page.goto("/packs/mendari/no-such-pack");
  expect(response?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});

test("pages fit a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/", "/packs/mendari/referral-router", "/publishers/mendari"]) {
    await page.goto(path);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(fits, path).toBe(true);
  }
});

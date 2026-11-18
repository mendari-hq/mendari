// SPDX-License-Identifier: Apache-2.0
import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

// The registry the tests run against holds the five reference packs (verified publisher) and,
// from a community publisher, a pack whose text tries to run a script (scripts/hub_seed.py
// --hostile-example). Search lists them by id.
const PACKS = [
  "Hostile Text Example",
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

test("the home page lists every published pack, with no uploaded figure", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Decision Packs");
  await expect(page.getByRole("list", { name: "Packs" }).getByRole("heading", { level: 3 }).getByRole("link")).toHaveCount(PACKS.length);
  for (const [index, name] of PACKS.entries()) {
    await expect(page.locator(".results h3 a").nth(index)).toHaveAccessibleName(name);
  }
  await expect(page.locator(".badge.verified")).toHaveCount(5);
  // The risk level is the publisher's own word, and nothing the publisher uploaded is a figure here.
  await expect(page.getByText(/^Risk \d \(declared\)$/)).toHaveCount(PACKS.length);
  await expect(page.getByText(/Benchmark accuracy/)).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("search narrows by words, risk level and standard", async ({ page }) => {
  await page.goto("/?q=referral+specialty");
  await expect(page.locator(".results h3 a")).toHaveCount(1);
  await expect(page.locator(".results h3 a")).toHaveAccessibleName("Referral Router");
  await page.goto("/?risk=2");
  await expect(page.locator(".results h3 a")).toHaveCount(2);
  await expect(page.locator(".results h3 a").first()).toHaveAccessibleName("AI Guard");
  await expect(page.locator(".results h3 a").last()).toHaveAccessibleName("Clinical Trial Prescreener");
  await page.goto("/?standard=CDS+Hooks&q=authorization");
  await expect(page.locator(".results h3 a")).toHaveCount(1);
  await expect(page.locator(".results h3 a")).toHaveAccessibleName("Prior Authorization Preflight");
  await page.goto("/?q=nothing-matches-this");
  await expect(page.getByText("No pack matches these filters.")).toBeVisible();
});

test("a pack page shows what the registry checked apart from what the publisher claims", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Referral Router" }).click();
  await expect(page).toHaveURL(/\/packs\/mendari\/referral-router$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Referral Router");
  await expect(page.locator(".pull")).toHaveText(`mendari pull mendari/referral-router@${shippedVersion("referral-router")}`);
  await expect(page.locator(".fact").first()).toContainText(shippedVersion("referral-router"));
  await expect(page.locator(".badge.verified")).toHaveText("verified publisher");

  const checked = page.getByRole("region", { name: "Checked by the registry" });
  await expect(checked.locator("dt")).toHaveText(["Signature", "Publisher type", "Archive", "Pack digest", "Engine", "Decision Card", "Review", "Regression check", "Counter-signature"]);
  await expect(checked).toContainText("verified at publish");
  // The CI registry has no signing key, so it counter-signs nothing, and the page says so.
  await expect(checked).toContainText("Not counter-signed");
  await expect(checked).toContainText(/sha256 [0-9a-f]{64}/);
  await expect(checked).toContainText("The maintained engine: a copy of a reference pack");
  await expect(checked).toContainText("Rendered by the registry from the files the digest covers");
  await expect(checked).toContainText("Not reviewed by the registry");
  // The registry's own check on the publisher's set, labelled: never a ranking.
  await expect(checked).toContainText("publisher's own test set: regression check, not a ranking", { ignoreCase: true });
  await expect(checked).toContainText(/Accuracy \d+ of \d+ \[[<\d.]+-[>\d.]+%\] on its set \w+; nothing earlier to compare with/);

  const claimed = page.getByRole("region", { name: "Claimed by the publisher" });
  await expect(claimed.locator("dt")).toHaveText(["Publisher", "Risk level", "Licence", "Summary"]);
  await expect(claimed).toContainText("declared by the publisher, not confirmed by a review");
  await expect(claimed).toContainText("self-declared");

  // No leaderboard and no benchmark figure: the registry computed none.
  await expect(page.getByRole("heading", { name: "Provider leaderboard" })).toHaveCount(0);
  await expect(page.getByText(/Benchmark accuracy/)).toHaveCount(0);

  // The card is the registry's, its tables rendered as tables (GFM), the risk level declared.
  await expect(page.getByRole("heading", { name: "Decision Card", exact: true })).toBeVisible();
  const card = page.locator("article.markdown");
  await expect(card.locator("table").first()).toBeVisible();
  await expect(card.getByRole("cell", { name: /^1, clinical information processing .*\(declared by the publisher\)$/ })).toBeVisible();
  await expect(card).toContainText("Rendered by the registry from the pack's own files");
  await expect(card).not.toContainText("|---|");
  expect(errors).toEqual([]);
});

test("publisher text is shown as text: no script runs and no unsafe link is made", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/packs/hub-test/hostile-text");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Hostile Text Example");
  // The summary's script tag is shown as the characters it is made of, never parsed.
  await expect(page.locator(".lead")).toHaveText("Publisher text that tries to run: <script>window.__hostile = 1</script>");
  expect(await page.evaluate(() => (window as unknown as { __hostile?: number }).__hostile)).toBeUndefined();
  const main = page.locator("main");
  await expect(main.locator("script")).toHaveCount(0);
  await expect(main.locator("img")).toHaveCount(0);
  await expect(main.locator('[onerror], a[href^="javascript:" i]')).toHaveCount(0);
  // The evidence entry keeps its title; its javascript: target is dropped.
  const card = page.locator("article.markdown");
  await expect(card.getByText("A link that runs a script")).toBeVisible();
  await expect(card.getByRole("link", { name: "A link that runs a script" })).toHaveCount(0);
  // Markdown in publisher text is text: no heading, table, image or rule passes for the registry's.
  await expect(page.getByRole("heading", { name: "Checked by the registry" })).toHaveCount(1);
  await expect(card.getByRole("cell", { name: "0.99" })).toHaveCount(0);
  await expect(card.locator("hr")).toHaveCount(1); // the card's own, above its footer
  await expect(card.locator("strong", { hasText: /^Clinically reviewed/ })).toHaveCount(0);
  await expect(card).toContainText("# Checked by the registry | Benchmark accuracy | 0.99 |");
  await expect(page.locator(".badge.community")).toHaveText("community publisher");
  // The registry's review found a higher risk level than the publisher declared; its note is text.
  const checked = page.getByRole("region", { name: "Checked by the registry" });
  await expect(checked).toContainText(
    /Reviewed by the registry on \d{4}-\d\d-\d\d: risk level 2 found, where the publisher declared 0\. Reads free text from patients <b>not<\/b> routing alone/,
  );
  // An earlier version, recalled by its publisher: listed apart, with its reason as text.
  const recalled = page.getByRole("region", { name: "Recalled versions" });
  await expect(recalled.locator("li")).toHaveCount(1);
  await expect(recalled).toContainText(
    /0\.9\.0 high recalled by its publisher on \d{4}-\d\d-\d\d, signed with ed25519:[0-9a-f]{16}: Routed a <b>refill<\/b> request as routine/,
  );
  await expect(page.locator(".chip.mono")).toHaveText(["1.0.0"]);
  await expect(page.getByRole("region", { name: "Claimed by the publisher" })).toContainText(
    "declared by the publisher; the registry's review found level 2",
  );
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
  for (const path of ["/", "/packs/mendari/referral-router", "/packs/hub-test/hostile-text", "/publishers/mendari"]) {
    await page.goto(path);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(fits, path).toBe(true);
  }
});

// Site evidence (LP-22h): two synthetic sites shared signed counts of the Referral Router
// (scripts/hub_seed.py --sites); its own section, apart from the registry's checks and any rank.
test("a pack page shows site evidence in its own section: sites, cases, a plot and its table", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/packs/mendari/referral-router");
  const section = page.getByRole("region", { name: /^Site evidence/ });
  await expect(section.getByRole("heading", { level: 2 })).toHaveText("Site evidence: 2 sites, 900 cases");
  await expect(section).toContainText("not part of any hidden-set result or rank");
  await expect(section.getByRole("heading", { level: 3 }).first()).toHaveText("2 sites, 900 cases");
  const plot = section.getByRole("img", { name: /accuracy of next_step: 2 sites/ });
  await expect(plot).toBeVisible();
  const table = section.getByRole("table").first();
  await expect(table.getByRole("rowheader")).toHaveText([
    "North Health (synthetic)",
    "South Clinic (synthetic)",
    "Pooled (fixed effect, summed counts)",
    "Random effects (DerSimonian-Laird)",
  ]);
  await expect(table).toContainText(/\d+ of 600 \[[<\d.]+-[>\d.]+%\]/);
  await expect(table).toContainText(/\d+ of 900 \[/);
  await expect(table).toContainText("insufficient evidence (needs 3 sites)");
  // No bare percentage anywhere in the section: every share is k of n with its interval.
  const text = (await section.innerText()).replace(/\[[^[\]]*\]/g, "");
  expect(text.split("\n").filter((line) => line.includes("%"))).toEqual([]);
  expect(errors).toEqual([]);
});

test("a pack no site has evaluated says so in words", async ({ page }) => {
  await page.goto("/packs/mendari/ai-guard");
  const section = page.getByRole("region", { name: /^Site evidence/ });
  await expect(section.getByRole("heading", { level: 2 })).toHaveText("Site evidence");
  await expect(section).toContainText("No site has shared signed counts for version");
  await expect(section.getByRole("img")).toHaveCount(0);
});

test("a shared search restores its filters and recovers from an invalid risk level", async ({ page }) => {
  await page.goto("/?risk=unknown&q=referral&q=ignored");
  await expect(page.getByRole("searchbox")).toHaveValue("referral");
  await expect(page.getByLabel("Risk level")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("Choose a risk level from 0 to 4, or any risk level.")).toBeVisible();
  await page.getByLabel("Risk level").selectOption("");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByRole("link", { name: "Referral Router" })).toBeVisible();
  await page.getByRole("link", { name: "Clear filters" }).click();
  await expect(page.getByRole("searchbox")).toHaveValue("");
  await expect(page.locator(".results h3")).toHaveCount(PACKS.length);
});

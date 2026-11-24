// SPDX-License-Identifier: Apache-2.0
import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

// The registry the tests run against holds the demo agent this release bundles (verified
// publisher) and, from a community publisher, a copy of it whose text tries to run a script
// (scripts/hub_seed.py --hostile-example). Search lists them by id.
const AGENTS = ["Hostile Text Example", "Portal Message Agent"];
const DEMO = "/agents/mendari/portal-message-agent";

// The demo agent's version as it ships: the registry the tests run against publishes it.
// `npm test` runs in hub/.
function shippedVersion(): string {
  const manifest = "../agents/portal-message-agent/agent.yaml";
  const version = /^version: (.+)$/m.exec(readFileSync(manifest, "utf8"))?.[1];
  if (!version) throw new Error(`no version in ${manifest}`);
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

test("the home page lists every published agent, with no uploaded figure", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Healthcare AI Agents");
  await expect(page.getByRole("list", { name: "Agents" }).getByRole("heading", { level: 3 })).toHaveText(AGENTS);
  await expect(page.locator(".badge.verified")).toHaveCount(1);
  // The risk level is the publisher's own word, and nothing the publisher uploaded is a figure here.
  await expect(page.getByText(/^Risk \d \(declared\)$/)).toHaveCount(AGENTS.length);
  await expect(page.getByText(/Benchmark accuracy/)).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("search narrows by words and risk level", async ({ page }) => {
  await page.goto("/?q=hostile");
  await expect(page.locator(".results h3")).toHaveText(["Hostile Text Example"]);
  await page.goto("/?risk=0");
  await expect(page.locator(".results h3")).toHaveText(AGENTS);
  await page.goto("/?risk=2");
  await expect(page.getByText("No agent matches these filters.")).toBeVisible();
  await page.goto("/?q=nothing-matches-this");
  await expect(page.getByText("No agent matches these filters.")).toBeVisible();
});

test("an agent page shows what the registry checked apart from what the publisher claims", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Portal Message Agent" }).click();
  await expect(page).toHaveURL(/\/agents\/mendari\/portal-message-agent$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Portal Message Agent");
  await expect(page.locator(".pull")).toHaveText("pip install mendari && mendari pull mendari/portal-message-agent");
  await expect(page.locator(".fact").first()).toContainText(shippedVersion());
  await expect(page.locator(".badge.verified")).toHaveText("verified publisher");

  const checked = page.getByRole("region", { name: "Checked by the registry" });
  await expect(checked.locator("dt")).toHaveText(["Signature", "Publisher type", "Archive", "Agent digest", "Engine", "Decision Card", "Review", "Regression check", "Counter-signature"]);
  await expect(checked).toContainText("verified at publish");
  // The CI registry has no signing key, so it counter-signs nothing, and the page says so.
  await expect(checked).toContainText("Not counter-signed");
  await expect(checked).toContainText(/sha256 [0-9a-f]{64}/);
  await expect(checked).toContainText("The maintained engine: a copy of an agent the registry's own release bundles");
  await expect(checked).toContainText("Rendered by the registry from the files the digest covers");
  await expect(checked).toContainText("Not reviewed by the registry");
  // The registry's own check on the publisher's set, labelled: never a ranking.
  await expect(checked).toContainText("publisher's own test set: regression check, not a ranking", { ignoreCase: true });
  // The demo agent's default configuration calls an agent, and the registry calls none.
  await expect(checked).toContainText("Not run: its default providers call a model, which the registry does not");

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
  await expect(card.getByRole("cell", { name: /^0, .*\(declared by the publisher\)$/ })).toBeVisible();
  await expect(card).toContainText("Rendered by the registry from the agent's own files");
  await expect(card).not.toContainText("|---|");
  expect(errors).toEqual([]);
});

test("publisher text is shown as text: no script runs and no unsafe link is made", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/agents/hub-test/hostile-text");
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

test("a publisher page lists its agents and signing keys", async ({ page }) => {
  await page.goto("/publishers/mendari");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Mendari");
  await expect(page.getByText("Verified publisher")).toBeVisible();
  await expect(page.locator("ul").first().getByRole("link")).toHaveCount(1);
  await expect(page.locator(".key-list li")).toHaveCount(1);
  await expect(page.locator(".key-list li.revoked")).toHaveCount(0); // a live key carries no mark
});

test("an unknown agent is a 404 page", async ({ page }) => {
  const response = await page.goto("/agents/mendari/no-such-agent");
  expect(response?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});

// An agent's page was at /packs/<namespace>/<name> before: a link made then still opens it, and a
// name is only ever a name in the address it leads to.
test("the address an agent's page had before leads to the page", async ({ page, request }) => {
  const moved = await request.get("/packs/mendari/portal-message-agent", { maxRedirects: 0 });
  expect(moved.status()).toBe(308);
  expect(moved.headers()["location"]).toBe(DEMO);
  const odd = await request.get("/packs/%2F%2Fexample.org/a%3Fb", { maxRedirects: 0 });
  expect(odd.headers()["location"]).toBe("/agents/%2F%2Fexample.org/a%3Fb");
  await page.goto("/packs/mendari/portal-message-agent");
  await expect(page).toHaveURL(/\/agents\/mendari\/portal-message-agent$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Portal Message Agent");
});

test("pages fit a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/", DEMO, "/agents/hub-test/hostile-text", "/publishers/mendari"]) {
    await page.goto(path);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(fits, path).toBe(true);
  }
});

// Site evidence (LP-22h): two synthetic sites shared signed counts of the demo agent
// (scripts/hub_seed.py --sites); its own section, apart from the registry's checks and any rank.
test("an agent page shows site evidence in its own section: sites, cases, a plot and its table", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto(DEMO);
  const section = page.getByRole("region", { name: /^Site evidence/ });
  await expect(section.getByRole("heading", { level: 2 })).toHaveText("Site evidence: 2 sites, 900 cases");
  await expect(section).toContainText("not part of any hidden-set result or rank");
  await expect(section.getByRole("heading", { level: 3 }).first()).toHaveText(
    "2 sites, 900 cases (a configuration with remote providers, consented)",
  );
  const plot = section.getByRole("img", { name: /accuracy of request: 2 sites/ });
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

test("an agent no site has evaluated says so in words", async ({ page }) => {
  await page.goto("/agents/hub-test/hostile-text");
  const section = page.getByRole("region", { name: /^Site evidence/ });
  await expect(section.getByRole("heading", { level: 2 })).toHaveText("Site evidence");
  await expect(section).toContainText("No site has shared signed counts for version");
  await expect(section.getByRole("img")).toHaveCount(0);
});

// Published evaluations (ADR-0020): the publisher's signed evaluations of the demo agent, one per
// configuration (scripts/hub_seed.py --evaluations); apart from the registry's checks and from
// site evidence, never ranked.
test("an agent page shows the publisher's evaluations side by side, one row per configuration", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto(DEMO);
  const section = page.getByRole("region", { name: "Published evaluations" });
  await expect(section.getByRole("heading", { level: 2 })).toHaveText("Published evaluations");
  await expect(section).toContainText("own public dataset: test (600 cases)");
  await expect(section).toContainText("can be tuned to a set its publisher chose");
  await expect(section).toContainText("site evidence and hidden sets are stronger evidence");
  const table = section.getByRole("table");
  await expect(table.getByRole("columnheader")).toHaveText([
    "Configuration (latency and cost as reported by the publisher)",
    "Status",
    "Critical detection (urgent_symptoms=yes)",
    "Automated misses (urgent_symptoms=yes)",
    "Accuracy (request)",
    "False automation",
  ]);
  const rows = table.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(table.getByRole("rowheader").locator(".mono")).toHaveText(["reader=agent", "reader=rules"]);
  await expect(rows.nth(0)).toContainText("Signed by the publisher; not re-run: calls a model");
  await expect(rows.nth(0)).toContainText("Models: mock-portal-agent");
  await expect(rows.nth(1)).toContainText("Re-run by the registry: matched");
  await expect(rows.nth(1).locator("td").nth(1)).toHaveText(/^\d+ of \d+ \[[<\d.]+-[>\d.]+%\]$/);
  await expect(rows.nth(1)).toContainText(/\d+ of 600 \[[<\d.]+-[>\d.]+%\]/);
  // No row is marked best, and no share is a bare percentage: each is k of n with its interval.
  await expect(section).not.toContainText(/best/i);
  const text = (await section.innerText()).replace(/\[[^[\]]*\]/g, "");
  expect(text.split("\n").filter((line) => line.includes("%"))).toEqual([]);
  expect(errors).toEqual([]);
});

test("an agent with no published evaluation says so in words", async ({ page }) => {
  await page.goto("/agents/hub-test/hostile-text");
  const section = page.getByRole("region", { name: "Published evaluations" });
  await expect(section).toContainText("The publisher has published no evaluation of version 1.0.0");
  await expect(section.getByRole("table")).toHaveCount(0);
});

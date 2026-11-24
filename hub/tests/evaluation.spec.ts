// SPDX-License-Identifier: Apache-2.0
// An agent's evaluation page (app/agents/[namespace]/[name]/evaluation), against the registry the
// other tests run against: the demo agent with its publisher's signed evaluations of two
// configurations, and a community agent with none (scripts/hub_seed.py --hostile-example
// --evaluations). The states a seeded registry cannot be in are in evaluation-states.spec.ts.
import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

import { SITE } from "./sites";

const DEMO = "/agents/mendari/portal-message-agent";
const EVALUATION = `${DEMO}/evaluation`;
const REGISTRY = (process.env.REGISTRY_URL ?? "http://127.0.0.1:8100").replace(/\/$/, "");
const FIGURE = /^\d+ of \d+ \[[<\d.]+-[>\d.]+%\]$/;

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

test("an evaluation page says what was evaluated and on what data, in words", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto(EVALUATION);
  await expect(page).toHaveTitle("Evaluation of mendari/portal-message-agent · Mendari Agent Hub");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Evaluation of Portal Message Agent");
  await expect(page.locator(".pack-id-pill")).toHaveText(`mendari/portal-message-agent@${shippedVersion()}`);
  await expect(page.locator(".lead")).toHaveText(
    `Its publisher, Mendari, ran 2 configurations of version ${shippedVersion()} on 600 synthetic cases and ` +
      "published the results as signed counts. This page shows those counts, and what they do and do not tell you.",
  );
  // The data is synthetic, and the page says so before anything else.
  await expect(page.locator("main").locator(".notice")).toHaveText(
    "Synthetic example. Not clinically reviewed. Not for patient care.",
  );
  const glance = page.getByRole("list", { name: "Evaluation at a glance" });
  await expect(glance.locator(".trust-k")).toHaveText(["Signatures", "Data", "Critical cases automated", "Re-run by the registry"]);
  await expect(glance.locator(".trust-v")).toHaveText([
    "2 of 2 records verified",
    "600 synthetic cases",
    /^(\d+ to )?\d+ of 114$/,
    "1 of 2 configurations",
  ]);

  const what = page.getByRole("region", { name: "What was evaluated, and on what data" });
  await expect(what.locator("dt")).toHaveText(["Agent", "Who ran it", "Dataset", "Engine"]);
  await expect(what).toContainText(/Exactly this build: sha256:[0-9a-f]{64}/);
  await expect(what).toContainText("Its publisher, Mendari, on its own machines. Nobody else chose the cases.");
  await expect(what).toContainText("test: 600 cases, a file inside the agent's signed archive");
  await expect(what).toContainText("Where the cases come from. Synthetic: written or generated for testing. No patient's record is in it.");
  await expect(what).toContainText("Who labelled them. Not stated by the publisher.");
  await expect(what).toContainText("What counts as critical. urgent_symptoms=yes (114 of the 600 cases)");
  await expect(what).toContainText(/Mendari \d+\.\d+\.\d+\S*, as stated by the publisher\./);
  await expect(what.getByRole("link", { name: "Mendari" })).toHaveAttribute("href", "/publishers/mendari");
  expect(errors).toEqual([]);
});

test("the configurations are compared: whether one did best, with intervals, and which miss critical cases", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto(EVALUATION);
  const section = page.getByRole("region", { name: "How the configurations compare" });
  await expect(section).toContainText("Every row below is the same 600 synthetic cases, so the rows can be compared.");
  // What the comparison shows, in sentences: who is first, by what, and whether it could be
  // chance. One is named as having done best only where the registry names it; the row is
  // then marked, and otherwise no row is.
  const verdict = section.getByRole("note", { name: "What the comparison shows" });
  await expect(verdict.locator(".eval-verdict-head")).toHaveText(
    /^(reader=(rules|agent) did best on this dataset|No configuration is clearly best on this dataset: reader=(rules|agent) is listed first, but it is not clearly ahead of reader=(rules|agent)\.$)/,
  );
  const named = /did best on this dataset/.test((await verdict.locator(".eval-verdict-head").textContent()) ?? "");
  await expect(section.locator("tr.eval-best")).toHaveCount(named ? 1 : 0);
  await expect(section.getByRole("table").first().locator("tbody tr").first().locator(".badge")).toHaveText(
    named ? "Did best on this data" : "Listed first",
  );
  await expect(verdict).toContainText(/\d+ of \d+ \[[<\d.]+-[>\d.]+%\]/);
  await expect(verdict).toContainText(/The two intervals (do not overlap|overlap)/);
  await expect(verdict).toContainText("Cost never decides it.");
  // Which miss critical cases: one sentence for each configuration.
  const critical = section.getByRole("list", { name: "Critical cases, by configuration" });
  await expect(critical.getByRole("listitem")).toHaveText([
    /^reader=\w+ (did not detect \d+ of 114 critical cases|detected all 114 critical cases); (none was|\d+ of 114 (was|were)) decided automatically/,
    /^reader=\w+ (did not detect \d+ of 114 critical cases|detected all 114 critical cases); (none was|\d+ of 114 (was|were)) decided automatically/,
  ]);

  const table = section.getByRole("table").first();
  await expect(table.getByRole("columnheader")).toHaveText([
    "Configuration",
    "Critical cases detected (urgent_symptoms=yes)more is better",
    "Critical cases automated (urgent_symptoms=yes)fewer is better",
    "Wrong answers automatedfewer is better",
    "Correct answers (request)more is better",
  ]);
  const rows = table.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  const labels = await table.getByRole("rowheader").locator(".mono").allTextContents();
  expect([...labels].sort()).toEqual(["reader=agent", "reader=rules"]);
  const rules = rows.filter({ hasText: "reader=rules" });
  const agent = rows.filter({ hasText: "reader=agent" });
  await expect(rules).toContainText("Answered by: rules@");
  await expect(rules).toContainText("Re-run by the registry: it counted the same.");
  await expect(agent).toContainText("Model: mock-portal-agent");
  await expect(agent).toContainText("Not re-run: it calls a model, which the registry does not. These counts are the publisher's signed word.");
  // Every number carries its denominator: each cell is k of n with its interval.
  for (const row of [rules, agent]) {
    for (const cell of await row.locator("td").all()) await expect(cell).toHaveText(FIGURE);
    await expect(row.locator("td").nth(0)).toHaveText(/ of 114 \[/);
    await expect(row.locator("td").nth(3)).toHaveText(/ of 600 \[/);
  }

  // The outcome mix, and so the automation rate.
  const outcomes = section.getByRole("table").nth(1);
  await expect(outcomes.getByRole("columnheader")).toHaveText([
    "Configuration",
    "Decided automatically",
    "Sent to a person",
    "No suggestion made",
    "Blocked",
  ]);
  for (const cell of await outcomes.locator("tbody td").all()) await expect(cell).toHaveText(/^\d+ of 600 \[[<\d.]+-[>\d.]+%\]$/);
  await expect(section).toContainText("The share decided automatically is the automation rate.");
  // Cost and latency are the publisher's own, labelled so, and say how many cases they are over.
  await expect(section).toContainText("These are the publisher's own measurements on its own machines, over the same 600 cases.");
  await expect(section.locator(".eval-claimed li")).toHaveText([
    /^reader=\w+: half of the decisions took under [\d.,]+ ms, 19 in 20 under [\d.,]+ ms; USD \d+\.\d{4} per decision\. No provider failure in the 600 cases\.$/,
    /^reader=\w+: half of the decisions took under [\d.,]+ ms, 19 in 20 under [\d.,]+ ms; USD \d+\.\d{4} per decision\. No provider failure in the 600 cases\.$/,
  ]);
  // No share is a bare percentage anywhere in the comparison: each is k of n with its interval.
  const text = (await section.innerText()).replace(/\[[^[\]]*\]/g, "").replace("(a 95% interval)", "");
  expect(text.split("\n").filter((line) => line.includes("%"))).toEqual([]);
  expect(errors).toEqual([]);
});

test("the page says whose key signed each result, what the numbers do not show, and where the card is", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto(EVALUATION);
  const signatures = page.getByRole("region", { name: "Signatures" });
  await expect(signatures.locator(".group-tag")).toHaveText("2 of 2 verified");
  await expect(signatures).toContainText("The registry checks every signature again each time this page is opened");
  await expect(signatures).toContainText("It does not show that the counts are right.");
  await expect(signatures.getByRole("listitem")).toHaveText([
    /^Signature verified reader=\w+ on test: signed \d{4}-\d\d-\d\d with key ed25519:[0-9a-f]{16} of Mendari\. Record [0-9a-f]{16}…$/,
    /^Signature verified reader=\w+ on test: signed \d{4}-\d\d-\d\d with key ed25519:[0-9a-f]{16} of Mendari\. Record [0-9a-f]{16}…$/,
  ]);
  await expect(signatures.getByRole("link", { name: "the keys Mendari registered" })).toHaveAttribute("href", "/publishers/mendari");

  const limits = page.getByRole("region", { name: "What these numbers do not show" });
  await expect(limits.locator("li strong")).toHaveText([
    "How the agent will do on your patients.",
    "An independent test.",
    "A ranking of agents.",
    "Speed and cost at your site.",
    "Clinical review.",
  ]);
  await expect(limits).toContainText("These are the publisher's cases, and they are synthetic: written for testing, not taken from care.");
  await expect(limits).toContainText("mendari eval mendari/portal-message-agent --dataset your-cases.jsonl");
  await expect(limits).toContainText(
    "The registry ran 1 of 2 configurations again itself and counted the same; the other one calls a model the " +
      "registry does not call, so its counts rest on the publisher's signature.",
  );

  const next = page.getByRole("region", { name: "Read next" });
  await next.getByRole("link", { name: "The agent's Decision Card" }).click();
  await expect(page).toHaveURL(new RegExp(`${DEMO}#decision-card-heading$`));
  await expect(page.getByRole("heading", { name: "Decision Card", exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("the page shows the badge the registry serves, and the Markdown that links it to the page", async ({ page, request }) => {
  const errors = watchConsole(page);
  await page.goto(EVALUATION);
  const section = page.getByRole("region", { name: "The badge" });
  const image = section.getByRole("img");
  await expect(image).toHaveAttribute(
    "alt",
    /^Mendari: evaluated, 2 configurations, 600 synthetic cases, (\d+ to )?\d+ critical automated$/,
  );
  // The registry's own image, shown as data: it drew (the page's policy lets it), and nothing ran.
  const source = (await image.getAttribute("src")) ?? "";
  expect(source.startsWith("data:image/svg+xml;base64,")).toBe(true);
  expect(await image.evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth > 0)).toBe(true);
  const badgeUrl = `${REGISTRY}/v1/agents/mendari/portal-message-agent/badge.svg`;
  await expect(section.locator("code")).toHaveText(`[![Mendari evaluation](${badgeUrl})](${SITE}${EVALUATION})`);
  // It is the badge at that address, byte for byte: an image any page may show, kept five minutes.
  const served = await request.get(badgeUrl);
  expect(served.status()).toBe(200);
  expect(served.headers()["content-type"]).toBe("image/svg+xml");
  expect(served.headers()["cache-control"]).toBe("public, max-age=300");
  expect(served.headers()["cross-origin-resource-policy"]).toBe("cross-origin");
  expect(served.headers()["x-content-type-options"]).toBe("nosniff");
  expect(Buffer.from(source.split(",")[1] ?? "", "base64").toString("utf8")).toBe(await served.text());
  expect(await served.text()).toContain(((await image.getAttribute("alt")) ?? "").replace("Mendari: ", ""));
  expect(errors).toEqual([]);
});

test("an agent with no published evaluation says so plainly", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto("/agents/hub-test/hostile-text/evaluation");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Evaluation of Hostile Text Example");
  await expect(page.locator(".lead")).toHaveText(
    "Its publisher, Hub test publisher, has published no evaluation of version 1.0.0. There is nothing here to weigh yet.",
  );
  const section = page.getByRole("region", { name: "No evaluation published" });
  await expect(section).toContainText("The publisher has not published how version 1.0.0 did on any dataset.");
  await expect(section).toContainText("That says nothing good or bad about the agent");
  await expect(section).toContainText("mendari push hub-test/hostile-text --key publisher.key --evaluate DATASET");
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Signatures" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "What these numbers do not show" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "The badge" }).getByRole("img")).toHaveAttribute(
    "alt",
    "Mendari: no evaluation published",
  );
  // The card is still one click away.
  await expect(page.getByRole("link", { name: "The agent's Decision Card" })).toHaveAttribute(
    "href",
    "/agents/hub-test/hostile-text#decision-card-heading",
  );
  expect(errors).toEqual([]);
});

test("an agent's page leads to its evaluation page", async ({ page }) => {
  await page.goto(DEMO);
  const section = page.getByRole("region", { name: "Published evaluations" });
  await section.getByRole("link", { name: "Read the evaluation page" }).click();
  await expect(page).toHaveURL(new RegExp(`${EVALUATION}$`));
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Evaluation of Portal Message Agent");
  // And back, by the breadcrumb.
  await page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "portal-message-agent" }).click();
  await expect(page).toHaveURL(new RegExp(`${DEMO}$`));
});

test("an evaluation of an agent or a version that is not there is a 404 page", async ({ page }) => {
  for (const path of [
    "/agents/mendari/no-such-agent/evaluation",
    `${EVALUATION}?version=99.0.0`,
    `${EVALUATION}?version=not-a-version`,
    `${EVALUATION}?version=1.0.0&version=2.0.0`,
  ]) {
    const response = await page.goto(path);
    expect(response?.status(), path).toBe(404);
    await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
  }
  const exact = await page.goto(`${EVALUATION}?version=${shippedVersion()}`);
  expect(exact?.status()).toBe(200);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Evaluation of Portal Message Agent");
});

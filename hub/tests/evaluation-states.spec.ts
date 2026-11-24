// SPDX-License-Identifier: Apache-2.0
// The evaluation page in every state it has words for, against a stand-in registry
// (tests/stub-registry.mjs) that answers what a real registry answered in each: the fixture is
// tests/fixtures/evaluation_pages.json in the repository, written by
// tests/support/evaluation_pages.py from a registry's own routes and checks. One, two and several
// configurations; a clear first, a first inside the intervals, a tie, too few critical cases and
// none; a record changed after it was stored, a key revoked, a recall; publisher text that tries
// to run; an earlier version.
//
// A configuration is named as having done best only where the registry names it
// (`comparison.best`: it is alone in the first band, ADR-0020 as amended 2026-11-24): the cases
// "two" and "clear". Everywhere else the page says that none is clearly best.
import { expect, test, type Page } from "@playwright/test";

import { STATES } from "./sites";

const at = (name: string, query = "") => `${STATES}/agents/states/${name}/evaluation${query}`;
const CASES = [
  "none",
  "one",
  "two",
  "several",
  "overlap",
  "clear",
  "clean",
  "tied",
  "insufficient",
  "no-critical",
  "first-format",
  "failed",
  "hostile",
  "tampered",
  "unverified",
  "revoked",
  "recalled",
  "older",
];

function watchConsole(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

const compared = (page: Page) => page.getByRole("region", { name: "How the configurations compare" });
const verdict = (page: Page) => compared(page).getByRole("note", { name: "What the comparison shows" });
const headline = (page: Page) => verdict(page).locator(".eval-verdict-head");
const rows = (page: Page) => compared(page).getByRole("table").first().locator("tbody tr");
const badgeAlt = (page: Page) => page.getByRole("region", { name: "The badge" }).getByRole("img");

test("one configuration is compared with nothing", async ({ page }) => {
  await page.goto(at("one"));
  await expect(page.locator(".lead")).toContainText("ran 1 configuration of version");
  await expect(headline(page)).toHaveText("One configuration was published, so there is nothing to compare it with.");
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).locator(".badge")).toHaveCount(0); // nothing is first where nothing is compared
  await expect(page.getByRole("region", { name: "Signatures" }).locator(".group-tag")).toHaveText("1 of 1 verified");
  await expect(page.getByRole("region", { name: "What these numbers do not show" })).toContainText(
    "The registry ran the configuration again itself and counted the same, which shows the counts are real, not that the dataset is hard.",
  );
  await expect(badgeAlt(page)).toHaveAttribute("alt", /^Mendari: evaluated, 600 synthetic cases, \d+ critical automated$/);
});

test("two configurations, one alone in the first band: it is named, with what decided it, and that it still lets critical cases through", async ({ page }) => {
  await page.goto(at("two"));
  await expect(headline(page)).toHaveText(
    /^reader=rules did best on this dataset, and it still let \d+ of 114 critical cases through with no person reading them\.$/,
  );
  await expect(verdict(page)).toHaveClass(/eval-verdict-warn/); // best of two, and no reassurance
  await expect(verdict(page)).toContainText(
    /It automated a wrong answer in 0 of 600 \[0\.0-0\.6%\] cases; reader=agent in \d+ of 600 \[[\d.]+-[\d.]+%\]\./,
  );
  await expect(verdict(page)).toContainText("The two intervals do not overlap, so the difference is unlikely to be chance.");
  await expect(rows(page).locator("th .mono")).toHaveText(["reader=rules", "reader=agent"]);
  await expect(rows(page).nth(0).locator(".badge")).toHaveText("Did best on this data");
  await expect(rows(page).nth(0).locator(".badge")).not.toHaveClass(/eval-first/);
  await expect(rows(page).nth(1).locator(".badge")).toHaveCount(0);
  // Who labelled the cases is the publisher's own word, shown as such.
  await expect(page.getByRole("region", { name: "What was evaluated, and on what data" })).toContainText(
    "Who labelled them. The script that wrote the cases (scripts/demo_agent_data.py) (as stated by the publisher)",
  );
});

test("several configurations in the first band: they are listed in the comparison's order, and none is clearly best", async ({ page }) => {
  await page.goto(at("several"));
  await expect(page.locator(".lead")).toContainText("ran 3 configurations of version");
  await expect(rows(page).locator("th .mono")).toHaveText(["reader=cautious", "reader=rules", "reader=agent"]);
  // Every configuration the counts cannot tell from the first is named with it.
  await expect(headline(page)).toHaveText(
    "No configuration is clearly best on this dataset: reader=cautious is listed first, but it is not clearly ahead of reader=rules or reader=agent.",
  );
  await expect(verdict(page)).toHaveClass(/eval-verdict-warn/); // critical cases were automated: no reassurance
  await expect(compared(page).locator("tr.eval-best")).toHaveCount(0);
  await expect(verdict(page)).toContainText(
    /It automated \d+ of 114 \[[\d.]+-[\d.]+%\] of the critical cases; reader=rules automated \d+ of 114 \[[\d.]+-[\d.]+%\]\./,
  );
  await expect(verdict(page)).toContainText("The two intervals overlap: with this many cases, the difference could be chance.");
  await expect(verdict(page)).toContainText("The other 1 configuration follows in the same order.");
  await expect(rows(page).nth(0).locator(".badge")).toHaveText("Listed first");
  await expect(compared(page)).not.toContainText("Did best on this data");
  await expect(badgeAlt(page)).toHaveAttribute(
    "alt",
    /^Mendari: evaluated, 3 configurations, 600 synthetic cases, \d+ to \d+ critical automated$/,
  );
  await page.goto(at("overlap"));
  await expect(headline(page)).toHaveText(
    "No configuration is clearly best on this dataset: reader=wider is listed first, but it is not clearly ahead of reader=rules.",
  );
  await expect(rows(page).nth(0).locator(".badge")).toHaveText("Listed first");
  await expect(compared(page)).not.toContainText("Did best on this data");
});

test("a configuration that lets no critical case through and is clearly ahead reads as having done best", async ({ page }) => {
  await page.goto(at("clear"));
  await expect(headline(page)).toHaveText("reader=careful did best on this dataset.");
  await expect(verdict(page)).toHaveClass(/eval-verdict-ok/);
  await expect(compared(page).locator("tr.eval-best")).toHaveCount(1);
  await expect(compared(page).locator("tr.eval-best th .mono")).toHaveText("reader=careful");
  await expect(verdict(page)).toContainText(
    /It automated 0 of 114 \[0\.0-3\.2%\] of the critical cases; reader=rules automated \d+ of 114 \[[\d.]+-[\d.]+%\]\./,
  );
  await expect(rows(page).nth(0).locator(".badge")).toHaveText("Did best on this data");
  await expect(rows(page).nth(0).locator(".badge")).toHaveClass(/eval-first/);
  const critical = compared(page).getByRole("list", { name: "Critical cases, by configuration" }).getByRole("listitem");
  await expect(critical.nth(0)).toHaveText("reader=careful detected all 114 critical cases; none was decided automatically.");
  await expect(critical.nth(0)).not.toHaveClass(/eval-flag/);
  await expect(critical.nth(1)).toHaveText(
    /^reader=rules did not detect \d+ of 114 critical cases; \d+ of 114 were decided automatically, with no person reading them\.$/,
  );
  await expect(critical.nth(1)).toHaveClass(/eval-flag/);
  // What it cost: the outcome mix shows that the careful rules automated nothing at all.
  const outcomes = compared(page).getByRole("table").nth(1).locator("tbody tr");
  await expect(outcomes.nth(0).locator("td").nth(0)).toHaveText("0 of 600 [0.0-0.6%]");
  await expect(outcomes.nth(0).locator("td").nth(1)).toHaveText(/^600 of 600 \[/);
  // The badge gives the fewest and the most, never the best configuration alone.
  await expect(badgeAlt(page)).toHaveAttribute(
    "alt",
    /^Mendari: evaluated, 2 configurations, 600 synthetic cases, 0 to \d+ critical automated$/,
  );
  const glance = page.getByRole("list", { name: "Evaluation at a glance" });
  await expect(glance.locator(".trust-v").nth(2)).toHaveText(/^0 to \d+ of 114$/);
});

test("an evaluation in which no critical case was automated says so, and no more", async ({ page }) => {
  await page.goto(at("clean"));
  await expect(badgeAlt(page)).toHaveAttribute("alt", "Mendari: evaluated, 600 synthetic cases, 0 critical automated");
  const glance = page.getByRole("list", { name: "Evaluation at a glance" });
  await expect(glance.locator(".trust-v").nth(2)).toHaveText("0 of 114");
  await expect(glance.locator("li").nth(2)).toHaveClass(/trust-pill-ok/);
  // One configuration: nothing is compared, so nothing reads as a verdict.
  await expect(verdict(page)).toHaveClass(/eval-verdict-neutral/);
  await expect(headline(page)).toHaveText("One configuration was published, so there is nothing to compare it with.");
});

test("a tie, too few critical cases and no critical label name no configuration as best", async ({ page }) => {
  await page.goto(at("tied"));
  await expect(headline(page)).toHaveText(
    "No configuration is clearly best: reader=rules and reader=same did the same on every measure compared.",
  );
  await expect(compared(page).locator(".badge")).toHaveCount(0);

  await page.goto(at("insufficient"));
  await expect(headline(page)).toHaveText("Too few critical cases to say which configuration works best.");
  await expect(verdict(page)).toContainText("at least 100 positive cases");
  await expect(rows(page).nth(0).locator("td").nth(0)).toContainText("too few cases to be sure");
  await expect(compared(page).locator(".badge")).toHaveCount(0);
  await expect(page.locator(".lead")).toContainText("on 300 synthetic cases");

  await page.goto(at("no-critical"));
  await expect(headline(page)).toHaveText("No configuration is named as best: this agent names no critical label.");
  await expect(rows(page).nth(0).locator("td").nth(0)).toHaveText("No critical label");
  await expect(compared(page).getByRole("list", { name: "Critical cases, by configuration" })).toHaveCount(0);
  await expect(page.getByRole("list", { name: "Evaluation at a glance" })).toContainText("No critical label named");
  await expect(page.getByRole("region", { name: "What was evaluated, and on what data" })).toContainText(
    "What counts as critical. The agent names no critical label.",
  );
  await expect(badgeAlt(page)).toHaveAttribute("alt", "Mendari: evaluated, 2 configurations, 600 synthetic cases");
});

test("a record changed after it was stored is shown as not verified, with none of its numbers", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto(at("tampered"));
  // Someone with the database rewrote the rules' record to read 114 of 114 detected, 0 automated.
  const signatures = page.getByRole("region", { name: "Signatures" });
  await expect(signatures.locator(".group-tag")).toHaveText("1 of 2 verified");
  const unverified = signatures.locator("li.eval-unverified");
  await expect(unverified).toHaveText(
    /^Not verified reader=rules on test, key ed25519:[0-9a-f]{16}: the stored record is no longer the one that was signed\. None of its numbers are shown, and it takes no part in the comparison or the badge\.$/,
  );
  await expect(signatures.locator("li").first()).toContainText("Signature verified reader=agent on test");
  await expect(page.getByRole("list", { name: "Evaluation at a glance" })).toContainText("1 of 2 records verified");
  // The comparison holds the one record that verifies, and nothing the forged one says.
  await expect(rows(page).locator("th .mono")).toHaveText(["reader=agent"]);
  await expect(headline(page)).toHaveText("One configuration was published, so there is nothing to compare it with.");
  await expect(page.locator("main")).not.toContainText("114 of 114");
  await expect(page.locator(".lead")).toContainText("ran 1 configuration of version");
  // What is left calls a model, so nothing shown here was run again by the registry.
  await expect(page.getByRole("region", { name: "What these numbers do not show" })).toContainText(
    "The registry did not run it again: it calls a model the registry does not call, so its counts rest on the publisher's signature.",
  );
  expect(errors).toEqual([]);
});

test("an evaluation none of which verifies shows no number at all", async ({ page }) => {
  await page.goto(at("unverified"));
  await expect(page.locator(".lead")).toHaveText(
    /^An evaluation of version \S+ was published, but none of it can be verified, so none of its numbers are shown\.$/,
  );
  const nothing = page.getByRole("region", { name: "Nothing here can be verified" });
  await expect(nothing).toContainText("the registry cannot verify any of it today, so this page shows none of its numbers");
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "How the configurations compare" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "What these numbers do not show" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Signatures" }).locator(".group-tag")).toHaveText("0 of 1 verified");
  await expect(badgeAlt(page)).toHaveAttribute("alt", "Mendari: evaluation not verified");
  await expect(page.locator("main")).not.toContainText(/\d+ of 114/);

  await page.goto(at("revoked"));
  await expect(page.getByRole("region", { name: "Signatures" }).locator("li.eval-unverified")).toContainText(
    "its key was revoked after it signed this",
  );
  await expect(badgeAlt(page)).toHaveAttribute("alt", "Mendari: evaluation not verified");
});

test("a recalled agent says so, and shows no evaluation", async ({ page }) => {
  await page.goto(at("recalled"));
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Evaluation of states/recalled");
  const recalled = page.getByRole("region", { name: "Recalled" });
  await expect(recalled).toContainText("This version is recalled. Do not use it.");
  await expect(recalled.getByRole("link", { name: "The agent's page" })).toHaveAttribute("href", "/agents/states/recalled");
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Signatures" })).toHaveCount(0);
  await expect(badgeAlt(page)).toHaveAttribute("alt", "Mendari: recalled");
});

test("what an earlier record does not state is said to be not stated, and failed cases are said", async ({ page }) => {
  await page.goto(at("first-format"));
  const what = page.getByRole("region", { name: "What was evaluated, and on what data" });
  await expect(what).toContainText(
    "Where the cases come from. Not stated in the signed record. The agent's own manifest marks its data as synthetic.",
  );
  await expect(what).toContainText("Who labelled them. Not stated by the publisher.");
  await expect(what).toContainText("Not stated in the signed records.");
  await expect(compared(page).locator(".eval-claimed li")).not.toContainText("provider");
  await expect(page.getByRole("region", { name: "Signatures" }).locator(".group-tag")).toHaveText("1 of 1 verified");

  await page.goto(at("failed"));
  await expect(verdict(page)).toContainText(
    "A provider failed on 3 of the 600 cases in reader=agent's run. Each went to a person, so its figures are partly of the failure.",
  );
  const claimed = compared(page).locator(".eval-claimed li");
  await expect(claimed).toHaveText(
    "reader=agent: half of the decisions took under 1,840 ms, 19 in 20 under 3,921 ms; USD 0.0062 per decision. " +
      "A provider failed on 3 of 600 cases: each went to a person, so those cases say nothing of this configuration.",
  );
  await expect(claimed.locator(".eval-warn")).toHaveCount(1);
});

test("publisher text is shown as text: an agent's name and who labelled its cases run nothing", async ({ page }) => {
  const errors = watchConsole(page);
  await page.goto(at("hostile"));
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Evaluation of Hostile <script>window.__hostile = 5</script> Agent",
  );
  await expect(page.getByRole("region", { name: "What was evaluated, and on what data" })).toContainText(
    'Who labelled them. <img src=x onerror="window.__hostile = 6"> "two" & <b>nurses</b> (as stated by the publisher)',
  );
  expect(await page.evaluate(() => (window as unknown as { __hostile?: number }).__hostile)).toBeUndefined();
  const main = page.locator("main");
  await expect(main.locator("script")).toHaveCount(0);
  await expect(main.locator("[onerror], b")).toHaveCount(0);
  await expect(main.locator("img")).toHaveCount(1); // the badge, and no image a publisher named
  await expect(badgeAlt(page)).toHaveAttribute("alt", /^Mendari: evaluated, 600 synthetic cases, /);
  expect(errors).toEqual([]);
});

test("a new version starts with no evaluation, and an earlier one's is its own page", async ({ page }) => {
  await page.goto(at("older"));
  await expect(page.locator(".pack-id-pill")).toHaveText("states/older@2.0.0");
  const none = page.getByRole("region", { name: "No evaluation published" });
  await expect(none).toContainText("The publisher has not published how version 2.0.0 did on any dataset.");
  await expect(none).toContainText("An earlier version has a published evaluation: 1.0.0. It is of that version, not of this one.");
  await expect(badgeAlt(page)).toHaveAttribute("alt", "Mendari: no evaluation published");
  await none.getByRole("link", { name: "1.0.0" }).click();
  await expect(page).toHaveURL(at("older", "?version=1.0.0"));
  await expect(page.locator(".pack-id-pill")).toHaveText("states/older@1.0.0");
  await expect(page.locator(".eval-older")).toHaveText("You are reading version 1.0.0. The newest version is 2.0.0: read its evaluation.");
  await expect(rows(page)).toHaveCount(2);
  // The badge is the newest version's, so the earlier version's page shows none.
  await expect(page.getByRole("region", { name: "The badge" })).toHaveCount(0);
  await page.locator(".eval-older").getByRole("link", { name: "read its evaluation" }).click();
  await expect(page).toHaveURL(at("older"));
});

test("an agent with no evaluation, and one the registry does not hold", async ({ page }) => {
  await page.goto(at("none"));
  await expect(page.getByRole("region", { name: "No evaluation published" })).toContainText("did on any dataset");
  await expect(page.getByRole("region", { name: "No evaluation published" })).not.toContainText("earlier version");
  const missing = await page.goto(at("no-such-agent"));
  expect(missing?.status()).toBe(404);
});

test("every state fits a phone, runs nothing, and shows no bare percentage", async ({ page }) => {
  const errors = watchConsole(page);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const name of CASES) {
    const response = await page.goto(at(name));
    expect(response?.status(), name).toBe(200);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(fits, name).toBe(true);
    await expect(page.getByRole("heading", { level: 1 }), name).toContainText("Evaluation of ");
    // Every share is k of n with its interval; the one percentage in words is the interval's own.
    const text = (await page.locator("main").innerText()).replace(/\[[^[\]]*\]/g, "").replace("(a 95% interval)", "");
    expect(text.split("\n").filter((line) => line.includes("%")), name).toEqual([]);
  }
  expect(errors).toEqual([]);
});

import { expect, test, type Page } from "@playwright/test";

const PACKS = [
  "AI Guard",
  "Clinical Trial Prescreener",
  "Patient Inbox Router",
  "Prior Authorization Preflight",
  "Referral Router",
];

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
  await expect(page.locator(".fact").first()).toContainText("0.1.1");
  await expect(page.locator(".badge.verified")).toHaveText("verified publisher");
  await expect(page.locator("details").first()).toHaveAttribute("open", "");
  await expect(page.locator("details").first().locator("tbody tr").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Decision Card", exact: true })).toBeVisible();
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

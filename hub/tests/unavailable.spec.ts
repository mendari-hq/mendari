// SPDX-License-Identifier: Apache-2.0
// With the registry unreachable, every page says so. A production build tells the error
// boundary nothing of why a page failed, so a page that only threw said "Something went wrong".
import { expect, test } from "@playwright/test";

const SITE = "http://127.0.0.1:3101"; // playwright.config.ts: its REGISTRY_URL refuses

for (const path of ["/packs/mendari/portal-message-agent", "/publishers/mendari"]) {
  test(`${path} says the registry cannot be reached`, async ({ page }) => {
    await page.goto(`${SITE}${path}`);
    const alert = page.getByRole("main").getByRole("alert");
    await expect(alert.getByRole("heading", { level: 1 })).toHaveText("The Hub can't reach the registry");
    await expect(alert.getByRole("link", { name: "Try again" })).toHaveAttribute("href", path);
  });
}

test("the catalog says the registry cannot be reached", async ({ page }) => {
  await page.goto(`${SITE}/`);
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Registry temporarily unreachable");
});

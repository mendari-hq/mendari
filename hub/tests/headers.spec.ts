// SPDX-License-Identifier: Apache-2.0
// The security headers on every page and asset (LP-18f, proxy.ts), by exact value.
import { expect, test } from "@playwright/test";

const EXPECTED: Record<string, string> = {
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
  "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
  "cross-origin-opener-policy": "same-origin",
  "cross-origin-resource-policy": "same-origin",
  "cross-origin-embedder-policy": "require-corp",
};

const CSP =
  /^default-src 'self'; script-src 'self' 'nonce-([A-Za-z0-9+/=]+)' 'strict-dynamic'; style-src 'self' 'nonce-\1'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'$/;

const PAGES = ["/", "/?q=portal", "/agents/mendari/portal-message-agent", "/agents/hub-test/hostile-text", "/publishers/mendari", "/agents/mendari/no-such-agent", "/no-such-page"];
// Where an agent's page was before, and where its redirect leads.
const MOVED = [
  ["/packs/mendari/portal-message-agent", "/agents/mendari/portal-message-agent"],
  ["/packs/hub-test/hostile-text", "/agents/hub-test/hostile-text"],
] as const;

// The policy's nonce, once every header of an answer is as expected.
function checked(headers: Record<string, string>, path: string): string {
  for (const [name, value] of Object.entries(EXPECTED)) expect(headers[name], `${path} ${name}`).toBe(value);
  const policy = CSP.exec(headers["content-security-policy"] ?? "");
  expect(policy, path).not.toBeNull();
  // This machine: no HSTS (browsers ignore it over plain http, and it would bind localhost).
  expect(headers["strict-transport-security"], path).toBeUndefined();
  return policy?.[1] ?? "";
}

test("every page and asset carries the headers, each nonce fresh", async ({ page, request }) => {
  await page.goto("/");
  const assets = await page.locator('link[rel="stylesheet"], script[src]').evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute("href") ?? node.getAttribute("src") ?? "").filter(Boolean),
  );
  expect(assets.length).toBeGreaterThan(0);
  const nonces = new Set<string>();
  for (const path of [...PAGES, ...assets.slice(0, 3)]) {
    const answer = await request.get(path);
    nonces.add(checked(answer.headers(), path));
  }
  expect(nonces.size).toBe(PAGES.length + Math.min(assets.length, 3));
});

test("the redirect from the address an agent's page had before carries the headers too", async ({ request }) => {
  for (const [before, now] of MOVED) {
    const answer = await request.get(before, { maxRedirects: 0 });
    expect(answer.status(), before).toBe(308);
    expect(answer.headers()["location"], before).toBe(now);
    checked(answer.headers(), before);
  }
});

test("a page's scripts carry its nonce and nothing breaks the policy", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (message) => {
    if (/Content Security Policy|Refused to/.test(message.text())) violations.push(message.text());
  });
  const answer = await page.goto("/agents/mendari/portal-message-agent");
  const nonce = CSP.exec(answer?.headers()["content-security-policy"] ?? "")?.[1];
  expect(nonce).toBeTruthy();
  const inline = await page.locator("script:not([src])").evaluateAll((nodes) =>
    nodes.map((node) => (node as HTMLScriptElement).nonce),
  );
  expect(inline.length).toBeGreaterThan(0);
  expect(new Set(inline)).toEqual(new Set([nonce]));
  expect(violations).toEqual([]);
});

test("HSTS on any other host", async ({ request }) => {
  const answer = await request.get("/", { headers: { host: "hub.example.org" } });
  expect(answer.headers()["strict-transport-security"]).toBe("max-age=63072000");
});

// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "@playwright/test";

// The site against a running registry that holds the demo agent (REGISTRY_URL, seeded by
// scripts/hub_seed.py). `npm run build` first; this starts the production server, and a second
// one that reaches no registry.
export default defineConfig({
  testDir: "tests",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:3100" },
  webServer: [
    {
      command: "npm run start",
      url: "http://127.0.0.1:3100",
      reuseExistingServer: !process.env.CI,
      env: { REGISTRY_URL: process.env.REGISTRY_URL ?? "http://127.0.0.1:8100" },
    },
    // The same site with no registry to read (tests/unavailable.spec.ts): port 9 refuses.
    {
      command: "npx next start -p 3101",
      url: "http://127.0.0.1:3101",
      reuseExistingServer: !process.env.CI,
      env: { REGISTRY_URL: "http://127.0.0.1:9" },
    },
  ],
});

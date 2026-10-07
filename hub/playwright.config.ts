// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "@playwright/test";

// The site against a running registry that holds the five reference packs (REGISTRY_URL, seeded
// by scripts/hub_seed.py). `npm run build` first; this starts the production server.
export default defineConfig({
  testDir: "tests",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:3100" },
  webServer: {
    command: "npm run start",
    url: "http://127.0.0.1:3100",
    reuseExistingServer: !process.env.CI,
    env: { REGISTRY_URL: process.env.REGISTRY_URL ?? "http://127.0.0.1:8100" },
  },
});

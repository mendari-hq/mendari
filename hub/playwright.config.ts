// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "@playwright/test";

import { port, SITE, STATES, STUB_REGISTRY, UNREACHABLE } from "./tests/sites";

// The site against a running registry that holds the demo agent (REGISTRY_URL, seeded by
// scripts/hub_seed.py). `npm run build` first; this starts the production server, a second one
// that reaches no registry, and a third that reads a stand-in registry (tests/sites.ts).
export default defineConfig({
  testDir: "tests",
  timeout: 30_000,
  use: { baseURL: SITE },
  webServer: [
    {
      command: `npx next start -p ${port(SITE)}`,
      url: SITE,
      reuseExistingServer: !process.env.CI,
      env: { REGISTRY_URL: process.env.REGISTRY_URL ?? "http://127.0.0.1:8100" },
    },
    // The same site with no registry to read (tests/unavailable.spec.ts): port 9 refuses.
    {
      command: `npx next start -p ${port(UNREACHABLE)}`,
      url: UNREACHABLE,
      reuseExistingServer: !process.env.CI,
      env: { REGISTRY_URL: "http://127.0.0.1:9" },
    },
    // A stand-in registry that answers an agent's evaluation in every state, and the same site
    // reading it (tests/evaluation-states.spec.ts).
    {
      command: `node tests/stub-registry.mjs ${port(STUB_REGISTRY)}`,
      url: `${STUB_REGISTRY}/v1/health`,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: `npx next start -p ${port(STATES)}`,
      url: STATES,
      reuseExistingServer: !process.env.CI,
      env: { REGISTRY_URL: STUB_REGISTRY },
    },
  ],
});

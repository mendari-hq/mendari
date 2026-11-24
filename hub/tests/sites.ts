// SPDX-License-Identifier: Apache-2.0
// The sites the tests open, which playwright.config.ts starts: the built Hub three times, each
// reading another registry. HUB_TEST_PORT moves them all (two checkouts tested at once on one
// machine would otherwise share ports, and each would open the other's build).
const BASE = Number(process.env.HUB_TEST_PORT ?? 3100);

// Against a running registry that holds the demo agent (REGISTRY_URL, seeded by scripts/hub_seed.py).
export const SITE = `http://127.0.0.1:${BASE}`;
// Its registry refuses every connection (tests/unavailable.spec.ts).
export const UNREACHABLE = `http://127.0.0.1:${BASE + 1}`;
// Against tests/stub-registry.mjs: a registry's own answers for an agent's evaluation in every
// state (tests/evaluation-states.spec.ts).
export const STATES = `http://127.0.0.1:${BASE + 2}`;
export const STUB_REGISTRY = `http://127.0.0.1:${BASE + 99}`;

export const port = (site: string) => new URL(site).port;

// SPDX-License-Identifier: Apache-2.0
import { createRequire } from "node:module";
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// eslint-plugin-react 7.37.5 detects React's version through context.getFilename(), which ESLint 10
// removed (jsx-eslint/eslint-plugin-react#3977, vercel/next.js#89764). Read the installed version
// here instead of eslint-config-next's "detect"; drop this once eslint-config-next ships a fixed plugin.
const reactVersion = createRequire(import.meta.url)("react/package.json").version;

const config = [
  ...nextCoreWebVitals,
  ...nextTypescript,
  { settings: { react: { version: reactVersion } } },
  { ignores: [".next/**", "node_modules/**", "playwright-report/**", "test-results/**"] },
];

export default config;

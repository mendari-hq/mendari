// SPDX-License-Identifier: Apache-2.0
// A stand-in registry for the evaluation page's tests (tests/evaluation-states.spec.ts): it
// answers an agent's evaluation and its badge from tests/fixtures/evaluation_pages.json in the
// repository, which holds a real registry's own answers in every state the page has words for
// (written by tests/support/evaluation_pages.py; a Python test holds it to what the registry
// answers now). A seeded registry cannot be in most of those states: a record changed after it
// was stored, a key revoked, a tie.
//
//   node tests/stub-registry.mjs PORT
import { readFileSync } from "node:fs";
import { createServer } from "node:http";

const fixture = JSON.parse(readFileSync(new URL("../../tests/fixtures/evaluation_pages.json", import.meta.url), "utf8"));
const port = Number(process.argv[2] ?? 3199);
const ROUTE = /^\/v1\/agents\/([^/]+)\/([^/]+)\/(evaluation|badge\.svg)$/;

function answer(response, status, type, body) {
  response.writeHead(status, { "content-type": type, "cache-control": "no-store" });
  response.end(body);
}

createServer((request, response) => {
  const url = new URL(request.url ?? "/", "http://stub.invalid");
  if (url.pathname === "/v1/health") return answer(response, 200, "application/json", '{"status":"ok"}');
  const match = ROUTE.exec(url.pathname);
  const version = url.searchParams.get("version");
  const found = match ? fixture.agents[`${match[1]}/${match[2]}${version ? `@${version}` : ""}`] : undefined;
  if (!match || !found) return answer(response, 404, "application/json", '{"detail":"Not Found"}');
  if (match[3] === "badge.svg") return answer(response, 200, "image/svg+xml", found.badge);
  return answer(response, 200, "application/json", JSON.stringify(found.evaluation));
}).listen(port, "127.0.0.1");

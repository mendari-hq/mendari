// SPDX-License-Identifier: Apache-2.0
// Every answer's security headers (LP-18f): a CSP with a fresh nonce per page (the site renders
// at request time, so Next stamps the nonce on its own scripts), no framing, no referrer, and
// HSTS except on this machine. tests/headers.spec.ts asserts each value on every page.
import { NextResponse, type NextRequest } from "next/server";

const STATIC_HEADERS: Record<string, string> = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Cross-Origin-Embedder-Policy": "require-corp",
};
const HSTS = "max-age=63072000";

export function csp(nonce: string): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

function local(host: string): boolean {
  const name = host.replace(/^\[|\]$/g, "").toLowerCase();
  return name === "localhost" || name.endsWith(".localhost") || name === "127.0.0.1" || name === "::1";
}

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const policy = csp(nonce);
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", policy);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", policy);
  for (const [name, value] of Object.entries(STATIC_HEADERS)) response.headers.set(name, value);
  // The host the browser asked for (Next's own URL names the host it listens on).
  const host = (request.headers.get("host") ?? request.nextUrl.hostname).replace(/:\d+$/, "");
  if (!local(host)) response.headers.set("Strict-Transport-Security", HSTS);
  return response;
}

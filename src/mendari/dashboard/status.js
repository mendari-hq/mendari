// SPDX-License-Identifier: Apache-2.0
// The status page (/status, status.html, LP-21b): this deployment's readiness
// (/api/health/ready, server/readiness.py), read every 30 seconds while the page is open. The
// answer holds fixed codes only, so the page shows nothing that is not public: whether the
// database answers, whether the packs loaded, and when it last asked. An answer that does not
// come is an outage too. Only this deployment's own state: the operator's status page
// (docs/operations/status-checks.md) adds sign-in, a decision, the registry and the Hub, and
// survives an outage that takes this page down.

import { setupTheme } from "./theme.js";
import { esc, OWN_REQUEST } from "./ui.js";

const EVERY_MS = 30_000;
const TIMEOUT_MS = 10_000;
const STATES = {
  ready: { tone: "", title: "Working", text: "The service answers and its packs are loaded." },
  degraded: { tone: "warning", title: "Working, with a problem", text: "The service answers, but a pack did not load." },
  unready: { tone: "danger", title: "Not working", text: "The service cannot take decisions now." },
  silent: { tone: "danger", title: "Not answering", text: "The service did not answer." },
};
const DATABASE = { ok: "Answering", unreachable: "Not answering" };
const PACKS = { ok: "Loaded", degraded: "One or more did not load", none: "None served" };

const body = document.getElementById("status-body");

setupTheme();

async function readiness() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    // Public, so any browser reads it; with Mendari's own header all the same, as every page's request.
    const answer = await fetch("/api/health/ready", { cache: "no-store", headers: OWN_REQUEST, signal: controller.signal });
    const json = await answer.json().catch(() => null);
    return json && STATES[json.status] ? json : null;
  } catch {
    return null; // no answer at all
  } finally {
    clearTimeout(timer);
  }
}

function show(answer) {
  const state = STATES[answer?.status] ?? STATES.silent;
  const checks = answer?.checks ?? {};
  const when = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  body.innerHTML = `
    <div class="callout ${state.tone || "neutral"}" data-status="${esc(answer?.status ?? "silent")}">
      <b>${esc(state.title)}.</b> ${esc(state.text)}
    </div>
    <dl class="facts">
      <dt>Database</dt><dd data-check="database">${esc(DATABASE[checks.database] ?? "Unknown")}</dd>
      <dt>Packs</dt><dd data-check="packs">${esc(PACKS[checks.packs] ?? "Unknown")}</dd>
      <dt>Version</dt><dd>${esc(answer?.version ?? "Unknown")}</dd>
      <dt>Checked</dt><dd>${esc(when)}, again every 30 seconds</dd>
    </dl>`;
}

async function check() {
  show(await readiness());
}

check();
setInterval(() => { if (!document.hidden) check(); }, EVERY_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });

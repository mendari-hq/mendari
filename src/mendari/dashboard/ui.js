// SPDX-License-Identifier: Apache-2.0
// Shared helpers: formatting, badges and icons, the API and its refusals, messages, local storage,
// the app state, the membership and machine roles, links to the docs site, focus and busy states,
// a dialog that asks before a change, and the stat block. Imports nothing: the invitation page
// (invite.js) uses it too.

export const $ = (selector, root = document) => root.querySelector(selector);

export const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// A single case's confidence or probability, never a share of cases (that is a figure, below).
export const pct = (v, d = 1) => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(d)}%`);

// ---- figures: k of n with its exact interval (mendari.figures/1, worked out by the server) ----
// Printed as the card and the command line print them (render.shown in Python), digit for digit:
// tests/fixtures/shown.json holds what Python prints and the browser suite checks this against it.

// x with d decimals as Python's f"{x:.{d}f}": toFixed rounds an exact binary tie up (98.25 to
// "98.3"), Python to the even digit ("98.2"). The double's own digits (toFixed(100)) show a tie.
function fixed(x, d) {
  if (typeof x !== "number" || !Number.isFinite(x)) return "n/a";
  const [whole, frac] = x.toFixed(100).split(".");
  if (frac[d] === "5" && /^0*$/.test(frac.slice(d + 1))) {
    const last = d ? frac[d - 1] : whole[whole.length - 1];
    if (Number(last) % 2 === 0) return d ? `${whole}.${frac.slice(0, d)}` : whole;
  }
  return x.toFixed(d);
}

const BOUND_DIGITS = 4;
const UNDER = `<${(10 ** -BOUND_DIGITS).toFixed(BOUND_DIGITS)}`;
const OVER = `>${(100 - 10 ** -BOUND_DIGITS).toFixed(BOUND_DIGITS)}`;

// A bound as a percentage, with the digits it needs (up to 4) never to read 0 or 100 when it is not.
export function percent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
  let digits = 1;
  while (digits < BOUND_DIGITS && value > 0 && value < 1
    && [(0).toFixed(digits), (100).toFixed(digits)].includes(fixed(value * 100, digits))) digits++;
  return fixed(value * 100, digits);
}

// "98.3-99.8": a bound strictly inside (0, 100) never reads as either.
function bounds(p) {
  let low = percent(p.low);
  let high = percent(p.high);
  if (Number(low) === 0 && p.k > 0) low = UNDER;
  else if (Number(low) === 100) low = OVER;
  if (Number(high) === 100 && p.k < p.n) high = OVER;
  else if (Number(high) === 0) high = UNDER;
  return `${low}-${high}`;
}

// "596 of 600 [98.3-99.8%]"; "no cases" when n is 0; "n/a" when what it is of is not known.
export function shown(p) {
  if (p === null || p === undefined) return "n/a";
  if (!p.n) return "no cases";
  return `${p.k} of ${p.n} [${bounds(p)}%]`;
}

// The same text as HTML, the interval set smaller (its text is unchanged for screen readers).
export function figureHtml(p) {
  if (p === null || p === undefined || !p.n) return esc(shown(p));
  return `${esc(p.k)} of ${esc(p.n)} <span class="interval">[${esc(bounds(p))}%]</span>`;
}

// k / n, for sorting, tone and geometry only (never shown as a number).
export const share = (p) => (p && p.n ? p.k / p.n : null);

// Beside a critical label's figure with fewer positives than ranking and comparing need.
export const evidenceMark = (insufficient, min) => (insufficient
  ? ` <span class="evidence-mark" title="Fewer than ${esc(min)} positives: too few to rank or compare on">insufficient evidence</span>`
  : "");

export const num = (v, d = 3) => (v === null || v === undefined ? "n/a" : Number(v).toFixed(d));

// Times and days are shown in the deployment's time zone (from /api/health, default US Eastern),
// whatever the viewer's own clock says; the zone's short name (EDT, EST) is part of every time.
export const DEFAULT_TIMEZONE = "America/New_York";

export const time = (iso) => (iso ? state.timeFormat.format(new Date(iso)) : "");

export const shortTime = (iso) => (iso ? state.shortFormat.format(new Date(iso)) : "");

export const timeFormatFor = (timeZone) => new Intl.DateTimeFormat("en-US", {
  timeZone, year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
});

export const shortFormatFor = (timeZone) => new Intl.DateTimeFormat("en-US", {
  timeZone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
});

// "ET" for US Eastern: right in both EDT and EST, unlike the seasonal short name.
export function zoneShort() {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: state.timezone, timeZoneName: "shortGeneric" }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? state.timezone;
  } catch {
    return state.timezone;
  }
}

export function zoneName() {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: state.timezone, timeZoneName: "longGeneric" }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? state.timezone;
  } catch {
    return state.timezone;
  }
}

// Cost per decision: "$0" when free, two significant figures below a cent ("$0.000043"), cents above.
export function money(v) {
  if (v === null || v === undefined) return "n/a";
  if (v === 0) return "$0";
  if (v < 0.000001) return "< $0.000001";
  return v < 0.01 ? `$${Number(v.toPrecision(2))}` : `$${v.toFixed(2)}`;
}

export const OUTCOMES = ["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK"];

// Plain words first; the exact code stays one hover away (notes/ui-ux-review.md, decision 1).
const OUTCOME_TEXT = { AUTO: "Automated", HUMAN_REVIEW: "Human review", ABSTAIN: "Abstained", BLOCK: "Blocked" };

export const PRIORITY_TEXT = { stat: "Stat", asap: "ASAP", urgent: "Urgent", routine: "Routine" };

export const outcomeText = (o) => OUTCOME_TEXT[o] ?? o;

export const outcomeBadge = (o) => `<span class="badge o-${esc(o)}" title="${esc(o)}">${esc(outcomeText(o))}</span>`;

export const priorityBadge = (p) => `<span class="badge p-${esc(p)}" title="priority: ${esc(p)}">${esc(PRIORITY_TEXT[p] ?? p)}</span>`;

// Icons come from the sprite in index.html; they are decorative, so the text beside them names them.
export const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${esc(name)}"/></svg>`;

// Memberships mode: the organization this window shows goes with each request, so one meant for
// it never lands in another chosen since in a second window (the server answers 409).
export const orgPin = () => (state.accounts === "memberships" && state.me?.org ? { "x-mendari-org": state.me.org } : {});

// Every request a Mendari page makes says it is one (server/origin.py `OWN_REQUEST`): a browser
// without Fetch Metadata (Safari before 16.4) sends no `Sec-Fetch-Site`, so a read with the
// session cookie and without this header could be another site's link, and is refused. Another
// site cannot add it without a CORS preflight, which Mendari never grants. Downloads are fetched
// for it too (`download`): a plain link cannot send a header.
export const OWN_REQUEST = { "x-mendari-request": "1" };

export async function api(path, options = {}, asked = false) {
  const pin = orgPin();
  const headers = options.headers
    ? { ...OWN_REQUEST, ...pin, ...options.headers }
    : { "content-type": "application/json", ...OWN_REQUEST, ...pin };
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => null);
  // Refused for want of the notice's acknowledgement (a new version since the dashboard opened):
  // ask, then try once more (notice.js).
  if (response.status === 403 && response.headers.get("Mendari-Notice-Required") && !asked) {
    const { acknowledgeNotice } = await import("./notice.js");
    await acknowledgeNotice();
    return api(path, options, true);
  }
  refusal(path, response, body);
  return body;
}

// Throws what a refused answer means here (an ended session, launch or access; another
// organization chosen since; else its own detail).
function refusal(path, response, body) {
  if (response.status === 403 && body?.error === "NoOrganization" && state.accounts === "memberships") throw accessEnded();
  if (response.status === 409 && body?.error === "OrganizationChanged") throw organizationChanged();
  if (response.status === 401 && path.startsWith("/api/smart/")) throw launchEnded();
  if (response.status === 409 && path.startsWith("/api/smart/")) throw launchReplaced();
  if (response.status === 401 && signedIn()) throw sessionEnded();
  // Anything else: its words, with its status and error code for a view that tells refusals apart,
  // and what else the answer held (a refusal that names keys, or where a problem was found).
  if (!response.ok) {
    throw Object.assign(new Error(refusalText(body) || response.statusText), { status: response.status, code: body?.error ?? null, body });
  }
}

// A refusal's words: FastAPI's 422 lists its problems ({loc, msg}); a FHIR route's is an
// OperationOutcome (each issue's diagnostics, after where it is); everything else has a detail.
export const refusalText = (body) => {
  if (Array.isArray(body?.detail)) return body.detail.map((d) => String(d?.msg ?? "").replace(/^Value error, /, "")).join("; ");
  if (body?.resourceType === "OperationOutcome") {
    return (body.issue ?? []).map((i) => [i?.expression?.join(", "), i?.diagnostics].filter(Boolean).join(": ")).join("; ");
  }
  return body?.detail || body?.error || "";
};

// A file to save, fetched so it carries Mendari's own header and, in memberships mode, the
// window's organization as every other request does (a link cannot send a header), and refused,
// saying so, once that organization is not the chosen one any more. Saved under the name the
// server gives it, else `name`.
export async function download(path, name = "download") {
  try {
    const response = await fetch(path, { headers: { ...OWN_REQUEST, ...orgPin() } });
    if (!response.ok) refusal(path, response, await response.json().catch(() => null));
    const named = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "");
    saveFile(await response.blob(), named?.[1] ?? name);
  } catch (error) {
    toastError(error.message);
  }
}

// A Blob saved as a file called `name`, as a download link saves it.
export function saveFile(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

// Today in the deployment's time zone ("2026-10-08"): the date in a saved file's name.
export function today() {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: state.timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const part = (type) => parts.find((p) => p.type === type).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

// A file name's part from an id ("mendari/portal-message-agent" → "mendari-portal-message-agent").
export const fileSlug = (id) => String(id ?? "").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "all";

// A 401 for someone who was signed in: the session ended (30 minutes idle or its 8 hours, or
// signed out elsewhere). Say so where it stays on screen, with the way back to this same view.
// Nothing navigates away by itself: a review note being typed is not lost.
export const SESSION_ENDED = "Your session ended. Sign in again.";

function sessionEnded() {
  const banner = $("#session-ended");
  if (banner.hidden) {
    const here = `${location.pathname}${location.search}${location.hash}`;
    $("#sign-in-again").href = `/auth/login?next=${encodeURIComponent(here)}`;
    banner.hidden = false;
    announce(SESSION_ENDED, { error: true });
  }
  return new Error(SESSION_ENDED);
}

// Memberships mode: the membership this window acts with ended (removed, or given again and not
// chosen since). Said where it stays, for a window that had an organization; reloading shows what
// the person may do now.
export const ACCESS_ENDED = "Your access to this organization ended.";

function accessEnded() {
  return banner("#access-ended", ACCESS_ENDED, Boolean(state.me?.org));
}

// Memberships mode: another organization was chosen in another window since this one opened.
export const ORGANIZATION_CHANGED = "This window shows another organization than the one you chose since.";

function organizationChanged() {
  return banner("#org-changed", ORGANIZATION_CHANGED, true);
}

function banner(selector, message, shown) {
  const box = $(selector);
  if (shown && box.hidden) {
    box.querySelector("[data-reload]").onclick = () => location.reload();
    box.hidden = false;
    announce(message, { error: true });
  }
  return new Error(message);
}

// A 401 from the launch's own routes: the EHR launch ended (idle, its limit, the EHR's token, or
// ended elsewhere), whether or not someone is signed in here too. Signing in to Mendari would not
// bring it back: only the EHR can launch again.
export const LAUNCH_ENDED = "This EHR launch ended. Open Mendari again from the EHR.";

function launchEnded() {
  const banner = $("#launch-ended");
  if (banner.hidden) {
    banner.hidden = false;
    announce(LAUNCH_ENDED, { error: true });
  }
  return Object.assign(new Error(LAUNCH_ENDED), { launchEnded: true });
}

// A 409 from the launch's own routes: the window's launch (the `X-Mendari-Launch` it sends) is no
// longer the browser's, a later launch replaced it. The patient view says so in place.
function launchReplaced() {
  return Object.assign(new Error("This window's launch was replaced."), { launchReplaced: true });
}

// A short message. Errors are announced at once (role="alert") and stay on screen longer; other
// messages are announced politely. The live region is emptied first, so a repeat is announced too.
export function toast(message, { error = false } = {}) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.classList.add("show");
  announce(message, { error });
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), error ? 8000 : 2600);
}

export const toastError = (message) => toast(message, { error: true });

// For screen readers only (the eye has it on screen already).
export function announce(message, { error = false } = {}) {
  const region = $(error ? "#announce-alert" : "#announce-status");
  region.textContent = "";
  requestAnimationFrame(() => (region.textContent = message));
}

export function storageGet(key, fallback) {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}

export function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode: fine */ }
}

// `launchOnly`: an EHR launch and no one signed in (sign-in on): the patient view is all there is,
// the packs come from the launch's session, and nothing else is asked of the API. `launchPin`: the
// launch this window is pinned to (dashboard/patient.js), kept when the address loses it.
// `accounts`: "claims" or "memberships" (`/api/auth/me`); `memberships`: the person's own, in
// memberships mode; `noAccess`: why they see the access page instead of the sections (access.js).
// `backendServices`: `{token_endpoint}` when SMART Backend Services is on (`/api/health`, signed
// in), else null. `checkpointKey`: the id of the key that signs the daily checkpoint (`/api/health`),
// else null: the audit trail then asks for no checkpoint. `labelling`: the person holds `label` and
// the deployment serves the labelling routes (labelling.js); `written`: likewise a grader's
// routes over written output (written.js); `labelOnly`: and holds nothing else, so those screens
// are all they see. `evalsets`: the deployment has the evaluation-set keys
// (`/api/health` names their ids to a signed-in reader): in memberships mode, its administrators'
// hidden sets screen is served (evalsets.js). `registry`: the pack registry is served here
// (MENDARI_REGISTRY=on; `/api/health` says so to a signed-in reader): the Access view adds its
// namespaces and publisher tokens (registry.js). `reviewOnly`: the person holds nothing but
// `registry_review`, so the registry's review screen is all they see.
export const state = {
  packs: [], packId: null, detail: null, view: null, smart: null, me: null, launchOnly: false, launchPin: null, phiMode: "off",
  accounts: "claims", memberships: [], noAccess: null, backendServices: null, checkpointKey: null, labelling: false, written: false, labelOnly: false,
  evalsets: false, registry: false, reviewOnly: false, replayDraft: null, packQueue: new Map(),
  timezone: DEFAULT_TIMEZONE, timeFormat: timeFormatFor(DEFAULT_TIMEZONE), shortFormat: shortFormatFor(DEFAULT_TIMEZONE),
};

// What the signed-in person may do ("decide", "review", "evaluate", "approve"...; open mode:
// everything). The server enforces the same table; this only hides what would be refused.
export const may = (permission) => !state.me || state.me.permissions.includes(permission);
export const signedIn = () => Boolean(state.me && state.me.kind !== "open");

// The roles a membership may hold (core/accounts.py MEMBERSHIP_ROLES; a test keeps them equal), in
// plain words. A machine's own role is never one of them, and is never offered here.
export const MEMBERSHIP_ROLES = [
  { key: "viewer", label: "Viewer", text: "reads decisions and the audit trail" },
  { key: "reviewer", label: "Reviewer", text: "decides and reviews cases" },
  { key: "service", label: "Service", text: "decides and records outcomes, for a system" },
  { key: "governance", label: "Governance", text: "approves agents and holds automation" },
  { key: "admin", label: "Administrator", text: "everything but labelling, and manages people" },
  { key: "pack_author", label: "Agent author", text: "writes agents in the organization's agent store, where it is on" },
  { key: "labeller", label: "Labeller", text: "labels the cases of hidden evaluation sets, blind" },
  // Given in the pack registry's review organization only (a 422 in any other, LP-19e): offered
  // where the server says this organization is that one (`me.registry_review_org`).
  { key: "registry_reviewer", label: "Registry reviewer", text: "reviews the agent registry: namespace claims, recalls and deletions", reviewOrg: true },
];

// The roles this organization's administrators may give: every one, but the registry
// reviewer's outside the registry's review organization.
export const offeredRoles = () => MEMBERSHIP_ROLES.filter((r) => !r.reviewOrg || Boolean(state.me?.registry_review_org));

export const roleName = (key) => MEMBERSHIP_ROLES.find((r) => r.key === key)?.label ?? key;

// A machine's roles, the only ones an administrator gives an API key or a Backend Services client (core/audit.py
// MACHINE_ROLES; a test keeps them equal), in the same words.
export const MACHINE_ROLES = MEMBERSHIP_ROLES.filter((r) => ["viewer", "service"].includes(r.key));

// The documentation site (mkdocs.yml `site_url`; a test keeps them equal).
export const DOCS_SITE = "https://mendari-hq.github.io/mendari/";

// A page of the docs by its source path under docs/ ("guides/api.md#fhir-in-fhir-out", as the
// key presets name it) as the site serves it ("…/guides/api/#fhir-in-fhir-out"); null for
// anything that would leave the site.
export function docsLink(doc) {
  if (typeof doc !== "string") return null;
  const [page, anchor = ""] = doc.split("#", 2);
  if (!/^[a-z0-9][a-z0-9/_-]*\.md$/i.test(page) || page.includes("..")) return null;
  const path = page.replace(/(^|\/)index\.md$/, "$1").replace(/\.md$/, "/");
  const site = new URL(DOCS_SITE);
  const url = new URL(path, site);
  if (url.origin !== site.origin || !url.pathname.startsWith(site.pathname) || url.search) return null;
  url.hash = anchor;
  return url.href;
}

export const packPath = () => `/api/agents/${state.packId}`;

// A pack chosen while another was still loading wins: the older answer is dropped when it arrives.
// The load in flight is kept: a view that reads the pack waits for it (`packLoaded`, router.js),
// one that reads none (the Access view) does not.
let packLoad = Promise.resolve();
let packFailed = false;

export function loadPack(packId) {
  state.packId = packId;
  packFailed = false;
  if (state.launchOnly) return (packLoad = Promise.resolve()); // a launch reads no pack's details (the patient view needs none)
  const load = api(`/api/agents/${packId}`).then((detail) => {
    if (state.packId === packId) state.detail = detail;
  });
  // Said by the view that waits for it, not as an unhandled rejection.
  load.catch(() => { if (packLoad === load) packFailed = true; });
  packLoad = load;
  return load;
}

// The chosen pack's details, loaded. Once they failed (and the view said so), the next view
// shown, or its "Try again", asks for them again.
export const packLoaded = () => (packFailed ? loadPack(state.packId) : packLoad);

export function watchEdges(el) {
  const edges = () => {
    if (!el.isConnected) return observer.disconnect();
    el.classList.toggle("scrolled", el.scrollLeft > 0);
    el.classList.toggle("more-right", el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
  };
  el.addEventListener("scroll", edges, { passive: true });
  const observer = new ResizeObserver(edges);
  observer.observe(el);
  edges();
}

// Copy text to clipboard with consistent feedback (used across card, audit, try, admin);
// `failed` says what to select by hand when the browser refuses.
export async function copyText(text, label = "Copied", failed = "Copying isn't available here: select the text and copy it") {
  try {
    await navigator.clipboard.writeText(text);
    toast(label);
    return true;
  } catch {
    toastError(failed);
    return false;
  }
}

export function focusHeading(heading, { scroll = true } = {}) {
  if (!heading) return;
  heading.tabIndex = -1;
  if (scroll) heading.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
  heading.focus({ preventScroll: true });
}

export const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const scrollBehavior = () => (reducedMotion() ? "auto" : "smooth");

// Disable a button while its request runs, so a slow provider can't be submitted twice.
export async function whileBusy(button, busyText, task) {
  const content = button.innerHTML; // icons included
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = busyText;
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.innerHTML = content;
  }
}

// A change asked about first, in a modal: `title` (text), `body` (trusted HTML: escape what it
// holds), Cancel (first, focused) and the `action` button (`danger` for what takes something
// away). The action runs `run(form)`: its result closes the dialog and is what the promise gives
// (true for an answer without a body); a refusal is said inside the dialog (nothing under a
// modal is heard) and leaves it open. Cancel or Escape gives null. Leaving the view closes it,
// unless its request is running (`data-busy`): then it closes with the answer.
let dialogs = 0;

export function actionDialog({ title, body, action, danger = false, run }) {
  return new Promise((resolve) => {
    const id = `action-dialog-${++dialogs}`;
    const dialog = document.createElement("dialog");
    dialog.className = "confirm";
    dialog.dataset.admin = "";
    dialog.setAttribute("aria-labelledby", `${id}-title`);
    dialog.innerHTML = `<form method="dialog" novalidate>
      <h2 id="${id}-title">${esc(title)}</h2>
      ${body}
      <p class="error" role="alert" hidden></p>
      <div class="row mt-4"><span class="grow"></span>
        <button type="button" value="cancel" autofocus>Cancel</button>
        <button type="submit" class="${danger ? "danger" : "primary"}" data-act>${esc(action)}</button>
      </div>
    </form>`;
    document.body.append(dialog);
    const form = dialog.querySelector("form");
    const error = dialog.querySelector(".error");
    const act = dialog.querySelector("[data-act]");
    const cancel = dialog.querySelector('button[value="cancel"]');
    let busy = false;
    let result = null;
    cancel.addEventListener("click", () => { if (!busy) dialog.close("cancel"); });
    dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); }); // not mid-request
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(result);
    });
    // `data-busy` while its request runs: leaving the view leaves it open until the answer, so what
    // follows the answer (a page reload after changing your own roles) still happens.
    // Submit handler (not only button click) so pressing Enter inside a modal input submits instead of cancelling.
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      busy = true;
      dialog.dataset.busy = "";
      cancel.disabled = true;
      try {
        const answer = await whileBusy(act, `${action}…`, () => run(form));
        result = answer ?? true;
        dialog.close();
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      } finally {
        busy = false;
        delete dialog.dataset.busy;
        cancel.disabled = false;
      }
    });
    dialog.showModal();
  });
}

// Which pack, how risky, and what is deployed, in words; the exact identifiers stay one line below
// (decision 1 in notes/ui-ux-review.md: plain words first, codes one step away).
export const slotText = (slot) => {
  const s = String(slot ?? "");
  return s ? `${s[0].toUpperCase()}${s.slice(1).replaceAll("_", " ")}` : "";
};

// An empty view or section: an icon, what is (not) here in one line, and the one thing to do next.
// `text` is trusted HTML (it may link somewhere).
export function emptyState({ icon: name, title, text, action = null, level = 2 }) {
  return `<div class="empty-state">
    <span class="empty-icon" aria-hidden="true">${icon(name)}</span>
    <h${level}>${esc(title)}</h${level}>
    <p class="muted">${text}</p>
    ${action ? `<a class="button primary" href="${esc(action.href)}">${esc(action.label)}</a>` : ""}
  </div>`;
}

// `trend`: a sparkline beside the value (decorative); `change`: how it moved, in words.
export function stat({ label, value, foot = "", tone = "", html = false, size = "", trend = "", change = "" }) {
  return `<div class="stat ${size} ${tone ? `tone-${tone}` : ""}">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${html ? value : esc(value)}${trend}</div>
    ${foot ? `<div class="stat-foot">${foot}</div>` : ""}${change}
  </div>`;
}

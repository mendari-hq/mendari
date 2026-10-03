// Shared helpers: formatting, badges and icons, the API, messages, local storage, the app state,
// focus and busy states, and the stat block.

export const $ = (selector, root = document) => root.querySelector(selector);

export const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export const pct = (v, d = 1) => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(d)}%`);

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

// Plain words first; the exact code stays one hover away (docs/ui-ux-review.md, decision 1).
const OUTCOME_TEXT = { AUTO: "Automated", HUMAN_REVIEW: "Human review", ABSTAIN: "Abstained", BLOCK: "Blocked" };

export const PRIORITY_TEXT = { stat: "Stat", asap: "ASAP", urgent: "Urgent", routine: "Routine" };

export const outcomeText = (o) => OUTCOME_TEXT[o] ?? o;

export const outcomeBadge = (o) => `<span class="badge o-${esc(o)}" title="${esc(o)}">${esc(outcomeText(o))}</span>`;

export const priorityBadge = (p) => `<span class="badge p-${esc(p)}" title="priority: ${esc(p)}">${esc(PRIORITY_TEXT[p] ?? p)}</span>`;

// Icons come from the sprite in index.html; they are decorative, so the text beside them names them.
export const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

export async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  const body = await response.json().catch(() => null);
  if (response.status === 401 && signedIn()) throw sessionEnded();
  if (!response.ok) throw new Error((body && (body.detail || body.error)) || response.statusText);
  return body;
}

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

export const state = {
  packs: [], packId: null, detail: null, view: null, smart: null, me: null, phiMode: "off",
  timezone: DEFAULT_TIMEZONE, timeFormat: timeFormatFor(DEFAULT_TIMEZONE), shortFormat: shortFormatFor(DEFAULT_TIMEZONE),
};

// What the signed-in person may do ("decide", "review", "evaluate", "approve"...; open mode:
// everything). The server enforces the same table; this only hides what would be refused.
export const may = (permission) => !state.me || state.me.permissions.includes(permission);
export const signedIn = () => Boolean(state.me && state.me.kind !== "open");

export const packPath = () => `/api/packs/${state.packId}`;

// A pack chosen while another was still loading wins: the older answer is dropped when it arrives.
export async function loadPack(packId) {
  state.packId = packId;
  const detail = await api(`/api/packs/${packId}`);
  if (state.packId === packId) state.detail = detail;
}

export function watchEdges(el) {
  const edges = () => {
    el.classList.toggle("scrolled", el.scrollLeft > 0);
    el.classList.toggle("more-right", el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
  };
  el.addEventListener("scroll", edges, { passive: true });
  new ResizeObserver(edges).observe(el);
  edges();
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

// Which pack, how risky, and what is deployed, in words; the exact identifiers stay one line below
// (decision 1 in docs/ui-ux-review.md: plain words first, codes one step away).
export const slotText = (slot) => `${slot[0].toUpperCase()}${slot.slice(1).replaceAll("_", " ")}`;

// A stat: a label, a value, and what the value is out of. `html` values are trusted markup.
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

// SPDX-License-Identifier: Apache-2.0
// The invitation page (/invite, invite.html; memberships mode, ADR-0008). invite.html's first
// script took the token from the link's fragment, cleared the address and kept the token in this
// tab's sessionStorage. Here: who is signed in, then, on a press only, accepting. Signing in
// comes back here (`next=/invite`, a literal: never this page's address), and the token waits in
// the tab. It goes nowhere but the accept request's body: never into the page, an attribute, a
// URL, a log or another storage, and it is forgotten once the answer about it is final (joined,
// not valid, already a member, malformed, or invitations not used here). This page never uses
// the dashboard's `api()`: that names an organization, and its session banner copies the address.
//
// A link opened again in this tab changes only the address's fragment, which loads no new page:
// invite.html's script takes it the same way and says so ("invite-link"), and the page starts
// over with it. An answer that arrives for the flow before is dropped, and Accept always sends
// the token the tab holds when it is pressed.

import { setupTheme } from "./theme.js";
import { announce, esc, focusHeading, OWN_REQUEST, refusalText, roleName, storageSet, whileBusy } from "./ui.js";

const TOKEN = "oh.invite";
const SIGN_IN = "/auth/login?next=%2Finvite";
const OTHER_ACCOUNT = "/auth/login?account=choose&next=%2Finvite";
const LAST_CHOSEN = "oh.org"; // the organization last chosen in this browser (access.js)

const heading = document.getElementById("invite-title");
const body = document.getElementById("invite-body");
const actions = document.getElementById("invite-actions");

let current = 0; // the flow on screen: each start is a new one

setupTheme();

function held() {
  try { return sessionStorage.getItem(TOKEN); } catch { return null; }
}

// Forgets `token` only: never one a link opened since put in its place.
function forget(token) {
  try {
    if (sessionStorage.getItem(TOKEN) === token) sessionStorage.removeItem(TOKEN);
  } catch { /* nothing was kept */ }
}

// Same-origin JSON, with Mendari's own header (`OWN_REQUEST`, ui.js) and never the dashboard's
// organization header. Its answer, whatever it is.
async function send(path, payload) {
  const options = { cache: "no-store", credentials: "same-origin", headers: { ...OWN_REQUEST } };
  if (payload !== undefined) {
    Object.assign(options, { method: "POST", body: JSON.stringify(payload) });
    options.headers["content-type"] = "application/json";
  }
  const response = await fetch(path, options);
  const answer = await response.json().catch(() => null);
  return { status: response.status, body: answer, retryAfter: response.headers.get("retry-after") };
}

// One state of the page: its heading (focused, and announced: a refusal at once), what it says
// (trusted HTML: every name and address in it is escaped) and what can be done.
function show({ title, html, buttons = [], alert = false }) {
  heading.textContent = title;
  body.innerHTML = html;
  actions.replaceChildren(...buttons.map(control));
  focusHeading(heading, { scroll: false });
  announce(`${title}. ${body.textContent.trim()}`, { error: alert });
}

// While the page finds out who is signed in: nothing to press (a button left from the state before
// would act on it), and nothing announced or focused yet.
function opening() {
  heading.textContent = "Invitation";
  body.innerHTML = "<p>Opening your invitation…</p>";
  actions.replaceChildren();
}

// A button runs `run` (busy while it does); a link goes to `href`. Labels are text, never markup.
function control({ label, run, href, primary = false }) {
  const element = document.createElement(href ? "a" : "button");
  element.textContent = label;
  if (primary) element.classList.add("primary");
  if (href) {
    element.classList.add("button");
    element.href = href;
  } else {
    element.type = "button";
    element.addEventListener("click", () => whileBusy(element, `${label}…`, run));
  }
  return element;
}

const signInButton = (primary = true) => ({ label: "Sign in", primary, run: () => location.assign(SIGN_IN) });

// Signing out keeps the token in the tab; signing in again comes back here.
const otherAccount = (primary = false) => ({
  label: "Use another account", primary,
  run: async () => {
    await send("/auth/logout", {}).catch(() => null);
    location.assign(OTHER_ACCOUNT);
  },
});

const signOut = { label: "Sign out", run: async () => { await send("/auth/logout", {}).catch(() => null); return start(); } };
const tryAgain = { label: "Try again", primary: true, run: () => start() };
const openMendari = { label: "Open Mendari", href: "/", primary: true };

const failed = (detail) => show({
  title: "Something went wrong",
  html: `<p>${esc(detail || "Mendari couldn't be reached.")} Your invitation is still open.</p>`,
  buttons: [tryAgain],
  alert: true,
});

async function start() {
  const run = ++current;
  const why = document.documentElement.dataset.invite;
  if (why === "unstored") {
    return show({ title: "This browser keeps no data for this site", html: "<p>Allow site data for this site, then open the link again.</p>", alert: true });
  }
  if (why === "damaged") return damaged(); // invite.html dropped the token already
  const token = held();
  if (!token) return show({ title: "No invitation in this tab", html: "<p>Open the link you were sent again, in this tab.</p>" });
  opening();
  let me;
  try {
    me = await send("/api/auth/me");
  } catch {
    return run === current && failed();
  }
  if (run !== current) return; // another link was opened meanwhile: its flow shows
  if (me.status !== 200 || !me.body) return failed(refusalText(me.body));
  if (me.body.accounts !== "memberships") { // a deployment that changed its mode since the link was made
    forget(token);
    return show({ title: "Invitations aren't used here", html: "<p>Ask whoever invited you how to sign in to this site.</p>", alert: true });
  }
  if (!me.body.signed_in || me.body.principal?.kind !== "user") return signedOut();
  return ready(me.body, run);
}

function damaged() {
  return show({
    title: "This link is incomplete",
    html: "<p>Copy the whole link, including everything after #, and open it again.</p>",
    alert: true,
  });
}

function signedOut() {
  return show({
    title: "Sign in to accept your invitation",
    html: "<p>Sign in with the e-mail address it was sent to; you'll come back here.</p>",
    buttons: [signInButton()],
  });
}

function ready(me, run) {
  const { name, actor } = me.principal;
  return show({
    title: "Accept your invitation",
    html: `<p>Signed in as <b>${esc(name)}</b> (${esc(actor)}). Accepting adds you to the organization it is for, with the roles it gives.</p>`,
    buttons: [{ label: "Accept invitation", primary: true, run: () => accept(me, run) }, otherAccount()],
  });
}

async function accept(me, run) {
  const token = held(); // the one this tab holds now
  if (!token) return start();
  let answer;
  try {
    answer = await send("/api/auth/invitations/accept", { token });
  } catch {
    return run === current && failed();
  }
  const { status, body: said } = answer;
  const refusal = said?.error === "InvitationRefused";
  if (status === 200 || status === 422 || (refusal && (status === 404 || status === 409))) {
    forget(token); // a final answer about this token, whatever is on screen now
  }
  if (run !== current) return; // another link was opened meanwhile: its flow shows
  if (status === 200) return joined(me, said, run);
  if (status === 401) return signedOut();
  if (status === 403 && refusal && said.reason === "other_address") {
    return show({
      title: "This invitation is for another e-mail address",
      html: `<p>You're signed in as ${esc(me.principal.actor)}. Sign in with the address it was sent to.</p>`,
      buttons: [otherAccount(true), signOut],
      alert: true,
    });
  }
  if (status === 403 && refusal) {
    return show({
      title: "Your address isn't verified",
      html: "<p>Sign in with an account whose identity provider verifies the address it was sent to.</p>",
      buttons: [otherAccount(true)],
      alert: true,
    });
  }
  if (status === 404 && refusal) {
    return show({
      title: "This invitation isn't valid",
      html: "<p>It was used, revoked or has expired. Ask whoever invited you for a new link.</p>",
      alert: true,
    });
  }
  if (status === 409 && refusal) {
    return show({
      title: "You're already a member",
      html: "<p>Its administrators change roles.</p>",
      buttons: [openMendari],
      alert: true,
    });
  }
  if (status === 422) return damaged();
  if (status === 429) {
    const wait = Number(answer.retryAfter);
    return show({
      title: "Too many tries",
      html: `<p>${Number.isFinite(wait) && wait > 0 ? `Try again in ${wait} seconds.` : "Try again in a minute."}</p>`,
      buttons: [tryAgain],
      alert: true,
    });
  }
  return failed(refusalText(said));
}

function joined(me, made, run) {
  const org = made.name || made.org;
  const roles = (made.roles ?? []).map(roleName).join(", ");
  if (made.selected) {
    return show({
      title: `You've joined ${org}`,
      html: `<p>Your roles: ${esc(roles)}.</p>`,
      buttons: [openMendari],
    });
  }
  const now = me.memberships?.find((m) => m.org === me.principal.org)?.name ?? me.principal.org;
  return show({
    title: `You've joined ${org}`,
    html: `<p>Your roles: ${esc(roles)}. This browser still works in ${esc(now)}.</p>`,
    buttons: [
      { label: `Switch to ${org}`, primary: true, run: () => switchTo(made, run) },
      { label: `Stay in ${now}`, href: "/" },
    ],
  });
}

// Act for the organization just joined, in this browser, then open the dashboard there.
async function switchTo(made, run) {
  let answer;
  try {
    answer = await send("/api/auth/organization", { org: made.org });
  } catch {
    answer = null;
  }
  if (run !== current) return; // another link was opened meanwhile: its flow shows
  if (answer?.status === 200) {
    storageSet(LAST_CHOSEN, made.org);
    location.assign("/");
    return;
  }
  const why = answer ? refusalText(answer.body) : "Mendari couldn't be reached.";
  show({
    title: "Switching didn't work",
    html: `<p>You're a member of ${esc(made.name || made.org)} now. ${esc(why)} Open Mendari and choose it in the top bar.</p>`,
    buttons: [openMendari],
    alert: true,
  });
}

// invite.html's first script took a link opened again in this tab (a fragment change).
document.addEventListener("invite-link", () => start());

start();

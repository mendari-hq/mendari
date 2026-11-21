// SPDX-License-Identifier: Apache-2.0
// The pack registry served under this engine (MENDARI_REGISTRY=on, LP-19f, ADR-0015), in the
// dashboard. Two tabs in the Access view (admin.js), for an administrator who signed in (`keys`)
// where the server says the registry is on (`/api/health`): their organization's namespaces (claim
// one, see whether the registry lists it, register a publisher key) and its publisher tokens (mint
// one, shown once in the one-time dialog; revoke). And the registry's review screen
// (`#registry-review`), for a person who holds `registry_review` in the registry's review
// organization (`/api/auth/me` says `registry_review_org`): the namespace claims waiting, each
// version with what the registry checked beside what its publisher claims, the card the registry
// rendered as plain text, and Approve or Refuse with a one-line note; and the evaluating sites
// waiting (LP-22i), each verified or refused with a note. A third tab, Sites, registers this
// organization's sites and adds or revokes their keys from the key file `mendari site init`
// writes (a site's private key is never typed here).
// Namespaces, names, notes, ids and cards are what people typed or a publisher uploaded: always
// escaped, and a card is only ever text (`textContent`), never markup.

import { adminKit, extendAdmin } from "./admin.js";
import { views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, actionDialog, api, emptyState, esc, figureHtml, focusHeading, may, signedIn, state, time, toast, toastError, whileBusy } from "./ui.js";

const { lists, reload, showOnce, actionButton, ofOrg } = adminKit;

// The registry's limits (hub/registry.py, server/registry_routes.py), so the forms say them.
const NAME_PATTERN = "[a-z0-9]+(-[a-z0-9]+)*";
const MAX_NAMESPACE = 63;
const MAX_TOKEN_NAME = 80;
const TOKEN_DAYS = 90;
const MAX_TOKEN_DAYS = 365;
const MAX_NOTE = 300; // hub/yank.py MAX_REASON

// An administrator who signed in, where the registry is served here.
const accounts = () => state.registry && signedIn() && may("keys");

extendAdmin({
  before: "usage",
  tabs: [
    { key: "namespaces", label: "Namespaces", shown: accounts, load: () => api("/api/registry/namespaces"), show: showNamespaces },
    { key: "publisher-tokens", label: "Publisher tokens", shown: accounts, load: () => api("/api/registry/tokens"), show: showTokens },
    { key: "sites", label: "Sites", shown: accounts, load: () => api("/api/registry/sites"), show: showSites },
  ],
  actions: {
    "add-key": ["namespaces", addKey],
    "revoke-token": ["publisher-tokens", revokeToken],
    "add-site-key": ["sites", addSiteKey],
    "revoke-site-key": ["sites", revokeSiteKey],
  },
  forms: { "claim-form": claim, "mint-form": mint, "site-form": registerSite },
});

// Who made or claimed something, by their id: you, a member by name, else the id one hover away
// (a member since removed, or claims mode, where the dashboard has no list of people).
function person(id) {
  if (!id) return "";
  if (id === lists.you) return "You";
  const name = lists.names.get(id);
  if (name) return esc(name);
  const who = state.accounts === "memberships" ? "Former member" : "Someone who signed in";
  return `<span title="${esc(id)}">${who}</span>`;
}

const badge = (text, tone, code) => `<span class="badge ${tone}" title="${esc(code)}">${esc(text)}</span>`;

// -- namespaces ---------------------------------------------------------------------------------

// A claim's status in words (the code one hover away).
const CLAIM_STATUS = {
  pending: ["Waiting for the registry's review", "s-warning"],
  approved: ["Listed", "s-ok"],
  refused: ["Refused", "o-ABSTAIN"],
};

const claimBadge = (status) => badge(...(CLAIM_STATUS[status] ?? [status, ""]), status);

// What a pending claim waits for, in one line.
function waitsFor(c) {
  if (c.status === "refused") return c.note ? `Refused: ${esc(c.note)}` : "Refused by the registry's reviewers.";
  if (c.status === "approved") return "Served to everyone: search, pull and the Hub.";
  return "Nothing of it is served until a reviewer approves its first published version.";
}

function showNamespaces() {
  const form = `<form class="panel admin-form" id="claim-form" novalidate>
      <h2>Claim a namespace</h2>
      <p class="muted">The first part of a pack's id (<code>namespace/pack</code>). Nothing published in it is served to anyone until the registry's reviewers approve its first version.</p>
      <label for="claim-name">Name</label>
      <input id="claim-name" name="name" required maxlength="${MAX_NAMESPACE}" pattern="${NAME_PATTERN}" autocomplete="off" spellcheck="false" class="field-full mono" aria-describedby="claim-name-hint">
      <p class="muted" id="claim-name-hint">Lowercase letters, digits and dashes. <code>mendari</code> is reserved.</p>
      <label for="claim-display">Display name (optional)</label>
      <input id="claim-display" name="display_name" maxlength="200" autocomplete="off" class="field-full">
      <p class="error" id="claim-error" role="alert" hidden></p>
      <div class="row mt-4"><button type="submit" class="primary">Claim</button></div>
    </form>`;
  const claims = lists.namespaces ?? [];
  if (!claims.length) {
    return form + emptyState({ icon: "inbox", title: "No namespaces yet", text: "Claim one with the form above, then mint a publisher token for it." });
  }
  return form + tableHtml({
    id: "namespaces",
    caption: `Namespaces${ofOrg()}, oldest first`,
    columns: [
      {
        key: "namespace", label: "Namespace", sort: (c) => c.namespace,
        cell: (c) => `<code>${esc(c.namespace)}</code>${c.display_name && c.display_name !== c.namespace ? `<br><span class="muted">${esc(c.display_name)}</span>` : ""}`,
      },
      { key: "status", label: "Status", sort: (c) => c.status, cell: (c) => `${claimBadge(c.status)}<br><span class="muted">${waitsFor(c)}</span>` },
      { key: "claimed", label: "Claimed", sort: (c) => c.created_at, cell: (c) => `${esc(time(c.created_at))}<br><span class="muted">by ${person(c.claimed_by)}</span>` },
      {
        key: "keys", label: "Publisher keys",
        cell: (c) => (c.keys.length ? c.keys.map((k) => `<code class="wrap">${esc(k.key_id)}</code>`).join("<br>") : '<span class="muted">None yet</span>'),
      },
      {
        key: "actions", label: "Actions", className: "actions",
        cell: (c) => (c.status === "refused" ? "" : actionButton("add-key", c.namespace, `Add key to ${c.namespace}`, "Add key…")),
      },
    ],
    rows: claims,
  });
}

// A refusal of a claim, in words: a reserved name plainly; anything else as the server says it.
function claimRefusal(error, name) {
  if (error.status === 422 && /reserved/.test(error.message)) {
    return `“${name}” is reserved for Mendari's own packs. Choose another name.`;
  }
  if (error.status === 409 && /exists/.test(error.message)) return `“${name}” is taken. Choose another name.`;
  return error.message;
}

async function claim(event) {
  event.preventDefault();
  const form = event.target;
  const error = form.querySelector("#claim-error");
  error.hidden = true;
  const input = form.querySelector("#claim-name");
  input.value = input.value.trim();
  input.setCustomValidity(input.validity.patternMismatch ? "Lowercase letters, digits and dashes only, starting and ending with a letter or digit." : "");
  if (!form.reportValidity()) return;
  const name = input.value;
  const displayName = form.querySelector("#claim-display").value.trim();
  try {
    await whileBusy(form.querySelector('button[type="submit"]'), "Claiming…", () =>
      api("/api/registry/namespaces", { method: "POST", body: JSON.stringify({ name, display_name: displayName }) }));
  } catch (failure) {
    if (!form.isConnected) throw failure; // said as a toast: the form left the page
    error.textContent = claimRefusal(failure, name);
    error.hidden = false;
    input.focus();
    return;
  }
  toast(`${name} is claimed. It is waiting for the registry's review.`);
  if (form.isConnected) form.reset();
  await reload("namespaces");
}

async function addKey(namespace) {
  const added = await actionDialog({
    title: `Add a publisher key to ${namespace.namespace}`,
    body: `<p>The Ed25519 public key (PEM) that signs this namespace's archives. Its private key stays with whoever publishes.</p>
      <label for="key-pem">Public key</label>
      <textarea id="key-pem" class="mono field-full" rows="5" required spellcheck="false" placeholder="-----BEGIN PUBLIC KEY-----"></textarea>`,
    action: "Add key",
    run: (form) => {
      const pem = form.querySelector("#key-pem").value.trim();
      if (!pem) throw new Error("Paste the public key.");
      return api(`/api/registry/namespaces/${encodeURIComponent(namespace.namespace)}/keys`, {
        method: "POST", body: JSON.stringify({ public_pem: pem }),
      });
    },
  });
  if (!added) return;
  toast(`Key ${added.key_id} is added to ${namespace.namespace}.`);
  await reload("namespaces");
}

// -- publisher tokens ---------------------------------------------------------------------------

const TOKEN_STATUS = {
  active: ["Active", "s-ok"],
  expired: ["Expired", "o-ABSTAIN"],
  revoked: ["Revoked", "o-ABSTAIN"],
};

const tokenStatus = (t) => (t.revoked_at ? "revoked" : new Date(t.expires_at) <= new Date() ? "expired" : "active");
const tokenBadge = (t) => badge(...TOKEN_STATUS[tokenStatus(t)], tokenStatus(t));

// The namespaces a token can be minted for: this organization's, not refused.
const mintable = () => (lists.namespaces ?? []).filter((c) => c.status !== "refused");

function showTokens() {
  const spaces = mintable();
  const form = spaces.length
    ? `<form class="panel admin-form" id="mint-form" novalidate>
      <h2>Mint a publisher token</h2>
      <p class="muted">For whatever publishes packs (a CI job, say): it publishes to one namespace only, at <code>/registry/v1/</code>, and is no credential anywhere else. It is shown once.</p>
      <label for="mint-name">Name</label>
      <input id="mint-name" name="name" required maxlength="${MAX_TOKEN_NAME}" autocomplete="off" class="field-full">
      <label for="mint-namespace">Namespace</label>
      <select id="mint-namespace" class="field-full">${spaces.map((c) => `<option value="${esc(c.namespace)}">${esc(c.namespace)}</option>`).join("")}</select>
      <label for="mint-days">Expires after (days)</label>
      <input id="mint-days" name="days" type="number" min="1" max="${MAX_TOKEN_DAYS}" value="${TOKEN_DAYS}" required>
      <div class="row mt-4"><button type="submit" class="primary">Mint token</button></div>
    </form>`
    : emptyState({ icon: "inbox", title: "No namespace to publish to", text: 'Claim a namespace first, on the <a href="#admin/namespaces">Namespaces</a> tab.' });
  const tokens = lists["publisher-tokens"] ?? [];
  if (!tokens.length) {
    return form + (spaces.length ? emptyState({ icon: "inbox", title: "No publisher tokens yet", text: "Mint one with the form above." }) : "");
  }
  return form + tableHtml({
    id: "publisher-tokens",
    caption: `Publisher tokens${ofOrg()}, oldest first`,
    columns: [
      { key: "name", label: "Name", cell: (t) => esc(t.name), sort: (t) => t.name.toLowerCase() },
      { key: "namespace", label: "Namespace", cell: (t) => `<code>${esc(t.namespace)}</code>`, sort: (t) => t.namespace },
      { key: "made", label: "Made by", cell: (t) => person(t.owner) },
      { key: "created", label: "Created", className: "nowrap", cell: (t) => esc(time(t.created_at)), sort: (t) => t.created_at },
      { key: "expires", label: "Expires", className: "nowrap", cell: (t) => esc(time(t.expires_at)), sort: (t) => t.expires_at },
      { key: "used", label: "Last used", className: "nowrap", cell: (t) => (t.last_used_day ? esc(t.last_used_day) : '<span class="muted">Never</span>'), sort: (t) => t.last_used_day ?? "" },
      {
        key: "status", label: "Status", sort: tokenStatus,
        cell: (t) => `${tokenBadge(t)}${t.revoked_at ? `<br><span class="muted">${esc(time(t.revoked_at))}, by ${person(t.revoked_by)}</span>` : ""}`,
      },
      {
        key: "actions", label: "Actions", className: "actions",
        cell: (t) => (t.revoked_at ? "" : actionButton("revoke-token", t.id, `Revoke ${t.name}`, "Revoke")),
      },
    ],
    rows: tokens,
  });
}

async function mint(event) {
  event.preventDefault();
  const form = event.target;
  const nameInput = form.querySelector("#mint-name");
  nameInput.value = nameInput.value.trim();
  if (!form.reportValidity()) return;
  const body = {
    name: nameInput.value,
    namespace: form.querySelector("#mint-namespace").value,
    days: Number(form.querySelector("#mint-days").value),
  };
  const made = await whileBusy(form.querySelector('button[type="submit"]'), "Minting…", () =>
    api("/api/registry/tokens", { method: "POST", body: JSON.stringify(body) }));
  // The token first, whatever became of the form meanwhile: it is shown once, and a token minted
  // and never shown is lost.
  showOnce({
    id: "publisher-token",
    input: "publisher-token-secret",
    title: `Publisher token ${esc(made.name)}`,
    warning: "Copy it now: the registry keeps only a digest and can't show it again. A lost token is revoked and another minted.",
    label: "Token",
    value: made.token,
    details: `Publishes to <code>${esc(made.namespace)}</code> only, sent as <code>Authorization: Bearer</code> to <code>/registry/v1/</code>, until ${esc(time(made.expires_at))}.`,
    copy: "Copy token",
  });
  if (form.isConnected) form.reset();
  await reload("publisher-tokens");
}

async function revokeToken(token) {
  const revoked = await actionDialog({
    title: `Revoke ${token.name}?`,
    body: `<p>The registry refuses it from its next request: nothing more is published to <code>${esc(token.namespace)}</code> with it. This can't be undone; mint another if you need one.</p>`,
    action: "Revoke",
    danger: true,
    run: () => api(`/api/registry/tokens/${encodeURIComponent(token.id)}`, { method: "DELETE" }).catch((error) => {
      if (error.status === 404) return "gone";
      throw error;
    }),
  });
  if (!revoked) return;
  toast(revoked === "gone" ? "It is gone: revoked or removed meanwhile." : `${token.name} is revoked.`);
  await reload("publisher-tokens");
}

// -- sites (LP-22i) -----------------------------------------------------------------------------
// An evaluating site of this organization (LP-22, ADR-0017): registered here, verified by the
// registry's reviewers, signing with keys its own machine made (`mendari site init`). A site's
// private key is never typed or made on this page: it asks for the key file that command wrote.

const MAX_SITE_NAME = 120;

const SITE_STATUS = {
  pending: ["Waiting for the registry's review", "s-warning"],
  verified: ["Verified", "s-ok"],
  refused: ["Refused", "o-ABSTAIN"],
  closed: ["Closed", "o-ABSTAIN"],
};

const siteBadge = (status) => badge(...(SITE_STATUS[status] ?? [status, ""]), status);

function siteWaits(site) {
  if (site.status === "pending") return "It gets no ticket and shares nothing until a reviewer verifies it.";
  if (site.status === "verified") return "It may ask for tickets and share signed counts.";
  if (site.status === "refused") return site.note ? `Refused: ${esc(site.note)}` : "Refused by the registry's reviewers.";
  return "Closed: it shares nothing again; what it shared stays.";
}

function siteKeys(site) {
  if (!site.keys.length) return '<span class="muted">None yet</span>';
  return site.keys.map((k) => {
    const state = k.revoked_at
      ? `<span class="muted">revoked ${esc(time(k.revoked_at))}${k.compromised ? ", as compromised: its reports leave the pooled figures" : ""}</span>`
      : `<span class="muted">added ${esc(time(k.created_at))}</span> <button type="button" data-action="revoke-site-key" data-id="${esc(site.id)}" data-key="${esc(k.key_id)}" aria-label="${esc(`Revoke key ${k.key_id} of ${site.name}`)}">Revoke…</button>`;
    return `<code class="wrap">${esc(k.key_id)}</code> ${state}`;
  }).join("<br>");
}

function showSites() {
  const form = `<form class="panel admin-form" id="site-form" novalidate>
      <h2>Register a site</h2>
      <p class="muted">A place that evaluates packs on its own chart-reviewed cases and shares signed counts, never cases. It is shown by this name once the registry's reviewers verify it.</p>
      <label for="site-name">Name</label>
      <input id="site-name" name="name" required maxlength="${MAX_SITE_NAME}" autocomplete="off" class="field-full">
      <p class="error" id="site-error" role="alert" hidden></p>
      <div class="row mt-4"><button type="submit" class="primary">Register</button></div>
    </form>`;
  const sites = lists.sites ?? [];
  if (!sites.length) {
    return form + emptyState({ icon: "inbox", title: "No sites yet", text: "Register one with the form above, then add the key file its machine makes with <code>mendari site init</code>." });
  }
  return form + tableHtml({
    id: "sites",
    caption: `Sites${ofOrg()}, oldest first`,
    columns: [
      { key: "name", label: "Site", sort: (x) => x.name.toLowerCase(), cell: (x) => `${esc(x.name)}<br><code class="muted">${esc(x.id)}</code>` },
      { key: "status", label: "Status", sort: (x) => x.status, cell: (x) => `${siteBadge(x.status)}<br><span class="muted">${siteWaits(x)}</span>` },
      { key: "registered", label: "Registered", sort: (x) => x.created_at, cell: (x) => `${esc(time(x.created_at))}<br><span class="muted">by ${person(x.created_by)}</span>` },
      { key: "keys", label: "Keys", cell: siteKeys },
      {
        key: "actions", label: "Actions", className: "actions",
        cell: (x) => (["refused", "closed"].includes(x.status) ? "" : actionButton("add-site-key", x.id, `Add key to ${x.name}`, "Add key…")),
      },
    ],
    rows: sites,
  });
}

async function registerSite(event) {
  event.preventDefault();
  const form = event.target;
  const error = form.querySelector("#site-error");
  error.hidden = true;
  const input = form.querySelector("#site-name");
  input.value = input.value.trim();
  if (!form.reportValidity()) return;
  try {
    await whileBusy(form.querySelector('button[type="submit"]'), "Registering…", () =>
      api("/api/registry/sites", { method: "POST", body: JSON.stringify({ name: input.value }) }));
  } catch (failure) {
    if (!form.isConnected) throw failure;
    error.textContent = failure.message;
    error.hidden = false;
    input.focus();
    return;
  }
  toast(`${input.value} is registered. It is waiting for the registry's review.`);
  if (form.isConnected) form.reset();
  await reload("sites");
}

async function addSiteKey(site) {
  const added = await actionDialog({
    title: `Add a key to ${site.name}`,
    body: `<p>Paste the key file <code>mendari site init --site ${esc(site.id)}</code> wrote on the site's machine (<code>site-key.json</code>): its public key and the proof that the machine holds the private one. The private key never leaves that machine.</p>
      <label for="site-key-file">Key file</label>
      <textarea id="site-key-file" class="mono field-full" rows="8" spellcheck="false" placeholder='{"format": "mendari.site-key/1", ...}'></textarea>`,
    action: "Add key",
    run: (form) => {
      let file;
      try {
        file = JSON.parse(form.querySelector("#site-key-file").value);
      } catch {
        throw new Error("Paste the whole key file: it is JSON.");
      }
      if (typeof file?.public_pem !== "string" || typeof file?.proof !== "string") {
        throw new Error("This is not a site key file: it has no public key and proof.");
      }
      if (file.site && file.site !== site.id) throw new Error("This key file was made for another site.");
      return api(`/api/registry/sites/${encodeURIComponent(site.id)}/keys`, {
        method: "POST", body: JSON.stringify({ public_pem: file.public_pem, proof: file.proof }),
      });
    },
  });
  if (!added) return;
  toast(`A key is added to ${site.name}.`);
  await reload("sites");
}

async function revokeSiteKey(site, button) {
  const keyId = button.dataset.key;
  const revoked = await actionDialog({
    title: `Revoke key ${keyId}?`,
    body: `<p>${esc(site.name)} can no longer sign with it, from now. This can't be undone; add another key if the site needs one.</p>
      <label for="site-key-reason">Why (one line)</label>
      <input id="site-key-reason" class="field-full" maxlength="${MAX_NOTE}" autocomplete="off">
      <fieldset class="role-choices mt-3"><legend>Compromised?</legend><label>
        <input type="checkbox" id="site-key-compromised">
        <span>The key may have been copied: take every report it signed out of the pooled figures</span>
      </label></fieldset>`,
    action: "Revoke",
    danger: true,
    run: (form) => {
      const reason = form.querySelector("#site-key-reason").value.trim();
      if (!reason) throw new Error("Say why, in one line.");
      const compromised = form.querySelector("#site-key-compromised").checked;
      return api(`/api/registry/sites/${encodeURIComponent(site.id)}/keys/${encodeURIComponent(keyId)}`, {
        method: "DELETE", body: JSON.stringify({ reason, compromised }),
      });
    },
  });
  if (!revoked) return;
  toast(`Key ${keyId} is revoked.`);
  await reload("sites");
}

// -- the registry's review ----------------------------------------------------------------------

const reviewer = () => signedIn() && state.me?.registry_review_org === true && may("registry_review");

const PUBLISHER_TYPE = { community: "Community", verified: "Verified", institutional: "Institutional" };

let queue = { pending: [], more: 0 };
let siteQueue = { pending: [], more: 0 };

views["registry-review"] = async (main) => {
  [queue, siteQueue] = await Promise.all([api("/api/registry/review"), api("/api/registry/review/sites")]);
  main.innerHTML = `<h1>Registry review</h1>
    <p class="muted">Namespace claims waiting for a reviewer. Approving lists the namespace: every version it has and will have is served to everyone. What the registry checked stands apart from what the publisher claims. You never decide a claim you filed yourself.</p>
    <div id="registry-queue">${queueHtml()}</div>
    <h2 class="mt-4" id="site-queue-heading">Sites waiting</h2>
    <p class="muted">Evaluating sites an organization registered. Check outside Mendari that the organization is who it says before you verify its site: its name is shown beside its signed counts. You never decide your own organization's site.</p>
    <div id="site-queue">${siteQueueHtml()}</div>`;
  const body = $("#registry-queue");
  body.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-review]");
    if (button) decide(button.dataset.namespace, button.dataset.review).catch((error) => toastError(error.message));
  });
  const sites = $("#site-queue");
  sites.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-site-review]");
    if (button) decideSite(button.dataset.site, button.dataset.name, button.dataset.siteReview).catch((error) => toastError(error.message));
  });
  wire(sites);
  body.addEventListener("toggle", showCard, true); // `toggle` does not bubble
  wire(body);
};

views["registry-review"].available = reviewer;

// The registry's review, not a pack's: shown without waiting for a pack (router.js).
views["registry-review"].packless = true;

views["registry-review"].leave = () => document.querySelectorAll("dialog[data-admin]:not([data-busy])").forEach((d) => d.close());

function siteQueueHtml() {
  if (!siteQueue.pending.length) {
    return emptyState({ icon: "check", title: "No sites waiting", text: "A site shows here once an organization registers one." });
  }
  const more = siteQueue.more ? `<p class="muted mt-3">And ${siteQueue.more} more, older first.</p>` : "";
  return siteQueue.pending.map((x) => {
    const id = `site-${slug(x.id)}`;
    const buttons = x.own
      ? '<p class="callout neutral">Your organization registered this site: another reviewer decides it.</p>'
      : `<div class="row mt-4"><button type="button" class="primary" data-site-review="approve" data-site="${esc(x.id)}" data-name="${esc(x.name)}" aria-label="Verify ${esc(x.name)}">Verify…</button>
        <button type="button" data-site-review="refuse" data-site="${esc(x.id)}" data-name="${esc(x.name)}" aria-label="Refuse ${esc(x.name)}">Refuse…</button></div>`;
    return `<article class="panel review-card registry-site" aria-labelledby="${id}">
      <h3 class="card-title" id="${id}">${esc(x.name)}</h3>
      <dl class="facts mt-3">
        <dt>Organization</dt><dd><code>${esc(x.org)}</code></dd>
        <dt>Registered</dt><dd>${esc(time(x.created_at))}, by <code class="wrap">${esc(x.created_by)}</code></dd>
        <dt>Keys</dt><dd>${x.keys.length ? x.keys.map((k) => `<code class="wrap">${esc(k)}</code>`).join(", ") : "None yet"}</dd>
      </dl>${buttons}
    </article>`;
  }).join("") + more;
}

// Verify or refuse a site, with a one-line note its organization sees.
async function decideSite(siteId, name, decision) {
  const verify = decision === "approve";
  const decided = await actionDialog({
    title: `${verify ? "Verify" : "Refuse"} ${name}?`,
    body: `<p>${verify
      ? "The site may ask for tickets and share signed counts, shown by this name on the Hub. Verify it only once you have checked who it is."
      : "The site gets no ticket and shares nothing. Its organization sees your note."} This is decided once.</p>
      <label for="site-decision-note">How you checked it (one line)</label>
      <input id="site-decision-note" class="field-full" maxlength="${MAX_NOTE}" autocomplete="off">`,
    action: verify ? "Verify" : "Refuse",
    danger: !verify,
    run: (form) => {
      const note = form.querySelector("#site-decision-note").value.trim();
      return api(`/api/registry/review/sites/${encodeURIComponent(siteId)}`, {
        method: "POST", body: JSON.stringify({ decision, note }),
      });
    },
  });
  if (!decided) return;
  toast(verify ? `${name} is verified.` : `${name} is refused.`);
  siteQueue = await api("/api/registry/review/sites");
  const body = $("#site-queue");
  if (!body) return;
  body.innerHTML = siteQueueHtml();
  wire(body);
  focusHeading($("#site-queue-heading"), { scroll: false });
}

function queueHtml() {
  if (!queue.pending.length) {
    return emptyState({ icon: "check", title: "No claims waiting", text: "A claim shows here once an organization claims a namespace." });
  }
  const more = queue.more ? `<p class="muted mt-3">And ${queue.more} more, older first: decide these to see them.</p>` : "";
  return queue.pending.map(claimHtml).join("") + more;
}

const slug = (text) => String(text).replace(/[^a-z0-9-]/g, "-");

function claimHtml(item) {
  const id = `claim-${slug(item.namespace)}`;
  const claimant = item.own ? "you" : `<code class="wrap" title="${esc(item.claimed_by)}">${esc(item.claimed_by)}</code>`;
  const versions = item.versions.length
    ? item.versions.map((v) => versionHtml(item.namespace, v)).join("")
    : '<p class="callout neutral">Nothing published yet. A claim is approved on its first published version; it can be refused now.</p>';
  const moreVersions = item.more_versions ? `<p class="muted">And ${item.more_versions} older ${item.more_versions === 1 ? "version" : "versions"}.</p>` : "";
  const buttons = item.own
    ? '<p class="callout neutral">You claimed this namespace: another reviewer decides it.</p>'
    : `<div class="row mt-4">${item.versions.length ? `<button type="button" class="primary" data-review="approve" data-namespace="${esc(item.namespace)}" aria-label="Approve ${esc(item.namespace)}">Approve…</button>` : ""}
        <button type="button" data-review="refuse" data-namespace="${esc(item.namespace)}" aria-label="Refuse ${esc(item.namespace)}">Refuse…</button></div>`;
  return `<article class="panel review-card registry-claim" aria-labelledby="${id}" data-namespace="${esc(item.namespace)}">
      <h2 class="card-title" id="${id}">${esc(item.display_name || item.namespace)} <code>${esc(item.namespace)}</code></h2>
      <dl class="facts mt-3">
        <dt>Organization</dt><dd><code>${esc(item.org)}</code></dd>
        <dt>Claimed</dt><dd>${esc(time(item.created_at))}, by ${claimant}</dd>
        <dt>Publisher type</dt><dd>${esc(PUBLISHER_TYPE[item.publisher_type] ?? item.publisher_type)}</dd>
        <dt>Publisher keys</dt><dd>${item.keys.length ? item.keys.map((k) => `<code class="wrap">${esc(k)}</code>`).join(", ") : "None"}</dd>
      </dl>
      ${versions}${moreVersions}${buttons}
    </article>`;
}

// A row of a facts list; `value` is trusted HTML.
const fact = (label, value) => `<dt>${esc(label)}</dt><dd>${value}</dd>`;
const code = (value) => (value ? `<code class="wrap">${esc(value)}</code>` : '<span class="muted">Not recorded</span>');

function reviewText(review) {
  if (!review || review.status !== "reviewed") return "Not reviewed";
  const matches = review.matches_declared === null ? "" : review.matches_declared ? ", as declared" : ", not the level declared";
  return `Risk level ${esc(review.risk_level)}${matches}, ${esc(time(review.reviewed_at))}`;
}

function regressionText(regression) {
  if (!regression) return "Not run: published without a test set";
  const parts = [esc(regression.status ?? "")];
  if (regression.accuracy !== undefined) parts.push(`accuracy ${figureHtml(regression.accuracy)}`);
  if (Array.isArray(regression.regressions)) parts.push(`${regression.regressions.length} regressed`);
  if (regression.reason) parts.push(esc(regression.reason));
  return parts.filter(Boolean).join("; ");
}

function checkedHtml(checked) {
  const signature = checked.signature ?? {};
  return [
    fact("Signature", `${esc(signature.algorithm ?? "")} ${signature.verified ? "verified" : "not verified"}, key ${code(signature.key_id)}`),
    fact("Digest", code(checked.digest)),
    fact("Engine", `${code(checked.engine)}${checked.engine_release ? ` <span class="muted">(release ${esc(checked.engine_release)})</span>` : ""}`),
    fact("Card", checked.card === "rendered" ? "Rendered by the registry from the archive" : "Not rendered"),
    fact("Status line", checked.status_label ? esc(checked.status_label) : '<span class="muted">None</span>'),
    fact("Review", reviewText(checked.review)),
    fact("Regression check", regressionText(checked.regression)),
    fact("Counter-signed", checked.accepted ? "Yes" : "Not yet: the registry signs a version when it is listed"),
  ].join("");
}

function claimedHtml(claimed) {
  const publisher = claimed.publisher ?? {};
  const risk = claimed.risk ?? {};
  return [
    fact("Publisher", publisher.name ? `${esc(publisher.name)} <span class="muted">(${esc(publisher.type ?? "")})</span>` : '<span class="muted">Not named</span>'),
    fact("Risk level", risk.level === null || risk.level === undefined ? '<span class="muted">Not declared</span>' : `${esc(risk.level)}${risk.category ? `, ${esc(risk.category)}` : ""}`),
    fact("Licence", claimed.license ? esc(claimed.license) : '<span class="muted">Not named</span>'),
    fact("Summary", claimed.summary ? esc(claimed.summary) : '<span class="muted">None</span>'),
  ].join("");
}

function versionHtml(namespace, v) {
  const mark = v.deleted ? badge("Deleted", "o-ABSTAIN", "deleted") : v.yank ? badge("Recalled", "o-BLOCK", "recalled") : "";
  const path = [namespace, v.name, v.version].map(encodeURIComponent).join("/");
  return `<section class="registry-version mt-4" aria-label="${esc(`${namespace}/${v.name} ${v.version}`)}">
      <h3 class="mt-0"><code>${esc(namespace)}/${esc(v.name)}</code> ${esc(v.version)} ${mark}</h3>
      <p class="muted mt-0">Published ${esc(time(v.published_at))}</p>
      <div class="grid-2">
        <div><h4>What the registry checked</h4><dl class="facts">${checkedHtml(v.checked ?? {})}</dl></div>
        <div><h4>What the publisher claims</h4><dl class="facts">${claimedHtml(v.claimed ?? {})}</dl></div>
      </div>
      <details data-card="${esc(path)}"><summary>The card the registry rendered</summary>
        <pre class="json registry-card" tabindex="0" aria-label="The card of ${esc(`${namespace}/${v.name} ${v.version}`)}"></pre>
      </details>
    </section>`;
}

// A version's card, read when its section is first opened (a card is up to 1 MB): as text only.
async function showCard(event) {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement) || !details.open || !details.dataset.card || details.dataset.loaded) return;
  const pre = details.querySelector("pre");
  details.dataset.loaded = "1";
  pre.textContent = "Loading the card…";
  try {
    const version = await api(`/api/registry/review/packs/${details.dataset.card}`);
    pre.textContent = version.card_markdown ?? "No card: this version was published before the registry rendered its own.";
  } catch (error) {
    delete details.dataset.loaded; // asked again when opened again
    pre.textContent = `Could not load the card: ${error.message}`;
  }
}

// Approve or refuse a claim, with a one-line note the claim's organization sees.
async function decide(namespace, decision) {
  const approve = decision === "approve";
  const decided = await actionDialog({
    title: `${approve ? "Approve" : "Refuse"} ${namespace}?`,
    body: `<p>${approve
      ? "The namespace is listed: every version it has and will have is served to everyone, in search, pulls and the Hub."
      : "The namespace stays taken and is never listed. Its organization sees your note."} This is decided once.</p>
      <label for="decision-note">Note (one line${approve ? ", optional" : ""})</label>
      <input id="decision-note" class="field-full" maxlength="${MAX_NOTE}" autocomplete="off">`,
    action: approve ? "Approve" : "Refuse",
    danger: !approve,
    run: (form) => {
      const note = form.querySelector("#decision-note").value.trim();
      return api(`/api/registry/review/namespaces/${encodeURIComponent(namespace)}`, {
        method: "POST", body: JSON.stringify({ decision, note }),
      });
    },
  });
  if (!decided) return;
  toast(approve ? `${namespace} is approved and listed.` : `${namespace} is refused.`);
  queue = await api("/api/registry/review");
  const body = $("#registry-queue");
  if (!body) return; // the person moved on
  body.innerHTML = queueHtml();
  wire(body);
  focusHeading($("main h1"), { scroll: false });
}

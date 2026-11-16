// SPDX-License-Identifier: Apache-2.0
// The Access view (`#admin/<tab>`, "Access" in the section list; "#access" is the no-access page,
// access.js): an administrator who signed in (`keys`, never a key or a token) manages their
// organization's credentials and, in memberships mode (MENDARI_ACCOUNTS=memberships, ADR-0008),
// its people. The tabs: its members (change roles, remove), its invitations (a link made here is
// shown once, in a dialog, and never again; revoke) and its requests to join (grant with roles,
// or refuse), in memberships mode only; its API keys (create from a preset or by hand, the key
// shown once; rotate, revoke, history) and, with SMART Backend Services on, its clients
// (register with a public key, replace its key set, revoke, history), in either accounts mode;
// and its usage (the usage ledger, LP-21g): the remote model calls its decisions and evaluations
// made over the last 30 days and what they cost, never a cost where a call was not priced; and
// its activity (12g-ii, administrators): its administrative events of the last 90 days, newest
// first, asked for each time the tab is shown, and all of them exported as FHIR. Where the pack
// registry is served here (MENDARI_REGISTRY=on), registry.js adds its namespaces and publisher
// tokens (`extendAdmin`).
// Open mode has no view: its API acts for every organization. Someone who may use none of the
// tabs never sees the view: the router asks `available()`, and no list is fetched. Every request
// names the window's organization (`api`, ui.js). Names, addresses, organization names, scopes,
// key ids and actors are what people typed or a server recorded: always escaped.

import { views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, actionDialog, api, docsLink, download, emptyState, esc, fileSlug, icon, MACHINE_ROLES, may, MEMBERSHIP_ROLES, money, offeredRoles, roleName, signedIn, state, time, toast, toastError, today, whileBusy } from "./ui.js";

// An organization's people: memberships mode, an administrator who signed in. All three checks:
// open mode's principal may do everything, `keys` included.
const people = () => state.accounts === "memberships" && signedIn() && may("keys");
// Its credentials: an administrator who signed in, in either accounts mode (never open mode).
const credentials = () => signedIn() && may("keys");

export const ADMIN_TABS = [
  { key: "members", label: "Members", shown: people, load: () => api("/api/auth/members"), show: showMembers },
  { key: "invitations", label: "Invitations", shown: people, load: () => api("/api/auth/invitations"), show: showInvitations },
  { key: "requests", label: "Requests to join", shown: people, load: () => api("/api/auth/access-requests"), show: showRequests },
  { key: "keys", label: "API keys", shown: credentials, load: loadKeys, show: showKeys },
  { key: "clients", label: "Clients", shown: () => credentials() && Boolean(state.backendServices), load: loadClients, show: showClients },
  { key: "usage", label: "Usage", shown: () => credentials() && may("admin"), load: () => api(`/api/usage?days=${USAGE_DAYS}`), show: showUsage },
  // Asked for when shown (`showActivity`), so it includes what the other tabs just changed.
  { key: "activity", label: "Activity", shown: () => signedIn() && may("admin"), load: () => null, show: showActivity },
];
const USAGE_DAYS = 30;

// What the tabs show; `names`: each member's name by their id (who made what), `you`: the
// caller's own id (the members list marks it); `presets`: the key presets (the server's table).
// Never a key's secret: it lives in its one-time dialog's input only.
const adminState = {
  members: [], invitations: [], requests: [], keys: [], clients: [], usage: null, presets: [], names: new Map(), you: null,
};
let wanted = null; // the tab the address names, kept while the lists load

const shownTabs = () => ADMIN_TABS.filter((t) => t.shown());
const orgName = () => state.memberships.find((m) => m.org === state.me?.org)?.name ?? state.me?.org ?? "";
const ofOrg = () => (people() ? ` of ${esc(orgName())}` : ""); // claims mode: the one organization

views.admin = async (main, params) => {
  wanted = params[0];
  const tabs = shownTabs();
  await load(tabs);
  const intro = people()
    ? `Who can use ${esc(orgName())} and with which roles. Mendari sends no e-mail: you pass invitation links on yourself.`
    : "API keys and Backend Services clients: the credentials machines use here. A key's secret is shown once; Mendari keeps only a digest.";
  main.innerHTML = `<h1>Access</h1>
    <p class="muted">${intro}</p>
    <nav class="segmented tabs" aria-label="Access views">${tabs.map((t) => `<a href="#admin/${t.key}" data-tab="${t.key}">${esc(t.label)}${
      t.key === "requests" ? ` <span class="count" id="requests-count">${adminState.requests.length || ""}</span>` : ""}</a>`).join("")}</nav>
    <div id="admin-body" class="admin-body"></div>`;
  const body = $("#admin-body");
  body.addEventListener("click", act);
  body.addEventListener("submit", submitted);
  body.addEventListener("change", changed);
  showTab(wanted);
};

views.admin.update = (params) => {
  wanted = params[0];
  showTab(wanted);
};

views.admin.available = () => ADMIN_TABS.some((t) => t.shown());

// An organization's people and credentials, not a pack's: shown without waiting for the pack's
// details (router.js).
views.admin.packless = true;

// The link leaves the page with the view: an open dialog (a link or a key shown once among them)
// closes, but one whose request is running (`data-busy`, ui.js actionDialog) closes with its
// answer, so what follows it (a reload after changing your own roles) still happens.
views.admin.leave = () => document.querySelectorAll("dialog[data-admin]:not([data-busy])").forEach((d) => d.close());

async function load(tabs) {
  const lists = await Promise.all(tabs.map((t) => t.load()));
  tabs.forEach((t, i) => (adminState[t.key] = lists[i]));
  adminState.names = new Map(adminState.members.filter((m) => m.user_id).map((m) => [m.user_id, m.name || m.email]));
  adminState.you = adminState.members.find((m) => m.you)?.user_id ?? null;
}

// The key presets, asked for once (the same for every organization: the server's table).
let presetsAsked = null;

function presets() {
  presetsAsked ??= api("/api/auth/keys/presets").then((list) => {
    adminState.presets = list;
    return list;
  }, (error) => {
    presetsAsked = null; // asked again next time
    throw error;
  });
  return presetsAsked;
}

async function loadKeys() {
  const [keys] = await Promise.all([api("/api/auth/keys"), presets()]);
  return keys;
}

async function loadClients() {
  const [clients] = await Promise.all([api("/api/auth/clients"), presets()]);
  return clients;
}

function showTab(requested) {
  const body = $("#admin-body");
  if (!body) return; // still loading: the view shows `wanted` when it is done
  const tabs = shownTabs();
  const tab = tabs.find((t) => t.key === requested) ?? tabs[0];
  document.querySelectorAll(".tabs a[data-tab]").forEach((a) => {
    if (a.dataset.tab === tab.key) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  body.dataset.tab = tab.key;
  body.innerHTML = tab.show();
  wire(body);
}

// After a change: the lists it touched again, the tab shown again, focus on the tab's link.
async function reload(...keys) {
  try {
    await load(ADMIN_TABS.filter((t) => keys.includes(t.key) && t.shown()));
  } catch (error) {
    toastError(error.message);
    return;
  }
  const count = $("#requests-count");
  if (count) count.textContent = adminState.requests.length || "";
  const body = $("#admin-body");
  if (!body) return; // the person moved on
  showTab(body.dataset.tab);
  settle();
}

const settle = () => $('.tabs a[aria-current="page"]')?.focus();

// -- shared pieces ------------------------------------------------------------------------------

const roleText = (key) => {
  const role = MEMBERSHIP_ROLES.find((r) => r.key === key);
  return role?.reserved ? `${role.label} (reserved: grants nothing yet)` : roleName(key);
};

// Plain words first, each role's code one hover away (notes/ui-ux-review.md, decision 1).
const rolesHtml = (roles) => roles.map((r) => `<span title="${esc(r)}">${esc(roleText(r))}</span>`).join(", ");

const roleChoices = (checked) => `<fieldset class="role-choices"><legend>Roles</legend>${offeredRoles().map((r) => `<label>
      <input type="checkbox" name="roles" value="${esc(r.key)}"${checked.includes(r.key) ? " checked" : ""}>
      <span><b>${esc(r.label)}</b> <span class="muted">${esc(r.reserved ? "reserved: grants nothing yet" : r.text)}</span></span>
    </label>`).join("")}</fieldset>`;

const checkedRoles = (form) => [...form.querySelectorAll('input[name="roles"]:checked')].map((input) => input.value);

function chosenRoles(form) {
  const roles = checkedRoles(form);
  if (!roles.length) throw new Error("Choose at least one role.");
  return roles;
}

// Who made or did something: you, a member by name, a member since removed (their id one hover
// away), an invitation (by whoever made it), or what was recorded (the command line, say).
function who(id) {
  if (!id) return "";
  if (id === adminState.you) return "You";
  const name = adminState.names.get(id);
  if (name) return esc(name);
  if (id.startsWith("user:")) return `<span title="${esc(id)}">Former member</span>`;
  if (id.startsWith("invitation:")) {
    const made = adminState.invitations.find((i) => `invitation:${i.id}` === id);
    return made ? `Invitation from ${who(made.created_by)}` : `<span title="${esc(id)}">An invitation</span>`;
  }
  return esc(id);
}

// Who made a key or a client, as the chain records them (an e-mail address, or `issuer|subject`):
// "You" for this window's own, a member's name for a member's address (memberships mode), else
// what was recorded.
function byActor(actor) {
  if (!actor) return "";
  if (actor === state.me?.actor) return "You";
  return esc(adminState.members.find((m) => m.email && m.email === actor)?.name || actor);
}

// A refusal answered inside the dialog, in words: the last administrator's, plainly; a 404 (gone
// meanwhile) closes it, said after.
const GONE = { gone: true };

function refused(error) {
  if (error.status === 404) return GONE;
  if (error.status === 409 && error.code === "AuthError" && /without an administrator/.test(error.message)) {
    throw new Error(`${orgName()} needs an administrator who has signed in. Make someone else an administrator first.`);
  }
  throw error;
}

const actionButton = (action, id, label, text) =>
  `<button type="button" data-action="${action}" data-id="${esc(id)}" aria-label="${esc(label)}">${esc(text)}</button>`;

const STATUS = {
  open: ["Open", "s-warning"],
  accepted: ["Accepted", "s-ok"],
  active: ["Active", "s-ok"],
  replaced: ["Replaced", "s-warning"],
  revoked: ["Revoked", "o-ABSTAIN"],
  expired: ["Expired", "o-ABSTAIN"],
};

const statusBadge = (status) => {
  const [text, tone] = STATUS[status] ?? [status, ""];
  return `<span class="badge ${tone}" title="${esc(status)}">${esc(text)}</span>`;
};

// Every row's id: a key's, an invitation's or a publisher token's `id`, a client's `client_id`, a
// namespace claim's `namespace`.
const idOf = (row) => String(row.id ?? row.client_id ?? row.namespace);

// The row buttons of tabs another module adds (`extendAdmin`): the list its row is in, and the
// handler.
const ACTIONS = {};

// One click handler for every row's buttons (the rows are drawn again after each change).
function act(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const [list, handle] = {
    roles: ["members", changeRoles],
    remove: ["members", removeMember],
    revoke: ["invitations", revokeInvitation],
    grant: ["requests", grantRequest],
    refuse: ["requests", refuseRequest],
    rotate: ["keys", rotateKey],
    "revoke-key": ["keys", revokeKey],
    "key-history": ["keys", keyHistory],
    "copy-id": ["clients", copyClientId],
    "replace-keys": ["clients", replaceKeys],
    "revoke-client": ["clients", revokeClient],
    "client-history": ["clients", clientHistory],
    ...ACTIONS,
  }[button.dataset.action] ?? [];
  const item = list && adminState[list]?.find((row) => idOf(row) === button.dataset.id);
  if (item) said(handle(item, button));
}

// A handler's failure, said (never an unhandled rejection the person never sees).
const said = (task) => Promise.resolve(task).catch((error) => toastError(error.message));

// The forms of tabs another module adds (`extendAdmin`), by the form's id.
const FORMS = {};

// One submit handler for the tab's form.
function submitted(event) {
  const handle = { "invite-form": invite, "key-form": createKey, "client-form": registerClient, ...FORMS }[event.target.id];
  if (handle) said(handle(event));
}

// Tabs another module adds (registry.js, LP-19f), placed before the tab `before`: their lists load
// and show as these do, with their rows' actions ({action: [list, handler]}) and their forms ({form
// id: handler}). Called when that module loads, before the view is first drawn.
export function extendAdmin({ tabs, before, actions = {}, forms = {} }) {
  const at = ADMIN_TABS.findIndex((t) => t.key === before);
  ADMIN_TABS.splice(at < 0 ? ADMIN_TABS.length : at, 0, ...tabs);
  Object.assign(ACTIONS, actions);
  Object.assign(FORMS, forms);
}

// What such a module draws with: the lists (by tab), the one-time dialog, a row's button, a list
// reloaded after a change, and the organization's name for a caption.
export const adminKit = {
  lists: adminState,
  reload: (...keys) => reload(...keys),
  showOnce: (options) => showOnce(options),
  actionButton: (...args) => actionButton(...args),
  ofOrg: () => ofOrg(),
};

// Choosing a preset redraws what it says (and, for a key, its days), keeping what was typed.
function changed(event) {
  const prefix = { "key-preset": "key", "client-preset": "client" }[event.target.id];
  if (prefix) showPreset(event.target.form, prefix, presetById(event.target.value));
}

// Within its own form, which may have left the page meanwhile (another tab chosen while a request
// ran): then there is nothing to draw.
function showPreset(form, prefix, chosen) {
  const detail = form?.querySelector(`#${prefix}-preset-detail`);
  if (!detail) return;
  detail.innerHTML = presetDetail(prefix, chosen);
  const days = form.querySelector("#key-days");
  if (prefix === "key" && days) {
    days.min = String(chosen?.min_days ?? 1);
    days.max = String(chosen?.max_days ?? 365);
    days.value = String(chosen?.days ?? KEY_DAYS);
  }
}

// A form sent: emptied, and its preset's detail drawn again for the first preset it now shows;
// nothing to do for a form no longer on the page.
function cleared(form, prefix) {
  if (!form.isConnected) return;
  form.reset();
  showPreset(form, prefix, presetById(form.querySelector(`#${prefix}-preset`)?.value));
}

// -- members ------------------------------------------------------------------------------------

const personName = (m) => (m.pending ? m.email : m.name || m.email || "Someone");

function showMembers() {
  return tableHtml({
    id: "members",
    caption: `Members of ${esc(orgName())}`,
    columns: [
      {
        key: "person", label: "Person", sort: (m) => personName(m).toLowerCase(),
        cell: (m) => (m.pending
          ? '<span class="muted">Not signed in yet</span>'
          : `${esc(personName(m))}${m.you ? ' <span class="badge">You</span>' : ""}`),
      },
      { key: "email", label: "E-mail", cell: (m) => esc(m.email ?? ""), sort: (m) => m.email ?? "" },
      { key: "roles", label: "Roles", cell: (m) => rolesHtml(m.roles) },
      { key: "since", label: "Member since", className: "nowrap", cell: (m) => esc(time(m.created_at)), sort: (m) => m.created_at },
      { key: "by", label: "Added by", cell: (m) => who(m.created_by) },
      {
        key: "actions", label: "Actions", className: "nowrap",
        cell: (m) => `<div class="row">${actionButton("roles", m.id, `Change roles for ${personName(m)}`, "Change roles")}${
          actionButton("remove", m.id, `Remove ${personName(m)}`, "Remove")}</div>`,
      },
    ],
    rows: adminState.members,
  });
}

async function changeRoles(member) {
  const person = personName(member);
  const own = member.you
    ? '<p class="callout warning">These are your own roles: without Administrator, you lose this screen.</p>'
    : "";
  const changed = await actionDialog({
    title: `Roles for ${person}`,
    body: `${roleChoices(member.roles)}${own}`,
    action: "Save roles",
    run: (form) => api(`/api/auth/members/${encodeURIComponent(member.id)}/roles`, {
      method: "PUT", body: JSON.stringify({ roles: chosenRoles(form) }),
    }).catch(refused),
  });
  if (!changed) return;
  if (changed !== GONE && member.you) {
    location.reload(); // what this window may do changed with them: start over
    return;
  }
  toast(changed === GONE ? `${person} left meanwhile.` : `${person}'s roles: ${(changed.roles ?? []).map(roleName).join(", ")}.`);
  await reload("members");
}

async function removeMember(member) {
  const person = personName(member);
  const org = orgName();
  const revokes = member.email ? ` Open invitations to ${esc(member.email)} are revoked with it.` : "";
  const removed = await actionDialog({
    title: `Remove ${person} from ${org}?`,
    body: member.you
      ? `<p>You lose access to ${esc(org)} at once.${revokes} Keys and invitations you made stay until they are revoked.</p>`
      : `<p>They lose access at their next request.${revokes} Keys and invitations they made stay until you revoke them.</p>`,
    action: "Remove",
    danger: true,
    run: () => api(`/api/auth/members/${encodeURIComponent(member.id)}`, { method: "DELETE" }).catch(refused),
  });
  if (!removed) return;
  if (removed !== GONE && member.you) {
    location.reload(); // no access to it any more: start over
    return;
  }
  toast(removed === GONE ? `${person} left meanwhile.` : `${person} was removed from ${org}.`);
  await reload("members", "invitations");
}

// -- invitations --------------------------------------------------------------------------------

function ends(i) {
  if (i.status === "accepted") return `Accepted ${esc(time(i.accepted_at))} by ${who(i.accepted_by)}`;
  if (i.status === "revoked") return `Revoked ${esc(time(i.revoked_at))} by ${who(i.revoked_by)}`;
  return `${i.status === "expired" ? "Expired" : "Expires"} ${esc(time(i.expires_at))}`;
}

const revocable = (i) => i.status === "open" || i.status === "expired"; // the server revokes both

function showInvitations() {
  const form = `<form class="panel invite-form" id="invite-form" novalidate>
      <h2>Invite someone</h2>
      <p class="muted">You get a link that works once, for that address signed in and verified. Mendari sends no e-mail.</p>
      <label for="invite-email">E-mail address</label>
      <input id="invite-email" name="email" type="email" required maxlength="254" autocomplete="off" spellcheck="false" class="field-full">
      ${roleChoices(["viewer"])}
      <label for="invite-days">Expires after (days)</label>
      <input id="invite-days" name="days" type="number" min="1" max="30" value="7" required>
      <div class="row mt-4"><button type="submit" class="primary">Create link</button></div>
    </form>`;
  if (!adminState.invitations.length) {
    return form + emptyState({ icon: "inbox", title: "No invitations yet", text: "Invite someone with the form above." });
  }
  return form + tableHtml({
    id: "invitations",
    caption: `Invitations to ${esc(orgName())}, newest first`,
    columns: [
      { key: "email", label: "Address", cell: (i) => esc(i.email), sort: (i) => i.email },
      { key: "roles", label: "Roles", cell: (i) => rolesHtml(i.roles) },
      { key: "status", label: "Status", cell: (i) => statusBadge(i.status), sort: (i) => i.status },
      { key: "by", label: "Made by", cell: (i) => who(i.created_by) },
      { key: "made", label: "Made", className: "nowrap", cell: (i) => esc(time(i.created_at)), sort: (i) => i.created_at },
      { key: "ends", label: "Ends", className: "nowrap", cell: ends },
      {
        key: "actions", label: "Actions", className: "nowrap",
        cell: (i) => (revocable(i) ? actionButton("revoke", i.id, `Revoke the invitation for ${i.email}`, "Revoke") : ""),
      },
    ],
    rows: adminState.invitations,
  });
}

// The invite form: an address, its roles and its days; the link it makes is shown once.
async function invite(event) {
  const form = event.target.closest("#invite-form");
  if (!form) return;
  event.preventDefault();
  const roles = checkedRoles(form);
  form.querySelector('input[name="roles"]').setCustomValidity(roles.length ? "" : "Choose at least one role.");
  if (!form.reportValidity()) return;
  const email = form.querySelector("#invite-email").value.trim();
  const days = Number(form.querySelector("#invite-days").value);
  let made;
  try {
    made = await whileBusy(form.querySelector('button[type="submit"]'), "Creating…", () =>
      api("/api/auth/invitations", { method: "POST", body: JSON.stringify({ email, roles, days }) }));
  } catch (error) {
    toastError(error.message);
    return;
  }
  const roleWords = made.roles.map(roleText).join(", ");
  showOnce({
    id: "link",
    input: "invite-link",
    title: `Invitation link for ${esc(made.email)}`,
    warning: `Copy it now: Mendari keeps only a digest and can't show it again. Mendari sends no e-mail: send it to ${esc(made.email)} yourself.`,
    label: "Link",
    value: location.origin + made.link,
    details: `Works once, for ${esc(made.email)} signed in with that address verified, until ${esc(time(made.expires_at))}. Roles: ${esc(roleWords)}.`,
    copy: "Copy link",
  });
  if (form.isConnected) form.reset(); // after the link is shown, whatever became of the form
  await reload("invitations");
}

// A secret, once (an invitation's link, an API key): in a modal, in an input's value only (never
// markup, an attribute, the page's text, a toast, a live region, storage or the console), gone
// with the dialog, which closes when the view is left. Mendari keeps only a digest of it: a lost
// one is revoked (or rotated) and made again. `title`, `warning` and `details` are trusted HTML:
// escape what they hold.
function showOnce({ id, input: inputId, title, warning, label, value, details, copy: copyText }) {
  const dialog = document.createElement("dialog");
  dialog.className = "confirm";
  dialog.dataset.admin = "";
  dialog.setAttribute("aria-labelledby", `${id}-title`);
  dialog.setAttribute("aria-describedby", `${id}-warning`);
  dialog.innerHTML = `<form method="dialog">
      <h2 id="${id}-title">${title}</h2>
      <p id="${id}-warning" class="callout warning"><strong>Shown once.</strong> ${warning}</p>
      <label for="${inputId}">${esc(label)}</label>
      <input id="${inputId}" class="mono field-full" readonly autocomplete="off" spellcheck="false">
      <p class="muted">${details}</p>
      <p class="muted" role="status" id="${id}-copied"></p>
      <div class="row mt-4"><span class="grow"></span>
        <button type="button" id="copy-${id}" autofocus>${icon("copy")}<span>${esc(copyText)}</span></button>
        <button value="done" class="primary">Done</button>
      </div>
    </form>`;
  document.body.append(dialog);
  const input = dialog.querySelector(`#${inputId}`);
  input.value = value;
  const copyButton = dialog.querySelector(`#copy-${id}`);
  const said = dialog.querySelector(`#${id}-copied`);
  // Said inside the dialog: nothing under a modal is heard.
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      copyButton.innerHTML = `${icon("check")}<span>Copied</span>`;
      said.textContent = "Copied to the clipboard.";
    } catch {
      input.focus();
      input.select();
      said.textContent = "Copying isn't available here: it is selected, copy it with Ctrl+C or Command+C.";
    }
  });
  dialog.addEventListener("close", () => {
    input.value = "";
    dialog.remove();
    settle();
  });
  dialog.showModal();
}

async function revokeInvitation(invitation) {
  const revoked = await actionDialog({
    title: `Revoke the invitation for ${invitation.email}?`,
    body: "<p>Its link stops working at once.</p>",
    action: "Revoke",
    danger: true,
    run: () => api(`/api/auth/invitations/${encodeURIComponent(invitation.id)}`, { method: "DELETE" }).catch(refused),
  });
  if (!revoked) return;
  toast(revoked === GONE ? "It was accepted or revoked meanwhile." : `The invitation for ${invitation.email} is revoked.`);
  await reload("invitations");
}

// -- requests to join ---------------------------------------------------------------------------

const requester = (r) => r.name || r.email || "Someone";

function showRequests() {
  if (!adminState.requests.length) {
    return emptyState({ icon: "inbox", title: "No requests to join", text: `When someone asks to join ${esc(orgName())}, it shows here.` });
  }
  return tableHtml({
    id: "requests",
    caption: `Requests to join ${esc(orgName())}, oldest first`,
    columns: [
      { key: "person", label: "Person", cell: (r) => esc(requester(r)), sort: (r) => requester(r).toLowerCase() },
      { key: "email", label: "E-mail", cell: (r) => (r.email ? esc(r.email) : '<span class="muted">No verified address</span>') },
      { key: "asked", label: "Asked", className: "nowrap", cell: (r) => esc(time(r.created_at)), sort: (r) => r.created_at },
      {
        key: "actions", label: "Actions", className: "nowrap",
        cell: (r) => `<div class="row">${actionButton("grant", r.id, `Grant ${requester(r)}'s request`, "Grant…")}${
          actionButton("refuse", r.id, `Refuse ${requester(r)}'s request`, "Refuse")}</div>`,
      },
    ],
    rows: adminState.requests,
  });
}

async function grantRequest(request) {
  const person = requester(request);
  const org = orgName();
  const granted = await actionDialog({
    title: `Let ${person} join ${org}`,
    body: `<p>With the roles you choose.${request.email ? ` Their verified address: ${esc(request.email)}.` : ""}</p>${roleChoices(["viewer"])}`,
    action: "Grant",
    run: (form) => api(`/api/auth/access-requests/${encodeURIComponent(request.id)}`, {
      method: "POST", body: JSON.stringify({ outcome: "granted", roles: chosenRoles(form) }),
    }).catch(refused),
  });
  if (!granted) return;
  toast(granted === GONE ? `${person}'s request was handled meanwhile.` : `${person} joined ${org}.`);
  await reload("requests", "members");
}

async function refuseRequest(request) {
  const person = requester(request);
  const refusedNow = await actionDialog({
    title: `Refuse ${person}'s request?`,
    body: "<p>Mendari doesn't tell them; they can ask again.</p>",
    action: "Refuse",
    danger: true,
    run: () => api(`/api/auth/access-requests/${encodeURIComponent(request.id)}`, {
      method: "POST", body: JSON.stringify({ outcome: "refused" }),
    }).catch(refused),
  });
  if (!refusedNow) return;
  toast(refusedNow === GONE ? `${person}'s request was handled meanwhile.` : `${person}'s request is refused.`);
  await reload("requests");
}

// -- what keys and clients are for: presets, roles and scopes ------------------------------------

const presetById = (id) => adminState.presets.find((p) => p.id === id) ?? null;
const machineRole = (key) => MACHINE_ROLES.find((r) => r.key === key);
const machineRoleHtml = (key) => `<span title="${esc(key)}">${esc(machineRole(key)?.label ?? key)}</span>`;
const EVERYTHING = "Everything its role can use";

// A preset's sentence, its `code` spans as code (escaped first).
const withCode = (text) => esc(text).replace(/`([^`]+)`/g, "<code>$1</code>");

// The presets a key can be made from (all), or a client registered with (those with a scope: a
// client is always registered with scopes; `rest`, the role's own, is for keys only).
const presetsFor = (prefix) => adminState.presets.filter((p) => prefix === "key" || p.scope !== null);

const presetOptions = (prefix) => `${presetsFor(prefix).map((p) => `<option value="${esc(p.id)}">${esc(p.title)}</option>`).join("")}
  <option value="">Custom</option>`;

// What a preset gives, read-only; or, for Custom, the role and the scope to choose.
function presetDetail(prefix, preset) {
  if (!preset) {
    const empty = prefix === "key" ? "the key has its role's permissions" : "everything its role can use";
    return `<fieldset class="role-choices"><legend>Role</legend>${MACHINE_ROLES.map((r, i) => `<label>
        <input type="radio" name="${prefix}-role" value="${esc(r.key)}"${i === 0 ? " checked" : ""}>
        <span><b>${esc(r.label)}</b> <span class="muted">${esc(r.text)}</span></span>
      </label>`).join("")}</fieldset>
      <label for="${prefix}-scope">Scope (optional)</label>
      <input id="${prefix}-scope" class="mono field-full" maxlength="1000" autocomplete="off" spellcheck="false" aria-describedby="${prefix}-scope-hint">
      <p class="muted" id="${prefix}-scope-hint">Space-separated: <code>system/Task.rs</code>, <code>https://mendari.co/fhir/scope/decide</code>, … Left empty, ${empty}.</p>`;
  }
  const link = docsLink(preset.doc);
  const reaches = preset.reaches ?? [];
  return `<p>${withCode(preset.purpose)}</p>
    <p>Role: ${machineRoleHtml(preset.role)}. Scope: ${preset.scope === null ? EVERYTHING : `<code class="wrap">${esc(preset.scope)}</code>`}.</p>
    <details><summary>Reaches ${reaches.length} ${reaches.length === 1 ? "route" : "routes"}</summary>
      <ul class="reaches">${reaches.map(([method, path]) => `<li><code class="wrap">${esc(method)} ${esc(path)}</code></li>`).join("")}</ul>
    </details>
    ${link ? `<p><a href="${esc(link)}" target="_blank" rel="noopener noreferrer">How to use it ${icon("external")}</a></p>` : ""}`;
}

// What the form says the key or client is: a preset's role and scope, or the chosen ones.
function purposeOf(form, prefix) {
  const preset = presetById(form.querySelector(`#${prefix}-preset`).value);
  if (preset) return { preset, role: preset.role, scope: preset.scope };
  const role = form.querySelector(`input[name="${prefix}-role"]:checked`)?.value ?? "viewer";
  const scope = form.querySelector(`#${prefix}-scope`).value.trim().replace(/\s+/g, " ");
  return { preset: null, role, scope: scope || null };
}

// A name, required, not only spaces: said on the field, as the browser says what is missing.
function nameOf(form, prefix) {
  const field = form.querySelector(`#${prefix}-name`);
  field.setCustomValidity(field.value.trim() || !field.value ? "" : "Give it a name.");
  return field.value.trim();
}

// A whole number from a field, within its bounds, or a refusal in words (an empty overlap must
// never read as 0, "stop the old key at once").
function wholeNumber(form, selector, what) {
  const field = form.querySelector(selector);
  const value = Number(field.value);
  const [low, high] = [Number(field.min), Number(field.max)];
  if (field.value.trim() === "" || !Number.isInteger(value) || value < low || value > high) {
    throw new Error(`${what} is a whole number from ${low} to ${high}.`);
  }
  return value;
}

// -- API keys -----------------------------------------------------------------------------------

const KEY_DAYS = 90; // core/audit.py KEY_DAYS, for a key made by hand

// A key's scope in words: its preset's title (the scope one hover away), the role's own, or the
// scope itself.
function keyScope(k) {
  if (k.preset) return `<span title="${esc(k.scope ?? EVERYTHING)}">${esc(presetById(k.preset)?.title ?? k.preset)}</span>`;
  return k.scope === null || k.scope === undefined ? EVERYTHING : `<code class="wrap">${esc(k.scope)}</code>`;
}

// Rotated: offered on an active key, and on a replaced one whose replacement was revoked (if the
// answer carrying it was lost, the server lets it be rotated again).
// Only a machine's key rotates: one the operator made on the command line for evaluation runs
// (a role outside MACHINE_ROLES) is listed and revoked here, never rotated (the server refuses).
const rotatable = (k) => MACHINE_ROLES.some((r) => r.key === k.role) && (k.status === "active"
  || (k.status === "replaced" && adminState.keys.find((r) => r.id === k.replaced_by)?.status === "revoked"));

function showKeys() {
  const first = adminState.presets[0] ?? null;
  const form = `<form class="panel admin-form" id="key-form" novalidate>
      <h2>Create an API key</h2>
      <p class="muted">For a machine: one role, viewer or service, narrowed by a scope. It is shown once.</p>
      <label for="key-name">Name</label>
      <input id="key-name" name="name" required maxlength="100" autocomplete="off" class="field-full">
      <label for="key-preset">What it is for</label>
      <select id="key-preset" class="field-full">${presetOptions("key")}</select>
      <div id="key-preset-detail" class="preset-detail">${presetDetail("key", first)}</div>
      <label for="key-days">Expires after (days)</label>
      <input id="key-days" name="days" type="number" min="${first?.min_days ?? 1}" max="${first?.max_days ?? 365}" value="${first?.days ?? KEY_DAYS}" required>
      <div class="row mt-4"><button type="submit" class="primary">Create key</button></div>
    </form>`;
  if (!adminState.keys.length) {
    return form + emptyState({ icon: "inbox", title: "No API keys yet", text: "Create one with the form above." });
  }
  return form + tableHtml({
    id: "keys",
    caption: `API keys${ofOrg()}, oldest first`,
    columns: [
      { key: "name", label: "Name", cell: (k) => esc(k.name), sort: (k) => k.name.toLowerCase() },
      { key: "role", label: "Role", cell: (k) => machineRoleHtml(k.role), sort: (k) => k.role },
      { key: "scope", label: "Scope", className: "scope", cell: keyScope },
      { key: "created", label: "Created", cell: (k) => madeBy(k.created_at, k.created_by), sort: (k) => k.created_at },
      {
        key: "expires", label: "Expires", sort: (k) => k.expires_at,
        cell: (k) => (k.status === "replaced" ? `Stops ${esc(time(k.expires_at))}` : esc(time(k.expires_at))),
      },
      { key: "status", label: "Status", cell: (k) => statusBadge(k.status), sort: (k) => k.status },
      {
        key: "actions", label: "Actions", className: "actions",
        cell: (k) => `<div class="row">${rotatable(k) ? actionButton("rotate", k.id, `Rotate ${k.name}`, "Rotate…") : ""}${
          k.status !== "revoked" ? actionButton("revoke-key", k.id, `Revoke ${k.name}`, "Revoke") : ""}${
          actionButton("key-history", k.id, `History of ${k.name}`, "History")}</div>`,
      },
    ],
    rows: adminState.keys,
  });
}

// When a key or a client was made, and by whom (on a line of its own).
const madeBy = (at, actor) => `${esc(time(at))}<br><span class="muted">by ${byActor(actor)}</span>`;

// Role, and scope or preset, in words (the one-time dialog, a history line).
function keyFacts(made) {
  const preset = made.preset ? presetById(made.preset) : null;
  const scope = preset ? `preset ${esc(preset.title)}` : made.scope ? `scope <code class="wrap">${esc(made.scope)}</code>` : "its role's permissions";
  return `${esc(machineRole(made.role)?.label ?? made.role)}, ${scope}`;
}

// The create form: a preset (or a role and scope by hand) and days; the key is shown once.
async function createKey(event) {
  event.preventDefault();
  const form = event.target;
  const name = nameOf(form, "key");
  if (!form.reportValidity()) return;
  const { preset, role, scope } = purposeOf(form, "key");
  const days = Number(form.querySelector("#key-days").value);
  const body = preset ? { name, role, preset: preset.id, days } : { name, role, scope, days };
  let made;
  try {
    made = await whileBusy(form.querySelector('button[type="submit"]'), "Creating…", () =>
      api("/api/auth/keys", { method: "POST", body: JSON.stringify(body) }));
  } catch (error) {
    toastError(error.message);
    return;
  }
  // The key first, whatever became of the form meanwhile (another tab, another view): it is
  // shown once, and a key made and never shown is lost.
  showKey(made, `${keyFacts(made)}; expires ${esc(time(made.expires_at))}.`);
  cleared(form, "key");
  await reload("keys");
}

function showKey(made, details) {
  showOnce({
    id: "key",
    input: "key-secret",
    title: `API key ${esc(made.name)}`,
    warning: "Copy it now: Mendari keeps only a digest and can't show it again. A lost key is revoked or rotated.",
    label: "Key",
    value: made.key,
    details,
    copy: "Copy key",
  });
}

async function rotateKey(key) {
  const days = presetById(key.preset)?.days ?? KEY_DAYS;
  const rotated = await actionDialog({
    title: `Rotate ${key.name}`,
    body: `<p>A new key with the same name, role and scope, shown once.</p>
      <label for="rotate-overlap">Hours the old key keeps working</label>
      <input id="rotate-overlap" type="number" min="0" max="168" value="24" required aria-describedby="rotate-overlap-hint">
      <p class="muted" id="rotate-overlap-hint">Never past ${esc(time(key.expires_at))}, when it would expire anyway. 0 stops it at once, for a leaked key.</p>
      <label for="rotate-days">New key expires after (days)</label>
      <input id="rotate-days" type="number" min="1" max="365" value="${esc(days)}" required>`,
    action: "Rotate",
    run: (form) => {
      const overlap = wholeNumber(form, "#rotate-overlap", "The overlap");
      const lives = wholeNumber(form, "#rotate-days", "The new key's days");
      if (overlap > lives * 24) throw new Error("The overlap is longer than the new key lives.");
      return api(`/api/auth/keys/${encodeURIComponent(key.id)}/rotate`, {
        method: "POST", body: JSON.stringify({ overlap_hours: overlap, days: lives }),
      }).catch(refused);
    },
  });
  if (!rotated) return;
  if (rotated === GONE) {
    toast("It was revoked meanwhile.");
  } else {
    showKey(rotated, `${keyFacts(rotated)}; expires ${esc(time(rotated.expires_at))}. ${esc(key.name)} (the old key) keeps working until ${esc(time(rotated.replaces?.expires_at))}.`);
  }
  await reload("keys");
}

async function revokeKey(key) {
  const revoked = await actionDialog({
    title: `Revoke ${key.name}?`,
    body: "<p>Requests that send it are refused from now on. This can't be undone; make a new key if you need one.</p>",
    action: "Revoke",
    danger: true,
    run: () => api(`/api/auth/keys/${encodeURIComponent(key.id)}`, { method: "DELETE" }).catch(refused),
  });
  if (!revoked) return;
  toast(revoked === GONE ? "It was revoked meanwhile." : `${key.name} is revoked.`);
  await reload("keys");
}

const keyHistory = (key, button) => showHistory(button, historyOf("key", key.id));

// -- Backend Services clients -------------------------------------------------------------------

// A client's scope in words: the scoped presets it equals (it stores no label: several presets
// share one scope), the scope one hover away; else the scope itself.
function clientScope(c) {
  const titles = adminState.presets.filter((p) => p.scope !== null && p.role === c.role && p.scope === c.scope).map((p) => p.title);
  return titles.length ? `<span title="${esc(c.scope)}">${esc(titles.join(" or "))}</span>` : `<code class="wrap">${esc(c.scope)}</code>`;
}

const keysHtml = (keys) => (keys ?? []).map((k) => `<span title="${esc(k.thumbprint)}"><code class="wrap">${esc(k.kid)}</code> (${esc(k.kty)})</span>`).join(", ");

function showClients() {
  const first = presetsFor("client")[0] ?? null;
  const endpoint = state.backendServices?.token_endpoint;
  const form = `<form class="panel admin-form" id="client-form" novalidate>
      <h2>Register a client</h2>
      <p class="muted">A machine that signs its own requests for five-minute tokens (SMART Backend Services). Its private key never leaves it: paste its public key.</p>
      ${endpoint ? `<p class="muted">Token endpoint: <code class="wrap">${esc(endpoint)}</code></p>` : ""}
      <label for="client-name">Name</label>
      <input id="client-name" name="name" required maxlength="100" autocomplete="off" class="field-full">
      <label for="client-preset">What it is for</label>
      <select id="client-preset" class="field-full">${presetOptions("client")}</select>
      <div id="client-preset-detail" class="preset-detail">${presetDetail("client", first)}</div>
      ${keysField("client-keys")}
      <div class="row mt-4"><button type="submit" class="primary">Register client</button></div>
    </form>`;
  if (!adminState.clients.length) {
    return form + emptyState({ icon: "inbox", title: "No clients yet", text: "Register one with the form above." });
  }
  return form + tableHtml({
    id: "clients",
    caption: `Backend Services clients${ofOrg()}, oldest first`,
    columns: [
      {
        key: "name", label: "Name and client id", sort: (c) => c.name.toLowerCase(),
        cell: (c) => `${esc(c.name)}<div class="client-id"><code>${esc(c.client_id)}</code><button type="button" class="icon-button" data-action="copy-id" data-id="${esc(c.client_id)}" aria-label="${esc(`Copy the client id of ${c.name}`)}">${icon("copy")}</button></div>`,
      },
      { key: "role", label: "Role", cell: (c) => machineRoleHtml(c.role), sort: (c) => c.role },
      { key: "scope", label: "Scope", className: "scope", cell: clientScope },
      {
        key: "keys", label: "Keys", className: "keys",
        cell: (c) => `${keysHtml(c.keys)}${c.keys_replaced_at ? `<br><span class="muted">replaced ${esc(time(c.keys_replaced_at))}</span>` : ""}`,
      },
      { key: "registered", label: "Registered", cell: (c) => madeBy(c.created_at, c.created_by), sort: (c) => c.created_at },
      { key: "status", label: "Status", cell: (c) => statusBadge(c.revoked_at ? "revoked" : "active"), sort: (c) => Boolean(c.revoked_at) },
      {
        key: "actions", label: "Actions", className: "actions",
        cell: (c) => `<div class="row">${c.revoked_at ? "" : `${actionButton("replace-keys", c.client_id, `Replace keys of ${c.name}`, "Replace keys…")}${
          actionButton("revoke-client", c.client_id, `Revoke ${c.name}`, "Revoke")}`}${
          actionButton("client-history", c.client_id, `History of ${c.name}`, "History")}</div>`,
      },
    ],
    rows: adminState.clients,
  });
}

const keysField = (id) => `<label for="${id}">Public keys</label>
      <p class="muted" id="${id}-hint">A JWK Set, a JWK, or a PEM public key (<code>BEGIN PUBLIC KEY</code>): RSA of 2048 bits or more, or EC P-384. Never the private key.</p>
      <textarea id="${id}" class="mono field-full" rows="8" spellcheck="false" autocomplete="off" aria-describedby="${id}-hint"></textarea>`;

// The keys pasted, as a public JWK Set; a private key is cleared from the field and never sent.
async function pastedKeys(field) {
  try {
    return await publicKeySet(field.value);
  } catch (error) {
    if (error.privateKey || looksPrivate(field.value)) field.value = "";
    throw error;
  }
}

async function registerClient(event) {
  event.preventDefault();
  const form = event.target;
  const name = nameOf(form, "client");
  if (!form.reportValidity()) return;
  const { role, scope } = purposeOf(form, "client");
  const field = form.querySelector("#client-keys");
  let made;
  try {
    made = await whileBusy(form.querySelector('button[type="submit"]'), "Registering…", async () => {
      const jwks = await pastedKeys(field);
      return api("/api/auth/clients", { method: "POST", body: JSON.stringify({ name, role, scope, jwks }) });
    });
  } catch (error) {
    toastError(error.message);
    return;
  }
  toast(`Client ${made.name} registered.`);
  cleared(form, "client");
  await reload("clients");
}

async function copyClientId(client, button) {
  try {
    await navigator.clipboard.writeText(client.client_id);
    toast(`The client id of ${client.name} is copied.`);
  } catch {
    toastError("Copying isn't available here: select the id and copy it.");
  }
  button.focus();
}

async function replaceKeys(client) {
  const replaced = await actionDialog({
    title: `Replace the keys of ${client.name}`,
    body: `<p>Its keys now: ${keysHtml(client.keys)}.</p>
      ${keysField("replace-keys")}
      <p class="callout warning">The tokens it holds stop at once, and assertions signed with a key not in the new set are refused. To switch without a gap: replace with the old and new keys, move the client to the new one, then replace with the new one alone.</p>`,
    action: "Replace keys",
    danger: true,
    run: async (form) => {
      const jwks = await pastedKeys(form.querySelector("#replace-keys"));
      return api(`/api/auth/clients/${encodeURIComponent(client.client_id)}/keys`, {
        method: "POST", body: JSON.stringify({ jwks }),
      }).catch(refused);
    },
  });
  if (!replaced) return;
  const dropped = replaced.tokens_dropped;
  toast(replaced === GONE
    ? "It isn't there any more."
    : `Keys of ${client.name} replaced; ${dropped} ${dropped === 1 ? "token" : "tokens"} dropped.`);
  await reload("clients");
}

async function revokeClient(client) {
  const revoked = await actionDialog({
    title: `Revoke ${client.name}?`,
    body: "<p>Its tokens stop at once and it gets no new one. This can't be undone; register a new client if you need one.</p>",
    action: "Revoke",
    danger: true,
    run: () => api(`/api/auth/clients/${encodeURIComponent(client.client_id)}`, { method: "DELETE" }).catch(refused),
  });
  if (!revoked) return;
  toast(revoked === GONE ? "It was revoked meanwhile." : `${client.name} is revoked.`);
  await reload("clients");
}

const clientHistory = (client, button) => showHistory(button, historyOf("client", client.client_id));

// -- a public key pasted: JWK Set, JWK or PEM, never a private key ------------------------------

// Members only a private or symmetric JWK holds (backend_services.py refuses them too).
const PRIVATE_MEMBERS = ["d", "p", "q", "dp", "dq", "qi", "oth", "k"];
const PRIVATE_KEY = "That is a private key: paste the public key only, and treat this one as exposed.";
const NOT_A_KEY = "Paste a JWK Set, a JWK or a PEM public key.";

const privateKey = () => Object.assign(new Error(PRIVATE_KEY), { privateKey: true });
const isPrivate = (jwk) => Boolean(jwk) && typeof jwk === "object" && !Array.isArray(jwk)
  && (PRIVATE_MEMBERS.some((member) => Object.hasOwn(jwk, member)) || jwk.kty === "oct");

// Parsed JSON holding a private or symmetric JWK anywhere: a set, an array, a JWK with no `kty`.
// Walked with a stack, never recursion (what is walked is at most MAX_PASTED long).
function holdsPrivate(parsed) {
  const stack = [parsed];
  while (stack.length) {
    const value = stack.pop();
    if (isPrivate(value)) return true;
    for (const member of Object.values(value)) if (member && typeof member === "object") stack.push(member);
  }
  return false;
}

// Far more than a JWK Set of 10 keys takes; anything longer is refused unread.
const MAX_PASTED = 64 * 1024;

const PEM = /^-----BEGIN PUBLIC KEY-----([A-Za-z0-9+/=\s]+)-----END PUBLIC KEY-----$/;
const BARE_BASE64 = /^[A-Za-z0-9+/=\s]{64,}$/;

const derOf = (base64) => Uint8Array.from(atob(base64.replace(/\s+/g, "")), (c) => c.charCodeAt(0));

// A DER private key (PKCS#1, PKCS#8 or SEC 1) is a SEQUENCE whose first member is an INTEGER, its
// version; a public key (SPKI) or a certificate starts with a SEQUENCE.
function derIsPrivate(base64) {
  let der;
  try {
    der = derOf(base64);
  } catch {
    return false;
  }
  if (der.length < 4 || der[0] !== 0x30) return false;
  const first = 2 + (der[1] & 0x80 ? der[1] & 0x7f : 0);
  return der[first] === 0x02;
}

// Whether pasted text is, or holds, a private key this page recognises: any PEM `PRIVATE KEY`
// block (PKCS#1, PKCS#8, SEC 1, OpenSSH, encrypted), JSON with a private or symmetric JWK
// anywhere in it, or a private key's DER in base64, bare or under a PUBLIC KEY header.
function looksPrivate(text) {
  const pasted = String(text ?? "").trim();
  if (/PRIVATE KEY/.test(pasted)) return true;
  if (pasted.length > MAX_PASTED) return false;
  if (/^[[{]/.test(pasted)) {
    try {
      return holdsPrivate(JSON.parse(pasted));
    } catch {
      return false;
    }
  }
  const body = PEM.exec(pasted)?.[1] ?? (BARE_BASE64.test(pasted) ? pasted : null);
  return body !== null && derIsPrivate(body);
}

// What was pasted, as a JWK Set to send: refused in the browser, before anything is sent, if it
// is or holds a private key (`looksPrivate`). A PEM public key (SPKI) becomes a JWK here, its
// `kid` its RFC 7638 thumbprint. The server's check of what is stored
// (backend_services.check_jwks) stays the only one that counts.
export async function publicKeySet(text) {
  const pasted = String(text ?? "").trim();
  if (looksPrivate(pasted)) throw privateKey();
  if (pasted.length > MAX_PASTED) throw new Error("That is too long for a JWK Set: paste the public keys only.");
  if (pasted.startsWith("{")) {
    let parsed;
    try {
      parsed = JSON.parse(pasted);
    } catch {
      throw new Error(NOT_A_KEY);
    }
    const keys = Array.isArray(parsed?.keys) ? parsed.keys : parsed && typeof parsed === "object" && "kty" in parsed ? [parsed] : null;
    if (!keys) throw new Error(NOT_A_KEY);
    return { keys };
  }
  const pem = PEM.exec(pasted);
  if (pem) return { keys: [await jwkFromSpki(pem[1])] };
  throw new Error(NOT_A_KEY);
}

const SPKI = [
  [{ name: "RSASSA-PKCS1-v1_5", hash: "SHA-384" }, "RS384"],
  [{ name: "ECDSA", namedCurve: "P-384" }, "ES384"],
];

async function jwkFromSpki(base64) {
  if (!globalThis.crypto?.subtle) throw new Error("This page can't read a PEM key here: paste it as a JWK.");
  let der;
  try {
    der = derOf(base64);
  } catch {
    throw new Error(NOT_A_KEY);
  }
  for (const [algorithm, alg] of SPKI) {
    let key;
    try {
      key = await crypto.subtle.importKey("spki", der, algorithm, true, ["verify"]);
    } catch {
      continue; // not this kind of key: the next
    }
    const exported = await crypto.subtle.exportKey("jwk", key);
    const members = exported.kty === "RSA" ? ["kty", "n", "e"] : ["kty", "crv", "x", "y"];
    const jwk = Object.fromEntries(members.map((m) => [m, exported[m]]));
    return { ...jwk, alg, use: "sig", kid: await thumbprint(jwk) };
  }
  throw new Error("That PEM is not an RSA or EC P-384 public key.");
}

// RFC 7638: SHA-256 of the required members, in order, without whitespace, base64url (as
// core/audit.py `_thumbprint` computes it).
async function thumbprint(jwk) {
  const members = jwk.kty === "RSA" ? ["e", "kty", "n"] : ["crv", "kty", "x", "y"];
  const body = JSON.stringify(Object.fromEntries(members.map((m) => [m, jwk[m]])));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body)));
  return btoa(String.fromCharCode(...digest)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

// -- a credential's history: its chain, oldest first ----------------------------------------------

// Where a key's or a client's history is, and what to call it.
function historyOf(kind, id) {
  const path = kind === "key" ? "keys" : "clients";
  const row = adminState[path].find((r) => idOf(r) === id);
  return {
    kind,
    id,
    name: row?.name ?? id,
    path: `/api/auth/${path}/${encodeURIComponent(id)}/history`,
    expires: kind === "key" ? row?.expires_at : undefined,
  };
}

// Another key named in a line (a rotation's old or new key): a button that opens its history.
const keyLink = (id) => `<button type="button" class="linklike" data-history="${esc(id)}"><code>${esc(id)}</code></button>`;

const kids = (keys) => (keys ?? []).map((k) => esc(k.kid)).join(", ") || "none";

// One event in words; every value escaped. An event this page does not know is named.
function describe({ event, payload }) {
  const p = payload ?? {};
  switch (event) {
    case "key.created":
      return `Created by ${byActor(p.created_by)} (${keyFacts(p)}), expires ${esc(time(p.expires_at))}${p.replaces ? `, replacing key ${keyLink(p.replaces)}` : ""}`;
    case "key.rotated":
      return `Rotated by ${byActor(p.rotated_by)}: replaced by key ${keyLink(p.replaced_by)}; works until ${esc(time(p.expires_at))} (overlap ${esc(p.overlap_hours)} h; was ${esc(time(p.expires_at_before))})`;
    case "key.revoked":
      return `Revoked by ${byActor(p.revoked_by)}`;
    case "client.registered":
      return `Registered by ${byActor(p.created_by)} with keys ${kids(p.keys)}`;
    case "client.keys_replaced":
      return `Keys replaced by ${byActor(p.replaced_by)}: ${kids(p.keys_before)} → ${kids(p.keys)}; ${esc(p.tokens_dropped)} ${p.tokens_dropped === 1 ? "token" : "tokens"} dropped`;
    case "client.revoked":
      return `Revoked by ${byActor(p.revoked_by)}`;
    default:
      return esc(event);
  }
}

function historyHtml(subject, found) {
  const expires = subject.expires ? `<p class="muted">Expires ${esc(time(subject.expires))}.</p>` : "";
  const lines = found.events.map((e) => `<li><span class="muted nowrap">${esc(time(e.at))}</span> ${describe(e)}</li>`).join("");
  const more = found.truncated ? '<p class="muted">The 200 latest events.</p>' : "";
  return `${expires}${more}<ol class="timeline history">${lines}</ol>`;
}

// A history, asked for first (the button busy meanwhile), then in a dialog; a key named in it
// opens its own history in the same dialog.
async function showHistory(button, subject) {
  let found;
  try {
    found = await whileBusy(button, "Loading…", () => api(subject.path));
  } catch (error) {
    toastError(error.message);
    return;
  }
  const dialog = document.createElement("dialog");
  dialog.className = "confirm";
  dialog.dataset.admin = "";
  dialog.setAttribute("aria-labelledby", "history-title");
  dialog.innerHTML = `<form method="dialog">
      <h2 id="history-title" tabindex="-1"></h2>
      <div id="history-body"></div>
      <div class="row mt-4"><span class="grow"></span><button value="done" class="primary" autofocus>Close</button></div>
    </form>`;
  const fill = (shown, answer) => {
    dialog.querySelector("#history-title").textContent = `History of ${shown.name}`;
    dialog.querySelector("#history-body").innerHTML = historyHtml(shown, answer);
  };
  fill(subject, found);
  dialog.querySelector("#history-body").addEventListener("click", async (event) => {
    const link = event.target.closest("button[data-history]");
    if (!link) return;
    const other = historyOf("key", link.dataset.history);
    try {
      fill(other, await whileBusy(link, "Loading…", () => api(other.path)));
    } catch (error) {
      const said = document.createElement("p");
      said.className = "error";
      said.setAttribute("role", "alert");
      said.textContent = error.status === 404 ? "That key isn't this organization's." : error.message;
      dialog.querySelector("#history-body").prepend(said);
      return;
    }
    dialog.querySelector("#history-title").focus();
  });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (button.isConnected) button.focus();
    else settle(); // the rows were drawn again meanwhile
  });
  document.body.append(dialog);
  dialog.showModal();
}


// -- usage (LP-21i) ------------------------------------------------------------------------------
// The organization's remote model calls by provider and purpose over the days asked, then by
// day, newest first: calls, how many were priced, tokens, and the cost, "unknown" unless every
// call was priced (never a partial sum, never $0 for what was not priced).

const PROVIDER_TEXT = { jev: "Jev", llm: "Language model" };
const PURPOSE_TEXT = { decision: "Decisions", evaluation: "Evaluations" };
const UNPRICED = "Some of these calls had no price: their provider gave none, or the call failed";
const whole = (n) => esc(Number(n).toLocaleString("en-US"));
const costHtml = (u) => (u.cost_usd === null ? `<span class="muted" title="${esc(UNPRICED)}">unknown</span>` : esc(money(u.cost_usd)));

function usageColumns() {
  return [
    { key: "provider", label: "Provider", cell: (u) => esc(PROVIDER_TEXT[u.provider_type] ?? u.provider_type), sort: (u) => u.provider_type },
    { key: "purpose", label: "For", cell: (u) => esc(PURPOSE_TEXT[u.purpose] ?? u.purpose), sort: (u) => u.purpose },
    { key: "calls", label: "Calls", numeric: true, cell: (u) => whole(u.calls), sort: (u) => u.calls },
    { key: "priced", label: "Priced", numeric: true, cell: (u) => whole(u.priced_calls), sort: (u) => u.priced_calls },
    { key: "input", label: "Tokens in", numeric: true, cell: (u) => whole(u.input_tokens), sort: (u) => u.input_tokens },
    { key: "output", label: "Tokens out", numeric: true, cell: (u) => whole(u.output_tokens), sort: (u) => u.output_tokens },
    { key: "cost", label: "Cost", numeric: true, cell: costHtml, sort: (u) => u.cost_usd },
  ];
}

function showUsage() {
  const usage = adminState.usage ?? { rows: [], totals: [] };
  const intro = `<p class="muted">The calls to Jev and language models${ofOrg()} from ${esc(usage.since ?? "")} to ${esc(usage.until ?? "")} (local days), for decisions and for evaluations. A cost shows only when every call was priced.</p>`;
  if (!usage.rows.length) {
    return intro + emptyState({ icon: "inbox", title: "No remote calls", text: "Decisions and evaluations that call Jev or a language model show here." });
  }
  const totals = tableHtml({ id: "usage-totals", caption: `Over the last ${USAGE_DAYS} days`, columns: usageColumns(), rows: usage.totals });
  const days = tableHtml({
    id: "usage-days",
    caption: "By day, newest first",
    columns: [{ key: "day", label: "Day", className: "nowrap", cell: (u) => esc(u.day), sort: (u) => u.day }, ...usageColumns()],
    rows: usage.rows,
    defaultCompare: (a, b) => b.day.localeCompare(a.day),
  });
  return intro + totals + days;
}

// -- activity (12g-ii) ---------------------------------------------------------------------------
// The organization's administrative events (its keys, clients, approvals, holds, evaluation
// reports, record, people and hidden evaluation sets) of the last ACTIVITY_DAYS days, newest
// first: what happened in words, about what, by whom, when. Never a payload's other values (a
// hold's reason, an approval's reference). Asked for newest first (`order=desc`), page by page up
// to ACTIVITY_PAGES, so a window with more events than that shows its newest, and says so. The
// export is every event, FHIR AuditEvents of ids and codes.

const ACTIVITY_DAYS = 90;
const ACTIVITY_PAGE = 500;
const ACTIVITY_PAGES = 10;
const inOrg = () => (people() ? ` in ${esc(orgName())}` : ""); // claims mode: the one organization

// Each kind in words (as the export's displays, standards/audit_fhir.py); an unknown one is named.
const ACTIVITY_TEXT = {
  "key.created": "API key created",
  "key.rotated": "API key rotated",
  "key.revoked": "API key revoked",
  "client.registered": "Client registered",
  "client.revoked": "Client revoked",
  "client.keys_replaced": "Client's key set replaced",
  "governance.approved": "Automation approved",
  "governance.revoked": "Automation approval revoked",
  "registry_pack.approved": "Pack from a registry approved",
  "registry_pack.revoked": "Pack from a registry no longer approved",
  "automation.held": "Automation held",
  "automation.lifted": "Automation hold lifted",
  "eval.recorded": "Evaluation report recorded",
  "organization.created": "Organization created",
  "membership.created": "Member added",
  "membership.roles_changed": "Member's roles changed",
  "membership.removed": "Member removed",
  "membership.bound": "Member signed in for the first time",
  "invitation.created": "Invitation made",
  "invitation.accepted": "Invitation accepted",
  "invitation.revoked": "Invitation revoked",
  "access.requested": "Asked to join",
  "access.handled": "Request to join answered",
  "evalset.created": "Hidden evaluation set created",
  "evalset.case_added": "Hidden evaluation case added",
  "evalset.case_removed": "Hidden evaluation case removed",
  "evalset.sampled": "Hidden evaluation set sampled",
  "evalset.assigned": "Hidden evaluation case assigned",
  "evalset.case_read": "Hidden evaluation case read by its labeller",
  "evalset.labelled": "Hidden evaluation case labelled",
  "evalset.measured": "Labellers' agreement measured",
  "evalset.frozen": "Hidden evaluation set frozen",
  "evalset.activated": "Hidden evaluation set activated",
  "evalset.graded": "Hidden evaluation set read to grade a pack",
  "evalset.retired": "Hidden evaluation set retired",
  "pack_version.created": "Pack store version added",
  "pack_version.held": "Pack store version held for the operator's review",
  "pack_version.released": "Pack store version released",
  "pack_version.withdrawn": "Pack store version withdrawn",
  "store_key.added": "Pack store author key registered",
  "store_key.revoked": "Pack store author key revoked",
};

// What an event is about, by its chain id's prefix (core/audit.py SUBJECT_TABLES).
const SUBJECT_TEXT = {
  key: "API key", client: "Client", governance: "Automation approval", registry_approval: "Registry approval",
  hold: "Automation hold", eval: "Evaluation report", org: "Organization", membership: "Member",
  invitation: "Invitation", access: "Request to join", evalset: "Hidden evaluation set",
};

// Who acted: the first of these a payload has, or a kind's own (standards/audit_fhir.py _ACTORS).
const ACTOR_KEYS = ["created_by", "rotated_by", "revoked_by", "set_by", "lifted_by", "approver", "changed_by", "removed_by", "handled_by", "ended_by", "actor", "user"];
const ACTOR_KEY = { "client.keys_replaced": "replaced_by", "evalset.case_read": "labeller", "evalset.labelled": "labeller", "evalset.graded": "grader" };

let activityAsked = 0; // the latest request: an older answer, arriving after it, is dropped

function showActivity() {
  const asked = ++activityAsked;
  fillActivity(asked);
  // Once showTab has put this in the page (a microtask runs when the current task's code returns).
  queueMicrotask(() => $("#activity-export")?.addEventListener("click", (event) => whileBusy(event.currentTarget, "Exporting…", () => download(
    "/api/audit/administrative/export/fhir", `activity-${fileSlug(state.me?.org)}-${today()}.ndjson`,
  ))));
  return `<p class="muted">What changed${inOrg()} in the last ${ACTIVITY_DAYS} days, from the audit chain: credentials, people, approvals and holds. The export is all of it, as FHIR AuditEvents of ids and codes.</p>
    <div class="row toolbar"><span class="grow"></span><button type="button" id="activity-export">${icon("download")} Export as FHIR</button></div>
    <div id="activity" aria-busy="true"><div class="skeleton" aria-hidden="true"><div class="sk-rows">${'<div class="sk sk-row"></div>'.repeat(5)}</div></div><p class="sr-only">Loading…</p></div>`;
}

async function fillActivity(asked) {
  let html;
  try {
    html = activityTable(await activityEvents());
  } catch (error) {
    html = `<p class="error">${esc(error.message)}</p>`;
  }
  const box = $("#activity");
  if (!box || asked !== activityAsked) return; // another tab, or a newer request, meanwhile
  box.innerHTML = html;
  box.removeAttribute("aria-busy");
  wire(box);
}

async function activityEvents() {
  const since = new Date(Date.now() - ACTIVITY_DAYS * 86_400_000).toISOString();
  const events = [];
  let after = null;
  for (let page = 0; page < ACTIVITY_PAGES; page++) {
    const query = new URLSearchParams({ since, limit: ACTIVITY_PAGE, order: "desc" });
    if (after) query.set("after", after);
    const found = await api(`/api/audit/administrative?${query}`);
    events.push(...found.items);
    after = found.next;
    if (!after) return { events, more: false };
  }
  return { events, more: true };
}

function activityTable({ events, more }) {
  if (!events.length) {
    return emptyState({ icon: "history", title: "No activity", text: `Nothing changed${inOrg()} in the last ${ACTIVITY_DAYS} days.` });
  }
  const cut = more ? `<p class="muted">The newest ${whole(events.length)} events of these ${ACTIVITY_DAYS} days; the export has every one.</p>` : "";
  return cut + tableHtml({
    id: "activity",
    caption: more
      ? `The newest ${whole(events.length)} administrative events${ofOrg()} of the last ${ACTIVITY_DAYS} days, newest first`
      : `Administrative events${ofOrg()}, newest first`,
    limitHeight: true,
    columns: [
      {
        key: "time", label: "Time", className: "nowrap", sort: (e) => e.created_at, firstDir: "descending",
        cell: (e) => `<time datetime="${esc(e.created_at)}">${esc(time(e.created_at))}</time>`,
      },
      { key: "event", label: "Event", cell: (e) => `<span title="${esc(e.event)}">${esc(eventText(e))}</span>`, sort: eventText },
      { key: "about", label: "About", cell: subjectHtml, sort: (e) => e.subject },
      { key: "by", label: "By", cell: actorHtml, sort: (e) => actorOf(e) ?? "" },
    ],
    rows: events, // asked for newest first
  });
}

const eventText = (e) => ACTIVITY_TEXT[e.event] ?? e.event;

// The kind of row, and its name where this page knows it (else its id).
function subjectHtml({ subject, payload }) {
  const [prefix, ...rest] = subject.split(":");
  const id = rest.join(":");
  const name = subjectName(prefix, id, payload ?? {});
  return `${esc(SUBJECT_TEXT[prefix] ?? prefix)} ${name ? esc(name) : `<span class="mono">${esc(id)}</span>`}`;
}

function subjectName(prefix, id, payload) {
  const named = (list, key = "id") => adminState[list].find((row) => String(row[key]) === id);
  if (prefix === "key") return named("keys")?.name ?? payload.name;
  if (prefix === "client") return named("clients", "client_id")?.name ?? payload.name;
  if (prefix === "membership") {
    const member = named("members");
    return member ? personName(member) : null;
  }
  if (prefix === "invitation") return named("invitations")?.email;
  if (prefix === "org" && id === state.me?.org) return orgName();
  return null;
}

function actorOf({ event, payload }) {
  const p = payload ?? {};
  const found = ACTOR_KEY[event] ? p[ACTOR_KEY[event]] : ACTOR_KEYS.map((key) => p[key]).find(Boolean);
  return found ? String(found) : null;
}

// A member or an invitation by `who`, anyone else as the chain records them (`byActor`).
function actorHtml(event) {
  const actor = actorOf(event);
  if (!actor) return '<span class="muted">not recorded</span>';
  return adminState.names.has(actor) || /^(user|invitation):/.test(actor) ? who(actor) : byActor(actor);
}

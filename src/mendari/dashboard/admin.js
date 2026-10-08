// SPDX-License-Identifier: Apache-2.0
// Memberships mode (MENDARI_ACCOUNTS=memberships, ADR-0008): an organization's administrators (an
// `admin` who signed in: `keys`, never a key) see who may use it and change that, at
// "#admin/<tab>" ("Access" in the section list; "#access" is the no-access page, access.js). The
// tabs: its members (change roles, remove), its invitations (a link made here is shown once, in a
// dialog, and never again; revoke) and its requests to join (grant with roles, or refuse). LP-12h
// adds keys and clients as tabs of their own (ADMIN_TABS). Someone who may use none of the tabs
// never sees the view: the router asks `available()`, and no list is fetched. Every request names
// the window's organization (`api`, ui.js). Names, addresses and organization names are what
// people typed or their identity provider said: always escaped.

import { views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, actionDialog, api, emptyState, esc, icon, may, MEMBERSHIP_ROLES, roleName, signedIn, state, time, toast, toastError, whileBusy } from "./ui.js";

// An organization's people: memberships mode, an administrator who signed in. All three checks:
// open mode's principal may do everything, `keys` included.
const people = () => state.accounts === "memberships" && signedIn() && may("keys");

// LP-12h appends {key: "keys", …} and {key: "clients", …}, shown: () => signedIn() && may("keys").
export const ADMIN_TABS = [
  { key: "members", label: "Members", shown: people, load: () => api("/api/auth/members"), show: showMembers },
  { key: "invitations", label: "Invitations", shown: people, load: () => api("/api/auth/invitations"), show: showInvitations },
  { key: "requests", label: "Requests to join", shown: people, load: () => api("/api/auth/access-requests"), show: showRequests },
];

// What the tabs show; `names`: each member's name by their id (who made what), `you`: the
// caller's own id (the members list marks it).
const adminState = { members: [], invitations: [], requests: [], names: new Map(), you: null };
let wanted = null; // the tab the address names, kept while the lists load

const shownTabs = () => ADMIN_TABS.filter((t) => t.shown());
const orgName = () => state.memberships.find((m) => m.org === state.me?.org)?.name ?? state.me?.org ?? "";

views.admin = async (main, params) => {
  wanted = params[0];
  const tabs = shownTabs();
  await load(tabs);
  main.innerHTML = `<h1>Access</h1>
    <p class="muted">Who can use ${esc(orgName())} and with which roles. Mendari sends no e-mail: you pass invitation links on yourself.</p>
    <nav class="segmented tabs" aria-label="Access views">${tabs.map((t) => `<a href="#admin/${t.key}" data-tab="${t.key}">${esc(t.label)}${
      t.key === "requests" ? ` <span class="count" id="requests-count">${adminState.requests.length || ""}</span>` : ""}</a>`).join("")}</nav>
    <div id="admin-body" class="admin-body"></div>`;
  const body = $("#admin-body");
  body.addEventListener("click", act);
  body.addEventListener("submit", invite);
  showTab(wanted);
};

views.admin.update = (params) => {
  wanted = params[0];
  showTab(wanted);
};

views.admin.available = () => ADMIN_TABS.some((t) => t.shown());

// The link leaves the page with the view: an open dialog (a link shown once among them) closes,
// but one whose request is running (`data-busy`, ui.js actionDialog) closes with its answer, so
// what follows it (a reload after changing your own roles) still happens.
views.admin.leave = () => document.querySelectorAll("dialog[data-admin]:not([data-busy])").forEach((d) => d.close());

async function load(tabs) {
  const lists = await Promise.all(tabs.map((t) => t.load()));
  tabs.forEach((t, i) => (adminState[t.key] = lists[i]));
  adminState.names = new Map(adminState.members.filter((m) => m.user_id).map((m) => [m.user_id, m.name || m.email]));
  adminState.you = adminState.members.find((m) => m.you)?.user_id ?? null;
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

const roleChoices = (checked) => `<fieldset class="role-choices"><legend>Roles</legend>${MEMBERSHIP_ROLES.map((r) => `<label>
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
  }[button.dataset.action] ?? [];
  const item = list && adminState[list].find((row) => String(row.id) === button.dataset.id);
  if (item) handle(item);
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

const STATUS = {
  open: ["Open", "s-warning"],
  accepted: ["Accepted", "s-ok"],
  revoked: ["Revoked", "o-ABSTAIN"],
  expired: ["Expired", "o-ABSTAIN"],
};

const statusBadge = (status) => {
  const [text, tone] = STATUS[status] ?? [status, ""];
  return `<span class="badge ${tone}" title="${esc(status)}">${esc(text)}</span>`;
};

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
  form.reset();
  showLink(made);
  await reload("invitations");
}

// The link, once: in a modal, in an input's value only (never an attribute or the page's text),
// gone with the dialog. Mendari keeps only a digest of it: a lost link is revoked and made again.
function showLink(made) {
  const dialog = document.createElement("dialog");
  dialog.className = "confirm";
  dialog.dataset.admin = "";
  dialog.setAttribute("aria-labelledby", "link-title");
  dialog.setAttribute("aria-describedby", "link-warning");
  const roles = made.roles.map(roleText).join(", ");
  dialog.innerHTML = `<form method="dialog">
      <h2 id="link-title">Invitation link for ${esc(made.email)}</h2>
      <p id="link-warning" class="callout warning"><strong>Shown once.</strong> Copy it now: Mendari keeps only a digest and can't show it again. Mendari sends no e-mail: send it to ${esc(made.email)} yourself.</p>
      <label for="invite-link">Link</label>
      <input id="invite-link" class="mono field-full" readonly>
      <p class="muted">Works once, for ${esc(made.email)} signed in with that address verified, until ${esc(time(made.expires_at))}. Roles: ${esc(roles)}.</p>
      <p class="muted" role="status" id="link-copied"></p>
      <div class="row mt-4"><span class="grow"></span>
        <button type="button" id="copy-link" autofocus>${icon("copy")}<span>Copy link</span></button>
        <button value="done" class="primary">Done</button>
      </div>
    </form>`;
  document.body.append(dialog);
  const input = dialog.querySelector("#invite-link");
  input.value = location.origin + made.link;
  const copy = dialog.querySelector("#copy-link");
  const said = dialog.querySelector("#link-copied");
  // Said inside the dialog: nothing under a modal is heard.
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      copy.innerHTML = `${icon("check")}<span>Copied</span>`;
      said.textContent = "Copied to the clipboard.";
    } catch {
      input.focus();
      input.select();
      said.textContent = "Copying isn't available here: the link is selected, copy it with Ctrl+C or Command+C.";
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

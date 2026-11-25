// SPDX-License-Identifier: Apache-2.0
// Memberships mode (MENDARI_ACCOUNTS=memberships, ADR-0008): what a person signed in sees while
// they act for no organization, instead of the sections. Holding memberships, they choose one
// (the server keeps the choice for this browser). Holding none, they see who they are signed in
// as and may ask to join an organization by its id: the server answers the same whether or not
// it exists, and this page never lists organizations. Holding one whose roles grant nothing
// here, they are told so; one who only grades written output, where no round holds work of
// theirs, is told what they do (where one does, they see the grading screen instead,
// written.js). Reached only through `state.noAccess` (router.js).

import { views } from "./router.js";
import { api, emptyState, esc, roleName, state, storageGet, storageSet, toast, toastError, whileBusy } from "./ui.js";

// core/accounts.py ORG_ID; the hyphen escaped, as a `pattern` (the `v` flag) needs it in a class.
const ORG_ID = "[a-z0-9][a-z0-9\\-]{1,62}";
const LAST_CHOSEN = "oh.org";

views.access = async (main, [reason = "none"]) => {
  const who = state.me ? `<p class="muted">Signed in as <b>${esc(state.me.actor)}</b>.</p>` : "";
  if (reason === "choose") return choose(main, who);
  if (reason === "label") {
    main.innerHTML = `<h1>Nothing to do here yet</h1>${who}${emptyState({
      icon: "shield-check",
      title: "You grade what agents write",
      text: "No round of written output has texts for you yet. Once one does, they are shown here.",
    })}${accountButtons()}`;
    wireAccount(main);
    return;
  }
  if (reason === "author") {
    main.innerHTML = `<h1>Nothing to do here yet</h1>${who}${emptyState({
      icon: "shield-check",
      title: "You write agents in your organization's agent store",
      text: "The agent store is not switched on on this deployment yet. Once it is, its screens appear here.",
    })}${accountButtons()}`;
    wireAccount(main);
    return;
  }
  if (reason === "nothing") {
    const held = state.memberships.find((m) => m.org === state.me?.org);
    main.innerHTML = `<h1>Nothing to do here yet</h1>${who}${emptyState({
      icon: "shield-x",
      title: `Your roles in ${held?.name ?? state.me?.org ?? ""} grant nothing yet`,
      text: "Ask one of its administrators for a role that reads or reviews."
        + (state.memberships.length > 1 ? " Your other organizations are in the top bar." : ""),
    })}${accountButtons()}`;
    wireAccount(main);
    return;
  }
  main.innerHTML = `<h1>You don't have access yet</h1>${who}${emptyState({
    icon: "shield-x",
    title: "You are not a member of any organization here",
    text: "An organization's administrators decide who joins it. Ask with its id below.",
  })}
    <form class="panel" id="ask-form">
      <h2>Ask to join</h2>
      <p class="muted" id="ask-hint">The organization's id, as it gave it to you: lower-case letters, digits and hyphens.</p>
      <div class="approve-form">
        <label for="ask-org">Organization id</label>
        <input id="ask-org" required pattern="${ORG_ID}" maxlength="63" autocomplete="off" spellcheck="false"
          placeholder="e.g. clinic-a" aria-describedby="ask-hint">
        <button type="submit" class="primary">Ask to join</button>
      </div>
    </form>${accountButtons()}`;
  main.querySelector("#ask-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const org = form.querySelector("#ask-org").value.trim();
    await whileBusy(form.querySelector("button[type=submit]"), "Asking…", async () => {
      try {
        await api("/api/auth/access-requests", { method: "POST", body: JSON.stringify({ org }) });
        toast("If that organization exists, your request is recorded.");
        form.reset();
      } catch (error) {
        toastError(error.message);
      }
    });
  });
  wireAccount(main);
};

// Several memberships (or one given since signing in) and none chosen: pick one. The one chosen
// last in this browser is offered first.
function choose(main, who) {
  const last = storageGet(LAST_CHOSEN, "");
  const checked = state.memberships.some((m) => m.org === last) ? last : state.memberships[0]?.org;
  const options = state.memberships.map((m) => `<label class="org-choice">
      <input type="radio" name="org" value="${esc(m.org)}"${m.org === checked ? " checked" : ""}>
      <span><b>${esc(m.name)}</b> <span class="muted">${esc(m.org)} · ${esc(m.roles.map(roleName).join(", ") || "no role")}</span></span>
    </label>`).join("");
  main.innerHTML = `<h1>Choose an organization</h1>${who}
    <form class="panel" id="choose-form">
      <fieldset class="org-choices"><legend>You are a member of</legend>${options}</fieldset>
      <div class="row mt-4"><button type="submit" class="primary">Continue</button></div>
    </form>${accountButtons()}`;
  main.querySelector("#choose-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const org = new FormData(event.currentTarget).get("org");
    await whileBusy(event.currentTarget.querySelector("button[type=submit]"), "Opening…", () => chooseOrganization(org));
  });
  wireAccount(main);
}

// Act for `org` from now on in this browser, then start over in it (the address keeps its view).
export async function chooseOrganization(org) {
  // What the view holds (a review override in its undo window) goes to the organization it was
  // made in, before the session acts for another.
  await views[state.view]?.leave?.();
  try {
    await api("/api/auth/organization", { method: "POST", body: JSON.stringify({ org }) });
    storageSet(LAST_CHOSEN, org);
    location.reload();
  } catch (error) {
    toastError(error.message);
  }
}

const accountButtons = () => `<div class="row mt-4">
    <button type="button" id="access-sign-out">Sign out</button>
    <button type="button" id="other-account">Use another account</button>
  </div>`;

function wireAccount(main) {
  const signOut = () => api("/auth/logout", { method: "POST" }).catch(() => null);
  main.querySelector("#access-sign-out").addEventListener("click", async () => {
    await signOut();
    location.assign("/auth/login");
  });
  main.querySelector("#other-account").addEventListener("click", async () => {
    await signOut();
    location.assign("/auth/login?account=choose");
  });
}

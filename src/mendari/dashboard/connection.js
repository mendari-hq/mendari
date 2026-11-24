// SPDX-License-Identifier: Apache-2.0
// An agent's connection (ADR-0021): the service that answers for it in this organization. One
// panel, shown on the agent's Decision Card and opened from the Agents view: where its answers
// come from now, and, for the organization's administrators, the address and token to set, a
// check that sends one synthetic example, and clearing it. The token is typed and never shown
// again: the server returns none, so the field is always empty when the panel opens.
// Everything the server says of an address or a person is escaped.

import { $, actionDialog, api, esc, icon, may, time, toast, toastError, whileBusy } from "./ui.js";

const agentPath = (packId) => `/api/connections/${packId.split("/").map(encodeURIComponent).join("/")}`;

export const readConnection = (packId) => api(agentPath(packId));

// Where an agent's answers come from now, in a few words, for a list and a badge.
export function connectionWords(c) {
  if (!c?.applies) return { text: "Decides on this server", tone: "" };
  if (c.source === "organization") return { text: "Connected", tone: "s-ok" };
  if (c.source === "deployment") return { text: "Connected by the deployment", tone: "s-ok" };
  if (c.source === "agent") return { text: "Connected by its own settings", tone: "s-ok" };
  if (c.source === "mixed") return { text: "Partly connected", tone: "s-warning" };
  return { text: "Not connected", tone: "s-warning" };
}

function whereFrom(c) {
  const own = c.connection;
  if (c.source === "organization" && own) {
    return `<p><b>Connected</b> to <code class="break-anywhere">${esc(own.endpoint ?? own.origin)}</code>.</p>
      <dl class="facts">
        <dt>Address set</dt><dd>${esc(time(own.set_at))} by ${esc(own.set_by)}</dd>
        <dt>Token</dt><dd>${own.token === "set"
          ? `Set ${esc(time(own.token_set_at))} by ${esc(own.token_set_by)}. It is kept sealed and is never shown again.`
          : "None: calls are sent without one."}</dd>
      </dl>`;
  }
  if (c.source === "organization") {
    return `<p class="callout danger"><b>The saved connection cannot be read.</b> Its token does not open with this deployment's keys,
      so every case goes to a person. Set the address and the token again.</p>`;
  }
  if (c.source === "deployment") {
    return `<p><b>Connected by the deployment.</b> This agent answers through the service whoever runs this deployment configured for every agent.${
      c.may_set ? " You can set your organization's own below; it is then used instead." : ""}</p>`;
  }
  if (c.source === "agent") return "<p><b>Connected by its own settings.</b> This agent names the address of its service itself.</p>";
  if (c.source === "mixed") return "<p><b>Partly connected.</b> Some of its questions have a live service and some answer from canned examples.</p>";
  return `<p class="callout warning"><b>Not connected.</b> This agent answers from the canned examples it carries, so a person decides every case.
    Connect the service that runs it to let it answer for real.</p>`;
}

function rules(c) {
  const reach = c.allowed_origins === null
    ? "The address must be on the public internet. For a service on your own network, whoever runs this deployment lists it first (MENDARI_PROVIDER_ALLOWED_ORIGINS)."
    : c.allowed_origins.length
      ? `This deployment may call: ${c.allowed_origins.map((origin) => `<code>${esc(origin)}</code>`).join(", ")}. Another address is added by whoever runs it.`
      : "This deployment lists no address it may call: whoever runs it adds one first (MENDARI_PROVIDER_ALLOWED_ORIGINS).";
  const token = c.seals
    ? "The token is kept sealed and never shown again. Changing the address removes it unless you type one."
    : "This deployment cannot keep a token (it has no key to seal one under): save an address without a token, or ask whoever runs it to set MENDARI_PHI_MODE=encrypt.";
  return `<p class="muted">${reach}</p><p class="muted">${esc(token)}</p>`;
}

// The panel's HTML for one agent's state (`GET /api/connections/<agent>`).
export function connectionHtml(c, { heading = "h3" } = {}) {
  if (!c.applies) {
    return `<p class="prose">This agent decides on this server, with rules and local models: it has no service to connect.</p>`;
  }
  const own = c.source === "organization" && c.connection;
  const form = c.may_set ? `<form class="admin-form connection-form" data-connection-form novalidate>
      <${heading}>${own ? "Change the connection" : "Connect its service"}</${heading}>
      <label for="connection-endpoint">Address of the service</label>
      <input id="connection-endpoint" name="endpoint" type="url" inputmode="url" autocomplete="off" spellcheck="false" required
        maxlength="2048" placeholder="https://agents.example.org/decide" value="${esc(own ? c.connection.endpoint ?? "" : "")}"
        aria-describedby="connection-rules">
      <label for="connection-token">Token${own && c.connection.token === "set" ? " (leave empty to keep the one that is set)" : " (optional)"}</label>
      <input id="connection-token" name="token" type="password" autocomplete="new-password" spellcheck="false" maxlength="4096"${c.seals ? "" : " disabled"}
        aria-describedby="connection-rules">
      <div id="connection-rules">${rules(c)}</div>
      <p class="error" role="alert" hidden></p>
      <div class="row">
        <button type="submit" class="primary">Save connection</button>
        ${own ? '<button type="button" data-connection-clear>Clear…</button>' : ""}
      </div>
    </form>` : c.enabled
    ? `<p class="muted">An administrator of your organization sets or changes it.</p>`
    : `<p class="muted">Connections are set by whoever runs this deployment: setting them here is switched off.</p>`;
  // Checking sends a request from this server: the organization's administrators', as setting is.
  const check = may("keys")
    ? `<div class="row mt-3"><button type="button" data-connection-check>${icon("activity")} Check connection</button>
        <span class="muted">Sends one synthetic example the agent carries. Never a patient's case.</span></div>
      <div data-connection-result aria-live="polite"></div>`
    : "";
  return `<div class="connection" data-connection>${whereFrom(c)}${form}${check}</div>`;
}

function resultHtml(found) {
  const rows = found.checked.map((item) => {
    const facts = [item.latency_ms != null && item.ok ? `${item.latency_ms} ms` : null, item.model ? `model ${item.model}` : null,
      item.agent_version ? `version ${item.agent_version}` : null].filter(Boolean).join(" · ");
    return `<li><b>${item.ok ? "Answered." : "Did not answer."}</b> ${esc(item.said)}${facts ? ` <span class="muted">${esc(facts)}</span>` : ""}
      <span class="mono muted">${esc(item.slot)}</span></li>`;
  }).join("");
  return `<div class="callout ${found.ok ? "ok" : "danger"}" role="status">
    <p><b>${found.ok ? "The connection works." : "The connection does not work yet."}</b>
      Checked with the example “${esc(found.example)}”.</p><ul>${rows}</ul></div>`;
}

// Wire a panel `connectionHtml` made for `packId`, inside `root`; `changed()` runs after the
// connection was saved or cleared, to show it again.
export function wireConnection(root, packId, changed) {
  const panel = root.querySelector("[data-connection]");
  if (!panel) return;
  const form = panel.querySelector("[data-connection-form]");
  const error = form?.querySelector(".error");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    error.hidden = true;
    const endpoint = form.endpoint.value.trim();
    if (!endpoint) {
      error.textContent = "Type the address of the service, starting with https://";
      error.hidden = false;
      form.endpoint.focus();
      return;
    }
    const body = { endpoint };
    if (form.token.value) body.token = form.token.value; // left empty: the one that is set stays
    whileBusy(form.querySelector('button[type="submit"]'), "Saving…", async () => {
      try {
        await api(agentPath(packId), { method: "PUT", body: JSON.stringify(body) });
        form.token.value = "";
        toast("Connection saved");
        await changed();
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      }
    });
  });
  panel.querySelector("[data-connection-clear]")?.addEventListener("click", async () => {
    const cleared = await actionDialog({
      title: "Clear this connection?",
      body: `<p>The address and the token are removed. The agent then answers as it did before one was set: through the
        deployment's own service if it has one, else from its canned examples, where a person decides every case.</p>`,
      action: "Clear",
      danger: true,
      run: () => api(agentPath(packId), { method: "DELETE" }),
    });
    if (!cleared) return;
    toast("Connection cleared");
    await changed();
  });
  panel.querySelector("[data-connection-check]")?.addEventListener("click", (event) => {
    const box = panel.querySelector("[data-connection-result]");
    whileBusy(event.currentTarget, "Checking…", async () => {
      try {
        box.innerHTML = resultHtml(await api(`${agentPath(packId)}/check`, { method: "POST" }));
      } catch (failure) {
        box.innerHTML = `<p class="callout danger" role="alert">${esc(failure.message)}</p>`;
      }
    });
  });
}

// The panel in a dialog, for an agent that is not the one the page shows (the Agents view).
// Resolves once it is closed; `changed()` runs after each save or clear.
export async function connectionDialog(packId, name, changed = () => {}) {
  let state;
  try {
    state = await readConnection(packId);
  } catch (failure) {
    toastError(failure.message);
    return;
  }
  const dialog = document.createElement("dialog");
  dialog.className = "confirm connection-dialog";
  dialog.dataset.admin = "";
  dialog.setAttribute("aria-labelledby", "connection-dialog-title");
  const paint = () => {
    dialog.innerHTML = `<h2 id="connection-dialog-title">Connection of ${esc(name)}</h2>
      ${connectionHtml(state)}
      <div class="row mt-4"><span class="grow"></span><button type="button" data-close autofocus>Close</button></div>`;
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    wireConnection(dialog, packId, async () => {
      state = await readConnection(packId);
      paint();
      await changed();
    });
  };
  document.body.append(dialog);
  paint();
  await new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve();
    });
    dialog.showModal();
  });
}

// The Decision Card's section: read for the agent shown, painted into `#card-connection-body`.
export async function cardConnection(packId) {
  const body = $("#card-connection-body");
  if (!body) return;
  const paint = async () => {
    try {
      const state = await readConnection(packId);
      if (!body.isConnected) return;
      body.innerHTML = connectionHtml(state);
      wireConnection(body, packId, paint);
    } catch (failure) {
      if (body.isConnected) body.innerHTML = `<p class="muted">Its connection could not be read: ${esc(failure.message)}</p>`;
    }
  };
  await paint();
}

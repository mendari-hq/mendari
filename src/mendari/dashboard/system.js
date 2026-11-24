// SPDX-License-Identifier: Apache-2.0
// The System view ("#system"; "#system/<check>" lands on one check, "#system/erase/<decision>"
// on erasing that decision's kept input): what this deployment needs to run safely, as a
// checklist someone in a hospital's IT can act on, and the operator's actions that needed a
// terminal before.
//
// The checks come from the server (GET /api/system/checks: each one's state, one sentence on
// what is so, what to do about it, and when); nothing here works a state out again. The actions
// are the engine's own routes, each refused by the server to anyone without the right, and
// recorded as the engine records it:
// - verify the audit trail now (POST /api/system/audit/verify);
// - send a test alert (POST /api/system/alerts/test);
// - check an EHR's connection (POST /api/system/ehr-check);
// - hold all automation, and lift a hold (POST /api/holds, DELETE /api/holds/{id}): asked about
//   first, with a reason, and chained;
// - erase what a decision keeps of its input (POST /api/audit/{id}/erase): asked about first,
//   and chained.
// A served version its registry recalled is said at the top.
//
// Who sees it: the deployment's operators (server/auth.py `operate`: an administrator of the
// organization that operates it, signed in as a person; in open mode, everyone). The router
// asks `available()`, and the section list hides its link from everyone else.
//
// `setupPanel` is the Overview's "Finish setting up": the same checks, the ones still to do, in
// order, while the deployment is new.
//
// Everything a check says is the server's own wording about this deployment, and every name
// here (an agent, an EHR's address, who set a hold) is what a person typed or a server
// recorded: always escaped.

import { render, views } from "./router.js";
import { $, actionDialog, api, esc, focusHeading, icon, may, signedIn, stat, state, time, toast, toastError, whileBusy } from "./ui.js";

// A check's state (server/system.py `STATES`) in words, with a mark: never colour alone.
export const CHECK_STATES = {
  ok: { label: "In order", badge: "s-ok", mark: "check" },
  attention: { label: "Needs attention", badge: "s-warning", mark: "alert" },
  not_set: { label: "Not set up", badge: "o-ABSTAIN", mark: "settings" },
  not_applicable: { label: "Does not apply", badge: "o-ABSTAIN", mark: "" },
};
const TO_DO = ["attention", "not_set"];
// An EHR check's findings (server/smart_check.py), in the same words.
const FINDINGS = {
  ok: { label: "In order", badge: "s-ok" },
  warn: { label: "Check this", badge: "s-warning" },
  unknown: { label: "Ask the EHR", badge: "o-ABSTAIN" },
  fail: { label: "Would be refused", badge: "s-degraded" },
};

const operator = () => may("operate");
const stateBadge = (key) => {
  const shown = CHECK_STATES[key] ?? CHECK_STATES.attention;
  return `<span class="badge ${shown.badge} check-state">${shown.mark ? icon(shown.mark) : ""}${esc(shown.label)}</span>`;
};
// The view where agents are found and adopted, when this console has one (it registers itself
// as `agents`); the checks about agents and the registry point there.
const agentsView = () => (views.agents && (views.agents.available?.() ?? true) ? "#agents" : null);
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// ---- the view -----------------------------------------------------------------------------------

views.system = async (main, params) => {
  const [checks, holds, health] = await Promise.all([
    api("/api/system/checks"),
    api("/api/holds").catch(() => []),
    api("/api/health").catch(() => null),
  ]);
  const areas = [];
  for (const check of checks.checks) {
    const area = areas.find((a) => a.name === check.area) ?? areas[areas.push({ name: check.area, checks: [] }) - 1];
    area.checks.push(check);
  }
  const { ok, attention, not_set: notSet } = checks.summary;
  const ehrs = health?.smart_ehrs ?? [];
  // In an element of its own, made anew each time: what listens for its clicks goes with it.
  main.innerHTML = `<div id="system-view">
    <h1>System</h1>
    <p class="muted">What this deployment needs to run safely, and what to do about each item.
      Checked ${esc(time(checks.checked_at))}. <button type="button" id="system-recheck">Check again</button></p>
    ${recallBanner(checks.recalls)}
    <div class="stats-primary" aria-label="The checks, counted">
      ${stat({ label: "In order", value: String(ok), foot: plural(checks.checks.length, "check") })}
      ${stat({ label: "Need attention", value: String(attention), tone: attention ? "warning" : "", foot: attention ? "Each says what to do" : "Nothing to look into" })}
      ${stat({ label: "Not set up", value: String(notSet), foot: notSet ? "Optional, or still to do" : "Everything is set up" })}
    </div>
    ${automationPanel(holds)}
    ${areas.map((area, index) => `
      <section class="panel" aria-labelledby="system-area-${index}">
        <h2 id="system-area-${index}">${esc(area.name)}</h2>
        <ul class="check-list">${area.checks.map((check) => checkRow(check, ehrs)).join("")}</ul>
      </section>`).join("")}
    ${erasePanel(health)}
  </div>`;
  const root = $("#system-view");
  wireSystem(root, holds);
  return landOn(root, params);
};
views.system.available = operator;
views.system.packless = true; // it reads no agent's details
views.system.update = (params) => focusHeading(landOn($("#system-view"), params) ?? $("main h1"));

// Where an address lands: a check ("#system/alerts"), or the erase form with a decision's id
// in it ("#system/erase/<id>"). Returns the heading to focus, if any.
function landOn(root, params) {
  const [name, id] = params;
  root.querySelectorAll(".check-row.target").forEach((row) => row.classList.remove("target"));
  if (name === "erase") {
    const input = root.querySelector("#erase-decision");
    if (input && id) input.value = id;
    return root.querySelector("#system-erase-title");
  }
  const row = name ? root.querySelector(`.check-row[data-check="${CSS.escape(name)}"]`) : null;
  row?.classList.add("target");
  return row?.querySelector("h3") ?? null;
}

// A served version its registry recalled no longer automates: said first, with why.
function recallBanner(recalls) {
  if (!recalls.length) return "";
  const where = agentsView();
  return `<div class="callout danger" role="status" id="system-recalls">
    <b>${recalls.length === 1 ? "A version this deployment serves was recalled by its registry." : `${recalls.length} versions this deployment serves were recalled by their registry.`}</b>
    A recalled version no longer automates: every decision it makes goes to a person.
    <ul>${recalls.map((r) => `<li><b>${esc(r.agent)}</b> ${esc(r.version)}: ${esc(r.reason)}</li>`).join("")}</ul>
    ${where ? `<a class="button" href="${where}">Adopt the fixed version</a>` : "Adopt the fixed version from its registry."}
  </div>`;
}

function checkRow(check, ehrs) {
  return `<li class="check-row" data-check="${esc(check.key)}" data-state="${esc(check.state)}">
    ${stateBadge(check.state)}
    <div class="check-body">
      <h3>${esc(check.label)}</h3>
      <p>${esc(check.detail)}</p>
      ${check.action ? `<p><b>What to do:</b> ${esc(check.action)}</p>` : ""}
      ${check.at ? `<p class="muted">As of ${esc(time(check.at))}.</p>` : ""}
      ${check.key === "ehr" ? ehrList(ehrs) : ""}
    </div>
    <div class="check-actions">${checkActions(check)}</div>
  </li>`;
}

// What can be done about a check from here: an action of the engine's, or the view where it
// is done.
function checkActions(check) {
  const agents = agentsView();
  switch (check.key) {
    case "operators":
      return views.admin?.available?.() ? `<a class="button" href="#admin/members">Open Access</a>` : "";
    case "audit_chain":
      return `<button type="button" data-act="verify">Verify now</button>`;
    case "checkpoint_signing":
      return check.state === "ok" ? `<a class="button" href="#audit">Open the audit trail</a>` : "";
    case "alerts":
      return check.state === "not_set" ? "" : `<button type="button" data-act="test-alert">Send a test alert</button>`;
    case "registry":
    case "agents":
      return agents ? `<a class="button" href="${agents}">Open Agents</a>` : "";
    default:
      return "";
  }
}

// The EHRs registered to this organization, each with its own check.
function ehrList(ehrs) {
  if (!ehrs.length) return "";
  return `<ul class="check-findings">${ehrs.map((ehr, index) => `<li data-ehr="${index}">
    <span class="row"><code class="break-anywhere">${esc(ehr.base)}</code>
      <button type="button" data-act="ehr-check" data-base="${esc(ehr.base)}">Check the connection</button></span>
    <div class="ehr-result" aria-live="polite"></div></li>`).join("")}</ul>`;
}

// ---- automation: hold everything, lift a hold --------------------------------------------------

const orgName = () => state.memberships.find((m) => m.org === state.me?.org)?.name ?? null;

function automationPanel(holds) {
  if (!may("approve")) return "";
  const everything = holds.filter((h) => !h.pack_id);
  const some = holds.filter((h) => h.pack_id);
  const listed = holds.map((h) => {
    const covers = h.pack_id ? `<b>${esc(h.pack_id)}</b>` : "every agent";
    // A hold of the deployment's (set from the command line, for every organization) is lifted
    // there; in memberships mode it comes without its reason (ADR-0008).
    if (!h.org) {
      return `<div class="callout danger"><b>Automation is on hold</b> for ${covers}, for the whole deployment, since ${esc(time(h.set_at))}:
        set from this machine's command line${h.reason === undefined ? "" : ` (${esc(h.reason)})`}.
        It is lifted there: <code>mendari automation lift ${esc(h.id)}</code>
        (on a one-machine install, <code>./mendari-site lift ${esc(h.id)}</code>).</div>`;
    }
    return `<div class="callout danger"><b>Automation is on hold</b> for ${covers}: ${esc(h.reason)}.
      Set by ${esc(h.set_by)}, ${esc(time(h.set_at))}.
      <button type="button" data-lift="${esc(h.id)}">Lift…</button></div>`;
  }).join("");
  const running = everything.length ? "" : `<p>${some.length
    ? "Other agents automate what their policies allow."
    : "Agents automate what their policies allow, their approvals and their risk level's limits."}
    Holding all automation sends every decision to a person at once, without stopping anything.</p>
    <button type="button" class="danger" data-act="hold-all">Hold all automation…</button>`;
  return `<section class="panel" aria-labelledby="system-automation-title" id="system-automation">
    <h2 id="system-automation-title">Automation</h2>
    ${listed}${running}
  </section>`;
}

// What holding everything does, in the words of the dialog that asks first.
function holdText() {
  const whose = orgName() ? `of <b>${esc(orgName())}</b>` : "your organization serves";
  return `<p>Every agent ${whose} stops deciding on its own, at once: a decision that would have been
      automatic goes to a person instead, with the same recommendation and priority. Decisions
      still run and are recorded, and nothing is switched off.</p>
    <p>It lasts until someone lifts it here. The hold, its reason and who set it are kept in the audit trail.</p>
    <label for="hold-all-reason">Why, in a few words</label>
    <input id="hold-all-reason" name="reason" maxlength="200" autocomplete="off" placeholder="e.g. investigating a missed emergency">`;
}

function liftText(hold) {
  return `<p>${hold?.pack_id ? `<b>${esc(hold.pack_id)}</b> decides` : "Every agent decides"} on its own again:
      what a policy automates is no longer sent to a person because of this hold.</p>
    <p>Another hold, a recall by a registry, an approval an agent still needs and its risk level's
      limits keep applying. The lift is kept in the audit trail.</p>`;
}

// ---- erasing a decision's kept input ------------------------------------------------------------

function erasePanel(health) {
  if (!may("admin")) return "";
  const keeps = health?.phi_mode === "encrypt";
  const body = keeps ? `
    <p>On a patient's request, or for a case recorded by mistake: what the decision keeps apart from the
      audit trail (its sealed input and evidence, and its notes) is deleted for good. The decision's
      record and the audit trail stay, and still verify. A decision's id is on its record in the
      <a href="#audit">audit trail</a>.</p>
    <form class="approve-form" id="erase-form">
      <label for="erase-decision">Decision id</label>
      <input id="erase-decision" required maxlength="200" autocomplete="off" spellcheck="false">
      <label for="erase-reason">Reason</label>
      <select id="erase-reason">
        <option value="request">A request to erase it</option>
        <option value="error">Recorded by mistake</option>
      </select>
      <button type="submit" class="danger">Erase…</button>
    </form>
    <div id="erase-result" aria-live="polite"></div>` : `
    <p class="muted">This deployment keeps no input: only a fingerprint of each case is stored, so there is nothing to erase.</p>`;
  return `<section class="panel" aria-labelledby="system-erase-title" id="system-erase">
    <h2 id="system-erase-title">Erase a decision's kept input</h2>${body}
  </section>`;
}

function eraseText(id) {
  return `<p>What decision <code class="break-anywhere">${esc(id)}</code> keeps of its input is deleted for good:
      nobody can read it here again, a reviewer included.</p>
    <p>Backups made before now still hold it until they are dropped, and so does anything already
      exported. The erasure, its reason and who asked are kept in the audit trail.</p>`;
}

// ---- actions ------------------------------------------------------------------------------------

function wireSystem(root, holds) {
  const again = () => { if (root.isConnected) render(); };
  $("#system-recheck").addEventListener("click", again);

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-act], button[data-lift]");
    if (!button || !root.contains(button)) return;
    if (button.dataset.lift) {
      const id = button.dataset.lift;
      const lifted = await actionDialog({
        title: "Lift this automation hold?",
        body: liftText(holds.find((h) => h.id === id)),
        action: "Lift",
        danger: true,
        run: () => api(`/api/holds/${encodeURIComponent(id)}`, { method: "DELETE" }),
      });
      if (!lifted) return;
      toast("Hold lifted");
      return again();
    }
    switch (button.dataset.act) {
      case "verify":
        return whileBusy(button, "Verifying…", () => verifyNow(again));
      case "test-alert":
        return whileBusy(button, "Sending…", () => testAlert(again));
      case "ehr-check":
        return whileBusy(button, "Checking…", () => ehrCheck(button));
      case "hold-all": {
        const held = await actionDialog({
          title: "Hold all automation?",
          body: holdText(),
          action: "Hold all automation",
          danger: true,
          run: (form) => {
            const reason = form.elements.reason.value.trim();
            if (!reason) throw new Error("Say why, in a few words: it is kept with the hold.");
            return api("/api/holds", { method: "POST", body: JSON.stringify({ reason }) });
          },
        });
        if (!held) return;
        toast("All automation is held: a person reviews every decision");
        return again();
      }
      default:
        return undefined;
    }
  });

  root.querySelector("#erase-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = $("#erase-decision").value.trim();
    const reason = $("#erase-reason").value;
    if (!id) return;
    const erased = await actionDialog({
      title: "Erase this decision's kept input?",
      body: eraseText(id),
      action: "Erase",
      danger: true,
      run: () => api(`/api/audit/${encodeURIComponent(id)}/erase`, { method: "POST", body: JSON.stringify({ reason }) }),
    });
    const out = $("#erase-result");
    if (!erased || !out) return;
    const notes = erased.notes?.length ?? 0;
    const what = [erased.input ? "its sealed input and evidence" : "", notes ? plural(notes, "note") : ""].filter(Boolean).join(" and ");
    out.innerHTML = `<div class="callout ok"><b>Erased:</b> ${esc(what || "what it kept")}.
      ${erased.still_in_chain ? "Its record in the audit trail itself still holds its input, kept there under PHI mode off: nothing erases that without breaking the trail." : "Its record and the audit trail stay."}</div>`;
    toast("Erased");
    $("#erase-decision").value = "";
  });
}

async function verifyNow(again) {
  try {
    const result = await api("/api/system/audit/verify", { method: "POST" });
    if (result.ok) toast(`The audit trail verifies: ${plural(result.events, "event")} checked`);
    else toastError("The audit trail does not verify. Hold all automation, keep the database as it is, and follow the incident runbook.");
    again();
  } catch (error) {
    toastError(error.message);
  }
}

async function testAlert(again) {
  try {
    const sent = await api("/api/system/alerts/test", { method: "POST" });
    if (sent.delivered) toast("The test alert was delivered: check that it arrived where you expect");
    else toastError(`The test alert was not delivered (${sent.status ? `the receiver answered ${sent.status}` : sent.error ?? "no answer"}).`);
    again();
  } catch (error) {
    toastError(error.message);
  }
}

// One EHR's findings, shown under it: what a launch from it would meet here.
async function ehrCheck(button) {
  const out = button.closest("li")?.querySelector(".ehr-result");
  try {
    const result = await api("/api/system/ehr-check", { method: "POST", body: JSON.stringify({ base: button.dataset.base }) });
    if (!out?.isConnected) return;
    out.innerHTML = `<p class="mt-2"><b>${result.ok ? "A launch from this EHR would be accepted." : "A launch from this EHR would be refused."}</b></p>
      <ul class="check-findings">${result.findings.map((f) => {
        const shown = FINDINGS[f.status] ?? FINDINGS.unknown;
        return `<li><span class="badge ${shown.badge}">${esc(shown.label)}</span> <b>${esc(f.check)}</b>: ${esc(f.detail)}</li>`;
      }).join("")}</ul>`;
    if (result.ok) toast("The EHR connection is in order");
    else toastError("The EHR connection has something that would refuse its launches");
  } catch (error) {
    toastError(error.message);
  }
}

// ---- the Overview's "Finish setting up" ---------------------------------------------------------

// The checks behind the first-run panel: asked for by an operator who signed in, while the
// deployment is new (no decision yet, only the bundled demo agent) and something is left to
// do. Once it is no longer new, or everything passes, nothing more is asked in this window.
let setupOver = false;

export async function setupChecks() {
  if (setupOver || !signedIn() || !operator()) return null;
  const checks = await api("/api/system/checks").catch(() => null);
  if (!checks) return null;
  const showing = checks.setup.fresh && !checks.setup.finished;
  if (!showing) setupOver = true;
  return showing ? checks : null;
}

// Where a step is done: the view that does it, else its check in the System view.
function stepAction(check) {
  if (check.key === "operators" && views.admin?.available?.()) return { href: "#admin/members", label: "Open Access" };
  if (check.key === "agents" && agentsView()) return { href: agentsView(), label: "Open Agents" };
  return { href: `#system/${encodeURIComponent(check.key)}`, label: "Open in System" };
}

// What is left, in the checks' own order, ending with adopting an agent: that is where setting
// up ends. Each step says what is so and what to do, in the server's words.
export function setupPanel(checks) {
  if (!checks) return "";
  const left = checks.checks.filter((c) => TO_DO.includes(c.state));
  const steps = [...left.filter((c) => c.key !== "agents"), ...left.filter((c) => c.key === "agents")];
  const done = checks.checks.length - left.length;
  return `<section class="panel setup" aria-labelledby="setup-title" id="setup">
    <div class="row adoption-head">
      <h2 id="setup-title">Finish setting up</h2>
      <span class="muted">${done} of ${checks.checks.length} checks in order</span>
    </div>
    <p class="muted">This deployment is new. What is left, in the order to do it; <a href="#system">System</a> shows every check.</p>
    <ol class="adoption-steps">${steps.map((check, index) => {
      const action = stepAction(check);
      const current = index === 0;
      return `<li class="adoption-step todo${current ? " current" : ""}" ${current ? 'aria-current="step"' : ""} data-step="${esc(check.key)}">
        <span class="adoption-mark" aria-hidden="true">${index + 1}</span>
        <div class="adoption-body">
          <p class="adoption-title"><b>${esc(check.label)}</b> <span class="sr-only">(${esc(CHECK_STATES[check.state].label.toLowerCase())}${current ? ", next step" : ""})</span></p>
          <p class="muted">${esc(check.detail)}${check.action ? ` ${esc(check.action)}` : ""}</p>
        </div>
        <a class="button${current ? " primary" : ""} adoption-action" href="${action.href}">${esc(action.label)}</a>
      </li>`;
    }).join("")}</ol>
  </section>`;
}

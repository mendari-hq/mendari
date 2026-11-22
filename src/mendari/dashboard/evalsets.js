// SPDX-License-Identifier: Apache-2.0
// The hidden evaluation sets' screen (`#evalsets[/<set id>[/<tab>]]`, "Hidden sets" in the section
// list; LP-11n, ADR-0011): an administrator who signed in (`curate`), in memberships mode where
// the deployment has the evaluation-set keys, runs and follows the blind labelling of their
// organization's hidden sets, over its curators' routes (LP-11k) and the members list only. The
// list: each set's name, pack, state and cases. A set: its facts (state, commitment, evaluation
// runs used and left) and its tabs, the Access view's pattern (admin.js) copied, not imported:
// Progress (its cases by status, and each labeller's assignments by name from the members list),
// Sample, Assign and Adjudicate (a draft's only: drawing its cases and giving them to labellers,
// never an administrator), Agreement (percent agreement as k of n with its exact interval, each
// pair's kappa and the pool's alpha, "not estimable" in words) and Coverage (counts by label, the
// critical labels with the insufficient-evidence mark, the mix of languages, sources and strata).
// It shows what those routes answer: ids, counts, user ids (by name) and the aggregates the key
// holder stored; never a case, an input, a label given, an attribute, a case id or a case hash.
// Making a set, measuring it, freezing and retiring stay the key holder's commands. Names, packs
// and label words are what people wrote: always escaped.

import { render, views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, api, emptyState, esc, evidenceMark, figureHtml, may, num, signedIn, slotText, stat, state, time, toast, toastError, whileBusy } from "./ui.js";

const SETS = "/api/evalsets";
const MEMBERS = "/api/auth/members";
// Who may be given a case (core/accounts.py LABELLER and CURATING; a test keeps them equal): a
// labeller holding no role that manages hidden sets. The server refuses anyone else; this only
// leaves them out of the choice.
const LABELLER = "labeller";
const CURATING = ["admin"];
// A critical label with fewer distinct positives is too few to measure (evals/result.py
// MIN_POSITIVES; a test keeps them equal). The report marks each one; this is its wording.
const MIN_POSITIVES = 100;

// An administrator who signed in, in memberships mode, where the deployment has the evaluation-set
// keys (`/api/health` names their ids to a signed-in reader): its curators' routes are mounted
// then, and only then (server/app.py). Never a labeller, another role, a key or open mode.
const curating = () => state.accounts === "memberships" && signedIn() && may("curate") && state.evalsets;

// The set shown, as last read (`set`; null while one loads), and the organization's members
// (`members`; `names`: each one's name by user id). `wanted`: the tab the address names.
// `asked`: the latest read of the set; an older answer, arriving after it, is dropped.
const current = { set: null, members: [], names: new Map() };
let wanted = null;
let asked = 0;

const orgName = () => state.memberships.find((m) => m.org === state.me?.org)?.name ?? state.me?.org ?? "";
const setPath = (id) => `${SETS}/${encodeURIComponent(id)}`;
const setLink = (id, tab = "") => `#evalsets/${encodeURIComponent(id)}${tab ? `/${tab}` : ""}`;
const counted = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

views.evalsets = async (main, [setId, tab]) => (setId ? showSet(main, setId, tab) : showSets(main));

// Another tab of the set shown: the set read again first (its labellers work meanwhile), the tab
// shown by the latest read; another set, the list, or any address while a set loads: the view
// anew (renders run in turn, so the last address asked for is the one shown).
views.evalsets.update = async ([setId, tab]) => {
  if (!setId || setId !== current.set?.set) return render({ navigated: true });
  wanted = tab;
  if (await refresh()) showTab(wanted);
};

views.evalsets.available = curating;

// An organization's sets, not a pack's: shown without waiting for the pack's details (router.js).
views.evalsets.packless = true;

// -- the list ---------------------------------------------------------------------------------------

const STATES = {
  draft: { text: "Draft", tone: "s-warning", about: "being labelled: its cases are drawn and assigned here" },
  frozen: { text: "Frozen", tone: "o-ABSTAIN", about: "its content is fixed: nothing is drawn or assigned any more" },
  active: { text: "In use", tone: "s-ok", about: "frozen, and in use for evaluation runs" },
  retired: { text: "Retired", tone: "o-ABSTAIN", about: "no longer in use" },
};

const stateBadge = (key) => {
  const shown = STATES[key] ?? { text: key, tone: "" };
  return `<span class="badge ${shown.tone}" title="${esc(key)}">${esc(shown.text)}</span>`;
};

async function showSets(main) {
  current.set = null;
  const sets = await api(SETS);
  const intro = `<h1>Hidden sets</h1>
    <p class="muted">The hidden evaluation sets of ${esc(orgName())}: draw their cases, give them to
      labellers blind and follow the labelling. The key holder makes them, measures them, freezes
      and retires them from the command line.</p>`;
  if (!sets.length) {
    main.innerHTML = intro + emptyState({
      icon: "inbox",
      title: "No hidden sets yet",
      text: "The key holder makes one from the command line (<code>mendari evalset create</code>). It shows here once made.",
    });
    return;
  }
  main.innerHTML = intro + tableHtml({
    id: "evalsets",
    caption: `Hidden sets of ${esc(orgName())}, oldest first`,
    columns: [
      {
        key: "name", label: "Set", sort: (s) => s.name.toLowerCase(),
        cell: (s) => `<a class="set-link" href="${esc(setLink(s.set))}">${esc(s.name)}</a><br><code class="mono">${esc(s.set)}</code>`,
      },
      { key: "pack", label: "Agent", sort: (s) => s.pack, cell: (s) => `<code>${esc(s.pack)}</code>` },
      { key: "state", label: "State", sort: (s) => Object.keys(STATES).indexOf(s.state), cell: (s) => stateBadge(s.state) },
      { key: "cases", label: "Cases", numeric: true, sort: (s) => s.cases, cell: (s) => esc(s.cases) },
      { key: "made", label: "Made", className: "nowrap", sort: (s) => s.created_at, cell: (s) => esc(time(s.created_at)) },
    ],
    rows: sets,
  });
  wire(main);
}

// -- a set and its tabs -------------------------------------------------------------------------------

const draft = () => current.set?.state === "draft"; // only a draft is drawn and assigned

const EVALSET_TABS = [
  { key: "progress", label: "Progress", shown: () => true, show: showProgress },
  { key: "sample", label: "Sample", shown: draft, show: showSample },
  { key: "assign", label: "Assign", shown: draft, show: showAssign },
  { key: "adjudicate", label: "Adjudicate", shown: draft, show: showAdjudicate },
  { key: "agreement", label: "Agreement", shown: () => true, show: showAgreement },
  { key: "coverage", label: "Coverage", shown: () => true, show: showCoverage },
];

const shownTabs = () => EVALSET_TABS.filter((t) => t.shown());

const tabLinks = () => shownTabs().map((t) =>
  `<a href="${esc(setLink(current.set.set, t.key))}" data-tab="${t.key}">${esc(t.label)}</a>`).join("");

// An unknown set and another organization's are one `404` (the server's): said as such.
const unknown = (error) => {
  if (error.status === 404) return null;
  throw error;
};

async function showSet(main, setId, tab) {
  current.set = null; // none shown until this one is: an address meanwhile renders anew
  wanted = tab;
  const [set, members] = await Promise.all([api(setPath(setId)).catch(unknown), api(MEMBERS)]);
  // An id the browser resolves to another path ("." asks for the list) is no set either.
  if (!set || set.set !== setId) {
    main.innerHTML = `<h1>Hidden sets</h1>${emptyState({
      icon: "inbox",
      title: "No such set",
      text: `It is not one of the hidden sets of ${esc(orgName())}.`,
      action: { href: "#evalsets", label: "All hidden sets" },
    })}`;
    return;
  }
  current.set = set;
  current.members = members;
  current.names = new Map(members.filter((m) => m.user_id).map((m) => [m.user_id, m.name || m.email || m.user_id]));
  // A set name is free text: one with no break in it wraps rather than widening a phone's page.
  main.innerHTML = `<p class="back"><a href="#evalsets">All hidden sets</a></p>
    <h1 class="break-anywhere">${esc(set.name)}</h1>
    <div id="evalset-facts">${factsHtml(set)}</div>
    <nav class="segmented tabs" id="evalset-tabs" aria-label="This set's views">${tabLinks()}</nav>
    <div id="evalset-body" class="evalset-body"></div>`;
  $("#evalset-body").addEventListener("submit", submitted);
  showTab(wanted);
}

function showTab(requested) {
  const body = $("#evalset-body");
  if (!body) return; // the person moved on
  const tabs = shownTabs();
  const tab = tabs.find((t) => t.key === requested) ?? tabs[0];
  document.querySelectorAll("#evalset-tabs a[data-tab]").forEach((a) => {
    if (a.dataset.tab === tab.key) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  body.dataset.tab = tab.key;
  body.innerHTML = tab.show();
  wire(body);
}

// The set read again, its facts drawn again, and its tabs when its state changed (a draft frozen
// meanwhile loses its forms; focus on the tab bar stays there). True when this is the latest read
// of the set still shown; a refusal said, the set as last read kept.
async function refresh() {
  const id = current.set?.set;
  if (!id) return false; // no set shown: the person moved on
  const mine = ++asked;
  let set;
  try {
    set = await api(setPath(id));
  } catch (error) {
    if (mine === asked) toastError(error.message);
    return false;
  }
  if (mine !== asked || current.set?.set !== id) return false; // a newer read, or another set, meanwhile
  const before = current.set.state;
  current.set = set;
  const facts = $("#evalset-facts");
  if (facts) facts.innerHTML = factsHtml(set);
  const nav = $("#evalset-tabs");
  if (nav && set.state !== before) {
    const focused = nav.contains(document.activeElement) ? document.activeElement.dataset.tab : null;
    nav.innerHTML = tabLinks();
    if (focused) (nav.querySelector(`a[data-tab="${focused}"]`) ?? nav.querySelector("a")).focus();
  }
  return true;
}

// After a draw or an assignment: the set again, the address's tab shown again, focus on its link.
async function reload() {
  if (!(await refresh())) return; // a refusal, a newer read (it shows the tab), or the person moved on
  showTab(wanted);
  $('#evalset-tabs a[aria-current="page"]')?.focus();
}

function factsHtml(s) {
  const about = STATES[s.state]?.about;
  const facts = [
    ["Set", `<code class="wrap">${esc(s.set)}</code>`],
    ["Agent", `<code class="wrap">${esc(s.pack)}</code>`],
    ["State", `${stateBadge(s.state)}${about ? ` <span class="muted">${esc(about)}</span>` : ""}`],
    ["Cases", esc(s.cases)],
    ["Made", esc(time(s.created_at))],
  ];
  if (s.frozen_at) facts.push(["Frozen", esc(time(s.frozen_at))]);
  if (s.commitment) facts.push(["Commitment", `<code class="wrap">${esc(s.commitment)}</code>`]);
  facts.push(["Evaluation runs", esc(runsText(s))]);
  if (s.retired_at) facts.push(["Retired", esc(time(s.retired_at))]);
  return `<dl class="facts evalset-facts">${facts.map(([term, value]) => `<dt>${term}</dt><dd>${value}</dd>`).join("")}</dl>`;
}

// The rotation rule: set at freeze (both limits), counted from then.
function runsText(s) {
  if (s.max_runs === null || s.max_runs === undefined) return "None yet: the key holder sets how many it allows when freezing it.";
  const used = `${s.runs.used} used, ${s.runs.left} left of ${s.max_runs}`;
  if (s.retired_at) return `${s.runs.used} used of ${s.max_runs}.`;
  return s.graded_until ? `${used}, until ${time(s.graded_until)} (${s.max_age_days} days from freezing).` : `${used}.`;
}

// Who a user id is: a member's name (their user id one hover away), or the id of someone who is
// no longer a member.
function labellerHtml(user) {
  const name = current.names.get(user);
  if (name) return `<span title="${esc(user)}">${esc(name)}</span>`;
  return `<span class="muted">Former member</span> <code class="mono">${esc(user)}</code>`;
}

const nameOf = (user) => (current.names.get(user) ?? user).toLowerCase();

// -- progress -----------------------------------------------------------------------------------------

const STATUS_STATS = [
  { key: "draft", label: "Not drawn", foot: "not labelled unless drawn" },
  { key: "sampled", label: "Waiting for labels", foot: "drawn: waiting for their two first labels" },
  { key: "agreed", label: "Agreed", foot: "their two first labels match" },
  { key: "disputed", label: "Disputed", foot: "their first labels differ: waiting for a third", tone: "warning" },
  { key: "resolved", label: "Resolved", foot: "settled by the majority of three" },
  { key: "unresolved", label: "Unresolved", foot: "three labels and no majority: it keeps the set from freezing", tone: "danger" },
];

const ROUND_COLUMNS = [
  ["assigned", "Assigned"],
  ["labelled", "Labelled"],
  ["open", "Waiting"],
  ["withdrawn", "Withdrawn"],
];

function roundTable(id, caption, round, rows) {
  return tableHtml({
    id,
    caption: esc(caption),
    columns: [
      { key: "who", label: "Labeller", sort: (r) => nameOf(r.user), cell: (r) => labellerHtml(r.user) },
      ...ROUND_COLUMNS.map(([key, label]) => ({
        key, label, numeric: true, sort: (r) => r[round][key], cell: (r) => esc(r[round][key]),
      })),
    ],
    rows,
    defaultCompare: (a, b) => nameOf(a.user).localeCompare(nameOf(b.user)), // by name, not by id
  });
}

function showProgress() {
  const s = current.set;
  const stats = `<h2>Cases by status</h2><div class="stats-primary">${STATUS_STATS.map(({ key, label, foot, tone }) => stat({
    label, value: String(s.statuses[key]), foot: esc(foot), size: "small", tone: s.statuses[key] && tone ? tone : "",
  })).join("")}</div>`;
  const rows = Object.entries(s.labellers).map(([user, rounds]) => ({ user, ...rounds }));
  if (!rows.length) {
    return stats + emptyState({
      icon: "user",
      title: "No one is assigned yet",
      text: draft() ? "Draw cases (Sample), then give them to labellers (Assign)." : "No case of this set was given to a labeller.",
    });
  }
  const third = rows.filter((r) => r.adjudication.assigned);
  return `${stats}<h2>Labellers</h2>
    <p class="muted">Each labeller's assignments: waiting ones are theirs to label; a withdrawn one went to someone else once their membership no longer held.</p>
    ${roundTable("evalset-first", "First labels, by labeller", "first", rows)}
    ${third.length ? roundTable("evalset-third", "Third labels (disputed cases), by labeller", "adjudication", third) : ""}`;
}

// -- drawing and assigning (a draft only) ------------------------------------------------------------

const SEED_MAX = 2 ** 32 - 1; // server/app.py `Seed`
const COUNT_MAX = 1_000_000; // server/app.py `SampleRequest`

const seedField = (id) => `<label for="${id}">Seed</label>
  <p class="muted" id="${id}-hint">The same set and seed give the same result.</p>
  <input id="${id}" type="number" inputmode="numeric" min="0" max="${SEED_MAX}" value="0" required aria-describedby="${id}-hint">`;

// The form's refusal (`<form id>-error`), tied to the fields it marks invalid (showError).
const submitRow = (form, text) => `<p class="field-error" id="${form}-error" role="alert" hidden></p>
  <div class="row mt-4"><button type="submit" class="primary">${esc(text)}</button></div>`;

function showSample() {
  const s = current.set;
  const drawn = s.cases - s.statuses.draft;
  return `<form class="panel evalset-form" id="sample-form" novalidate>
    <h2>Draw cases to label</h2>
    <p class="muted">A seeded draw within each stratum: only drawn cases are labelled, and each one's
      weight is its stratum's cases over those drawn from it. ${esc(counted(drawn, "case"))} of ${esc(s.cases)} drawn so far;
      ${esc(s.statuses.draft)} left.</p>
    <fieldset class="pick-list"><legend>Draw</legend>
      <label class="pick"><input type="radio" name="how" value="per_stratum" checked>
        <span><b>From each stratum</b> <span class="muted">this many more, or all that are left of a smaller one</span></span></label>
      <label class="pick"><input type="radio" name="how" value="cases">
        <span><b>In all</b> <span class="muted">this many more, shared out by the cases left in each stratum</span></span></label>
    </fieldset>
    <label for="sample-count">How many</label>
    <input id="sample-count" type="number" inputmode="numeric" min="1" max="${COUNT_MAX}" required>
    ${seedField("sample-seed")}
    ${submitRow("sample-form", "Draw cases")}
  </form>`;
}

// The labellers an administrator may choose: members who signed in, holding the labeller role and
// none that manages hidden sets.
const labellers = () => current.members
  .filter((m) => m.user_id && m.roles.includes(LABELLER) && !m.roles.some((r) => CURATING.includes(r)))
  .sort((a, b) => nameOf(a.user_id).localeCompare(nameOf(b.user_id)));

function labellerChoices(legend) {
  return `<fieldset class="pick-list"><legend>${esc(legend)}</legend>${labellers().map((m) => `<label class="pick">
      <input type="checkbox" name="labellers" value="${esc(m.user_id)}">
      <span><b>${esc(current.names.get(m.user_id))}</b> <code class="mono muted">${esc(m.user_id)}</code></span>
    </label>`).join("")}</fieldset>`;
}

const noLabellers = () => emptyState({
  icon: "user",
  title: `No labellers in ${orgName()} yet`,
  text: `Invite people with the Labeller role (<a href="#admin/invitations">Access</a>). They can be chosen here once they have signed in. An administrator is never given a case.`,
});

function showAssign() {
  const s = current.set;
  if (s.cases === s.statuses.draft) {
    return emptyState({ icon: "inbox", title: "Nothing is drawn yet", text: "Draw cases first: only drawn cases are labelled.", action: { href: setLink(s.set, "sample"), label: "Draw cases" } });
  }
  if (!labellers().length) return noLabellers();
  return `<form class="panel evalset-form" id="assign-form" novalidate>
    <h2>Give each drawn case two labellers</h2>
    <p class="muted">Each drawn case goes to two different labellers of those you choose, blind: neither sees
      the other's label. The work is balanced across labellers and pairs, and a case that has its two
      already keeps them. ${esc(waiting(s.statuses.sampled))}</p>
    ${labellerChoices("Labellers (two or more)")}
    ${seedField("assign-seed")}
    ${submitRow("assign-form", "Assign")}
  </form>`;
}

const waiting = (n) => (n
  ? `${counted(n, "drawn case waits", "drawn cases wait")} for its first two labels.`
  : "No drawn case waits for its first two labels.");

function showAdjudicate() {
  const s = current.set;
  if (!labellers().length) return noLabellers();
  const disputed = s.statuses.disputed
    ? `${counted(s.statuses.disputed, "case is", "cases are")} disputed.`
    : "No case is disputed now.";
  return `<form class="panel evalset-form" id="adjudicate-form" novalidate>
    <h2>Give each disputed case a third labeller</h2>
    <p class="muted">A disputed case's two first labels differ. A third labeller, neither of its first two,
      labels it from its input alone, blind, and the majority on each question settles it. ${esc(disputed)}</p>
    ${labellerChoices("Labellers (one or more)")}
    ${seedField("adjudicate-seed")}
    ${submitRow("adjudicate-form", "Adjudicate")}
  </form>`;
}

// A whole number from a field, within its bounds, or a refusal in words that names the field.
function wholeNumber(field, what) {
  const value = Number(field.value);
  const [low, high] = [Number(field.min), Number(field.max)];
  if (field.value.trim() === "" || !Number.isInteger(value) || value < low || value > high) {
    throw Object.assign(new Error(`${what} is a whole number from ${low} to ${high}.`), { field });
  }
  return value;
}

function chosenLabellers(form, least) {
  const chosen = [...form.querySelectorAll('input[name="labellers"]:checked')].map((input) => input.value);
  if (chosen.length < least) {
    const field = form.querySelector('input[name="labellers"]');
    throw Object.assign(new Error(least === 1 ? "Choose a labeller or more." : `Choose ${least} labellers or more.`), { field, group: true });
  }
  return chosen;
}

// What each form sends, and what its answer says.
const FORMS = {
  "sample-form": (form) => {
    const how = form.querySelector('input[name="how"]:checked')?.value === "cases" ? "cases" : "per_stratum";
    const count = wholeNumber(form.querySelector("#sample-count"), "How many");
    const seed = wholeNumber(form.querySelector("#sample-seed"), "The seed");
    return send(form, "sample", { [how]: count, seed }, "Drawing…", (a) =>
      `Drew ${counted(a.drawn, "case")}: ${a.sampled} drawn in all (seed ${a.seed}).`);
  },
  "assign-form": (form) => {
    const chosen = chosenLabellers(form, 2);
    const seed = wholeNumber(form.querySelector("#assign-seed"), "The seed");
    return send(form, "assignments", { labellers: chosen, seed }, "Assigning…", (a) => (a.assigned
      ? `Made ${counted(a.assigned, "assignment")}${withdrawnText(a)}.`
      : `Every drawn case has its two labellers already${withdrawnText(a)}.`));
  },
  "adjudicate-form": (form) => {
    const chosen = chosenLabellers(form, 1);
    const seed = wholeNumber(form.querySelector("#adjudicate-seed"), "The seed");
    return send(form, "adjudication", { labellers: chosen, seed }, "Assigning…", (a) => (a.assigned
      ? `Gave ${counted(a.assigned, "disputed case")} a third labeller${withdrawnText(a)}.`
      : `No disputed case waits for a third labeller${withdrawnText(a)}.`));
  },
};

const withdrawnText = (a) => (a.withdrawn ? `; withdrew ${a.withdrawn} whose labeller no longer qualifies` : "");

async function submitted(event) {
  const handle = FORMS[event.target.id];
  if (!handle) return;
  event.preventDefault();
  const form = event.target;
  clearError(form);
  try {
    await handle(form);
  } catch (error) {
    showError(form, error);
  }
}

// The button stays busy until the set is read again and the form drawn anew with its counts: a
// draw is chained and never undone, so a second press must not draw a second batch.
async function send(form, path, body, busyText, said) {
  await whileBusy(form.querySelector('button[type="submit"]'), busyText, async () => {
    const answer = await api(`${setPath(current.set.set)}/${path}`, { method: "POST", body: JSON.stringify(body) });
    toast(said(answer));
    await reload();
  });
}

// A refusal, said in the form (and heard: role="alert"), on the field it names when it names one,
// which it then describes.
function showError(form, error) {
  const box = form.querySelector(".field-error");
  box.textContent = error.message;
  box.hidden = false;
  if (!error.field) return;
  const fields = error.group ? form.querySelectorAll(`input[name="${error.field.name}"]`) : [error.field];
  fields.forEach((field) => {
    field.setAttribute("aria-invalid", "true");
    describedBy(field, box.id, true);
  });
  error.field.focus();
}

function clearError(form) {
  const box = form.querySelector(".field-error");
  box.hidden = true;
  box.textContent = "";
  form.querySelectorAll("[aria-invalid]").forEach((field) => {
    field.removeAttribute("aria-invalid");
    describedBy(field, box.id, false);
  });
}

// A field's description with `id` added or taken away, any other (a seed's hint) kept.
function describedBy(field, id, add) {
  const ids = (field.getAttribute("aria-describedby") ?? "").split(" ").filter((other) => other && other !== id);
  if (add) ids.push(id);
  if (ids.length) field.setAttribute("aria-describedby", ids.join(" "));
  else field.removeAttribute("aria-describedby");
}

// -- the stored aggregates: agreement and coverage ----------------------------------------------------

const notMeasured = () => emptyState({
  icon: "clock",
  title: "Not measured yet",
  text: "The key holder measures agreement and coverage from the command line (<code>mendari evalset agreement</code> and <code>coverage</code>) once the first labels are in, and freezing measures them again. They show here once stored.",
});

const measured = (stored) => `Measured ${esc(time(stored.measured_at))} over the ${esc(counted(stored.n, "drawn case"))}.`;

// A coefficient's estimate to 3 places, or "not estimable": never a blank, NaN or null.
const estimable = (c) => c.status === "ok" && c.estimate !== null && c.estimate !== undefined;
const estimate = (c) => (estimable(c) ? num(c.estimate, 3) : "not estimable");
const UNDEFINED = "the cases leave it undefined";

// Its interval, or why there is none, in words.
function spread(c) {
  if (!estimable(c)) return c.reason || UNDEFINED;
  const interval = c.interval;
  if (!interval) return "no interval";
  if (interval.status !== "ok") return `interval not estimable: ${interval.reason || UNDEFINED}`;
  return `${num(interval.low, 3)} to ${num(interval.high, 3)}`;
}

function questionAgreement(question, m, i) {
  const pairs = m.kappa.length
    ? tableHtml({
      id: `evalset-kappa-${i}`,
      caption: "Cohen's kappa for each pair of labellers",
      columns: [
        { key: "pair", label: "Labellers", sort: (p) => p.raters.map(nameOf).join(" "), cell: (p) => p.raters.map(labellerHtml).join(" and ") },
        { key: "n", label: "Cases shared", numeric: true, sort: (p) => p.n, cell: (p) => esc(p.n) },
        { key: "kappa", label: "Kappa", numeric: true, sort: (p) => (estimable(p) ? p.estimate : null), cell: (p) => esc(estimate(p)) },
        { key: "interval", label: "Interval, or why there is none", className: "why", cell: (p) => esc(spread(p)) },
      ],
      rows: m.kappa,
    })
    : `<p class="muted">No two labellers share a case on this question.</p>`;
  return `<section class="panel evalset-question" aria-labelledby="evalset-agreement-${i}">
    <h2 id="evalset-agreement-${i}">${esc(slotText(question))}</h2>
    <p class="muted">${esc(counted(m.cases, "case"))} labelled on this question.</p>
    <div class="stats-primary two">
      ${stat({ label: "Percent agreement", value: figureHtml(m.percent), html: true, foot: "of the cases two labellers or more labelled, those whose first labels all match" })}
      ${stat({ label: "Krippendorff's alpha", value: estimate(m.alpha), foot: esc(`${spread(m.alpha)}; ${counted(m.alpha.cases, "case")}, ${counted(m.alpha.ratings, "rating")}`) })}
    </div>
    ${pairs}
  </section>`;
}

function showAgreement() {
  const stored = current.set.aggregates;
  if (!stored) return notMeasured();
  const report = stored.agreement;
  const questions = Object.entries(report.questions);
  return `<p class="muted evalset-intro">${measured(stored)} First labels only, given blind. Percent
      agreement is k of n with its exact interval; kappa (each pair of labellers) and alpha (all of
      them) have percentile bootstrap intervals at level ${esc(report.level)}
      (seed ${esc(report.seeds.bootstrap)}, ${esc(report.seeds.resamples)} resamples).</p>
    ${questions.length ? questions.map(([q, m], i) => questionAgreement(q, m, i)).join("") : '<p class="muted">No question was measured.</p>'}`;
}

const MIX = [["language", "Language"], ["source", "Source"], ["stratum", "Stratum"]];

function showCoverage() {
  const stored = current.set.aggregates;
  if (!stored) return notMeasured();
  const c = stored.coverage;
  const stats = `<div class="stats-primary">
      ${stat({ label: "Cases", value: String(c.cases), size: "small", foot: "drawn, with their settled labels" })}
      ${stat({ label: "Distinct inputs", value: String(c.distinct.exact), size: "small", foot: esc(`${c.distinct.normalised} under ${c.norm}`) })}
      ${stat({ label: "Duplicate groups", value: String(c.duplicate_groups), size: "small", foot: esc(`${c.conflicting_groups} with labels that disagree`), tone: c.conflicting_groups ? "warning" : "" })}
    </div>`;
  const warnings = c.warnings.length
    ? `<div class="callout warning"><ul class="evalset-warnings">${c.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`
    : "";
  const critical = c.critical.length
    ? tableHtml({
      id: "evalset-critical",
      caption: "Distinct positives of each critical label, counted the stricter, normalised way",
      columns: [
        { key: "question", label: "Question", sort: (r) => r.question, cell: (r) => esc(slotText(r.question)) },
        { key: "label", label: "Label", cell: (r) => esc(r.label) },
        { key: "cases", label: "Cases", numeric: true, sort: (r) => r.cases, cell: (r) => esc(r.cases) },
        { key: "distinct", label: "Distinct", numeric: true, sort: (r) => r.distinct, cell: (r) => `${esc(r.distinct)}${evidenceMark(r.insufficient, MIN_POSITIVES)}` },
      ],
      rows: c.critical,
    })
    : "";
  const labels = Object.entries(c.questions).flatMap(([question, q]) =>
    Object.entries(q.labels).map(([label, n]) => ({ question, label, ...n })));
  const mix = MIX.flatMap(([kind, name]) => Object.entries(c.mix[kind] ?? {}).map(([value, cases]) => ({ kind: name, value, cases })));
  return `<p class="muted evalset-intro">${measured(stored)} Each case counts with its settled labels (an
      agreed case's, or the majority of a resolved one); a case still waiting counts none.</p>
    ${stats}${warnings}
    <h2>Critical labels</h2>${critical || '<p class="muted">The agent names no critical label.</p>'}
    <h2>Labels</h2>${tableHtml({
      id: "evalset-labels",
      caption: "Each question's labels: cases, and distinct inputs among them",
      columns: [
        { key: "question", label: "Question", sort: (r) => r.question, cell: (r) => esc(slotText(r.question)) },
        { key: "label", label: "Label", sort: (r) => r.label, cell: (r) => esc(r.label) },
        { key: "cases", label: "Cases", numeric: true, sort: (r) => r.cases, cell: (r) => esc(r.cases) },
        { key: "distinct", label: "Distinct", numeric: true, sort: (r) => r.distinct, cell: (r) => esc(r.distinct) },
      ],
      rows: labels,
    })}
    <h2>Mix</h2>${tableHtml({
      id: "evalset-mix",
      caption: "The cases' languages, sources and strata, as their authors gave them (unknown where none)",
      columns: [
        { key: "kind", label: "Of", sort: (r) => r.kind, cell: (r) => esc(r.kind) },
        { key: "value", label: "Value", sort: (r) => r.value, cell: (r) => esc(r.value) },
        { key: "cases", label: "Cases", numeric: true, sort: (r) => r.cases, cell: (r) => esc(r.cases) },
      ],
      rows: mix,
    })}`;
}

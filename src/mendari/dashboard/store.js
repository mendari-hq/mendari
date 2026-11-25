// SPDX-License-Identifier: Apache-2.0
// An organization's pack store (LP-20j, ADR-0016 Proposed), in the dashboard: shown where the
// server says the store is on (`/api/auth/me` `store`) to a person who holds `author` (the
// `pack_author` role) or `promote` (`governance`, `admin`). Five tabs:
// - Agents (#store/agents): the organization's agents and their versions, with their state in
//   words; copy an agent served here, upload a signed archive (in parts, with progress); a refused
//   upload shows the PHI report's kinds, files and lines, never a value; a held one says only that
//   it is held.
// - A version (#store/version/<digest>): its files, an editor for a text file that saves a new
//   draft, its validation, its behaviour tests and the proposed tests from reviewers'
//   corrections, and the Decision Card the server rendered, as text.
// - Results: datasets with their check reports, jobs with their progress, run a slice at a time.
// - Compare: draft against current on the same cases, each figure k of n with its interval, the
//   differences with their adjusted p-values, "insufficient evidence" where it applies, and the
//   changed cases by id.
// - Promote: the versions that may be promoted, the reports to rely on, a reference and a note,
//   and a confirmation that says what will be served and to whom. The control is absent for the
//   version's author, with the reason.
// Everything a person typed or uploaded (names, paths, notes, cards) is escaped; a card is text.

import { views, wire } from "./router.js";
import { tableHtml } from "./table.js";
import { $, actionDialog, api, emptyState, esc, figureHtml, focusHeading, may, orgPin, OWN_REQUEST, state, time, toast, toastError, whileBusy } from "./ui.js";

// A table of rows of cells, each `{ html, sort? }` (the html already escaped), as `tableHtml`
// takes it: a column sorts where its cells carry a sort value.
const cellTable = ({ id, columns, rows }) =>
  tableHtml({
    id,
    rows,
    columns: columns.map((label, i) => ({
      key: String(i),
      label: esc(label),
      cell: (row) => row[i].html,
      ...(rows.some((row) => row[i].sort !== undefined) ? { sort: (row) => row[i].sort } : {}),
    })),
  });

// A JSON request through `api` (its refusals in words).
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

const TABS = [
  { key: "agents", label: "Agents", shown: () => may("author") },
  { key: "results", label: "Results", shown: () => may("author") },
  { key: "compare", label: "Compare", shown: () => may("author") },
  { key: "promote", label: "Promote", shown: () => may("promote") },
];
// A part of an archive: under the engine's body limit (4 MiB by default).
const PART_BYTES = 3 * 1024 * 1024;
const STATE_WORDS = {
  draft: ["Draft", ""],
  held: ["Held for the operator's review", "s-warning"],
  promoted: ["Serving", "s-ok"],
  retired: ["Retired", ""],
  withdrawn: ["Withdrawn", "o-ABSTAIN"],
};

const stateBadge = (value) => {
  const [text, tone] = STATE_WORDS[value] ?? [value, ""];
  return `<span class="badge ${tone}" title="${esc(value)}">${esc(text)}</span>`;
};
const short = (digest) => esc(String(digest).replace("sha256:", "").slice(0, 12));
const digestCode = (digest) => `<code title="${esc(digest)}">${short(digest)}</code>`;

views.store = async (main, params = []) => {
  const [tab = defaultTab(), ...rest] = params;
  if (tab === "version" && rest[0]) return showVersion(main, rest[0]);
  const tabs = TABS.filter((t) => t.shown());
  const current = tabs.find((t) => t.key === tab) ?? tabs[0];
  main.innerHTML = `<h1>Agent store</h1>
    <p class="muted">Your organization's own versions of agents: written, checked and evaluated here, and served to your organization only once someone who did not write them promotes them.</p>
    <nav class="segmented tabs" aria-label="Agent store views">${tabs
      .map((t) => `<a href="#store/${t.key}" data-tab="${t.key}"${t === current ? ' aria-current="page"' : ""}>${esc(t.label)}</a>`)
      .join("")}</nav>
    <section id="store-body" aria-busy="true"></section>`;
  const body = $("#store-body");
  try {
    if (current.key === "agents") await showPacks(body);
    else if (current.key === "results") await showResults(body);
    else if (current.key === "compare") await showCompare(body);
    else await showPromote(body);
  } catch (error) {
    body.innerHTML = emptyState({ icon: "shield-x", title: "This could not be read", text: esc(error.message) });
  } finally {
    body.removeAttribute("aria-busy");
  }
  wire(body);
};

views.store.available = () => Boolean(state.store) && (may("author") || may("promote"));
views.store.packless = true;
views.store.leave = () => document.querySelectorAll("dialog[data-admin]:not([data-busy])").forEach((d) => d.close());

function defaultTab() {
  return may("author") ? "agents" : "promote";
}

// -- packs ---------------------------------------------------------------------------------------

async function showPacks(body) {
  const lineages = await api("/api/store/agents");
  const list = lineages.length
    ? lineages.map(lineageHtml).join("")
    : emptyState({ icon: "package", title: "No agent in your store yet", text: "Copy an agent this deployment serves, or upload a signed archive." });
  body.innerHTML = `${list}
    <form class="panel admin-form" id="copy-form" novalidate>
      <h2>Copy an agent</h2>
      <p class="muted">A draft of an agent this deployment serves, under your organization's name. It runs on the engine for agents the deployment does not own.</p>
      <label for="copy-pack">Agent to copy</label>
      <input id="copy-pack" name="pack" required placeholder="mendari/portal-message-agent" autocomplete="off">
      <label for="copy-name">Name of the copy</label>
      <input id="copy-name" name="name" required pattern="[a-z0-9][a-z0-9\\-]{0,62}" placeholder="portal-agent">
      <button type="submit" class="primary">Copy</button>
    </form>
    <form class="panel admin-form" id="upload-form" novalidate>
      <h2>Upload a signed archive</h2>
      <p class="muted">Built with <code>mendari archive build</code> and signed with one of your organization's author keys. Files that read as a person's details are refused.</p>
      <label for="upload-file">Archive</label>
      <input id="upload-file" name="file" type="file" required accept=".mendari,application/gzip">
      <progress id="upload-progress" max="1" value="0" hidden aria-label="Upload progress"></progress>
      <button type="submit" class="primary">Upload</button>
    </form>
    <div id="upload-result" aria-live="polite"></div>`;
  $("#copy-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const pack = form.pack.value.trim();
    const name = form.name.value.trim();
    if (!pack || !name) return toastError("Enter both the source agent and a name for the copy.");
    whileBusy(form.querySelector("button"), "Copying…", async () => {
      const made = await post("/api/store/copies", { pack, name });
      toast(`Copied into a draft: ${made.pack_id}`);
      location.hash = `#store/version/${encodeURIComponent(made.digest)}`;
    }).catch((error) => toastError(error.message));
  });
  $("#upload-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const file = event.target.file.files[0];
    if (!file) return toastError("Choose an archive to upload.");
    whileBusy(event.target.querySelector("button"), "Uploading…", () => upload(file)).catch((error) => toastError(error.message));
  });
}

function lineageHtml(lineage) {
  const rows = lineage.versions.map((v) => [
    { html: `<a href="#store/version/${encodeURIComponent(v.digest)}">${esc(v.version)}</a> ${digestCode(v.digest)}`, sort: v.created_at },
    { html: stateBadge(v.state) },
    { html: v.state === "held" ? "" : v.valid ? "Validates" : '<span class="badge o-ABSTAIN">Does not validate</span>' },
    { html: esc(v.source ?? "") },
    { html: esc(time(v.created_at)), sort: v.created_at },
  ]);
  return `<article class="panel" aria-label="${esc(lineage.pack_id)}">
    <h2><code>${esc(lineage.pack_id)}</code></h2>
    ${cellTable({ id: `store-${lineage.pack_id}`, columns: ["Version", "State", "Validation", "From", "Made"], rows })}
  </article>`;
}

async function upload(file) {
  const progress = $("#upload-progress");
  const result = $("#upload-result");
  const data = new Uint8Array(await file.arrayBuffer());
  let answer;
  try {
    if (data.length <= PART_BYTES) {
      answer = await send("/api/store/versions", { method: "POST", raw: data });
    } else {
      const digest = await crypto.subtle.digest("SHA-256", data);
      const sha256 = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
      const begun = await post("/api/store/uploads", { size: data.length, sha256 });
      const uploadId = encodeURIComponent(begun.upload);
      progress.hidden = false;
      progress.value = 0;
      for (let n = 0, start = 0; start < data.length; n += 1, start += PART_BYTES) {
        await send(`/api/store/uploads/${uploadId}/parts/${n}`, { method: "PUT", raw: data.slice(start, start + PART_BYTES) });
        progress.value = Math.min(1, (start + PART_BYTES) / data.length);
      }
      answer = await send(`/api/store/uploads/${uploadId}/complete`, { method: "POST" });
    }
  } finally {
    progress.hidden = true;
  }
  result.innerHTML = uploadedHtml(answer);
  if (answer.ok && answer.body.state !== "held") toast(`Uploaded: ${answer.body.pack_id}`);
}

// A request whose refusal is shown in the page (the PHI report), not only in a toast.
async function send(url, { method, raw }) {
  const response = await fetch(url, {
    method,
    body: raw,
    headers: { "content-type": "application/octet-stream", ...OWN_REQUEST, ...orgPin() },
    credentials: "same-origin",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok && !body.phi) throw new Error(body.detail || `The server refused it (${response.status}).`);
  return { ok: response.ok, body };
}

function uploadedHtml({ ok, body }) {
  if (!ok && body.phi) {
    const rows = body.phi.findings.map((f) => [{ html: esc(f.kind) }, { html: `<code>${esc(f.file)}</code>` }, { html: esc(f.line || "its name") }]);
    return `<div class="callout danger" role="alert"><h3>Refused: some files read as a person's details</h3>
      <p>Use synthetic data only. Where each was found (never what):</p>
      ${cellTable({ id: "phi-report", columns: ["Kind", "File", "Line"], rows })}</div>`;
  }
  if (body.state === "held") {
    return '<div class="callout neutral" role="status"><h3>Held for the operator\'s review</h3><p>This version is kept, held: it cannot be tested, edited from or promoted until the operator reviews it.</p></div>';
  }
  return `<div class="callout ok" role="status">A draft: <a href="#store/version/${encodeURIComponent(body.digest)}">${esc(body.pack_id)} ${esc(body.version)}</a>.</div>`;
}

// -- one version -----------------------------------------------------------------------------------

async function showVersion(main, digest) {
  const version = await api(`/api/store/versions/${encodeURIComponent(digest)}`);
  const held = version.state === "held";
  main.innerHTML = `<p><a href="#store/agents">← Agent store</a></p>
    <h1><code>${esc(version.pack_id)}</code> ${esc(version.version)}</h1>
    <p>${stateBadge(version.state)} ${digestCode(version.digest)}</p>
    ${held ? '<div class="callout neutral" role="status">Held for the operator\'s review. Nothing more is shown until the operator answers.</div>' : versionBody(version)}`;
  if (held) return focusHeading(main.querySelector("h1") ?? main);
  $("#run-tests")?.addEventListener("click", (event) =>
    whileBusy(event.currentTarget, "Running…", async () => {
      const ran = await post(`/api/store/versions/${encodeURIComponent(digest)}/tests`, {});
      const box = $("#tests-result");
      if (!box) return;
      box.innerHTML = testsHtml(ran);
      wire(box);
    }).catch((error) => toastError(error.message)));
  $("#edit-path")?.addEventListener("change", (event) => loadFile(digest, event.target.value));
  $("#edit-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    whileBusy(form.querySelector("button"), "Saving…", async () => {
      const made = await post(`/api/store/versions/${encodeURIComponent(digest)}/edits`, {
        files: [{ path: form.path.value, content: form.content.value }],
      });
      toast(made.state === "held" ? "Saved: held for the operator's review" : "Saved as a new draft");
      location.hash = `#store/version/${encodeURIComponent(made.digest)}`;
    }).catch((error) => toastError(error.message));
  });
  const card = $("#version-card");
  if (card) card.textContent = version.card_markdown ?? "";
  wire(main);
  focusHeading(main.querySelector("h1") ?? main);
}

function versionBody(version) {
  const problems = version.validation.length
    ? `<ul>${version.validation.map((p) => `<li><code>${esc(p.file)}</code> ${esc(p.place)}: ${esc(p.text)}</li>`).join("")}</ul>`
    : "<p>Validates.</p>";
  const datasets = (version.checks?.datasets ?? []).map((d) => [
    { html: esc(d.dataset) },
    { html: esc(d.cases) },
    { html: esc(d.schema_problems.count + d.label_problems.count) },
    { html: esc(d.duplicates["norm/v1"].count) },
  ]);
  const editable = version.files.filter((f) => /\.(ya?ml|json|md|txt|csv)$/i.test(f.path) && f.size <= 1024 * 1024);
  return `<section class="panel"><h2>Validation</h2>${problems}</section>
    <section class="panel"><h2>Datasets it carries</h2>${datasets.length
      ? cellTable({ id: "version-datasets", columns: ["Dataset", "Cases", "Schema and label problems", "Duplicate groups"], rows: datasets })
      : '<p class="muted">None declared.</p>'}</section>
    <section class="panel"><h2>Behaviour tests</h2>
      <button type="button" id="run-tests">Run the tests</button>
      <div id="tests-result" aria-live="polite"></div></section>
    <form class="panel admin-form" id="edit-form" novalidate>
      <h2>Edit a text file</h2>
      <p class="muted">Saving makes a new draft; this version never changes. A calibration is fitted and uploaded, not edited here.</p>
      <label for="edit-path">File</label>
      <select id="edit-path" name="path"><option value="">Choose a file</option>${editable.map((f) => `<option>${esc(f.path)}</option>`).join("")}</select>
      <label for="edit-content">Content</label>
      <textarea id="edit-content" name="content" rows="16" spellcheck="false"></textarea>
      <button type="submit" class="primary">Save as a new draft</button>
    </form>
    <section class="panel"><h2>Files</h2>${cellTable({
      id: "version-files",
      columns: ["Path", "Size"],
      rows: version.files.map((f) => [{ html: `<code>${esc(f.path)}</code>` }, { html: esc(f.size), sort: f.size }]),
    })}</section>
    <section class="panel"><h2>Decision Card</h2><p class="muted">Rendered by the server from the version's files; nothing uploaded about it is shown.</p><pre id="version-card" class="wrap"></pre></section>`;
}

let fileLoadSeq = 0;

async function loadFile(digest, path) {
  const area = $("#edit-content");
  const seq = ++fileLoadSeq;
  if (!path) return (area.value = "");
  const file = await api(`/api/store/versions/${encodeURIComponent(digest)}/file?path=${encodeURIComponent(path)}`).catch((error) => toastError(error.message));
  if (file && seq === fileLoadSeq && area.isConnected) area.value = file.text;
}

function testsHtml(ran) {
  const row = (c) => [{ html: esc(c.name ?? c.case) }, { html: c.passed ? "Passed" : '<span class="badge o-ABSTAIN">Failed</span>' }, { html: esc((c.failures ?? []).join("; ")) }];
  const own = cellTable({ id: "tests-own", columns: ["Test", "Result", "Why"], rows: ran.cases.map(row) });
  const proposed = ran.proposed?.cases?.length
    ? `<h3>Accepted from reviewers' corrections</h3>${cellTable({ id: "tests-proposed", columns: ["Case", "Result", "Why"], rows: ran.proposed.cases.map(row) })}`
    : "";
  return `<p>${esc(ran.passed)} passed, ${esc(ran.failed)} failed.</p>${own}${proposed}`;
}

// -- results ----------------------------------------------------------------------------------------

async function showResults(body) {
  const [datasets, jobs, lineages] = await Promise.all([api("/api/store/datasets"), api("/api/store/jobs"), api("/api/store/agents")]);
  const versions = lineages.flatMap((l) => l.versions.filter((v) => v.state !== "held" && v.state !== "withdrawn"));
  const datasetRows = datasets.map((d) => [
    { html: esc(d.name) },
    { html: d.state === "held" ? stateBadge("held") : esc(d.kind ?? "upload") },
    { html: esc(d.cases ?? "") },
    { html: d.report ? esc(d.report.schema_problems.count + d.report.label_problems.count) : "" },
    { html: esc(time(d.created_at)), sort: d.created_at },
  ]);
  const jobRows = jobs.map((j) => [
    { html: j.pack ? `<code>${esc(j.pack)}</code>` : digestCode(j.digest) },
    { html: `<progress max="${esc(j.total)}" value="${esc(j.done)}" aria-label="${esc(j.done)} of ${esc(j.total)} cases"></progress> ${esc(j.done)} of ${esc(j.total)}` },
    { html: esc(j.state) },
    { html: j.state === "queued" || j.state === "running" ? `<button type="button" data-advance="${esc(j.id)}">Run</button>` : "" },
  ]);
  body.innerHTML = `<section class="panel"><h2>Datasets</h2>${datasetRows.length
    ? cellTable({ id: "store-datasets", columns: ["Name", "Kind", "Cases", "Schema and label problems", "Added"], rows: datasetRows })
    : emptyState({ icon: "database", title: "No dataset yet", text: "Upload a labelled dataset, or run a job on a dataset a version declares.", level: 3 })}</section>
    <form class="panel admin-form" id="job-form" novalidate>
      <h2>Make an evaluation job</h2>
      <label for="job-version">Version</label>
      <select id="job-version" name="version">${versions.map((v) => `<option value="${esc(v.digest)}">${esc(v.pack_id)} ${esc(v.version)} (${short(v.digest)})</option>`).join("")}
        <option value="">The agent it was copied from</option></select>
      <label for="job-pack">Or an agent served here (the current one)</label>
      <input id="job-pack" name="pack" placeholder="mendari/portal-message-agent">
      <label for="job-dataset">Dataset</label>
      <select id="job-dataset" name="source">${datasets.filter((d) => d.state !== "held").map((d) => `<option value="id:${esc(d.id)}">${esc(d.name)} (${esc(d.cases)} cases)</option>`).join("")}
        <option value="name:test">The version's own test set</option></select>
      <button type="submit" class="primary">Make the job</button>
    </form>
    <section class="panel"><h2>Jobs</h2>${jobRows.length
      ? cellTable({ id: "store-jobs", columns: ["Runs", "Progress", "State", ""], rows: jobRows })
      : emptyState({ icon: "list", title: "No job yet", text: "A job evaluates a version on a dataset, a slice at a time.", level: 3 })}</section>`;
  $("#job-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const [kind, value] = form.source.value.split(/:(.*)/s);
    const target = form.pack.value.trim() ? { pack: form.pack.value.trim() } : { version: form.version.value };
    whileBusy(form.querySelector("button"), "Making…", async () => {
      await post("/api/store/jobs", { ...target, dataset: { [kind === "id" ? "id" : "name"]: value } });
      toast("Job made: run it below");
      await showResults(body);
      wire(body);
    }).catch((error) => toastError(error.message));
  });
  if (!body.dataset.resultsWired) {
    body.dataset.resultsWired = "1";
    body.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-advance]");
      if (button) {
        whileBusy(button, "Running…", () => runJob(button.dataset.advance, body))
          .then(() => (body.isConnected ? showResults(body).then(() => wire(body)) : undefined))
          .catch((error) => toastError(error.message));
      }
    });
  }
}

async function runJob(id, anchor) {
  const jobId = encodeURIComponent(id);
  for (let i = 0; i < 1000; i += 1) {
    if (anchor && !anchor.isConnected) return;
    const job = await post(`/api/store/jobs/${jobId}/advance`, {});
    if (job.state !== "queued" && job.state !== "running") {
      toast(job.state === "done" ? "Job done: its report is kept" : `Job ${job.state}: ${job.error ?? ""}`);
      return;
    }
  }
}

// -- compare ----------------------------------------------------------------------------------------

async function showCompare(body) {
  const jobs = (await api("/api/store/jobs")).filter((j) => j.state === "done");
  const option = (j) => `<option value="${esc(j.id)}">${j.pack ? esc(j.pack) : short(j.digest)} on ${short(j.dataset_sha256)}</option>`;
  body.innerHTML = `<form class="panel admin-form" id="compare-form" novalidate>
      <h2>Draft against current</h2>
      <p class="muted">Two finished jobs on the same dataset, paired on the same cases.</p>
      <label for="compare-draft">Draft</label><select id="compare-draft" name="draft">${jobs.map(option).join("")}</select>
      <label for="compare-current">Current</label><select id="compare-current" name="current">${jobs.map(option).join("")}</select>
      <button type="submit" class="primary">Compare</button>
    </form><div id="compare-result" aria-live="polite">${jobs.length ? "" : emptyState({ icon: "list", title: "No finished job yet", text: "Run a job for the draft and one for the current version on the same dataset.", level: 3 })}</div>`;
  $("#compare-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    whileBusy(form.querySelector("button"), "Comparing…", async () => {
      const compared = await api(`/api/store/comparisons?draft=${encodeURIComponent(form.draft.value)}&current=${encodeURIComponent(form.current.value)}`);
      $("#compare-result").innerHTML = comparisonHtml(compared);
      wire($("#compare-result"));
    }).catch((error) => toastError(error.message));
  });
}

function comparisonHtml(c) {
  const rows = c.figures.map((f) => [
    { html: esc(f.figure) },
    { html: figureHtml(f.a) },
    { html: figureHtml(f.b) },
    { html: f.evidence === "insufficient" ? '<span class="badge">Insufficient evidence</span>' : esc(Number(f.p_adjusted).toPrecision(3)) },
    { html: f.different ? "Different" : "Not shown to differ" },
  ]);
  const changed = c.changed.length
    ? `<h3>Cases that changed (${esc(c.changed.length)})</h3><ul class="wrap">${c.changed.map((x) => `<li><code>${esc(x.case)}</code>: ${esc(x.changes.join(", "))}</li>`).join("")}</ul>`
    : "<p>No case changed.</p>";
  return `<section class="panel"><h2>${esc(c.dataset.paired)} cases paired</h2>
    ${cellTable({ id: "comparison", columns: ["Figure", "Draft", "Current", "Adjusted p", "Difference"], rows })}
    <p class="muted">Exact McNemar tests, Holm-adjusted over the figures listed; exact 95% intervals.</p>${changed}</section>`;
}

// -- promote ------------------------------------------------------------------------------------------

async function showPromote(body) {
  const { promotions, versions } = await api("/api/store/promotions");
  const candidates = versions.length
    ? versions.map(candidateHtml).join("")
    : emptyState({ icon: "check", title: "Nothing to promote", text: "A validated draft with a finished evaluation shows here.", level: 3 });
  const inForce = promotions.filter((p) => !p.retired_at);
  const serving = inForce.length
    ? cellTable({
        id: "promotions",
        columns: ["Agent", "Version", "Promoted", ""],
        rows: inForce.map((p) => [
          { html: `<code>${esc(p.pack_id)}</code>` },
          { html: `${esc(p.version)} ${digestCode(p.digest)}` },
          { html: esc(time(p.created_at)) },
          { html: `<button type="button" data-retire="${esc(p.id)}">Stop serving…</button>` },
        ]),
      })
    : '<p class="muted">Nothing from the store is served yet.</p>';
  body.innerHTML = `<section class="panel"><h2>Served from the store</h2>${serving}</section>${candidates}`;
  if (!body.dataset.promoteWired) {
    body.dataset.promoteWired = "1";
    body.addEventListener("submit", (event) => {
      const form = event.target.closest("form[data-promote]");
      if (!form) return;
      event.preventDefault();
      promote(form).then((done) => done && showPromote(body).then(() => wire(body))).catch((error) => toastError(error.message));
    });
    body.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-retire]");
      if (!button) return;
      const promoId = encodeURIComponent(button.dataset.retire);
      actionDialog({
        title: "Stop serving this version?",
        body: `<p>Your organization is then served no version of this agent until another is promoted.</p>`,
        action: "Stop serving",
        danger: true,
        run: () => post(`/api/store/promotions/${promoId}/retire`, {}),
      }).then((done) => done && showPromote(body).then(() => wire(body)));
    });
  }
}

function candidateHtml(v) {
  const id = `promote-${short(v.digest)}`;
  const head = `<h2 id="${id}"><code>${esc(v.pack_id)}</code> ${esc(v.version)} ${digestCode(v.digest)}</h2>`;
  if (!v.may_promote) {
    return `<article class="panel" aria-labelledby="${id}">${head}<p class="callout neutral">You made this version, or one it was made from: someone else signs it off.</p></article>`;
  }
  if (!v.reports.length) {
    return `<article class="panel" aria-labelledby="${id}">${head}<p class="muted">No finished evaluation of this version yet: run a job on it first.</p></article>`;
  }
  return `<form class="panel admin-form" data-promote="${esc(v.digest)}" data-pack="${esc(v.pack_id)}" data-version="${esc(v.version)}" aria-labelledby="${id}" novalidate>
    ${head}
    <fieldset><legend>Evaluations relied on</legend>${v.reports.map((r, i) => `<label><input type="checkbox" name="reports" value="${esc(r)}" ${i === 0 ? "checked" : ""}> Report <code>${esc(r.slice(0, 8))}</code></label>`).join("")}</fieldset>
    <label for="${id}-reference">Reference (where the sign-off is recorded)</label>
    <input id="${id}-reference" name="reference" required minlength="3" maxlength="500">
    <label for="${id}-note">Note</label>
    <textarea id="${id}-note" name="note" rows="2" maxlength="2000"></textarea>
    <button type="submit" class="primary">Promote…</button>
  </form>`;
}

async function promote(form) {
  const reports = [...form.querySelectorAll('input[name="reports"]:checked')].map((i) => i.value);
  const org = state.memberships?.find((m) => m.org === state.me?.org)?.name ?? state.me?.org ?? "your organization";
  return actionDialog({
    title: "Serve this version?",
    body: `<p>From now on, <code>${esc(form.dataset.pack)}</code> ${esc(form.dataset.version)} answers every decide request for this agent made in <b>${esc(org)}</b>, by people, keys, clients and the EHR, and no other organization's. The version served before it is retired. It recommends; it automates only where its risk level and the automation approval allow.</p>`,
    action: "Promote",
    run: () =>
      post("/api/store/promotions", {
        version: form.dataset.promote,
        reports,
        reference: form.reference.value.trim(),
        note: form.note.value.trim() || null,
      }),
  });
}

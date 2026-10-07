// SPDX-License-Identifier: Apache-2.0
// Evaluations: the sub-views (comparison, report, run, history) and the run confirmation.

import { leaderboardView, wireLeaderboard } from "./leaderboard.js";
import { providerWords, remoteModels } from "./pack.js";
import { reportView, runOf } from "./report.js";
import { views, wire } from "./router.js";
import { $, announce, api, emptyState, esc, may, packPath, reducedMotion, slotText, state, time, toastError, whileBusy } from "./ui.js";

const evalsState = { reports: [] };

const EVAL_TABS = [
  ["compare", "Provider comparison"],
  ["report", "Benchmark report"],
  ["run", "Run on a dataset"],
  ["history", "History"],
];

const evalTabs = () => EVAL_TABS.filter(([key]) => key !== "compare" || state.detail.leaderboard);

views.evals = async (main, params) => {
  evalsState.reports = await api(`/api/evals?pack_id=${encodeURIComponent(state.packId)}`);
  main.innerHTML = `
    <h1>Evaluations</h1>
    <p class="muted">How the pack performs on labelled data, including your own.</p>
    <nav class="segmented tabs" aria-label="Evaluation views">${evalTabs().map(([key, label]) =>
      `<a href="#evals/${key}" data-tab="${key}">${esc(label)}</a>`).join("")}</nav>
    <div id="evals-body"></div>`;
  showEvalsTab(params[0]);
};

views.evals.update = (params) => showEvalsTab(params[0]);

function showEvalsTab(requested) {
  const tabs = evalTabs();
  const tab = tabs.some(([key]) => key === requested) ? requested : tabs[0][0];
  document.querySelectorAll(".tabs a[data-tab]").forEach((a) => {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  const body = $("#evals-body");
  if (body.dataset.tab && body.dataset.tab !== tab && !reducedMotion()) {
    body.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 160, easing: "ease-out" }); // a crossfade, not a cut
  }
  body.dataset.tab = tab; // an answer that arrives after the user moved on is dropped
  const empty = (text) => `<div class="panel empty">${text}</div>`;
  if (tab === "compare") {
    body.innerHTML = leaderboardView(state.detail.leaderboard);
    wireLeaderboard(body, state.detail.leaderboard);
  } else if (tab === "report") {
    body.innerHTML = state.detail.benchmark ? reportView(state.detail.benchmark) : empty("No benchmark published for this pack.");
  } else if (tab === "run") {
    showRunTab(body, empty).catch((error) => {
      if (body.dataset.tab === "run") body.innerHTML = empty(`Could not load the datasets: ${esc(error.message)}`);
    });
  } else {
    const reports = evalsState.reports;
    body.innerHTML = reports.length ? `
      <div class="row toolbar">
        <label for="history">Report</label>
        <select id="history">${reports.map((r, i) => `<option value="${i}">${esc(time(runOf(r).created_at))} · ${esc(r.dataset.name)} (n=${r.dataset.n})</option>`).join("")}</select>
      </div>
      <div id="history-report">${reportView(reports[0])}</div>` : emptyState({
      icon: "history", title: "No evaluations run here yet",
      text: "Run the pack on a dataset to see how this deployment does, next to the published benchmark.",
      action: { href: "#evals/run", label: "Run on a dataset" },
    });
    $("#history")?.addEventListener("change", (e) => {
      $("#history-report").innerHTML = reportView(reports[Number(e.target.value)]);
      wire($("#history-report"));
    });
  }
  wire(body);
}

async function showRunTab(body, empty) {
  if (!may("evaluate")) {
    body.innerHTML = empty("Running an evaluation is for administrators. The published benchmark and past runs are on the other tabs.");
    return;
  }
  const datasets = await api(`${packPath()}/datasets`);
  if (body.dataset.tab !== "run") return;
  const preselected = datasets.find((d) => d.name.includes("local-site")) ?? datasets[0];
  const remote = remoteSlots();
  body.innerHTML = `
    <div class="row toolbar">
      <label for="dataset">Dataset</label>
      <select id="dataset">${datasets.map((d) => `<option value="${esc(d.name)}" ${d === preselected ? "selected" : ""}>${esc(d.name)} (${d.n} records)</option>`).join("")}</select>
      <button type="button" class="primary" id="run-eval">Run evaluation</button>
    </div>
    <p class="muted mt-3">Anything other than the benchmark dataset is compared against the published benchmark (local validation).
      ${remote.length ? "The deployed configuration calls remote models, so you'll be asked to confirm first." : "The deployed configuration runs locally: no remote calls."}</p>
    <div id="run-report">${empty("Choose a dataset and run it; the report appears here.")}</div>`;
  $("#run-eval").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    const dataset = datasets.find((d) => d.name === $("#dataset").value);
    if (remote.length && !(await confirmRun(dataset, remote))) return button.focus();
    const stopProgress = runProgress($("#run-report"), dataset);
    $("#dataset").disabled = true;
    await whileBusy(button, "Running…", async () => {
      try {
        const report = await api(`${packPath()}/evals`, { method: "POST", body: JSON.stringify({ dataset: dataset.name }) });
        evalsState.reports.unshift(report);
        $("#run-report").innerHTML = reportView(report);
        wire($("#run-report"));
      } catch (error) {
        $("#run-report").innerHTML = empty("The run did not finish. Choose a dataset and try again.");
        toastError(error.message);
      } finally {
        stopProgress();
        $("#dataset").disabled = false;
      }
    });
  });
}

// A run is one request that returns when every record is done, so progress can't be a percentage:
// an indeterminate bar says it is alive, and the elapsed time says for how long.
function runProgress(container, dataset) {
  const started = Date.now();
  container.innerHTML = `<div class="panel run-progress">
      <div class="progress" aria-hidden="true"><span></span></div>
      <p>Running <b>${dataset.n}</b> records of <b>${esc(dataset.name)}</b> through the deployed configuration.
        <span class="muted">Elapsed <span class="elapsed">0 s</span></span></p>
      <p class="muted">The report appears here when every record is done.</p>
    </div>`;
  announce(`Running ${dataset.n} records. The report appears when every record is done.`);
  const elapsed = container.querySelector(".elapsed");
  const timer = setInterval(() => (elapsed.textContent = elapsedText(Date.now() - started)), 1000);
  return () => clearInterval(timer);
}

const elapsedText = (ms) => {
  const seconds = Math.floor(ms / 1000);
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
};

// Slots whose deployed provider calls a remote model (Jev, an LLM), in words.
const remoteSlots = () => Object.entries(state.detail.active_providers)
  .filter(([, p]) => remoteModels(p.model).length)
  .map(([slot, p]) => `${esc(slotText(slot))}: ${providerWords(p)}`);

// Before an evaluation that calls remote models: what it runs, on how many records, and which
// models it calls. No cost estimate is invented; the numbers shown are the ones we know.
function confirmRun(dataset, remote) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm";
    dialog.setAttribute("aria-labelledby", "confirm-title");
    dialog.innerHTML = `<form method="dialog">
      <h2 id="confirm-title">Run this evaluation?</h2>
      <p>It runs all <b>${dataset.n}</b> records of <b>${esc(dataset.name)}</b> through the deployed configuration, which calls these remote models for each record:</p>
      <ul>${remote.map((r) => `<li>${r}</li>`).join("")}</ul>
      <p class="muted">These are real, metered calls. The report is saved to this deployment's history.</p>
      <div class="row mt-4"><span class="grow"></span><button value="cancel" autofocus>Cancel</button><button value="run" class="primary">Run ${dataset.n} records</button></div>
    </form>`;
    document.body.append(dialog);
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "run");
      dialog.remove();
    });
    dialog.showModal();
  });
}

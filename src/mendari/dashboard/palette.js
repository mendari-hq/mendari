// SPDX-License-Identifier: Apache-2.0
// Command palette (Cmd/Ctrl+K), G-chord view navigation, and keyboard shortcuts reference (?).
// Uses native <dialog> for modal focus management and Escape handling; suppresses global chords
// while typing in form controls or while another dialog or drawer is open.

import { drawerOpen } from "./drawer.js";
import { $, esc, icon, state } from "./ui.js";

const VIEW_SHORTCUTS = {
  o: "overview",
  r: "review",
  a: "audit",
  e: "evals",
  m: "monitoring",
  f: "feedback",
  c: "card",
  t: "try",
};

const DECISION_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){0,3}(?:-[0-9a-f]{1,12})?$/i;

const isTyping = (target) => {
  const el = target?.closest?.("input, textarea, select, [contenteditable=''], [contenteditable='true']");
  return Boolean(el);
};

const modalOpen = () => Boolean(document.querySelector("dialog[open]")) || drawerOpen();

export function setupPalette({ onSelectAgent }) {
  const trigger = $("#palette-trigger");
  if (trigger) {
    const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
    const kbd = trigger.querySelector("kbd");
    if (kbd) kbd.textContent = isMac ? "⌘K" : "Ctrl K";
    trigger.addEventListener("click", () => openPalette(onSelectAgent));
  }

  let gChordTimer = null;
  let waitingForG = false;
  const clearChord = () => {
    waitingForG = false;
    clearTimeout(gChordTimer);
  };

  document.addEventListener("keydown", (event) => {
    const mod = event.metaKey || event.ctrlKey;
    if (mod && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "k") {
      event.preventDefault();
      clearChord();
      if (document.querySelector("dialog.palette[open]")) {
        document.querySelector("dialog.palette[open]").close();
      } else if (!modalOpen()) {
        openPalette(onSelectAgent);
      }
      return;
    }
    if (mod || event.altKey || isTyping(event.target) || modalOpen()) {
      clearChord();
      return;
    }
    const key = event.key.toLowerCase();
    if (waitingForG) {
      // The key after G belongs to the chord, whatever it names: a view's own keys (the review
      // queue's C confirms) leave a key whose default is prevented alone.
      clearChord();
      event.preventDefault();
      const view = VIEW_SHORTCUTS[key];
      if (view && isViewVisible(view)) location.hash = `#${view}`;
      return;
    }
    if (key === "g" && !event.shiftKey) {
      waitingForG = true;
      gChordTimer = setTimeout(clearChord, 900);
    } else if (event.key === "?") {
      event.preventDefault();
      openShortcuts();
    }
  });
}

const isViewVisible = (view) => Boolean(document.querySelector(`.nav a[data-view="${view}"]:not([hidden])`));

function buildItems(query, onSelectAgent) {
  const items = [];
  const trimmed = query.trim();
  const q = trimmed.toLowerCase();

  // Direct Decision ID jump when the user types or pastes a hex prefix / UUID.
  if (DECISION_ID.test(trimmed) && isViewVisible("audit")) {
    items.push({
      group: "Decision",
      icon: "scroll-text",
      label: `Open decision ${trimmed}`,
      detail: "Audit trail",
      run: () => { location.hash = `#audit/${encodeURIComponent(trimmed)}`; },
    });
  }

  // Visible navigation views.
  const navLinks = [...document.querySelectorAll(".nav a[data-view]:not([hidden])")];
  const shortcutByView = Object.fromEntries(Object.entries(VIEW_SHORTCUTS).map(([k, v]) => [v, `G ${k.toUpperCase()}`]));
  for (const a of navLinks) {
    const view = a.dataset.view;
    const label = a.querySelector(".nav-label")?.textContent?.trim() || a.textContent.trim();
    items.push({
      group: "Views",
      icon: a.querySelector("use")?.getAttribute("href")?.replace(/^#i-/, "") || "arrow-right",
      label,
      detail: view === state.view ? "Current" : "",
      shortcut: shortcutByView[view] ?? "",
      search: `${label} ${view}`,
      run: () => { location.hash = a.getAttribute("href"); },
    });
  }

  // Governed agents (when available), enriched with live cross-agent queue counts.
  if (state.agents.length > 1 && !$("#agent-select")?.closest(".agent-picker")?.hidden) {
    for (const agent of state.agents) {
      const q = state.agentQueue?.get?.(agent.id);
      const queueNote = q?.pending
        ? ` · ${q.pending} waiting${q.stat ? ` (${q.stat} stat)` : ""}`
        : "";
      items.push({
        group: "Agents",
        icon: "package",
        label: agent.name,
        detail: `${agent.id} v${agent.version}${agent.id === state.agentId ? " · Active" : ""}${queueNote}`,
        search: `${agent.name} ${agent.id} ${agent.origin ?? ""} ${q?.stat ? "stat urgent" : ""}`,
        run: () => onSelectAgent(agent.id),
      });
    }
  }

  // Theme & quick actions.
  for (const [choice, label, ico] of [
    ["light", "Switch to Light theme", "sun"],
    ["dark", "Switch to Dark theme", "moon"],
    ["system", "Match system theme", "monitor"],
  ]) {
    items.push({
      group: "Actions",
      icon: ico,
      label,
      detail: "Appearance",
      search: `${label} theme appearance ${choice}`,
      run: () => document.querySelector(`[data-theme-choice="${choice}"]`)?.click(),
    });
  }
  items.push({
    group: "Actions",
    icon: "external",
    label: "Open Mendari Agent Hub",
    detail: "mendari-agent-hub.vercel.app",
    search: "agent hub registry browse vetted agents evals",
    run: () => window.open("https://mendari-agent-hub.vercel.app", "_blank", "noopener,noreferrer"),
  });
  items.push({
    group: "Actions",
    icon: "external",
    label: "Open API documentation",
    detail: "/docs",
    search: "api docs openapi fhir cds hooks reference",
    run: () => location.assign("/docs"),
  });
  items.push({
    group: "Actions",
    icon: "keyboard",
    label: "Keyboard shortcuts",
    shortcut: "?",
    search: "keyboard shortcuts hotkeys help",
    run: () => openShortcuts(),
  });

  if (!q) return items;
  const tokens = q.split(/\s+/).filter(Boolean);
  return items.filter((item) => {
    const hay = `${item.label} ${item.detail ?? ""} ${item.group} ${item.search ?? ""}`.toLowerCase();
    return tokens.every((t) => hay.includes(t));
  });
}

function openPalette(onSelectAgent) {
  const opener = document.activeElement;
  const dialog = document.createElement("dialog");
  dialog.className = "confirm palette";
  dialog.setAttribute("aria-label", "Command palette");
  dialog.innerHTML = `
    <div class="palette-head">
      ${icon("search")}
      <label class="sr-only" for="palette-input">Jump to a view, agent or action</label>
      <input id="palette-input" type="search" role="combobox" aria-expanded="true" aria-controls="palette-results" aria-autocomplete="list"
        autocomplete="off" spellcheck="false" placeholder="Jump to a view, agent, decision ID, or action…">
      <button type="button" class="icon-button" data-close aria-label="Close">${icon("x")}</button>
    </div>
    <div class="palette-results" id="palette-results" role="listbox" aria-label="Matches"></div>
    <div class="palette-foot muted">
      <span><kbd class="kbd">↑</kbd><kbd class="kbd">↓</kbd> Navigate</span>
      <span><kbd class="kbd">↵</kbd> Select</span>
      <span><kbd class="kbd">Esc</kbd> Close</span>
    </div>`;
  document.body.append(dialog);

  const input = dialog.querySelector("#palette-input");
  const results = dialog.querySelector("#palette-results");
  let matches = [];
  let active = 0;

  // Focus stays in the box; the arrows move the active option, which the box names
  // (aria-activedescendant), so a screen reader says each one as it is reached.
  const option = (item, i) => `<div role="option" id="palette-option-${i}" class="palette-item${i === active ? " is-active" : ""}" data-index="${i}" aria-selected="${i === active}">
        ${icon(item.icon)}
        <span class="palette-item-main">
          <span class="palette-item-label">${esc(item.label)}</span>
          ${item.detail ? `<span class="palette-item-detail muted">${esc(item.detail)}</span>` : ""}
        </span>
        ${item.shortcut ? `<kbd class="kbd">${esc(item.shortcut)}</kbd>` : ""}
      </div>`;
  const paint = () => {
    matches = buildItems(input.value, onSelectAgent);
    active = matches.length ? Math.min(active, matches.length - 1) : 0;
    if (!matches.length) {
      results.innerHTML = `<p class="palette-empty muted" role="presentation">No matching view, agent, or action.</p>`;
      input.removeAttribute("aria-activedescendant");
      return;
    }
    const groups = [];
    matches.forEach((item, i) => {
      if (groups.at(-1)?.name !== item.group) groups.push({ name: item.group, html: [] });
      groups.at(-1).html.push(option(item, i));
    });
    results.innerHTML = groups.map((group, g) => `<div role="group" aria-labelledby="palette-group-${g}">
        <div class="palette-group" id="palette-group-${g}" role="presentation">${esc(group.name)}</div>${group.html.join("")}
      </div>`).join("");
    input.setAttribute("aria-activedescendant", `palette-option-${active}`);
  };

  const highlight = (next) => {
    if (!matches.length) return;
    active = (next + matches.length) % matches.length;
    results.querySelectorAll(".palette-item").forEach((el, i) => {
      const on = i === active;
      el.classList.toggle("is-active", on);
      el.setAttribute("aria-selected", String(on));
      if (on) el.scrollIntoView({ block: "nearest" });
    });
    input.setAttribute("aria-activedescendant", `palette-option-${active}`);
  };

  const choose = (index) => {
    const item = matches[index];
    if (!item) return;
    dialog.close();
    item.run();
  };

  input.addEventListener("input", () => { active = 0; paint(); });
  results.addEventListener("click", (event) => {
    const button = event.target.closest(".palette-item");
    if (button) choose(Number(button.dataset.index));
  });
  results.addEventListener("mousemove", (event) => {
    const button = event.target.closest(".palette-item");
    if (button && Number(button.dataset.index) !== active) highlight(Number(button.dataset.index));
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog || event.target.closest("[data-close]")) dialog.close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(active + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(active - 1);
    } else if (event.key === "Enter" && matches.length) {
      event.preventDefault();
      choose(active);
    }
  });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (opener?.isConnected && document.activeElement === document.body) opener.focus();
  });

  paint();
  dialog.showModal();
  input.focus();
}

function openShortcuts() {
  if (document.querySelector("dialog.shortcuts[open]")) return;
  const opener = document.activeElement;
  const dialog = document.createElement("dialog");
  dialog.className = "confirm shortcuts";
  dialog.setAttribute("aria-labelledby", "shortcuts-title");
  const row = (keys, desc) => `<tr><td>${keys.map((k) => `<kbd class="kbd">${esc(k)}</kbd>`).join(" ")}</td><td>${esc(desc)}</td></tr>`;
  dialog.innerHTML = `<form method="dialog">
    <div class="row"><h2 id="shortcuts-title">Keyboard shortcuts</h2><span class="grow"></span>
      <button type="submit" class="icon-button" aria-label="Close">${icon("x")}</button></div>
    <table class="shortcuts-table">
      <tbody>
        ${row(["⌘K / Ctrl+K"], "Open command palette")}
        ${row(["?"], "Show keyboard shortcuts")}
        ${row(["G", "O"], "Go to Overview")}
        ${row(["G", "R"], "Go to Review queue")}
        ${row(["G", "A"], "Go to Audit trail")}
        ${row(["G", "E"], "Go to Evaluations")}
        ${row(["G", "M"], "Go to Monitoring")}
        ${row(["G", "F"], "Go to Feedback")}
        ${row(["G", "C"], "Go to Decision Card")}
        ${row(["G", "T"], "Go to Try it")}
        ${row(["J", "K"], "Review queue: next / previous card")}
        ${row(["C"], "Review queue: confirm focused recommendation")}
        ${row(["Z"], "Review queue: undo an override in its 5 seconds")}
        ${row(["[", "]"], "Audit drawer: previous / next decision")}
        ${row(["⌘↵ / Ctrl+↵"], "Try it: submit decision")}
      </tbody>
    </table>
    <div class="row mt-4"><span class="grow"></span><button type="submit" class="primary" autofocus>Done</button></div>
  </form>`;
  document.body.append(dialog);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (opener?.isConnected) opener.focus();
  });
  dialog.showModal();
}

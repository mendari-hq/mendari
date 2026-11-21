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

export function setupPalette({ onSelectPack }) {
  const trigger = $("#palette-trigger");
  if (trigger) {
    const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
    const kbd = trigger.querySelector("kbd");
    if (kbd) kbd.textContent = isMac ? "⌘K" : "Ctrl K";
    trigger.addEventListener("click", () => openPalette(onSelectPack));
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
        openPalette(onSelectPack);
      }
      return;
    }
    if (mod || event.altKey || isTyping(event.target) || modalOpen()) {
      clearChord();
      return;
    }
    const key = event.key.toLowerCase();
    if (waitingForG) {
      clearChord();
      const view = VIEW_SHORTCUTS[key];
      if (view && isViewVisible(view)) {
        event.preventDefault();
        location.hash = `#${view}`;
      }
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

function buildItems(query, onSelectPack) {
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
  if (state.packs.length > 1 && !$("#pack-select")?.closest(".pack-picker")?.hidden) {
    for (const pack of state.packs) {
      const q = state.packQueue?.get?.(pack.id);
      const queueNote = q?.pending
        ? ` · ${q.pending} waiting${q.stat ? ` (${q.stat} stat)` : ""}`
        : "";
      items.push({
        group: "Agents",
        icon: "package",
        label: pack.name,
        detail: `${pack.id} v${pack.version}${pack.id === state.packId ? " · Active" : ""}${queueNote}`,
        search: `${pack.name} ${pack.id} ${pack.origin ?? ""} ${q?.stat ? "stat urgent" : ""}`,
        run: () => onSelectPack(pack.id),
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

function openPalette(onSelectPack) {
  const opener = document.activeElement;
  const dialog = document.createElement("dialog");
  dialog.className = "confirm palette";
  dialog.setAttribute("aria-label", "Command palette");
  dialog.innerHTML = `
    <div class="palette-head">
      ${icon("search")}
      <label class="sr-only" for="palette-input">Jump to a view, agent or action</label>
      <input id="palette-input" type="search" autocomplete="off" spellcheck="false" placeholder="Jump to a view, agent, decision ID, or action…">
      <button type="button" class="icon-button" data-close aria-label="Close">${icon("x")}</button>
    </div>
    <div class="palette-results" id="palette-results"></div>
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

  const paint = () => {
    matches = buildItems(input.value, onSelectPack);
    active = matches.length ? Math.min(active, matches.length - 1) : 0;
    if (!matches.length) {
      results.innerHTML = `<p class="palette-empty muted">No matching view, agent, or action.</p>`;
      return;
    }
    let lastGroup = "";
    results.innerHTML = matches.map((item, i) => {
      const heading = item.group !== lastGroup ? `<div class="palette-group">${esc(item.group)}</div>` : "";
      lastGroup = item.group;
      return `${heading}<button type="button" class="palette-item${i === active ? " is-active" : ""}" data-index="${i}"${i === active ? ' aria-current="true"' : ""}>
        ${icon(item.icon)}
        <span class="palette-item-main">
          <span class="palette-item-label">${esc(item.label)}</span>
          ${item.detail ? `<span class="palette-item-detail muted">${esc(item.detail)}</span>` : ""}
        </span>
        ${item.shortcut ? `<kbd class="kbd">${esc(item.shortcut)}</kbd>` : ""}
      </button>`;
    }).join("");
  };

  const highlight = (next) => {
    if (!matches.length) return;
    active = (next + matches.length) % matches.length;
    results.querySelectorAll(".palette-item").forEach((el, i) => {
      const on = i === active;
      el.classList.toggle("is-active", on);
      if (on) {
        el.setAttribute("aria-current", "true");
        el.scrollIntoView({ block: "nearest" });
      } else {
        el.removeAttribute("aria-current");
      }
    });
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

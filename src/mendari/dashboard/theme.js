// SPDX-License-Identifier: Apache-2.0
// Theme: System / Light / Dark, set before first paint by index.html and kept in sync here.

import { storageGet, storageSet } from "./ui.js";

// index.html resolves the theme before first paint; this keeps it in sync with the toggle, and with
// the system setting while "System" is chosen.
const THEME_CHOICES = ["system", "light", "dark"];

const systemDark = matchMedia("(prefers-color-scheme: dark)");

const themeChoice = () => {
  const choice = storageGet("oh.theme", "system");
  return THEME_CHOICES.includes(choice) ? choice : "system";
};

function applyTheme() {
  const choice = themeChoice();
  const dark = choice === "dark" || (choice === "system" && systemDark.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === choice)));
}

export function setupTheme() {
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => {
    storageSet("oh.theme", b.dataset.themeChoice);
    applyTheme();
  }));
  systemDark.addEventListener("change", applyTheme);
  applyTheme();
}

// Phones: the theme and the pack sit in a sheet under the top bar, opened by the Settings button (a
// disclosure: the sheet follows the button in the tab order). Escape, a click elsewhere or choosing a
// pack closes it. Wider screens hide the button and show the controls in the bar.

import { $ } from "./ui.js";

export function setupSettings() {
  const bar = $(".topbar");
  const button = $("#settings-button");
  const setOpen = (open) => {
    bar.classList.toggle("settings-open", open);
    button.setAttribute("aria-expanded", String(open));
  };
  const isOpen = () => button.getAttribute("aria-expanded") === "true";
  button.addEventListener("click", () => setOpen(!isOpen()));
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isOpen()) return;
    setOpen(false);
    button.focus();
  });
  document.addEventListener("click", (event) => {
    if (isOpen() && !bar.contains(event.target)) setOpen(false);
  });
  $("#pack-select").addEventListener("change", () => setOpen(false));
}

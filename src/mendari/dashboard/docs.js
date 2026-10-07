// SPDX-License-Identifier: Apache-2.0
// The developer landing at /docs (docs.html, filled by server/landing.py): the theme toggle, this
// server's address in the examples, and copy buttons. Everything else is static HTML, so the page
// reads the same without JavaScript; only the copy buttons stay hidden.

import { setupTheme } from "./theme.js";
import { announce, icon } from "./ui.js";

setupTheme();

// The examples call the server $MENDARI. The page names this one from the address bar: the server
// never writes a request's Host header into a page.
document.querySelectorAll("[data-origin]").forEach((el) => (el.textContent = location.origin));

for (const button of document.querySelectorAll("[data-copy]")) {
  button.hidden = false;
  button.addEventListener("click", () => copy(button));
}

// Copies the example exactly as shown. Without a clipboard (a page not served over HTTPS, or the
// permission refused) the example is selected instead, to copy with the keyboard.
async function copy(button) {
  const code = document.getElementById(button.dataset.copy);
  try {
    await navigator.clipboard.writeText(code.textContent);
  } catch {
    getSelection().selectAllChildren(code);
    announce("Copying isn't available here: the example is selected, copy it with Ctrl+C or Command+C", { error: true });
    return;
  }
  button.innerHTML = `${icon("check")}<span>Copied</span>`;
  button.classList.add("copied");
  announce("Copied to the clipboard");
  clearTimeout(button.timer);
  button.timer = setTimeout(() => {
    button.innerHTML = `${icon("copy")}<span>Copy</span>`;
    button.classList.remove("copied");
  }, 2000);
}

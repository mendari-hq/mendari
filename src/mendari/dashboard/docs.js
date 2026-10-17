// SPDX-License-Identifier: Apache-2.0
// The developer landing at /docs (docs.html, filled by server/landing.py): the theme toggle, this
// server's address in the examples, copy buttons, and the OpenAPI document saved through a fetch.
// Everything else is static HTML, so the page reads the same without JavaScript; only the copy
// buttons stay hidden.

import { setupTheme } from "./theme.js";
import { announce, icon, OWN_REQUEST, saveFile } from "./ui.js";

setupTheme();

// The examples call the server $MENDARI. The page names this one from the address bar: the server
// never writes a request's Host header into a page.
document.querySelectorAll("[data-origin]").forEach((el) => (el.textContent = location.origin));

// The OpenAPI document needs a signed-in reader: fetched with Mendari's own header (ui.js) and
// saved, as the dashboard's downloads are, since a browser without Fetch Metadata is refused a
// plain link to it (server/origin.py). Any other answer (signed out, say) is shown as the link
// would show it.
document.querySelector("[data-openapi]")?.addEventListener("click", async (event) => {
  event.preventDefault();
  const link = event.currentTarget;
  try {
    const response = await fetch(link.href, { headers: OWN_REQUEST });
    if (!response.ok) throw new Error(response.statusText);
    saveFile(await response.blob(), link.getAttribute("download"));
  } catch {
    location.assign(link.href);
  }
});

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

// SPDX-License-Identifier: Apache-2.0
// The API reference at /docs/reference: Swagger UI, vendored (scripts/vendor_docs_ui.py), on this
// deployment's OpenAPI document. It runs after the deferred bundle, which defines SwaggerUIBundle.
// Its requests (the document, "Try it out", and the links to the document below) carry
// Mendari's own header, as every page's do (ui.js `OWN_REQUEST`; a test keeps them equal): a
// browser without Fetch Metadata is refused a read with the session cookie without it
// (server/origin.py).
const OWN_REQUEST = { "x-mendari-request": "1" };

// The dashboard's theme choice (theme.js: "oh.theme", System / Light / Dark), shown the way
// Swagger UI does dark mode: a class on <html>.
const systemDark = matchMedia("(prefers-color-scheme: dark)");

function applyTheme() {
  let choice = "system";
  try { choice = localStorage.getItem("oh.theme") || "system"; } catch { /* private mode */ }
  const dark = choice === "dark" || (choice !== "light" && systemDark.matches);
  document.documentElement.classList.toggle("dark-mode", dark);
}

applyTheme();
systemDark.addEventListener("change", applyTheme);
addEventListener("storage", (event) => { if (event.key === "oh.theme") applyTheme(); });

window.SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#reference",
  deepLinking: true,
  // Swagger UI would otherwise send this deployment's OpenAPI document to validator.swagger.io.
  validatorUrl: null,
  // Settings come from this file only, never from the page's URL (?url=, ?configUrl=).
  queryConfigEnabled: false,
  // A key or token typed into "Authorize" lives in this page only, never in localStorage.
  persistAuthorization: false,
  requestInterceptor: (request) => {
    request.headers = { ...request.headers, ...OWN_REQUEST };
    return request;
  },
});

// The page links the OpenAPI document twice (Swagger UI's link under the title, and the
// introduction's), and a plain link goes without Mendari's header: a browser without Fetch
// Metadata would be refused it. So a click on either fetches the document with the header and
// saves it, as the landing's link does (docs.js); any other answer (signed out, say) is shown as
// the link would show it.
document.addEventListener("click", async (event) => {
  const link = event.target.closest?.("a[href]");
  if (!link || link.origin !== location.origin || link.pathname !== "/openapi.json") return;
  event.preventDefault();
  try {
    const response = await fetch(link.href, { headers: OWN_REQUEST });
    if (!response.ok) throw new Error(response.statusText);
    const url = URL.createObjectURL(await response.blob());
    const save = Object.assign(document.createElement("a"), { href: url, download: "openapi.json" });
    document.body.append(save);
    save.click();
    save.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch {
    location.assign(link.href);
  }
});

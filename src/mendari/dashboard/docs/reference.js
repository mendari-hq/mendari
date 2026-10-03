// The API reference at /docs/reference: Swagger UI, vendored (scripts/vendor_docs_ui.py), on this
// deployment's OpenAPI document. It runs after the deferred bundle, which defines SwaggerUIBundle.

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
});

# mendari.co

The public page: static HTML and CSS, no scripts and no third-party requests. It is its own
Vercel project, separate from the engine's, and deploys from this directory with the CLI (run it
inside `web/`: with `--cwd web` the CLI reads the engine's `vercel.json` at the repository root):

```bash
cd web && vercel deploy --prod
```

Domains: `mendari.co`; `www.mendari.co`, `mendari.ai` and `www.mendari.ai` redirect to it (308).
Preview it locally with a static file server from `web/`, for example
`python3 -m http.server -d web 8080`: the page uses root paths (`/style.css`, `/fonts/`), so
opening `index.html` directly shows it unstyled.

The brand: cobalt (`#1F4FC9`) is the one brand colour, and the four outcome colours are the
dashboard's. Type is Newsreader, Public Sans and IBM Plex Mono, self-hosted from `fonts/` beside
their SIL Open Font License texts (listed in `NOTICE`); `vercel.json`'s CSP allows `font-src 'self'`
for them and nothing else from outside. `og.png` is the 1200 × 630 link preview; `favicon.svg`
is the mark below 32 px, the same diamond the dashboard uses.

The scores (the Patient Inbox Router's public `test` set) sit between two marker comments and
are written by `uv run python scripts/scoreboard_page.py` from the scoreboard's results store
(`scoreboard/`): never edit them by hand. Its `--check`, a step of CI's `reference-pack` job,
fails when they are not the store's. Each figure is k of n with its exact 95% interval, the set
is public (a regression check, not a ranking), and a remote model is named only once its
result's disclosure is stated. The packs' figures further down are from their committed
benchmarks (`packs/*/evals/benchmark.json`) and are typed in: change them when those change.

`.well-known/security.txt` (RFC 9116) names the repository's private vulnerability reporting form
as the security contact and links `SECURITY.md`. Its `Expires` is at most a year ahead: renew it
and deploy again before it passes (`tests/test_security_txt.py` fails 30 days before).

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

`terms.html`, `privacy.html` and `acceptable-use.html` (served as `/terms`, `/privacy` and
`/acceptable-use` by `cleanUrls`, and linked from every page's footer) are **drafts pending
legal review**: each says so under its heading, is `noindex`, and marks every fact still to be
supplied as a bracketed placeholder (`class="placeholder"`), listed with the questions for the
owner and counsel in `notes/legal-pages-open-questions.md`. State in them only what the code and
docs support, and change the "last updated" date with the text (`tests/test_web_legal.py`).
Removing the draft marker is the owner's decision, after counsel's review.

The brand: cobalt (`#1F4FC9`) is the one brand colour, and the four outcome colours are the
dashboard's. Type is Newsreader, Public Sans and IBM Plex Mono, self-hosted from `fonts/` beside
their SIL Open Font License texts (listed in `NOTICE`); `vercel.json`'s CSP allows `font-src 'self'`
for them and nothing else from outside. `og.png` is the 1200 × 630 link preview; `favicon.svg`
is the mark below 32 px, the same diamond the dashboard uses.

The scores sit between two marker comments and are written by
`uv run python scripts/scoreboard_page.py` from the scoreboard's results store (`scoreboard/`):
never edit them by hand. No result is stored yet for the bundled demo agent, and the block says
so. Each figure, once there is one, is k of n with its exact 95% interval, the set is public (a
regression check, not a ranking), and a remote model, or a configuration that calls one, is named
only once its provider type's disclosure is stated. The intro above the markers is typed in: keep
it to what the block shows.

The example decision at the top and the demo agent's figure further down (12 of 12 on its bundled
examples) are typed in: they are what `mendari/portal-message-agent` (`agents/portal-message-agent/`)
decides on that message and what `mendari eval agents/portal-message-agent --dataset examples`
prints. Change them when the agent changes (`tests/test_agent_provider.py` checks both).

`.well-known/security.txt` (RFC 9116) names the repository's private vulnerability reporting form
as the security contact and links `SECURITY.md`. Its `Expires` is at most a year ahead: renew it
and deploy again before it passes (`tests/test_security_txt.py` fails 30 days before).

# mendari.co

The public "coming soon" page: static HTML and CSS, no scripts, and no links to the repository
(it is private). It is its own Vercel project, `mendari-web`, separate from the engine's, and
deploys from this directory with the CLI (run it inside `web/`: with `--cwd web` the CLI reads
the engine's `vercel.json` at the repository root):

```bash
cd web && vercel deploy --prod
```

Domains: `mendari.co`; `www.mendari.co`, `mendari.ai` and `www.mendari.ai` redirect to it (308).
DNS is at Porkbun. Preview it locally with the `web` configuration in `.claude/launch.json`.

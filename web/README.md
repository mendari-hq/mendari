# mendari.co

The public "coming soon" page: static HTML and CSS, no scripts. It is its own Vercel project,
separate from the engine's, and deploys from this directory with the CLI (run it inside `web/`: with `--cwd web` the CLI reads
the engine's `vercel.json` at the repository root):

```bash
cd web && vercel deploy --prod
```

Domains: `mendari.co`; `www.mendari.co`, `mendari.ai` and `www.mendari.ai` redirect to it (308).
Preview it locally by opening `index.html`, or with any static file server.

`.well-known/security.txt` (RFC 9116) names the repository's private vulnerability reporting form
as the security contact and links `SECURITY.md`. Its `Expires` is at most a year ahead: renew it
and deploy again before it passes (`tests/test_security_txt.py` fails 30 days before).

# Mendari on one machine

Everything a hospital or a clinic needs to run one deployment, in this folder: the engine, its
database, its scheduled jobs and backups, and a front that serves https. The machine needs
Docker and nothing else.

```bash
./mendari-site install
```

It asks seven things only you can answer (the address people will open, your identity
provider's issuer, client id and client secret, the first administrator's sign-in address, what
your organization is called, and whether each case's input is kept) and one you may leave for
later (the agent registry you adopt agents from), generates everything else, checks the result
as the server will read it, and starts. Run it again to change an answer: a secret it generated
is never made twice, and no data is lost.

From there on the console is enough: the first administrator signs in, follows "Finish setting
up", and adopts an agent in the Agents view.

Follow the guide, which is one page: [`docs/guides/install-at-your-site.md`](../../docs/guides/install-at-your-site.md).

**Status.** Mendari is a research preview. It is not a medical device, and it is not to be used
with real patient data until its external security review is done.

## What is here

| File | What it is |
|---|---|
| `mendari-site` | The one command: `install`, `status`, `start`, `stop`, `upgrade`, `rollback`, `backup`, `restore`, `verify`, `hold`, `holds`, `lift`, `logs`, `forget`. `./mendari-site help` lists them. |
| `docker-compose.yml` | The five services: `postgres`, `migrate` (runs once at each start), `engine`, `proxy` and `jobs`. Use `mendari-site`, not this file by hand. |
| `proxy.conf` | The front: TLS with the certificate in `tls/`, http redirected to https, a limit per address on sign-in, and a log that never holds a query string. |
| `jobs.sh` | The daily jobs, in order: retention, the audit trail's checkpoint, the drift check, a backup. A job that fails is said, never passed over. |

## What an install adds

| Made by `install` | What it is |
|---|---|
| `.env` | Every setting and every generated secret, readable by its owner only. Keep a copy where your organization keeps its secrets. |
| `tls/` | `fullchain.pem` and `privkey.pem`: your certificate and its key, which you put there. With `--self-signed`, a trial certificate and a note that says so. |
| `backups/` | A backup every night, kept 14 days. Copy them off this machine. |

Git ignores all three.

## The other ways to run

This folder is for one machine. A cluster uses [`../kubernetes/`](../kubernetes), a server
where you bring your own proxy and schedule uses [`../onprem/`](../onprem), and the project's
own hosted site is described in [`../vercel/`](../vercel). The long reference for every setting
is [`docs/deploying-with-phi.md`](../../docs/deploying-with-phi.md).

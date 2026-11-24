# Deploying Mendari at your organization

**Start with [`site/`](site): one machine, one command.** `./mendari-site install` asks the few
things only you can answer, generates every secret, and starts the engine, its database, its
scheduled jobs and backups, and a front that serves https. The one-page guide is
[`docs/guides/install-at-your-site.md`](../docs/guides/install-at-your-site.md).

The other templates run the engine on infrastructure you already operate, with Postgres, your
identity provider, PHI mode and the scheduled jobs. Read
[`docs/deploying-with-phi.md`](../docs/deploying-with-phi.md) first: it explains every setting
below and the checks to run before go-live.

| Template | For |
|---|---|
| [`site/`](site) | One machine with Docker and nothing else to bring: `./mendari-site install` |
| [`onprem/`](onprem) | One server with Docker where you bring the proxy, the certificate and the backups: `docker compose --env-file .env up -d` |
| [`kubernetes/`](kubernetes) | A cluster: `kubectl apply -f kubernetes/mendari.yaml` after creating the secret |
| [`onprem/nginx-limits.conf`](onprem/nginx-limits.conf) | A reverse proxy in front of either, with per-address limits on sign-in |
| [`vercel/`](vercel) | The project's own Vercel deployment: its firewall rule and runbook |

A release publishes one image to GitHub Container Registry (workflow `release`, started by a
maintainer, never by a tag push alone), for linux/amd64 and linux/arm64: `mendari-engine`, which
serves every bundled agent. No release has been tagged yet, so build your own until then:
`docker build .`, or, for an image that serves only one agent,
`docker build --build-arg AGENT=mendari/portal-message-agent .`.

The image installs exactly the dependencies in `uv.lock`, on a base image pinned by digest.
**Deploy by digest, not by tag**: `ghcr.io/mendari-hq/mendari-engine:v0.1.0@sha256:<digest>`
(a release's tag is `v` and the package version, so the first is `v0.1.0`; `docker buildx
imagetools inspect <image>:<tag>` shows the digest), in `MENDARI_IMAGE` for docker
compose, with `./mendari-site install --image` for `site/`, and in both `image:` fields for
Kubernetes. A tag can be pushed again; a digest always names the same image you tested.

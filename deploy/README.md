# Deploying Mendari at your organization

Two templates for running the engine on your own infrastructure, with Postgres, your identity
provider, PHI mode and the scheduled jobs. Read [`docs/deploying-with-phi.md`](../docs/deploying-with-phi.md)
first: it explains every setting below and the checks to run before go-live.

| Template | For |
|---|---|
| [`onprem/`](onprem) | One server with Docker: `docker compose --env-file .env up -d` |
| [`kubernetes/`](kubernetes) | A cluster: `kubectl apply -f kubernetes/mendari.yaml` after creating the secret |
| [`onprem/nginx-limits.conf`](onprem/nginx-limits.conf) | A reverse proxy in front of either, with per-address limits on sign-in |
| [`vercel/`](vercel) | The project's own Vercel deployment: its firewall rule and runbook |

Images are published to GitHub Container Registry by a release (workflow `release`, started by
a maintainer, never by a tag push alone): `mendari-engine` (every pack) or one image per pack, such as
`mendari-referral-router`, which serves only that pack. No release has been tagged yet, so
build your own until then: `docker build --build-arg PACK=mendari/referral-router .`.

The image installs exactly the dependencies in `uv.lock`, on a base image pinned by digest.
**Deploy by digest, not by tag**: `ghcr.io/junebugg1214/mendari-engine:v0.1.0@sha256:<digest>`
(a release's tag is `v` and the package version, so the first is `v0.1.0`; `docker buildx
imagetools inspect <image>:<tag>` shows the digest), in `MENDARI_IMAGE` for docker
compose and in both `image:` fields for Kubernetes. A tag can be pushed again; a digest always
names the same image you tested.

# Deploying Mendari at your organization

Two templates for running the engine on your own infrastructure, with Postgres, your identity
provider, PHI mode and the scheduled jobs. Read [`docs/deploying-with-phi.md`](../docs/deploying-with-phi.md)
first: it explains every setting below and the checks to run before go-live.

| Template | For |
|---|---|
| [`onprem/`](onprem) | One server with Docker: `docker compose --env-file .env up -d` |
| [`kubernetes/`](kubernetes) | A cluster: `kubectl apply -f kubernetes/mendari.yaml` after creating the secret |

Images are published to GitHub Container Registry when a release is tagged (`v*`, workflow
`images`): `mendari-engine` (every pack) or one image per pack, such as
`mendari-referral-router`, which serves only that pack. No release has been tagged yet, so
build your own until then: `docker build --build-arg PACK=mendari/referral-router .`.

The image installs exactly the dependencies in `uv.lock`, on a base image pinned by digest.
**Deploy by digest, not by tag**: `ghcr.io/junebugg1214/mendari-engine:v0.6.0@sha256:<digest>`
(`docker buildx imagetools inspect <image>:<tag>` shows it), in `MENDARI_IMAGE` for docker
compose and in both `image:` fields for Kubernetes. A tag can be pushed again; a digest always
names the same image you tested.

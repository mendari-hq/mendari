# Deploying OpenHealth at your organization

Two templates for running the engine on your own infrastructure, with Postgres, your identity
provider, PHI mode and the scheduled jobs. Read [`docs/deploying-with-phi.md`](../docs/deploying-with-phi.md)
first: it explains every setting below and the checks to run before go-live.

| Template | For |
|---|---|
| [`onprem/`](onprem) | One server with Docker: `docker compose --env-file .env up -d` |
| [`kubernetes/`](kubernetes) | A cluster: `kubectl apply -f kubernetes/openhealth.yaml` after creating the secret |

Images are published to GitHub Container Registry when a release is tagged (`v*`, workflow
`images`): `openhealth-engine` (every pack) or one image per pack, such as
`openhealth-referral-router`, which serves only that pack. No release has been tagged yet, so
build your own until then: `docker build --build-arg PACK=openhealth/referral-router .`.

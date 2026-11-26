# Install Mendari at your site

One machine, one command, under an hour: from a blank server to an administrator signed in
through your own identity provider. This page is for the person in a hospital's or a clinic's IT
who runs that machine. It needs no Python and no knowledge of Mendari's internals.

!!! warning "Status: a research preview"
    Mendari is a research preview. It is **not a medical device**, and it is **not to be used
    with real patient data until its external security review is done**. Install it to
    evaluate it, on synthetic data. The settings below are the ones a deployment with patient
    data will need, so that what you evaluate is what you would run.

## What you get

`./mendari-site install` starts five things on one machine with Docker, and nothing else:

| Service | What it does |
|---|---|
| `proxy` | The only one the network reaches. It serves https with your certificate, sends plain http to https, and limits sign-in attempts per address. |
| `engine` | Mendari: the console, the API, the audit trail. |
| `postgres` | The database, never published on the machine's ports, its data in a Docker volume. |
| `migrate` | Runs once at each start, before the engine: it brings the database up to date, and the engine does not start if it fails. |
| `jobs` | Once a day: deletes kept inputs past their retention, verifies the audit trail and records its signed checkpoint, checks every agent for drift, and backs the database up. |

Every service restarts with the machine, has a memory limit, and writes bounded logs that never
hold a request's body, a query string or a database statement.

## Before you start

| You need | Why |
|---|---|
| A Linux machine (x86-64 or arm64) with 2 CPUs, 4 GB of memory and 20 GB of disk | It runs everything. |
| [Docker Engine](https://docs.docker.com/engine/install/) with the Compose plugin (`docker compose version` answers) | The only software the machine needs. |
| A DNS name for the machine, such as `mendari.example.org`, and ports 443 and 80 open to the people who will use it | The address they open. Port 80 only redirects to https. |
| The TLS certificate for that name (with its chain) and its private key, as PEM files | A deployment people sign in to is never served over plain http. For a trial on a private network, the installer can make a self-signed certificate instead. |
| A client registered for Mendari at your identity provider (Entra ID, Okta, Keycloak, any OpenID Connect provider) | People sign in through it. Register a **web application** with the redirect address `https://<your name>/auth/callback` and the scopes `openid`, `profile` and `email`. Note its **issuer address**, **client id** and **client secret**. The provider must say that each person's e-mail address is verified (`email_verified`). |
| The sign-in e-mail address of the person who will administer Mendari | They become the first administrator at their first sign-in. |

## Install

```bash
git clone https://github.com/mendari-hq/mendari.git
cd mendari/deploy/site
mkdir -p tls
cp /path/to/your/fullchain.pem /path/to/your/privkey.pem tls/
./mendari-site install
```

Without `--image`, the script builds Mendari's image from the copy you cloned (a few minutes the
first time). Name a release's image by digest instead, so that what runs is exactly what was
published:

```bash
./mendari-site install --image ghcr.io/mendari-hq/mendari-engine:v0.1.0@sha256:<digest>
```

For a trial on a private network without a certificate, add `--self-signed`. Browsers will warn
about that certificate, and `tls/SELF-SIGNED.txt` says what it is. Replace it with your own
before anyone else uses the deployment.

The script stops with one plain line if something is missing or wrong: Docker, the certificate,
an answer the server would refuse, a database it cannot bring up to date. Nothing half-working
is left running. Correct what the line says and run it again.

### What it asks, and why

| Question | Why only you can answer it |
|---|---|
| The address people will open in their browser | Your DNS name, as `https://mendari.example.org`. Sign-in sends people back to it, and the certificate must be for it. |
| Your identity provider's issuer address | Where Mendari finds your provider's sign-in pages and keys. |
| The client id your identity provider gave Mendari | Which registered application Mendari is. |
| Its client secret | Typed hidden, written to `.env` and nowhere else. |
| The first administrator's sign-in address | Who may set everything else up. See [First sign-in](#first-sign-in). |
| What your organization is called | Shown in the console and in invitations. |
| Whether each case's input is kept, encrypted | **Yes**: inputs are sealed with a key made for this deployment, read only by reviewers and administrators, each read recorded, and deleted after 30 days. **No**: no input is ever stored, only a keyed fingerprint, and reviewers cannot read a case in the console. This is chosen once: installing again does not change it. |

Then one question you may leave for later, by pressing Enter:

| Question | What it is for |
|---|---|
| The agent registry to adopt agents from | The https address of the registry the console searches and adds agents from: the Agent Hub's, or a registry of your own. Without one the console's Agents view has nothing to search, and the System view says so. Run the command again to give it; type `none` to have none. |

Everything else is generated or set to a safe default. Run the command again at any time to
change an answer: each question then offers what is there as its default.

**It is safe to run twice.** A secret it generated is never made again, your own additions to
`.env` are kept, and no data is touched. If `.env` cannot be read, or has lost one of its
generated secrets, the script stops rather than make a new one: a new database password would
not open the database, and a new sealing key would not open what the old one sealed.

### Without questions

For an unattended install, give the answers in a file (`NAME=value` lines, quoted as in a shell
when a value has spaces):

```bash
./mendari-site install --answers answers.env
```

```
PUBLIC_URL=https://mendari.example.org
OIDC_ISSUER=https://login.example.org/realms/clinic
OIDC_CLIENT_ID=mendari
OIDC_CLIENT_SECRET=...
ADMIN_EMAIL=ada@example.org
ORGANIZATION="St Mary's Hospital"
PATIENT_DATA=yes
```

It may also name `REGISTRY_URL` (the agent registry you adopt agents from), `TIMEZONE` (the
machine's is used otherwise), `ALERT_WEBHOOK`, `RETENTION_DAYS`, `BACKUP_KEEP_DAYS`,
`HTTPS_PORT`, `HTTP_PORT` and `SELF_SIGNED=yes`. Delete the file afterwards: it holds the
client secret.

## What it made

The script ends with a summary of what it made and where. It never prints a secret.

| In the install's folder | What it is |
|---|---|
| `.env` | Every setting and every generated secret, readable by its owner only. **Keep a copy where your organization keeps its secrets.** Without `MENDARI_PHI_KEY`, what was sealed cannot be read again. |
| `tls/` | Your certificate and key, as you put them there (or the self-signed one, with its note). |
| `backups/` | A database backup every night, kept 14 days. **Copy them off the machine**: a backup on the same disk does not survive the disk. |

The six secrets it generated, each from the system's random source: the database's password
(`POSTGRES_PASSWORD`), the key that seals kept inputs (`MENDARI_PHI_KEY`), the key of each
input's fingerprint (`MENDARI_INPUT_HASH_KEY`), the key that signs the daily checkpoint
(`MENDARI_CHECKPOINT_KEY`), the scheduled jobs' secret (`CRON_SECRET`) and the secret that signs
alerts (`MENDARI_ALERT_SECRET`).

The defaults it set:

- **https only**, with your certificate; port 80 only redirects; a name that is not yours is
  not served.
- **Sign-in required**, with people and roles managed in the console (`MENDARI_ACCOUNTS`), and
  your organization's administrators as the deployment's operators (`MENDARI_OPERATOR_ORG`).
- **Nothing leaves your network until someone allows it**: the lists of services Mendari may
  call (`MENDARI_PROVIDER_ALLOWED_ORIGINS`) and of FHIR servers it may read
  (`MENDARI_FHIR_ALLOWED_SERVERS`) start empty. When you connect an adopted agent's service
  in the console, you are asked to allow that one address for that one agent: it must be on
  the public internet, and your allowing it is kept in the audit trail. A service on your own
  network is listed in `.env` instead ([Adopting an agent](adopting-an-agent.md#5-connect-its-service)).
- **Adopting an agent needs no terminal**: your organization's agent store is on
  (`MENDARI_AGENT_STORE`), and the registry you named is the one the console searches
  (`MENDARI_REGISTRY_URL`).
- **Limits on**, per caller in Mendari and per address on sign-in at the front
  (`MENDARI_RATE_LIMITS`).
- **A signed checkpoint of the audit trail every day**, and an alert if the trail ever fails
  to verify.
- **Backups every night**, each read back before it counts as one.
- **Restart policies, health checks and memory limits** on every service.
- **Agents from a registry are refused unless the registry counter-signed them**
  (`MENDARI_REGISTRY_COUNTERSIGNATURE`).

## First sign-in

1. At your identity provider, check that the client's redirect address is exactly
   `https://<your name>/auth/callback`. The summary prints it.
2. Open the address. You are sent to your identity provider, and back.
3. The person whose verified e-mail address is the one you gave becomes the administrator of
   your organization. Anyone else who signs in first belongs to no organization and can do
   nothing until an administrator invites them.
4. Read and acknowledge the notice (research preview, synthetic data only).
5. The console opens on **Finish setting up**: what is left, in order. Follow it.

**The first-administrator setting works once.** When that person signs in, the server makes
them an administrator and writes a mark into the database in the same step, recorded in the
audit trail. From then on the setting (`MENDARI_BOOTSTRAP_ADMIN`) gives nothing: not to the same
address again, not to another address written in its place, not after a restart or a restore
of a later backup. It also gives nothing if your organization already has an administrator.
You may leave it in `.env` or remove it. More administrators are invited from the console's
Access view; invite a second one early, so that one person's absence does not lock you out.

If the address was wrong and nobody can sign in as the administrator, run `./mendari-site
install` again with the right address before anyone has become one.

## The System view

The console's section list follows the order you will work in: **Overview** (with "Finish
setting up" while the deployment is new), then **Adopt**, **Evaluate**, **Review and monitor**
and **Administer**.

**System**, under Administer, shows what this deployment needs to run safely, as a list of
checks. Only your organization's administrators see it. Each check says, in one sentence, what
is so, and what to do about it:

| State | Meaning |
|---|---|
| In order | Nothing to do. |
| Needs attention | Something to look into; the check says what. |
| Not set up | Optional, or still to do: alerts, a registry, an EHR, an agent of your own. |
| Does not apply | Not relevant to how this deployment is set up. |

The checks: sign-in, who runs the deployment, the public address, each person's own address as
the server sees it, patient data mode, the sealing key, the database, the audit trail's last
verification, the daily checkpoint and its signature, the daily jobs, backups, alerts, limits,
the EHR connection, the agent registry, the agents served, and the engine's version.

From the same view, without a terminal:

- **Verify now**: verify the whole audit trail.
- **Send a test alert**, once an alert destination is set.
- **Check the connection** of each EHR registered to launch Mendari.
- **Hold all automation**: every decision goes to a person at once, until the hold is lifted.
  Both ask first, take a reason, and are kept in the audit trail.
- **Erase a decision's kept input**, on a patient's request or for a case recorded by mistake.
- A banner when a version you serve was recalled by its registry.

A fresh install leaves a few steps for you, and "Finish setting up" on the Overview lists
them in order: invite a second administrator (**Access**), set an alert destination
(`MENDARI_ALERT_WEBHOOK`, then send a test alert), name the agent registry if you left it for
later (run `./mendari-site install` again and give its address), and, last, adopt an agent in
the **Agents** view. Until then the bundled demo agent is the only one: it answers from canned
examples and decides nothing real.

**Invite the second administrator before you adopt.** Whoever adds an agent can neither start
serving it nor approve it: someone else does. With one person in your organization, an added
agent stays "not in use yet" ([Adopting an agent](adopting-an-agent.md#one-person-alone)).

## Day to day

```bash
./mendari-site status     # what is running, whether it is ready, the last backup
./mendari-site logs jobs  # what the daily jobs wrote (also: engine, proxy, postgres)
./mendari-site stop       # stop everything; the data stays
./mendari-site start      # start again, or restart after editing .env
```

The daily jobs run a few minutes after the first start, then once a day. A job that fails is
never passed over: it is written in the jobs' log, the `jobs` service shows as unhealthy in
`./mendari-site status` until a run passes, and the System view shows it. If the audit trail
does not verify, the engine also tells your alert destination.

**To change a setting**, edit `.env` and run `./mendari-site start`. The script first checks
the settings as the server will read them, and changes nothing if the server would refuse them.

**To renew the certificate**, replace `tls/fullchain.pem` and `tls/privkey.pem`, then run
`./mendari-site stop` and `./mendari-site start`: the front reads them when it starts. The
install's summary says when the current one expires.

## Backups and restore

A backup is made every night into `backups/` and read back before it is kept. One more, now:

```bash
./mendari-site backup
```

Copy `backups/` and a copy of `.env` somewhere off the machine. Both are needed to recover:
the backup holds the data, `.env` holds the keys that open it.

To put a backup back:

```bash
./mendari-site restore backups/mendari-20261124T020000Z.dump
```

It asks first. Then it restores the backup into a new database, **verifies the restored audit
trail before using it**, puts it in place while keeping the database as it was beside it, holds
all automation, signs everyone out, and starts. Nothing is deleted. It ends by saying what to do
next: what was done after that backup (people or keys removed, approvals revoked, inputs
erased) must be done again, as the [incident runbook](../operations/incident-runbook.md)
describes. Lift the hold with `./mendari-site lift <id>` once you know why you restored, and
drop the kept database with `./mendari-site forget <name>` when the incident is closed.

To move to a new machine: install there with your copy of `.env` in place (the script keeps
every secret in it), copy a backup into `backups/`, and restore it.

## Upgrades

```bash
git pull                  # the new version of this folder and of Mendari
./mendari-site upgrade    # or: ./mendari-site upgrade --image <image>@sha256:<digest>
```

One command, six steps, each stopping the upgrade if it fails:

1. The audit trail is verified with the new version and checked for forks, **read-only**,
   before anything changes (the checks the [incident runbook](../operations/incident-runbook.md)
   asks for before a deployment).
2. The settings are read by the new version.
3. A backup is made.
4. The new version brings the database up to date, while the version running still serves.
5. The new version is recorded in `.env`; the one before is kept.
6. The services restart on the new version, and the script waits until they are healthy.

To go back to the version before:

```bash
./mendari-site rollback
```

If the older version cannot read what the newer one wrote, restore the backup the upgrade made
(step 3): it is the newest file in `backups/`.

## In an emergency

```bash
./mendari-site hold "investigating a missed emergency"   # every decision goes to a person
./mendari-site holds                                     # the holds in force
./mendari-site lift <id>                                 # lift one
./mendari-site verify                                    # verify the whole audit trail
```

`hold` is written straight to the database, so it works with the engine down (the database
server is started if it is not running). If `verify` fails, hold all automation, keep the
database as it is, and follow the [incident runbook](../operations/incident-runbook.md).

## The settings in `.env`

The installer writes these. Change one by editing `.env` and running `./mendari-site start`;
[Deploying Mendari with real patient data](../deploying-with-phi.md) explains each of Mendari's
own settings in full, and many more.

| Setting | What it is |
|---|---|
| `MENDARI_IMAGE`, `MENDARI_IMAGE_PREVIOUS` | The engine's image in use, and the one before it (`upgrade` and `rollback` change them). |
| `MENDARI_PUBLIC_URL`, `SITE_HOST` | The address people open, and its name. |
| `SITE_HTTPS_PORT`, `SITE_HTTP_PORT` | The ports the machine listens on (443 and 80). |
| `SITE_UID`, `SITE_GID` | Who owns the files in `backups/`: whoever installed. |
| `MENDARI_OIDC_ISSUER`, `MENDARI_OIDC_CLIENT_ID`, `MENDARI_OIDC_CLIENT_SECRET` | Sign-in with your identity provider. |
| `MENDARI_ACCOUNTS` | `memberships`: people and their roles are managed in the console. |
| `MENDARI_OPERATOR_ORG` | The organization whose administrators run this deployment: they see the System view and may verify the whole audit trail. Yours (`default`). |
| `MENDARI_BOOTSTRAP_ADMIN`, `MENDARI_BOOTSTRAP_ORG_NAME` | The first administrator's sign-in address, used once, and what your organization is called. |
| `MENDARI_PHI_MODE`, `MENDARI_PHI_KEY`, `MENDARI_INPUT_HASH_KEY`, `MENDARI_RETENTION_DAYS` | Patient data mode (`encrypt` or `hash`), its two keys, and after how many days kept inputs are deleted. |
| `POSTGRES_PASSWORD` | The database's password. |
| `MENDARI_CHECKPOINT_KEY` | Signs the audit trail's daily checkpoint. |
| `CRON_SECRET` | What the daily jobs show the engine. It is of no use from outside: the front refuses their routes. |
| `BACKUP_KEEP_DAYS` | How many days a backup is kept in `backups/`. |
| `MENDARI_ALERT_WEBHOOK`, `MENDARI_ALERT_SECRET` | Where alerts go (empty until you set it; treat it as a secret), and the secret that signs them. |
| `MENDARI_TIMEZONE` | Calendar days and displayed times. |
| `MENDARI_RATE_LIMITS` | Limits on costly calls and on sign-in (`on`). |
| `MENDARI_ENVIRONMENT` | What this deployment is called in its own records (`production`). |
| `MENDARI_PROVIDER_ALLOWED_ORIGINS`, `MENDARI_FHIR_ALLOWED_SERVERS` | The only services Mendari may call and the only FHIR servers it may read, as comma-separated origins. A lone comma means none. An administrator can allow one agent's service at a public address from the console; a service on your own network, and every FHIR server, is added here. |
| `MENDARI_REGISTRY_URL`, `MENDARI_REGISTRY_COUNTERSIGNATURE` | The agent registry you adopt agents from (empty until you give it: `./mendari-site install` asks), and that it must have counter-signed what it lists. |
| `MENDARI_AGENT_STORE` | `on`: your organization keeps the versions of agents it adds from the registry, in the database. |
| `MENDARI_CACHE_DIR`, `MENDARI_CONFIG_DIR` | Where the engine keeps what it pulls from a registry: a Docker volume, kept across upgrades. |

A few more may be added at the end of `.env` if the machine needs them. They are read by the
template, not by Mendari:

| Setting | What it is |
|---|---|
| `SITE_ENGINE_MEMORY`, `SITE_POSTGRES_MEMORY`, `SITE_PROXY_MEMORY`, `SITE_JOBS_MEMORY` | Each service's memory limit (1g, 1g, 128m and 256m). |
| `SITE_SUBNET`, `SITE_PROXY_ADDRESS` | The private network the services share (`172.31.87.0/24`) and the front's address in it, the only one the engine trusts to say where a request came from. Change both if that range is taken on your machine. |

## Removing it

```bash
./mendari-site stop
docker compose down --volumes   # in this folder: this deletes the database
```

Keep `backups/` and `.env` if the audit trail must be kept.

## The other ways to run, and the rest

This page covers one machine. The same engine also runs:

- on **Kubernetes**: [`deploy/kubernetes/`](https://github.com/mendari-hq/mendari/tree/main/deploy/kubernetes);
- on **one server where you bring the proxy, the certificate and the backups**:
  [`deploy/onprem/`](https://github.com/mendari-hq/mendari/tree/main/deploy/onprem);
- on **Vercel**, as the project's own hosted site does:
  [`deploy/vercel/`](https://github.com/mendari-hq/mendari/tree/main/deploy/vercel);
- on **a developer's machine**, with synthetic data: `docker compose up --build` at the
  repository's root.

[Deploying Mendari with real patient data](../deploying-with-phi.md) is the long reference: every
setting, sign-in and roles in detail, limits and quotas, what leaves your network, logs, and
the checks to run before go-live. [Connecting an EHR](connecting-an-ehr.md) covers the launch
from your EHR, and the [incident runbook](../operations/incident-runbook.md) what to do when
something goes wrong.

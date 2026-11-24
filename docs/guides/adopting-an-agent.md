# Adopting an agent from the console

A hospital can take an agent from a registry to its own decisions without a terminal: find it,
add a verified version, evaluate it on this server, start serving it, connect the service that
runs it, and approve it to act on its own. Each step is a different permission, and two of
them must be taken by someone who did not take the first. This page walks through them in the
order the console shows them, under **Adopt, Agents**.

Nothing here shortens what the engine requires. Adding an agent is not approving it: an added
agent answers no request, a served one decides nothing alone until your organization approves
that exact version, and connecting its service changes where its answers come from and nothing
else.

## Before you start

Whoever runs the deployment sets these once. The console says which are missing, in words, on
the page that needs them, and the **System** view's "Agent registry" check says what to do.
[The one-machine install](install-at-your-site.md) sets all of them: it asks for the
registry's address and turns the rest on.

| Setting | What it gives the console |
|---|---|
| `MENDARI_REGISTRY_URL` | The agent registry that **Agents, Find agents** searches and adds from: the Agent Hub's address, or your own registry's. It must be https. Unset, the Agents page lists what you use and has nothing to search. |
| `MENDARI_AGENT_STORE=on`, with `MENDARI_ACCOUNTS=memberships` | Each organization keeps the versions it adds ([The agent store](../operations/agent-store.md)). Without it the registry can be read here and nothing can be added from the console. |
| `MENDARI_PHI_MODE=encrypt` and `MENDARI_PHI_KEY` | A connection's token is kept sealed under this key. Without one, a connection can hold an address and no token. |
| `MENDARI_PROVIDER_ALLOWED_ORIGINS` | Optional. Set, a connection's address must be one of these origins, and may then be on your own network; an address it does not name can be allowed for one agent, to the public internet only, by an administrator of the organization that operates the deployment ([step 5](#5-connect-its-service)). Unset, a connection leads to the public internet only. |
| `MENDARI_OPERATOR_ORG` | The organization whose administrators operate the deployment: they see the System view, and may allow an address as above. The one-machine install names your own organization. |
| `MENDARI_AGENT_CONNECTIONS` | `off` turns connections set in the console off: every agent then answers through the deployment's own `MENDARI_AGENT_URL`, or from its canned examples. On unless set. |
| `MENDARI_REGISTRY_COUNTERSIGNATURE` | As for `mendari pull`: whether a version must carry the registry's own counter-signature to be added. |

## Who does what

| Step | Who |
|---|---|
| Find an agent and read its listing | Anyone who can read the console |
| Add a version, evaluate it | An **Agent author** (the role `pack_author`) who can also read the console. An administrator gives the role under **Access, Members**, to themself too |
| Start serving it | **Governance** or an **Administrator** who did not add it |
| Connect its service | An **Administrator**. Where the deployment lists the addresses it may call and the service's is not one of them: an administrator of the organization that operates the deployment |
| Approve it to act on its own | **Governance** or an **Administrator** who did not add it |

No API key, client or EHR launch can take any of the steps that change something: they are
people's, signed in.

## One person alone

**An organization of one person cannot start serving, or approve, an agent it added.** This is
the two-person rule, and it is how the engine is built, not a fault in your installation:

- whoever adds a version is its author, and an author never promotes their own version: **Start
  serving…** is not offered to them, and the server refuses it (`403`) if asked;
- the same person cannot approve that version to act on its own, for the same reason;
- giving yourself more roles changes nothing: the rule is about who added the version (or an
  earlier one in its history), not about which roles they hold.

So with a single administrator, an agent added from the registry stays under **Added, not in
use yet**. It can be read, evaluated and connected there, and it answers no request. To go
further, invite a second person (**Access, Members**) with the Governance or Administrator
role, and have them start serving it and approve it. A fresh install's "Finish setting up"
asks for a second administrator before it asks you to adopt an agent, for this reason.

Whether a one-person organization should be able to do both steps, for example a solo
practice, or a trial on synthetic data, is an **open decision for the project**: it would
change who the engine trusts with what ([ADR-0016](../decisions/ADR-0016-an-agent-store-per-organization.md)),
and it has not been taken. Until it is, the rule holds everywhere, and this page will say so
when it changes.

## 1. Find the agent

**Agents, Find agents** searches the agent registry and lists each agent's newest version. An
agent's page keeps two things apart:

- **Checked by the registry**: the publisher's signature (verified when the version was
  published), the registry's counter-signature if it signs, the publisher type its reviewers
  set, its review of the risk level, and the version's digest.
- **Claimed by the publisher**: who the publisher says it is, the risk level it declares, the
  licence and the standards. The registry has not checked these.

Below them: whether the publisher published evaluations of this version, the versions the
registry recalled and why, and the agent's Decision Card as the registry rendered it. Everything
a registry or a publisher wrote is shown as text.

## 2. Add a version

**Add version…** on the agent's page. The server downloads the version and checks it before
keeping anything, exactly as `mendari pull` does:

1. the archive's SHA-256 is the one the registry recorded;
2. the archive is signed by a key of the publisher's namespace, and every file matches the
   signed manifest;
3. where the registry signs, its counter-signature names this version, this digest and that
   key;
4. it is the agent and the version that were asked for.

Then it passes every check a version of the agent store passes: no file that reads as a
person's details, it loads on the engine it will run on, the hidden-set check, the size limits
and your organization's daily quota.

**Nothing is added** when the registry does not answer, the version is recalled or deleted,
the archive or the signature does not verify, the publisher's key was revoked, your
organization already holds that version, or a check or a quota refuses it. The page says which.

**A changed signing key.** The first version your organization adds from a publisher pins that
publisher's keys for your organization. A later version signed with a key you have not seen is
refused, and the console shows the new key beside the ones seen before. Check the new key with
the publisher through a channel you trust, then accept it. What your organization accepts
changes nothing for another organization on the same server.

The version is now under **Agents, Our agents, Added, not in use yet**. It is kept in the
engine's database for your organization, so it is there after a restart and on every instance.

## 3. Evaluate it here

**Evaluate…** on the added version runs it on a labelled dataset the agent carries, on this
server, and keeps the report for your organization. Nothing is sent to the registry. To
evaluate on your own cases instead, upload a dataset in the **Agent store** under Results and
run the job there. Whoever starts serving the version names the reports they relied on.

## 4. Start serving it

**Start serving…** is offered to Governance and Administrators, and never to the person who
added the version. They name the evaluations they relied on and where the decision is recorded
(a committee's minutes, a ticket). From then on the version answers every request for that
agent made in your organization, and in no other.

It still does not act on its own: until the approval in step 6, a person decides every case.
Choosing **Recommend only** keeps it that way whatever is approved later.

## 5. Connect its service

An agent that asks an HTTP service for its answers has a **Connection**: on its Decision Card,
and behind **Connection…** on the Agents page. Until one is set the agent answers from the
canned examples it carries, and a server where people sign in never automates on those.

An administrator sets:

- **the address of the service**, starting with `https://`, with no user, password, query or
  fragment. Where the deployment has no list of the addresses it may call, the address must
  lead to the public internet. Where it has one, the address must be on it, or be allowed for
  this agent as described below. An address on your own network is refused either way until
  whoever runs the deployment lists it;
- **a token**, optional, sent as `Authorization: Bearer …` with each call. It is typed once,
  kept sealed, and never shown again by any page, route or log. Changing the address removes
  the token unless a new one is typed with it.

**An address the deployment's list does not name.** A deployment may list the addresses its
providers may be called at (`MENDARI_PROVIDER_ALLOWED_ORIGINS`); the one-machine install starts
with a list that names none, so that nothing leaves your network until someone decides it may.
When the address you type is not on the list:

- if you are an administrator of the organization that operates the deployment (on a
  one-machine install, you are), the form shows the address's origin, such as
  `https://agents.example.org`, and a box: **Allow this address for this agent**. Tick it and
  save. The origin is then allowed for this agent in your organization, and for nothing else;
- the address must still be on the public internet. This machine, a private address, a name
  that leads to one, and your own network are refused as before: only the list in the
  deployment's settings reaches those, and that is a file on the machine;
- who allowed it and when is shown beside the connection, and recorded in the audit chain
  (`agent_connection.origin_allowed`);
- it holds for that origin alone. A connection changed to another origin needs allowing
  again, and clearing the connection withdraws it;
- anyone else is told who can: an administrator of the organization that operates the
  deployment, or whoever runs it, by adding the origin to the list.

**Check connection** sends one synthetic example the agent carries, never a patient's case,
and says what came back: that it answered, how long it took, and the model and version the
service reports; or why not, in plain words.

Where a live call goes is decided in this order:

1. your organization's own connection for the agent;
2. the address the agent's own manifest names, for an agent the deployment owns;
3. the deployment's `MENDARI_AGENT_URL`, with `MENDARI_AGENT_TOKEN`;
4. none of them: the agent's canned examples.

A connection that is set and cannot be read fails the decision, which goes to a person: it
never falls through to another service. The decision behind this section is
[ADR-0021](../decisions/ADR-0021-an-agents-connection-set-in-the-console.md).

## 6. Approve it to act on its own

On the agent's Decision Card, under **Automation approval**, Governance or an Administrator
who did not add the version approves this exact version (its digest). At risk levels 2 and 3
the agent also needs your organization's governance approval, and at level 4 a person always
decides. A hold stops it at once, and so does a recall by its registry, which the server reads
in the background.

The **Overview** then shows the agent's adoption path from there: trying it on synthetic
cases, a shadow pilot, validation on your own cases, and monitoring.

## What is recorded

Every step is an event in your organization's audit chain, readable under **Access, Activity**:

| Step | Event |
|---|---|
| A version added | `pack_version.created`, with the registry, the namespace, the signing key and the archive's SHA-256 |
| An evaluation | `store_job.created`, `eval.recorded` |
| Serving started, stopped | `pack_version.promoted`, `pack_version.retired` |
| A connection set or changed, cleared | `agent_connection.set` (the agent, the address's origin, whether it carries a token, who), `agent_connection.cleared` |
| An address allowed that the deployment's list does not name | `agent_connection.origin_allowed` (the agent, the origin, who), just before the connection's own event |
| The version approved | `registry_pack.approved` |

A connection's token is in none of them, and neither is the path of its address.

The security log adds, for incident response: `store.pull.added` and `store.pull.refused` (with
a fixed reason: `not_listed`, `key_changed`, `verification`, `unreachable`, `registry_answer`,
`phi` or `checks`), and `admin.connection.set`, `admin.connection.cleared`,
`admin.connection.checked`, `admin.connection.origin_allowed` and `admin.connection.refused`
(with a fixed reason: `checks`, `origin_not_listed`, or `not_operator` for someone who asked to
allow an address and may not), each with the agent's id and who acted.

## The same through the API

| Route | Needs |
|---|---|
| `GET /api/hub` | read |
| `GET /api/hub/search`, `GET /api/hub/agents/{namespace}/{name}` | read |
| `POST /api/store/pulls` | `author` (a person) |
| `GET /api/connections`, `GET /api/connections/{namespace}/{name}` | read |
| `PUT`, `DELETE /api/connections/{namespace}/{name}` | `keys` (an administrator, a person); `PUT` with `allow_origin: true` also needs `operate` (an administrator of the organization that operates the deployment) |
| `POST /api/connections/{namespace}/{name}/check` | `keys` |

Each is described at `/docs/reference` on your deployment. The searches, the listings and the
check each make the server send one request elsewhere, so they are counted per person against
the same rate limit as labelling.

## What still needs the operator

- Naming the registry, turning the agent store on, and the PHI key: settings of the deployment.
  The one-machine install sets them, and `./mendari-site install` asks for the registry again
  if it was left for later.
- Listing an origin on your own network in `MENDARI_PROVIDER_ALLOWED_ORIGINS` before a
  connection can lead there. Nothing in the console reaches inside.
- On a deployment shared by several organizations: allowing an address the list does not name,
  for any organization but the one that operates the deployment.
- Answering a version the hidden-set check held ([The agent store](../operations/agent-store.md)).

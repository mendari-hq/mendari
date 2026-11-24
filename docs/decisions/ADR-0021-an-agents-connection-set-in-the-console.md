# ADR-0021: An agent's connection, set in the console by its organization

## Status
Accepted (2026-11-24, with the change that builds it).
Decided by the maintainer on 2026-11-24, before the repository was public, so no comment period ran; open to revision when it is.

It came from the owner's brief for a web console in place of the command line: "an
administrator of the organization sets, per agent, the endpoint and bearer token of the service
that answers for it". The brief fixed what must hold: https only, the provider allow-list
honoured, internal addresses refused, a write-only token sealed at rest and refused where there
is no key, every change chained, each organization its own, one resolution order, a switch to
turn it off. The choices this record adds to that are marked **(chosen here)**. One was added
when the pieces of the release were put together, and is marked **(added at acceptance)**: an
operator of the deployment may allow, for one agent, an address the deployment's list does not
name (point 5). Who an operator is: [ADR-0022](ADR-0022-the-first-administrator-and-the-operator-organization.md).

## Context
An agent's `agent` variant asks an HTTP service outside Mendari for its answers
(`providers/agent.py`). Where that service is has been a setting of the deployment:
`MENDARI_AGENT_URL`, with `MENDARI_AGENT_TOKEN`, in the server's environment. That is one
service for every agent and every organization on the server, set by whoever runs it, and read
at start-up. Without it an agent answers from the canned examples it carries, and a server with
sign-in never automates on those.

A hospital can now find an agent in the registry from the console, add a verified version of it
to its own agent store, evaluate it and start serving it
([ADR-0016](ADR-0016-an-agent-store-per-organization.md),
[ADR-0012](ADR-0012-a-registry-that-shows-only-what-it-checked.md)). The last step was still a
terminal: until the operator sets an environment variable and restarts, the adopted agent
answers from canned examples. Two organizations on one server cannot use two services, nor one
organization a service for each of its agents.

Letting a person set, through the web, an address the server will send requests to is a new
trust boundary, and each of these is a way it can go wrong:

- **The server reaches what only the server can reach**: this machine, its private network, a
  cloud's metadata address. A name can also answer a public address when it is checked and a
  private one when it is called.
- **The server keeps a credential.** It must be able to use it, so it cannot be hashed; a copy
  of the database must not reveal it; no route, log line or audit event may.
- **Cases go to another party.** What is sent is a decision's input, cut to the agent's declared
  `state_fields`: at a hospital that is patient data. Who pointed it where, and when, must be on
  the record, and one organization must never set, see or use another's.

## Decision
1. **One connection per organization and agent**: an endpoint and, optionally, a bearer token,
   kept in the engine's database (`agent_connections`, `core/connections.py`). It is used for
   every version of that agent the organization runs: the one it is served, and a draft of its
   store when that draft is evaluated or checked.
2. **Its organization's administrators set it, and only people.** Setting, changing, clearing
   and checking need the `keys` permission (**chosen here**): what an organization's
   administrators hold and no API key, Backend Services client, EHR launch or CDS client ever
   does (`server/auth.py` `PEOPLE_ONLY`), so no machine's credential can redirect an agent's
   cases or plant a token. Without sign-in (open mode, synthetic data on one machine) whoever
   reaches the server may, as for everything else there. Any reader sees whether an agent is
   connected, the endpoint's origin, and who set it when; the endpoint's path is shown only to
   those who may set it. Every route acts for the organization the caller is signed in to: none
   takes an organization from a path, a query or a body.
3. **What is stored**: the endpoint as it was given; the token sealed; who set each, and when.
   The token is sealed under the deployment's PHI key (`core/phi.py`, a kind of its own,
   `connection`), with the row's id, the organization and the agent as associated data, so a
   sealed token moved to another row opens nothing. It is opened only to make a call. No route
   returns it, no log line or event holds it, and a read shows `token: "set"` with who set it
   and when. A deployment with no PHI key (`MENDARI_PHI_MODE` other than `encrypt`) refuses a
   token before anything is written and keeps an address alone. `mendari phi rotate` re-seals
   tokens with everything else, and its check counts them.

   The PHI key and not the session seal (**chosen here**, between the two the brief named):
   `core/session_seal.py` derives its key from a secret only the browser holds, which is its
   point, and so the server can open such a row only while a request carries that cookie. A
   connection's token must open for a service key's call and for a scheduled job, with no
   browser anywhere. The PHI key is the one long-lived data key a deployment already
   provisions, protects, rotates and can check; a token is sealed by the same ring and the same
   procedures, and there is no new key to provision or lose.
4. **A token stays with the address it was set for.** Changing the endpoint drops the token
   unless a new one is sent with it (**chosen here**). Otherwise a person who cannot read a
   token could have the server deliver it to an address of theirs.
5. **What it can reach.** The endpoint is https (plain http to this machine only), with no
   user, password, query or fragment, written in plain URL characters with a host that is a
   name or an IP address: nothing two parsers could read two ways (a backslash, an accented
   look-alike of a dot, a percent sign in the host). Then one of two rules, by how the
   deployment is set up:
   - **With `MENDARI_PROVIDER_ALLOWED_ORIGINS` set**, the endpoint's origin is on that list or
     it is refused. The list is the operator's statement of what the server may call, so a
     listed origin may be on the site's own network, and it is called as the deployment's own
     endpoints are.
   - **With the list set and the origin not on it, an operator of the deployment may allow
     that origin for that agent in their organization** (**added at acceptance**). An operator
     is an administrator of the organization that operates the deployment, signed in as a
     person (`operate`, [ADR-0022](ADR-0022-the-first-administrator-and-the-operator-organization.md)):
     never an API key or a client, never open mode, and never an administrator of another
     organization than the one the deployment names (where sign-in is by claims and it names
     none, every administrator operates it, as ADR-0022 says). They ask for it when they set
     the connection (`allow_origin`); it is
     kept on the connection's row with who allowed it and when, and chained
     (`agent_connection.origin_allowed`: the agent, the origin, who) before the connection's
     own event. An origin allowed this way is held to the rule of a deployment with no list,
     below: **the public internet only**, checked when it is set and again by the client at
     every connection. So an operator's word in the console reaches a public service and
     nothing else; this machine and the site's own network are reached only through the
     list, which is a file on the machine. What was allowed holds for that origin alone, for
     whoever changes the connection afterwards: a connection moved to another origin, or
     cleared, has lost it, and once the list names the origin the list speaks for it. Anyone
     who is not an operator stays bound by the list: `allow_origin` from them is refused
     whole (`403`), and without it the list's refusal stands.

     Why: the one-machine install starts with a list that names no origin, so that nothing
     leaves the hospital's network until someone decides it may. Without this, connecting the
     first adopted agent meant editing a file on the server and restarting it, which is the
     terminal this record set out to remove. The people this trusts are the ones who already
     hold the file.
   - **Without a list, the public internet only** (**chosen here**, `providers/reach.py`). The
     host is not this machine, a single-label name, or a number that is not a plain IP address
     (`127.1`, `0x7f.0.0.1`, which resolvers read as addresses and not all the same way), and
     every address it resolves to is public: not loopback, private, link-local, shared,
     multicast or reserved, nor one of those wrapped in IPv6. That is checked when the
     connection is set, and again by the client
     itself at every connection it opens: it looks the name up, refuses unless every address is
     public, and connects to the address it checked, never to the name again, through no proxy.
     So a name that changes its answer between the check and the call connects to nothing.

   Either way no redirect is followed, an answer is read as the agent contract or not at all
   and is capped in size, and a failure is a fixed code: never a word of what the other side
   answered.
6. **One resolution order**, written once in the code (`AgentProvider.live`) and repeated here:
   the organization's own connection for the agent; else the variant's own `endpoint`, which
   only an agent the deployment owns may name
   ([ADR-0009](ADR-0009-bounded-evaluation-for-agents-the-deployment-does-not-own.md)); else the
   deployment's `MENDARI_AGENT_URL`; else none, and the agent answers from its canned examples,
   which never automate where people sign in. A connection that is set and cannot be read (its
   token does not open) fails the call: it never falls through to another service. A token
   goes only to the endpoint it was set with: an organization's to its connection,
   `MENDARI_AGENT_TOKEN` to `MENDARI_AGENT_URL`, none to a manifest's.
7. **Every change is chained**, in the organization's administrative events:
   `agent_connection.set` (the agent, the endpoint's origin, whether it carries a token, who),
   `agent_connection.cleared`, and `agent_connection.origin_allowed` when an operator allows
   an origin the list does not name (point 5). Never the token, nor the endpoint's path. A
   cleared connection's row is kept, emptied, because its events name it.
8. **A check that sends no patient data.** "Check connection" sends the agent's first behaviour
   test, a synthetic example published with the agent, to the service a decision would call
   now, and says what came back in fixed words with the time it took and the model and version
   the service reports. Nothing is recorded. It is counted against the same rate limit as a
   labeller's requests, per person, since each one is a request the server sends.
9. **A switch.** `MENDARI_AGENT_CONNECTIONS=off` (**chosen here**: on unless set) turns the
   feature off for a deployment that wants only its own endpoint: nothing is set or changed,
   what was set before is neither shown nor called, and the reads still say where each agent's
   answers come from.
10. **A connection is not an approval.** It changes where answers come from and nothing else.
    The agent's policy, the risk caps ([ADR-0002](ADR-0002-runtime-enforced-risk-caps.md)), the
    governance approval, the approval of a registry agent's digest, holds, recalls and the
    store's promotion all apply as before, and none of them can be met by connecting a service.

## Consequences
- A hospital connects the agent it adopted without a terminal or a restart. A change takes
  effect at once on the instance that saved it and within five seconds on every other.
- Two organizations on one server, and two agents of one organization, can each have a service
  of their own; `MENDARI_AGENT_URL` remains the deployment's default for all of them.
- The server holds a credential it can use. A copy of the database without the PHI key reveals
  no token; with the key it reveals them, as it does the inputs it holds. Losing the PHI key
  loses the tokens: they are set again in the console.
- An administrator can send their organization's cases to any public https service, or to any
  origin the operator listed. That is their organization's decision; it is on the chain with
  the origin, who and when, and an operator who wants a tighter boundary sets the allow-list
  or the switch.
- A service on the hospital's own network cannot be connected from the console alone, with a
  list or without one: whoever runs the deployment lists its origin first. That is one line of
  configuration, once, and it is the operator saying what the server may reach inside.
- On the one-machine install, whose list names no origin, an administrator connects an agent's
  public service without touching the machine: they are the deployment's operators, and the
  console asks them to allow the address, by name, for that agent. The audit chain then holds
  who widened what the server may call, for which agent, and when. The list in the settings
  file is unchanged by it and still says what is allowed for every agent.
- On a deployment shared by several organizations, only the operating organization's own
  connections can be allowed this way: every route acts for the organization the person is
  signed in to. Another organization's administrator asks whoever runs the deployment to list
  the origin, as before.
- Evaluations run through the connection too, so an evaluation of a connected agent calls its
  service and counts against the organization's quota for remote calls.
- The reason a canned answer gives on a decision still names `MENDARI_AGENT_URL`. It is a
  stable string that tests and pages quote, so it is left as it is here and is to be reworded
  with them.

## Alternatives considered
- **Keep the environment setting only**: one service for the whole server, set by the operator
  at a restart. It is why the last step of adopting an agent needed a terminal.
- **Seal the token under the session's secret** (`core/session_seal.py`): nothing to provision,
  and the server could not open it for any call that no browser makes.
- **A key of its own for connections**: one more secret to provision, rotate, check and lose,
  protecting something the PHI key ring already protects in the same database.
- **Require the allow-list for every connection**, as the study behind this change proposed. On
  a server shared by many organizations, each new service would then need the operator again.
  Reaching the public internet only, enforced by the client that opens the connection, keeps
  what the list was for, the server's own network, out of reach without it.
- **A permission a machine's credential can hold**: a leaked key or client could then send an
  organization's cases elsewhere. `keys` is already the permission for what only signed-in
  administrators do with credentials.
- **Let an operator add an origin to the deployment's list from the console.** The list would
  then stop being what the settings file says, for every agent and every organization, and an
  origin added there may be on the site's own network. Allowing one origin for one agent, to
  public addresses only, changes nothing the file is the word for.
- **Let every administrator allow an origin for their own organization.** On a shared server
  that makes the operator's list advisory: any organization's administrator could send the
  server to any public address. Only whoever runs the server widens what it may call.
- **Install with no list at all**, so that any public address can be connected. Then nothing
  records that someone decided a given service may be called, and a model or agent endpoint
  set by mistake in the deployment's own settings is called without a word.
- **Name the endpoint in the agent's manifest**: a manifest is content, the same for every
  organization that runs it, and an agent the deployment does not own may not name one
  (ADR-0009).

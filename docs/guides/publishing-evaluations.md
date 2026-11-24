# Publishing an agent's evaluation

A publisher can show how each configuration of an agent did on one of its own datasets: one
command publishes the results, signed, and the Agent Hub shows them on a public page that a
README can link, beside a badge that says the same in one line.

```text
https://<hub>/agents/<namespace>/<name>/evaluation        the page
https://<registry>/v1/agents/<namespace>/<name>/badge.svg  the badge
```

This page is for the publisher, and for whoever reads the evaluation page or the badge and wants
to know what stands behind them. The decision is
[ADR-0020](../decisions/ADR-0020-publisher-evaluations.md); the registry's side is in
[Running a registry](../operations/registry.md#publisher-evaluations).

## Before you start

- A namespace on the registry, a publisher token for it, and a publisher key registered there
  ([Authoring an agent](authoring-an-agent.md#15-share-it)).
- A dataset the agent's manifest declares under `evaluation.datasets`, with at least 100 cases.
  It travels inside the agent's signed archive, so the registry and everyone who pulls the
  agent hold the same cases. A file outside the agent is refused.
- A provider that calls a model needs what it needs to run (its address, its key): the
  evaluation runs on your machine.

## The command

<!-- check: skip registry: needs a registry and a publisher token -->
```bash
mendari login --registry https://registry.example --token ohr_...
mendari push my-agent --key keys/publisher.key --evaluate test \
  --labelled-by "Two nurses, independently; a third settled disagreements"
```

`push` signs the agent and publishes the version, as it always did. With `--evaluate DATASET`
it then runs every configuration of the agent on that dataset (one variant for each provider
slot), signs each result with the same key, and publishes it:

```text
published my-clinic/my-agent@0.3.0 sha256:5b1c…
  reader=agent: signed by the publisher; not re-run: calls a model
  reader=rules: re-run by the registry: matched
published 2 of 2 evaluations of my-clinic/my-agent@0.3.0 on test (600 cases), counts only, signed
page:  https://hub.example/agents/my-clinic/my-agent/evaluation
badge: [![Mendari evaluation](https://registry.example/v1/agents/my-clinic/my-agent/badge.svg)](https://hub.example/agents/my-clinic/my-agent/evaluation)
       it reads: Mendari | evaluated, 2 configurations, 600 cases, 0 critical automated
```

- **Run it again whenever you like.** A version the registry already holds, the same build, is
  not pushed twice (versions are immutable; a different build under the same version is
  refused, and needs a new version). An evaluation published again replaces the live one of the
  same configuration and dataset; the registry keeps both.
- **Evaluations belong to a version.** A new version starts with none, and its page and badge
  say so until you publish them. Publishing the version and its evaluations in one command is
  how the page never goes empty.
- **A first version in a new namespace** is published, but takes no evaluation until a registry
  reviewer lists the namespace. The command says so; run it again then.

| Option | What it does |
|---|---|
| `--evaluate DATASET` | The dataset to evaluate on, by its name in the manifest |
| `--configuration SLOT=VARIANT[,SLOT=VARIANT]` | Evaluate this configuration only (repeat for several; a slot left out keeps its default). Without it, every configuration, when the agent has at most 8 |
| `--labelled-by TEXT` | Who labelled the dataset, in one line of at most 200 characters. It is signed and shown as you wrote it |
| `--workers N` | Decisions in parallel (8 for a provider that calls a model, else 1) |
| `--registry URL` | Another registry than the one logged into |

One configuration at a time works too, and prints a full report of the run:

<!-- check: skip registry: needs a registry and a publisher token -->
```bash
mendari eval my-agent --dataset test --provider reader=rules --publish --key keys/publisher.key
```

## What is published, and what is not

Each configuration's result is one signed record, `mendari.publisher-evaluation/2`:

```json
{"format": "mendari.publisher-evaluation/2",
 "pack": {"id": "my-clinic/my-agent", "version": "0.3.0", "digest": "sha256:…"},
 "configuration": {"reader": "rules"},
 "dataset": {"name": "test", "sha256": "<64 hex>", "n": 600,
             "provenance": "synthetic", "labelled_by": "The script that wrote the cases"},
 "report": {"format": "mendari.site-report/2", "…": "…"},
 "claimed": {"latency_ms_p50": 0.05, "latency_ms_p95": 0.07,
             "cost_usd_per_decision": 0.0, "failed_cases": 0},
 "models": ["rules@33463eddba9c"],
 "engine": "0.1.0",
 "created_at": "2026-11-23T12:00:00Z",
 "key_id": "ed25519:<16 hex>",
 "signature": "<base64>"}
```

| Published | Where it comes from |
|---|---|
| The agent's id, version and digest | The build that was evaluated; the registry refuses any other than the published version's |
| The configuration, and each slot's provider type and model | The run. The models' names are your word |
| The dataset's name, SHA-256 and size | The file in the signed archive; the registry checks all three against its copy |
| What the cases were built from (`provenance`) | The manifest's `card.provenance`: `synthetic`, `de_identified`, `site_records`, or null when it does not say. The registry refuses any other value than the manifest's |
| Who labelled them (`labelled_by`) | Your own words, or null |
| The counts: per question how many cases were labelled and right, the confusion matrix, the confidence deciles; how many cases each outcome had; how many were automated with a wrong answer; each critical label's cases, detected and automated; tokens | Counted from the run's ledger, case by case. No rate is sent: the registry works out every rate and every 95% interval itself |
| Latency (median and 95th percentile), cost per decision, and how many cases a provider failed on | Measured by your run. The registry cannot check them, and the page labels them as yours |
| The Mendari release that ran it (`engine`) | Your machine |

Never published: a case's text, a case's id, the ledger, calibration error and Brier scores,
subgroups, and anything about who approved a governance override (an evaluation measures the
agent's own policy, and one run under an override is refused).

`mendari.publisher-evaluation/1`, the format before this one, had no `provenance`,
`labelled_by`, `failed_cases` or `engine`. A registry still takes it from an older client, and a
record stored in it still verifies and is still shown: what it does not state reads "not
stated".

## What the registry checks

Before it stores a record: that one of your namespace's keys signed it and the key is not
revoked; that it is of a listed version and of that version's digest; that the configuration
and every name in the counts are the agent's; that the dataset is the one in the archive; that
the counts are whole and add up. Then:

- A configuration whose providers all decide on the machine (`rules`, `criteria`, `dosing`,
  `naive_bayes`) is **run again by the registry**, and refused unless it counts exactly what
  you counted.
- One that calls a model (`agent`, `llm`, `jev`) is not run again: the registry calls no model.
  Its counts rest on your signature, and the page says so on its row.

Every time the page or the badge is asked for, each stored record is checked again: it must
still be the record that was signed, by a key that is still your namespace's. One that is not
is listed as **not verified**, with none of its numbers, and takes no part in the comparison or
the badge.

## How to read the page

The page is written for someone deciding whether to try the agent, not for a statistician.

1. **What was evaluated, and on what data.** The agent's version and digest, who ran it, the
   dataset, where its cases come from (it says plainly when they are synthetic), who labelled
   them, and which answers count as critical.
2. **How the configurations compare.** Every configuration ran on the same cases, so they can
   be set side by side. Each figure is a count with what it is of ("81 of 114") and, in
   brackets, the range the true rate plausibly lies in (a 95% interval), wide when the cases
   are few. A sentence says which configuration did best and what decided it; one line for
   each configuration says how many critical cases it did not detect and how many it decided
   automatically, with no person reading them. Below: what happened to the cases (decided
   automatically, sent to a person, no suggestion made, blocked), and the publisher's own
   speed, cost and failures.
3. **Signatures.** Each result with the key that signed it, and whether it verifies today.
4. **What these numbers do not show.**
5. **Read next:** the agent's Decision Card, its page, its publisher.

**How "did best" is decided.** The order is fixed, the same for every agent, and puts safety
first: fewest critical cases automated, then most critical cases detected (the worst critical
label's, by the low end of its interval), then fewest wrong answers automated, then most correct
answers (`mendari.rank/1`). Cost never decides it: it is the publisher's own figure. The page
names the first configuration as having done best only when its interval and the second's do not
overlap on the measure that decided it; otherwise it says the first is not clearly ahead. It
names none when no critical label has 100 positive cases, when the agent names no critical
label, or when the first two did the same on every measure. And when even the first let critical
cases through, the headline says that too.

**What the numbers do not show.** How the agent will do on your patients: the cases are the
publisher's, a configuration can be tuned to them, and a model's counts are the publisher's
word. Evaluate the agent on your own cases, in shadow, before it decides anything
([Evaluate an agent at your site](evaluate-at-your-site.md)). The comparison is between
configurations of one agent; it ranks no agent against another, and it is not a grade.

## The badge

The Markdown the command printed puts the badge in a README, linked to the page:

```text
[![Mendari evaluation](https://registry.example/v1/agents/my-clinic/my-agent/badge.svg)](https://hub.example/agents/my-clinic/my-agent/evaluation)
```

It is an image the registry draws each time from the same signed records as the page, for the
agent's newest version that is not recalled:

| It reads | When |
|---|---|
| `evaluated, 600 cases, 0 critical automated` | One configuration, on a dataset of 600 cases; none of its critical cases was decided automatically |
| `evaluated, 2 configurations, 600 synthetic cases, 0 to 9 critical automated` | Several configurations; the manifest says the data is synthetic; the one that automated the fewest critical cases automated none, the one that automated the most, 9. Always both: a weak configuration is neither hidden behind a good one nor shown alone |
| `evaluated, 3 configurations, 2 datasets, …` | More than one dataset |
| `evaluated, 250 cases` | The agent names no critical label |
| `no evaluation published` | The newest version has none (an earlier version's does not count) |
| `evaluation not verified` | Every evaluation of the newest version fails its check, or its key was revoked |
| `recalled` | Every version is recalled |
| `agent not found` | No such agent on this registry (a `404`) |

It says words and whole numbers only: no rate, and no name that you or anyone chose (the
agent's id is in the address, nowhere in the image). It is never green: nothing in it is a
pass, only what was measured. The image runs no script, loads nothing and links nowhere. It may
be kept for five minutes by whoever fetched it (a browser, a README's image proxy), so a new
evaluation shows within a few minutes. The registry sets no cookie on it and reads nothing of
who asks; a request for it counts against the registry's read limit, as every read does.

## Checking a record yourself

The page states what the registry verified. Anyone can check a record without trusting the
registry's word: it serves each signed record as stored, and the namespace's public keys.

<!-- check: skip registry: needs a registry -->
```python
from mendari.hub import archive, evaluations
from mendari.hub.client import RegistryClient

with RegistryClient("https://registry.example") as registry:
    shown = registry.evaluation("my-clinic", "my-agent")
    version = shown["versions"]["shown"]
    keys = {key["key_id"]: key["public_pem"] for key in registry.namespace("my-clinic")["keys"]}
    for dataset in shown["datasets"]:
        for configuration in dataset["configurations"]:
            held = registry.evaluation_record("my-clinic", "my-agent", version, configuration["id"])
            record = held["record"]
            key = archive.load_public_key(keys[record["key_id"]].encode())
            assert evaluations.signature_holds(record, key)                # the publisher signed it
            assert evaluations.evaluation_id(record) == configuration["id"]  # and it is this one
```

The signature is Ed25519 over `mendari-publisher-evaluation-v1`, a zero byte, the record's
format, a zero byte, then the record's canonical JSON without its signature (sorted keys, no
spaces, ASCII). Compare the key's id with the one on the publisher's page of the Hub.

## For whoever runs the registry

| Setting | Where | What it does |
|---|---|---|
| `MENDARI_REGISTRY_HUB_URL` | The registry (the engine, where the registry runs under it) | The Agent Hub's public address, such as `https://hub.example`. With it the registry names the Hub's page for each agent, and the command line prints it after a publish. Unset, the command line prints the badge linked to the registry's own answer and says the registry names no Hub. A value that is not an https address stops the registry |
| `REGISTRY_PUBLIC_URL` | The Agent Hub | The registry's public address, for the badge line the page shows, when the Hub reaches the registry at an address the public cannot (a container's name). Unset, `REGISTRY_URL` is used when it is https |

The three reads behind all this need no token: `GET /v1/agents/{namespace}/{name}/evaluation`
(the page's answer, `mendari.agent-evaluation/1`; `?version=` for an earlier version),
`GET /v1/agents/{namespace}/{name}/badge.svg`, and
`GET /v1/agents/{namespace}/{name}/{version}/evaluations/{id}` (one signed record).

## When something is refused

| It says | What to do |
|---|---|
| `--evaluate: the dataset is one the agent's manifest declares … never a file` | Name a dataset under `evaluation.datasets`, not a path |
| `a published evaluation is of at least 100 cases` | The dataset is too small to publish; nothing was sent |
| `version: it is recalled, deleted or not listed` | The namespace is not listed yet (a first publish waits for a reviewer), or the version was recalled |
| `is published as another build … give this one a new version` | The agent changed since this version was published: raise `version` in the manifest |
| `report: the registry's re-run counted otherwise: refused` | The registry got other counts for a local configuration. Evaluate the published build, with no local change |
| `configuration: this registry does not re-run evaluations …` | The registry's re-runs are off (`MENDARI_REGISTRY_REGRESSION=off`): it takes only configurations that call a model |
| `created_at: it is not now: sign it again` | The machine's clock is more than ten minutes off the registry's |
| `has 11 configurations: name the ones to evaluate` | Pass `--configuration` for each one to publish |
| `note: a provider failed on 3 of 600 cases …` | Published, and the page says so. Run it again for an evaluation with no failure |

## Publishing the bundled demo agent's evaluation

The demo agent (`agents/portal-message-agent`) has two configurations: `reader=rules`, local,
which the registry runs again, and `reader=agent`, which answers from a canned file unless
`MENDARI_AGENT_URL` names a live agent service (the canned agent misses paraphrased urgent
messages on purpose, and reports its model as `mock-portal-agent`). To publish the live
agent's result, start the service of [`examples/claude-agent`](https://github.com/mendari-hq/mendari/tree/main/examples/claude-agent)
and point Mendari at it.

It needs the `mendari` namespace's publisher key and a publisher token, which only the
registry's operator can mint. Publishing to a live registry is the owner's action:

<!-- check: skip registry: the owner's, on the live registry: needs its database, the publisher key and a token -->
```bash
# 1. A publisher token that lasts a day (it is printed once), on the registry's database.
mendari registry token --db "$DATABASE_URL_UNPOOLED" --namespace mendari --org mendari \
  --reason "publish the demo agent's evaluation" --days 1

# 2. The registry, and the live agent the `agent` variant calls (leave both MENDARI_AGENT_*
#    unset to publish the canned agent's result instead, which the page labels mock-portal-agent).
export MENDARI_REGISTRY_URL=https://<the engine's public origin>/registry
export MENDARI_REGISTRY_TOKEN=ohr_...            # from step 1
export MENDARI_AGENT_URL=https://<the agent service>/decide
export MENDARI_AGENT_TOKEN=...

# 3. The version, then every configuration's evaluation on its 600 synthetic cases.
mendari push agents/portal-message-agent --key "$PUBLISHER_KEY" --evaluate test \
  --labelled-by "Synthetic cases, labelled by the script that wrote them (scripts/demo_agent_data.py)"

# 4. Revoke the token (its id was printed with it).
mendari registry revoke-token --db "$DATABASE_URL_UNPOOLED" TOKEN_ID \
  --reason "the evaluation is published"
```

Step 3 prints the page's address and the badge's Markdown. The registry names the page only
once `MENDARI_REGISTRY_HUB_URL` is set on it.

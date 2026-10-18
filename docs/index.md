# Mendari

Mendari is an open platform for **Decision Packs**: clinical and operational decisions
written down so software can apply them and people can check them. A pack holds:

- the questions it asks, in words;
- the policy that says when a person must look;
- behaviour tests;
- a benchmark;
- a Decision Card.

Every decision ends in an explicit `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a
hash-chained audit record.

!!! warning "Research preview"
    Everything here runs on synthetic data. Mendari is not a medical device, and it must not
    be used with real patients until a deployment follows the PHI guide and the owner approves.

## Start here

| If you want to | Read |
|---|---|
| write a pack, without programming | [Writing a Decision Pack](guides/authoring-a-pack.md) |
| contribute a pack for everyone, by pull request | [Contributing a community pack](guides/contributing-a-pack.md) |
| call Mendari from your code, over REST or FHIR | [Using the API](guides/api.md) (on a running server: `/docs`) |
| let an EHR call the CDS Hooks services with its own signed token | [Connecting an EHR](guides/connecting-an-ehr.md) |
| run Mendari at your organization | [Deploying with real patient data](deploying-with-phi.md) |
| know exactly what a pack may contain | [The Decision Pack spec](spec/decision-pack-0.2.md) |
| understand how the engine works | [Architecture](architecture.md) |
| review its security | [Threat model](security/threat-model.md) |
| see where the project is going | [Roadmap](roadmap.md) |

## In one minute

In a clone of the [repository](https://github.com/mendari-hq/mendari) (Mendari is not on PyPI
until its first release), with [uv](https://docs.astral.sh/uv/):

<!-- check: run quickstart -->
```bash
uv sync
uv run mendari demo                                      # route, review, audit, validate locally
uv run mendari new my-pack --id my-clinic/my-pack       # your own pack, in YAML
uv run mendari test my-pack
```

`uv sync` installs the server too (without uv: `pip install '.[server]'` in the clone); then
`uv run mendari serve` serves the reference packs, and `MENDARI_PACKS_PATH` names your own packs'
folders to serve them as well. Every decision a pack can automate goes to a person instead until
its policy's safeguards run: the patient inbox, for one, automates only while a model screens
messages for emergencies, and sends everything to a person with the keyword rules alone.

The code, the reference packs and the contribution guide are in the
[repository](https://github.com/mendari-hq/mendari).

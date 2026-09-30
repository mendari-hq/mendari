# OpenHealth Hub

OpenHealth Hub is an open platform for **Decision Packs**: clinical and operational decisions
written down so software can apply them and people can check them. A pack holds:

- the questions it asks, in words;
- the policy that says when a person must look;
- behaviour tests;
- a benchmark;
- a Decision Card.

Every decision ends in an explicit `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a
hash-chained audit record.

!!! warning "Research preview"
    Everything here runs on synthetic data. OpenHealth is not a medical device, and it must not
    be used with real patients until a deployment follows the PHI guide and the owner approves.

## Start here

| If you want to | Read |
|---|---|
| write a pack, without programming | [Writing a Decision Pack](guides/authoring-a-pack.md) |
| run OpenHealth at your organization | [Deploying with real patient data](deploying-with-phi.md) |
| know exactly what a pack may contain | [The Decision Pack spec](spec/decision-pack-0.2.md) |
| understand how the engine works | [Architecture](architecture.md) |
| review its security | [Threat model](security/threat-model.md) |
| see where the project is going | [Roadmap](roadmap.md) |

## In one minute

```bash
uv sync
uv run openhealth demo                                   # route, review, audit, validate locally
uv run openhealth new my-pack --id my-clinic/my-pack    # your own pack, in YAML
uv run openhealth test my-pack
```

The code, the reference packs and the contribution guide are in the
[repository](https://github.com/Junebugg1214/openhealth-hub).

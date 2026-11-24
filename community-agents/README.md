# Community agents

Agents contributed by pull request, one per folder: `community-agents/<name>/`, with the
id `community/<name>`. They are not the agents Mendari maintains (`agents/`), they are not
clinically reviewed, and they never ship in the Python package or the images.

**How they are served.** Only where a deployment sets `MENDARI_COMMUNITY_AGENTS=on` (default
off). There, a community agent recommends and never automates: a person confirms every decision
(so no policy rule of its own blocks). It decides with local providers unless the deployment
opts it in to another variant by name, keeps only a hash of its inputs, reads at most 10,000
characters of text, runs its patterns on RE2 (which never backtracks), keeps its FHIR systems
under its own url, and is at most risk level 1.

**Contributing one.**

1. Fork the repository, clone your fork and run `uv sync`.
2. `uv run mendari new community-agents/<name> --community --publisher "<you>"` writes a working
   agent with the community defaults, its benchmark and its Decision Card.
3. Write the criteria, rules, synthetic data (never real patient data) and behaviour tests, with
   at least one test case and five benchmark cases for each critical label.
4. Regenerate the benchmark and the card after every change:
   `uv run mendari eval community-agents/<name> --dataset <benchmark dataset> --baseline none --out community-agents/<name>/evals/benchmark.json`,
   then `uv run mendari card community-agents/<name> --write`.
5. Add your agent to [`MAINTAINERS.yaml`](MAINTAINERS.yaml) with your GitHub user name.
6. Run `uv run mendari community-check community-agents/<name> --base origin/main`: the checks
   CI runs, each saying what to fix.
7. Open a pull request that changes only `community-agents/<name>/` and your entry in
   `MAINTAINERS.yaml`.

**What CI checks.** The `agent-check` job runs `mendari community-check` on every community
agent a pull request changes:

- scope: only the agent's own folder, plus its entry in `MAINTAINERS.yaml`; no git submodule;
- identity: the id `community/<name>` matches the folder, is not taken by another agent nor
  removed, and the agent is listed with its maintainers; its name, CDS Hooks title and
  publisher neither copy a maintained agent's name or CDS Hooks title nor use the word Mendari
  (or OpenHealth) as whole words (a look-alike of either is a warning for the reviewer); its
  FHIR systems live under `https://mendari.co/packs/community/<name>/`;
- the manifest: `license: Apache-2.0`, `publisher.type: community`,
  `audit.store_input: hash`, local default providers, no pinned model, no policy rule that
  blocks, `degradation_tolerance` at most 0.05, risk level at most 1;
- correctness: `mendari validate` and `mendari test` pass;
- evaluation: no critical case automated and each critical label found at least 90% of the
  time; an update does not regress against the benchmark on the base branch, within the
  tolerance the base branch set, on its own benchmark dataset and on the base branch's, and
  keeps the base branch's critical labels;
- honest numbers: the committed benchmark, Decision Card and any trained model are what the
  agent gives when CI builds them again;
- patterns: every pattern compiles on RE2, the engine community agents' patterns run on, which
  never backtracks (no lookarounds or back references; the guide lists what to write instead);
- hygiene: at most 2 MB and 200 files, text files only (a trained model aside), and a version
  bump with a `CHANGELOG.md` entry whenever what the agent decides with changes;
- personal data: text in `data/`, `tests/` and a trained model's vocabulary that looks like a
  real person's details is a warning for you and the reviewer.

A reviewer then reads what CI cannot judge: the clinical content, the risk level, the synthetic
data and where the content comes from.

**Removal.** Only the owner removes an agent (CI fails anyone else's removal), by deleting its
folder and keeping its entry in `MAINTAINERS.yaml` as a tombstone (`removed:` and `reason:`), so
its id is never reused.

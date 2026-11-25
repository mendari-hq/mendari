# Contributing

Clinicians, informaticists, researchers and engineers are all welcome. By taking part you agree
to the [code of conduct](CODE_OF_CONDUCT.md); how decisions are made is in
[GOVERNANCE.md](GOVERNANCE.md). Report security issues privately ([SECURITY.md](SECURITY.md)),
never in a public issue. Most contributions will be one of these:

## Improve the demo agent

The bundled agent, `agents/portal-message-agent`, is the one agent Mendari maintains.

1. Change its questions and criteria, its policy, its rules or its canned answers. Its datasets
   (`data/test.jsonl`, `data/written.jsonl`) and the simulated grades of what it writes
   (`data/written_grades.jsonl`) are written by a script: edit `scripts/demo_agent_data.py`,
   run it (`uv run python scripts/demo_agent_data.py`) and commit the script with what it
   wrote; CI fails when they differ. The canned answers miss some urgent messages worded as a
   patient might, on purpose: the evaluation is there to show it.
2. Run `uv run mendari validate agents/portal-message-agent`, then `test` and
   `eval --dataset test` on the same folder.
3. Add a `tests/cases.yaml` case for any behaviour you fixed or introduced, especially safety
   behaviour.
4. Raise the agent's version (MAJOR for label changes, MINOR for behaviour changes, PATCH for
   documentation).

### What CI does with a change to it

A CI job, `agent-check`, runs on every PR (with no agent changed, it says so and passes). For
every bundled agent (`agents/`) your PR changes, it:
- validates it, checks its datasets, runs its behaviour tests, and evaluates it on its
  benchmark dataset;
- renders its Decision Card (an artifact of the run);
- compares its metrics with the benchmark it publishes on `main`, in the job summary (an agent
  with none there has nothing to compare with, and passes).

It fails if a critical label is found less often, if a critical case that was never automated
now is, or if the primary accuracy or the false-automation rate gets worse by more than the
agent's tolerance. An agent that gets worse on purpose (say, a safer policy that automates less)
explains why in the PR and updates its benchmark.

## Your own agent

Mendari maintains one agent, the demo agent, and takes no other into this repository: yours
lives in a repository of your own, and you publish it to a registry under your organization's
namespace ([The Agent Hub](README.md#the-agent-hub-publish-and-pull)). Mendari is not on PyPI yet:
install it from a clone ([Install](README.md#install)). Start with
`uv run mendari new <dir> --id <namespace>/<name>` and the
[authoring guide](docs/guides/authoring-an-agent.md), which needs no programming; what `new`
writes includes a workflow that runs the same checks on every change
([Evaluating an agent on every change](docs/guides/evaluate-in-ci.md)). Commands take the
agent's folder (`uv run mendari test <dir>`); to try it in the console, put that folder on the
search path: `MENDARI_AGENTS_PATH=<dir> uv run mendari serve`. Publish it when:

- `mendari validate <dir>` and `mendari test <dir>` pass (a registry refuses a version that
  `mendari validate` refuses);
- the Decision Card states intended use, users, population, out-of-scope uses and known
  failure modes honestly;
- the risk level matches the intended use (the risk levels are in
  [the spec](docs/spec/agent-manifest.md));
- critical labels (cases a person must see: never automated by default) are declared under
  `evaluation.critical`, and its evaluations go with it (`mendari push --evaluate`);
- all data is synthetic: **never real patient data**, not even de-identified;
- a clinician other than the author has reviewed the criteria.

The demo agent is the owner's demonstration of how an agent is built: a synthetic example, not
clinically reviewed and not for patient care, as its Decision Card says. An agent meant for use
keeps the rule above.

## Code changes

```bash
uv sync
uv run pytest && uv run ruff check src tests scripts && uv run ruff format --check src tests scripts && uv run mypy src tests
uv run pytest -m browser    # if you touched the dashboard (once: uv run playwright install chromium)
```

- Keep changes small and focused: one PR does one thing. Write conventional commit messages
  (`feat(…)`, `fix(…)`, `docs:`).
- Add or change a test with every behaviour change.
- Architectural changes need an ADR in `docs/decisions/`.
- Dashboard styles use the design tokens only, and dashboard scripts are ES modules
  (`CLAUDE.md` lists the house rules).
- CI must be green before review. It covers tests on three Pythons, the browser suite, FHIR
  conformance, the sandbox end to end, security scans, the docs build and more.

### Pinned actions and images

Actions, images and downloads are pinned, so a moved tag can't change what CI or the image runs
(`tests/test_supply_chain.py` enforces it). Dependabot proposes most bumps; when you do one by
hand:

- **An action:** resolve the tag to its commit, then write
  `uses: owner/repo@<40-hex sha>  # vX.Y.Z`:
  `git ls-remote https://github.com/owner/repo.git 'refs/tags/vX.Y.Z*'` (for an annotated tag,
  take the `^{}` line, which is the commit). New `actions/checkout` steps keep
  `persist-credentials: false`.
- **An image:** keep the tag for readers and add the digest of its multi-arch index,
  `image:tag@sha256:<digest>`, from `docker buildx imagetools inspect image:tag`. Dependabot
  does not see images in workflow `services:` or `docker run` lines, or the images an action
  pulls for itself (`release.yml`'s emulator, `setup-qemu-action`'s `image`, and builder,
  `setup-buildx-action`'s `driver-opts`), or the secret scan's gitleaks (`scripts/secret_scan.sh`,
  its one pin), so check those by hand.
- **uv:** change `version:` in every `setup-uv` step and the `ghcr.io/astral-sh/uv` stage of the
  `Dockerfile` together.
- **A download:** pin its version and its sha256, and check it with `sha256sum -c` right after.
- **Swagger UI (the API reference) and axe-core (the browser tests' accessibility checks):**
  Dependabot changes only `package.json` and its lockfile in
  `src/mendari/dashboard/vendor/swagger-ui` or `tests/browser/vendor/axe-core`, and
  `tests/test_vendor.py` fails until the files follow. In that PR, run
  `uv run python scripts/vendor_docs_ui.py` (or name the package: `… axe-core`), read the hosts
  it lists (a new one deserves a look at the release notes), then rebuild `NOTICE`, which names
  both packages at the version vendored: `uv run python scripts/build_notice.py` (`--check` says
  whether it is current; `tests/test_notice_file.py` fails until it is). Commit what they wrote.
- **Accessibility:** `uv run pytest -m browser` runs axe-core on every dashboard view, an open
  record and the developer pages, in both themes (WCAG 2.0 to 2.2, A and AA). A new view or
  control is checked by adding it there (`tests/browser/test_accessibility.py`).
- **Python dependencies:** `uv lock --upgrade-package <name>`, never a bare `uv lock --upgrade`
  in a feature PR. The `Dockerfile` installs `uv.lock` exactly.
- **What the images bundle** (`licenses/image/`): every image carries the licences and notices
  of everything in it, and a release pushes none that misses one
  (`scripts/check_image_licences.py`, in CI's `docker` job and the release). A bump of a package
  with native code (`components.toml`'s `[native]`: cffi, cryptography, google-re2,
  psycopg-binary, pydantic-core, PyYAML, rpds-py) fails `tests/test_image_licences.py` until it
  is reviewed: find what its new wheels bundle or link statically (an auditwheel or cargo SBOM
  in its `.dist-info/sboms/`, its build's own scripts, the libraries' `.comment` sections),
  update or add the `[[component]]` entries with their upstream licence texts' URLs and, for
  GPL or LGPL software, its exact source pinned by SHA-256 (`corresponding_source`); then
  `uv run python scripts/image_licence_texts.py fetch`, and for a package built from Rust
  `docker build` one image per architecture and run `... crates <amd64 image> <arm64 image>`.
  A new base image (CPython or pip) is reviewed the same way, and CPython's build recipe pinned
  anew: the base image's index names the `docker-library/python` commit that built it
  (`docker buildx imagetools inspect --raw python:3.12-slim@sha256:…`, annotation
  `org.opencontainers.image.revision`); set the CPython component's `base_image` and its
  Dockerfile's URL and SHA-256 (`tests/test_image_licences.py` fails until they match the
  `Dockerfile`). Commit `licenses/image/` whole.
- **The SBOM generator:** `release.yml` names BuildKit's SBOM generator
  (`docker/buildkit-syft-scanner`) by digest, by hand like the emulator and builder, and CI's
  `image-sbom` job runs the release's SBOM scan with the same generator and builder on every
  change (a test keeps the two files the same). A base image bump that names a licence
  differently fails there; so does a new generator, so bump it in both files in one pull
  request.

## Commands in the docs

Every bash or Python block in `README.md`, `docs/index.md` and `docs/guides/api.md` says, on the
line before it, how CI checks it: `<!-- check: run ... -->` runs it on a clean runner,
`<!-- check: snippet <file>/<step> -->` is the same text as an API example CI runs, and
`<!-- check: skip <reason>: <why> -->` says why it cannot run (`scripts/doc_blocks.py` lists the
markers and the reasons). `tests/test_doc_blocks.py` fails on a block without one.

## Sign off: the Developer Certificate of Origin

Every commit is signed off: `git commit -s` adds a line
`Signed-off-by: Your Name <you@example.org>` (the name and address of your git configuration;
GitHub's `…@users.noreply.github.com` address works). By signing off you certify the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): in short, that you
wrote the contribution or have the right to submit it under the project's licence, and that
it is public and kept with your sign-off. A pull request with a commit that is not signed off
is not merged: CI's `dco` check (`scripts/check_dco.py`) fails it, naming each commit whose
message has no `Signed-off-by:` line with the author's own address. Merge commits and
Dependabot's are not checked. To sign off commits you already made:
`git rebase --signoff origin/main`, then push with `--force-with-lease`.

## Content provenance

Agents carry clinical content, so where it comes from matters as much as the code:

- **No real patient data**, anywhere: not in data, tests, issues or pull requests, not even
  de-identified. Write synthetic cases yourself.
- **No proprietary criteria or copyrighted instruments.** Do not copy text from InterQual, MCG,
  payer medical policies, licensed questionnaires or scoring instruments. Write criteria in
  your own words from public sources (guidelines, published papers, regulations), and cite
  them in `card.evidence`.
- **Code systems with licence terms** (CPT, licensed by the AMA; SNOMED CT, whose Affiliate
  Licence is free in member countries and paid in some others) appear only as their licences
  allow. List the systems an agent uses in its manifest's `code_systems`; a licensed one needs
  a `licence_note` with the terms, and an agent whose files name one by its url or OID without
  it does not load ([the spec](docs/spec/agent-manifest.md)). A bare code names no system, so
  declaring where your codes come from is yours to do. Prefer free systems (ICD-10-CM, LOINC)
  or a fictional one named as such in place of CPT and SNOMED CT (no package carries either).
- **Say what you used.** The pull request names the sources of the criteria and of
  `card.evidence`, and who reviewed the clinical content.

## Licence

Contributions are accepted under the project's licence, Apache-2.0: whatever you contribute, you
license on the same terms (inbound equals outbound). There is no separate agreement to sign: the
sign-off above is the record that you may. Source files start with
`SPDX-License-Identifier: Apache-2.0`; keep it in a file you add (`tests/test_spdx.py` checks).
Third-party material keeps its own licence: say what it is in the PR, and add it to
`licenses/notice.toml` (then `uv run python scripts/build_notice.py` writes `NOTICE`).

A maintainer merges ([GOVERNANCE.md](GOVERNANCE.md)): a pull request needs green CI and one
maintainer's approval, and an agent Mendari maintains (`mendari/…`) also needs a clinical
reviewer's sign-off.

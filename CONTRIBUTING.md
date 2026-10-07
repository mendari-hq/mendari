# Contributing

Clinicians, informaticists, researchers and engineers are all welcome. By taking part you agree
to the [code of conduct](CODE_OF_CONDUCT.md); how decisions are made is in
[GOVERNANCE.md](GOVERNANCE.md). Report security issues privately ([SECURITY.md](SECURITY.md)),
never in a public issue. Most contributions will be one of these:

## Improve a pack

1. Change criteria, rules, policy or data generation.
2. Run `./scripts/build-reference-pack.sh` to regenerate data, retrain, run the pack's
   behaviour tests, publish the benchmark and render the Decision Card.
3. Add a `tests/cases.yaml` case for any behaviour you fixed or introduced, especially safety
   behaviour.
4. Bump the pack version (MAJOR for label changes, MINOR for behaviour changes, PATCH for
   documentation) and add a `CHANGELOG.md` entry.

## Contribute a new pack

Fork the repository, clone your fork and run `uv sync` in it (Mendari is not on PyPI yet; see
[Install](README.md#install)). Start with `uv run mendari new <dir> --id <namespace>/<name>` and
the [pack-authoring guide](docs/guides/authoring-a-pack.md), which needs no programming. Commands
take the pack's folder (`uv run mendari test <dir>`); to try it in the dashboard, put its parent
folder on the search path: `MENDARI_PACKS_PATH=<parent> uv run mendari serve`. A pack is ready
for review when:

- `mendari validate <dir>` and `mendari test <dir>` pass;
- the Decision Card states intended use, users, population, out-of-scope uses and known
  failure modes honestly;
- the risk level matches the intended use (see `docs/spec/decision-pack-0.2.md` §7);
- a benchmark report is published, and critical labels (cases a person must see: never
  automated by default; see the spec, §8) are declared under `evaluation.critical`;
- all data is synthetic or openly licensed and de-identified: **never real patient data**;
- a clinician other than the author has reviewed the criteria (say who in the PR).

A pack contributed for others to use goes in `community-packs/<name>/` with the id
`community/<name>`. [Contributing a community pack](docs/guides/contributing-a-pack.md) is the
whole path: propose it with a "Propose a pack" issue, start it with
`uv run mendari new community-packs/<name> --community`, check it with
`uv run mendari community-check community-packs/<name> --base origin/main` (the checks CI runs,
each saying what to fix), and open the PR with the pack template (`?template=pack.md`).
[community-packs/README.md](community-packs/README.md) lists what CI checks.

### What CI does with a pack PR

The `pack-check` job of the `packs` workflow runs on every PR (with no pack changed, it says so
and passes). For every maintained pack (`packs/`) your PR changes, it:
- validates it, runs its behaviour tests, and evaluates it on its benchmark dataset;
- renders its Decision Card (an artifact of the run);
- compares its metrics with the benchmark on `main`, in the job summary.

It fails if a critical label is found less often, if a critical case is automated, or if the
primary accuracy or the false-automation rate gets worse by more than the pack's tolerance.
A pack that gets worse on purpose (say, a safer policy that automates less) explains why in the
PR and updates its benchmark.

For a community pack (`community-packs/`) it runs `mendari community-check` instead, and puts
the metrics and the rendered Decision Card in the job summary. A PR that changes a community
pack and anything outside its folder (its entry in `community-packs/MAINTAINERS.yaml` aside)
fails.

## Code changes

```bash
uv sync
uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests
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
  it lists (a new one deserves a look at the release notes), and commit what it wrote.
- **Accessibility:** `uv run pytest -m browser` runs axe-core on every dashboard view, an open
  record and the developer pages, in both themes (WCAG 2.0 to 2.2, A and AA). A new view or
  control is checked by adding it there (`tests/browser/test_accessibility.py`).
- **Python dependencies:** `uv lock --upgrade-package <name>`, never a bare `uv lock --upgrade`
  in a feature PR. The `Dockerfile` installs `uv.lock` exactly.

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

Packs carry clinical content, so where it comes from matters as much as the code:

- **No real patient data**, anywhere: not in data, tests, issues or pull requests, not even
  de-identified. Write synthetic cases yourself
  ([how](docs/guides/contributing-a-pack.md#synthetic-data-without-python)).
- **No proprietary criteria or copyrighted instruments.** Do not copy text from InterQual, MCG,
  payer medical policies, licensed questionnaires or scoring instruments. Write criteria in
  your own words from public sources (guidelines, published papers, regulations), and cite
  them in `card.evidence`.
- **Code systems with licence terms** (CPT, licensed by the AMA; SNOMED CT, whose Affiliate
  Licence is free in member countries and paid in some others) appear only as their licences
  allow. List the
  systems a pack uses in its manifest's `code_systems`; a licensed one needs a `licence_note`
  with the terms, and a pack whose files name one by its url or OID without it does not load
  (spec §3). A bare code names no system, so declaring where your codes come from is yours to
  do. Prefer
  free systems (ICD-10-CM, LOINC) or a fictional one named as such, as the prior-authorization
  reference pack does.
- **Say what you used.** The pack template asks for the sources of the criteria and of
  `card.evidence`, and who reviewed the clinical content.

## Licence

Contributions are accepted under the project's licence, Apache-2.0: whatever you contribute, you
license on the same terms (inbound equals outbound). There is no separate agreement to sign: the
sign-off above is the record that you may. Source files start with
`SPDX-License-Identifier: Apache-2.0`; keep it in a file you add (`tests/test_spdx.py` checks).
Third-party material keeps its own licence: say what it is in the PR, and add it to
`licenses/notice.toml` (then `uv run python scripts/build_notice.py` writes `NOTICE`).

A maintainer merges ([GOVERNANCE.md](GOVERNANCE.md)): a pull request needs green CI and one
maintainer's approval, and a reference pack also needs a clinical reviewer's sign-off.

# Mendari platform audit

November 18, 2026. Review base `b62f39ea`, the private repository’s `main` at the start of the audit.

Mendari’s engine, dashboard and public Hub can be exercised together on synthetic data. This branch addresses several places where the software could expose unnecessary case data, mix evaluation configurations, or present misleading evidence. The repository still classifies the platform as a research preview. The launch requirements for hosted operations and clinical pilots include work and independent evidence outside this engineering review. [Launch gates](../launch/gates.yaml)

## Coverage

The starting revision contained 1,120 tracked files and 397,090 text lines. The inventory covered every tracked path, including vendored browser code, generated clinical fixtures, package locks, licenses, dashboards, deployment configuration and historical project notes. Automated checks parsed source and structured records, inspected file signatures and relative documentation targets, and recorded content hashes. The file inventory also identifies changed paths and the static-analysis rules flagged in each scanned Python file. The private review artifacts include the per-file CSV.

Manual inspection concentrated on routes that handle identity, patient input, pack discovery, release artifacts, evaluation jobs and evidence shown to users. Generated ledgers and pack outputs were checked against their generators and the repository’s immutable evidence checks. This is a combination of automated whole-tree analysis and focused engineering review. An independent human line review of all 397,090 lines is outstanding.

## Findings and changes

| ID | Priority | Finding | Change and evidence |
|---|---|---|---|
| 1 | High | Dashboard navigation requested up to 500 review records for a numeric badge. Encrypted deployments could open and audit patient inputs simply to populate the navigation. | A permission- and organization-scoped count query returns only a number. Checks covered pack and tenant isolation, encrypted inputs, 501 pending records, and an update after resolution. [Count route](../src/mendari/server/app.py#L2233) · [Database query](../src/mendari/core/audit.py#L5066) |
| 2 | High | A malformed identity-provider role claim could be interpreted as a set of role names or interrupt sign-in. Publisher and site limits were checked separately from the writes they constrained. | Role parsing validates claim shapes. Registry account authorization and quota admission run inside the serialized write. Simultaneous admission is covered on SQLite and PostgreSQL. [Role parsing](../src/mendari/server/auth.py#L741) · [Registry store](../src/mendari/hub/registry.py) |
| 3 | High | A composite evaluation provider could avoid its remote-provider quota check. Restarted evaluations could select different defaults halfway through a report. Polling job and dataset summaries also loaded case bodies and partial ledgers. | A job uses the pack’s declared variants unless the caller explicitly selects others. It records that selection, enforces every applicable remote quota, and checks model consistency when resuming work. The two-organization integration and an HTTP regression verify the live deployment’s Jev default cannot trigger a remote evaluation. Polling and listings have metadata-only queries. The store also supports explicit release or withdrawal of held datasets with an audit record. [Evaluation jobs](../src/mendari/evals/store_jobs.py) · [Pack store](../src/mendari/core/pack_store.py) |
| 4 | High | An interrupted FHIR bulk export could leave part of a cohort available for evaluation. The importer could associate foreign absolute references with local patients by an ID suffix. | Exports validate manifest entries, resource types and counts before publishing a complete cohort. Patient matching uses source identity. Ambiguous patient records and foreign references fail before dataset publication. SMART key and pagination fixes retain supported integrations. [Bulk export and import](../src/mendari/standards/bulk.py) · [FHIR client](../src/mendari/standards/fhir_client.py) |
| 5 | High | The development registry seed script printed a 30-day publisher token to standard output. The engine’s Docker build context could include local deployment files. | Seeding revokes its temporary credentials. Explicit token export creates an owner-only file. The Docker context admits the files the image needs. Kubernetes workloads drop capabilities and disable the unused service-account token. [Seed credentials](../scripts/hub_seed.py#L55) · [Build context](../.dockerignore) · [Kubernetes manifests](../deploy/kubernetes/mendari.yaml) |
| 6 | Medium | The Hub could lose the entire pack page during an evidence-service outage, combine site counts across configurations, or show an unversioned command for the version on screen. The catalog’s retry button reused the failed server result. | Pack details retain recall and registry checks when evidence is missing or malformed. Evidence groups retain their own counts. CLI commands identify the displayed version. Retry fetches a fresh response and retains the search. [Pack view](../hub/app/packs/%5Bnamespace%5D/%5Bname%5D/page.tsx) · [Registry client](../hub/lib/registry.ts) · [Retry control](../hub/app/error.tsx) |
| 7 | Medium | Registry health and publisher listings rendered complete version metadata to return counts and names. Pack discovery entered unrelated directories below its documented depth. | Health and publisher routes query only relevant identifiers. Search renders the latest eligible versions, and discovery prunes the filesystem walk. [Registry catalog](../src/mendari/hub/registry.py) · [Pack discovery](../src/mendari/core/registry.py) |
| 8 | Medium | Pack scaffolding treated names as YAML syntax. Release source collection recursively deleted the supplied output directory and could expose incomplete downloads. | Templates preserve names as strings. Both commands stage their work, preserve existing output, and support retry. Release inputs use bounded filenames and the checksum listing streams source files. [Scaffolding](../src/mendari/cli.py) · [Source collector](../scripts/image_sources.py) |

Bandit flagged the SHA-1 comparison used to retrieve Debian source descriptors by their snapshot identifier. The snapshot service supplies SHA-1 identifiers. Source payloads listed in the descriptor are checked against its SHA-256 entries, and pinned component sources have their own SHA-256. The descriptor itself depends on the Debian delivery channel. [Source collector](../scripts/image_sources.py)

## Product changes

The Hub catalog now shows publisher, version, declared risk, and supported standards together. Search URLs restore their visible filter values. Empty results have a clear path back to the full catalog. Pack pages provide direct links to registry checks, publisher claims, site evidence and the Decision Card. Phone layouts give readers exact values in a table at a readable size. [Hub catalog](../hub/app/page.tsx) · [Evidence view](../hub/app/packs/%5Bnamespace%5D/%5Bname%5D/forest.tsx)

The operations dashboard separates pack context from activity, loads full-chain verification on demand, and prevents a previous pack’s form or delayed result from becoming active after a switch. The queue handles multiple pages and the data tables preserve keyboard access to overflowing content. The public site keeps its section navigation at phone widths. [Dashboard overview](../src/mendari/dashboard/overview.js) · [Review queue](../src/mendari/dashboard/review.js) · [Public site](../web/style.css)

## Measurements

| Operation | Before | After | Method |
|---|---:|---:|---|
| Public health pack count | 133.33 ms | 1.48 ms | SQLite, 66 synthetic packs including 720 additional historical version rows, 9 warm reads, median |
| Latest-version catalog read | 55.9 ms | 32.9 ms | SQLite, 727 stored versions and 66 returned packs, median of 18 interleaved warm comparisons immediately before and after batched metadata loading, with identical output |
| Latest-version catalog read | 24.29 MiB | 6.11 MiB | Peak traced Python allocation on the same catalog |
| Dataset metadata list | 32.0 MiB | 39.7 KiB | Peak traced Python allocation, 32 synthetic 1 MiB dataset bodies, identical metadata returned |
| Dataset metadata list | 360.6 ms | 0.43 ms | Same fixture, 7 measured runs with allocation tracing, median |

These are local measurements of the named operations. Production response time depends on the database, traffic, cache state and deployment.

## Dependency review

- All 112 public Python package versions across the lockfile and the pinned build backend, the Hub runtime and development graph, vendored Swagger UI, and vendored axe-core were checked against package advisories. The Hub’s starting npm audit identified 11 affected package entries. The updated lockfile clears those findings and pins Next.js 16.4.6. [Hub lockfile](../hub/package-lock.json) · [Next.js advisory](https://github.com/vercel/next.js/security/advisories/GHSA-mfr8-8qww-v76h)
- Both Ruby conformance lockfiles received compatible updates. Four advisories remain across Active Support and sqlite3 in the test images. Inferno Core 1.6.0 requires versions below the patched lines. The image-specific review records the constraints and individual advisories. [Inferno dependency review](../sandbox/inferno/README.md#ruby-dependency-review)
- The engine pins CPython 3.12.16, its base image digest, its corresponding source and the build recipe. Both the system package installer and the offline installer used by new virtual environments now use pip 26.3. Debian’s signed security snapshot supplies the zlib maintenance release. A source-package advisory match needs component-level triage to establish impact on a shipped binary. [Engine image](../Dockerfile) · [Component inventory](../licenses/image/components.toml) · [Debian zlib advisory](https://security-tracker.debian.org/tracker/DSA-6613-1)

The rebuilt ARM64 image contained 61 distinct Debian source-package/version pairs. OSV matched 98 advisory IDs across 19 source packages. Debian classified 95 as open, two as undetermined, and one as resolved with fixed version zero. Affected runtime functions and production reachability were not established for those matches. The review artifacts include the per-package triage CSV. [Debian Security Tracker](https://security-tracker.debian.org/tracker/)

The image’s 141 bundled Rust crate versions had no OSV matches. The remaining PyPI component match concerned the setuptools version declared for pip’s vendored `pkg_resources` subset. The three distinct advisories name modules absent from that wheel. The complete engine and single-pack images on AMD64 and ARM64 each bootstrapped a new virtual environment with pip 26.3 while disconnected from the network.

## Verification evidence

The complete Python run collected 8,632 cases, including Chromium and the optional slow suites, using disposable SQLite and PostgreSQL 16 databases and 55 digest-verified FHIR packages. It passed 8,625 with seven skips. The official HL7 schema was then cached and its skipped reproducibility test passed. The final overview wording also passed its focused browser test. Across the completed checks, **8,626 distinct cases passed, six database-specific parameter cases were skipped, and zero failures remained**. The 16 Hub Playwright tests run separately.

- All five reference packs rebuilt with byte-identical data, models and ledgers. Each regenerated benchmark matched its stored result. The scoreboard verified all 21 results. All 150 OMOP round-trip cases matched.
- The R4 structural rules regenerated byte for byte. All 5,309 official examples produced their expected verdicts. The pinned offline HL7 validator accepted 99 US Core, terminology and engine samples and 13 Da Vinci samples. It identified the expected problems in both negative fixtures. The repository’s warning allowlist accounted for 568 warnings in the first group and 92 in the second.
- All 104 selected Inferno results matched the checked-in expectations across DTR, CRD, SMART App Launch and Bulk Data. The observed outcomes were 63 passes, 14 known failures, 13 skips and 14 omissions. Those failures and omissions matter when evaluating readiness for a specific EHR. [Conformance fixtures](../sandbox/inferno/README.md)
- The Chromium dashboard suite passed 422 tests. The Hub production build passed its 16 Playwright tests. In an additional browser exercise, registry recovery restored a filtered search after an outage. A missing, malformed or unavailable evidence response retained the pack’s recall and verification information. Search and filter reset also worked with JavaScript disabled.
- The Hub had zero axe WCAG A/AA findings and zero page-width overflows across 16 combinations of page, viewport and theme. Firefox and WebKit also completed the catalog-to-pack and dashboard-to-review-queue workflows at phone width. Public-site checks covered the landing, privacy, terms and acceptable-use pages in both themes.
- The documented quickstart completed in 13 seconds in a clean checkout. The API snippets ran in open mode and with a service key. The CLI synthetic demo completed on Python 3.11, 3.12 and 3.13. The core suite on each of Python 3.11 and 3.13 passed 675 tests with one conditional skip. The strict documentation build, wheel build and source-distribution content checks passed.
- The final AMD64 and ARM64 images matched all 248 tracked application source files and assets byte for byte. Both passed their license inventory checks. The release scanner checked SPDX inventories of 349 packages on ARM64 and 346 on AMD64, including the pip vendor manifest matched to its installed distribution metadata. A clean virtual environment installed the built wheel, pulled the displayed Referral Router version from the local registry, and passed its 14 pack behavior cases.
- The Kubernetes manifests validated against v1.36.1 OpenAPI. The on-premises engine and worker authenticated against an isolated PostgreSQL instance with reserved characters in its test password. A live Kubernetes rollout and production load measurements remain outside this evidence.

## Release decision

| Stage transition | Prerequisites met |
|---|---:|
| Design partners | 6 of 6 |
| Open-source launch | 3 of 8 |
| Scoreboards and pack store | 2 of 7 |
| Self-service | 2 of 6 |
| Clinical pilots | 1 of 6 |

The gate totals use the repository’s own prerequisite evaluator and the observed test outcomes from this review. The open-source launch, hosted service and clinical pilot each have separate prerequisites. The release owner still needs to resolve items including hosted documentation and registry setup, external security review, clinical pack review and site-specific validation. The repository also records outstanding product work for later stages. A passing synthetic test suite supplies engineering evidence for those decisions. [Current release prerequisites](../launch/gates.yaml)

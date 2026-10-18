# SPDX-License-Identifier: Apache-2.0
"""Check FHIR resources with the official HL7 FHIR validator against US Core 6.1.0.

    uv run python scripts/fhir_validate.py --valid FILE... [--invalid FILE...] [--docker]
                                           [--offline] [--ig PACKAGE#VERSION|FOLDER|FILE.tgz ...]

- `--valid` files must have no errors, and no warnings but those `EXPECTED_WARNINGS` lists
  with the reason, so a new one is noticed: they are what Mendari produces or accepts as
  conforming (the sandbox's resources, the US Core fixtures, Tasks, the audit export, the
  terminology).
- `--invalid` files hold resources that `mendari.standards.us_core` or the structural R4 gate
  (`fhir_structure`) flags. Every resource they flag must also have an error from the validator,
  so the in-process checks never reject what R4 and US Core accept (which
  `MENDARI_FHIR_VALIDATION=enforce` would turn into a block).

`--ig` loads more implementation guides next to US Core: a package tarball, e.g. the Da Vinci
guides for the prior authorization samples (`var/fhir-packages/hl7.fhir.us.davinci-crd-2.2.1.tgz`),
a package by name (`hl7.fhir.us.davinci-crd#2.2.1`), or a folder of resources, e.g. Mendari's
terminology (`scripts/fhir_terminology.py`), so a code Mendari emits that its CodeSystem lacks is an
error. The validator runs without a terminology
server (`-tx n/a`), so the result does not depend on a remote service. Java comes from the PATH,
or from the eclipse-temurin image with `--docker`. Set `FHIR_VALIDATOR_JAR` to the jar (default
`var/validator/validator_cli.jar`).

`--offline` runs the validator with no HTTP access (`-no-http-access`; with `--docker`, in a
container with no network too), so it loads FHIR packages only from its package cache. Each run
gets a cache of its own, holding only what it loads (the validator's own packages, US Core and
each `--ig` package) and every package they need, from the tarballs `scripts/fhir_packages.lock`
pins, each checked against its sha256 (`scripts/fhir_packages.py install`). That is how CI runs
it: a package by name then resolves to the locked one, a package the lock lacks fails the run,
and so does one the validator put in its cache while it ran. A resource that cites a canonical
of a guide the run does not load (a Da Vinci operation in the US Core run) cannot pull that
guide in, with another US Core it needs, for the files after it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # fhir_packages, a script beside this one

import fhir_packages  # noqa: E402

from mendari.standards import fhir_structure, us_core  # noqa: E402

JAR = Path(os.environ.get("FHIR_VALIDATOR_JAR", ROOT / "var/validator/validator_cli.jar"))
IMAGE = "eclipse-temurin:21-jre"
ARGS = ["-version", "4.0.1", "-ig", "hl7.fhir.us.core#6.1.0", "-tx", "n/a"]
OFFLINE = "-no-http-access"
# Java ignores HOME: the package cache follows user.home, which the container sets to this.
DOCKER_HOME = ROOT / "var/validator/cache"
LOG_TAIL = 40  # lines of the validator's own output shown when it writes no report
_RESOURCE = re.compile(r"/\*(\w+/[^*]+)\*/")


@dataclass(frozen=True)
class Expected:
    """A warning the validator gives that is expected, and why: `message` and `path` are
    regular expressions searched in its text and its expression (FHIRPath)."""

    message: str
    path: str
    why: str

    def matches(self, issue: dict[str, str]) -> bool:
        return bool(re.search(self.message, issue["text"]) and re.search(self.path, issue["where"]))


# What Mendari narrates (dom-6); inputs and the Da Vinci resources have no narrative yet.
NARRATED = "Task|AuditEvent|Provenance|CodeSystem|NamingSystem"
NO_TERMINOLOGY_SERVER = "CI runs the validator without a terminology server (-tx n/a)"
EXPECTED_WARNINGS = (
    Expected(
        r"^Constraint failed: dom-6:",
        rf"^(?!({NARRATED})$)(?!.*/\*({NARRATED})/)",
        "inputs (US Core fixtures, sandbox records, the record Bundle) and the Da Vinci resources "
        "carry no narrative; what Mendari decides and records, and its terminology, do",
    ),
    Expected(
        r"^The Coding provided \(https://mendari\.co/fhir/CodeSystem/audit-event#[\w.]+\) was "
        r"not found in the value set 'Audit Event Sub-Type'",
        r"\.subtype\[\d+\]$",
        "the subtype stays a Mendari code (an extensible binding): a FHIR restful-interaction "
        "code would claim REST interactions that never happened",
    ),
    Expected(
        r"^Best Practice Recommendation: In general, all observations should have a performer",
        r"/\*Observation/",
        "synthetic lab results name no performer, which US Core allows",
    ),
    Expected(
        r"^No code provided, and a code should be provided from the value set "
        r"'US Core Condition Codes'",
        r"/\*ServiceRequest/[^*]*\*/\.reasonCode\[\d+\]$",
        "synthetic referrals give their reason as text (an extensible binding)",
    ),
    Expected(
        r"^None of the codings provided are in the value set 'US Core Procedure Codes' .*"
        r"\(codes = https://mendari\.co/fhir/CodeSystem/fictional-referrals#FR\d{3}\)$",
        r"/\*ServiceRequest/[^*]*\*/\.code$",
        "a synthetic referral's code is the fictional referral code (LP-14: SNOMED CT, whose "
        "codes the value set's referrals are in, is in no package), an extensible binding",
    ),
    Expected(r"CodeSystem 'urn:ietf:bcp:47'.* could not be found", "", NO_TERMINOLOGY_SERVER),
    Expected(
        r"^A definition for CodeSystem 'http://www\.cms\.gov/Medicare/Coding/ICD10'( version "
        r"'null')? could not be found, so the code cannot be validated",
        r"\.(code|productOrService)\.coding\[\d+\]\.system$",
        "ICD-10-PCS, the public-domain code a synthetic order also carries for the payer (LP-14), "
        "is in no FHIR package; " + NO_TERMINOLOGY_SERVER,
    ),
    Expected(
        r"^Unable to validate code .* in system 'http://unitsofmeasure\.org' because the "
        "validator is running without terminology services",
        "",
        NO_TERMINOLOGY_SERVER,
    ),
    Expected(
        r"matches the reference Patient/record-patient by type and id but its fullUrl "
        r"urn:mendari:Patient/record-patient does not match",
        r"\.subject$",
        "the record Bundle (standards/record_fhir.py) refers to its Patient by type and id, "
        "which is how CQL engines read it",
    ),
    Expected(
        r"(CodeSystem|code system) 'https://codesystem\.x12\.org/|"
        r"^ValueSet 'https://valueset\.x12\.org/",
        "",
        "Da Vinci PAS uses X12 code systems, which are licensed and in no FHIR package",
    ),
    Expected(
        r"^No code provided, and a code should be provided from the value set "
        r"'CRD Coverage Assertion Reasons Value Set'",
        r"^ServiceRequest\.extension\[\d+\]\.extension\[\d+\]",
        "a pack gives its coverage reason as text (an extensible binding)",
    ),
    Expected(
        r"^None of the codings provided are in the value set 'IdentifierType'",
        r"/\*Coverage/[^*]*\*/\.identifier\[\d+\]\.type$",
        "the PAS member id is typed MB (v2-0203), which PAS uses and R4's value set lacks",
    ),
    Expected(
        r"^The questionnaire 'https://mendari\.co/packs/[^']+' could not be resolved",
        r"^Parameters\.parameter\[\d+\]\.resource",
        "the DTR package carries its Questionnaire, but the validator resolves canonicals only "
        "from loaded packages",
    ),
    Expected(
        r"^URN reference is not locally contained within the bundle urn:uuid:",
        r"/\*ClaimResponse/[^*]*\*/\.request$",
        "the PAS response refers to the Claim in the request Bundle",
    ),
)


def run_validator(
    files: list[Path], docker: bool, igs: list[str] | None = None, offline: bool = False
) -> dict[str, list[dict[str, str]]]:
    """Errors and warnings per file:
    [{"severity": "error", "resource": "Patient/p1", "where": ..., "text": ...}]."""
    local = _mounted if docker else str
    loaded = [local(Path(ig)) if Path(ig).exists() else ig for ig in igs or []]
    args = [*ARGS, *([OFFLINE] if offline else []), *(p for ig in loaded for p in ("-ig", ig))]
    (ROOT / "var").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "var") as scratch:
        out = Path(scratch) / "out.json"
        log = Path(scratch) / "validator.log"
        # The package cache follows user.home (Java ignores HOME). Offline, the run has a cache
        # of its own with only what it loads, so a resource that cites another package's
        # canonical finds nothing more to load (scripts/fhir_packages.py, GUIDES).
        home = Path(scratch) / "home" if offline else DOCKER_HOME if docker else Path.home()
        cache = home / ".fhir" / "packages"
        installed = _install(cache, igs or []) if offline else set()
        command = ["java", f"-Duser.home={local(home)}", "-jar", local(JAR)]
        if docker:
            network = ["--network", "none"] if offline else []
            container = ["docker", "run", "--rm", *network, "-v", f"{ROOT}:/work"]
            command = [*container, "-e", f"HOME={local(home)}", IMAGE, *command]
        command += [*args, *map(local, files), "-output", local(out)]
        with log.open("wb") as handle:
            subprocess.run(command, check=False, stdout=handle, stderr=subprocess.STDOUT)
        if not out.exists():
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
            print("\n".join(tail), file=sys.stderr)
            sys.exit("the validator produced no report; the end of its output is above")
        report = json.loads(out.read_text(encoding="utf-8"))
        fetched = sorted(_cached(cache) - installed) if offline else []
    if fetched:
        sys.exit(
            f"the validator put {', '.join(fetched)} in its package cache, which this run's "
            "packages from scripts/fhir_packages.lock do not include"
        )
    outcomes = [e["resource"] for e in report["entry"]] if "entry" in report else [report]
    issues: dict[str, list[dict[str, str]]] = {}
    for outcome, path in zip(outcomes, files, strict=True):
        found = []
        for issue in outcome.get("issue", []):
            if issue["severity"] not in ("error", "fatal", "warning"):
                continue
            where = (issue.get("expression") or [""])[0]
            match = _RESOURCE.search(where)
            found.append(
                {
                    "severity": "warning" if issue["severity"] == "warning" else "error",
                    "resource": match.group(1) if match else "",
                    "where": where,
                    "text": issue.get("details", {}).get("text", ""),
                }
            )
        issues[str(path)] = found
    return issues


def _mounted(path: Path) -> str:
    """Where a file under the repository is in the container (mounted at /work)."""
    return f"/work/{path.resolve().relative_to(ROOT)}"


def loads(igs: list[str]) -> list[str]:
    """The packages a run loads, as `name#version`: the validator's own, US Core (`ARGS`) and
    each `--ig` package (a tarball the lock pins, or `name#version`; a folder of resources is
    none)."""
    lock = fhir_packages.read_lock(fhir_packages.LOCK)
    tarballs = {(fhir_packages.DOWNLOADS / p.tarball).resolve(): p.ref for p in lock}
    found = [*fhir_packages.VALIDATOR_ROOTS]
    found += [ARGS[i + 1] for i, arg in enumerate(ARGS) if arg == "-ig"]
    for ig in igs:
        if ig.endswith(".tgz"):
            if Path(ig).resolve() not in tarballs:
                sys.exit(f"--ig {ig}: not a tarball scripts/fhir_packages.lock pins")
            found.append(tarballs[Path(ig).resolve()])
        elif not Path(ig).is_dir():
            found.append(ig)
    return found


def _install(cache: Path, igs: list[str]) -> set[str]:
    """Put in `cache` what the run loads and every package that needs, from the tarballs the
    lock pins, and return what it holds."""
    try:
        chosen = fhir_packages.install(
            cache, fhir_packages.DOWNLOADS, fhir_packages.LOCK, fhir_packages.ROOTS, loads(igs)
        )
    except fhir_packages.LockError as error:
        sys.exit(str(error))
    print(f"package cache ({len(chosen)} locked packages): {', '.join(p.ref for p in chosen)}")
    return {package.ref for package in chosen}


def _cached(cache: Path) -> set[str]:
    """The packages in the validator's package cache, as `name#version`."""
    if not cache.is_dir():
        return set()
    return {entry.name for entry in cache.iterdir() if entry.is_dir() and "#" in entry.name}


def unexpected(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    """The errors, and the warnings no entry of EXPECTED_WARNINGS explains."""
    return [
        issue
        for issue in issues
        if issue["severity"] == "error" or not any(e.matches(issue) for e in EXPECTED_WARNINGS)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--valid", nargs="*", default=[], type=Path)
    parser.add_argument("--invalid", nargs="*", default=[], type=Path)
    parser.add_argument("--docker", action="store_true", help="run Java in the temurin image")
    parser.add_argument(
        "--offline", action="store_true", help="no HTTP access: only the locked packages"
    )
    parser.add_argument(
        "--ig",
        action="append",
        default=[],
        help="another IG: a package tarball (.tgz), package#version, or a folder",
    )
    args = parser.parse_args()
    files = [*args.valid, *args.invalid]
    if not files:
        parser.error("no files")
    for ig in args.ig:
        if ig.endswith(".tgz") and not Path(ig).is_file():
            parser.error(f"--ig {ig}: no such package tarball (see scripts/fhir_packages.lock)")
    issues = run_validator(files, args.docker, args.ig, args.offline)
    failed = False
    for path in args.valid:
        found = issues[str(path)]
        wrong = unexpected(found)
        errors = sum(issue["severity"] == "error" for issue in wrong)
        warnings = len(wrong) - errors
        status = f"{errors} error(s), {warnings} unexpected warning(s)" if wrong else "ok"
        if len(found) > len(wrong):
            status += f" ({len(found) - len(wrong)} expected warnings)"
        print(f"valid    {path}: {status}")
        for issue in wrong:
            print(f"    {issue['severity']} {issue['where']}: {issue['text']}")
        failed |= bool(wrong)
    for path in args.invalid:
        document = json.loads(path.read_text())
        found = fhir_structure.check(document) + us_core.validate(document)
        flagged = {i.resource for i in found}
        confirmed = {e["resource"] for e in issues[str(path)] if e["severity"] == "error"}
        missed = sorted(flagged - confirmed)
        status = "ok" if not missed else "the validator accepts what Mendari flags"
        print(f"invalid  {path}: {len(flagged)} flagged in process, {status}")
        for resource in missed:
            print(f"    {resource}")
        failed |= bool(missed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Check FHIR resources with the official HL7 FHIR validator against US Core 6.1.0.

    uv run python scripts/fhir_validate.py --valid FILE... [--invalid FILE...] [--docker]
                                           [--ig PACKAGE#VERSION|FOLDER ...]

- `--valid` files must have no errors, and no warnings but those `EXPECTED_WARNINGS` lists
  with the reason, so a new one is noticed: they are what Mendari produces or accepts as
  conforming (the sandbox's resources, the US Core fixtures, Tasks, the audit export, the
  terminology).
- `--invalid` files hold resources that `mendari.standards.us_core` flags. Every resource it
  flags must also have an error from the validator, so the in-process subset never rejects what
  US Core accepts (which `MENDARI_FHIR_VALIDATION=enforce` would turn into a block).

`--ig` loads more implementation guides next to US Core, e.g. the Da Vinci guides for the prior
authorization samples (`hl7.fhir.us.davinci-crd#2.2.1`), or a folder of resources, e.g. Mendari's
terminology (`scripts/fhir_terminology.py`), so a code Mendari emits that its CodeSystem lacks is an
error. The validator runs without a terminology
server (`-tx n/a`), so the result does not depend on a remote service. Java comes from the PATH,
or from the eclipse-temurin image with `--docker`. Set `FHIR_VALIDATOR_JAR` to the jar (default
`var/validator/validator_cli.jar`).
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

from mendari.standards import us_core  # noqa: E402

JAR = Path(os.environ.get("FHIR_VALIDATOR_JAR", ROOT / "var/validator/validator_cli.jar"))
IMAGE = "eclipse-temurin:21-jre"
ARGS = ["-version", "4.0.1", "-ig", "hl7.fhir.us.core#6.1.0", "-tx", "n/a"]
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
    Expected(r"CodeSystem 'urn:ietf:bcp:47'.* could not be found", "", NO_TERMINOLOGY_SERVER),
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
    files: list[Path], docker: bool, igs: list[str] | None = None
) -> dict[str, list[dict[str, str]]]:
    """Errors and warnings per file:
    [{"severity": "error", "resource": "Patient/p1", "where": ..., "text": ...}]."""
    local = _mounted if docker else str
    loaded = [local(Path(ig)) if Path(ig).exists() else ig for ig in igs or []]
    args = [*ARGS, *(part for ig in loaded for part in ("-ig", ig))]
    (ROOT / "var").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "var") as scratch:
        out = Path(scratch) / "out.json"
        if docker:
            # Java ignores HOME: the package cache follows user.home.
            cache = "/work/var/validator/cache"
            command = ["docker", "run", "--rm", "-v", f"{ROOT}:/work", "-e", f"HOME={cache}"]
            command += [IMAGE, "java", f"-Duser.home={cache}", "-jar", _mounted(JAR)]
            command += [*args, *map(_mounted, files), "-output", _mounted(out)]
        else:
            command = ["java", "-jar", str(JAR), *args, *map(str, files), "-output", str(out)]
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL)
        if not out.exists():
            sys.exit("the validator produced no output; run it by hand to see why")
        report = json.loads(out.read_text(encoding="utf-8"))
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
        "--ig", action="append", default=[], help="another IG: package#version, or a folder"
    )
    args = parser.parse_args()
    files = [*args.valid, *args.invalid]
    if not files:
        parser.error("no files")
    issues = run_validator(files, args.docker, args.ig)
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
        flagged = {i.resource for i in us_core.validate(json.loads(path.read_text()))}
        confirmed = {e["resource"] for e in issues[str(path)] if e["severity"] == "error"}
        missed = sorted(flagged - confirmed)
        status = "ok" if not missed else "the validator accepts what us_core flags"
        print(f"invalid  {path}: {len(flagged)} flagged by us_core, {status}")
        for resource in missed:
            print(f"    {resource}")
        failed |= bool(missed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Check FHIR resources with the official HL7 FHIR validator against US Core 6.1.0.

    uv run python scripts/fhir_validate.py --valid FILE... [--invalid FILE...] [--docker]
                                           [--ig PACKAGE#VERSION ...]

- `--valid` files must have no errors: they are what OpenHealth produces or accepts as
  conforming (the sandbox's resources, the US Core fixtures, the audit export).
- `--invalid` files hold resources that `openhealth.standards.us_core` flags. Every resource it
  flags must also have an error from the validator, so the in-process subset never rejects what
  US Core accepts (which `OPENHEALTH_FHIR_VALIDATION=enforce` would turn into a block).

`--ig` loads more implementation guides next to US Core, e.g. the Da Vinci guides for the prior
authorization samples (`hl7.fhir.us.davinci-crd#2.2.1`). The validator runs without a terminology
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from openhealth.standards import us_core  # noqa: E402

JAR = Path(os.environ.get("FHIR_VALIDATOR_JAR", ROOT / "var/validator/validator_cli.jar"))
IMAGE = "eclipse-temurin:21-jre"
ARGS = ["-version", "4.0.1", "-ig", "hl7.fhir.us.core#6.1.0", "-tx", "n/a"]
_RESOURCE = re.compile(r"/\*(\w+/[^*]+)\*/")


def run_validator(
    files: list[Path], docker: bool, igs: list[str] | None = None
) -> dict[str, list[dict[str, str]]]:
    """Errors per file: [{"resource": "Patient/p1", "where": ..., "text": ...}]."""
    args = [*ARGS, *(part for ig in igs or [] for part in ("-ig", ig))]
    with tempfile.TemporaryDirectory(dir=ROOT / "var") as scratch:
        out = Path(scratch) / "out.json"
        if docker:
            mounted = [f"/work/{f.resolve().relative_to(ROOT)}" for f in files]
            jar = f"/work/{JAR.resolve().relative_to(ROOT)}"
            command = ["docker", "run", "--rm", "-v", f"{ROOT}:/work"]
            command += ["-e", "HOME=/work/var/validator/cache", IMAGE, "java", "-jar", jar]
            command += [*args, *mounted, "-output", f"/work/{out.relative_to(ROOT)}"]
        else:
            command = ["java", "-jar", str(JAR), *args, *map(str, files), "-output", str(out)]
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL)
        if not out.exists():
            sys.exit("the validator produced no output; run it by hand to see why")
        report = json.loads(out.read_text(encoding="utf-8"))
    outcomes = [e["resource"] for e in report["entry"]] if "entry" in report else [report]
    errors: dict[str, list[dict[str, str]]] = {}
    for outcome, path in zip(outcomes, files, strict=True):
        found = []
        for issue in outcome.get("issue", []):
            if issue["severity"] not in ("error", "fatal"):
                continue
            where = (issue.get("expression") or [""])[0]
            match = _RESOURCE.search(where)
            found.append(
                {
                    "resource": match.group(1) if match else "",
                    "where": where,
                    "text": issue.get("details", {}).get("text", ""),
                }
            )
        errors[str(path)] = found
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--valid", nargs="*", default=[], type=Path)
    parser.add_argument("--invalid", nargs="*", default=[], type=Path)
    parser.add_argument("--docker", action="store_true", help="run Java in the temurin image")
    parser.add_argument("--ig", action="append", default=[], help="another IG, package#version")
    args = parser.parse_args()
    files = [*args.valid, *args.invalid]
    if not files:
        parser.error("no files")
    errors = run_validator(files, args.docker, args.ig)
    failed = False
    for path in args.valid:
        found = errors[str(path)]
        status = "ok" if not found else f"{len(found)} error(s)"
        print(f"valid    {path}: {status}")
        for error in found:
            print(f"    {error['where']}: {error['text']}")
        failed |= bool(found)
    for path in args.invalid:
        flagged = {i.resource for i in us_core.validate(json.loads(path.read_text()))}
        confirmed = {e["resource"] for e in errors[str(path)]}
        missed = sorted(flagged - confirmed)
        status = "ok" if not missed else "the validator accepts what us_core flags"
        print(f"invalid  {path}: {len(flagged)} flagged by us_core, {status}")
        for resource in missed:
            print(f"    {resource}")
        failed |= bool(missed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

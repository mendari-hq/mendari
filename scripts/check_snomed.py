# SPDX-License-Identifier: Apache-2.0
"""No SNOMED CT content in a package (LP-14). The wheel and the sdist (and the images, which copy
the same src/, packs/ and datasets/) carry no SNOMED CT concept id the repository has used, no
SNOMED CT concept URI, and SNOMED CT's url or OID only in the modules that must name the system
(`NAMES_THE_SYSTEM`, none holding a code). The reference packs, their data and the sandbox use
fictional codes instead. The tests keep SNOMED CT (the US Core fixtures code procedures and
referrals in it) and ship in no package. Exit 1, naming each file and what it carries.

    uv run python scripts/check_snomed.py dist/mendari-*.tar.gz dist/*.whl

tests/test_code_systems.py runs the same check on the tracked files the packages are built from.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from mendari.core.spec import licensed_systems

SNOMED = "http://snomed.info/sct"
# Every SNOMED CT concept id the repository has carried: the six the reference packs, their data
# and the sandbox used before LP-14 swapped them for fictional codes, and the three only the US
# Core test fixtures use. A bare id names no system, so the scan looks for these values.
CONCEPT_IDS = (
    "44054006",  # type 2 diabetes mellitus
    "46635009",  # type 1 diabetes mellitus
    "22298006",  # myocardial infarction
    "230690007",  # cerebrovascular accident
    "432504007",  # cerebral infarction
    "3457005",  # patient referral
    "183519002",  # referral to cardiology service (tests only)
    "91251008",  # physical therapy procedure (tests only)
    "108252007",  # laboratory procedure (tests only)
)
_CONCEPT_ID = re.compile(r"(?<![\w.])(" + "|".join(CONCEPT_IDS) + r")(?![\w.])")
# The modules that name SNOMED CT's url or OID, by their path in the package, and why.
NAMES_THE_SYSTEM = {
    "mendari/core/spec.py": "the licensed-code-system check, which names SNOMED CT's url and OID "
    "to refuse a pack that carries them undeclared",
    "mendari/standards/us_core.py": "US Core's rule us-core-4, which asks for SNOMED CT in an "
    "Observation's coded value",
    "mendari/standards/record_fhir.py": "reads a site's own condition codes (neither ICD-10-CM "
    "nor fictional) as SNOMED CT when it turns a record into FHIR",
}


def package_path(name: str) -> str:
    """A member's path as installed: the sdist's root folder and `src/` dropped, so
    `mendari-0.1.0/src/mendari/core/spec.py` and the wheel's `mendari/core/spec.py` agree."""
    path = name
    if not path.startswith(("mendari/", "src/")) and "/" in path:
        path = path.split("/", 1)[1]  # the sdist's root folder
    return path.removeprefix("src/")


def findings(name: str, text: str) -> list[str]:
    """What the file `name` (a package member or a tracked path) carries of SNOMED CT."""
    found = [
        f"{name}: SNOMED CT concept id {code}" for code in sorted(set(_CONCEPT_ID.findall(text)))
    ]
    if "snomed.info/id/" in text:
        found.append(f"{name}: a SNOMED CT concept URI")
    if SNOMED in licensed_systems(text) and package_path(name) not in NAMES_THE_SYSTEM:
        found.append(f"{name}: names SNOMED CT (its url, OID or a concept URI)")
    return found


def members(path: Path) -> Iterator[tuple[str, str]]:
    """Each file in a wheel, an sdist or a directory, with its text."""
    if path.is_dir():
        for file in sorted(p for p in path.rglob("*") if p.is_file()):
            yield file.relative_to(path).as_posix(), file.read_text("utf-8", errors="replace")
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info).decode("utf-8", errors="replace")
    else:
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                extracted = archive.extractfile(member) if member.isfile() else None
                if extracted is not None:
                    yield member.name, extracted.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("packages", nargs="+", type=Path, help="a .whl, an sdist or a directory")
    args = parser.parse_args()
    failed = False
    for package in args.packages:
        files = list(members(package))
        found = [line for name, text in files for line in findings(name, text)]
        for line in found:
            print(line)
        if not found:
            print(f"ok  {package.name}: {len(files)} files, no SNOMED CT content")
        failed |= bool(found)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

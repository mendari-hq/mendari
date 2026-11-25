# SPDX-License-Identifier: Apache-2.0
"""Generate `src/mendari/standards/us_core_6_1_0.json` from the official FHIR packages.

The in-process US Core check (`mendari.standards.us_core`) reads that file. It holds, for each
profile the agents' FHIR mappings read:

- `required`: element paths with a minimum cardinality of 1 (a child counts only where its parent
  is present);
- `codes`: required bindings whose value set can be enumerated from the packages, as
  `[system, code]` pairs;
- `slices`: required `category` slices, as the codings that satisfy them;
- `invariants`: the profile's own `us-core-*` constraints, which the checker implements by key.

Usage (the packages are downloaded once and verified by digest):

    uv run python scripts/us_core_subset.py
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src/mendari/standards/us_core_6_1_0.json"
CACHE = ROOT / "var/fhir-packages"
PACKAGES = {
    "us_core": (
        "hl7.fhir.us.core",
        "6.1.0",
        "ec096f29bc4ce8f04117dfc4ef82e7289c67a7e2188892e86288c45389d158d9",
    ),
    "r4": (
        "hl7.fhir.r4.core",
        "4.0.1",
        "ebd7731df7d36b5b7d39d5fb6c9d77b44bb7fe5742f1a2e87f164738c3289d44",
    ),
}
# US Core profiles for the resources the agents read, and base R4 for the types US Core 6.1.0
# does not profile.
US_CORE = [
    "us-core-patient",
    "us-core-condition-problems-health-concerns",
    "us-core-condition-encounter-diagnosis",
    "us-core-observation-lab",
    "us-core-servicerequest",
    "us-core-documentreference",
    "us-core-diagnosticreport-lab",
    "us-core-diagnosticreport-note",
    "us-core-coverage",
    "us-core-procedure",
    "us-core-medicationrequest",
    "us-core-encounter",
]
BASE = ["Observation", "MedicationStatement", "Communication"]
MAX_DEPTH = 2  # element paths below Resource.a.b are left to the HL7 validator


def package(name: str, version: str, sha256: str) -> dict[str, Any]:
    """Every JSON file in the package, by file name."""
    path = CACHE / f"{name}-{version}.tgz"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://packages.fhir.org/{name}/{version}"
        with urllib.request.urlopen(url, timeout=120) as response:
            path.write_bytes(response.read())
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        sys.exit(f"{path}: sha256 {digest}, expected {sha256}")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith(".json"):
                handle = archive.extractfile(member)
                if handle is not None:
                    try:
                        files[Path(member.name).name] = json.loads(handle.read())
                    except json.JSONDecodeError:
                        continue
    return files


def by_url(*packages: dict[str, Any]) -> dict[str, Any]:
    index = {}
    for files in packages:
        for resource in files.values():
            if isinstance(resource, dict) and "url" in resource:
                index[resource["url"].split("|")[0]] = resource
    return index


def concepts(code_system: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(items: list[dict[str, Any]]) -> None:
        for item in items:
            found.append(item["code"])
            walk(item.get("concept", []))

    walk(code_system.get("concept", []))
    return found


def expand(value_set_url: str, index: dict[str, Any]) -> list[list[str]] | None:
    """The value set's codes as [system, code] pairs, or None if it can't be enumerated here
    (filters, other value sets, or code systems not in the packages)."""
    value_set = index.get(value_set_url.split("|")[0])
    if value_set is None or "compose" not in value_set:
        return None
    pairs: list[list[str]] = []
    for include in value_set["compose"].get("include", []):
        system = include.get("system")
        if include.get("filter") or include.get("valueSet") or not system:
            return None
        if "concept" in include:
            pairs += [[system, c["code"]] for c in include["concept"]]
            continue
        code_system = index.get(system)
        if code_system is None or code_system.get("content") != "complete":
            return None
        pairs += [[system, code] for code in concepts(code_system)]
    if value_set["compose"].get("exclude"):
        return None
    return pairs or None


def profile(structure: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    kind = structure["type"]
    required: list[str] = []
    codes: dict[str, list[list[str]]] = {}
    slices: list[dict[str, Any]] = []
    invariants: list[dict[str, str]] = []
    for element in structure["snapshot"]["element"]:
        element_id: str = element["id"]
        path = element["path"].removeprefix(kind).removeprefix(".")
        for constraint in element.get("constraint", []):
            if constraint["key"].startswith("us-core-"):
                invariants.append(
                    {"key": constraint["key"], "path": path, "human": constraint["human"]}
                )
        if not path or "extension" in path.lower() or path.count(".") >= MAX_DEPTH:
            continue
        binding = element.get("binding", {})
        if ":" in element_id:
            # A slice. Only required category slices are checked here (profiles tell resources
            # apart by category); the rest is left to the HL7 validator.
            if element_id.count(":") != 1 or element_id.split(":")[0] != f"{kind}.category":
                continue
            if element.get("min", 0) < 1 or "." in element_id.split(":")[1]:
                continue
            pattern = element.get("patternCodeableConcept")
            if pattern:
                pairs = [[c["system"], c["code"]] for c in pattern.get("coding", [])]
            elif binding.get("strength") == "required":
                pairs = expand(binding["valueSet"], index) or []
            else:
                pairs = []
            if pairs:
                slices.append(
                    {"path": "category", "name": element_id.split(":")[1], "codings": pairs}
                )
            continue
        if element.get("min", 0) >= 1:
            required.append(path)
        if binding.get("strength") == "required" and "valueSet" in binding:
            pairs = expand(binding["valueSet"], index)
            if pairs is not None:
                codes[path] = pairs
    # A child is only required where its parent is present, so keep children of any element:
    # the checker applies "a.b" to every present "a".
    return {
        "url": structure["url"],
        "type": kind,
        "title": structure.get("title") or structure["name"],
        "required": required,
        "codes": codes,
        "slices": slices,
        "invariants": invariants,
    }


def main() -> None:
    us_core = package(*PACKAGES["us_core"])
    r4 = package(*PACKAGES["r4"])
    index = by_url(r4, us_core)
    profiles = {}
    for name in US_CORE:
        profiles[name] = profile(us_core[f"StructureDefinition-{name}.json"], index)
    for kind in BASE:
        profiles[f"base-{kind.lower()}"] = profile(r4[f"StructureDefinition-{kind}.json"], index)
    out = {
        "source": {
            key: {"package": f"{name}#{version}", "sha256": sha256}
            for key, (name, version, sha256) in PACKAGES.items()
        },
        "generated_by": "scripts/us_core_subset.py",
        "profiles": profiles,
    }
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(profiles)} profiles)")


if __name__ == "__main__":
    main()

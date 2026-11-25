# SPDX-License-Identifier: Apache-2.0
"""Generate `src/mendari/standards/fhir_r4_structure.json` from HL7's R4 JSON Schema.

The structural gate on FHIR input (`mendari.standards.fhir_structure`) reads that file: the
definitions of every R4 resource and data type, with JSON types, elements, formats, required
elements and required codes. The source is the schema published with R4 (CC0), pinned by the
digest of `fhir.schema.json`; the zip around it was re-zipped by HL7 after publication, so its
digest is only reported. The schema in the `hl7.fhir.r4.core` package (`openapi/`) is an older
build that differs in twelve definitions, and is not used.

The published schema needs these patches, each recorded in the output's `source.patches`:

1. `"type": "object"` on every complex type. The schema leaves it off, so a string where an
   object belongs passes.
2. The `_x` sibling (its id and extensions) of every `canonical` element that lacks one, such as
   `Meta._profile`. Every other primitive element has it. A canonical element is also taken off
   its parent's `required` list, like every other primitive, since its `_x` alone may carry it.
3. ASCII semantics, `(?a)`, for the `string` and `markdown` pattern: Python's `\\S` is Unicode,
   so the pattern rejects a no-break space, which FHIR allows. The pattern also accepts a vertical
   tab and a form feed (a word processor's soft line break, a page break), which the HL7
   validator only warns about. The `code` pattern keeps Unicode semantics, so a code with a
   no-break space is still rejected.
4. `integer`, `positiveInt` and `unsignedInt` as JSON Schema integers within 32 bits, with a
   minimum of 1 and 0 for the last two. The schema types them as any number, and its patterns
   only apply to strings, so 1.5, 0 for a positiveInt and 2147483648 passed; the HL7 validator
   rejects all three.
5. No `enum` on `Expression.language`: R4 binds it as extensible (CQL and SDC use other media
   types), and the schema enumerates it as if it were required. It is the only one of the
   schema's 246 enumerations whose R4 binding is not required (`tests/test_fhir_structure.py`
   checks this against the `hl7.fhir.r4.core` package when it is cached).

The 146-way `oneOf` of `ResourceList` becomes a choice by `resourceType` (the
`resourceChoice` keyword): one resource definition is checked instead of all of them. The
`description` keyword is dropped (not the elements named `description`).

Usage (the schema is downloaded once into `var/fhir-packages/` and verified by digest; the
output is byte-reproducible, and CI fails on any difference):

    uv run python scripts/fhir_r4_schema.py
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src/mendari/standards/fhir_r4_structure.json"
CACHE = ROOT / "var/fhir-packages/fhir.schema.json.zip"
URL = "https://hl7.org/fhir/R4/fhir.schema.json.zip"
SHA256 = "2230406893b4cf002a4ee1e5e2bbeca22ac5d2d4931b3e9ef7b9594bbc376a01"
SIZE = 3_386_892
ZIP_SHA256 = "75e5560da3cf503895a44c8ca7af17a83b4cca6c2cb5ba1883d2aec0d1cb5ac6"
STRING_PATTERN = "^[ \\r\\n\\t\\S]+$"
TEXT_PATTERN = "(?a)^[ \\r\\n\\t\\x0b\\x0c\\S]+$"
EXTENSIBLE_ENUMS = (("Expression", "language"),)  # R4 binds these as extensible
INT32 = (-(2**31), 2**31 - 1)
INTEGERS = {"integer": INT32[0], "positiveInt": 1, "unsignedInt": 0}  # name: minimum


def download() -> bytes:
    """`fhir.schema.json`, verified by its digest."""
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=120) as response:
            CACHE.write_bytes(response.read())
    archive = CACHE.read_bytes()
    zip_digest = hashlib.sha256(archive).hexdigest()
    if zip_digest != ZIP_SHA256:
        print(f"note: {CACHE.name} has sha256 {zip_digest} (re-zipped?); checking its content")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        data = zipped.read("fhir.schema.json")
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256 or len(data) != SIZE:
        sys.exit(f"fhir.schema.json: sha256 {digest} ({len(data)} bytes), expected {SHA256}")
    return data


def schemas(definitions: dict[str, Any]) -> list[dict[str, Any]]:
    """Every schema object: the definitions, their elements' schemas and those schemas' items."""
    found = []
    for definition in definitions.values():
        found.append(definition)
        for element in definition.get("properties", {}).values():
            found.append(element)
            if "items" in element:
                found.append(element["items"])
    return found


def type_objects(definitions: dict[str, Any]) -> int:
    complex_types = [d for d in definitions.values() if "properties" in d]
    for definition in complex_types:
        definition["type"] = "object"
    return len(complex_types)


def canonical_siblings(definitions: dict[str, Any]) -> tuple[int, int]:
    added = unrequired = 0
    element = {"$ref": "#/definitions/Element"}
    for definition in definitions.values():
        properties = definition.get("properties", {})
        for name, schema in list(properties.items()):
            if (schema.get("items") or schema).get("$ref") != "#/definitions/canonical":
                continue
            required = definition.get("required", [])
            if name in required:
                required.remove(name)
                unrequired += 1
                if not required:
                    definition.pop("required")
            if "_" + name not in properties:
                repeating = schema.get("type") == "array"
                properties["_" + name] = (
                    {"items": dict(element), "type": "array"} if repeating else dict(element)
                )
                added += 1
    return added, unrequired


def ascii_strings(definitions: dict[str, Any]) -> int:
    patched = [s for s in schemas(definitions) if s.get("pattern") == STRING_PATTERN]
    for schema in patched:
        schema["pattern"] = TEXT_PATTERN
    return len(patched)


def extensible_enums(definitions: dict[str, Any]) -> int:
    for definition, element in EXTENSIBLE_ENUMS:
        schema = definitions[definition]["properties"][element]
        (schema.get("items") or schema).pop("enum")
    return len(EXTENSIBLE_ENUMS)


def integers(definitions: dict[str, Any]) -> int:
    """The three integer types, by definition and wherever an element repeats their pattern
    inline (the `value[x]` of extensions and of ElementDefinition, for instance)."""
    by_pattern = {definitions[name]["pattern"]: minimum for name, minimum in INTEGERS.items()}
    patched = [s for s in schemas(definitions) if s.get("pattern") in by_pattern]
    for schema in patched:
        schema["type"] = "integer"
        schema["minimum"] = by_pattern[schema["pattern"]]
        schema["maximum"] = INT32[1]
    return len(patched)


def strip_descriptions(definitions: dict[str, Any]) -> None:
    for schema in schemas(definitions):
        schema.pop("description", None)


def build(data: bytes) -> dict[str, Any]:
    schema = json.loads(data)
    definitions: dict[str, Any] = schema["definitions"]
    resource_types = sorted(schema["discriminator"]["mapping"])
    patches = [
        f"type object on {type_objects(definitions)} complex types",
        "_x sibling (Element) for {} canonical elements; {} required ones made optional, as "
        "for every other primitive".format(*canonical_siblings(definitions)),
        f"(?a), and vertical tab and form feed allowed, on {ascii_strings(definitions)} string "
        "and markdown patterns (not code)",
        f"integer within 32 bits, minimum 1 (positiveInt) or 0 (unsignedInt), on "
        f"{integers(definitions)} integer, positiveInt and unsignedInt schemas",
        f"no enum on {extensible_enums(definitions)} extensible binding (Expression.language)",
        f"ResourceList: a choice by resourceType among {len(resource_types)} resource types, "
        "instead of oneOf",
        "description keyword removed",
    ]
    definitions["ResourceList"] = {"resourceChoice": "resourceType"}
    strip_descriptions(definitions)
    return {
        "definitions": definitions,
        "generated_by": "scripts/fhir_r4_schema.py",
        "resourceTypes": resource_types,
        "source": {
            "url": URL,
            "file": "fhir.schema.json",
            "sha256": SHA256,
            "zip_sha256": ZIP_SHA256,
            "patches": patches,
        },
    }


def main() -> None:
    out = build(download())
    text = json.dumps(out, sort_keys=True, separators=(",", ":"))
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(out['resourceTypes'])} resource types)")
    for patch in out["source"]["patches"]:
        print(f"  {patch}")


if __name__ == "__main__":
    main()

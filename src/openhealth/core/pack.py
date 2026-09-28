"""Loading a Decision Pack directory into a validated, content-addressed `Pack`."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from openhealth.core.spec import Outcome, PackManifest, Priority
from openhealth.errors import PackValidationError

MANIFEST = "pack.yaml"
# Attestations *about* a pack (eval reports, the rendered card) are excluded from its digest,
# otherwise publishing a benchmark would change the identity of the pack it describes.
_DIGEST_EXCLUDED_DIRS = {"evals"}
_DIGEST_EXCLUDED_FILES = {"DECISION_CARD.md"}


@dataclass(frozen=True)
class Pack:
    manifest: PackManifest
    root: Path
    digest: str
    input_schema: dict[str, Any]

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def path(self, relative: str) -> Path:
        """Resolve a pack-relative path, refusing anything that escapes the pack directory."""
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise PackValidationError(self.id, [f"path {relative!r} escapes the pack directory"])
        return resolved

    def read_yaml(self, relative: str) -> Any:
        with self.path(relative).open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def read_json(self, relative: str) -> Any:
        with self.path(relative).open(encoding="utf-8") as handle:
            return json.load(handle)

    @cached_property
    def output_schema(self) -> dict[str, Any]:
        """JSON Schema of the decision envelope, generated from the pack's questions."""
        m = self.manifest
        answers = {
            key: {
                "type": "object",
                "required": ["label", "confidence", "probabilities", "provider"],
                "properties": {
                    "label": {"enum": list(question.criteria)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "probabilities": {
                        "type": "object",
                        "properties": {
                            label: {"type": "number", "minimum": 0, "maximum": 1}
                            for label in question.criteria
                        },
                    },
                    "provider": {"type": "object"},
                },
            }
            for key, question in m.questions.items()
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://openhealth.dev/packs/{m.id}/{m.version}/output.schema.json",
            "title": f"{m.name} decision",
            "type": "object",
            "required": ["id", "pack", "outcome", "recommendation", "priority", "answers"],
            "properties": {
                "id": {"type": "string"},
                "pack": {"type": "object"},
                "outcome": {"enum": [outcome.value for outcome in Outcome]},
                "recommendation": {"enum": [*m.labels(m.policy.primary), None]},
                "priority": {"enum": [priority.value for priority in Priority]},
                "answers": {"type": "object", "properties": answers},
                "reasons": {"type": "array", "items": {"type": "string"}},
                "fallback": {"type": ["string", "null"]},
            },
        }


def load_pack(path: str | Path) -> Pack:
    root = Path(path).resolve()
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise PackValidationError(str(path), [f"{MANIFEST} not found in {root}"])
    with manifest_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise PackValidationError(str(path), [f"{MANIFEST} must be a mapping"])
    label = str(raw.get("id", path))

    # `policy: policy.yaml` keeps governance-relevant settings in their own reviewable file.
    if isinstance(raw.get("policy"), str):
        policy_path = _confined(root, raw["policy"], label)
        if not policy_path.is_file():
            raise PackValidationError(label, [f"policy file {raw['policy']!r} not found"])
        with policy_path.open(encoding="utf-8") as handle:
            raw["policy"] = yaml.safe_load(handle)

    try:
        manifest = PackManifest.model_validate(raw)
    except ValidationError as error:
        raise PackValidationError(label, _format_errors(error)) from None

    problems = [
        f"missing file {relative!r}"
        for relative in _referenced_files(manifest)
        if not _confined(root, relative, label).is_file()
    ]
    if problems:
        raise PackValidationError(manifest.id, problems)

    with _confined(root, manifest.inputs.json_schema, label).open(encoding="utf-8") as handle:
        input_schema = json.load(handle)
    try:
        Draft202012Validator.check_schema(input_schema)
    except SchemaError as error:
        raise PackValidationError(manifest.id, [f"input schema: {error.message}"]) from None

    return Pack(manifest=manifest, root=root, digest=pack_digest(root), input_schema=input_schema)


def pack_digest(root: Path) -> str:
    """sha256 over every file that defines the pack's behaviour, in a stable order."""
    digest = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = file.relative_to(root)
        if (
            relative.parts[0] in _DIGEST_EXCLUDED_DIRS
            or relative.name in _DIGEST_EXCLUDED_FILES
            or any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
        ):
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _referenced_files(m: PackManifest) -> list[str]:
    files = [m.inputs.json_schema, *m.evaluation.datasets.values()]
    if m.inputs.fhir:
        files.append(m.inputs.fhir)
    if m.tests:
        files.append(m.tests)
    for slot in m.providers.values():
        for variant in slot.variants.values():
            configs = [variant.model_extra or {}]
            members = configs[0].get("members")
            if isinstance(members, dict):
                configs.extend(member for member in members.values() if isinstance(member, dict))
            for config in configs:
                files.extend(config[key] for key in ("config", "model_file") if key in config)
    return files


def _confined(root: Path, relative: str, label: str) -> Path:
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise PackValidationError(label, [f"path {relative!r} escapes the pack directory"])
    return resolved


def _format_errors(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "pack"
        message = item["msg"].removeprefix("Value error, ")
        problems.append(f"{location}: {message}")
    return problems

"""Loading a Decision Pack directory into a validated, content-addressed `Pack`."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from mendari.core import input_schema as schema_rules
from mendari.core.spec import Outcome, PackManifest, Priority
from mendari.errors import PackValidationError

MANIFEST = "pack.yaml"
# Attestations *about* a pack (eval reports, the rendered card) are excluded from its digest,
# otherwise publishing a benchmark would change the identity of the pack it describes.
_DIGEST_EXCLUDED_DIRS = {"evals"}
_DIGEST_EXCLUDED_FILES = {"DECISION_CARD.md"}
# A YAML document's nodes, counting an alias each time it is used: a few lines of anchors and
# aliases ("a billion laughs") otherwise expand to a graph whose every walk is exponential. The
# shipped packs' largest file has under 2,000.
MAX_YAML_NODES = 1_000_000


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
        return load_yaml(self.path(relative), relative, self.id)

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
                    "value": {"type": "number"},
                },
            }
            for key, question in m.questions.items()
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://mendari.co/packs/{m.id}/{m.version}/output.schema.json",
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


def load_pack(path: str | Path, *, missing_models_ok: bool = False) -> Pack:
    """Load and validate a pack. `missing_models_ok` lets `mendari train` load a pack whose
    trained artifacts (`model_file`, `calibration`) do not exist yet."""
    root = Path(path).resolve()
    manifest_path = root / MANIFEST
    links = _outside_links(root)
    if links:
        raise PackValidationError(
            str(path), [f"{link}: a symbolic link that leads outside the pack" for link in links]
        )
    if not manifest_path.is_file():
        raise PackValidationError(str(path), [f"{MANIFEST} not found in {root}"])
    raw = load_yaml(manifest_path, MANIFEST, str(path))
    if not isinstance(raw, dict):
        raise PackValidationError(str(path), [f"{MANIFEST} must be a mapping"])
    label = str(raw.get("id", path))

    # `policy: policy.yaml` keeps governance-relevant settings in their own reviewable file.
    if isinstance(raw.get("policy"), str):
        policy_path = _confined(root, raw["policy"], label)
        if not policy_path.is_file():
            raise PackValidationError(label, [f"policy file {raw['policy']!r} not found"])
        raw["policy"] = load_yaml(policy_path, raw["policy"], label)

    try:
        manifest = PackManifest.model_validate(raw)
    except ValidationError as error:
        raise PackValidationError(label, _format_errors(error)) from None

    problems = [
        f"missing file {relative!r}"
        for relative in _referenced_files(manifest, models=not missing_models_ok)
        if not _confined(root, relative, label).is_file()
    ]
    if problems:
        raise PackValidationError(manifest.id, problems)

    schema_path = _confined(root, manifest.inputs.json_schema, label)
    try:
        with schema_path.open(encoding="utf-8") as handle:
            input_schema = json.load(handle)
    except (ValueError, RecursionError) as error:  # not JSON, not UTF-8, nested too deep
        problem = f"{manifest.inputs.json_schema}: not a JSON document ({type(error).__name__})"
        raise PackValidationError(manifest.id, [problem]) from None
    problems = schema_rules.problems(input_schema)  # before any jsonschema code reads it
    if problems:
        raise PackValidationError(manifest.id, problems)
    try:
        Draft202012Validator.check_schema(input_schema)
    except SchemaError as error:
        raise PackValidationError(manifest.id, [f"input schema: {error.message}"]) from None
    except (OverflowError, RecursionError, TypeError, ValueError) as error:
        problem = f"input schema: not a schema the validator can read ({type(error).__name__})"
        raise PackValidationError(manifest.id, [problem]) from None

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


def _referenced_files(m: PackManifest, *, models: bool = True) -> list[str]:
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
                keys = ("config", "model_file", "calibration") if models else ("config",)
                files.extend(config[key] for key in keys if isinstance(config.get(key), str))
    return files


def load_yaml(path: Path, name: str, label: str) -> Any:
    """A YAML file of a pack, parsed safely. One that does not parse, is not UTF-8, nests too
    deep for the parser, refers to itself or expands past `MAX_YAML_NODES` is the pack's
    problem (a `PackValidationError` naming `name`), never a raw error or a stall."""
    try:
        with path.open(encoding="utf-8") as handle:
            loader = yaml.SafeLoader(handle)
            try:
                node = loader.get_single_node()
                if node is None:
                    return None
                problem = _expansion_problem(node)
                if problem is not None:
                    raise PackValidationError(label, [f"{name}: {problem}"])
                return loader.construct_document(node)
            finally:
                loader.dispose()
    except yaml.YAMLError as error:
        raise PackValidationError(label, [f"{name}: invalid YAML: {error}"]) from None
    except (UnicodeDecodeError, RecursionError) as error:
        raise PackValidationError(
            label, [f"{name}: invalid YAML ({type(error).__name__})"]
        ) from None


def _expansion_problem(root: yaml.Node) -> str | None:
    """Why a composed YAML graph may not be constructed: an alias inside its own anchor, or
    more than `MAX_YAML_NODES` nodes once every alias is expanded. Each node is visited once
    (by identity), so this is linear in the file, however much the aliases would expand."""
    sizes: dict[int, int] = {}
    open_nodes: set[int] = set()
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:
        node, finished = stack.pop()
        key = id(node)
        children = _children(node)
        if finished:
            open_nodes.discard(key)
            sizes[key] = min(1 + sum(sizes[id(child)] for child in children), MAX_YAML_NODES + 1)
            continue
        if key in sizes:
            continue
        if key in open_nodes:
            return "an alias refers to the anchor it is inside"
        open_nodes.add(key)
        stack.append((node, True))
        stack.extend((child, False) for child in children if id(child) not in sizes)
    if sizes[id(root)] > MAX_YAML_NODES:
        return f"expands to more than {MAX_YAML_NODES:,} nodes (aliases counted each time used)"
    return None


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    if isinstance(node, yaml.MappingNode):
        return [part for pair in node.value for part in pair]
    return []


def _outside_links(root: Path) -> list[str]:
    """Every symbolic link in the pack's directory, hidden entries and caches included, that
    resolves outside `root`: whatever reads a pack's file, through `Pack.path` or not, then stays
    inside it. Links within the pack are allowed."""
    found = []
    for directory, subdirectories, files in os.walk(root):  # links to directories not followed
        for name in [*subdirectories, *files]:
            entry = Path(directory, name)
            if not entry.is_symlink():
                continue
            try:
                target: Path | None = entry.resolve(strict=True)
            except FileNotFoundError:  # dangling: where it would lead once the target exists
                target = entry.resolve()
            except (OSError, RuntimeError):  # a loop (RuntimeError before Python 3.13)
                target = None
            if target is None or not target.is_relative_to(root):
                found.append(entry.relative_to(root).as_posix())
    return sorted(found)


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

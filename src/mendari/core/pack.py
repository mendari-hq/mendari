"""Loading a Decision Pack directory into a validated, content-addressed `Pack`."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from mendari.core import input_schema as schema_rules
from mendari.core import patterns
from mendari.core.spec import Outcome, PackManifest, Priority
from mendari.errors import PackValidationError

MANIFEST = "pack.yaml"
# Attestations *about* a pack (eval reports, the rendered card) are excluded from its digest,
# otherwise publishing a benchmark would change the identity of the pack it describes.
_DIGEST_EXCLUDED_DIRS = {"evals"}
_DIGEST_EXCLUDED_FILES = {"DECISION_CARD.md"}
# A YAML document that uses anchors, with every alias counted each time it is used: a few lines
# of anchors and aliases ("a billion laughs") otherwise expand to a graph whose every walk is
# exponential, or, over one long scalar, to a document of gigabytes once anything serializes it
# (as JSON: a pack's examples and detail). A scalar's text is counted at its JSON-escaped length,
# so a control character (`"\0"` in YAML, `\u0000` in JSON) counts six. The shipped packs'
# largest file has under 2,000 nodes and 20,000 characters.
MAX_YAML_NODES = 1_000_000
MAX_YAML_CHARS = 32_000_000  # characters of scalar text, JSON-escaped
# A YAML number is refused before it is built when its text (underscores removed) is longer
# than this, or has more `:` parts than `MAX_YAML_NUMBER_PARTS` (`1:30:00`, base 60): PyYAML
# builds a long sexagesimal number in quadratic time, and Python's 4,300-digit refusal covers
# decimal integers only. An integer is also refused when it has more decimal digits than this
# (a hexadecimal one of 4,000 characters does), so every value can be printed.
MAX_YAML_NUMBER_CHARS = 4_300
MAX_YAML_NUMBER_PARTS = 4
_INT_LIMIT = 10**MAX_YAML_NUMBER_CHARS
# The files `pack_digest` hashes, by identity on disk: (st_dev, st_ino).
FileIds = frozenset[tuple[int, int]]


@dataclass(frozen=True)
class Pack:
    manifest: PackManifest
    root: Path
    digest: str
    input_schema: dict[str, Any]
    # Provider configs already read (and checked) at load, by pack-relative path: `read_yaml`
    # returns them rather than reading the file again.
    parsed: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    # The files `pack_digest` hashed at load, by identity on disk; None: computed when first
    # needed (a `Pack` built by hand).
    digested: FileIds | None = field(default=None, compare=False, repr=False)

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
        """A file of the pack, for what it decides or tests with: `relative` resolved inside the
        pack, and, if anything is there, one of the files `pack_digest` hashed, compared by the
        file on disk, so no spelling (a case variant on a case-insensitive filesystem, a link)
        reaches a file the digest leaves out. A path that escapes the pack, goes through a hidden
        entry or `__pycache__` or is an attestation is refused naming why; any other file
        outside the digest is refused too. A path with nothing there is returned (a file to
        write, or one a caller checks for)."""
        return _pack_file(self.root, relative, self.id, self._digested)

    def attestation(self, relative: str) -> Path:
        """A file *about* the pack, which its digest leaves out: in `evals/` (its benchmark and
        leaderboard) or a `DECISION_CARD.md`. Read by fixed names (and `evaluation.benchmark`),
        never as what the pack decides with."""
        return _attestation_file(self.root, relative, self.id)

    @cached_property
    def _digested(self) -> FileIds:
        return self.digested if self.digested is not None else _digest(self.root)[1]

    def read_yaml(self, relative: str) -> Any:
        if relative in self.parsed:
            return self.parsed[relative]
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
    links = _outside_links(root)
    if links:
        raise PackValidationError(
            str(path), [f"{link}: a symbolic link that leads outside the pack" for link in links]
        )
    # Hashed first: everything below is read through `_pack_file`, which accepts only a file the
    # digest covers, whatever spelling reaches it.
    digest, digested = _digest(root)
    manifest_path = _pack_file(root, MANIFEST, str(path), digested)
    if not manifest_path.is_file():
        raise PackValidationError(str(path), [f"{MANIFEST} not found in {root}"])
    raw = load_yaml(manifest_path, MANIFEST, str(path))
    if not isinstance(raw, dict):
        raise PackValidationError(str(path), [f"{MANIFEST} must be a mapping"])
    label = str(raw.get("id", path))

    # `policy: policy.yaml` keeps governance-relevant settings in their own reviewable file.
    if isinstance(raw.get("policy"), str):
        policy_path = _pack_file(root, raw["policy"], label, digested, "policy")
        if not policy_path.is_file():
            raise PackValidationError(label, [f"policy file {raw['policy']!r} not found"])
        raw["policy"] = load_yaml(policy_path, raw["policy"], label)

    try:
        manifest = PackManifest.model_validate(raw)
    except ValidationError as error:
        raise PackValidationError(label, _format_errors(error)) from None

    problems = [
        f"missing file {relative!r}"
        for where, relative in _referenced_files(manifest, models=not missing_models_ok)
        if not _pack_file(root, relative, label, digested, where).is_file()
    ]
    if problems:
        raise PackValidationError(manifest.id, problems)
    if manifest.evaluation.benchmark is not None:
        _attestation_file(root, manifest.evaluation.benchmark, label, "evaluation.benchmark")

    schema_path = _pack_file(root, manifest.inputs.json_schema, label, digested)
    try:
        with schema_path.open(encoding="utf-8") as handle:
            input_schema = json.load(handle)
    except (ValueError, RecursionError) as error:  # not JSON, not UTF-8, nested too deep
        problem = f"{manifest.inputs.json_schema}: not a JSON document ({type(error).__name__})"
        raise PackValidationError(manifest.id, [problem]) from None
    problems = schema_rules.problems(input_schema)  # before any jsonschema code reads it
    if problems:
        raise PackValidationError(manifest.id, problems)
    parsed = {
        relative: load_yaml(_pack_file(root, relative, label, digested), relative, manifest.id)
        for relative in _rules_files(manifest)
    }
    problems = _pattern_problems(input_schema, parsed)  # jsonschema compiles them too
    if problems:
        raise PackValidationError(manifest.id, problems)
    try:
        Draft202012Validator.check_schema(input_schema)
    except SchemaError as error:
        raise PackValidationError(manifest.id, [f"input schema: {error.message}"]) from None
    except (OverflowError, RecursionError, TypeError, ValueError) as error:
        problem = f"input schema: not a schema the validator can read ({type(error).__name__})"
        raise PackValidationError(manifest.id, [problem]) from None

    return Pack(
        manifest=manifest,
        root=root,
        digest=digest,
        input_schema=input_schema,
        parsed=parsed,
        digested=digested,
    )


def pack_digest(root: Path) -> str:
    """sha256 over every file that defines the pack's behaviour, in a stable order."""
    return _digest(root)[0]


def _digest(root: Path) -> tuple[str, FileIds]:
    """The pack digest, and the identity on disk of every file it hashed (of the file read, so
    a link inside the pack counts as the file it leads to)."""
    digest = hashlib.sha256()
    digested = set()
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = file.relative_to(root)
        if _attestation(relative.parts) or any(_excluded(part) for part in relative.parts):
            continue
        with file.open("rb") as handle:
            content = handle.read()
            status = os.fstat(handle.fileno())
        digested.add((status.st_dev, status.st_ino))
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest(), frozenset(digested)


# Manifest fields naming a file the pack decides or tests with, in a provider variant or a
# composite member's config. Every one is checked at load, naming the field; a file read under
# any other key goes through `Pack.path`, which accepts only a digested file as well.
_PROVIDER_FILE_KEYS = ("config", "library", "model_file", "calibration")
_TRAINED_FILE_KEYS = ("model_file", "calibration")  # may not exist yet (`missing_models_ok`)


def _referenced_files(m: PackManifest, *, models: bool = True) -> list[tuple[str, str]]:
    """Every file the manifest names that must be inside the digest, as (the manifest field
    naming it, its path). `evaluation.benchmark` is not one: it is an attestation."""
    files = [("inputs.schema", m.inputs.json_schema)]
    files += [(f"evaluation.datasets.{name}", path) for name, path in m.evaluation.datasets.items()]
    if m.inputs.fhir:
        files.append(("inputs.fhir", m.inputs.fhir))
    if m.tests:
        files.append(("tests", m.tests))
    keys = [key for key in _PROVIDER_FILE_KEYS if models or key not in _TRAINED_FILE_KEYS]
    for slot_name, slot in m.providers.items():
        for variant_name, variant in slot.variants.items():
            where = f"providers.{slot_name}.variants.{variant_name}"
            configs = [(where, variant.model_extra or {})]
            members = configs[0][1].get("members")
            if isinstance(members, dict):
                configs += [
                    (f"{where}.members.{name}", member)
                    for name, member in members.items()
                    if isinstance(member, dict)
                ]
            for at, config in configs:
                files += [
                    (f"{at}.{key}", config[key]) for key in keys if isinstance(config.get(key), str)
                ]
    return files


def _rules_files(m: PackManifest) -> list[str]:
    """The config file of every `rules` variant and composite member, once each."""
    files: set[str] = set()
    for slot in m.providers.values():
        for variant in slot.variants.values():
            config = {"type": variant.type, **(variant.model_extra or {})}
            members = config.get("members")
            configs = [config]
            if isinstance(members, dict):
                configs += [member for member in members.values() if isinstance(member, dict)]
            files.update(
                c["config"]
                for c in configs
                if c.get("type") == "rules" and isinstance(c.get("config"), str)
            )
    return sorted(files)


def _pattern_problems(input_schema: Any, rules: dict[str, Any]) -> list[str]:
    """Every pack-authored pattern compiled as it will run (`core/patterns.py`), after counting
    them, so a pack over the cap is refused before any is compiled."""
    found = [
        (f"input schema: {where}", text, 0)
        for where, text in patterns.schema_patterns(input_schema)
    ]
    for relative, data in rules.items():
        found += [
            (f"{relative}: {where}", text, patterns.RULES_FLAGS)
            for where, text in patterns.rules_patterns(data)
        ]
    if len(found) > patterns.MAX_PACK_PATTERNS:
        return [
            f"{len(found)} patterns; a pack holds at most {patterns.MAX_PACK_PATTERNS} patterns"
        ]
    problems = []
    for where, text, flags in found:
        problem = patterns.problem(text, flags)
        if problem is not None:
            problems.append(f"{where}: {problem}")
    return problems


class _NumberRefused(ValueError):
    """A YAML number `_PackLoader` will not build, and why."""

    def __init__(self, reason: str, node: yaml.Node):
        super().__init__(reason)
        self.reason = reason
        self.mark = node.start_mark


class _PackLoader(yaml.SafeLoader):
    """The safe loader, with every number (an int or a float, implicit or tagged, in any base)
    bounded before it is built: see `MAX_YAML_NUMBER_CHARS`."""

    def construct_yaml_int(self, node: yaml.ScalarNode) -> int:
        _check_number(node)
        value: int = super().construct_yaml_int(node)
        if abs(value) >= _INT_LIMIT:
            raise _NumberRefused(f"an integer of more than {MAX_YAML_NUMBER_CHARS:,} digits", node)
        return value

    def construct_yaml_float(self, node: yaml.ScalarNode) -> float:
        _check_number(node)
        value: float = super().construct_yaml_float(node)
        return value


_PackLoader.add_constructor("tag:yaml.org,2002:int", _PackLoader.construct_yaml_int)
_PackLoader.add_constructor("tag:yaml.org,2002:float", _PackLoader.construct_yaml_float)


def _check_number(node: yaml.Node) -> None:
    if not isinstance(node.value, str):
        return  # not a scalar: the safe constructor refuses it
    text = node.value.replace("_", "")
    if len(text) > MAX_YAML_NUMBER_CHARS:
        raise _NumberRefused(f"a number of more than {MAX_YAML_NUMBER_CHARS:,} characters", node)
    if text.count(":") >= MAX_YAML_NUMBER_PARTS:
        raise _NumberRefused(
            f"a number of more than {MAX_YAML_NUMBER_PARTS} ':' parts (base 60)", node
        )


def load_yaml(path: Path, name: str, label: str) -> Any:
    """A YAML file of a pack, parsed safely. One that does not parse, is not UTF-8, nests too
    deep for the parser, holds a value Python cannot build (a date such as 2024-02-30, an
    explicitly tagged scalar such as `!!bool maybe` or `!!float ''`) or a number over the
    bounds (`MAX_YAML_NUMBER_CHARS`, `MAX_YAML_NUMBER_PARTS`), refers to itself or, using
    anchors, expands past `MAX_YAML_NODES` or `MAX_YAML_CHARS` is the pack's problem (a
    `PackValidationError` naming `name`), never a raw error or a stall. A value the constructor
    cannot build is reported by its error's type and its line and column only: the error's text
    holds the value, which can be anything the pack wrote. A file that does not parse keeps the
    parser's message (what it expected, where, and the line around it)."""
    try:
        text = path.read_text(encoding="utf-8")
        loader = _PackLoader(text)
        try:
            node = loader.get_single_node()
            if node is None:
                return None
            if "&" in text:  # with no anchor there is no alias, and the graph is the file's tree
                problem = _expansion_problem(node)
                if problem is not None:
                    raise PackValidationError(label, [f"{name}: {problem}"])
            try:
                return loader.construct_document(node)
            except _NumberRefused as error:
                problem = f"{name}: {error.reason} ({_at(error.mark)})"
                raise PackValidationError(label, [problem]) from None
            except Exception as error:  # ConstructorError, or SafeConstructor's own, unwrapped
                mark = getattr(error, "problem_mark", None)
                at = f", {_at(mark)}" if isinstance(mark, yaml.Mark) else ""
                problem = f"{name}: invalid YAML value ({type(error).__name__}{at})"
                raise PackValidationError(label, [problem]) from None
        finally:
            loader.dispose()
    except (PackValidationError, OSError):  # ours, or the file cannot be read: not its content
        raise
    except yaml.YAMLError as error:  # the file does not parse: the parser's message
        raise PackValidationError(label, [f"{name}: invalid YAML: {error}"]) from None
    except Exception as error:  # not UTF-8, nested too deep for the parser, …
        raise PackValidationError(
            label, [f"{name}: invalid YAML ({type(error).__name__})"]
        ) from None


def _at(mark: yaml.Mark) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}"


def _expansion_problem(root: yaml.Node) -> str | None:
    """Why a composed YAML graph may not be constructed: an alias inside its own anchor, or more
    than `MAX_YAML_NODES` nodes or `MAX_YAML_CHARS` characters of scalars (JSON-escaped) once
    every alias is expanded. Each node is visited once (by identity), so this is linear in the
    file, however much the aliases would expand."""
    sizes: dict[int, tuple[int, int]] = {}  # (nodes, characters), each capped just past its limit
    open_nodes: set[int] = set()
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:
        node, finished = stack.pop()
        key = id(node)
        children = _children(node)
        if finished:
            open_nodes.discard(key)
            nodes = 1 + sum(sizes[id(child)][0] for child in children)
            chars = sum(sizes[id(child)][1] for child in children)
            if isinstance(node, yaml.ScalarNode):
                chars += _json_length(node.value)
            sizes[key] = (min(nodes, MAX_YAML_NODES + 1), min(chars, MAX_YAML_CHARS + 1))
            continue
        if key in sizes:
            continue
        if key in open_nodes:
            return "an alias refers to the anchor it is inside"
        open_nodes.add(key)
        stack.append((node, True))
        stack.extend((child, False) for child in children if id(child) not in sizes)
    nodes, chars = sizes[id(root)]
    if nodes > MAX_YAML_NODES:
        return f"expands to more than {MAX_YAML_NODES:,} nodes (aliases counted each time used)"
    if chars > MAX_YAML_CHARS:
        return (
            f"expands to more than {MAX_YAML_CHARS:,} characters of text (JSON-escaped; aliases "
            "counted each time used)"
        )
    return None


def _json_length(text: str) -> int:
    """The length of `text` in a JSON response (escaped, without its quotes)."""
    return len(json.dumps(text, ensure_ascii=False)) - 2


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    if isinstance(node, yaml.MappingNode):
        return [part for pair in node.value for part in pair]
    return []


def _outside_links(root: Path) -> list[str]:
    """Every symbolic link in the pack that resolves outside `root`: whatever reads a pack's
    file, through `Pack.path` or not, then stays inside it. Links within the pack are allowed.
    Hidden entries and caches are not part of a pack (`pack_digest` and the archive skip them,
    so a `.venv` beside the pack's files is fine); a path the manifest names through one is
    refused by `Pack.path` and `_confined`."""
    found = []
    for directory, subdirectories, files in os.walk(root):  # links to directories not followed
        subdirectories[:] = [name for name in subdirectories if not _excluded(name)]
        for name in [*subdirectories, *(name for name in files if not _excluded(name))]:
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


def _excluded(name: str) -> bool:
    """A hidden entry or a cache, which is not part of the pack."""
    return name.startswith(".") or name == "__pycache__"


def _confined(root: Path, relative: str, label: str, field: str | None = None) -> Path:
    """`relative` resolved inside `root`, or a `PackValidationError` naming `field` (the
    manifest field naming the path, if any): a path that escapes the pack, or that goes through
    a hidden entry or `__pycache__` as written or once links are resolved. Those are not part of
    the pack (`pack_digest` and the archive skip them, as an archive name with a hidden segment
    is refused). These checks read the path as written, for a clear message; what decides
    whether a file of the pack may be read is its identity on disk (`_pack_file`)."""
    where = f"{field}: " if field else ""
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise PackValidationError(label, [f"{where}path {relative!r} escapes the pack directory"])
    parts = resolved.relative_to(root).parts
    written = [part for part in Path(relative).parts if part not in (".", "..")]
    if any(_excluded(part) for part in [*written, *parts]):
        problem = (
            f"{where}path {relative!r} goes through a hidden entry or __pycache__, which is not "
            "part of the pack (nor its digest)"
        )
        raise PackValidationError(label, [problem])
    return resolved


def _pack_file(
    root: Path, relative: str, label: str, digested: FileIds, field: str | None = None
) -> Path:
    """A file the pack decides or tests with (`Pack.path`): `_confined`, not an attestation,
    and, if anything but a directory is there, one of the files `pack_digest` hashed
    (`digested`), compared by the file on disk. A spelling the digest does not hash (`EVALS/` or
    `__PYCACHE__/` on a case-insensitive filesystem, a link to a left-out file) is refused
    however it is written, so whatever a pack decides with is inside its digest."""
    resolved = _confined(root, relative, label, field)
    where = f"{field}: " if field else ""
    if _attestation(resolved.relative_to(root).parts):
        problem = (
            f"{where}path {relative!r} is in evals/ or a DECISION_CARD.md, attestations about the "
            "pack that are not part of its digest"
        )
        raise PackValidationError(label, [problem])
    try:
        status = os.stat(resolved)
    except (FileNotFoundError, NotADirectoryError):
        return resolved  # nothing there to read: a file to write, or one a caller checks for
    if stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) in digested:
        return resolved
    problem = (
        f"{where}path {relative!r} is not one of the files the pack's digest covers (it leaves "
        "out hidden entries, __pycache__, evals/ and DECISION_CARD.md, however a path spells them)"
    )
    raise PackValidationError(label, [problem])


def _attestation_file(root: Path, relative: str, label: str, field: str | None = None) -> Path:
    """A file about the pack (`Pack.attestation`): `_confined`, and in `evals/` or a
    `DECISION_CARD.md`, which the digest leaves out."""
    resolved = _confined(root, relative, label, field)
    if not _attestation(resolved.relative_to(root).parts):
        where = f"{field}: " if field else ""
        problem = (
            f"{where}path {relative!r} is not in evals/ or a DECISION_CARD.md: an attestation "
            "about the pack is kept outside its digest"
        )
        raise PackValidationError(label, [problem])
    return resolved


def _attestation(parts: tuple[str, ...]) -> bool:
    """A path (its parts, relative to the pack) that `pack_digest` leaves out as an attestation
    about the pack rather than part of it."""
    return bool(parts) and (
        parts[0] in _DIGEST_EXCLUDED_DIRS or parts[-1] in _DIGEST_EXCLUDED_FILES
    )


def _format_errors(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "pack"
        message = item["msg"].removeprefix("Value error, ")
        problems.append(f"{location}: {message}")
    return problems

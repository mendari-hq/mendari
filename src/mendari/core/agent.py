# SPDX-License-Identifier: Apache-2.0
"""Loading an agent directory into a validated, content-addressed `Agent`."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Any, NoReturn

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from mendari.core import input_schema as schema_rules
from mendari.core import patterns
from mendari.core.origin import (
    NOT_OWNED,
    ORIGIN_LABELS,
    RESERVED_NAMESPACES,
    Origin,
    pulled_record,
)
from mendari.core.origin import bounded as origin_bounded
from mendari.core.spec import (
    LICENSED_CODE_SYSTEMS,
    LICENSED_HINTS,
    AgentManifest,
    Effect,
    Outcome,
    Priority,
    card_summary_problems,
    licensed_systems,
    prefetch_problems,
    version_key,
)
from mendari.core.templates import template_problems
from mendari.errors import AgentValidationError

MANIFEST = "agent.yaml"
# Attestations *about* an agent (eval reports, the rendered card) are excluded from its digest,
# otherwise publishing a benchmark would change the identity of the agent it describes.
_DIGEST_EXCLUDED_DIRS = {"evals"}
_DIGEST_EXCLUDED_FILES = {"DECISION_CARD.md"}
# A YAML document that uses anchors, with every alias counted each time it is used: a few lines
# of anchors and aliases ("a billion laughs") otherwise expand to a graph whose every walk is
# exponential, or, over one long scalar, to a document of gigabytes once anything serializes it
# (as JSON: an agent's examples and detail). A scalar's text is counted at its JSON-escaped length,
# so a control character (`"\0"` in YAML, `\u0000` in JSON) counts six. The shipped agents'
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
# The files `agent_digest` hashed: the identity on disk (st_dev, st_ino) of each, with the SHA-256
# of the bytes it hashed. A file is read only if it is one of them, and its bytes only if they
# still have that hash (`Agent.read_bytes`).
Digested = Mapping[tuple[int, int], str]


@dataclass(frozen=True)
class Agent:
    manifest: AgentManifest
    root: Path
    digest: str
    input_schema: dict[str, Any]
    # Provider configs already read (and checked) at load, by agent-relative path: `read_yaml`
    # returns them rather than reading the file again.
    parsed: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    # The files `agent_digest` hashed at load, by identity on disk, with their content's hash;
    # None: computed when first needed (an `Agent` built by hand).
    digested: Digested | None = field(default=None, compare=False, repr=False)
    # Where the agent was found (`core/origin.py`), never what its manifest says.
    origin: Origin = field(default="local", compare=False)
    # What `load_agent` found wrong with an owned agent's prefetch and card templates
    # (`manifest_template_problems`) and provider configs (`providers.config_problems`): it
    # loads, and says so (`mendari list`, the startup log,
    # /api/health's `agent_warnings`); `mendari validate` refuses it. An agent the deployment does
    # not own is refused at load instead, so it has none.
    warnings: tuple[str, ...] = field(default=(), compare=False)
    # For a version an organization's agent store keeps that was pulled from a registry
    # (`core/agent_store.py`, source `hub`): where it came from, as the database holds it. None
    # for every other agent; one in the registry cache reads the marker beside it (`pulled`).
    origin_record: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def named_settings_only(self) -> bool:
        """An agent the deployment's unscoped provider settings and default variants never reach:
        only a setting that names it chooses its variants: an agent from a store (LP-20e), whose
        id is its organization's."""
        return self.origin == "store"

    @property
    def bounded(self) -> bool:
        """An agent the deployment does not own, on the bounded engine: RE2, the caps, the
        input limits and a CPU budget per decision (`core/origin.py` `bounded`, ADR-0009). Read
        from where the agent was found and the digest `load_agent` hashed, never its manifest."""
        return origin_bounded(self.origin, self.digest)

    @property
    def pulled(self) -> dict[str, Any]:
        """For an agent from a registry, what its verified marker records of where it came from
        (`core/origin.py` `pulled_record`), or, for one an organization's agent store keeps,
        the record the database holds (`origin_record`); for any other agent, nothing."""
        if self.origin != "hub":
            return {}
        if self.origin_record is not None:
            return dict(self.origin_record)
        return pulled_record(self.root)

    @property
    def origin_label(self) -> str:
        """What every surface says of where the agent comes from (`ORIGIN_LABELS`)."""
        return ORIGIN_LABELS[self.origin]

    @property
    def status_label(self) -> str:
        """What every surface says of the agent's status (`core/status.py`)."""
        from mendari.core.status import status_label

        return status_label(self.manifest)

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def path(self, relative: str) -> Path:
        """A file of the agent, for what it decides or tests with: `relative` resolved inside the
        agent, and, if anything is there, one of the files `agent_digest` hashed, compared by the
        file on disk, so no spelling (a case variant on a case-insensitive filesystem, a link)
        reaches a file the digest leaves out. A path that escapes the agent, goes through a hidden
        entry or `__pycache__` or is an attestation is refused naming why; any other file
        outside the digest is refused too. A path with nothing there is returned (a file to
        write, or one a caller checks for). Its content is read with `read_bytes` (or
        `read_text`, `read_yaml`, `read_json`), never through this path."""
        return _agent_file(self.root, relative, self.id, self._digested)

    def read_bytes(self, relative: str) -> bytes:
        """A file of the agent (`path`), as the bytes its digest covers: a file missing or
        unreadable, or whose content changed since the digest was computed (edited in place),
        is an `AgentValidationError` naming it, never read. So what an agent decides with is what
        its digest, and every decision recording it, describes."""
        return _read(self.root, relative, self.id, self._digested)

    def read_text(self, relative: str) -> str:
        """`read_bytes` as UTF-8 text, line endings read as `Path.read_text` reads them; a file
        that is not UTF-8 is an `AgentValidationError` naming it."""
        return _text(self.read_bytes(relative), relative, self.id)

    def attestation(self, relative: str) -> Path:
        """A file *about* the agent, which its digest leaves out: in `evals/` (its benchmark and
        leaderboard) or a `DECISION_CARD.md`. Read by fixed names (and `evaluation.benchmark`),
        never as what the agent decides with."""
        return _attestation_file(self.root, relative, self.id)

    def attestation_json(self, relative: str) -> tuple[dict[str, Any] | None, str | None]:
        """A JSON object the agent carries about itself (`attestation`), or None and why: none
        there (no reason), unreadable, not JSON, holding a value a JSON response cannot carry
        (`parse_json`) or not an object. Like the registry, Mendari treats an attestation it
        cannot read as absent, never as an error: it is not part of what the agent decides with."""
        path = self.attestation(relative)
        if not path.is_file():
            return None, None
        try:
            value = parse_json(path.read_bytes(), relative, self.id)
        except OSError as error:
            return None, f"cannot be read ({type(error).__name__})"
        except AgentValidationError as error:
            return None, error.problems[0].removeprefix(f"{relative}: ")
        if not isinstance(value, dict):
            return None, "not a JSON object"
        return value, None

    @cached_property
    def _digested(self) -> Digested:
        return self.digested if self.digested is not None else _digest(self.root, self.id)[1]

    def read_yaml(self, relative: str) -> Any:
        if relative in self.parsed:
            return self.parsed[relative]
        return parse_yaml(self.read_bytes(relative), relative, self.id)

    def read_json(self, relative: str) -> Any:
        """A JSON file of the agent (`parse_json`): one that is not JSON, or holds a value a JSON
        response cannot carry, is an `AgentValidationError` naming it."""
        return parse_json(self.read_bytes(relative), relative, self.id)

    @cached_property
    def output_schema(self) -> dict[str, Any]:
        """JSON Schema of the decision envelope, generated from the agent's questions."""
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
        # What the agent proposed to have done, each with what may be done with it (spec §13.7):
        # only for an agent that declares actions, so every other agent's schema is as it was.
        proposed = {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "name", "effect", "disposition", "reason"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "effect": {"enum": [*(effect.value for effect in Effect), None]},
                        "disposition": {"enum": ["released", "held", "blocked"]},
                        "reason": {"type": "string"},
                        "arguments": {"type": "object"},
                        "arguments_hash": {"type": "string"},
                        "problems": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        }
        # Where each text the agent wrote stands (spec §15.8), and the texts themselves, which
        # an answer carries only with a decision that waits for a person: only for an agent
        # that declares outputs, so every other agent's schema is as it was.
        texts = {
            "written": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {
                        "type": "object",
                        "required": ["state"],
                        "properties": {"state": {"enum": ["held", "not_released"]}},
                    }
                    for name in m.outputs
                },
            },
            "outputs": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "string", "maxLength": output.max_chars}
                    for name, output in m.outputs.items()
                },
            },
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://mendari.co/agents/{m.id}/{m.version}/output.schema.json",
            "title": f"{m.name} decision",
            "type": "object",
            "required": ["id", "agent", "outcome", "recommendation", "priority", "answers"],
            "properties": {
                "id": {"type": "string"},
                "agent": {"type": "object"},
                "outcome": {"enum": [outcome.value for outcome in Outcome]},
                "recommendation": {"enum": [*m.labels(m.policy.primary), None]},
                "priority": {"enum": [priority.value for priority in Priority]},
                "answers": {"type": "object", "properties": answers},
                "reasons": {"type": "array", "items": {"type": "string"}},
                "fallback": {"type": ["string", "null"]},
                **(proposed if m.actions else {}),
                **(texts if m.outputs else {}),
            },
        }


def manifest_name(where: Path | Iterable[str], label: str = "") -> str | None:
    """`MANIFEST` when it is among an agent's files, None when it is not. `where` is the agent's
    folder, or its files' paths relative to it (an archive's, a stored version's).

    A folder holds one manifest: two spellings that differ only in case (`Agent.yaml` beside
    `agent.yaml`), which a filesystem that ignores case reads as one file and one that does not
    as two, are an `AgentValidationError` (labelled `label`)."""
    if isinstance(where, Path):
        try:
            listed = os.listdir(where)
        except OSError:  # not a folder, or one that cannot be read: no manifest here
            listed = []
        spelled = [name for name in listed if name.casefold() == MANIFEST]
        spelled = [name for name in spelled if (where / name).is_file()]
        present = (where / MANIFEST).is_file()
    else:
        files = [path for path in where if "/" not in path]
        spelled = [name for name in files if name.casefold() == MANIFEST]
        present = MANIFEST in files
    if len(spelled) > 1:
        both = " and ".join(sorted(spelled))  # as spelled there
        raise AgentValidationError(
            label or str(where),
            [f"{both} are both here: an agent has one manifest, {MANIFEST}; remove the other"],
        )
    return MANIFEST if present else None


def has_manifest(folder: Path) -> bool:
    """Whether `folder` holds a manifest. A folder that holds two spellings of its name has one
    too: `load_agent` is what refuses it, saying why."""
    return (folder / MANIFEST).is_file()


def manifests_under(base: Path) -> list[Path]:
    """Every manifest in `base` or below it, sorted by path: one walk of its folders, links to
    folders not followed, the name looked for as `Path.glob` looks for a file's (by whether it
    is there, so a filesystem that ignores case finds any spelling)."""
    return sorted(
        Path(folder, MANIFEST) for folder, _, _ in os.walk(base) if Path(folder, MANIFEST).exists()
    )


def load_agent(path: str | Path, *, origin: Origin = "local") -> Agent:
    """Load and validate an agent. `origin` is where it was found (`core/registry.py` knows).
    An agent the deployment does not own (`Agent.bounded`: one from a registry or a store that
    is not a bundled agent of this release) is checked as it will run: its patterns on RE2, its
    schema's keywords and its caps; and one from a registry never has a bundled agent's id and
    version with other files (`_shadow_problem`). Every other agent's input schema is held to the
    expansion bounds (`core/input_schema.py` `expansion_problems`).

    Every provider variant and composite member is checked against its provider's closed config
    and the agent's files (`providers.config_problems`, without the environment or the network),
    and its CDS Hooks prefetch and card templates are held to relative reads and searches and
    bare placeholders (`manifest_template_problems`): an agent the deployment does not own is
    refused for any problem, and its FHIR mapping and Task text are checked too; an owned one
    loads, with the problems as its `warnings`, and a variant with one fails when it is
    built."""
    root = Path(path).resolve()
    links = _outside_links(root)
    if links:
        raise AgentValidationError(
            str(path), [f"{link}: a symbolic link that leads outside the agent" for link in links]
        )
    # Under one spelling (`manifest_name`).
    if manifest_name(root, str(path)) is None:
        raise AgentValidationError(str(path), [f"{MANIFEST} not found in {root}"])
    # Hashed first: everything below is read through `_read`, which accepts only a file the
    # digest covers, whatever spelling reaches it, and only the bytes the digest hashed.
    digest, digested, named = _digest(root, str(path))
    if not _agent_file(root, MANIFEST, str(path), digested).is_file():
        raise AgentValidationError(str(path), [f"{MANIFEST} not found in {root}"])
    raw = parse_yaml(_read(root, MANIFEST, str(path), digested), MANIFEST, str(path))
    if not isinstance(raw, dict):
        raise AgentValidationError(str(path), [f"{MANIFEST} must be a mapping"])
    label = str(raw.get("id", path))

    try:
        manifest = AgentManifest.model_validate(raw)
    except ValidationError as error:
        raise AgentValidationError(label, _format_errors(error)) from None
    if (problem := _shadow_problem(manifest, origin, digest, root)) is not None:
        raise AgentValidationError(manifest.id, [problem])
    # How the agent is held, and the engine its patterns run on: from where it was found and the
    # digest hashed above, never from its manifest (`core/origin.py` `bounded`, ADR-0009).
    bounded = origin_bounded(origin, digest)
    # Its prefetch and card templates: an agent the deployment does not own is refused for a
    # problem, an owned one warned (with its config problems, below).
    held = manifest_template_problems(manifest)
    if bounded and held:
        raise AgentValidationError(manifest.id, held)
    declared = {
        url
        for use in manifest.code_systems
        if use.licence_note is not None
        for url in licensed_systems(use.url)
    }
    problems = [
        f"{where} names {LICENSED_CODE_SYSTEMS[url][0]} ({url}), whose codes are licensed: "
        "declare it in code_systems with a licence_note"
        for url, where in sorted(named.items())
        if url not in declared
    ]
    if problems:
        raise AgentValidationError(manifest.id, problems)

    problems = [
        f"missing file {relative!r}"
        for where, relative in _referenced_files(manifest)
        if not _agent_file(root, relative, label, digested, where).is_file()
    ]
    if problems:
        raise AgentValidationError(manifest.id, problems)
    if manifest.evaluation.benchmark is not None:
        _benchmark_file(root, manifest.evaluation.benchmark, label)

    schema = manifest.inputs.json_schema
    input_schema = parse_json(_read(root, schema, label, digested), schema, manifest.id)
    problems = schema_rules.problems(input_schema)  # before any jsonschema code reads it
    if problems:
        raise AgentValidationError(manifest.id, problems)
    parsed = {
        relative: parse_yaml(_read(root, relative, label, digested), relative, manifest.id)
        for relative in _rules_files(manifest)
    }
    # Compiled as they run (on the engine `bounded` says), before jsonschema compiles the
    # schema's too.
    refused = _pattern_problems(input_schema, parsed, bounded=bounded)
    problems = list(refused)
    if bounded:
        problems += schema_rules.bounded_problems(input_schema)
    if refused:
        namespace = manifest.id.partition("/")[0]
        if bounded and namespace in RESERVED_NAMESPACES:
            problems.append(
                f"{manifest.id}@{manifest.version} is not the copy of it in this release, so it "
                "runs on the engine for agents from a registry, which cannot run the patterns "
                "above: use the copy this release bundles, or a version published for this "
                "release"
            )
        raise PatternsRefused(manifest.id, problems, refused)
    if problems:
        raise AgentValidationError(manifest.id, problems)
    try:
        Draft202012Validator.check_schema(input_schema)
    except SchemaError as error:
        raise AgentValidationError(manifest.id, [f"input schema: {error.message}"]) from None
    except (OverflowError, RecursionError, TypeError, ValueError) as error:
        problem = f"input schema: not a schema the validator can read ({type(error).__name__})"
        raise AgentValidationError(manifest.id, [problem]) from None
    if bounded:
        problems = _bounded_caps(manifest, input_schema, parsed)
        if problems:
            raise AgentValidationError(manifest.id, problems)
    # An agent the deployment owns has no time budget: its schema's shape is bounded instead (one
    # on the bounded engine keeps the caps above and the budget, ADR-0009).
    elif problems := schema_rules.expansion_problems(input_schema):
        raise AgentValidationError(manifest.id, problems)

    agent = Agent(
        manifest=manifest,
        root=root,
        digest=digest,
        input_schema=input_schema,
        parsed=parsed,
        digested=digested,
        origin=origin,
    )
    # Every variant against its provider's closed config, as `mendari validate` checks it.
    from mendari.providers import config_problems  # `providers/base.py` imports this module

    problems = config_problems(agent)
    if manifest.rubrics:
        # Each rubric's canned judge, as `mendari validate` checks it (spec section 15.4).
        from mendari.providers.judge import rubric_problems  # imports this module

        problems += rubric_problems(agent)
    if bounded:
        if problems:
            raise AgentValidationError(manifest.id, problems)
        if manifest.inputs.fhir is not None:
            # Its FHIR mapping, and its Task text held to bare placeholders, as `mendari
            # validate` reads them: refused here, before anything renders it.
            from mendari.standards.fhir import FhirAdapter  # imports this module

            FhirAdapter.for_agent(agent)
        return agent
    warnings = held + problems
    return replace(agent, warnings=tuple(warnings)) if warnings else agent


def manifest_template_problems(manifest: AgentManifest) -> list[str]:
    """What an agent's manifest templates are held to beyond their shape (LP-16e): each CDS Hooks
    prefetch template is a read or search relative to the EHR's FHIR server
    (`spec.prefetch_problems`), and each card summary uses bare placeholders
    (`templates.template_problems`; their names are checked with the manifest) and stays under
    a card's 140 characters however its placeholders are filled (`spec.card_summary_problems`,
    LP-23f). `load_agent` refuses an agent the deployment does not own for any and warns an owned
    one; `mendari validate` refuses both. The FHIR Task text is the mapping's
    (`standards/fhir.py`)."""
    hooks = manifest.cds_hooks
    problems = prefetch_problems(hooks)
    if hooks is not None and hooks.card is not None:
        problems += [
            f"cds_hooks.card.{where} {problem}"
            for where, summary in hooks.card.summary_templates().items()
            for problem in template_problems(summary, manifest.questions)
        ]
    return problems + card_summary_problems(manifest)


def _shadow_problem(manifest: AgentManifest, origin: Origin, digest: str, root: Path) -> str | None:
    """Why an agent of an origin the deployment does not own (`NOT_OWNED`: a registry's, LP-20's
    store) may not load: it has the id of a bundled agent of this release and a version that
    ties that agent's (`version_key`: `0.4.1-x` and `0.04.1` tie `0.4.1`), but other files (its
    digest is no bundled agent's). It would take the bundled agent's place: the search path
    serves the first of two tying versions, and the registry cache comes before the bundled
    agents. None for every other agent: an operator's local copy (its choice), a copy of a
    bundled agent, and any other version."""
    if origin not in NOT_OWNED:
        return None
    from mendari.core.reference import reference_agents, reference_digests  # imports this module

    if digest in reference_digests():
        return None
    wanted = version_key(manifest.version)
    tying = [
        reference
        for reference in reference_agents().get(manifest.id, ())
        if version_key(reference.version) == wanted
    ]
    if not tying:
        return None
    bundled, short = f"{manifest.id}@{tying[0].version}", digest[: len("sha256:") + 16]
    return (
        f"{manifest.id}@{manifest.version} has the id and version of an agent this "
        f"release bundles ({bundled}) but other files (digest {short}): an agent from a registry "
        "never takes a bundled agent's place. Use the copy this release bundles, or publish it "
        f"under another version{_cached(root)}"
    )


def _cached(root: Path) -> str:
    """What a reason adds for an agent in the registry cache, where `mendari pull` puts it
    (`<namespace>/<name>/<version>`) and pulled it before this release refused it: the directory
    to remove. Nothing for any other directory (an archive being checked, a pull's staging)."""
    from mendari.hub.client import agents_cache  # `hub/client.py` imports this module

    try:
        cache = agents_cache().resolve()
    except (OSError, RuntimeError):  # no home directory to find the cache in
        return ""
    cached = root.is_relative_to(cache) and len(root.relative_to(cache).parts) == 3
    return f"; remove {root} from the cache" if cached else ""


def _bounded_caps(manifest: AgentManifest, input_schema: Any, parsed: dict[str, Any]) -> list[str]:
    """What multiplies the pattern work of an agent on the bounded engine (`Agent.bounded`) past
    its caps (`core/caps.py`): its rules configs, its input schema, and the patterns they run
    together. The caps are imported only for such an agent."""
    from mendari.core import caps

    problems = [
        problem
        for relative, data in parsed.items()
        for problem in caps.rules_problems(relative, data)
    ]
    runs = caps.rules_runs(manifest, parsed)
    size = caps.rules_runs(manifest, parsed, caps.rules_size)
    return problems + caps.schema_problems(input_schema, runs, size)


def agent_digest(root: Path) -> str:
    """sha256 over every file that defines the agent's behaviour, in a stable order."""
    return _digest(root)[0]


def _digest(root: Path, label: str = "") -> tuple[str, Digested, dict[str, str]]:
    """The agent digest; the identity on disk of every file it hashed (of the file read, so a link
    inside the agent counts as the file it leads to) with the SHA-256 of what it read; and each
    licensed code system (`LICENSED_CODE_SYSTEMS`) a hashed file names, with the first file
    naming it. A file that cannot be read is an `AgentValidationError` naming it (labelled
    `label`)."""
    digest = hashlib.sha256()
    digested = {}
    named: dict[str, str] = {}
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = file.relative_to(root)
        if _attestation(relative.parts) or any(_excluded(part) for part in relative.parts):
            continue
        try:
            with file.open("rb") as handle:
                content = handle.read()
                status = os.fstat(handle.fileno())
        except OSError as error:  # unreadable (its mode), or gone since it was listed
            problem = f"{relative.as_posix()}: cannot be read ({type(error).__name__})"
            raise AgentValidationError(label or str(root), [problem]) from None
        digested[(status.st_dev, status.st_ino)] = hashlib.sha256(content).hexdigest()
        lower_content = content.lower()
        if any(hint in lower_content for hint in LICENSED_HINTS):
            for url in licensed_systems(content.decode("utf-8", "replace")):
                named.setdefault(url, relative.as_posix())
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest(), digested, named


# Manifest fields naming a file the agent decides or tests with, in a provider variant or a
# composite member's config. Every one is checked at load, naming the field; a file read under
# any other key goes through `Agent.path`, which accepts only a digested file as well.
_PROVIDER_FILE_KEYS = ("config", "calibration", "mock_file")


def _referenced_files(m: AgentManifest) -> list[tuple[str, str]]:
    """Every file the manifest names that must be inside the digest, as (the manifest field
    naming it, its path). `evaluation.benchmark` is not one: it is an attestation."""
    files = [("inputs.schema", m.inputs.json_schema)]
    files += [(f"evaluation.datasets.{name}", path) for name, path in m.evaluation.datasets.items()]
    if m.inputs.fhir:
        files.append(("inputs.fhir", m.inputs.fhir))
    if m.tests:
        files.append(("tests", m.tests))
    files += [
        (f"rubrics.{name}.judge.mock_file", rubric.judge.mock_file)
        for name, rubric in m.rubrics.items()
        if rubric.judge.mock_file is not None
    ]
    files += [
        (f"evaluation.grades.{name}.file", grades.file)
        for name, grades in m.evaluation.grades.items()
    ]
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
                    (f"{at}.{key}", config[key])
                    for key in _PROVIDER_FILE_KEYS
                    if isinstance(config.get(key), str)
                ]
    return files


def _rules_files(m: AgentManifest) -> list[str]:
    """The config file of every `rules` variant and composite member, once each."""
    return _provider_files(m, "rules")


def _provider_files(m: AgentManifest, kind: str) -> list[str]:
    """The config file of every variant and composite member of type `kind`, once each."""
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
                if c.get("type") == kind and isinstance(c.get("config"), str)
            )
    return sorted(files)


def authored_patterns(agent: Agent) -> list[tuple[str, str, int]]:
    """(where, pattern, flags) for every pattern the agent's author wrote, as it runs: its input
    schema's and its rules files'."""
    return _authored_patterns(agent.input_schema, agent.parsed)


def _authored_patterns(input_schema: Any, rules: dict[str, Any]) -> list[tuple[str, str, int]]:
    found = [
        (f"input schema: {where}", text, 0)
        for where, text in patterns.schema_patterns(input_schema)
    ]
    for relative, data in rules.items():
        found += [
            (f"{relative}: {where}", text, patterns.RULES_FLAGS)
            for where, text in patterns.rules_patterns(data)
        ]
    return found


def _pattern_problems(
    input_schema: Any, rules: dict[str, Any], *, bounded: bool = False
) -> list[str]:
    """Every agent-authored pattern compiled as it will run (`core/patterns.py`: on RE2 for an
    agent on the bounded engine, `bounded`), after counting them, so an agent over the cap is
    refused before any is compiled."""
    found = _authored_patterns(input_schema, rules)
    if len(found) > patterns.MAX_AGENT_PATTERNS:
        return [
            f"{len(found)} patterns; an agent holds at most {patterns.MAX_AGENT_PATTERNS} patterns"
        ]
    problems = []
    for where, text, flags in found:
        problem = patterns.problem(text, flags, bounded=bounded)
        if problem is not None:
            problems.append(f"{where}: {problem}")
    return problems


class PatternsRefused(AgentValidationError):
    """An agent refused at load with its own patterns among the reasons: `patterns` are those of
    `problems` that `_pattern_problems` found (too many, too long, or not compiling as they run),
    so a caller can tell them from the agent's other problems."""

    def __init__(self, agent: str, problems: list[str], patterns: list[str]):
        super().__init__(agent, problems)
        self.patterns = patterns


class _Refused(ValueError):
    """A YAML value `_ManifestLoader` will not build: why, and where (the text after the file's
    name in the problem). Never the value: it is whatever the agent wrote."""


class _ManifestLoader(yaml.SafeLoader):
    """The safe loader, building only what a JSON response can carry, with every value it
    cannot build named by its place:

    - every number (an int or a float, implicit or tagged, in any base) bounded before it is
      built: see `MAX_YAML_NUMBER_CHARS`;
    - a string with a lone surrogate (`"\\ud800"`, which no UTF-8 text holds) refused, and the
      `!!binary`, `!!set`, `!!omap` and `!!pairs` values (bytes, a set, lists of pairs) too;
    - a value the safe constructor raises on (a date such as 2024-02-30, `!!bool maybe`) named
      by the error's type and the value's line and column;
    - a mapping that holds one key twice refused, by both places: the safe loader keeps the
      last, so a reader of the first would be misled about what the agent decides with. A key
      merged in with `<<` may still be set again by the mapping itself, as YAML intends."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: dict[Any, yaml.Mark] = {}
            for key_node, _ in node.value:
                if key_node.tag == f"{_YAML_TAG}merge":
                    continue
                key = self.construct_object(key_node, deep=True)
                try:
                    first = seen.setdefault(key, key_node.start_mark)
                except TypeError:
                    continue  # unhashable: the safe constructor refuses it
                if first is not key_node.start_mark:
                    at = f"{_at(key_node.start_mark)}; first at {_at(first)}"
                    raise _Refused(f"a key given twice ({at})")
        mapping: dict[Any, Any] = super().construct_mapping(node, deep=deep)
        return mapping

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except (_Refused, RecursionError):
            raise
        except yaml.constructor.ConstructorError as error:
            mark = error.problem_mark or node.start_mark
            raise _Refused(f"invalid YAML value (ConstructorError, {_at(mark)})") from None
        except Exception as error:  # SafeConstructor's own, unwrapped: ValueError, KeyError, …
            kind = type(error).__name__
            raise _Refused(f"invalid YAML value ({kind}, {_at(node.start_mark)})") from None

    def construct_yaml_int(self, node: yaml.ScalarNode) -> int:
        _check_number(node)
        value: int = super().construct_yaml_int(node)
        if abs(value) >= _INT_LIMIT:
            _refuse(f"an integer of more than {MAX_YAML_NUMBER_CHARS:,} digits", node)
        return value

    def construct_yaml_float(self, node: yaml.ScalarNode) -> float:
        _check_number(node)
        value: float = super().construct_yaml_float(node)
        return value

    def construct_yaml_str(self, node: yaml.ScalarNode) -> str:
        value: str = super().construct_yaml_str(node)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _refuse("a string with a lone surrogate, which is not UTF-8 text", node)
        return value

    def construct_not_json(self, node: yaml.Node) -> Any:
        _refuse(f"a {node.tag.replace(_YAML_TAG, '!!')} value, which JSON cannot hold", node)


_YAML_TAG = "tag:yaml.org,2002:"
_ManifestLoader.add_constructor(f"{_YAML_TAG}int", _ManifestLoader.construct_yaml_int)
_ManifestLoader.add_constructor(f"{_YAML_TAG}float", _ManifestLoader.construct_yaml_float)
_ManifestLoader.add_constructor(f"{_YAML_TAG}str", _ManifestLoader.construct_yaml_str)
for _tag in ("binary", "set", "omap", "pairs"):
    _ManifestLoader.add_constructor(f"{_YAML_TAG}{_tag}", _ManifestLoader.construct_not_json)


def _refuse(reason: str, node: yaml.Node) -> NoReturn:
    raise _Refused(f"{reason} ({_at(node.start_mark)})")


def _check_number(node: yaml.Node) -> None:
    if not isinstance(node.value, str):
        return  # not a scalar: the safe constructor refuses it
    text = node.value.replace("_", "")
    if len(text) > MAX_YAML_NUMBER_CHARS:
        _refuse(f"a number of more than {MAX_YAML_NUMBER_CHARS:,} characters", node)
    if text.count(":") >= MAX_YAML_NUMBER_PARTS:
        _refuse(f"a number of more than {MAX_YAML_NUMBER_PARTS} ':' parts (base 60)", node)


def parse_yaml(data: bytes, name: str, label: str) -> Any:
    """A YAML file of an agent, parsed safely. One that does not parse, is not UTF-8, nests too
    deep for the parser, holds a value JSON cannot (a string with a lone surrogate, `!!binary`,
    `!!set`, `!!omap`, `!!pairs`) or Python cannot build (a date such as 2024-02-30, an
    explicitly tagged scalar such as `!!bool maybe` or `!!float ''`) or a number over the
    bounds (`MAX_YAML_NUMBER_CHARS`, `MAX_YAML_NUMBER_PARTS`), refers to itself or, using
    anchors, expands past `MAX_YAML_NODES` or `MAX_YAML_CHARS` is the agent's problem (an
    `AgentValidationError` naming `name`), never a raw error or a stall. A value that is refused
    or cannot be built is named by why (or its error's type) and its line and column only: the
    value can be anything the agent wrote. A file that does not parse keeps the parser's message
    (what it expected, where, and the line around it)."""
    try:
        loader = _ManifestLoader(_decoded(data))
        try:
            node = loader.get_single_node()
            if node is None:
                return None
            if b"&" in data:  # with no anchor there is no alias, and the graph is the file's tree
                problem = _expansion_problem(node)
                if problem is not None:
                    raise AgentValidationError(label, [f"{name}: {problem}"])
            try:
                return loader.construct_document(node)
            except _Refused as error:
                raise AgentValidationError(label, [f"{name}: {error}"]) from None
            except Exception as error:  # a ConstructorError outside any one value (a key), …
                mark = getattr(error, "problem_mark", None)
                at = f", {_at(mark)}" if isinstance(mark, yaml.Mark) else ""
                problem = f"{name}: invalid YAML value ({type(error).__name__}{at})"
                raise AgentValidationError(label, [problem]) from None
        finally:
            loader.dispose()
    except AgentValidationError:
        raise
    except yaml.YAMLError as error:  # the file does not parse: the parser's message
        raise AgentValidationError(label, [f"{name}: invalid YAML: {error}"]) from None
    except Exception as error:  # not UTF-8, nested too deep for the parser, …
        raise AgentValidationError(
            label, [f"{name}: invalid YAML ({type(error).__name__})"]
        ) from None


def parse_json(data: bytes, name: str, label: str) -> Any:
    """A JSON file of an agent. One that is not UTF-8 or not JSON, holds an integer over Python's
    4,300 digits or nests too deep for the parser, or holds a value a JSON response cannot carry
    (a string with a lone surrogate: `json` reads the escape `"\\ud800"`; NaN or Infinity) is
    the agent's problem (an `AgentValidationError` naming `name` and the error's type or what it
    holds), never a raw error."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as error:  # UnicodeDecodeError is a ValueError
        problem = f"{name}: not a JSON document ({type(error).__name__})"
        raise AgentValidationError(label, [problem]) from None
    what = not_json(value) or _beyond_floats(value)
    if what is not None:
        raise AgentValidationError(label, [f"{name}: not JSON, it holds {what}"])
    return value


_FLOAT_LIMIT = 2**1024  # the smallest integer float() refuses (OverflowError)


def _beyond_floats(value: Any) -> str | None:
    """An integer too large to be a float, which every figure an agent's JSON holds (benchmark,
    leaderboard, model, calibration) is read as somewhere: refused here once, for every reader."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, int) and not isinstance(item, bool) and abs(item) >= _FLOAT_LIMIT:
            return "an integer too large to be a number"
    return None


def not_json(value: Any) -> str | None:
    """What in `value` a JSON response cannot carry (encoded as UTF-8, numbers finite), named
    by its type, or None. YAML builds dates (and a JSON file can hold strings with a lone
    surrogate, `"\\ud800"`, and NaN), none of which an input, an example or a decision can
    hold. The text is never part of the answer: it is whatever the agent wrote."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except RecursionError:
        return "values nested too deep"
    except (TypeError, ValueError):  # UnicodeEncodeError is a ValueError
        pass
    else:
        return None
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item)  # the keys
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return "a string with a lone surrogate"
        elif isinstance(item, float) and not math.isfinite(item):
            return "a number that is not finite"
        elif not (item is None or isinstance(item, bool | int | float)):
            return f"a value of type {type(item).__name__}"
    return "a value JSON cannot hold"


def _decoded(data: bytes) -> str:
    """UTF-8 `data` as text, line endings translated as `Path.read_text` translates them."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _text(data: bytes, name: str, label: str) -> str:
    try:
        return _decoded(data)
    except UnicodeDecodeError:
        raise AgentValidationError(label, [f"{name}: not UTF-8 text"]) from None


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
    """Every symbolic link in the agent that resolves outside `root`: whatever reads an agent's
    file, through `Agent.path` or not, then stays inside it. Links within the agent are allowed.
    Hidden entries and caches are not part of an agent (`agent_digest` and the archive skip them,
    so a `.venv` beside the agent's files is fine); a path the manifest names through one is
    refused by `Agent.path` and `_confined`."""
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
    """A hidden entry or a cache, which is not part of the agent."""
    return name.startswith(".") or name == "__pycache__"


_SHOWN_PATH_CHARS = 80  # how much of a refused path a problem shows


def _shown_path(relative: str) -> str:
    """A path the manifest names, quoted, at most `_SHOWN_PATH_CHARS` of it."""
    if len(relative) <= _SHOWN_PATH_CHARS:
        return repr(relative)
    return repr(relative[:_SHOWN_PATH_CHARS]) + f"... ({len(relative):,} characters)"


def _confined(root: Path, relative: str, label: str, field: str | None = None) -> Path:
    """`relative` resolved inside `root`, or an `AgentValidationError` naming `field` (the
    manifest field naming the path, if any): a path that escapes the agent, or that goes through
    a hidden entry or `__pycache__` as written or once links are resolved. Those are not part of
    the agent (`agent_digest` and the archive skip them, as an archive name with a hidden segment
    is refused). These checks read the path as written, for a clear message; what decides
    whether a file of the agent may be read is its identity on disk (`_agent_file`)."""
    where = f"{field}: " if field else ""
    try:
        resolved = (root / relative).resolve()
    except (OSError, RuntimeError, ValueError) as error:  # a NUL in it, a link loop, …
        problem = f"{where}path {_shown_path(relative)} is not a path a file can have"
        raise AgentValidationError(label, [f"{problem} ({type(error).__name__})"]) from None
    if not resolved.is_relative_to(root):
        raise AgentValidationError(label, [f"{where}path {relative!r} escapes the agent's folder"])
    parts = resolved.relative_to(root).parts
    written = [part for part in Path(relative).parts if part not in (".", "..")]
    if any(_excluded(part) for part in [*written, *parts]):
        problem = (
            f"{where}path {relative!r} goes through a hidden entry or __pycache__, which is not "
            "part of the agent (nor its digest)"
        )
        raise AgentValidationError(label, [problem])
    return resolved


def _agent_file(
    root: Path, relative: str, label: str, digested: Digested, field: str | None = None
) -> Path:
    """A file the agent decides or tests with (`Agent.path`): `_confined`, not an attestation,
    and, if anything but a directory is there, one of the files `agent_digest` hashed
    (`digested`), compared by the file on disk. A spelling the digest does not hash (`EVALS/` or
    `__PYCACHE__/` on a case-insensitive filesystem, a link to a left-out file) is refused
    however it is written, so whatever an agent decides with is inside its digest."""
    resolved = _confined(root, relative, label, field)
    where = f"{field}: " if field else ""
    if _attestation(resolved.relative_to(root).parts):
        problem = (
            f"{where}path {relative!r} is in evals/ or a DECISION_CARD.md, attestations about the "
            "agent that are not part of its digest"
        )
        raise AgentValidationError(label, [problem])
    try:
        status = os.stat(resolved)
    except (FileNotFoundError, NotADirectoryError):
        return resolved  # nothing there to read: a file to write, or one a caller checks for
    except OSError as error:  # a name too long for the filesystem, a folder it may not read, …
        why = error.strerror or type(error).__name__  # the system's words, never the path
        problem = f"{where}path {_shown_path(relative)} cannot be read ({why})"
        raise AgentValidationError(label, [problem]) from None
    if stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) in digested:
        return resolved
    problem = (
        f"{where}path {relative!r} is not one of the files the agent's digest covers (it leaves "
        "out hidden entries, __pycache__, evals/ and DECISION_CARD.md, however a path spells them)"
    )
    raise AgentValidationError(label, [problem])


def _read(
    root: Path, relative: str, label: str, digested: Digested, field: str | None = None
) -> bytes:
    """The content of a file the agent decides or tests with (`_agent_file`), read once and
    checked against the hash `agent_digest` took of the same file: a file edited in place since
    (the same file on disk, other bytes) is refused, so what is used is what the digest covers.
    A file missing or unreadable is an `AgentValidationError` naming it."""
    path = _agent_file(root, relative, label, digested, field)
    where = f"{field}: " if field else ""
    try:
        with path.open("rb") as handle:
            content = handle.read()
            status = os.fstat(handle.fileno())
    except FileNotFoundError:
        raise AgentValidationError(label, [f"{where}missing file {relative!r}"]) from None
    except OSError as error:  # a directory, unreadable (its mode), …
        problem = f"{where}{relative}: cannot be read ({type(error).__name__})"
        raise AgentValidationError(label, [problem]) from None
    expected = digested.get((status.st_dev, status.st_ino))
    if expected is None or hashlib.sha256(content).hexdigest() != expected:
        problem = (
            f"{where}{relative}: changed since the agent's digest was computed (edited or replaced "
            "after the agent was loaded); load the agent again"
        )
        raise AgentValidationError(label, [problem])
    return content


def _benchmark_file(root: Path, relative: str, label: str) -> Path:
    """`evaluation.benchmark`: an attestation in `evals/` (not any `DECISION_CARD.md`)."""
    resolved = _attestation_file(root, relative, label, "evaluation.benchmark")
    if resolved.relative_to(root).parts[0] not in _DIGEST_EXCLUDED_DIRS:
        problem = f"evaluation.benchmark: path {relative!r} is not in evals/"
        raise AgentValidationError(label, [problem])
    return resolved


def _attestation_file(root: Path, relative: str, label: str, field: str | None = None) -> Path:
    """A file about the agent (`Agent.attestation`): `_confined`, and in `evals/` or a
    `DECISION_CARD.md`, which the digest leaves out."""
    resolved = _confined(root, relative, label, field)
    if not _attestation(resolved.relative_to(root).parts):
        where = f"{field}: " if field else ""
        problem = (
            f"{where}path {relative!r} is not in evals/ or a DECISION_CARD.md: an attestation "
            "about the agent is kept outside its digest"
        )
        raise AgentValidationError(label, [problem])
    return resolved


def _attestation(parts: tuple[str, ...]) -> bool:
    """A path (its parts, relative to the agent) that `agent_digest` leaves out as an attestation
    about the agent rather than part of it."""
    return bool(parts) and (
        parts[0] in _DIGEST_EXCLUDED_DIRS or parts[-1] in _DIGEST_EXCLUDED_FILES
    )


def _format_errors(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "agent"
        message = item["msg"].removeprefix("Value error, ")
        problems.append(f"{location}: {message}")
    return problems

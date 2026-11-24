# SPDX-License-Identifier: Apache-2.0
"""One vocabulary (tasks/todo.md, "Plan: the engineering to A+"): what Mendari governs is an
agent, and "pack" or "Decision Pack" is never written where a person reads or types.

**One rule** (`offences`): no whole word "pack" or "packs", in any letter case, and no "Decision
Pack" however it is joined. It is applied to every surface in `SURFACES`, each a name and a
function that yields `(where, text)` pairs. A later step holds its own surface to the rule (the
docs, the README, a front end) by adding one entry to that list.

**What the rule leaves alone**, each said here with its reason and nowhere else:

- `WIRE_NAMES`: names on the wire and in stored records (a JSON key, a query parameter, a FHIR
  operation's parameter). Decisions, audit events, registry records and signatures hold them, so
  renaming them is its own versioned change (the plan's wave 6). A wire name may be a
  parameter's or a property's name in the API description, and prose may name it as code
  (`` `pack` ``) or as a JSON key (`"pack":`).
- `KEPT_NAMES`: identifiers that hold the old word and are not words: canonical URLs that stored
  FHIR resources carry.
- `KEPT_SCHEMAS`: Python classes whose names an API description shows as its schemas' names
  (none now: the one there was is listed under a title, `server/api_docs.py` `titled`).
- `ALLOWED`: the few texts that name something from before on purpose.
- `NOT_READ`: files of the product this test does not read, each with who does.

`src/mendari/cli.py` has its own test: the command line's help and messages."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

import pytest

from mendari.cli import main
from mendari.core.card import render_card
from mendari.core.origin import Origin
from mendari.core.pack import load_pack
from mendari.hub.registry import create_registry_app
from mendari.server.app import create_app
from tests.conftest import DEMO_AGENT_DIR, ROOT, encrypt_mode, encrypted_store, evalset_on

# -- the rule -------------------------------------------------------------------------------------

_WORD = re.compile(r"\bpacks?\b", re.IGNORECASE)
# "Decision Pack" however it is joined: DecisionPack, decision_pack, decision-packs.
_JOINED = re.compile(r"decision[\s_-]*packs?(?![a-z])", re.IGNORECASE)
# Names of requests, answers and stored records: never renamed here (module docstring). The
# first eight are the plan's; `packs` is a key of the registry's and the engine's answers (a
# namespace's ids, a count in health).
WIRE_NAMES = frozenset(
    {
        "pack",
        "pack_id",
        "pack_version",
        "pack_digest",
        "pack_name",
        "pack_author",
        "pack_unavailable",
        "pack_warnings",
        "packs",
    }
)
# How prose names a wire name without breaking the rule: as code (`` `pack` ``), as a JSON key
# (`"pack":`), or as the field a message is about, at its start (`pack.digest: it is not ...`,
# `report.pack: ...`, the form of every "field: what is wrong with it" message).
# The same holds for the name of an event the registry's chain records (`` `pack.yanked` ``): the
# chain holds those names, as it holds its payloads' keys.
_AS_CODE = re.compile(
    "|".join(rf'`{name}`|"{name}"(?=\s*:)' for name in sorted(WIRE_NAMES, key=len, reverse=True))
    + r"|^(?:\w+\.)*packs?(?:\.\w+)*:(?=\s)"
    + r"|`pack\.[a-z_]+`"
)
# Identifiers, not words: canonical URLs that decisions' stored FHIR resources carry (a Task's
# tag, an AuditEvent's agent, a coding's system) and other systems hold.
KEPT_NAMES = {
    "CodeSystem/decision-pack": "the CodeSystem of agent versions, by its canonical URL",
    "NamingSystem/decision-pack": "the identifier system of agent ids, by its canonical URL",
    "mendari.co/packs": "the canonical URLs of an agent's FHIR terminology, schema and library",
}
_KEPT = re.compile("|".join(re.escape(name) for name in KEPT_NAMES))


def offences(text: str) -> list[str]:
    """The words in `text` that break the rule, as written: "pack" or "packs" as a whole word
    (so not `pack_id`, `unpack` or `package`), or "Decision Pack" joined any way. A wire name
    written as code, and a kept name, are taken out first."""
    cleaned = _KEPT.sub(" ", _AS_CODE.sub(" ", text))
    return [found.group(0) for pattern in (_WORD, _JOINED) for found in pattern.finditer(cleaned)]


def old_name(name: str) -> bool:
    """Whether a name (a parameter, a property, an operation id) has "pack" or "packs" as one
    of its parts, however the parts are joined: `pack_id`, `packId`, `store-packs`."""
    parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)
    return any(part.lower() in ("pack", "packs") for part in parts)


def test_the_rule() -> None:
    for said in (
        "a pack",
        "Packs served here",
        "the Decision Pack",
        "DecisionPacks",
        "decision_pack",
        "(pack)",
        "pack.yaml",
        "/api/packs/x",
        "mendari-pack.json",
        "--pack",
        "the `packs` and a pack",
        "community-packs/<name>/",
        "tests/fixtures/reference-packs/",
    ):
        assert offences(said), said
    for said in (
        "an agent",
        "pack_id",
        "the `pack_digest` of a decision",
        "name it as `pack`",
        '{"pack": {"id": "a/b"}, "packs": 3}',
        "unpacked",
        "a package",
        "PackValidationError",
        "resolve_pack",
        "MENDARI_PACKS_PATH",
        "community-agents/<name>/",
        "tests/fixtures/reference-agents/",
        "https://mendari.co/fhir/CodeSystem/decision-pack|{namespace}/{name}@{version}",
        "under its own url, https://mendari.co/packs/community/<name>/",
        "Chained `pack.yanked` with your id",
    ):
        assert not offences(said), said
    assert offences("`pack`s") == [] and offences("`pack` of packs") == ["packs"]
    assert offences('"pack" is not a key here') == ["pack"]  # quoted prose is prose
    # The field a message is about, at its start; never the word in the middle of a sentence.
    assert not offences("pack: it is not this agent and version")
    assert not offences("pack.digest: it is not this version's digest")
    assert not offences("report.pack: it is not the evaluation's agent")
    assert offences("the pack: it is not this agent") == ["pack"]
    assert offences("pack it up") == ["pack"]
    assert all(old_name(name) for name in ("pack", "pack_id", "packId", "list_packs_api_get"))
    assert not any(old_name(name) for name in ("agent_id", "package", "unpack", "backpacks"))


# -- surfaces -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Surface:
    """Something a person reads or types: its name, and its texts as `(where, text)`."""

    name: str
    texts: Callable[[Path, pytest.MonkeyPatch], Iterable[tuple[str, str]]]


# Texts that say the old word on purpose: (surface, the whole text) -> why. An entry that no
# surface holds any more fails the test, so the list never outlives its reasons.
ALLOWED: dict[tuple[str, str], str] = {}


# The API descriptions --------------------------------------------------------------------------

_DATA = ("example", "default", "enum", "const")  # values, not words about them
_PROSE = ("title", "summary", "description")


def api_prose(document: Any, where: str = "") -> Iterator[tuple[str, str]]:
    """Every title, summary and description of an OpenAPI document, wherever it is, with every
    tag and every path; never an example's value, a default or an enumerated value (data)."""
    if isinstance(document, dict):
        for key, value in document.items():
            at = f"{where}/{key}"
            if key in _DATA:
                continue
            if key == "examples" and isinstance(value, dict):
                for name, example in value.items():
                    if isinstance(example, dict):
                        for said in ("summary", "description"):
                            if isinstance(example.get(said), str):
                                yield f"{at}/{name}/{said}", example[said]
                continue
            if key == "paths" and isinstance(value, dict):
                yield from ((f"{at}: a path", path) for path in value)
            if key == "tags" and isinstance(value, list):
                yield from ((at, tag) for tag in value if isinstance(tag, str))
            if key in _PROSE and isinstance(value, str):
                yield at, value
            yield from api_prose(value, at)
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from api_prose(value, f"{where}/{index}")


_AN_OPERATION, _A_SCHEMA = "an operation id", "a schema's name"


def api_names(document: Any, where: str = "") -> Iterator[tuple[str, str]]:
    """Every name of an OpenAPI document: those a caller types (each parameter's, each
    property's and those a schema requires), and those its reader sees (each operation's id
    and each schema's name)."""
    if isinstance(document, dict):
        for key, value in document.items():
            at = f"{where}/{key}"
            if key in _DATA or key == "examples":
                continue
            if at == "/components/schemas" and isinstance(value, dict):
                yield from ((f"{at}: {_A_SCHEMA}", name) for name in value)
            if key == "properties" and isinstance(value, dict):
                yield from ((at, name) for name in value)
            if key == "required" and isinstance(value, list):
                yield from ((at, name) for name in value if isinstance(name, str))
            if key == "parameters" and isinstance(value, list):
                yield from (
                    (at, parameter["name"])
                    for parameter in value
                    if isinstance(parameter, dict) and isinstance(parameter.get("name"), str)
                )
            if key == "operationId" and isinstance(value, str):
                yield f"{at}: {_AN_OPERATION}", value
            yield from api_names(value, at)
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from api_names(value, f"{where}/{index}")


def _open_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A release as it starts: open mode, serving the bundled agent."""
    return create_app(db_path=tmp_path / "open.db", packs_paths=[ROOT / "agents"]).openapi()


def _whole_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The engine with everything it can mount, so every route and every sentence that only
    some settings add is in the document: sign-in in memberships mode, Backend Services, the
    SMART launch, the registry under it with its reviewers, the agent store, hidden evaluation
    sets, and the rate limits on."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.test")
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", "registry-review")
    monkeypatch.setenv("MENDARI_AGENT_STORE", "on")
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on")
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.example.org")
    monkeypatch.setenv("MENDARI_SMART_EHRS", "https://ehr.example.org/fhir=default:decide")
    evalset_on(monkeypatch)
    with encrypted_store(tmp_path / "whole.db") as before:
        before.create_organization(org="registry-review", name="Registry review", created_by="t")
    app = create_app(db_path=tmp_path / "whole.db", packs_paths=[ROOT / "agents"])
    try:
        document = app.openapi()
    finally:
        app.state.registry_store.close()
    paths = set(document["paths"])
    for mounted in ("/api/store/agents", "/api/registry/review", "/registry/v1/search"):
        assert mounted in paths, f"{mounted} is not mounted: the document is not the whole one"
    assert "/api/evalsets/{set_id}/runs" in paths and "/api/smart/decide" in paths
    assert "securitySchemes" in document["components"]  # sign-in is on
    return document


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    return create_registry_app(tmp_path / "registry.db").openapi()


API_DOCUMENTS: dict[str, Callable[[Path, pytest.MonkeyPatch], Any]] = {
    "the engine's API description, as a release starts": _open_engine,
    "the engine's API description, everything on": _whole_engine,
    "the registry's API description": _registry,
}


def _api_surface(name: str) -> Surface:
    def texts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterable[tuple[str, str]]:
        return api_prose(API_DOCUMENTS[name](tmp_path, monkeypatch))

    return Surface(name, texts)


# The Python product's own words ----------------------------------------------------------------

PRODUCT = ROOT / "src" / "mendari"
# Files this surface does not read, each with who does or when it goes.
NOT_READ = {
    PRODUCT / "cli.py": "the command line: its help and messages have a test of their own",
}
_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_LABEL = re.compile(r"\bPacks?\b")  # the word with a capital: a heading or a label, however short


def prose_literals(source: str) -> Iterator[tuple[int, str]]:
    """`(line, text)` of every string in `source` that is prose: a literal that holds
    whitespace (an identifier, a key, a path or a pattern holds none) or the old word with a
    capital (a one-word label, `"Pack"`); an f-string taken whole, with `{}` where it
    interpolates. Never a docstring (one that the API description shows is read there, in its
    place), nor a name `__all__` exports."""
    tree = ast.parse(source)
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCUMENTED) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skipped.add(id(first.value))
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            skipped.update(id(name) for name in ast.walk(node.value))
    found: list[tuple[int, str]] = []
    for joined in (node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)):
        parts = [
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
            for part in joined.values
        ]
        skipped.update(id(part) for part in joined.values)  # read with the f-string, not alone
        found.append((joined.lineno, "".join(parts)))
    for constant in (node for node in ast.walk(tree) if isinstance(node, ast.Constant)):
        if isinstance(constant.value, str) and id(constant) not in skipped:
            found.append((constant.lineno, constant.value))
    return iter(
        sorted(
            (line, text) for line, text in found if re.search(r"\s", text) or _LABEL.search(text)
        )
    )


def _product_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    assert all(path.is_file() and why for path, why in NOT_READ.items())  # none outlives its file
    for path in sorted(PRODUCT.rglob("*.py")):
        if path in NOT_READ:
            continue
        for line, text in prose_literals(path.read_text(encoding="utf-8")):
            yield f"{path.relative_to(ROOT)}:{line}", text


def test_prose_literals_are_found_and_docstrings_and_names_are_not() -> None:
    source = '''
"""A module's docstring about a pack."""
__all__ = ["Pack", "load_pack"]
NAME = "pack"
KEY = "pack_id"
PATH = "/api/packs/{namespace}"
HEADING = "Pack"

def said(count: int, kind: str) -> str:
    """A docstring about packs."""
    if count:
        return f"{count} {kind}pack{'s' if count != 1 else ''}"
    return "no pack " "found here"

class Thing:
    """Another pack."""
    label = f"pack:{NAME}"
'''
    assert list(prose_literals(source)) == [
        (7, "Pack"),  # one word with a capital: a label, not a key (and not `__all__`'s name)
        (12, "{} {}pack{}"),  # an f-string, whole: prose, though no one part holds a space
        (13, "no pack found here"),  # adjacent literals are one
    ]


# What `mendari new` writes, and the bundled agent ----------------------------------------------

_TEXT = {".yaml", ".yml", ".json", ".jsonl", ".md", ".txt", ".py", ".toml", ""}


def _files(folder: Path, shown: str) -> Iterator[tuple[str, str]]:
    """Each file's path (a name is typed too), then its text."""
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        relative = path.relative_to(folder).as_posix()
        yield f"{shown}: a file's name", relative
        assert path.suffix in _TEXT, f"{shown}/{relative}: not a text file this test reads"
        yield f"{shown}/{relative}", path.read_text(encoding="utf-8")


def _scaffolded(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Each kind of agent `mendari new` scaffolds: the command as typed, and the folder made."""
    made = {
        "mendari new": (tmp_path / "plain" / "refills", ["--id", "my-clinic/refills"]),
        "mendari new --agent": (tmp_path / "agent" / "refills", ["--agent", "--id", "a/refills"]),
        "mendari new --community": (tmp_path / "community-agents" / "refills", ["--community"]),
    }
    for shown, (target, flags) in made.items():
        assert main(["new", str(target), *flags, "--publisher", "My Clinic"]) == 0, shown
        yield shown, target


def _scaffolds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    for shown, target in _scaffolded(tmp_path):
        yield from _files(target, shown)


def _bundled() -> list[Path]:
    bundled = sorted(path for path in (ROOT / "agents").iterdir() if path.is_dir())
    assert bundled == [DEMO_AGENT_DIR]  # the one agent a release bundles
    return bundled


def _bundled_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    for agent in _bundled():
        yield from _files(agent, f"agents/{agent.name}")


def _decision_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    """The Decision Card as `mendari card` renders it: the bundled agent's and each scaffolded
    agent's, from every place an agent comes from (each origin has sentences of its own)."""
    agents = [(f"agents/{agent.name}", agent) for agent in _bundled()]
    for shown, folder in [*agents, *_scaffolded(tmp_path)]:
        for origin in get_args(Origin):
            card = render_card(load_pack(folder, origin=origin))
            assert card.startswith("# Decision Card"), f"{shown} as {origin}"
            yield f"{shown}: its Decision Card, as {origin}", card


def _package_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    """What the package index shows: the project's description and keywords."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    yield "pyproject.toml: description", project["description"]
    yield from (("pyproject.toml: keywords", keyword) for keyword in project["keywords"])
    import mendari

    yield "mendari: the package's docstring", mendari.__doc__ or ""


SURFACES = [
    *(_api_surface(name) for name in API_DOCUMENTS),
    Surface("the Python product's messages", _product_words),
    Surface("what `mendari new` scaffolds", _scaffolds),
    Surface("the bundled agent's files", _bundled_agent),
    Surface("a Decision Card, as rendered", _decision_cards),
    Surface("the package's metadata", _package_metadata),
]


@pytest.mark.parametrize("surface", SURFACES, ids=lambda surface: surface.name)
def test_a_surface_says_agent(
    surface: Surface, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    texts = list(surface.texts(tmp_path, monkeypatch))
    assert texts, f"{surface.name}: nothing to read"
    found = [
        f"{where}: {', '.join(sorted(set(offences(text))))!r} in {text[:160]!r}"
        for where, text in texts
        if offences(text) and (surface.name, text) not in ALLOWED
    ]
    assert not found, f"{len(found)} in {surface.name}:\n" + "\n".join(found)


def test_every_allowed_text_is_still_there_and_still_needs_allowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    by_surface = {surface.name: surface for surface in SURFACES}
    for (name, text), reason in ALLOWED.items():
        assert reason.strip(), (name, text)
        held = [said for _, said in by_surface[name].texts(tmp_path / name[:8], monkeypatch)]
        assert text in held, f"{name}: no longer says {text[:80]!r}: take it off the list"
        assert offences(text), f"{name}: {text[:80]!r} breaks no rule: take it off the list"
    assert all(reason.strip() for reason in KEPT_NAMES.values())


# A schema's name in an API description is its Python class's, and classes are not renamed here
# (module docstring). A class that holds the old word sets a title, and the description lists it
# under that (`server/api_docs.py` `titled`), so none needs keeping: an entry here would be a
# class shown under its own name, with why it is shown at all.
KEPT_SCHEMAS: dict[str, str] = {}


@pytest.mark.parametrize("document", API_DOCUMENTS)
def test_an_api_description_names_only_wire_names(
    document: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parameter or a property may be called by a wire name and by no other name with the old
    word in it; an operation's id has none (it is made from the route's name and path), and a
    schema's name has none outside `KEPT_SCHEMAS`."""
    names = list(api_names(API_DOCUMENTS[document](tmp_path, monkeypatch)))
    assert names

    def kept(where: str, name: str) -> bool:
        if where.endswith(_A_SCHEMA):
            return name in KEPT_SCHEMAS
        return name in WIRE_NAMES and not where.endswith(_AN_OPERATION)

    found = sorted(
        {f"{where}: {name}" for where, name in names if old_name(name) and not kept(where, name)}
    )
    assert not found, "\n".join(found)


def test_every_kept_schema_is_still_shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The list never outlives its reasons: each kept schema is in the whole API description."""
    whole = API_DOCUMENTS["the engine's API description, everything on"](tmp_path, monkeypatch)
    shown = {name for where, name in api_names(whole) if where.endswith(_A_SCHEMA)}
    assert len(shown) > 100  # the whole description, not a part of it
    for name, reason in KEPT_SCHEMAS.items():
        assert reason.strip() and old_name(name), name
        assert name in shown, f"{name} is no longer a schema: take it off the list"


def test_a_class_named_for_the_old_word_is_listed_under_its_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model of a shared record's `pack` key: its Python class and the key it types keep
    their names (signed records hold the key), and the API description lists the schema under
    the title the model sets, with every reference to it following."""
    from mendari.evals.share import SharedPack, SiteReport
    from mendari.server import api_docs

    assert SharedPack.__name__ == "SharedPack" and SharedPack in api_docs.TITLED
    assert "pack" in SiteReport.model_fields  # the key on the wire, unchanged
    whole = API_DOCUMENTS["the engine's API description, everything on"](tmp_path, monkeypatch)
    schemas = whole["components"]["schemas"]
    assert "SharedPack" not in schemas and schemas["SharedAgent"]["title"] == "SharedAgent"
    assert set(schemas["SharedAgent"]["properties"]) == {"id", "version", "digest"}
    references = set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(whole)))
    assert "SharedAgent" in references and "SharedPack" not in references
    assert references <= set(schemas)  # every reference names a schema that is listed
    # A title another schema already has is left alone, and a document without the model too.
    taken: dict[str, Any] = {"components": {"schemas": {"SharedPack": {}, "SharedAgent": {}}}}
    assert api_docs.titled(taken) == taken
    assert api_docs.titled({"paths": {}}) == {"paths": {}}


# The wire names each API description types (the others are keys of answers it leaves untyped).
SHOWN = {
    "the engine's API description, as a release starts": {"pack_id", "pack_digest"},
    "the engine's API description, everything on": {"pack", "pack_id", "pack_digest", "packs"},
    "the registry's API description": {"packs"},
}


@pytest.mark.parametrize("document", API_DOCUMENTS)
def test_an_api_description_still_types_its_wire_names(
    document: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not vacuous, and nothing renamed by accident: the wire names an API description types are
    still there under their names, and `pack_id` is still the query parameter callers send."""
    names = list(api_names(API_DOCUMENTS[document](tmp_path, monkeypatch)))
    assert {name for _, name in names} & WIRE_NAMES == SHOWN[document]
    parameters = {name for where, name in names if where.endswith("/parameters")}
    assert parameters & WIRE_NAMES == SHOWN[document] & {"pack_id"}

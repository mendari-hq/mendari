# SPDX-License-Identifier: Apache-2.0
"""Nothing Mendari ships carries an X12 code value (LP-14, the owner's decision of 2026-10-07).

X12's code lists are X12's work, under X12's terms. The packages carry the code systems' names
(the `https://codesystem.x12.org/005010/...` urls of HL7's CC0 Da Vinci PAS guide) and no value:
a site gives the values as settings or with each request (`standards/pas.py`, `X12_SETTINGS`), and
the sandbox payer's review action is the sandbox's setting. What ships is what the wheel, the
sdist and the images take from the repository: src/, packs/, datasets/, README.md and NOTICE.
Repository-only files (tests, scripts, docker-compose.yml) may carry values as configuration;
NOTICE lists them.

The scan reads data as data (any JSON or YAML coding in an X12 system that has a code), Python as
code (a coding built in an X12 system from a literal code; an X12 parameter, field, setting or
flag with a default), and every shipped line that speaks of X12 for the values Mendari filled in
or showed before the decision."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from mendari.standards import pas

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_TREES = ("src", "packs", "datasets")
SHIPPED_FILES = ("README.md", "NOTICE")
X12_SYSTEMS = "https://codesystem.x12.org"
# The X12 values Mendari filled in, gave as examples or answered with before the decision: the
# request and certification types, the service type in the command's help, the Coverage's code
# for self, the organizations' entity codes and the review actions (the payer's and a docstring's).
FORMER = ("HS", "I", "62", "18", "PR", "1P", "A1", "A3", "A4")
_FORMER = re.compile(rf"(?<![\w.-])({'|'.join(FORMER)})(?![\w.-])")
_SPEAKS_OF_X12 = re.compile(r"X12|x12\.org")
# What names an X12 value in Python: the PAS parameters, the request's fields and the payer's.
X12_NAMES = frozenset(pas.X12_SETTINGS) | {"review_action"}
X12_VARIABLES = frozenset(pas.X12_SETTINGS.values()) | {"MENDARI_SANDBOX_PAYER_REVIEW_ACTION"}
X12_FLAGS = frozenset(f"--{name.replace('_', '-')}" for name in X12_NAMES)


def shipped(root: Path = ROOT) -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", *SHIPPED_TREES, *SHIPPED_FILES],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [root / path for path in listed.split("\0") if path and (root / path).is_file()]


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None  # an image or a font


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def data_codings(path: Path, text: str) -> list[str]:
    """The X12 codings with a code in a JSON, JSON Lines or YAML file. A file that is not data (a
    template a command fills in) may not name an X12 system at all."""
    if path.suffix not in (".json", ".jsonl", ".yaml", ".yml"):
        return []
    try:
        if path.suffix == ".json":
            documents = [json.loads(text)]
        elif path.suffix == ".jsonl":
            documents = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            documents = list(yaml.safe_load_all(text))
    except (ValueError, yaml.YAMLError):
        return ["a template that names an X12 system"] if "x12.org" in text else []
    return [
        f"{node['system']}#{node['code']}"
        for document in documents
        for node in _walk(document)
        if str(node.get("system", "")).startswith(X12_SYSTEMS) and "code" in node
    ]


def _names_x12(node: ast.AST) -> bool:
    """Whether an expression names an X12 code system: the `X12` constant (or any name with X12
    in it), or a string with an X12 url."""
    for part in ast.walk(node):
        if isinstance(part, ast.Name) and "x12" in part.id.lower():
            return True
        if isinstance(part, ast.Attribute) and "x12" in part.attr.lower():
            return True
        if isinstance(part, ast.Constant) and "x12.org" in str(part.value):
            return True
    return False


def _is_none(node: ast.AST | None) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


def python_findings(text: str) -> list[str]:
    """Where shipped Python builds an X12 coding from a literal code, or gives an X12 value a
    default: a parameter, a model field, an environment read or a command-line flag. In a module
    that names X12, a string that is one of the former values is one too (a value passed to what
    builds the coding, as `_organization(insurer, ..., "PR")` was)."""
    found = []
    tree = ast.parse(text)
    names_x12 = _names_x12(tree)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if names_x12 and isinstance(node, ast.Constant) and node.value in FORMER:
            found.append(f"line {line}: the former X12 value {node.value!r}")
        if isinstance(node, ast.Dict):
            entries = {
                key.value: value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant)
            }
            code = entries.get("code")
            if (
                "system" in entries
                and _names_x12(entries["system"])
                and isinstance(code, ast.Constant)
            ):
                found.append(f"line {line}: an X12 coding with the literal code {code.value!r}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            positional = [*node.args.posonlyargs, *node.args.args]
            with_defaults = positional[len(positional) - len(node.args.defaults) :]
            pairs = [
                *zip(with_defaults, node.args.defaults, strict=True),
                *zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True),
            ]
            for argument, given in pairs:
                if argument.arg in X12_NAMES and not _is_none(given):
                    found.append(f"line {line}: parameter {argument.arg} has a default")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in X12_NAMES and node.value is not None:
                default: ast.AST | None = node.value
                if isinstance(node.value, ast.Call):  # Field(default=...) or Field(...)
                    keywords = {k.arg: k.value for k in node.value.keywords}
                    default = keywords.get(
                        "default", node.value.args[0] if node.value.args else None
                    )
                if not _is_none(default):
                    found.append(f"line {line}: field {node.target.id} has a default")
        elif isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            name = first.value if isinstance(first, ast.Constant) else None
            called = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if called in ("get", "getenv") and name in X12_VARIABLES and len(node.args) > 1:
                fallback = node.args[1]
                if not (isinstance(fallback, ast.Constant) and fallback.value in ("", None)):
                    found.append(f"line {line}: {name} read with a default")
            if called == "add_argument" and name in X12_FLAGS:
                keywords = {k.arg: k.value for k in node.keywords}
                if not _is_none(keywords.get("default")):
                    found.append(f"line {line}: {name} has a default")
    return found


def former_values_beside_x12(text: str) -> list[str]:
    """Lines that speak of X12 and hold a value Mendari filled in or showed before (a comment,
    a docstring, a help text, a document)."""
    return [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _SPEAKS_OF_X12.search(line) and _FORMER.search(line)
    ]


def scan(root: Path = ROOT) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {}
    for path in shipped(root):
        text = _text(path)
        if text is None:
            continue
        found = former_values_beside_x12(text)
        if path.suffix == ".py":
            found += python_findings(text)
        else:
            found += [f"an X12 coding with a code: {c}" for c in data_codings(path, text)]
        if found:
            findings[path.relative_to(root).as_posix()] = found
    return findings


def test_the_scan_covers_what_the_packages_take() -> None:
    """The sdist takes these, the wheel src/mendari with packs/ and datasets/, and the image
    copies the same: a new top-level path in a package is scanned only once it is listed here."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    build = project["tool"]["hatch"]["build"]["targets"]
    scanned = {f"/{name}" for name in (*SHIPPED_TREES, *SHIPPED_FILES)}
    assert set(build["sdist"]["include"]) - scanned == {"/LICENSE", "/pyproject.toml"}
    assert set(build["wheel"]["force-include"]) <= set(SHIPPED_TREES)
    copied = re.findall(r"^COPY (?!--from)(.+) \S+$", (ROOT / "Dockerfile").read_text(), re.M)
    taken = {part for line in copied for part in line.split()}
    assert taken - {name.strip("/") for name in scanned} == {"pyproject.toml", "uv.lock", "LICENSE"}
    assert len(shipped()) > 100


def test_nothing_shipped_carries_an_x12_code_value() -> None:
    assert scan() == {}


def test_the_scan_sees_what_mendari_shipped_before() -> None:
    """Each way an X12 value was carried before the decision is still caught."""
    before = '''
X12 = "https://codesystem.x12.org/005010"
def request_bundle(*, request_type: str = "HS", certification_type: str = "I") -> None:
    """`request_type` (X12 1525) defaults to a health services review."""
    coding = {"system": f"{X12}/306", "code": "A1"}
class Request(BaseModel):
    request_type: str | None = Field(default="HS", description="X12 1525")
    review_action: str = "A1"
def options(parser):
    parser.add_argument("--service-type", help="X12 service type (1365), e.g. 62")
    parser.add_argument("--certification-type", default="I")
    os.environ.get("MENDARI_PAS_REQUEST_TYPE", "HS")
def payer(insurer):
    return _organization(insurer, PROFILE["insurer"], "PR")
'''
    found = python_findings(before) + former_values_beside_x12(before)
    for expected in (
        "the former X12 value 'PR'",
        "parameter request_type has a default",
        "parameter certification_type has a default",
        "the literal code 'A1'",
        "field request_type has a default",
        "field review_action has a default",
        "--certification-type has a default",
        "MENDARI_PAS_REQUEST_TYPE read with a default",
        "e.g. 62",
    ):
        assert any(expected in finding for finding in found), (expected, found)
    record = {"category": {"coding": [{"system": f"{X12_SYSTEMS}/005010/1365", "code": "62"}]}}
    assert data_codings(Path("x.json"), json.dumps(record)) == [f"{X12_SYSTEMS}/005010/1365#62"]
    assert data_codings(Path("x.yaml"), yaml.safe_dump(record)) != []
    # What ships now: names only, every value a site's.
    allowed = '''
coding = {"system": f"{X12}/1525", "code": request_type}
def request_bundle(*, request_type: str | None = None) -> None:
    """`request_type` (X12 1525): the request's own, else MENDARI_PAS_REQUEST_TYPE."""
'''
    assert python_findings(allowed) + former_values_beside_x12(allowed) == []

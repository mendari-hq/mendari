# SPDX-License-Identifier: Apache-2.0
"""An agent's input schema is self-contained, and checking an input never leaves the agent
(core/input_schema.py)."""

from __future__ import annotations

import http.server
import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from referencing.exceptions import Unresolvable

from mendari import Decision, load_agent
from mendari.core import input_schema
from mendari.core.schema_graph import _graph
from mendari.errors import AgentValidationError
from tests.conftest import FIXTURE_AGENTS, ROOT, TRIAGE_DIR, message

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BODY = ("properties", "message", "properties", "body")


@pytest.fixture
def triage_copy(tmp_path: Path) -> Path:
    """A copy of the triage fixture to edit."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    return root


def edit_schema(root: Path, edit: Callable[[dict[str, Any]], None]) -> Path:
    path = root / "schemas" / "input.schema.json"
    schema = json.loads(path.read_text())
    edit(schema)
    path.write_text(json.dumps(schema))
    return root


def _body(schema: dict[str, Any]) -> dict[str, Any]:
    node = schema
    for key in BODY:
        node = node[key]
    return node


@pytest.fixture
def listener() -> Iterator[tuple[str, list[str]]]:
    """An HTTP server on 127.0.0.1 that records every request and serves a schema."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = b'{"type": "integer"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/schema.json", hits
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("kind", ["http", "file"])
def test_a_reference_outside_the_schema_fails_to_load_and_fetches_nothing(
    triage_copy: Path, tmp_path: Path, listener: tuple[str, list[str]], kind: str
) -> None:
    # jsonschema fetched any `$ref` URL (or read a local file) on every input check, with no
    # timeout: a request from the server to a host the agent chose, and validation rules outside
    # the signed agent.
    url, hits = listener
    secret = tmp_path / "secret.json"
    secret.write_text('{"type": "integer"}')
    reference = url if kind == "http" else secret.as_uri()
    root = edit_schema(triage_copy, lambda schema: _body(schema).update({"$ref": reference}))
    with pytest.raises(AgentValidationError) as raised:
        load_agent(root)
    assert raised.value.problems == [
        f"input schema: properties/message/properties/body/$ref: {reference!r} is outside the "
        "schema (an agent's input schema may only refer to its own parts, '#...')"
    ]
    schema = json.loads((root / "schemas" / "input.schema.json").read_text())
    with pytest.raises(Unresolvable):  # and the validator itself retrieves nothing
        list(input_schema.validator(schema).iter_errors(message("hello")))
    assert hits == []


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (
            lambda schema: _body(schema).update({"$schema": DIALECT}),
            f"properties/message/properties/body/$schema: only the root may name a dialect, "
            f"{DIALECT}",
        ),
        (
            lambda schema: schema.update({"$schema": "http://json-schema.org/draft-07/schema#"}),
            f"$schema: only the root may name a dialect, {DIALECT}",
        ),
        (
            lambda schema: _body(schema).update({"$dynamicAnchor": "meta"}),
            "properties/message/properties/body/$dynamicAnchor: not allowed in an agent's input "
            "schema",
        ),
        (
            lambda schema: _body(schema).update({"$id": DIALECT}),
            "properties/message/properties/body/$id: only the root may set $id",
        ),
        (
            lambda schema: _body(schema).update({"$dynamicRef": "https://example.org/x"}),
            "properties/message/properties/body/$dynamicRef: 'https://example.org/x' is outside "
            "the schema (an agent's input schema may only refer to its own parts, '#...')",
        ),
        (
            lambda schema: _body(schema).update({"$ref": "#/$defs/missing"}),
            "properties/message/properties/body/$ref: '#/$defs/missing' is not in the schema",
        ),
        (
            lambda schema: _body(schema).update({"$ref": "#/required/abc"}),
            "properties/message/properties/body/$ref: '#/required/abc' is not in the schema",
        ),
        (
            lambda schema: _body(schema).update({"$ref": "#/required/0"}),
            "properties/message/properties/body/$ref: '#/required/0' is not a schema",
        ),
        (
            lambda schema: _body(schema).update(format="regex"),
            "properties/message/properties/body/format: format 'regex' is not allowed in an "
            "agent's input schema",
        ),
    ],
    ids=[
        "nested-schema",
        "other-dialect",
        "dynamic-anchor",
        "nested-id",
        "remote-dynamic-ref",
        "dangling-ref",
        "pointer-into-a-list",
        "pointer-to-a-string",
        "format-regex",
    ],
)
def test_a_schema_that_is_not_self_contained_fails_to_load(
    triage_copy: Path, edit: Callable[[dict[str, Any]], None], problem: str
) -> None:
    with pytest.raises(AgentValidationError) as raised:
        load_agent(edit_schema(triage_copy, edit))
    assert raised.value.problems == [f"input schema: {problem}"]


def test_format_regex_is_refused_because_it_compiles_the_input(triage_copy: Path) -> None:
    # T5: jsonschema checks `format: regex` with `re.compile` on the caller's string: about 4 ms
    # per case-insensitive class, so one large input held a worker for minutes, and a string too
    # deep or with a huge repeat count raised past jsonschema (a 500 with no decision record).
    assert input_schema.problems({"type": "string", "format": "regex"}) == [
        "input schema: format: format 'regex' is not allowed in an agent's input schema"
    ]
    assert input_schema.problems({"type": "string", "format": "date"}) == []


def test_local_references_and_the_root_dialect_still_work(triage_copy: Path) -> None:
    def edit(schema: dict[str, Any]) -> None:
        schema["$schema"] = DIALECT
        schema["$id"] = "https://example.org/triage.schema.json"
        schema["$defs"] = {"text": {"type": "string", "maxLength": 5}}
        schema["properties"]["note"] = {"$ref": "#/$defs/text"}
        schema["properties"]["reply"] = {"$ref": "#"}

    decision = Decision(load_agent(edit_schema(triage_copy, edit)))
    assert decision.validate({**message("hi"), "note": "short"}) == []
    assert decision.validate({**message("hi"), "note": "far too long"}) != []
    assert decision.validate({**message("hi"), "reply": message("inner")}) == []


def test_every_shipped_input_schema_is_self_contained() -> None:
    for root in sorted(FIXTURE_AGENTS.iterdir()):
        if (root / "agent.yaml").is_file():
            assert input_schema.problems(load_agent(root).input_schema) == [], root.name


# -- the expansion bounds on a schema the deployment owns (LP-16f) --------------------------------

APPLICATIONS = (
    "schemas (at most 1,000): a $ref, allOf, anyOf or oneOf that reaches the same schema more "
    "than once multiplies it"
)
LEADS_BACK = (
    "a $ref leads back to a schema holding it on the same value, or more than once; a schema may "
    "refer back to itself only once, through properties or items"
)
UNEVALUATED_AGAIN = (
    "its unevaluatedProperties or unevaluatedItems reaches a schema that holds one too (or itself "
    "again): jsonschema checks everything below the first again for it, so each one more doubles "
    "the work; keep at most one on any path, or use additionalProperties or items"
)


def fanning(depth: int, width: int) -> Callable[[dict[str, Any]], None]:
    """`note` is `$defs/t0`, and `t{i}` is allOf `width` `$ref`s to `t{i+1}`: the last applies
    `width ** depth` times to one value."""

    def edit(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"$ref": "#/$defs/t0"}
        schema["$defs"] = {
            f"t{i}": {"allOf": [{"$ref": f"#/$defs/t{i + 1}"}] * width} for i in range(depth)
        } | {f"t{depth}": {"type": "string"}}

    return edit


def triage_at(base: Path, name: str) -> Path:
    """A copy of the triage fixture in `base/name`."""
    root = base / name
    shutil.copytree(TRIAGE_DIR, root)
    return root


def refused(root: Path) -> list[str]:
    with pytest.raises(AgentValidationError) as raised:
        load_agent(root)
    return raised.value.problems


def test_a_nested_ref_schema_is_refused_at_load(triage_copy: Path) -> None:
    """LP-16's nested-`$ref` test, for an agent the deployment owns (one it does not own keeps the
    caps and the time budget, `tests/test_bounded_budget.py`): 28 levels of allOf over two
    `$ref`s apply the last schema 2**28 times to one value, and jsonschema would walk every one.
    The agent is refused at load, saying why."""
    assert refused(edit_schema(triage_copy, fanning(28, 2))) == [
        "input schema: #/properties/note: a chain of 29 $refs (at most 16)",
        f"input schema: one value can be checked against more than 1,000,000,000 {APPLICATIONS}",
    ]


def test_a_wide_fan_out_is_refused_at_load(triage_copy: Path) -> None:
    """Twenty `$ref`s at each of 4 levels: 160,000 applications of the last, with no long chain."""
    assert refused(edit_schema(triage_copy, fanning(4, 20))) == [
        f"input schema: one value can be checked against 336,842 {APPLICATIONS}"
    ]


def test_a_ref_cycle_in_place_is_refused_at_load(triage_copy: Path, tmp_path: Path) -> None:
    """A schema that applies itself to the same value never ends (jsonschema recurses until
    Python stops it); a tree whose node applies its child twice doubles at each level."""

    def in_place(schema: dict[str, Any]) -> None:
        schema["$defs"] = {"a": {"allOf": [{"$ref": "#/$defs/a"}]}}
        schema["properties"]["note"] = {"$ref": "#/$defs/a"}

    assert refused(edit_schema(triage_copy, in_place)) == [f"input schema: #/$defs/a: {LEADS_BACK}"]

    def doubling_tree(schema: dict[str, Any]) -> None:
        schema["$defs"] = {
            "n": {"allOf": [{"$ref": "#/$defs/m"}, {"$ref": "#/$defs/m"}]},
            "m": {"properties": {"child": {"$ref": "#/$defs/n"}}},
        }
        schema["properties"]["tree"] = {"$ref": "#/$defs/n"}

    other = tmp_path / "tree"
    shutil.copytree(TRIAGE_DIR, other)
    assert refused(edit_schema(other, doubling_tree)) == [f"input schema: #/$defs/n: {LEADS_BACK}"]


def test_a_binary_tree_recursion_is_refused_at_load(triage_copy: Path) -> None:
    """A node whose `left` and `right` are both the node leads back into the loop twice: refused
    as conflict 22 chose (a loop leads back once), though one value meets the node once."""

    def binary(schema: dict[str, Any]) -> None:
        node = {"properties": {"left": {"$ref": "#/$defs/n"}, "right": {"$ref": "#/$defs/n"}}}
        schema["$defs"] = {"n": node}
        schema["properties"]["tree"] = {"$ref": "#/$defs/n"}

    assert refused(edit_schema(triage_copy, binary)) == [f"input schema: #/$defs/n: {LEADS_BACK}"]


def nested_unevaluated(levels: int, last: dict[str, Any]) -> dict[str, Any]:
    """`levels` schemas, each anyOf the next and holding `unevaluatedProperties: false`."""
    inner = last
    for _ in range(levels):
        inner = {"anyOf": [inner], "unevaluatedProperties": False}
    return inner


def test_nested_unevaluated_keywords_are_refused_at_load_and_quickly(tmp_path: Path) -> None:
    """The review's schema: three `$defs`, each 13 `unevaluatedProperties` deep, the last of one
    a `$ref` to the next. It nests 29 deep, chains 3 `$ref`s and applies 43 schema objects to one
    value, within every bound, yet to find what is unevaluated jsonschema validates again all
    that is below each one, so each level multiplies the work (on `{"a": 1}`: one `$defs` took
    7 ms, two 3.5 s, three over 60 s). It is refused at load, in a few milliseconds; so is one
    nested through a property."""

    def review(schema: dict[str, Any]) -> None:
        schema["$defs"] = {
            f"d{i}": nested_unevaluated(13, {"$ref": f"#/$defs/d{i + 1}"} if i < 2 else {})
            for i in range(3)
        }
        schema["properties"]["note"] = {"$ref": "#/$defs/d0"}

    root = edit_schema(triage_at(tmp_path, "review"), review)
    schema = json.loads((root / "schemas" / "input.schema.json").read_text())
    counted = input_schema._expansion(_graph(schema))
    assert not isinstance(counted, str)
    assert input_schema._nesting(schema) == 29 and counted[:2] == (43, 3)
    started = time.perf_counter()
    assert refused(root) == [f"input schema: #/$defs/d0: {UNEVALUATED_AGAIN}"]
    assert time.perf_counter() - started < 2

    def through_a_property(schema: dict[str, Any]) -> None:
        inner: dict[str, Any] = {}
        for _ in range(3):
            inner = {"anyOf": [{"properties": {"a": inner}}], "unevaluatedProperties": False}
        schema["properties"]["note"] = inner

    root = edit_schema(triage_at(tmp_path, "property"), through_a_property)
    assert refused(root) == [f"input schema: #/properties/note: {UNEVALUATED_AGAIN}"]


def test_one_unevaluated_keyword_on_a_path_still_loads(tmp_path: Path) -> None:
    """One `unevaluatedProperties` (and another on a path of its own) is checked as before."""

    def once(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "anyOf": [{"properties": {"y": {"type": "integer"}}}],
            "unevaluatedProperties": False,
        }
        schema["properties"]["codes"] = {
            "prefixItems": [{"type": "string"}],
            "unevaluatedItems": False,
        }

    decision = Decision(load_agent(edit_schema(triage_at(tmp_path, "once"), once)))
    assert decision.validate({**message("hi"), "note": {"x": "a", "y": 1}, "codes": ["c"]}) == []
    assert decision.validate({**message("hi"), "note": {"x": "a", "z": 1}}) != []
    assert decision.validate({**message("hi"), "codes": ["c", "d"]}) != []


def test_a_recursive_schema_that_reaches_another_is_refused_at_load(triage_copy: Path) -> None:
    """A tree whose every node holds a recursive list: one value deep down meets the list's
    schema once for each level above it, so the count grows with the input's depth."""

    def nested_loops(schema: dict[str, Any]) -> None:
        schema["$defs"] = {
            "node": {
                "properties": {
                    "children": {"items": {"$ref": "#/$defs/node"}},
                    "tags": {"$ref": "#/$defs/tags"},
                }
            },
            "tags": {"items": {"$ref": "#/$defs/tags"}},
        }
        schema["properties"]["tree"] = {"$ref": "#/$defs/node"}

    assert refused(edit_schema(triage_copy, nested_loops)) == [
        "input schema: #/$defs/node: a schema that refers back to itself leads to another that "
        "does: the deeper the input, the more schemas one value is checked against"
    ]


def test_a_schema_nested_past_32_levels_is_refused_at_load(tmp_path: Path) -> None:
    """The triage fixture's schema with a property nested to 33 levels in all (`not` in
    `not`...); 32 loads."""

    def nested(levels: int) -> Callable[[dict[str, Any]], None]:
        def edit(schema: dict[str, Any]) -> None:
            deep: dict[str, Any] = {"type": "string"}
            for _ in range(levels - 3):  # the root, `properties` and the property: 3 levels
                deep = {"not": deep}
            schema["properties"]["note"] = deep

        return edit

    for levels in (32, 33):
        shutil.copytree(TRIAGE_DIR, tmp_path / str(levels))
    assert refused(edit_schema(tmp_path / "33", nested(33))) == [
        "input schema: nested 33 levels deep (at most 32)"
    ]
    assert load_agent(edit_schema(tmp_path / "32", nested(32))).input_schema


def test_a_long_ref_chain_is_refused_at_load(tmp_path: Path) -> None:
    def chain(refs: int) -> Callable[[dict[str, Any]], None]:
        def edit(schema: dict[str, Any]) -> None:
            schema["properties"]["note"] = {"$ref": "#/$defs/a0"}
            schema["$defs"] = {f"a{i}": {"$ref": f"#/$defs/a{i + 1}"} for i in range(refs - 1)}
            schema["$defs"][f"a{refs - 1}"] = {"type": "string"}

        return edit

    for refs in (16, 17):
        shutil.copytree(TRIAGE_DIR, tmp_path / str(refs))
    assert refused(edit_schema(tmp_path / "17", chain(17))) == [
        "input schema: #/properties/note: a chain of 17 $refs (at most 16)"
    ]
    decision = Decision(load_agent(edit_schema(tmp_path / "16", chain(16))))
    assert decision.validate({**message("hi"), "note": "x"}) == []
    assert decision.validate({**message("hi"), "note": 1}) != []


def test_a_recursive_list_schema_still_loads_and_decides(triage_copy: Path) -> None:
    """A tree whose nodes list their children (a simple loop through `items`) meets each of its
    schemas once on any value: it loads, checks a deep input, and decides as before."""

    def tree(schema: dict[str, Any]) -> None:
        node = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "children": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            },
        }
        schema["$defs"] = {"node": node}
        schema["properties"]["tree"] = {"$ref": "#/$defs/node"}

    edited = Decision(load_agent(edit_schema(triage_copy, tree)))
    deep: dict[str, Any] = {"name": "leaf"}
    for level in range(50):
        deep = {"name": f"level {level}", "children": [deep, {"name": "sibling"}]}
    given = message("Can I get a refill of my blood pressure medicine?")
    shipped = Decision(load_agent(TRIAGE_DIR)).decide(given)
    result = edited.decide({**given, "tree": deep})
    for field in ("outcome", "recommendation", "priority", "reasons", "labels"):
        assert getattr(result, field) == getattr(shipped, field), field
    leaf = deep
    while leaf.get("children"):
        leaf = leaf["children"][0]
    leaf["name"] = 5
    assert edited.validate({**given, "tree": deep}) != []


def test_every_shipped_schema_is_within_the_expansion_bounds() -> None:
    """The agents the tests bundle, the scaffolds (`mendari new`) and every other test fixture,
    with their margins: at most 6 deep (32), 1 `$ref` in a chain (16) and 2 schema objects on one
    value (1,000)."""
    found = sorted(
        {
            *FIXTURE_AGENTS.glob("*/schemas/*.json"),
            *ROOT.glob("src/mendari/templates/*/schemas/*.json"),
            *ROOT.glob("tests/fixtures/**/schemas/*.json"),
        }
    )
    bundled = [root for root in FIXTURE_AGENTS.iterdir() if (root / "agent.yaml").is_file()]
    assert bundled and all(any(path.is_relative_to(root) for path in found) for root in bundled)
    assert any(path.is_relative_to(ROOT / "src" / "mendari" / "templates") for path in found)
    for path in found:
        schema = json.loads(path.read_text())
        assert input_schema.expansion_problems(schema) == [], path
        counted = input_schema._expansion(_graph(schema))
        assert not isinstance(counted, str), path
        applications, chain, _ = counted
        assert input_schema._nesting(schema) <= 6 and chain <= 1 and applications <= 2, path
        assert not any(node.unevaluated for node in _graph(schema)), path  # none uses either
    for root in sorted(FIXTURE_AGENTS.iterdir()):
        if (root / "agent.yaml").is_file():
            assert input_schema.expansion_problems(load_agent(root).input_schema) == [], root.name

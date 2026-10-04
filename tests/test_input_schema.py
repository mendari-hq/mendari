"""A pack's input schema is self-contained, and checking an input never leaves the pack
(core/input_schema.py)."""

from __future__ import annotations

import http.server
import json
import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from referencing.exceptions import Unresolvable

from mendari import Decision, load_pack
from mendari.core import input_schema
from mendari.errors import PackValidationError
from tests.conftest import INBOX_DIR, message

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BODY = ("properties", "message", "properties", "body")


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """A copy of the inbox pack to edit."""
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
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
    inbox: Path, tmp_path: Path, listener: tuple[str, list[str]], kind: str
) -> None:
    # jsonschema fetched any `$ref` URL (or read a local file) on every input check, with no
    # timeout: a request from the server to a host the pack chose, and validation rules outside
    # the signed pack.
    url, hits = listener
    secret = tmp_path / "secret.json"
    secret.write_text('{"type": "integer"}')
    reference = url if kind == "http" else secret.as_uri()
    root = edit_schema(inbox, lambda schema: _body(schema).update({"$ref": reference}))
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    assert raised.value.problems == [
        f"input schema: properties/message/properties/body/$ref: {reference!r} is outside the "
        "schema (a pack input schema may only refer to its own parts, '#...')"
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
            "properties/message/properties/body/$dynamicAnchor: not allowed in a pack input schema",
        ),
        (
            lambda schema: _body(schema).update({"$id": DIALECT}),
            "properties/message/properties/body/$id: only the root may set $id",
        ),
        (
            lambda schema: _body(schema).update({"$dynamicRef": "https://example.org/x"}),
            "properties/message/properties/body/$dynamicRef: 'https://example.org/x' is outside "
            "the schema (a pack input schema may only refer to its own parts, '#...')",
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
            "properties/message/properties/body/format: format 'regex' is not allowed in a pack "
            "input schema",
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
    inbox: Path, edit: Callable[[dict[str, Any]], None], problem: str
) -> None:
    with pytest.raises(PackValidationError) as raised:
        load_pack(edit_schema(inbox, edit))
    assert raised.value.problems == [f"input schema: {problem}"]


def test_format_regex_is_refused_because_it_compiles_the_input(inbox: Path) -> None:
    # T5: jsonschema checks `format: regex` with `re.compile` on the caller's string: about 4 ms
    # per case-insensitive class, so one large input held a worker for minutes, and a string too
    # deep or with a huge repeat count raised past jsonschema (a 500 with no decision record).
    assert input_schema.problems({"type": "string", "format": "regex"}) == [
        "input schema: format: format 'regex' is not allowed in a pack input schema"
    ]
    assert input_schema.problems({"type": "string", "format": "date"}) == []


def test_local_references_and_the_root_dialect_still_work(inbox: Path) -> None:
    def edit(schema: dict[str, Any]) -> None:
        schema["$schema"] = DIALECT
        schema["$id"] = "https://example.org/inbox.schema.json"
        schema["$defs"] = {"text": {"type": "string", "maxLength": 5}}
        schema["properties"]["note"] = {"$ref": "#/$defs/text"}
        schema["properties"]["reply"] = {"$ref": "#"}

    decision = Decision(load_pack(edit_schema(inbox, edit)))
    assert decision.validate({**message("hi"), "note": "short"}) == []
    assert decision.validate({**message("hi"), "note": "far too long"}) != []
    assert decision.validate({**message("hi"), "reply": message("inner")}) == []


def test_every_shipped_input_schema_is_self_contained() -> None:
    for root in sorted(INBOX_DIR.parent.iterdir()):
        if (root / "pack.yaml").is_file():
            assert input_schema.problems(load_pack(root).input_schema) == [], root.name

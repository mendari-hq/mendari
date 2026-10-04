"""A pack never stops the server: whatever one pack's content makes loading, checking or
building raise, that pack is listed under /api/health `invalid` and every other pack is served
(core/pack.py, core/registry.py, server/app.py, standards/terminology.py, hub/registry.py)."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import load_pack
from mendari.core import registry as registry_module
from mendari.core.pack import MAX_YAML_NODES
from mendari.core.registry import version_key
from mendari.errors import PackValidationError
from mendari.server.app import create_app
from mendari.standards import terminology as terminology_module
from tests.conftest import INBOX_DIR

PACKS = INBOX_DIR.parent
TRIAL_DIR = PACKS / "clinical-trial-prescreener"


def _outsider(root: Path, edit: Callable[[Path], None], source: Path = INBOX_DIR) -> Path:
    """An outside copy of a shipped pack (its own id) under `root`, changed by `edit`."""
    pack = root / "outsider" / source.name
    shutil.copytree(source, pack)
    manifest_path = pack / "pack.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["id"] = f"outsider/{source.name}"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
    edit(pack)
    return pack


def _with_schema_property(value: Any) -> Callable[[Path], None]:
    def edit(pack: Path) -> None:
        path = pack / "schemas" / "input.schema.json"
        schema = json.loads(path.read_text())
        schema["properties"]["zz"] = value
        path.write_text(json.dumps(schema))

    return edit


def _write(relative: str, content: str | bytes) -> Callable[[Path], None]:
    def edit(pack: Path) -> None:
        data = content.encode() if isinstance(content, str) else content
        (pack / relative).write_bytes(data)

    return edit


def _append(relative: str, text: str) -> Callable[[Path], None]:
    def edit(pack: Path) -> None:
        path = pack / relative
        path.write_text(path.read_text() + text)

    return edit


def laughs(levels: int, leaf: str = "{a: 1}") -> str:
    """A YAML key whose value is 10**levels leaves, in a few hundred bytes of anchors."""
    lines = [f"x_junk:\n  l0: &l0 [{', '.join([leaf] * 10)}]"]
    for level in range(1, levels + 1):
        lines.append(f"  l{level}: &l{level} [{', '.join([f'*l{level - 1}'] * 10)}]")
    return "\n" + "\n".join(lines) + "\n"


def _shipped_packs_all_decide(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["packs"] >= 5
    for pack_dir in sorted(PACKS.iterdir()):
        case = yaml.safe_load((pack_dir / "tests" / "cases.yaml").read_text())[0]
        response = client.post(f"/api/packs/mendari/{pack_dir.name}/decide", json=case["input"])
        assert response.status_code == 200, (pack_dir.name, response.text)


DEEP = 5000  # nested deeper than the YAML, JSON and jsonschema readers recurse


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (_with_schema_property({"$ref": "#/required/abc"}), "'#/required/abc' is not in the"),
        (
            _with_schema_property({"type": "string", "pattern": "a{99999999999}"}),
            "input schema: properties/zz/pattern: invalid regular expression: the repetition",
        ),
        (_write("schemas/input.schema.json", "{not json"), "not a JSON document (JSONDecodeErr"),
        (
            _write("schemas/input.schema.json", b'{"a": "\xff\xfe"}'),
            "not a JSON document (UnicodeDecodeError)",
        ),
        (
            _write("schemas/input.schema.json", "[" * 200_000 + "]" * 200_000),
            "not a JSON document (RecursionError)",
        ),
        (
            _with_schema_property(json.loads('{"not":' * 900 + "{}" + "}" * 900)),
            "not a schema the validator can read (RecursionError)",
        ),
        (_write("pack.yaml", "a: " + "[" * DEEP + "]" * DEEP), "invalid YAML (RecursionError)"),
        (_write("pack.yaml", b"id: \xff\xfe"), "pack.yaml: invalid YAML (UnicodeDecodeError)"),
        (_append("pack.yaml", laughs(7)), "pack.yaml: expands to more than 1,000,000 nodes"),
        (_append("pack.yaml", "x_loop: &loop [*loop]\n"), "refers to the anchor it is inside"),
    ],
    ids=[
        "ref-index",
        "repeat-count",
        "schema-not-json",
        "schema-not-utf8",
        "schema-too-deep",
        "schema-too-deep-to-check",
        "manifest-too-deep",
        "manifest-not-utf8",
        "manifest-billion-laughs",
        "manifest-alias-loop",
    ],
)
def test_a_pack_whose_content_raises_while_loading_is_listed_and_the_others_decide(
    tmp_path: Path, edit: Callable[[Path], None], problem: str
) -> None:
    """R2, T2, T7: an outside pack whose content made loading raise something other than a
    Mendari error (a ValueError from a `$ref` pointer, a RecursionError from deep nesting), or
    whose YAML aliases expanded exponentially, stopped or stalled the server at startup, every
    Vercel cold start included, so no pack was served."""
    outside = tmp_path / "outside"
    pack = _outsider(outside, edit).resolve()
    started = time.perf_counter()
    client = TestClient(create_app(db_path=tmp_path / "o.db", packs_paths=[outside, PACKS]))
    assert time.perf_counter() - started < 10
    invalid = client.get("/api/health").json()["invalid"]
    assert list(invalid) == [str(pack)]
    assert problem in invalid[str(pack)] and "Traceback" not in invalid[str(pack)]
    _shipped_packs_all_decide(client)


def test_any_error_loading_one_pack_lists_it_by_type_and_serves_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever a pack's content makes loading raise, the pack is listed under `invalid` by the
    error's type (its text is the pack's, not Mendari's) and every other pack is served."""
    outside = tmp_path / "outside"
    pack = _outsider(outside, lambda pack: None).resolve()
    real = registry_module.load_pack

    def load(root: Path, **options: Any) -> Any:
        if Path(root) == pack:
            raise RuntimeError("text from the pack")
        return real(root, **options)

    monkeypatch.setattr(registry_module, "load_pack", load)
    client = TestClient(create_app(db_path=tmp_path / "r.db", packs_paths=[outside, PACKS]))
    assert client.get("/api/health").json()["invalid"] == {
        str(pack): "the pack could not be loaded (RuntimeError)"
    }
    _shipped_packs_all_decide(client)


def test_an_unexpected_error_building_terminology_is_named_and_the_pack_still_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = terminology_module.resources

    def resources(packs: Any) -> Any:
        packs = list(packs)
        if any(pack.id == "mendari/referral-router" for pack in packs):
            raise KeyError("text from the pack")
        return real(packs)

    monkeypatch.setattr(terminology_module, "resources", resources)
    client = TestClient(create_app(db_path=tmp_path / "t.db", packs_paths=[PACKS]))
    assert client.get("/api/health").json()["invalid"] == {
        "mendari/referral-router": "terminology: KeyError"
    }
    _shipped_packs_all_decide(client)


def _cql_default(change: Callable[[Path], None]) -> Callable[[Path], None]:
    def edit(pack: Path) -> None:
        manifest = (pack / "pack.yaml").read_text()
        assert "    use: criteria" in manifest
        (pack / "pack.yaml").write_text(manifest.replace("    use: criteria", "    use: cql", 1))
        change(pack)

    return edit


def _cql_extra_key(pack: Path) -> None:
    manifest = yaml.safe_load((pack / "pack.yaml").read_text())
    for slot in manifest["providers"].values():
        for variant in slot["variants"].values():
            if variant.get("type") == "cql":
                variant["bogus"] = 1
    (pack / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (_cql_extra_key, "Extra inputs are not permitted"),
        (
            lambda pack: (pack / "logic" / "criteria.cql").write_bytes(b"\xff\xfe library"),
            "its providers could not be built (UnicodeDecodeError)",
        ),
    ],
    ids=["extra-key", "not-utf8"],
)
def test_a_pack_whose_providers_cannot_be_built_never_fails_health(
    tmp_path: Path, change: Callable[[Path], None], problem: str
) -> None:
    """T9: providers are built when a pack is first used (main's lazy start), and /api/health
    built every served pack's; one pack whose default `cql` variant could not be built made
    /api/health a 422 or a 500 for every reader. It is listed under `invalid` now, its decisions
    answer 422 naming the problem, and every other pack is reported and decides."""
    outside = tmp_path / "outside"
    _outsider(outside, _cql_default(change), source=TRIAL_DIR)
    client = TestClient(create_app(db_path=tmp_path / "c.db", packs_paths=[outside, PACKS]))
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    body = health.json()
    assert list(body["invalid"]) == ["outsider/clinical-trial-prescreener"]
    assert problem in body["invalid"]["outsider/clinical-trial-prescreener"]
    assert "outsider/clinical-trial-prescreener" not in body["providers"]
    assert "mendari/clinical-trial-prescreener" in body["providers"]
    case = yaml.safe_load((TRIAL_DIR / "tests" / "cases.yaml").read_text())[0]
    refused = client.post(
        "/api/packs/outsider/clinical-trial-prescreener/decide", json=case["input"]
    )
    assert refused.status_code == 422, refused.text
    _shipped_packs_all_decide(client)


def test_a_pack_whose_tests_do_not_load_never_fails_the_api_reference(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _outsider(outside, _write("tests/cases.yaml", b"- name: \xff\xfe"))
    client = TestClient(create_app(db_path=tmp_path / "a.db", packs_paths=[outside, PACKS]))
    assert client.get("/openapi.json").status_code == 200


# -- versions -------------------------------------------------------------------------------------


def test_an_over_long_version_is_refused_at_load_and_never_stops_startup(tmp_path: Path) -> None:
    """T10: a version of 5,000 digits loaded, and comparing two versions of one pack at startup
    (`int()` refuses more than 4,300 digits) raised outside every isolation, so the server never
    started."""
    huge = "1" + "0" * 5000 + ".0.0"
    outside = tmp_path / "outside"
    pack = _outsider(
        outside, lambda pack: _replace(pack / "pack.yaml", "version: 0.3.0", f"version: '{huge}'")
    ).resolve()
    with pytest.raises(PackValidationError, match="at most 64 characters"):
        load_pack(pack)
    client = TestClient(create_app(db_path=tmp_path / "v.db", packs_paths=[outside, PACKS]))
    assert "at most 64 characters" in client.get("/api/health").json()["invalid"][str(pack)]
    _shipped_packs_all_decide(client)


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1))


def test_versions_compare_as_numbers_whatever_their_length() -> None:
    ordered = ["0.9.0", "0.10.0", "1.0.0", "1.0.10", "2.0.0", "1" + "0" * 5000 + ".0.0"]
    assert sorted(reversed(ordered), key=version_key) == ordered
    assert version_key("1.02.0") == version_key("1.2.0")
    assert version_key("1.0.0-rc.1") == version_key("1.0.0")


# -- YAML a pack carries ------------------------------------------------------------------------


def test_a_billion_laughs_in_a_rules_file_is_refused_in_milliseconds(tmp_path: Path) -> None:
    """T7: a rules file of a few KB whose aliases expand to 10**8 patterns took 15 s and 1.25 GB
    to walk. The expanded size is counted on the composed graph, each node once."""
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    _append("logic/red_flags.yaml", laughs(8, "{pattern: x}"))(root)
    started = time.process_time()
    with pytest.raises(PackValidationError, match=r"logic/red_flags\.yaml: expands to more than"):
        load_pack(root)
    assert time.process_time() - started < 0.5


def test_anchors_within_the_cap_still_load(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    _append("logic/red_flags.yaml", laughs(4))(root)  # 10**4 leaves: well within the cap
    rules = load_pack(root).read_yaml("logic/red_flags.yaml")
    assert len(rules["x_junk"]["l4"]) == 10 and rules["questions"]
    assert MAX_YAML_NODES == 1_000_000


def test_only_yaml_with_an_anchor_is_walked_for_its_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an anchor there is no alias, so the parsed graph is the file's own tree and
    walking it adds nothing but time to every load (a few milliseconds per start)."""
    from mendari.core import pack as pack_module

    walked: list[object] = []
    real = pack_module._expansion_problem

    def spy(node: Any) -> str | None:
        walked.append(node)
        return real(node)

    monkeypatch.setattr(pack_module, "_expansion_problem", spy)
    plain, anchored = tmp_path / "plain.yaml", tmp_path / "anchored.yaml"
    plain.write_text("a: [1, 2]\nb: {c: 3}\n")
    anchored.write_text("a: &x [1, 2]\nb: *x\n")
    assert pack_module.load_yaml(plain, "plain.yaml", "x") == {"a": [1, 2], "b": {"c": 3}}
    assert walked == []
    assert pack_module.load_yaml(anchored, "anchored.yaml", "x") == {"a": [1, 2], "b": [1, 2]}
    assert len(walked) == 1

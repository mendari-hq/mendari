# SPDX-License-Identifier: Apache-2.0
"""Writes tests/fixtures/evaluation_pages.json: what a registry answers for an agent's public
evaluation (`mendari.agent-evaluation/1`, `hub/evaluation_page.py`) and its badge, in every state
the Agent Hub's evaluation page has words for. The Hub's tests show the page from it
(`hub/tests/stub-registry.mjs` serves it as a registry would), since a seeded registry cannot be
in most of these states: a record changed after it was stored, a key revoked, a tie.

    uv run python -m tests.support.evaluation_pages          # rewrite the fixture
    uv run python -m tests.support.evaluation_pages --check  # exit 1 when it is not current

Every answer is a real one: a registry in this process, copies of the demo agent published to
it and evaluated on its synthetic test set through the registry's own routes and checks, then
the rows changed behind its back where a state needs it. Nothing is hand-written.

So that the same code gives the same file, what a run would vary is pinned: the publisher's
keys (made from fixed bytes, in the open: they sign nothing but this fixture), every time, the
latency and cost a run measures, and the release that ran it. `tests/test_evaluation_page.py`
checks the committed file against a fresh one by its shape (`shape`): the same keys, states and
words, whatever the counts are, so a change to the demo agent's data does not fail it and a
change to the answer does.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from mendari.core.decision import Decision
from mendari.core.pack import Pack, load_pack
from mendari.evals.harness import evaluation, resolve_dataset
from mendari.hub import archive, evaluations, regression, yank
from mendari.hub import registry as registry_module
from mendari.hub.registry import RegistryStore, create_registry_app

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "evaluation_pages.json"
DEMO = ROOT / "agents" / "portal-message-agent"
NAMESPACE = "states"
PUBLISHED_AT = "2026-11-23T11:00:00+00:00"
SIGNED_AT = "2026-11-23T12:00:00Z"
# Test keys, from fixed bytes: the same ids and signatures on every run.
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
OTHER_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
# What a run measures, pinned: a local configuration's, and one that calls a model.
LOCAL = {"latency_ms_p50": 0.05, "latency_ms_p95": 0.07, "cost_usd_per_decision": 0.0}
REMOTE = {"latency_ms_p50": 1840.0, "latency_ms_p95": 3920.5, "cost_usd_per_decision": 0.0062}
LABELLED_BY = "The script that wrote the cases (scripts/demo_agent_data.py)"
HOSTILE_NAME = "Hostile <script>window.__hostile = 5</script> Agent"
HOSTILE_LABELLER = '<img src=x onerror="window.__hostile = 6"> "two" & <b>nurses</b>'
Edit = Callable[[dict[str, Any], Path], None]


@contextlib.contextmanager
def pinned() -> Iterator[None]:
    """The registry's clock and limits as the fixture needs them, put back after: a record
    signed at `SIGNED_AT` is taken whenever this runs, every version is published at
    `PUBLISHED_AT`, and no limit counts. The registry's two jobs in an isolated child run in
    this process instead (the child is the product's, and `tests/test_registry_regression.py`
    and `tests/test_publisher_evaluations.py` cover it): a local configuration's re-run, and
    the check at publish, which for the demo agent only says its default calls a model."""
    settings = {"MENDARI_REGISTRY_RATE_LIMITS": "off", "MENDARI_REGISTRY_REGRESSION": "on"}

    def rerun(pack_dir: Path, configuration: Any, dataset: str) -> dict[str, Any]:
        return regression._rerun_main(str(pack_dir), dataset, json.dumps(dict(configuration)))

    def check(pack_dir: Path, previous: Any) -> dict[str, Any]:
        return {"format": regression.FORMAT, "label": regression.LABEL, "status": "needs_remote"}

    def never_skewed(created_at: str, *, now_: Any = None) -> bool:
        return False

    with (
        mock.patch.object(evaluations, "skewed", never_skewed),
        mock.patch.object(registry_module, "_now", lambda: PUBLISHED_AT),
        mock.patch.object(regression, "rerun", rerun),
        mock.patch.object(regression, "check", check),
        mock.patch.dict(os.environ, settings),  # the environment as it was, afterwards
    ):
        os.environ.pop("MENDARI_REGISTRY_HUB_URL", None)
        yield


class World:
    """A registry in this process with the `states` namespace, and what the cases do to it."""

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.db = str(scratch / "registry.db")
        self.store = RegistryStore(self.db)
        self.client = TestClient(create_registry_app(self.db))
        self.client.__enter__()
        self.store.create_namespace(NAMESPACE, "verified", "States (synthetic)")
        minted = self.store.mint_token(
            NAMESPACE, org=NAMESPACE, owner="operator", name="fixture", days=1, by="operator"
        )
        self.auth = {"authorization": f"Bearer {minted['token']}"}
        for key in (KEY, OTHER_KEY):
            public = archive.public_pem(key.public_key()).decode()
            added = self.client.post(
                f"/v1/namespaces/{NAMESPACE}/keys", json={"public_pem": public}, headers=self.auth
            )
            assert added.status_code == 201, added.text

    def close(self) -> None:
        self.client.__exit__(None, None, None)
        self.store.close()

    def agent(self, name: str, edit: Edit | None = None, *, version: str | None = None) -> Pack:
        """The demo agent under `states/<name>`, changed by `edit` (its manifest, and its
        folder), published."""
        root = self.scratch / f"{name}-{version or 'first'}"
        shutil.copytree(DEMO, root)
        manifest = yaml.safe_load((root / "agent.yaml").read_text(encoding="utf-8"))
        manifest["id"] = f"{NAMESPACE}/{name}"
        manifest["name"] = f"Fixture agent: {name}"
        if version is not None:
            manifest["version"] = version
        if edit is not None:
            edit(manifest, root)
        (root / "agent.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        pack = load_pack(root)
        sent = self.client.put(
            f"/v1/agents/{pack.id}/{pack.version}",
            content=archive.build(pack, KEY),
            headers=self.auth,
        )
        assert sent.status_code == 201, sent.text
        return pack

    def record(
        self,
        pack: Pack,
        variant: str,
        *,
        dataset: str = "test",
        key: Ed25519PrivateKey = KEY,
        labelled_by: str | None = LABELLED_BY,
        failed: int = 0,
        first_format: bool = False,
    ) -> dict[str, Any]:
        """A signed evaluation of `pack` with `reader=variant`, with what a run varies pinned."""
        decision = Decision(pack, providers={"reader": variant}, origin_cap=False)
        run = evaluation(decision, resolve_dataset(pack, dataset))
        built = evaluations.build(pack, run, key=key, at=SIGNED_AT, labelled_by=labelled_by)
        fields = {k: v for k, v in built.items() if k not in ("key_id", "signature")}
        measured = REMOTE if built["report"]["remote_consent"] else LOCAL
        fields["engine"] = "0.1.0"
        fields["claimed"] = {**measured, "failed_cases": failed}
        if first_format:
            fields["format"] = evaluations.FORMAT_V1
            del fields["engine"], fields["claimed"]["failed_cases"]
            fields["dataset"] = {k: fields["dataset"][k] for k in ("name", "sha256", "n")}
        return evaluations.sign(fields, key)

    def publish(self, pack: Pack, *variants: str, **how: Any) -> list[dict[str, Any]]:
        records = [self.record(pack, variant, **how) for variant in variants]
        for record in records:
            sent = self.client.post(
                f"/v1/agents/{pack.id}/{pack.version}/evaluations", json=record, headers=self.auth
            )
            assert sent.status_code == 201, sent.text
        return records

    def rewrite(self, record: dict[str, Any], changed: dict[str, Any]) -> None:
        """A stored record changed behind the registry's back, as someone with the database
        could change it."""
        body = json.dumps(changed, sort_keys=True, separators=(",", ":"))
        with self.store._db.session(exclusive=True) as db:
            db.all(
                "UPDATE hub_evaluations SET body = ? WHERE id = ?",
                (body, evaluations.evaluation_id(record)),
            )

    def answers(self, name: str, version: str | None = None) -> dict[str, Any]:
        base = f"/v1/agents/{NAMESPACE}/{name}"
        shown = self.client.get(
            f"{base}/evaluation", params={"version": version} if version else {}
        )
        assert shown.status_code == 200, shown.text
        image = self.client.get(f"{base}/badge.svg")
        assert image.status_code == 200, image.text
        return {"evaluation": shown.json(), "badge": image.text}


def flattered(record: dict[str, Any]) -> dict[str, Any]:
    """The record with every critical case detected and none automated: what someone who
    wanted the rules to look safe would write."""
    forged = copy.deepcopy(record)
    for label in forged["report"]["decisions"]["critical"]:
        label.update(detected=label["n"], auto_missed=0)
    return forged


def with_variant(name: str, pattern: str | None = None) -> Edit:
    """A third way to read the messages: the `rules` variant again under `name`, with one more
    pattern for urgent symptoms (none: the same rules)."""

    def edit(manifest: dict[str, Any], root: Path) -> None:
        config = f"logic/{name}.yaml"
        manifest["providers"]["reader"]["variants"][name] = {"type": "rules", "config": config}
        rules = yaml.safe_load((root / "logic" / "rules.yaml").read_text(encoding="utf-8"))
        if pattern is not None:
            rules["questions"]["urgent_symptoms"]["labels"]["yes"].append(
                {"name": "more", "pattern": pattern, "weight": 6}
            )
        (root / config).write_text(yaml.safe_dump(rules), encoding="utf-8")

    return edit


def short_dataset(manifest: dict[str, Any], root: Path) -> None:
    """A second dataset, `short`: the first 300 cases, with fewer than 100 critical ones."""
    lines = (root / "data" / "test.jsonl").read_text(encoding="utf-8").splitlines()[:300]
    (root / "data" / "short.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest["evaluation"]["datasets"]["short"] = "data/short.jsonl"


def no_critical_label(manifest: dict[str, Any], root: Path) -> None:
    manifest["evaluation"].pop("critical")


def hostile_name(manifest: dict[str, Any], root: Path) -> None:
    manifest["name"] = HOSTILE_NAME


def generate() -> dict[str, Any]:
    """Every case's answers, by the address the Hub asks at: `states/<name>`, and
    `states/<name>@<version>` where a case is of an earlier version."""
    with pinned(), tempfile.TemporaryDirectory() as scratch:
        world = World(Path(scratch))
        try:
            return {
                "about": "Written by tests/support/evaluation_pages.py: a registry's own "
                "answers, for the Agent Hub's tests. Synthetic data only.",
                "agents": _cases(world),
            }
        finally:
            world.close()


def _cases(world: World) -> dict[str, Any]:
    cases: dict[str, Any] = {}

    def keep(name: str, version: str | None = None) -> None:
        key = f"{NAMESPACE}/{name}" + (f"@{version}" if version else "")
        cases[key] = world.answers(name, version)

    world.agent("none")
    keep("none")

    world.publish(world.agent("one"), "rules")
    keep("one")

    world.publish(world.agent("two"), "rules", "agent")
    keep("two")

    several = world.agent("several", with_variant("cautious", "breath|chest|face|numb|weak"))
    world.publish(several, "rules", "cautious", "agent")
    keep("several")

    # Close: one more pattern catches a few more critical cases, inside the intervals.
    close = world.agent("overlap", with_variant("wider", "catch my breath|pressure in my chest"))
    world.publish(close, "rules", "wider")
    keep("overlap")

    # Clear: rules that read every message as urgent send every case to a person, so none of
    # the critical ones is automated (and nothing else is either, which the page also shows).
    world.publish(world.agent("clear", with_variant("careful", ".")), "rules", "careful")
    keep("clear")

    # Clean: those careful rules alone, so no configuration automated a critical case.
    world.publish(world.agent("clean", with_variant("careful", ".")), "careful")
    keep("clean")

    world.publish(world.agent("tied", with_variant("same")), "rules", "same")
    keep("tied")

    world.publish(world.agent("insufficient", short_dataset), "rules", "agent", dataset="short")
    keep("insufficient")

    world.publish(world.agent("no-critical", no_critical_label), "rules", "agent")
    keep("no-critical")

    world.publish(world.agent("first-format"), "agent", first_format=True)
    keep("first-format")

    world.publish(world.agent("failed"), "agent", failed=3)
    keep("failed")

    world.publish(world.agent("hostile", hostile_name), "agent", labelled_by=HOSTILE_LABELLER)
    keep("hostile")

    rules, _ = world.publish(world.agent("tampered"), "rules", "agent")
    world.rewrite(rules, flattered(rules))
    keep("tampered")

    [only] = world.publish(world.agent("unverified"), "rules")
    world.rewrite(only, flattered(only))
    keep("unverified")

    world.publish(world.agent("revoked"), "agent", key=OTHER_KEY)
    world.store.revoke_key(
        NAMESPACE, archive.key_id(OTHER_KEY.public_key()), reason="lost", by="operator"
    )
    keep("revoked")

    recalled = world.agent("recalled")
    world.publish(recalled, "agent")
    found = world.store.version(NAMESPACE, "recalled", recalled.version)
    signed = yank.sign(
        pack=recalled.id,
        version=recalled.version,
        digest=found["digest"],
        archive_sha256=found["archive_sha256"],
        reason="A fixture's recall",
        severity="high",
        signer="publisher",
        key=KEY,
        key_id=archive.key_id(KEY.public_key()),
    )
    sent = world.client.post(
        f"/v1/agents/{recalled.id}/{recalled.version}/yank",
        json={"record": signed},
        headers=world.auth,
    )
    assert sent.status_code == 201, sent.text
    keep("recalled")

    # Two versions: the earlier one evaluated, the newest one not yet.
    world.publish(world.agent("older", version="1.0.0"), "rules", "agent")
    world.agent("older", version="2.0.0")
    keep("older")
    keep("older", "1.0.0")
    return cases


_HEX = re.compile(r"[0-9a-f]{8,}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def shape(value: Any) -> Any:
    """`value` with what the counts decide taken out: every number as 0, and in a string
    every run of hex digits (an id, a digest, a key) as `#` and every number as `0`. Two
    answers of one shape say the same things in the same words about different counts."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return 0
    if isinstance(value, str):
        return _NUMBER.sub("0", _HEX.sub("#", value))
    if isinstance(value, list):
        return [shape(item) for item in value]
    return {key: shape(item) for key, item in value.items()}


def dumped(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, indent=1, ensure_ascii=True, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when the file is not current")
    args = parser.parse_args(argv)
    fixture = generate()
    if args.check:
        committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if shape(committed) != shape(fixture):
            print(f"{FIXTURE.relative_to(ROOT)} is not what the registry answers now: rewrite it")
            return 1
        return 0
    FIXTURE.write_text(dumped(fixture), encoding="utf-8")
    states = sorted({case["evaluation"]["state"] for case in fixture["agents"].values()})
    print(f"wrote {FIXTURE.relative_to(ROOT)}: {len(fixture['agents'])} answers ({states})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

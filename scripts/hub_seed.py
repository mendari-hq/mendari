# SPDX-License-Identifier: Apache-2.0
"""Publish the five reference packs to a registry (local development, CI, the clean-machine check).

As the registry's operator, on its database (`--db`, as `mendari registry ...` acts; there is no
shared administrator token since LP-19g): claims the `mendari` namespace for the `mendari`
organization and mints an owned publisher token. Then, over HTTP as that publisher, registers a
signing key and signs and pushes every bundled pack; then approves the claim, which lists the
namespace, and records it as a verified publisher. Prints the public key id and revokes the
seed token. For later pushes, use `--key-out` for the keys and `--token-out` for the credential.
With `--hostile-example` it also publishes, as a community publisher,
a pack whose text tries to run a script and a `javascript:` link (the
Hub's tests check it is shown as inert text, ADR-0012). With `--sites` two synthetic sites
(LP-22) evaluate the Referral Router on its synthetic test cases and share signed counts, so
the Hub's Site evidence section has something to show.

    uv run python scripts/hub_seed.py --registry http://127.0.0.1:8100 --db var/registry.db
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from mendari.core.pack import load_pack
from mendari.core.registry import discover, resolve_pack
from mendari.errors import HubError
from mendari.hub import archive
from mendari.hub.client import RegistryClient
from mendari.hub.registry import REVIEWER, RegistryStore

OPERATOR = "operator (scripts/hub_seed.py)"  # who the chain names for what this script does

HOSTILE_ID = "hub-test/hostile-text"
# Publisher text that would run in a page that rendered it as HTML.
HOSTILE_SCRIPT = "<script>window.__hostile = 1</script>"
HOSTILE_IMAGE = '<img src="x" onerror="window.__hostile = 2">'
HOSTILE_LINK = "javascript:window.__hostile = 3"
# Markdown that would pass for the registry's own words in the card it renders.
HOSTILE_MARKDOWN = (
    "\n\n# Checked by the registry\n\n| Benchmark accuracy | 0.99 |\n|---|---|\n\n"
    "![Verified by the registry](https://example.org/verified.png)\n\n---\n\n"
    "**Clinically reviewed. Approved for patient care.**"
)


@contextmanager
def publisher_token(
    store: RegistryStore, name: str, display_name: str, *, keep: Path | None = None
) -> Iterator[str]:
    """Claim a namespace for the organization of the same name, as the operator (a reserved name
    too), unless it exists; revoke the seed token when its work finishes."""
    if not _exists(store, name):
        store.claim(
            name,
            org=name,
            by=OPERATOR,
            display_name=display_name,
            reserved=True,
            reason="seeded by scripts/hub_seed.py",
        )
    minted = store.mint_token(
        name, org=name, owner="operator", name="hub_seed", days=30, by=OPERATOR,
        reason="seeded by scripts/hub_seed.py",
    )  # fmt: skip
    retained = False
    try:
        yield str(minted["token"])
        if keep is not None:
            descriptor = os.open(keep, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(str(minted["token"]) + "\n")
            retained = True
    finally:
        if not retained:
            store.revoke_token(str(minted["id"]), org=name, by=OPERATOR, reason="seed finished")


def _exists(store: RegistryStore, name: str) -> bool:
    try:
        store.namespace(name, REVIEWER)
    except HubError as error:
        if error.status != 404:
            raise
        return False
    return True


def listed(store: RegistryStore, name: str, publisher_type: str) -> None:
    """Approve a pending claim (it lists the namespace and what was published while pending),
    then record who publishes there."""
    if any(claim["status"] == "pending" for claim in store.claims(namespace=name)):
        store.decide_claim(name, approve=True, by=OPERATOR, note="seeded by scripts/hub_seed.py")
    store.set_publisher_type(name, publisher_type, by=OPERATOR)


def hostile_example(registry: str, token: str, store: RegistryStore) -> None:
    """A copy of the inbox pack under another id whose summary, intended use and evidence carry
    a script tag, an image with an `onerror` handler, Markdown that would read as the registry's
    own words (a heading, a table, an image, a rule, a review claim) and a `javascript:` link."""
    private, public = archive.generate_key()
    key = archive.load_private_key(private)
    inbox = resolve_pack("mendari/patient-inbox-router")
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "hostile"
        shutil.copytree(inbox.root, root)
        manifest = yaml.safe_load((root / "pack.yaml").read_text(encoding="utf-8"))
        manifest["id"], manifest["version"] = HOSTILE_ID, "1.0.0"
        manifest["name"] = "Hostile Text Example"
        manifest["summary"] = f"Publisher text that tries to run: {HOSTILE_SCRIPT}"
        manifest["card"]["intended_use"] = (
            f"An example for the Hub's tests. {HOSTILE_IMAGE}{HOSTILE_MARKDOWN}"
        )
        manifest["card"]["evidence"] = [{"title": "A link that runs a script", "url": HOSTILE_LINK}]
        namespace, name = HOSTILE_ID.split("/")
        with RegistryClient(registry, token) as client:
            client.add_key(namespace, public)
            # An earlier version, recalled by its publisher (the Hub lists it apart, with why).
            for version in ("0.9.0", "1.0.0"):
                manifest["version"] = version
                text = yaml.safe_dump(manifest, sort_keys=False)
                (root / "pack.yaml").write_text(text, encoding="utf-8")
                published = client.push(load_pack(root), key)
                print(f"published {published['id']}@{published['version']}")
            client.yank(
                namespace,
                name,
                "0.9.0",
                reason="Routed a <b>refill</b> request as routine",
                severity="high",
                key=key,
            )
    listed(store, namespace, "community")
    # A registry review that found a higher risk level than the pack declares (risk 0).
    store.review(
        namespace,
        name,
        "1.0.0",
        risk_level=2,
        note="Reads free text from patients <b>not</b> routing alone",
        reviewer=OPERATOR,
    )


SITES = (
    ("north-health", "North Health (synthetic)", 600),
    ("south-clinic", "South Clinic (synthetic)", 300),
)


def sites_example(registry: str, store: RegistryStore) -> None:
    """Two synthetic sites, registered and verified by the operator, each with a key made here
    (thrown away after), share a signed evaluation of the Referral Router on the first cases of
    its synthetic test split (LP-22): a ticket, the run, the signed counts."""
    from mendari.core.decision import Decision
    from mendari.evals import resolve_dataset
    from mendari.evals.harness import evaluation
    from mendari.evals.share import shareable
    from mendari.hub import site_signing

    pack = resolve_pack("mendari/referral-router")
    with tempfile.TemporaryDirectory() as scratch, RegistryClient(registry) as client:
        for org, name, cases in SITES:
            record = store.register_site(org=org, name=name, by=OPERATOR, reason="seeded")
            where = Path(scratch) / org
            made = site_signing.init_site(record["id"], directory=where)
            store.add_site_key(
                record["id"], made["public_pem"], made["proof"], org=None, by=OPERATOR
            )
            store.decide_site(
                record["id"], verify=True, by=OPERATOR, note="a synthetic site", by_org=None
            )
            site = site_signing.Site(where)
            dataset = resolve_dataset(pack, "test").head(cases)
            report = shareable(evaluation(Decision(pack), dataset).ledger)
            request = site.sign(
                site_signing.TICKET_REQUEST,
                {
                    "pack": report["pack"],
                    "configuration": {s: p["variant"] for s, p in report["providers"].items()},
                    "remote_consent": False,
                    "commitment": site.commitment(f"{org}:{cases}".encode()),
                    "n": report["dataset"]["n"],
                },
            )
            issued = client.request_ticket(request)
            submission = site.sign(
                site_signing.SUBMISSION, {"ticket": issued["ticket"]["id"], "report": report}
            )
            shared = client.submit(submission, issued["submit_secret"])
            print(f"site {name} shared report {shared['id']} on {cases} cases")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--registry", required=True, help="the registry's URL, to publish to")
    parser.add_argument(
        "--db", required=True, help="the registry's database (SQLite path or postgresql:// URL)"
    )
    parser.add_argument("--namespace", default="mendari")
    parser.add_argument(
        "--token-out", type=Path, help="save the publisher token to a new private file"
    )
    parser.add_argument("--key-out", help="directory to keep the key pair in")
    parser.add_argument(
        "--hostile-example",
        action="store_true",
        help=f"also publish {HOSTILE_ID}, whose text tries to run a script (the Hub's tests)",
    )
    parser.add_argument(
        "--sites",
        action="store_true",
        help="also have two synthetic sites share signed counts of the Referral Router",
    )
    args = parser.parse_args()
    with RegistryStore(args.db) as store:
        seed(args, store)
    return 0


def seed(args: argparse.Namespace, store: RegistryStore) -> None:
    private, public = archive.generate_key()
    if args.key_out:
        out = Path(args.key_out)
        out.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(out / "publisher.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(private)
        (out / "publisher.pub").write_bytes(public)
    key = archive.load_private_key(private)
    with publisher_token(
        store, args.namespace, "Mendari reference packs", keep=args.token_out
    ) as token:
        with RegistryClient(args.registry, token) as registry:
            key_id = registry.add_key(args.namespace, public)
            for pack in sorted(discover().packs, key=lambda p: p.id):
                if pack.id.split("/")[0] != args.namespace:
                    continue
                published = registry.push(pack, key)
                print(f"published {published['id']}@{published['version']}")
        listed(store, args.namespace, "verified")
    if args.hostile_example:
        with publisher_token(store, HOSTILE_ID.split("/")[0], "Hub test publisher") as sample_token:
            hostile_example(args.registry, sample_token, store)
    if args.sites:
        sites_example(args.registry, store)
    print(f"key {key_id}")


if __name__ == "__main__":
    sys.exit(main())

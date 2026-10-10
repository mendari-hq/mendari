# SPDX-License-Identifier: Apache-2.0
"""Publish the five reference packs to a registry (local development, CI, the clean-machine check).

Creates the `mendari` namespace as a verified publisher, a publisher token and a signing key,
then signs and pushes every bundled pack. Prints the token and key id; with `--key-out` it keeps
the key pair for later pushes. With `--hostile-example` it also publishes, as a community
publisher, a pack whose text tries to run a script and a `javascript:` link (the Hub's tests
check it is shown as inert text, ADR-0012).

    MENDARI_REGISTRY_ADMIN_TOKEN=... uv run python scripts/hub_seed.py --registry http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

from mendari.core.pack import load_pack
from mendari.core.registry import discover, resolve_pack
from mendari.hub import archive
from mendari.hub.client import RegistryClient

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


def namespace(http: httpx.Client, name: str, display_name: str, publisher_type: str) -> str:
    """Create a namespace (or keep it), set its publisher type, and return a new token for it."""
    created = http.post("/v1/namespaces", json={"name": name, "display_name": display_name})
    if created.status_code not in (201, 409):
        created.raise_for_status()
    http.patch(f"/v1/namespaces/{name}", json={"publisher_type": publisher_type}).raise_for_status()
    return str(http.post(f"/v1/namespaces/{name}/tokens").raise_for_status().json()["token"])


def hostile_example(registry: str, token: str, admin: str) -> None:
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
    # A registry review that found a higher risk level than the pack declares (risk 0).
    review = {"risk_level": 2, "note": "Reads free text from patients <b>not</b> routing alone"}
    with httpx.Client(base_url=registry.rstrip("/"), timeout=60) as http:
        http.post(
            f"/v1/packs/{HOSTILE_ID}/1.0.0/review",
            json=review,
            headers={"authorization": f"Bearer {admin}"},
        ).raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--registry", required=True)
    parser.add_argument("--namespace", default="mendari")
    parser.add_argument("--key-out", help="directory to keep the key pair in")
    parser.add_argument(
        "--hostile-example",
        action="store_true",
        help=f"also publish {HOSTILE_ID}, whose text tries to run a script (the Hub's tests)",
    )
    args = parser.parse_args()
    admin = os.environ.get("MENDARI_REGISTRY_ADMIN_TOKEN", "")
    if not admin:
        sys.exit("set MENDARI_REGISTRY_ADMIN_TOKEN")
    headers = {"authorization": f"Bearer {admin}"}
    with httpx.Client(base_url=args.registry.rstrip("/"), headers=headers, timeout=60) as http:
        token = namespace(http, args.namespace, "Mendari reference packs", "verified")
        hostile = (
            namespace(http, HOSTILE_ID.split("/")[0], "Hub test publisher", "community")
            if args.hostile_example
            else None
        )
    private, public = archive.generate_key()
    if args.key_out:
        out = Path(args.key_out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "publisher.pub").write_bytes(public)
        descriptor = os.open(out / "publisher.key", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(private)
    key = archive.load_private_key(private)
    with RegistryClient(args.registry, token) as registry:
        key_id = registry.add_key(args.namespace, public)
        for pack in sorted(discover().packs, key=lambda p: p.id):
            if pack.id.split("/")[0] != args.namespace:
                continue
            published = registry.push(pack, key)
            print(f"published {published['id']}@{published['version']}")
    if hostile is not None:
        hostile_example(args.registry, hostile, admin)
    print(f"token {token}\nkey {key_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

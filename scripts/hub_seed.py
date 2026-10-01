"""Publish the five reference packs to a registry (local development, CI, the clean-machine check).

Creates the `mendari` namespace as a verified publisher, a publisher token and a signing key,
then signs and pushes every bundled pack. Prints the token and key id; with `--key-out` it keeps
the key pair for later pushes.

    MENDARI_REGISTRY_ADMIN_TOKEN=... uv run python scripts/hub_seed.py --registry http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from mendari.core.registry import discover
from mendari.hub import archive
from mendari.hub.client import RegistryClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--registry", required=True)
    parser.add_argument("--namespace", default="mendari")
    parser.add_argument("--key-out", help="directory to keep the key pair in")
    args = parser.parse_args()
    admin = os.environ.get("MENDARI_REGISTRY_ADMIN_TOKEN", "")
    if not admin:
        sys.exit("set MENDARI_REGISTRY_ADMIN_TOKEN")
    headers = {"authorization": f"Bearer {admin}"}
    with httpx.Client(base_url=args.registry.rstrip("/"), headers=headers, timeout=60) as http:
        created = http.post(
            "/v1/namespaces",
            json={"name": args.namespace, "display_name": "Mendari reference packs"},
        )
        if created.status_code not in (201, 409):
            created.raise_for_status()
        http.patch(
            f"/v1/namespaces/{args.namespace}", json={"publisher_type": "verified"}
        ).raise_for_status()
        token = (
            http.post(f"/v1/namespaces/{args.namespace}/tokens").raise_for_status().json()["token"]
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
    print(f"token {token}\nkey {key_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

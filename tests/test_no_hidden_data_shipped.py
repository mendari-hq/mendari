# SPDX-License-Identifier: Apache-2.0
"""Hidden evaluation sets live in the deployment's database only (the owner's decision of
2026-10-07, ADR-0011): no file this repository tracks holds a sealed evaluation-set value, or a
value for any of the three evaluation-set key variables. Every evaluation-set token names its key
(`v2:<key id>:`), so one is found by its form; the only tokens tracked are the PHI policy's fixed
test vectors (`tests/test_evalset_keys.py`), sealed under a key made of the bytes 0 to 31."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from mendari.core.phi import key_id

ROOT = Path(__file__).resolve().parents[1]
# A sealed token: `v2:`, a key id, then at least a nonce and a tag (28 bytes) in base64.
TOKEN = re.compile(rb"v2:([0-9a-f]{16}):[A-Za-z0-9+/]{38,}={0,2}")
# A key variable given a value: 32 bytes in base64 (44 characters), alone or in a list.
KEY_VALUE = re.compile(
    rb"MENDARI_EVALSET_(?:KEY|READ_KEYS|INDEX_KEY)['\"]?\s*[:=,]\s*['\"]?[A-Za-z0-9+/]{43}="
)
# The PHI policy's fixed test vectors' key (`FIXED_KEY` in tests/test_evalset_keys.py).
FIXED_TEST_KEYS = {key_id(bytes(range(32)))}


def tracked() -> dict[str, bytes]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    files = {}
    for name in listed.stdout.decode().split("\0"):
        path = ROOT / name
        if name and path.is_file():
            files[name] = path.read_bytes()
    return files


def test_no_tracked_file_holds_a_sealed_token_or_an_evalset_key() -> None:
    files = tracked()
    assert len(files) > 500  # the whole tree, not an empty listing
    found = []
    for name, data in files.items():
        for match in TOKEN.finditer(data):
            if match.group(1).decode() not in FIXED_TEST_KEYS:
                found.append(f"{name}: a sealed token under key {match.group(1).decode()}")
        if KEY_VALUE.search(data):
            found.append(f"{name}: a value for an evaluation-set key variable")
    assert found == []


def test_the_patterns_find_what_they_look_for() -> None:
    import base64
    import secrets

    from mendari.core.evalset_keys import EvalsetKeys

    keys = EvalsetKeys(secrets.token_bytes(32), secrets.token_bytes(32))
    set_id = "6f1f3b1c-7f4e-4a52-9a0e-0d6c1c2b3a41"
    for value in ({}, "x", {"message": {"body": "a hand-written case"}}):
        token = keys.seal("header", value, set_id).encode()
        assert (match := TOKEN.search(b"sealed = '" + token + b"'")) is not None
        assert match.group(1).decode() == key_id(keys.key)
    key = base64.b64encode(secrets.token_bytes(32))
    for written in (
        b"MENDARI_EVALSET_KEY=" + key,
        b"export MENDARI_EVALSET_INDEX_KEY='" + key + b"'",
        b'"MENDARI_EVALSET_READ_KEYS": "' + key + b'"',
        b"MENDARI_EVALSET_READ_KEYS=" + key + b"," + key,
    ):
        assert KEY_VALUE.search(written), written
    for allowed in (b"MENDARI_EVALSET_KEY=", b"set MENDARI_EVALSET_KEY and the index key"):
        assert not KEY_VALUE.search(allowed)

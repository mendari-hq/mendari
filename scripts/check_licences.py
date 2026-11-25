# SPDX-License-Identifier: Apache-2.0
"""The licence of every dependency Mendari ships with or runs on, checked against an allow-list
(LP-14). Offline: it reads installed package metadata and the Hub's lockfile, never a service.

- **Python:** the runtime closure of `mendari` (with the `server` extra), walked through each
  installed distribution's requirements with their markers, as uv.lock installs it. A licence is
  read from `License-Expression` (PEP 639), else the `License` field when it is an SPDX id, else
  the trove classifiers.
- **The Hub (`hub/package-lock.json`):** every package that is not a development dependency.

A licence passes when it is permissive (`PERMISSIVE`), or when the package is one of the named
`EXCEPTIONS`, each with the reason it is accepted. Anything else, an unknown licence included,
fails with the package named. An `OR` expression passes when one choice does; `AND` when all do.

    uv run python scripts/check_licences.py          # the inventory, and exit 1 on a failure
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement

from mendari.core.spec import is_spdx_expression

ROOT = Path(__file__).resolve().parent.parent
HUB_LOCK = ROOT / "hub" / "package-lock.json"

PERMISSIVE = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BlueOak-1.0.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MIT-0",
        "PSF-2.0",
        "Python-2.0",
        "Unlicense",
        "Zlib",
    }
)
# SPDX ids of copyleft licences, read as stated (and then failed unless excepted).
KNOWN_COPYLEFT = re.compile(
    r"^(?:A?GPL|LGPL)-\d\.\d(?:-only|-or-later|\+)?$|^(?:MPL|EPL|CDDL|EUPL)-\d\.\d$|^CC-BY-SA-"
)
# A `License` field in words that names copyleft: unreadable, so it fails closed.
COPYLEFT_WORDS = re.compile(
    r"\bA?GPL|\bLGPL|General Public|Affero|\bMPL\b|Mozilla Public|\bEUPL|\bCDDL|\bEPL\b|"
    r"Eclipse Public|copyleft|ShareAlike",
    re.IGNORECASE,
)
# A trove classifier's licence, when that is all a package says. "BSD License" names no clause
# count; every BSD variant is permissive.
CLASSIFIERS = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT-0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
}
_LGPL_DRIVER = (
    "LGPL-3.0-only, unmodified and imported as its own package, never copied into Mendari's "
    "code: pip and uv install it from PyPI beside Mendari, and the published container images "
    "carry it, so they distribute it, with the native libraries psycopg-binary bundles (libpq, "
    "OpenSSL 3 and 1.1.1, libxcrypt and keyutils (LGPL-2.1+), Cyrus SASL, OpenLDAP, MIT Kerberos, "
    "PCRE2, libselinux; its SBOM lists them). The "
    "PostgreSQL driver of the hosted and on-prem deployments; moving it to an optional extra "
    "changes how production installs (the owner's decision), and what distributing it in the "
    "images requires is counsel's question (tasks/todo.md, LP-14)"
)
_SHARP = (
    "sharp's prebuilt libvips for one platform, an optional dependency of Next.js image "
    "optimization: libvips is LGPL-3.0-or-later, dynamically linked and unmodified, and only "
    "the platform's own package is installed"
)
# package name -> (the licence expression accepted, why). Exact names; a `*` ends a prefix.
EXCEPTIONS: dict[str, tuple[str, str]] = {
    "certifi": (
        "MPL-2.0",
        "MPL-2.0 is file-level copyleft: Mozilla's CA bundle, used unmodified as a separate "
        "package (httpx's certificates)",
    ),
    "psycopg": ("LGPL-3.0-only", _LGPL_DRIVER),
    "psycopg-binary": ("LGPL-3.0-only", _LGPL_DRIVER),
    "psycopg-pool": ("LGPL-3.0-only", _LGPL_DRIVER),
    "@img/sharp-libvips-*": ("LGPL-3.0-or-later", _SHARP),
    "@img/sharp-win32-*": ("Apache-2.0 AND LGPL-3.0-or-later", _SHARP),
    "@img/sharp-wasm32": ("Apache-2.0 AND LGPL-3.0-or-later AND MIT", _SHARP),
    "caniuse-lite": (
        "CC-BY-4.0",
        "browser-support data read at build time by browserslist (credited by its package); "
        "none of it reaches the Hub's pages",
    ),
}


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    licence: str | None  # an SPDX expression, or None when the metadata names none we know


def allowed(expression: str) -> bool:
    """Whether an SPDX expression is permissive: `OR` needs one choice, `AND` all of them.
    Parentheses are read as grouping; `WITH` and anything unparseable fail."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expression)
    position = 0

    def term() -> bool:
        nonlocal position
        if position >= len(tokens):
            raise ValueError(expression)
        token = tokens[position]
        position += 1
        if token == "(":
            value = either()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError(expression)
            position += 1
            return value
        if token in {")", "AND", "OR", "WITH"}:
            raise ValueError(expression)
        return token in PERMISSIVE

    def both() -> bool:
        nonlocal position
        value = term()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            value = term() and value
        return value

    def either() -> bool:
        nonlocal position
        value = both()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            value = both() or value
        return value

    try:
        result = either()
    except ValueError:
        return False
    return result and position == len(tokens)


def exception(name: str) -> tuple[str, str] | None:
    for pattern, accepted in EXCEPTIONS.items():
        if name == pattern or (pattern.endswith("*") and name.startswith(pattern[:-1])):
            return accepted
    return None


def problems(packages: list[Package]) -> list[str]:
    """Each package whose licence is unknown or not allowed, named."""
    found = []
    for package in packages:
        accepted = exception(package.name)
        if package.licence is None:
            found.append(f"{package.name} {package.version}: no licence we can read")
        elif accepted is not None:
            if package.licence != accepted[0]:
                found.append(
                    f"{package.name} {package.version}: {package.licence}, but its exception "
                    f"accepts {accepted[0]} only"
                )
        elif not allowed(package.licence):
            found.append(f"{package.name} {package.version}: {package.licence} is not allowed")
    return found


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_licence(distribution: metadata.Distribution) -> str | None:
    """A distribution's licence as an SPDX expression, or None when its metadata says none we can
    read. A stated licence wins: `License-Expression`, else a `License` field that is an SPDX
    expression of licences we know. A `License` field naming a copyleft licence in any other words
    (`GPLv3`, the GNU text) is unreadable: None, never the classifiers. Only a package that states
    nothing we can use is read from its trove classifiers, all of which must be known, joined with
    `AND`: classifiers list a package's parts, never a choice, so every one must pass."""
    fields = distribution.metadata
    expression = (fields.get_all("License-Expression") or [""])[0]
    if expression:
        return " ".join(expression.split())
    stated = (fields.get_all("License") or [""])[0].strip()
    if "\n" not in stated and is_spdx_expression(stated):
        ids = [token for token in re.findall(r"[^\s()]+", stated) if token not in {"AND", "OR"}]
        if all(token in PERMISSIVE or KNOWN_COPYLEFT.match(token) for token in ids):
            return " ".join(stated.split())
    if COPYLEFT_WORDS.search(stated):
        return None
    named = [
        CLASSIFIERS.get(c.split(" :: ")[-1])
        for c in fields.get_all("Classifier") or []
        if c.startswith("License :: ")
    ]
    if named and None not in named:
        return " AND ".join(sorted({n for n in named if n}))
    return None


def python_closure(root: str = "mendari", extras: tuple[str, ...] = ()) -> list[Package]:
    """The installed runtime closure of `root` with `extras`, each requirement's marker evaluated
    for this interpreter and the extras that asked for it."""
    wanted: dict[str, set[str]] = {}
    pending = [(root, set(extras))]
    while pending:
        name, asked = pending.pop()
        key = _canonical(name)
        if key in wanted and asked <= wanted[key]:
            continue
        wanted.setdefault(key, set()).update(asked)
        for line in metadata.distribution(name).requires or []:
            requirement = Requirement(line)
            # `evaluate` reads this interpreter's environment, with `extra` set as given.
            needed = requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in ("", *wanted[key])
            )
            if needed:
                pending.append((requirement.name, set(requirement.extras)))
    found = []
    for key in sorted(wanted):
        distribution = metadata.distribution(key)
        found.append(Package(key, distribution.version, python_licence(distribution)))
    return found


def hub_packages(lock: Path = HUB_LOCK) -> list[Package]:
    """Every package of the Hub's lockfile that is not a development dependency."""
    packages = json.loads(lock.read_text())["packages"]
    found = []
    for path, entry in sorted(packages.items()):
        if not path or entry.get("dev"):
            continue  # "" is the Hub itself
        name = path.rsplit("node_modules/", 1)[-1]
        licence = entry.get("license")
        found.append(
            Package(name, entry.get("version", "?"), licence if isinstance(licence, str) else None)
        )
    return found


def main() -> int:
    failed = False
    for title, packages in (("Python runtime", python_closure()), ("Hub", hub_packages())):
        found = problems(packages)
        print(f"{title}: {len(packages)} packages, {len(found)} not allowed")
        for line in found:
            print(f"  {line}", file=sys.stderr)
        failed = failed or bool(found)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

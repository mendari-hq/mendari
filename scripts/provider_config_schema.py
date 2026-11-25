# SPDX-License-Identifier: Apache-2.0
"""Write docs/spec/provider-configs.schema.json: the JSON Schema of every provider config an agent
may write (`mendari.providers.config_json_schema`, from the closed config models that
`load_agent` checks every variant and composite member against, LP-16e).

Usage:

    uv run python scripts/provider_config_schema.py          # write the schema
    uv run python scripts/provider_config_schema.py --check  # the file is what the code says
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mendari.providers import config_json_schema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "docs" / "spec" / "provider-configs.schema.json"


def render() -> str:
    """The schema as committed: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(config_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        if not SCHEMA.is_file() or SCHEMA.read_text(encoding="utf-8") != render():
            print(
                f"{SCHEMA.relative_to(ROOT)} is not what the provider config models say: run "
                "scripts/provider_config_schema.py",
                file=sys.stderr,
            )
            return 1
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    SCHEMA.write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

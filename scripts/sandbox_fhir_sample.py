# SPDX-License-Identifier: Apache-2.0
"""Write what the sandbox posts to its FHIR server, for the HL7 validator (CI job
`fhir-conformance`), each held to US Core 6.1.0: the bundled demo agent's portal messages, as
`mendari sandbox seed` loads them (`sandbox-messages.json`), and, for each `--agent` (an id or a
directory), another message agent's cases, as `mendari sandbox seed --agent` loads them
(`sandbox-<its name>.json`).

    uv run python scripts/sandbox_fhir_sample.py var/fhir-samples
    uv run python scripts/sandbox_fhir_sample.py var/fhir-samples --agent path/to/a-message-agent
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mendari.core.agent import load_agent
from mendari.core.registry import resolve_agent
from mendari.sandbox import fhir_workflow

DEMO_AGENT_DIR = Path(__file__).resolve().parent.parent / "agents" / "portal-message-agent"
MESSAGES = "messages"  # the demo agent's sample
COUNT = 20  # cases a sample holds


def main(out: Path, agents: list[str] | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    samples = {MESSAGES: fhir_workflow.seed_bundle(load_agent(DEMO_AGENT_DIR), COUNT)}
    for given in agents or []:
        agent = resolve_agent(given)
        name = agent.id.rsplit("/", 1)[-1]
        if name in samples:
            raise SystemExit(f"sandbox sample: two samples would be named sandbox-{name}.json")
        samples[name] = fhir_workflow.seed_bundle(agent, COUNT)
    for name, bundle in samples.items():
        written = out / f"sandbox-{name}.json"
        written.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        print(f"{len(bundle['entry'])} resources written to {written}")


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("out", nargs="?", type=Path, default=Path("var/fhir-samples"))
    parser.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="AGENT",
        help="also write what the sandbox posts for this message agent, an id or a directory",
    )
    args = parser.parse_args(argv)
    main(args.out, args.agent)
    return 0


if __name__ == "__main__":
    sys.exit(cli())

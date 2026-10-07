# SPDX-License-Identifier: Apache-2.0
"""Fail on medium or high risks in an OWASP ZAP report (CI job `security`).

    uv run python scripts/zap_check.py zap.json

Low and informational alerts are listed but pass. A medium or high alert fails unless it is in
ACCEPTED below, with the reason it is an accepted risk (none is, today).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RISK = {0: "info", 1: "low", 2: "medium", 3: "high"}
ACCEPTED: dict[str, str] = {}  # ZAP plugin id -> why the risk is accepted


def main(path: Path) -> int:
    report = json.loads(path.read_text(encoding="utf-8"))
    failed = False
    for site in report.get("site", []):
        for alert in site.get("alerts", []):
            risk = int(alert.get("riskcode", 0))
            plugin = str(alert.get("pluginid"))
            blocking = risk >= 2 and plugin not in ACCEPTED
            failed |= blocking
            mark = "FAIL" if blocking else "ok  "
            level = RISK.get(risk, risk)
            print(f"{mark} [{level}] {alert.get('name')} ({plugin}), {alert.get('count')}x")
    print("medium or high risks found" if failed else "no medium or high risks")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))

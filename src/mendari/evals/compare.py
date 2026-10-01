"""Local validation: compare an evaluation on local data with the pack's published benchmark,
and say *where* performance degrades, not just by how much."""

from __future__ import annotations

from typing import Any

MIN_GROUP = 15  # smallest subgroup worth flagging; smaller ones are too noisy to act on
MIN_LABEL_SUPPORT = 10


def compare(local: dict[str, Any], baseline: dict[str, Any], tolerance: float) -> dict[str, Any]:
    primary = local["primary"]
    local_q, base_q = local["questions"].get(primary), baseline["questions"].get(primary)
    if local_q is None or base_q is None:
        return {
            "status": "unknown",
            "reason": f"both reports need ground truth for {primary!r}",
            "findings": [],
        }

    findings: list[dict[str, Any]] = []
    delta = local_q["accuracy"] - base_q["accuracy"]

    for label, local_m in local_q["per_label"].items():
        base_recall = base_q["per_label"].get(label, {}).get("recall")
        if local_m["support"] >= MIN_LABEL_SUPPORT and None not in (local_m["recall"], base_recall):
            drop = base_recall - local_m["recall"]
            if drop > 2 * tolerance:
                findings.append(
                    {
                        "kind": "label_recall",
                        "subject": f"label {label}",
                        "baseline": base_recall,
                        "local": local_m["recall"],
                        "n": local_m["support"],
                        "drop": drop,
                    }
                )

    for attribute, values in local.get("subgroups", {}).items():
        base_values = baseline.get("subgroups", {}).get(attribute, {})
        for value, local_m in values.items():
            if local_m["n"] < MIN_GROUP or local_m["accuracy"] is None:
                continue
            reference = base_values.get(value)
            base_acc = (
                reference["accuracy"]
                if reference and reference["n"] >= MIN_GROUP and reference["accuracy"] is not None
                else base_q["accuracy"]
            )
            drop = base_acc - local_m["accuracy"]
            if drop > 2 * tolerance:
                findings.append(
                    {
                        "kind": "subgroup",
                        "subject": f"{attribute}={value}",
                        "baseline": base_acc,
                        "local": local_m["accuracy"],
                        "n": local_m["n"],
                        "drop": drop,
                    }
                )

    local_far = local["decisions"]["automation"]["false_automation_rate"]
    base_far = baseline["decisions"]["automation"]["false_automation_rate"]
    if local_far is not None and base_far is not None and local_far - base_far > tolerance:
        findings.append(
            {
                "kind": "false_automation",
                "subject": "false automation rate",
                "baseline": base_far,
                "local": local_far,
                "n": local["decisions"]["automation"]["n"],
                "drop": local_far - base_far,
            }
        )

    local_ece = local_q["calibration"]["ece"]
    base_ece = base_q["calibration"]["ece"]
    if local_ece is not None and base_ece is not None and local_ece - base_ece > 0.05:
        findings.append(
            {
                "kind": "calibration",
                "subject": "calibration error (ECE)",
                "baseline": base_ece,
                "local": local_ece,
                "n": local_q["n"],
                "drop": local_ece - base_ece,
            }
        )

    for item in local["decisions"]["critical"]:
        if item["auto_missed"]:
            findings.append(
                {
                    "kind": "critical",
                    "subject": f"{item['question']}={item['label']} cases automated",
                    "baseline": 0,
                    "local": item["auto_missed"],
                    "n": item["n"],
                    "drop": float(item["auto_missed"]),
                }
            )

    findings.sort(key=lambda f: (f["kind"] != "critical", -f["drop"]))
    blocking = {"critical", "false_automation"}
    if delta < -tolerance or any(f["kind"] in blocking for f in findings):
        status = "degraded"
    elif findings:
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "tolerance": tolerance,
        "baseline": {
            "dataset": baseline["dataset"],
            "accuracy": base_q["accuracy"],
            "pack_version": baseline["pack"]["version"],
        },
        "local": {"dataset": local["dataset"], "accuracy": local_q["accuracy"]},
        "delta": delta,
        "findings": findings,
    }

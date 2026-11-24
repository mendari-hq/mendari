# SPDX-License-Identifier: Apache-2.0
"""Turn a YAML file of case templates into a JSON Lines dataset, so an agent's author makes
synthetic data without writing Python (docs/guides/contributing-an-agent.md).

    uv run python scripts/synthetic_cases.py community-agents/<name>/data/cases.yaml \\
        --out community-agents/<name>/data/examples.jsonl

Each template is an `input`, its `labels`, and optionally `fill`: for each placeholder, the
values it takes. Every `{placeholder}` in the input's text is replaced by each combination of
the values, in the order written, so the output is the same every time:

    - input: {message: {body: "Please refill my {drug}, I have {left} left."}}
      labels: {request: refill, urgent_symptoms: "no"}
      fill:
        drug: [metformin, lisinopril]
        left: [two pills, "none"]

makes four cases. Only the names under `fill` are replaced; other braces stay as written. A
name under `fill` that the input never uses is refused (it is usually a typo), and so is a file
that would make more than MAX_CASES cases. Write every value by hand: no real person's name,
date of birth, address, record number or message, ever.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

MAX_CASES = 5000  # an agent is at most 2 MB, data included


class TemplateError(ValueError):
    """A template that cannot be expanded, with what to fix."""


def expand(templates: Any, prefix: str = "case") -> Iterator[dict[str, Any]]:
    """Every case the templates make, numbered `<prefix>-0001`, ... in the order written."""
    if not isinstance(templates, list) or not templates:
        raise TemplateError("the file must be a list of templates (each starting with '- ')")
    count = 0
    for number, template in enumerate(templates, start=1):
        where = f"template {number}"
        if not isinstance(template, dict) or not isinstance(template.get("input"), dict):
            raise TemplateError(f"{where}: needs an `input` mapping")
        unknown = set(template) - {"input", "labels", "fill"}
        if unknown:
            raise TemplateError(f"{where}: unknown keys {sorted(unknown)}")
        labels = template.get("labels", {})
        if not isinstance(labels, dict):
            raise TemplateError(f"{where}: `labels` must map each question to its label")
        fill = template.get("fill") or {}
        if not isinstance(fill, dict) or not all(
            isinstance(values, list) and values for values in fill.values()
        ):
            raise TemplateError(f"{where}: `fill` must map each placeholder to a list of values")
        text = json.dumps(template["input"], ensure_ascii=False)
        unused = [name for name in fill if "{" + str(name) + "}" not in text]
        if unused:
            raise TemplateError(f"{where}: the input never uses {{{unused[0]}}}")
        names = list(fill)
        for values in itertools.product(*(fill[name] for name in names)):
            count += 1
            if count > MAX_CASES:
                raise TemplateError(f"more than {MAX_CASES} cases: use fewer values")
            chosen = dict(zip(names, values, strict=True))
            yield {
                "id": f"{prefix}-{count:04d}",
                "input": _filled(template["input"], chosen),
                "labels": labels,
            }


def _filled(value: Any, chosen: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        for name, replacement in chosen.items():
            value = value.replace("{" + str(name) + "}", str(replacement))
        return value
    if isinstance(value, dict):
        return {key: _filled(item, chosen) for key, item in value.items()}
    if isinstance(value, list):
        return [_filled(item, chosen) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("templates", type=Path, help="a YAML file of case templates")
    parser.add_argument("--out", type=Path, required=True, help="the JSON Lines file to write")
    parser.add_argument("--prefix", default="case", help="the cases' id prefix (default: case)")
    args = parser.parse_args(argv)
    try:
        templates = yaml.safe_load(args.templates.read_text(encoding="utf-8"))
        cases = list(expand(templates, args.prefix))
    except (OSError, yaml.YAMLError, TemplateError) as error:
        print(f"{args.templates}: {error}", file=sys.stderr)
        return 1
    lines = [json.dumps(case, ensure_ascii=False) for case in cases]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

## What

<!-- What changed, and why. Which roadmap item or issue it closes. -->

## Checklist

- [ ] One focused change, with tests for any behaviour change
- [ ] `uv run pytest`, `ruff`, `mypy` pass (and `pytest -m browser` for dashboard changes)
- [ ] No real patient data anywhere (synthetic only)
- [ ] For a pack: validate, test and eval pass. The card is honest, and the version and
      CHANGELOG are bumped. A clinical reviewer is named: <!-- who -->
- [ ] For the spec, a trust boundary or the risk policy: an ADR in `docs/decisions/`

# SPDX-License-Identifier: Apache-2.0
"""The mark on what a pack learned from a site: a case `mendari improve` added to it, and a
model built from such cases.

`mendari improve --write` (`evals/improve.py`) puts the cases a pack learned from and calibrated
on into the pack's own datasets. They are a site's cases and may be patient text, and the model
trained on them holds their words. So each says what it is:

- a case a run adds carries `attributes.added_by: "mendari improve"` in its record, and `apply`
  writes none that does not;
- a model trained on such a case carries the same in its `training` record
  (`evals/training.py` `fit`), until it is trained again without them.

Whatever would carry a pack somewhere else looks for that mark in every file it would carry:
an archive's builder and a registry's publish (`hub/archive.py` `refuse_learned_cases`), and
the community-pack check (`community/check.py`). A file is searched as bytes, never parsed, so
it is found whatever the file is called and however it is laid out: a dataset the pack trains
on, one it only tests with, a copy the manifest does not name, the same cases as one JSON
document, a record cut short. A file that only quotes the mark is counted too.

The mark is the only sign. A case that looks like a run's (by its id, by where it came from, by
an attribute of that name that says something else) is not counted. Neither is what the mark
is not on, or what no longer reads as JSON in UTF-8: a site's cases put into a pack by hand,
the file a run was given, a copy compressed or in another encoding.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

ADDED_BY = "added_by"
IMPROVE = "mendari improve"
# The mark as JSON spells it, in bytes, with or without the space a writer puts after the colon.
_ADDED = re.compile(
    rb'"%s"\s*:\s*"%s"' % (re.escape(ADDED_BY.encode()), re.escape(IMPROVE.encode()))
)


def cases_added(data: bytes) -> int:
    """How many times a file's bytes hold the mark: once for each case a run added, and once
    in a model built from such cases."""
    return sum(1 for _ in _ADDED.finditer(data))


def added(record: Mapping[str, Any]) -> bool:
    """Whether a dataset record says `mendari improve` added it, read as a file of it is."""
    return bool(cases_added(json.dumps(record).encode()))


def learned_cases(files: Mapping[str, bytes]) -> dict[str, int]:
    """The files (path -> content) that hold cases `mendari improve` added, or a model built
    from them, each with how many times it is marked (`cases_added`)."""
    counted = {path: cases_added(data) for path, data in files.items()}
    return {path: count for path, count in counted.items() if count}

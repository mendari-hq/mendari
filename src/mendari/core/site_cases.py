# SPDX-License-Identifier: Apache-2.0
"""The marks on a site's cases: what says that a record came from a site's own decisions, and
that a model was built from such records.

Of the commands that write a site's cases, which may be patient text, into files, two mark
each record they write with what it is:

- `mendari feedback export`, and the route that answers the same (`evals/feedback.py`), writes
  rated and reviewed decisions as dataset records. Each carries
  `attributes.exported_by: "mendari feedback export"`.
- `mendari improve --write` (`evals/improve.py`) puts the cases a pack learned from and
  calibrated on into the pack's own datasets. Each carries
  `attributes.added_by: "mendari improve"`, and `apply` writes none that does not.

A model fitted on a case with either mark, as one it learned from (whose words it holds) or one
it was calibrated on, carries the same marks in its `training` record (`evals/training.py`
`fit`), until it is trained again without them.

Whatever would carry a pack somewhere else looks for those marks in every file it would carry:
an archive's builder and a registry's publish (`hub/archive.py` `refuse_site_cases`). A file is
searched as bytes, never parsed, so a
mark is found whatever the file is called and however it is laid out: a dataset the pack trains
on, one it only tests with, the file an export wrote, a copy the manifest does not name, the
same cases as one JSON document, a record cut short. A file that only quotes a mark is counted
too.

The marks are the only sign. A case that looks like one of these (by its id, by where it came
from, by an attribute of that name that says something else) is not counted. Neither is what no
mark is on, or what no longer reads as JSON in UTF-8: the datasets `mendari omop dataset` and
`mendari bulk dataset` build from a site's records, cases a site labelled in a file of its own,
a case retyped into a pack or copied without its `attributes`, a copy compressed or in another
encoding.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

ADDED_BY = "added_by"
IMPROVE = "mendari improve"
EXPORTED_BY = "exported_by"
EXPORT = "mendari feedback export"
# Each mark: the attribute that carries it, and the command it names. In this order wherever
# several are written (a model's `training` record), so the same data gives the same bytes.
MARKS: Final[Mapping[str, str]] = {ADDED_BY: IMPROVE, EXPORTED_BY: EXPORT}
# Each mark as JSON spells it, in bytes, with or without the space a writer puts after the colon.
_SPELT = {
    key: re.compile(rb'"%s"\s*:\s*"%s"' % (re.escape(key.encode()), re.escape(value.encode())))
    for key, value in MARKS.items()
}


def times_marked(data: bytes) -> int:
    """How many times a file's bytes hold a mark: once for each case an export wrote or a run
    added, and once for each mark of a model built from such cases."""
    return sum(1 for spelt in _SPELT.values() for _ in spelt.finditer(data))


def carried(records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """The marks any of `records` (dataset records) carries, each read as a file of it is, in
    the order of `MARKS` whichever record comes first."""
    found: set[str] = set()
    for record in records:
        written = json.dumps(record).encode()
        found.update(key for key, spelt in _SPELT.items() if spelt.search(written))
    return {key: value for key, value in MARKS.items() if key in found}


def added(record: Mapping[str, Any]) -> bool:
    """Whether a dataset record says `mendari improve` added it, read as a file of it is."""
    return ADDED_BY in carried([record])


def marked_files(files: Mapping[str, bytes]) -> dict[str, int]:
    """The files (path -> content) that hold a site's cases, or a model built from them, each
    with how many times it is marked (`times_marked`)."""
    counted = {path: times_marked(data) for path, data in files.items()}
    return {path: count for path, count in counted.items() if count}

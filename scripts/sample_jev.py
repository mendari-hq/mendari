"""A Jev that finds no emergency, for the FHIR samples only: it answers in this process and
nothing is sent anywhere. The inbox automates a routine message only while a model screens it for
urgency (its policy's `automation_requires`), and the HL7 validator should see automated
decisions too.

    with calm_jev() as providers:
        decide = Decision.from_hub("mendari/patient-inbox-router", providers=providers)
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

import httpx

from mendari.providers.jev import JevProvider

SETTINGS = {
    "JEV_API_URL": "https://jev.example/v1/decide",  # a reserved name: never called
    "JEV_MODEL": "jev-sample",
    "JEV_API_KEY": "sample",
}
# The inbox providers whose decisions may be automated.
SCREENED = {"safety_screen": "rules_and_jev"}


def _routine(request: httpx.Request) -> httpx.Response:
    questions = set(json.loads(request.content)["questions"])
    if questions != {"urgency"}:  # loudly: a sample built on a refused call would be wrong
        raise AssertionError(f"sample_jev answers the inbox's urgency only, not {questions}")
    probabilities = {"urgent": 0.02, "routine": 0.98}
    answer = {"type": "choice", "choice": "routine", "confidence": 0.98}
    usage = {"input_tokens": 100, "output_tokens": 10}
    answers = {"urgency": {**answer, "probabilities": probabilities}}
    return httpx.Response(200, json={"model": "jev-sample", "answers": answers, "usage": usage})


@contextmanager
def calm_jev() -> Iterator[dict[str, str]]:
    """While open, every Jev call gets a confident `routine` urgency; yields the inbox providers
    that use it."""
    original = JevProvider.__init__

    def init(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(_routine))
        original(self, *args, **kwargs)

    with mock.patch.dict(os.environ, SETTINGS), mock.patch.object(JevProvider, "__init__", init):
        yield dict(SCREENED)

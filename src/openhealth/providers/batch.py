"""Per-decision call batching: remote providers that share an endpoint answer in one call.

Without it, a decision whose router and safety screen both use Jev makes two HTTP calls with
the same message. Inside `batching(providers)`, the providers that do the calling (a
composite's members included) are grouped by `batch_key()`: same type and same key, no question
asked twice, and at most the type's `max_batch_questions`. The first member of a group to
answer makes one call for the whole group (`answer_batch`); the others read their answers, or
the same error, from it. The call is made only when a member actually needs it, and a budget
is charged once per call.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from openhealth.errors import ProviderError
from openhealth.providers.base import Provider, ProviderOutput

_ACTIVE: ContextVar[dict[int, _Group] | None] = ContextVar("openhealth_batch", default=None)


class _Group:
    def __init__(self, members: list[Provider]):
        self.members = members
        self._outputs: dict[int, ProviderOutput] | None = None
        self._error: ProviderError | None = None

    def fits(self, provider: Provider) -> bool:
        head = self.members[0]
        if type(provider) is not type(head) or provider.batch_key() != head.batch_key():
            return False
        asked = {key for member in self.members for key in member.questions}
        if asked & set(provider.questions):
            return False
        return len(asked) + len(provider.questions) <= head.max_batch_questions

    def answer(self, provider: Provider, inputs: Mapping[str, Any]) -> ProviderOutput:
        if self._outputs is None and self._error is None:
            order = [provider, *(member for member in self.members if member is not provider)]
            try:
                outputs = provider.answer_batch(order, inputs)
            except ProviderError as error:
                self._error = error
            else:
                self._outputs = {id(m): out for m, out in zip(order, outputs, strict=True)}
        if self._error is not None:
            raise ProviderError(self._error.code, self._error.detail)
        return (self._outputs or {})[id(provider)]


@contextmanager
def batching(providers: Iterable[Provider]) -> Iterator[None]:
    """Group the calling providers of one decision; see the module docstring."""
    groups: list[_Group] = []
    for leaf in (leaf for provider in providers for leaf in provider.leaves()):
        if leaf.batch_key() is None:
            continue
        group = next((group for group in groups if group.fits(leaf)), None)
        if group is None:
            groups.append(_Group([leaf]))
        else:
            group.members.append(leaf)
    shared = {id(m): group for group in groups if len(group.members) > 1 for m in group.members}
    token = _ACTIVE.set(shared)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def batched_answer(provider: Provider, inputs: Mapping[str, Any]) -> ProviderOutput | None:
    """The provider's share of its group's call, or None when it is not batched."""
    groups = _ACTIVE.get()
    group = groups.get(id(provider)) if groups else None
    return None if group is None else group.answer(provider, inputs)

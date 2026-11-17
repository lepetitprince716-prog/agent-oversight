"""A blocked task waits on a pull request that is open but cannot merge as it is."""

from __future__ import annotations

from collections.abc import Iterator

from oversight.checks import World, blocking_pr, cannot_merge_words, open_evidence, pr_blocked_tasks
from oversight.drift import Drift, DriftKind, Unchecked
from oversight.github import Mergeability, PrState

NAME = DriftKind.BLOCKER_STUCK

_ACTION = "The pull request cannot merge as it is: rebase or fix it. This does not need the human."
# GitHub computes mergeability lazily: no answer yet is never a pass.
MERGEABILITY_UNKNOWN = "GitHub has not computed whether it can merge yet; run again"


def check(world: World) -> Iterator[Drift | Unchecked]:
    for task, blocker in pr_blocked_tasks(world):
        pr = blocking_pr(world, task, blocker, NAME)
        # An unresolved repository or a failed lookup is reported by blocker_gone alone,
        # which looks up the same pull request; reporting it here too would duplicate it.
        if isinstance(pr, Unchecked) or pr.state is not PrState.OPEN:
            continue
        words = cannot_merge_words(pr)
        if words:
            yield Drift(NAME, task.id, task.title, open_evidence(pr, words), _ACTION)
        elif pr.mergeability is Mergeability.UNKNOWN:
            yield Unchecked(NAME, task.id, MERGEABILITY_UNKNOWN)

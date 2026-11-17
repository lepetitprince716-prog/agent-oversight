"""A blocked task waits on a pull request that is no longer open."""

from __future__ import annotations

from collections.abc import Iterator

from oversight.checks import World, blocking_pr, pr_blocked_tasks
from oversight.drift import Drift, DriftKind, Unchecked, iso_day
from oversight.github import PrState

NAME = DriftKind.BLOCKER_GONE

_MERGED_ACTION = "The wait is over: close the task if nothing is left, otherwise reopen it for the queue."
_CLOSED_ACTION = "Decide whether the task still stands, then reopen or close it."


def check(world: World) -> Iterator[Drift | Unchecked]:
    for task, blocker in pr_blocked_tasks(world):
        pr = blocking_pr(world, task, blocker, NAME)
        if isinstance(pr, Unchecked):
            yield pr
        elif pr.state is PrState.MERGED:
            evidence = f"PR #{pr.number} merged, last updated {iso_day(pr.updated_at)}"
            yield Drift(NAME, task.id, task.title, evidence, _MERGED_ACTION)
        elif pr.state is PrState.CLOSED:
            evidence = f"PR #{pr.number} closed without merging, last updated {iso_day(pr.updated_at)}"
            yield Drift(NAME, task.id, task.title, evidence, _CLOSED_ACTION)

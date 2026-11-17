"""An in-progress task nobody has touched for longer than the threshold."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from oversight.checks import World
from oversight.drift import Drift, DriftKind, Unchecked, whole_days

NAME = DriftKind.QUIET_HOLDER

_ACTION = (
    "Check whether the holder is still running. "
    "If not, take the task over or return it to the queue."
)


def check(world: World) -> Iterator[Drift | Unchecked]:
    limit = timedelta(days=world.config.quiet_days)
    for task in world.in_progress:
        if world.now - task.updated_at > limit:
            days = whole_days(task.updated_at, world.now)
            evidence = f"in progress, held by {task.assignee or 'nobody'}, no update for {days} days"
            yield Drift(NAME, task.id, task.title, evidence, _ACTION)

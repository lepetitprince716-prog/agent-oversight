"""A blocked task whose notes do not say what it waits on."""

from __future__ import annotations

from collections.abc import Iterator

from oversight.checks import World
from oversight.drift import Drift, DriftKind, Unchecked, iso_day
from oversight.tracker import NoBlocker

NAME = DriftKind.SILENT_WAIT

_ACTION = "Write the one thing being waited on, or reopen the task."


def check(world: World) -> Iterator[Drift | Unchecked]:
    for task in world.blocked:
        if isinstance(task.blocker, NoBlocker):
            evidence = f"blocked, last updated {iso_day(task.updated_at)}, notes name no blocker"
            yield Drift(NAME, task.id, task.title, evidence, _ACTION)

"""A branch whose commits exist only on this machine, quiet for longer than the threshold."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from oversight.checks import World
from oversight.drift import Drift, DriftKind, Unchecked, iso_day
from oversight.clones import LocalError

NAME = DriftKind.LOCAL_ONLY

_ACTION = "Push the branch, or delete it if the work is abandoned."


def check(world: World) -> Iterator[Drift | Unchecked]:
    limit = timedelta(days=world.config.quiet_days)
    for repo in world.config.repos:
        if repo.path is None:
            continue
        try:
            branches = world.clones.unpushed_branches(repo)
        except LocalError as error:
            yield Unchecked(NAME, repo.name, str(error))
            continue
        for branch in branches:
            if branch.only_here > 0 and world.now - branch.tip_at > limit:
                commits = "1 commit exists" if branch.only_here == 1 else f"{branch.only_here} commits exist"
                evidence = f"{commits} only on this machine, newest {iso_day(branch.tip_at)}"
                yield Drift(NAME, f"{repo.name} {branch.name}", branch.name, evidence, _ACTION)

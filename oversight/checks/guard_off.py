"""A public repository whose local clone has no executable pre-push hook."""

from __future__ import annotations

from collections.abc import Iterator

from oversight.checks import World
from oversight.drift import Drift, DriftKind, Unchecked
from oversight.clones import LocalError

NAME = DriftKind.GUARD_OFF

_ACTION = "Turn the repository's hooks on before the next push."


def check(world: World) -> Iterator[Drift | Unchecked]:
    for repo in world.config.repos:
        if not repo.public or repo.path is None:
            continue
        try:
            hooked = world.clones.has_pre_push_hook(repo)
        except LocalError as error:
            yield Unchecked(NAME, repo.name, str(error))
            continue
        if not hooked:
            evidence = "public repository, no executable pre-push hook in this clone"
            yield Drift(NAME, repo.name, "pre-push hook", evidence, _ACTION)

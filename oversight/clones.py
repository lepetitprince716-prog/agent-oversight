"""Facts about local clones: commits that exist only here, and whether the pre-push hook is on.

The facts come from read-only git commands or from recorded files. git's text output is parsed here and nowhere else.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeVar

from oversight.config import Repo
from oversight.sources import (
    SourceError,
    parse_utc,
    read_json,
    require,
    run_text,
)

T = TypeVar("T")

# Tabs between the fields: a ref name cannot hold one. lstrip=2 drops "refs/heads/" and, unlike
# refname:short, stays the full name when a tag shares it. The upstream is empty when none is set, so
# it sits in the middle and the line never ends in a tab.
_BRANCH_LIST = (
    "for-each-ref",
    "refs/heads",
    "--format=%(refname:lstrip=2)%09%(upstream:short)%09%(committerdate:iso-strict)",
)


class LocalError(Exception):
    """A fact about a local clone could not be read. Never means "all is well"."""

    def __init__(self, repo: str, what: str, reason: str) -> None:
        super().__init__(f"{what} for {repo}: {reason}")
        self.repo = repo
        self.what = what
        self.reason = reason


@dataclass(frozen=True)
class LocalBranch:
    name: str
    only_here: int  # commits in no remote-tracking ref and not counted under an earlier branch of the same list
    tip_at: datetime  # commit time of the newest commit, UTC


class LocalGit(Protocol):
    def unpushed_branches(self, repo: Repo) -> tuple[LocalBranch, ...]:
        """Branches with at least one commit that no remote-tracking ref holds."""
        ...

    def has_pre_push_hook(self, repo: Repo) -> bool:
        """Whether the hooks directory this clone really uses holds an executable pre-push."""
        ...


def _count(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        raise SourceError("git printed a commit count that is not a number") from None
    if number < 0:
        raise SourceError("git printed a negative commit count")
    return number


@dataclass(frozen=True)
class _Head:
    name: str
    tracks_a_remote: bool  # an upstream is configured for it
    tip_at: datetime


def _parse_branches(text: str) -> list[_Head]:
    heads = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not fields[0]:
            raise SourceError("git printed a branch line in an unexpected shape")
        name, upstream, stamp = fields
        heads.append(_Head(name, bool(upstream), parse_utc(stamp, "committerdate")))
    return heads


class GitCli:
    def __init__(self, command: Sequence[str] = ("git",), timeout_seconds: float = 30) -> None:
        self._command = tuple(command)
        self._timeout = timeout_seconds

    def _git(self, clone: Path, *args: str) -> str:
        return run_text([*self._command, "-C", str(clone), *args], self._timeout)

    def _guarded(self, repo: Repo, what: str, read: Callable[[Path], T]) -> T:
        try:
            if repo.path is None:
                raise SourceError("no path configured")
            self._require_top_of_a_clone(repo.path)
            return read(repo.path)
        except SourceError as error:
            raise LocalError(repo.name, what, str(error)) from error

    def _require_top_of_a_clone(self, path: Path) -> None:
        # git answers from any folder inside a work tree. A plain folder inside another repository
        # would be read as that repository: its branches, its hooks.
        top = self._git(path, "rev-parse", "--show-toplevel").rstrip("\n")
        if not top:
            raise SourceError("git did not say where the clone's top folder is")
        try:
            same = path.samefile(top)
        except OSError as error:
            raise SourceError(f"cannot compare the path with the clone git found: {error.strerror or error}") from error
        if not same:
            raise SourceError("the path is not the top folder of a clone")

    def unpushed_branches(self, repo: Repo) -> tuple[LocalBranch, ...]:
        return self._guarded(repo, "local-only commits", self._branches)

    def _branches(self, clone: Path) -> tuple[LocalBranch, ...]:
        if _count(self._git(clone, "rev-list", "--count", "--branches", "--not", "--remotes")) == 0:
            return ()
        heads = _parse_branches(self._git(clone, *_BRANCH_LIST))
        first = {}
        for head in heads:
            count = _count(self._git(clone, "rev-list", "--count", f"refs/heads/{head.name}", "--not", "--remotes"))
            if count > 0:
                first[head.name] = count
        # Second pass, fewest commits first: a branch another one grew out of holds the fewest, so the shared
        # commits are counted under it and the later branches report only what they add. On a tie the branch
        # that tracks a remote one goes first: `main` and a copy of it on one commit are reported as `main`.
        untracked = {head.name for head in heads if not head.tracks_a_remote}
        counted = {}
        reported: list[str] = []
        for name in sorted(first, key=lambda each: (first[each], each in untracked, each)):
            if not reported:
                count = first[name]  # same command as the first pass
            else:
                count = _count(
                    self._git(
                        clone,
                        "rev-list",
                        "--count",
                        f"refs/heads/{name}",
                        "--not",
                        "--remotes",
                        *(f"refs/heads/{earlier}" for earlier in reported),
                    )
                )
            if count > 0:
                counted[name] = count
                reported.append(name)
        return tuple(LocalBranch(head.name, counted[head.name], head.tip_at) for head in heads if head.name in counted)

    def has_pre_push_hook(self, repo: Repo) -> bool:
        return self._guarded(repo, "pre-push hook", self._hook)

    def _hook(self, clone: Path) -> bool:
        # The path may be relative to the clone: core.hooksPath often is.
        hooks = clone / self._git(clone, "rev-parse", "--git-path", "hooks").strip()
        hook = hooks / "pre-push"
        return hook.is_file() and os.access(hook, os.X_OK)


class RecordedLocal:
    """Reads `local-<repo name>.json`: the facts already parsed, in the shape of this module's types."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _read(self, repo: Repo, what: str, parse: Callable[[object], T]) -> T:
        try:
            return parse(read_json(self._directory / f"local-{repo.name}.json"))
        except SourceError as error:
            raise LocalError(repo.name, what, str(error)) from error

    def unpushed_branches(self, repo: Repo) -> tuple[LocalBranch, ...]:
        def parse(raw: object) -> tuple[LocalBranch, ...]:
            branches = []
            for item in require(raw, "branches", list):
                only_here = require(item, "only_here", int)
                if only_here < 0:
                    raise SourceError("field 'only_here' must not be negative")
                if only_here > 0:
                    branches.append(
                        LocalBranch(require(item, "name", str), only_here, parse_utc(require(item, "tip", str), "tip"))
                    )
            return tuple(branches)

        return self._read(repo, "local-only commits", parse)

    def has_pre_push_hook(self, repo: Repo) -> bool:
        return self._read(repo, "pre-push hook", lambda raw: require(raw, "pre_push_hook", bool))

"""The checks, the world they look at, and the helpers more than one of them needs."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from oversight.config import Config
from oversight.drift import Drift, DriftKind, Report, Unchecked, iso_day
from oversight.github import Checks, GitHub, GitHubError, Mergeability, PullRequest
from oversight.clones import LocalGit
from oversight.tracker import PullRequestBlocker, Task


@dataclass(frozen=True)
class World:
    config: Config
    blocked: tuple[Task, ...]
    in_progress: tuple[Task, ...]
    github: GitHub
    clones: LocalGit
    now: datetime


@dataclass(frozen=True)
class NoRepo:
    reason: str


def github_repo(world: World, task: Task) -> str | NoRepo:
    """The task's GitHub repository: exactly one `repo:<name>` label, and that name configured."""
    names = task.repo_names
    if not names:
        return NoRepo("task has no repo: label")
    if len(names) > 1:
        return NoRepo(f"task has several repo: labels ({', '.join(names)})")
    repo = world.config.github_for(names[0])
    return NoRepo(f"repo:{names[0]} is not in the config") if repo is None else repo


def blocker_repo(world: World, task: Task, blocker: PullRequestBlocker) -> str | NoRepo:
    """Where the pull request a task waits on lives: the repository the blocker names, else the task's own.

    A named repository is looked up whether or not the config lists it: gh reaches any repository
    the user can read, and waiting on someone else's pull request is common.
    """
    return blocker.repo or github_repo(world, task)


def pr_blocked_tasks(world: World) -> Iterator[tuple[Task, PullRequestBlocker]]:
    for task in world.blocked:
        if isinstance(task.blocker, PullRequestBlocker):
            yield task, task.blocker


def blocking_pr(world: World, task: Task, blocker: PullRequestBlocker, check: DriftKind) -> PullRequest | Unchecked:
    """The pull request a task waits on, or the reason it could not be looked up."""
    repo = blocker_repo(world, task, blocker)
    if isinstance(repo, NoRepo):
        return Unchecked(check, task.id, repo.reason)
    try:
        return world.github.pull_request(repo, blocker.number)
    except GitHubError as error:
        return Unchecked(check, task.id, str(error))


def cannot_merge_words(pr: PullRequest) -> list[str]:
    """What keeps an open pull request from merging as it is; empty when nothing is known to."""
    words = []
    if pr.mergeability is Mergeability.CONFLICTING:
        words.append("conflicting")
    if pr.checks is Checks.FAILING:
        words.append("checks failing")
    return words


def open_evidence(pr: PullRequest, words: Sequence[str]) -> str:
    return ", ".join([f"PR #{pr.number} open", *words, f"last updated {iso_day(pr.updated_at)}"])


class CheckModule(Protocol):
    NAME: DriftKind

    def check(self, world: World) -> Iterator[Drift | Unchecked]: ...


# Imported after the names above on purpose: the check modules import them from this package.
from oversight.checks import (  # noqa: E402
    blocker_gone,
    blocker_stuck,
    guard_off,
    local_only,
    pr_idle,
    quiet_holder,
    schedule_red,
    silent_wait,
)

ALL_CHECKS: tuple[CheckModule, ...] = (
    blocker_gone,
    blocker_stuck,
    silent_wait,
    quiet_holder,
    schedule_red,
    pr_idle,
    local_only,
    guard_off,
)


def run_checks(world: World) -> Report:
    drifts: list[Drift] = []
    unchecked: list[Unchecked] = []
    for module in ALL_CHECKS:
        for finding in module.check(world):
            (drifts if isinstance(finding, Drift) else unchecked).append(finding)
    waiting = tuple(sorted(world.blocked, key=lambda task: task.updated_at))
    return Report(world.now, waiting, tuple(drifts), tuple(unchecked))

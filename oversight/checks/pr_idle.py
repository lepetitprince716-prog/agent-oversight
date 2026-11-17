"""An open pull request nobody has touched for longer than the threshold, with no task waiting on it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from oversight.checks import NoRepo, World, blocker_repo, cannot_merge_words, open_evidence, pr_blocked_tasks
from oversight.drift import Drift, DriftKind, Unchecked
from oversight.github import GitHubError, Mergeability, PullRequest

NAME = DriftKind.PR_IDLE

_ACTION = "Finish and merge it, close it, or file a task that says what it waits for."


def _named_by_tasks(world: World) -> set[tuple[str, int]]:
    """(repository, number) of every pull request a blocked task already waits on: that wait is on record.

    GitHub repository names ignore case, so they are compared in lower case.
    """
    named = set()
    for task, blocker in pr_blocked_tasks(world):
        repo = blocker_repo(world, task, blocker)
        if not isinstance(repo, NoRepo):
            named.add((repo.lower(), blocker.number))
    return named


def _state_words(pr: PullRequest) -> list[str]:
    words = ["draft"] if pr.draft else []
    words += cannot_merge_words(pr)
    if not words and pr.mergeability is Mergeability.CLEAN:
        words.append("ready to merge")
    return words


def check(world: World) -> Iterator[Drift | Unchecked]:
    limit = timedelta(days=world.config.pr_days)
    named = _named_by_tasks(world)
    for repo in world.config.repos:
        try:
            open_prs = world.github.open_pull_requests(repo.github)
        except GitHubError as error:
            yield Unchecked(NAME, repo.github, str(error))
            continue
        for pr in open_prs:
            if world.now - pr.updated_at > limit and (repo.github.lower(), pr.number) not in named:
                yield Drift(NAME, f"{repo.github}#{pr.number}", pr.title, open_evidence(pr, _state_words(pr)), _ACTION)

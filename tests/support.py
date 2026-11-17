"""Builders shared by the test modules. Everything here is synthetic."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from oversight.checks import World
from oversight.config import DEFAULT_MARKERS, Config, Repo, parse_config
from oversight.github import (
    Checks,
    GitHubError,
    Mergeability,
    PrState,
    PullRequest,
    ScheduledRun,
    Workflow,
)
from oversight.clones import LocalBranch, LocalError
from oversight.tracker import Status, Task, parse_blocker

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def make_config(**overrides: object) -> Config:
    data: dict[str, object] = {
        "tracker": {"dir": "~/tasks"},
        "repos": {
            "widgets": {"github": "acme/widgets"},
            "gadgets": {"github": "acme/gadgets", "scheduled": False},
        },
    }
    data.update(overrides)
    return parse_config(data)


def make_task(
    task_id: str = "task-a1b2",
    *,
    status: Status = Status.BLOCKED,
    labels: Iterable[str] = ("repo:widgets",),
    notes: str = "",
    updated: datetime | None = None,
    assignee: str | None = None,
    title: str = "Sample task",
) -> Task:
    return Task(
        id=task_id,
        title=title,
        status=status,
        labels=frozenset(labels),
        assignee=assignee,
        updated_at=updated or days_ago(1),
        blocker=parse_blocker(notes, DEFAULT_MARKERS),
    )


def make_pr(
    number: int = 12,
    *,
    repo: str = "acme/widgets",
    state: PrState = PrState.OPEN,
    mergeability: Mergeability = Mergeability.CLEAN,
    checks: Checks = Checks.PASSING,
    updated: datetime | None = None,
    title: str = "Sample change",
    draft: bool = False,
) -> PullRequest:
    return PullRequest(repo, number, title, state, mergeability, checks, updated or days_ago(1), draft)


class FakeRun(NamedTuple):
    repo: str
    workflow: str
    run: ScheduledRun


def make_run(workflow: str, succeeded: bool, days: int, repo: str = "acme/widgets") -> FakeRun:
    return FakeRun(repo, workflow, ScheduledRun(days_ago(days), succeeded))


class FakeGitHub:
    """In-memory GitHub. A repository listed in `broken` fails every call.

    Workflows exist for every (repository, name) in `runs` and in `quiet`, the latter with no runs;
    runs of a workflow named in `broken_workflows` cannot be listed.
    """

    def __init__(
        self,
        prs: Iterable[PullRequest] = (),
        open_prs: Iterable[PullRequest] = (),
        runs: Iterable[FakeRun] = (),
        broken: Iterable[str] = (),
        quiet: Iterable[tuple[str, str]] = (),
        broken_workflows: Iterable[str] = (),
    ) -> None:
        self._prs = {(pr.repo, pr.number): pr for pr in prs}
        self._open = tuple(open_prs)
        self._broken = set(broken)
        self._broken_workflows = set(broken_workflows)
        self._workflows: dict[str, list[Workflow]] = {}
        self._runs: dict[tuple[str, int], list[ScheduledRun]] = {}
        for repo, name, run in runs:
            self._runs.setdefault((repo, self._workflow(repo, name).id), []).append(run)
        for repo, name in quiet:
            self._workflow(repo, name)

    def _workflow(self, repo: str, name: str) -> Workflow:
        known = self._workflows.setdefault(repo, [])
        found = next((workflow for workflow in known if workflow.name == name), None)
        if found is None:
            found = Workflow(sum(len(workflows) for workflows in self._workflows.values()), name)
            known.append(found)
        return found

    def _guard(self, repo: str, what: str) -> None:
        if repo in self._broken:
            raise GitHubError(repo, what, "simulated failure")

    def pull_request(self, repo: str, number: int) -> PullRequest:
        self._guard(repo, f"pull request #{number}")
        try:
            return self._prs[(repo, number)]
        except KeyError:
            raise GitHubError(repo, f"pull request #{number}", "not found") from None

    def open_pull_requests(self, repo: str) -> tuple[PullRequest, ...]:
        self._guard(repo, "open pull requests")
        return tuple(pr for pr in self._open if pr.repo == repo)

    def active_workflows(self, repo: str) -> tuple[Workflow, ...]:
        self._guard(repo, "workflows")
        return tuple(self._workflows.get(repo, ()))

    def scheduled_runs(self, repo: str, workflow: Workflow, limit: int) -> tuple[ScheduledRun, ...]:
        self._guard(repo, "scheduled runs")
        if workflow.name in self._broken_workflows:
            raise GitHubError(repo, "scheduled runs", "simulated failure")
        return tuple(self._runs.get((repo, workflow.id), ()))[:limit]


def make_branch(name: str = "fix/retry-budget", only_here: int = 3, days: float = 7) -> LocalBranch:
    return LocalBranch(name, only_here, days_ago(days))


class FakeLocal:
    """In-memory local clones, keyed by repository name. A repository in `broken` fails every call.

    A repository has the pre-push hook only when it is listed in `hooked`.
    """

    def __init__(
        self,
        branches: dict[str, Iterable[LocalBranch]] | None = None,
        hooked: Iterable[str] = (),
        broken: Iterable[str] = (),
    ) -> None:
        self._branches = {name: tuple(found) for name, found in (branches or {}).items()}
        self._hooked = set(hooked)
        self._broken = set(broken)

    def _guard(self, repo: Repo, what: str) -> None:
        if repo.name in self._broken:
            raise LocalError(repo.name, what, "simulated failure")

    def unpushed_branches(self, repo: Repo) -> tuple[LocalBranch, ...]:
        self._guard(repo, "local-only commits")
        return self._branches.get(repo.name, ())

    def has_pre_push_hook(self, repo: Repo) -> bool:
        self._guard(repo, "pre-push hook")
        return repo.name in self._hooked


def make_world(
    blocked: Iterable[Task] = (),
    in_progress: Iterable[Task] = (),
    github: FakeGitHub | None = None,
    config: Config | None = None,
    clones: FakeLocal | None = None,
) -> World:
    return World(
        config or make_config(),
        tuple(blocked),
        tuple(in_progress),
        github or FakeGitHub(),
        clones or FakeLocal(),
        NOW,
    )

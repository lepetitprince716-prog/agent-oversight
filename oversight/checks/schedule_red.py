"""A scheduled workflow whose newest runs keep failing."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

from oversight.checks import World
from oversight.drift import Drift, DriftKind, Unchecked, iso_day
from oversight.github import GitHubError, ScheduledRun

NAME = DriftKind.SCHEDULE_RED

RUNS_LOOKED_AT = 20
_ACTION = "Find the failing step and fix it; file a task if none exists."


def _failing_streak(newest_first: tuple[ScheduledRun, ...]) -> list[ScheduledRun]:
    streak: list[ScheduledRun] = []
    for run in newest_first:
        if run.succeeded:
            break
        streak.append(run)
    return streak


def _evidence(streak: list[ScheduledRun]) -> str:
    since = iso_day(streak[-1].created_at)
    # A streak that fills the window may be longer: the runs before it were not looked at.
    if len(streak) >= RUNS_LOOKED_AT:
        return f"failed at least {RUNS_LOOKED_AT} scheduled runs in a row, since {since} or earlier"
    return f"failed {len(streak)} scheduled runs in a row, since {since}"


def check(world: World) -> Iterator[Drift | Unchecked]:
    for repo in world.config.repos:
        if not repo.scheduled:
            continue
        try:
            workflows = world.github.active_workflows(repo.github)
        except GitHubError as error:
            yield Unchecked(NAME, repo.github, str(error))
            continue
        names = Counter(workflow.name for workflow in workflows)
        for workflow in workflows:
            subject = f"{repo.github} {workflow.name}"
            if names[workflow.name] > 1:
                subject += f" (id {workflow.id})"
            try:
                runs = world.github.scheduled_runs(repo.github, workflow, RUNS_LOOKED_AT)
            except GitHubError as error:
                yield Unchecked(NAME, subject, str(error))
                continue
            streak = _failing_streak(runs)
            if streak:
                yield Drift(NAME, subject, workflow.name, _evidence(streak), _ACTION)

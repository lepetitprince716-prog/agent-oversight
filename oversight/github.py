"""GitHub facts: pull requests, workflows and scheduled runs, from the gh CLI or from recorded files."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, TypeVar

from oversight.sources import (
    SourceError,
    decode_json,
    optional,
    parse_utc,
    read_text,
    require,
    require_list,
    run_text,
)

T = TypeVar("T")

_PR_FIELDS = "number,title,state,isDraft,mergeStateStatus,statusCheckRollup,updatedAt"
_WORKFLOW_FIELDS = "id,name,state"
_RUN_FIELDS = "conclusion,status,createdAt"
# The most gh is asked for in one listing; a listing that comes back this full may be cut short.
_LISTING_LIMIT = 1000


class GitHubError(Exception):
    """A GitHub fact could not be fetched or read. Never means "all is well"."""

    def __init__(self, repo: str, what: str, reason: str) -> None:
        super().__init__(f"{what} for {repo}: {reason}")
        self.repo = repo
        self.what = what
        self.reason = reason


class PrState(Enum):
    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"


class Mergeability(Enum):
    CLEAN = "clean"
    CONFLICTING = "conflicting"
    OTHER = "other"
    # GitHub computes mergeability lazily: the first query for a pull request often has no
    # answer yet. That is not the same as "no conflict", so it gets its own value.
    UNKNOWN = "unknown"


class Checks(Enum):
    PASSING = "passing"
    FAILING = "failing"
    PENDING = "pending"
    NONE = "none"


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    state: PrState
    mergeability: Mergeability
    checks: Checks
    updated_at: datetime
    draft: bool


@dataclass(frozen=True)
class Workflow:
    id: int
    name: str


@dataclass(frozen=True)
class ScheduledRun:
    created_at: datetime
    succeeded: bool


class GitHub(Protocol):
    def pull_request(self, repo: str, number: int) -> PullRequest: ...

    def open_pull_requests(self, repo: str) -> tuple[PullRequest, ...]: ...

    def active_workflows(self, repo: str) -> tuple[Workflow, ...]: ...

    def scheduled_runs(self, repo: str, workflow: Workflow, limit: int) -> tuple[ScheduledRun, ...]:
        """Newest first."""
        ...


_MERGEABILITY = {
    "CLEAN": Mergeability.CLEAN,
    "DIRTY": Mergeability.CONFLICTING,
    "UNKNOWN": Mergeability.UNKNOWN,
    "": Mergeability.UNKNOWN,
}
_IGNORED = {"SKIPPED", "NEUTRAL"}
_FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_UNFINISHED = {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED"}


def checks_from_rollup(items: Sequence[object]) -> Checks:
    values = []
    for item in items:
        # Check runs carry `conclusion`, status contexts carry `state`; either may be null or "".
        value = (optional(item, "conclusion", str) or optional(item, "state", str) or "").upper()
        if value not in _IGNORED:
            values.append(value)
    if any(value in _FAILED for value in values):
        return Checks.FAILING
    if any(value in _UNFINISHED for value in values):
        return Checks.PENDING
    return Checks.PASSING if "SUCCESS" in values else Checks.NONE


def parse_pull_request(repo: str, raw: object) -> PullRequest:
    try:
        state = PrState(require(raw, "state", str).upper())
    except ValueError:
        raise SourceError("field 'state' is not OPEN, MERGED or CLOSED") from None
    merge_state = (optional(raw, "mergeStateStatus", str) or "").upper()
    return PullRequest(
        repo=repo,
        number=require(raw, "number", int),
        title=require(raw, "title", str),
        state=state,
        mergeability=_MERGEABILITY.get(merge_state, Mergeability.OTHER),
        checks=checks_from_rollup(optional(raw, "statusCheckRollup", list) or []),
        updated_at=parse_utc(require(raw, "updatedAt", str), "updatedAt"),
        draft=optional(raw, "isDraft", bool) or False,
    )


def _listing(raw: object) -> list[object]:
    items = require_list(raw)
    if len(items) >= _LISTING_LIMIT:
        raise SourceError(f"listing returned its limit of {_LISTING_LIMIT}; there may be more")
    return items


def _parse_workflows(raw: object) -> tuple[Workflow, ...]:
    return tuple(
        Workflow(require(item, "id", int), require(item, "name", str))
        for item in _listing(raw)
        if require(item, "state", str) == "active"
    )


def _parse_runs(raw: object) -> tuple[ScheduledRun, ...]:
    runs = []
    for item in require_list(raw):
        if require(item, "status", str).lower() != "completed":
            continue
        created_at = parse_utc(require(item, "createdAt", str), "createdAt")
        conclusion = (optional(item, "conclusion", str) or "").upper()
        # A run that was skipped or came out neutral did no work either way; every other
        # conclusion that is not success, cancelled included, is a run that did not do its job.
        if conclusion not in _IGNORED:
            runs.append(ScheduledRun(created_at, conclusion == "SUCCESS"))
    return tuple(sorted(runs, key=lambda run: run.created_at, reverse=True))


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


class _Facts(ABC):
    """Turns raw JSON into facts; subclasses only say where the JSON comes from."""

    @abstractmethod
    def _load(self, args: Sequence[str], filename: str) -> str: ...

    def _get(
        self,
        repo: str,
        what: str,
        args: Sequence[str],
        filename: str,
        parse: Callable[[object], T],
        blank_as: str | None = None,
    ) -> T:
        """`blank_as` is the JSON to use when the output is blank; without it blank output is an error."""
        try:
            text = self._load(args, filename)
            return parse(decode_json(text if text.strip() or blank_as is None else blank_as))
        except SourceError as error:
            raise GitHubError(repo, what, str(error)) from error

    def pull_request(self, repo: str, number: int) -> PullRequest:
        args = ["pr", "view", str(number), "--repo", repo, "--json", _PR_FIELDS]
        return self._get(
            repo, f"pull request #{number}", args, f"pr-view-{_slug(repo)}-{number}.json",
            lambda raw: parse_pull_request(repo, raw),
        )

    def open_pull_requests(self, repo: str) -> tuple[PullRequest, ...]:
        args = ["pr", "list", "--repo", repo, "--state", "open", "--limit", str(_LISTING_LIMIT), "--json", _PR_FIELDS]
        return self._get(
            repo, "open pull requests", args, f"pr-list-{_slug(repo)}.json",
            lambda raw: tuple(parse_pull_request(repo, item) for item in _listing(raw)),
        )

    def active_workflows(self, repo: str) -> tuple[Workflow, ...]:
        args = ["workflow", "list", "--repo", repo, "--limit", str(_LISTING_LIMIT), "--json", _WORKFLOW_FIELDS]
        # gh prints nothing, not [], for a repository that has no workflows.
        return self._get(repo, "workflows", args, f"workflow-list-{_slug(repo)}.json", _parse_workflows, blank_as="[]")

    def scheduled_runs(self, repo: str, workflow: Workflow, limit: int) -> tuple[ScheduledRun, ...]:
        args = [
            "run", "list", "--repo", repo, "--workflow", str(workflow.id), "--event", "schedule",
            "--limit", str(limit), "--json", _RUN_FIELDS,
        ]
        # gh already honours --limit; slicing makes recorded files behave the same.
        return self._get(
            repo, "scheduled runs", args, f"run-list-{_slug(repo)}-{workflow.id}.json",
            lambda raw: _parse_runs(raw)[:limit],
        )


class GhCli(_Facts):
    def __init__(self, command: Sequence[str], timeout_seconds: float) -> None:
        self._command = tuple(command)
        self._timeout = timeout_seconds

    def _load(self, args: Sequence[str], filename: str) -> str:
        return run_text([*self._command, *args], self._timeout)


class Recorded(_Facts):
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _load(self, args: Sequence[str], filename: str) -> str:
        return read_text(self._directory / filename)


class Cached:
    """Fetches each distinct call once per run; a failed call stays failed."""

    def __init__(self, inner: GitHub) -> None:
        self._inner = inner
        self._memo: dict[tuple[object, ...], object] = {}

    def _once(self, key: tuple[object, ...], call: Callable[[], T]) -> T:
        if key not in self._memo:
            try:
                self._memo[key] = call()
            except GitHubError as error:
                self._memo[key] = error
        outcome = self._memo[key]
        if isinstance(outcome, GitHubError):
            raise outcome
        return outcome  # type: ignore[return-value]

    def pull_request(self, repo: str, number: int) -> PullRequest:
        return self._once(("pr", repo, number), lambda: self._inner.pull_request(repo, number))

    def open_pull_requests(self, repo: str) -> tuple[PullRequest, ...]:
        return self._once(("open", repo), lambda: self._inner.open_pull_requests(repo))

    def active_workflows(self, repo: str) -> tuple[Workflow, ...]:
        return self._once(("workflows", repo), lambda: self._inner.active_workflows(repo))

    def scheduled_runs(self, repo: str, workflow: Workflow, limit: int) -> tuple[ScheduledRun, ...]:
        return self._once(
            ("runs", repo, workflow.id, limit), lambda: self._inner.scheduled_runs(repo, workflow, limit)
        )

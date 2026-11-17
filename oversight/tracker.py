"""Beads adapter: tasks as the tracker reports them, with the blocker parsed once."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from oversight.sources import SourceError, optional, parse_utc, read_json, require, require_list, run_json


class TrackerError(Exception):
    """The tracker did not deliver tasks we can read."""


class Status(Enum):
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"


class WaitKind(Enum):
    DECISION = "decision"
    REVIEW = "review"
    NOTICE = "notice"
    UNTYPED = "untyped"


@dataclass(frozen=True)
class NoBlocker:
    pass


@dataclass(frozen=True)
class TextBlocker:
    text: str


@dataclass(frozen=True)
class PullRequestBlocker:
    text: str
    number: int
    repo: str | None = None  # "owner/name" when the text says which repository; otherwise the task's own


Blocker = NoBlocker | TextBlocker | PullRequestBlocker

_END_OF_BLOCKER = re.compile(r"[｜|\r\n]")
_OWNER_NAME = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
# "https://github.com/acme/widgets/pull/12"
_URL_REFERENCE = re.compile(rf"github\.com/({_OWNER_NAME})/pull/(\d+)", re.IGNORECASE)
# "acme/widgets#12", with or without "PR" before it
_QUALIFIED_REFERENCE = re.compile(rf"(?<![A-Za-z0-9_./-])({_OWNER_NAME})#(\d+)")
# "PR 12", "PR #12", "pull request #12"
_NAMED_REFERENCE = re.compile(r"(?<![A-Za-z])(?:PR|pull\s+request)\s*#?\s*(\d+)", re.IGNORECASE)
# a bare "#12" only counts when the text talks about merging before it
_BARE_REFERENCE = re.compile(r"(?<![A-Za-z0-9_.-])#(\d+)")
_MERGE_WORDS = re.compile(r"(?<![A-Za-z])PR(?![A-Za-z])|pull|merge|合并", re.IGNORECASE)

_WAIT_LABELS = {
    "wait:decision": WaitKind.DECISION,
    "wait:review": WaitKind.REVIEW,
    "wait:notice": WaitKind.NOTICE,
}
_REPO_PREFIX = "repo:"


def parse_blocker(notes: str, markers: Sequence[str]) -> Blocker:
    # Longest marker first, so an overlapping shorter one cannot win at the same spot.
    ordered = sorted(markers, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(marker) for marker in ordered), re.IGNORECASE)
    found = list(pattern.finditer(notes))
    if not found:
        return NoBlocker()
    tail = notes[found[-1].end():]
    end = _END_OF_BLOCKER.search(tail)
    text = (tail[: end.start()] if end else tail).strip()
    if not text:
        return NoBlocker()
    reference = _pull_request_reference(text)
    return TextBlocker(text) if reference is None else PullRequestBlocker(text, *reference)


def _pull_request_reference(text: str) -> tuple[int, str | None] | None:
    """(number, repository or None) of the first pull request the text names."""
    hits = [(match.start(), match.group(2), match.group(1)) for match in _URL_REFERENCE.finditer(text)]
    # Blank the links out, so their "pull" and their numbers are not read a second time.
    rest = _URL_REFERENCE.sub(lambda match: " " * len(match.group()), text)
    hits += [(match.start(), match.group(2), match.group(1)) for match in _QUALIFIED_REFERENCE.finditer(rest)]
    hits += [(match.start(), match.group(1), None) for match in _NAMED_REFERENCE.finditer(rest)]
    hits += [
        (match.start(), match.group(1), None)
        for match in _BARE_REFERENCE.finditer(rest)
        if _MERGE_WORDS.search(rest[: match.start()])
    ]
    if not hits:
        return None
    _, number, repo = min(hits, key=lambda hit: hit[0])
    return int(number), repo


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: Status
    labels: frozenset[str]
    assignee: str | None
    updated_at: datetime
    blocker: Blocker

    @property
    def repo_names(self) -> tuple[str, ...]:
        names = (label[len(_REPO_PREFIX):] for label in self.labels if label.startswith(_REPO_PREFIX))
        return tuple(sorted(name for name in names if name))

    @property
    def wait_kind(self) -> WaitKind:
        kinds = {_WAIT_LABELS[label] for label in self.labels if label in _WAIT_LABELS}
        return next(iter(kinds)) if len(kinds) == 1 else WaitKind.UNTYPED

    @property
    def blocker_text(self) -> str | None:
        return None if isinstance(self.blocker, NoBlocker) else self.blocker.text


def _task(raw: object, markers: Sequence[str]) -> Task:
    labels = optional(raw, "labels", list) or []
    if not all(type(label) is str for label in labels):
        raise SourceError("field 'labels' must be a list of strings")
    try:
        status = Status(require(raw, "status", str))
    except ValueError:
        raise SourceError("field 'status' is not blocked or in_progress") from None
    return Task(
        id=require(raw, "id", str),
        title=require(raw, "title", str),
        status=status,
        labels=frozenset(labels),
        assignee=optional(raw, "assignee", str) or None,
        updated_at=parse_utc(require(raw, "updated_at", str), "updated_at"),
        blocker=parse_blocker(optional(raw, "notes", str) or "", markers),
    )


class Tracker(ABC):
    def __init__(self, markers: Sequence[str]) -> None:
        self._markers = tuple(markers)

    @abstractmethod
    def _load(self, status: Status) -> object: ...

    def tasks(self, status: Status) -> tuple[Task, ...]:
        try:
            data = self._load(status)
            tasks = tuple(_task(item, self._markers) for item in require_list(data))
        except SourceError as error:
            raise TrackerError(f"tracker, status {status.value}: {error}") from error
        if any(task.status is not status for task in tasks):
            raise TrackerError(f"tracker, status {status.value}: a task has another status")
        return tasks


class BdCli(Tracker):
    def __init__(
        self, command: Sequence[str], directory: Path, markers: Sequence[str], timeout_seconds: float = 30
    ) -> None:
        super().__init__(markers)
        self._argv = (*command, "--readonly", "-C", str(directory))
        self._timeout = timeout_seconds

    def _load(self, status: Status) -> object:
        return run_json([*self._argv, "list", "-s", status.value, "--limit", "0", "--json"], self._timeout)


class RecordedTracker(Tracker):
    def __init__(self, directory: Path, markers: Sequence[str]) -> None:
        super().__init__(markers)
        self._directory = directory

    def _load(self, status: Status) -> object:
        return read_json(self._directory / f"tracker-{status.value}.json")

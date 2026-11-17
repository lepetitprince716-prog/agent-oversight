"""What the checks produce: drift (tracker and reality disagree), unchecked (could not look)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from oversight.tracker import Task


class DriftKind(Enum):
    BLOCKER_GONE = "blocker_gone"
    BLOCKER_STUCK = "blocker_stuck"
    SILENT_WAIT = "silent_wait"
    QUIET_HOLDER = "quiet_holder"
    SCHEDULE_RED = "schedule_red"
    PR_IDLE = "pr_idle"
    LOCAL_ONLY = "local_only"
    GUARD_OFF = "guard_off"


@dataclass(frozen=True)
class Drift:
    kind: DriftKind
    subject: str
    title: str
    evidence: str
    action: str


@dataclass(frozen=True)
class Unchecked:
    check: DriftKind
    subject: str
    reason: str


@dataclass(frozen=True)
class Report:
    checked_at: datetime
    waiting: tuple[Task, ...]  # every blocked task
    drifts: tuple[Drift, ...]
    unchecked: tuple[Unchecked, ...]

    @property
    def clean(self) -> bool:
        return not self.drifts and not self.unchecked


def iso_day(moment: datetime) -> str:
    """Calendar day in UTC; every moment in the program is already UTC."""
    return moment.date().isoformat()


def whole_days(since: datetime, now: datetime) -> int:
    """Whole days from `since` to `now`, rounded down, never negative."""
    return max(0, (now - since).days)

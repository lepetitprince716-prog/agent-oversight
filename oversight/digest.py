"""The digest: a few lines for a human, in the configured language."""

from __future__ import annotations

from oversight.config import Lang
from oversight.drift import Report, whole_days
from oversight.tracker import Task, WaitKind

# Display order of the waiting items.
_GROUPS = (WaitKind.DECISION, WaitKind.REVIEW, WaitKind.NOTICE, WaitKind.UNTYPED)

_KIND_LABEL = {
    Lang.EN: {
        WaitKind.DECISION: "decide",
        WaitKind.REVIEW: "review",
        WaitKind.NOTICE: "notice",
        WaitKind.UNTYPED: "untyped",
    },
    Lang.ZH: {
        WaitKind.DECISION: "要你定",
        WaitKind.REVIEW: "要你看",
        WaitKind.NOTICE: "出了事",
        WaitKind.UNTYPED: "未分类",
    },
}


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _counts_line(report: Report, lang: Lang) -> str:
    drifts, unchecked = len(report.drifts), len(report.unchecked)
    oldest = max((whole_days(task.updated_at, report.checked_at) for task in report.waiting), default=0)
    if lang is Lang.ZH:
        head = "没有在等你的事。"
        if report.waiting:
            head = f"等你 {len(report.waiting)} 件，最久 {oldest} 天。"
        return f"{head}对不上 {drifts} 处。没查到 {unchecked} 项。"
    head = "Nothing is waiting on you."
    if report.waiting:
        head = f"{len(report.waiting)} waiting on you, oldest {_plural(oldest, 'day')}."
    return f"{head} {_plural(drifts, 'drift')}. {unchecked} unchecked."


def _waiting_line(task: Task, report: Report, lang: Lang) -> str:
    missing = "(no blocker written)" if lang is Lang.EN else "（没写卡点）"
    days = whole_days(task.updated_at, report.checked_at)
    label = _KIND_LABEL[lang][task.wait_kind]
    return f"{label} {task.id} {task.title} — {task.blocker_text or missing} ({days}d)"


def _waiting_lines(report: Report, lang: Lang) -> list[str]:
    ordered = sorted(report.waiting, key=lambda task: (_GROUPS.index(task.wait_kind), task.updated_at))
    return [_waiting_line(task, report, lang) for task in ordered]


def _more_line(hidden: int, lang: Lang) -> str:
    if lang is Lang.ZH:
        return f"……还有 {hidden} 条，见 oversight reconcile"
    return f"... and {hidden} more: run oversight reconcile"


def render_digest(report: Report, lang: Lang, max_lines: int) -> str:
    """At most `max_lines` lines, the counts line first; `max_lines` must be at least 2."""
    items = _waiting_lines(report, lang)
    items += [f"{drift.kind.value}: {drift.subject} — {drift.evidence}" for drift in report.drifts]
    room = max_lines - 1
    lines = [_counts_line(report, lang)]
    if len(items) <= room:
        return "\n".join(lines + items)
    shown = room - 1  # one line is kept for the "and N more" note
    return "\n".join(lines + items[:shown] + [_more_line(len(items) - shown, lang)])

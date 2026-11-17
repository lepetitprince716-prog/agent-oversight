"""Command line: parse arguments, wire the adapters, print, choose the exit code."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from oversight.checks import World, run_checks
from oversight.config import Config, ConfigError, config_path, load_config
from oversight.digest import render_digest
from oversight.drift import Report, whole_days
from oversight.github import Cached, GhCli, GitHub, Recorded
from oversight.clones import GitCli, LocalGit, RecordedLocal
from oversight.tracker import BdCli, RecordedTracker, Status, Tracker, TrackerError

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2
DEFAULT_DIGEST_LINES = 10


def _instant(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
        return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {text!r}") from None


def _line_budget(text: str) -> int:
    if not text.isdigit() or int(text) < 2:
        raise argparse.ArgumentTypeError("must be a whole number, at least 2")
    return int(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oversight", description="Compare the task tracker with reality. Read-only.")
    parser.add_argument("--config", help="config file (default: $OVERSIGHT_CONFIG, then ~/.config/agent-oversight/config.toml)")
    parser.add_argument("--recorded", type=Path, metavar="DIR", help="read tracker and GitHub data from files in DIR; run nothing")
    parser.add_argument("--now", type=_instant, help="pretend it is this ISO 8601 time (default: now, UTC)")
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile", help="list where the tracker and reality disagree")
    reconcile.add_argument("--json", action="store_true", help="machine-readable output")
    digest = commands.add_parser("digest", help="a short summary of what waits on the human")
    digest.add_argument("--max", type=_line_budget, default=DEFAULT_DIGEST_LINES, metavar="N", help="at most N lines (default 10)")
    return parser


def _sources(config: Config, recorded: Path | None) -> tuple[Tracker, GitHub]:
    if recorded is not None:
        return RecordedTracker(recorded, config.blocker_markers), Cached(Recorded(recorded))
    tracker = BdCli(config.tracker_command, config.tracker_dir, config.blocker_markers)
    return tracker, Cached(GhCli(config.github_command, config.github_timeout_seconds))


def _local(recorded: Path | None) -> LocalGit:
    return GitCli() if recorded is None else RecordedLocal(recorded)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def render_text(report: Report) -> str:
    lines = []
    for drift in report.drifts:
        lines += [
            f"{drift.kind.name}  {drift.subject}  {drift.title}",
            f"  {drift.evidence}",
            f"  -> {drift.action}",
        ]
    lines += [f"unchecked  {item.check.value}  {item.subject}  {item.reason}" for item in report.unchecked]
    lines.append(f"{len(report.drifts)} drift, {len(report.unchecked)} unchecked, {len(report.waiting)} waiting")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return json.dumps(
        {
            "checked_at": _stamp(report.checked_at),
            "waiting": [
                {
                    "id": task.id,
                    "title": task.title,
                    "kind": task.wait_kind.value,
                    "blocker": task.blocker_text,
                    "days": whole_days(task.updated_at, report.checked_at),
                }
                for task in report.waiting
            ],
            "drifts": [
                {"kind": d.kind.value, "subject": d.subject, "title": d.title, "evidence": d.evidence, "action": d.action}
                for d in report.drifts
            ],
            "unchecked": [{"check": u.check.value, "subject": u.subject, "reason": u.reason} for u in report.unchecked],
        },
        ensure_ascii=False,
        indent=2,
    )


def _write_utf_8() -> None:
    # Everything read is UTF-8, so everything written is too, whatever the locale says.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: Sequence[str] | None = None) -> int:
    _write_utf_8()
    try:
        return _run(argv)
    except Exception as error:
        # Exit 1 means "something to act on"; a crash must not look like that.
        traceback.print_exc()
        print(f"oversight: internal error: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_ERROR


def _run(argv: Sequence[str] | None) -> int:
    args = _parser().parse_args(argv)
    now = args.now or datetime.now(timezone.utc)
    try:
        config = load_config(config_path(args.config, os.environ))
        tracker, github = _sources(config, args.recorded)
        world = World(config, tracker.tasks(Status.BLOCKED), tracker.tasks(Status.IN_PROGRESS), github, _local(args.recorded), now)
    except (ConfigError, TrackerError) as error:
        print(f"oversight: {error}", file=sys.stderr)
        return EXIT_ERROR
    report = run_checks(world)
    if args.command == "digest":
        print(render_digest(report, config.lang, args.max))
    else:
        print(render_json(report) if args.json else render_text(report))
    return EXIT_OK if report.clean else EXIT_FINDINGS

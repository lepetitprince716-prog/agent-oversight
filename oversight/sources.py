"""Shared plumbing of the tracker and GitHub adapters: run or read text, decode JSON, check its shape.

Each adapter wraps SourceError in its own error type, so callers never see this one.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


class SourceError(Exception):
    """A command or file did not deliver usable JSON."""


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else "no output"


def decode_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise SourceError(f"invalid JSON: {error.msg}") from error


def run_text(argv: Sequence[str], timeout_seconds: float) -> str:
    program = argv[0]
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SourceError(f"{program} timed out after {timeout_seconds:g}s") from error
    except OSError as error:
        raise SourceError(f"cannot run {program}: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        # bd and gh print UTF-8; the locale's encoding would misread it, so it is never used.
        raise SourceError(f"{program} printed output that is not UTF-8") from error
    if done.returncode != 0:
        raise SourceError(f"{program} exited {done.returncode}: {_first_line(done.stderr)}")
    # stderr is ignored on success: the tracker prints warnings there.
    return done.stdout


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise SourceError(f"cannot read {path.name}: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise SourceError(f"cannot read {path.name}: not UTF-8") from error


def run_json(argv: Sequence[str], timeout_seconds: float) -> object:
    return decode_json(run_text(argv, timeout_seconds))


def read_json(path: Path) -> object:
    return decode_json(read_text(path))


def require_list(raw: object) -> list[object]:
    if type(raw) is not list:
        raise SourceError("expected a JSON array")
    return raw


def require(obj: object, key: str, kind: type[T]) -> T:
    if not isinstance(obj, dict):
        raise SourceError("expected a JSON object")
    value = obj.get(key)
    # `type(...) is` on purpose: bool must not pass for int.
    if type(value) is not kind:
        raise SourceError(f"field {key!r} must be {kind.__name__}")
    return value


def optional(obj: object, key: str, kind: type[T]) -> T | None:
    if isinstance(obj, dict) and obj.get(key) is None:
        return None
    return require(obj, key, kind)


def parse_utc(text: str, key: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise SourceError(f"field {key!r} is not an ISO 8601 time") from error
    if moment.tzinfo is None:
        raise SourceError(f"field {key!r} has no time zone")
    try:
        return moment.astimezone(timezone.utc)
    except OverflowError as error:
        raise SourceError(f"field {key!r} is not an ISO 8601 time") from error

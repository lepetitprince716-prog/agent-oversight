"""Load and validate the TOML config. The only module that reads it.

Everything is checked here, once; the rest of the program trusts the Config.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_PATH = "~/.config/agent-oversight/config.toml"
DEFAULT_MARKERS = ("blocker:", "卡点：")
_OWNER_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REQUIRED = object()


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid."""


class Lang(Enum):
    EN = "en"
    ZH = "zh"


@dataclass(frozen=True)
class Repo:
    name: str
    github: str
    scheduled: bool
    path: Path | None  # the local clone; the local checks skip a repository without one
    public: bool  # public or public-bound


@dataclass(frozen=True)
class Config:
    lang: Lang
    tracker_dir: Path
    tracker_command: tuple[str, ...]
    blocker_markers: tuple[str, ...]
    github_command: tuple[str, ...]
    github_timeout_seconds: int
    quiet_days: int
    pr_days: int
    repos: tuple[Repo, ...]

    def github_for(self, name: str) -> str | None:
        return next((repo.github for repo in self.repos if repo.name == name), None)


def config_path(explicit: str | None, environ: Mapping[str, str]) -> Path:
    return Path(explicit or environ.get("OVERSIGHT_CONFIG") or DEFAULT_PATH).expanduser()


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{path}: cannot read: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"{path}: cannot read: not UTF-8") from error
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from error
    return parse_config(data)


def parse_config(data: dict[str, Any]) -> Config:
    root = _table(data, "", {"lang", "tracker", "github", "thresholds", "repos"})
    tracker = _table(root.get("tracker"), "tracker", {"dir", "command", "blocker_markers"}, "tracker")
    github = _table(root.get("github", {}), "github", {"command", "timeout_seconds"})
    limits = _table(root.get("thresholds", {}), "thresholds", {"quiet_days", "pr_days"})
    return Config(
        lang=_lang(root),
        tracker_dir=Path(_value(tracker, "tracker", "dir", str)).expanduser(),
        tracker_command=_words(tracker, "tracker", "command", ("bd",)),
        blocker_markers=_words(tracker, "tracker", "blocker_markers", DEFAULT_MARKERS),
        github_command=_words(github, "github", "command", ("gh",)),
        github_timeout_seconds=_positive(github, "github", "timeout_seconds", 30),
        quiet_days=_positive(limits, "thresholds", "quiet_days", 2),
        pr_days=_positive(limits, "thresholds", "pr_days", 3),
        repos=_repos(root.get("repos", {})),
    )


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _table(value: object, path: str, allowed: set[str], required: str | None = None) -> dict[str, Any]:
    if value is None and required:
        raise ConfigError(f"{required}: required table is missing")
    if not isinstance(value, dict):
        raise ConfigError(f"{path or 'config'}: expected a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{_join(path, unknown[0])}: unknown key")
    return value


def _value(table: dict[str, Any], path: str, key: str, kind: type, default: object = _REQUIRED) -> Any:
    if key not in table:
        if default is _REQUIRED:
            raise ConfigError(f"{_join(path, key)}: required")
        return default
    # `type(...) is` on purpose: bool must not pass for int.
    if type(table[key]) is not kind:
        raise ConfigError(f"{_join(path, key)}: must be {kind.__name__}")
    return table[key]


def _positive(table: dict[str, Any], path: str, key: str, default: int) -> int:
    number = _value(table, path, key, int, default)
    if number <= 0:
        raise ConfigError(f"{_join(path, key)}: must be greater than zero")
    return number


def _words(table: dict[str, Any], path: str, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    items = _value(table, path, key, list, list(default))
    if not items or not all(type(item) is str and item for item in items):
        raise ConfigError(f"{_join(path, key)}: must be a non-empty list of non-empty strings")
    return tuple(items)


def _lang(root: dict[str, Any]) -> Lang:
    text = _value(root, "", "lang", str, Lang.EN.value)
    try:
        return Lang(text)
    except ValueError:
        raise ConfigError('lang: must be "en" or "zh"') from None


def _repos(value: object) -> tuple[Repo, ...]:
    if not isinstance(value, dict):
        raise ConfigError("repos: expected a table")
    repos = []
    for name, body in value.items():
        path = f"repos.{name}"
        table = _table(body, path, {"github", "scheduled", "path", "public"})
        slug = _value(table, path, "github", str)
        if not _OWNER_NAME.fullmatch(slug):
            raise ConfigError(f"{path}.github: must look like owner/name")
        clone = _value(table, path, "path", str, None)
        folder = None
        if clone is not None:
            folder = Path(clone).expanduser()
            # A relative path would mean a different folder from each working directory.
            if not folder.is_absolute():
                raise ConfigError(f"{path}.path: must be absolute or start with ~")
        repos.append(
            Repo(
                name,
                slug,
                _value(table, path, "scheduled", bool, True),
                folder,
                _value(table, path, "public", bool, False),
            )
        )
    return tuple(repos)

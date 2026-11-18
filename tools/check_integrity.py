#!/usr/bin/env python3
"""Check-integrity guard: find commits that weaken the checks instead of passing them.

It reads git diffs and commit messages and reports changes that skip or delete tests,
drop assertions, silence linters and type checkers, bypass hooks or CI, or remove lines
from check configuration. Such a change is allowed only when its commit message carries
a top-level line ``Check-change: <reason of 20 or more characters>``; one line covers
every finding of that commit. The rules live in ``check_integrity_rules.json``. A rule has
a severity: ``block`` (the default) needs that line, ``note`` is printed and counted but
never fails the run. A repository can adjust severities, add ``allow`` patterns and exempt
paths in its own ``.check-integrity.json`` (see ``load_overlay``); any change to that file is
itself a blocking finding (rule ``overlay-changed``). A merge commit is judged by what the
merge changed beyond the automatic merge (``git show --remerge-diff``, git 2.36 or newer).

Every git call pins the configuration, attributes and environment that change diff text
(``GIT_CONFIG``, ``GIT_ENV_DROP``, ``DIFF_FLAGS``), so the verdict does not depend on the clone
it runs in. A change the scanner cannot read (a patch it cannot parse, a submodule, an octopus
merge) is a blocking finding, rule ``unreadable-change``: never a pass and never a crash. A file
whose type changes (a regular file becomes a symbolic link, or the reverse) counts as the old
file deleted; the new side is not judged.

Exit status: 0 no blocking finding, or every one justified; 1 an unjustified blocking
finding; 2 a usage error, a revision that does not exist, a missing or too old git, a git call
that fails, a broken rules file or a broken overlay file. The guard fails closed. Standard
library only; one file, so it can be copied into another repository together with its rules
file.
"""

from __future__ import annotations

import argparse
import bisect
import fnmatch
import functools
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2
PROG = "check-integrity"
DEFAULT_RULES = Path(__file__).resolve().with_name("check_integrity_rules.json")
KINDS = frozenset({"added", "net-added", "removed", "net-loss", "deleted"})
RULE_KEYS = frozenset(
    {
        "id",
        "kind",
        "classes",
        "why",
        "patterns",
        "allow",
        "skip_lines",
        "existing_only",
        "spare_with_source",
        "severity",
    }
)
DOCUMENT_KEYS = frozenset({"version", "note", "classes", "rules"})
BLOCK, NOTE = "block", "note"
SEVERITIES = frozenset({BLOCK, NOTE})
OVERLAY_NAME = ".check-integrity.json"
OVERLAY_KEYS = frozenset({"version", "rules"})
OVERLAY_RULE_KEYS = frozenset({"severity", "allow", "exempt_paths"})
# A change to the overlay file is judged by the scanner itself, not by a rule of the rules
# file, and the overlay cannot touch it: otherwise a commit could loosen its own judge.
OVERLAY_GUARD = "overlay-changed"
# A change the scanner cannot read is judged by the scanner itself too, and the overlay cannot
# lower it: a diff that could not be read must never pass for one that has nothing in it.
UNREADABLE = "unreadable-change"
BUILTIN_RULES = (OVERLAY_GUARD, UNREADABLE)
REMERGE_DIFF_GIT = (2, 36)
GITLINK = b"160000"  # the mode of a submodule entry
OCTOPUS_PATH = "(octopus merge)"  # stands in for the path of a change that has none
# The conflict markers a merge resolution removes; they are not content the author dropped.
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7}|\|{7})( .*)?$|^={7}$")

JUSTIFICATION = re.compile(r"Check-change:(.*)")
MIN_REASON = 20
SCISSORS = re.compile(r"^# -+ >8 -+$")
HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
SHORT = 7
TEXT_WIDTH = 80
WORD = re.compile(r"\w+")
CONDITION = re.compile(r"^\s*(-\s*)?if\s*:")
CONJUNCTION = re.compile(r"&&|\band\b")
MARKER_END = re.compile(r"\s--\s|\*/|(?<=.)#")
# A test file and the source it tests, by file name: test_x.py or x_test.py is x.py,
# x_test.go is x.go, x.test.ts or x.spec.ts is x plus any JavaScript or TypeScript suffix.
SCRIPT_SUFFIXES = ("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts")
TEST_NAME = (
    (re.compile(r"^test_(.+\.py)$"), ("{0}",)),
    (re.compile(r"^(.+)_test\.(py|go)$"), ("{0}.{1}",)),
    (re.compile(r"^(.+)\.(?:test|spec)\.[cm]?[jt]sx?$"), tuple(f"{{0}}.{s}" for s in SCRIPT_SUFFIXES)),
)


class IntegrityError(Exception):
    """A usage, rules, git or parse problem; it ends the run with exit status 2."""


# --- rules -------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    classes: tuple[str, ...]
    why: str
    patterns: tuple[re.Pattern[str], ...]
    allow: tuple[re.Pattern[str], ...]
    skip_lines: tuple[re.Pattern[str], ...]
    existing_only: bool = False
    spare_with_source: bool = False
    severity: str = BLOCK
    exempt_paths: tuple[str, ...] = ()

    def exempts(self, diff: FileDiff) -> bool:
        """True if the overlay exempts this file from the rule. A renamed file needs both its
        paths to match, so a move cannot carry a file into or out of an exemption."""
        paths = [path for path in (diff.path, diff.old_path) if path is not None]
        return bool(self.exempt_paths) and all(
            any(fnmatch.fnmatchcase(path, glob) for glob in self.exempt_paths) for path in paths
        )

    def skips(self, line: str) -> bool:
        return any(skip.search(line) for skip in self.skip_lines)

    def markers(self, line: str) -> list[str]:
        """One key per pattern match no allow-regex claims: the pattern and the marker text
        up to the end of its comment, so the same marker on a moved line has the same key."""
        if self.skips(line):
            return []
        keys = []
        for number, pattern in enumerate(self.patterns):
            for found in pattern.finditer(line):
                if any(allow.match(line, found.start()) for allow in self.allow):
                    continue
                tail = line[found.start() :]
                end = MARKER_END.search(tail)
                keys.append(f"{number}:{' '.join(tail[: end.start() if end else None].split())}")
        return keys

    def matches(self, line: str) -> bool:
        """True if a pattern matches the line at a place no allow-regex claims."""
        return bool(self.markers(line))


@dataclass(frozen=True)
class RuleSet:
    classes: dict[str, tuple[re.Pattern[str], ...]]
    rules: tuple[Rule, ...]

    def severity_of(self, rule_id: str) -> str:
        """The severity of a rule id; load_rules makes every entry of one id agree."""
        return next((rule.severity for rule in self.rules if rule.id == rule_id), BLOCK)

    def classes_of(self, path: str) -> frozenset[str]:
        return frozenset(
            name for name, regexes in self.classes.items() if any(r.search(path) for r in regexes)
        )


def string_list(raw: object, where: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise IntegrityError(f"{where}: must be a list of non-empty strings")
    return raw


def compile_list(raw: object, where: str) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for number, item in enumerate(string_list(raw, where), start=1):
        try:
            compiled.append(re.compile(item))
        except re.error as exc:
            raise IntegrityError(f"{where} entry {number}: invalid pattern ({exc.msg})") from exc
    return tuple(compiled)


def compile_rule(raw: object, where: str, known: set[str]) -> Rule:
    if not isinstance(raw, dict) or not {"id", "kind", "classes", "why"} <= set(raw) <= RULE_KEYS:
        raise IntegrityError(f"{where}: needs id, kind, classes and why; may add {sorted(RULE_KEYS)}")
    if raw["kind"] not in KINDS:
        raise IntegrityError(f"{where}: kind must be one of {sorted(KINDS)}")
    if not isinstance(raw["id"], str) or not isinstance(raw["why"], str):
        raise IntegrityError(f"{where}: id and why must be strings")
    classes = raw["classes"]
    if not isinstance(classes, list) or not classes or not set(classes) <= known:
        raise IntegrityError(f"{where}: classes must be a non-empty list of names from 'classes'")
    patterns = compile_list(raw.get("patterns", []), f"{where} patterns")
    if raw["id"] in BUILTIN_RULES:
        raise IntegrityError(f"{where}: {raw['id']} is the scanner's own rule and cannot be defined here")
    if raw["kind"] != "deleted" and not patterns:
        raise IntegrityError(f"{where}: kind {raw['kind']} needs patterns")
    flags = {key: raw.get(key, False) for key in ("existing_only", "spare_with_source")}
    if not all(isinstance(value, bool) for value in flags.values()):
        raise IntegrityError(f"{where}: existing_only and spare_with_source must be true or false")
    if flags["spare_with_source"] and raw["kind"] != "deleted":
        raise IntegrityError(f"{where}: spare_with_source works only with kind deleted")
    severity = raw.get("severity", BLOCK)
    if severity not in SEVERITIES:
        raise IntegrityError(f"{where}: severity must be one of {sorted(SEVERITIES)}")
    return Rule(
        raw["id"],
        raw["kind"],
        tuple(classes),
        raw["why"],
        patterns,
        compile_list(raw.get("allow", []), f"{where} allow"),
        compile_list(raw.get("skip_lines", []), f"{where} skip_lines"),
        **flags,
        severity=severity,
    )


def load_rules(path: Path) -> RuleSet:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"cannot read rules file {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise IntegrityError(f'{path}: expected a JSON object with "version": 1')
    unknown = sorted(set(document) - DOCUMENT_KEYS)
    if unknown:
        raise IntegrityError(f"{path}: unknown key(s): {', '.join(unknown)}")
    raw_classes, raw_rules = document.get("classes"), document.get("rules")
    if not isinstance(raw_classes, dict) or not isinstance(raw_rules, list) or not raw_rules:
        raise IntegrityError(f"{path}: needs a 'classes' object and a non-empty 'rules' list")
    classes = {
        name: compile_list(regexes, f"{path}: class {name}") for name, regexes in raw_classes.items()
    }
    rules = tuple(
        compile_rule(raw, f"{path}: rules entry {number}", set(classes))
        for number, raw in enumerate(raw_rules, start=1)
    )
    levels: dict[str, str] = {}
    for rule in rules:
        if levels.setdefault(rule.id, rule.severity) != rule.severity:
            raise IntegrityError(f"{path}: entries of rule {rule.id} must share one severity")
    return RuleSet(classes, rules)


def load_overlay(path: Path, rules: RuleSet) -> RuleSet:
    """Apply a repository's own ``.check-integrity.json`` to the rules. The file is
    ``{"version": 1, "rules": {"<rule id>": {"severity": "note", "allow": ["<regex>"],
    "exempt_paths": ["<glob>"]}}}``: per rule id it may set the severity, append allow-regexes
    and exempt files, whose repository-relative path matches a glob (``fnmatch`` rules: ``*``
    also matches ``/``), from the rule. Nothing else. Any other key, or an id the rules do not
    have, is an error, so the file cannot quietly change more than that. The ``overlay-changed``
    rule cannot be set at all."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("version", 1) != 1:
        raise IntegrityError(f'{path}: expected a JSON object; "version", when present, must be 1')
    unknown = sorted(set(document) - OVERLAY_KEYS)
    if unknown:
        raise IntegrityError(f"{path}: unknown key(s): {', '.join(unknown)}")
    changes = document.get("rules", {})
    if not isinstance(changes, dict):
        raise IntegrityError(f"{path}: 'rules' must be an object keyed by rule id")
    known = {rule.id for rule in rules.rules}
    allow: dict[str, tuple[re.Pattern[str], ...]] = {}
    exempt: dict[str, tuple[str, ...]] = {}
    severity: dict[str, str] = {}
    for rule_id, change in changes.items():
        where = f"{path}: rule {rule_id}"
        if rule_id == OVERLAY_GUARD:
            raise IntegrityError(
                f"{where}: the overlay cannot change this rule; any change to {OVERLAY_NAME} needs a reason line"
            )
        if rule_id == UNREADABLE:
            raise IntegrityError(
                f"{where}: the overlay cannot change this rule; a change the scanner cannot read always needs a reason line"
            )
        if rule_id not in known:
            raise IntegrityError(f"{where}: no such rule (known: {', '.join(sorted(known))})")
        if not isinstance(change, dict) or set(change) - OVERLAY_RULE_KEYS:
            raise IntegrityError(f"{where}: only {sorted(OVERLAY_RULE_KEYS)} may be set")
        if "severity" in change:
            if change["severity"] not in SEVERITIES:
                raise IntegrityError(f"{where}: severity must be one of {sorted(SEVERITIES)}")
            severity[rule_id] = change["severity"]
        if "allow" in change:
            allow[rule_id] = compile_list(change["allow"], f"{where} allow")
        if "exempt_paths" in change:
            exempt[rule_id] = tuple(string_list(change["exempt_paths"], f"{where} exempt_paths"))
    return RuleSet(
        rules.classes,
        tuple(
            replace(
                rule,
                severity=severity.get(rule.id, rule.severity),
                allow=rule.allow + allow.get(rule.id, ()),
                exempt_paths=rule.exempt_paths + exempt.get(rule.id, ()),
            )
            for rule in rules.rules
        ),
    )


def repository_overlay() -> Path | None:
    """The overlay file at the top of the repository the command runs in, if there is one."""
    top = Path(os.fsdecode(git(["rev-parse", "--show-toplevel"]).rstrip(b"\n")))
    path = top / OVERLAY_NAME
    return path if path.is_file() else None


# --- diff parsing --------------------------------------------------------------------


class Line(NamedTuple):
    """One added or removed line; ``number`` is its place in the new file, ``hunk`` the
    number of its hunk in the file's patch."""

    number: int
    text: str
    hunk: int = 0


@dataclass
class FileDiff:
    """One changed file. ``status`` is A (added, or copied), M, D or R (renamed from
    ``old_path``); a rename's lines are only the changes made on top of the move. A file that
    changed type has status D and ``typechange``: only its old side is judged. ``unreadable``
    says why the scanner could not read the change; such a file has no lines."""

    path: str
    status: str = "M"
    old_path: str | None = None
    binary: bool = False
    typechange: bool = False
    unreadable: str | None = None
    added: list[Line] = field(default_factory=list)
    removed: list[Line] = field(default_factory=list)

    @property
    def deleted(self) -> bool:
        return self.status == "D"


def decode(raw: bytes) -> str:
    """Text of one diff line; bytes that are not UTF-8 never stop the scan."""
    return raw.decode("utf-8", "replace")


def parse_patch(
    path: str, patch: bytes, deleted: bool = False, status: str | None = None, old_path: str | None = None
) -> FileDiff:
    """Parse a ``-U0`` patch of a single file. Hunk lengths decide what is content."""
    diff = FileDiff(path, status or ("D" if deleted else "M"), old_path)
    lines = patch.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    index = 0
    hunk = 0
    while index < len(lines) and not lines[index].startswith(b"@@"):
        header = lines[index]
        if header.startswith(b"Binary files ") or header == b"GIT binary patch":
            diff.binary = True
        elif header[:1] in (b"+", b"-") and not header.startswith((b"+++ ", b"--- ")):
            raise IntegrityError(f"cannot parse diff of {path}: content line before any hunk")
        index += 1
    while index < len(lines):
        found = HUNK.match(lines[index])
        if found is None:
            raise IntegrityError(f"cannot parse diff of {path}: expected a hunk header")
        old_left = 1 if found.group(2) is None else int(found.group(2))
        new_start = int(found.group(3))
        new_left = 1 if found.group(4) is None else int(found.group(4))
        new_line = new_start if new_left else new_start + 1
        if new_line < 1:
            new_line = 1
        index += 1
        hunk += 1
        while old_left or new_left:
            if index >= len(lines):
                raise IntegrityError(f"cannot parse diff of {path}: hunk ends early")
            raw = lines[index]
            index += 1
            if raw.startswith(b"\\"):
                continue  # "\ No newline at end of file"
            tag, text = raw[:1], decode(raw[1:])
            if tag == b"+" and new_left:
                diff.added.append(Line(new_line, text, hunk))
                new_line += 1
                new_left -= 1
            elif tag == b"-" and old_left:
                diff.removed.append(Line(new_line, text, hunk))
                old_left -= 1
            elif tag == b" " and old_left and new_left:
                new_line += 1
                old_left -= 1
                new_left -= 1
            else:
                raise IntegrityError(f"cannot parse diff of {path}: line does not fit its hunk")
        while index < len(lines) and lines[index].startswith(b"\\"):
            index += 1
    return diff


# --- git -----------------------------------------------------------------------------


# Configuration that changes the text of a diff, the files it lists or the automatic merge a
# merge commit is compared with. Each entry is passed as ``git -c``, which beats the
# repository's, the user's and the system's configuration and ``GIT_CONFIG_*`` alike. Entries
# that a command line flag of DIFF_FLAGS already pins (colour, renames) are not repeated.
GIT_CONFIG = (
    "core.quotePath=false",
    "merge.conflictStyle=merge",
    "merge.renames=true",
    "merge.directoryRenames=conflict",
    "merge.renameLimit=7000",
    "diff.noprefix=false",
    "diff.mnemonicPrefix=false",
    "diff.srcPrefix=a/",
    "diff.dstPrefix=b/",
    "diff.algorithm=myers",
    "diff.indentHeuristic=true",
    "diff.interHunkContext=0",
    "diff.relative=false",
    "diff.renameLimit=1000",
    "color.ui=false",
    "log.showSignature=false",
)
# Environment variables that change diff text or turn the pathspecs of a call into something
# else; they are removed before git runs. ``-c`` already beats the configuration variables.
GIT_ENV_DROP = frozenset(
    {
        "GIT_DIFF_OPTS",
        "GIT_EXTERNAL_DIFF",
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
    }
)


def run_git(args: list[str], stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Run git with the pinned configuration and environment; only a missing git raises."""
    command = ["git", *(part for setting in GIT_CONFIG for part in ("-c", setting)), *args]
    env = {key: value for key, value in os.environ.items() if key not in GIT_ENV_DROP}
    try:
        return subprocess.run(command, input=stdin, capture_output=True, check=False, env=env)
    except OSError as exc:
        raise IntegrityError(f"cannot run git: {exc}") from exc


def git(args: list[str]) -> bytes:
    """Run git with an argument list; any failure is an IntegrityError."""
    done = run_git(args)
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise IntegrityError(f"git {args[0]} failed: {detail}")
    return done.stdout


# Renames are found explicitly, whatever diff.renames says: a moved file is judged by the
# changes made on top of the move, not as a deletion plus an addition. ``--text`` makes git
# print a patch for a file its attributes (``-diff``, a driver with ``binary``) or its content
# call binary, so a repository's attributes cannot hide lines from the scan.
DIFF_FLAGS = [
    "--find-renames",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--text",
    "--ignore-submodules=none",
]


class Change(NamedTuple):
    """One line of ``git diff --raw``: ``old_path`` only for a rename."""

    status: str
    path: str
    old_path: str | None
    old_mode: bytes
    new_mode: bytes


def changed_files(command: list[str], base: list[str]) -> list[Change]:
    """Each changed file, from ``git <command> --raw -z``. A copy counts as an added file."""
    fields = git([*command, "--raw", "-z", *DIFF_FLAGS, *base]).split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    files: list[Change] = []
    index = 0
    while index < len(fields):
        meta = fields[index]
        parts = meta.split()
        if not meta.startswith(b":") or len(parts) < 5:
            raise IntegrityError("cannot parse the list of changed files from git")
        status = parts[4][:1].decode("ascii", "replace")
        count = 2 if status in ("R", "C") else 1
        paths = [os.fsdecode(raw) for raw in fields[index + 1 : index + 1 + count]]
        if len(paths) != count:
            raise IntegrityError("cannot parse the list of changed files from git")
        index += 1 + count
        modes = (parts[0][1:], parts[1])
        if status == "R":
            files.append(Change("R", paths[1], paths[0], *modes))
        elif status == "C":
            files.append(Change("A", paths[1], None, *modes))
        else:
            files.append(Change(status, paths[0], None, *modes))
    return files


def pathspecs(paths: list[str], names: list[str]) -> list[str]:
    """Literal pathspecs, from the top of the repository wherever the scan runs, for the files
    of one change; ``names`` is every path of the whole diff, sorted. A path also names a directory in git's pathspecs, so when a file and a
    directory of one name swap places, the patch of the file would hold the directory's files
    too: those are excluded one by one, and the patch of one file holds that file alone."""
    specs = [f":(top,literal){path}" for path in paths]
    for path in paths:
        first = bisect.bisect_left(names, path + "/")
        last = bisect.bisect_left(names, path + "0")  # "0" is the character after "/"
        specs += [f":(top,exclude,literal){name}" for name in names[first:last] if name not in paths]
    return specs


def read_change(command: list[str], base: list[str], change: Change, merge: bool, names: list[str]) -> FileDiff:
    """The lines of one changed file. What cannot be read comes back as a FileDiff with
    ``unreadable`` set, so the change becomes a finding instead of ending the run."""
    status, path, old_path = change.status, change.path, change.old_path
    if GITLINK in (change.old_mode, change.new_mode):
        return FileDiff(path, status, old_path, unreadable="a submodule changed")
    if status in ("U", "X"):
        return FileDiff(path, status, old_path, unreadable=f"git reports status {status} for this path")
    if status == "T":
        return FileDiff(path, "D", typechange=True)
    spec = pathspecs([old_path, path] if old_path else [path], names)
    patch = git([*command, "-p", "-U0", *DIFF_FLAGS, *base, "--", *spec])
    sections = sum(1 for line in patch.split(b"\n") if line.startswith(b"diff --git "))
    if sections != 1:
        return FileDiff(path, status, old_path, unreadable=f"git gave {sections} file patches for this one file")
    try:
        diff = parse_patch(path, patch, status=status, old_path=old_path)
    except IntegrityError as exc:
        return FileDiff(path, status, old_path, unreadable=str(exc))
    if merge:
        diff.removed = [line for line in diff.removed if not CONFLICT_MARKER.match(line.text)]
    return diff


def collect(base: list[str], merge: bool = False) -> list[FileDiff]:
    """Read every changed file of one diff, one git call per file. ``merge`` is for the one
    id of a merge commit: its diff is what the merge changed beyond the automatic merge, with
    the conflict markers that the resolution removed left out."""
    command = ["show", "--format=", "--remerge-diff"] if merge else ["diff"]
    changes = changed_files(command, base)
    names = sorted({name for change in changes for name in (change.path, change.old_path) if name})
    return [read_change(command, base, change, merge, names) for change in changes]


@functools.lru_cache(maxsize=1)
def require_remerge_diff() -> None:
    """A merge commit is judged with ``git show --remerge-diff``; an older git, or one whose
    version cannot be read, ends the run instead of judging the merge some other way."""
    need = ".".join(map(str, REMERGE_DIFF_GIT))
    found = re.search(r"(\d+)\.(\d+)", git(["--version"]).decode("utf-8", "replace"))
    if found is None or (int(found.group(1)), int(found.group(2))) < REMERGE_DIFF_GIT:
        raise IntegrityError(
            f"a merge commit needs git {need} or newer for --remerge-diff (this git: "
            f"{found.group(0) if found else 'version unreadable'})"
        )


def empty_tree() -> str:
    """The id of the empty tree in this repository's hash (sha1 or sha256)."""
    done = run_git(["hash-object", "-t", "tree", "--stdin"])
    if done.returncode != 0:
        raise IntegrityError(f"git hash-object failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("ascii").strip()


def resolve(rev: str) -> str:
    if rev.startswith("-"):
        raise IntegrityError(f"not a revision: {rev}")
    return git(["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"]).decode().strip()


def collect_commit(oid: str) -> list[FileDiff]:
    """The changes of one commit: against its parent, against the empty tree for a root
    commit, and for a merge commit against the automatic merge of its parents."""
    parents = git(["rev-list", "--parents", "-n", "1", oid]).decode().split()[1:]
    if len(parents) > 2:
        reason = f"an octopus merge ({len(parents)} parents): git has no --remerge-diff for it"
        return [FileDiff(OCTOPUS_PATH, unreadable=reason)]
    if len(parents) == 2:
        require_remerge_diff()
        return collect([oid], merge=True)
    return collect([parents[0] if parents else empty_tree(), oid])


def commit_message(oid: str) -> str:
    return decode(git(["show", "-s", "--format=%B", oid]))


def range_commits(spec: str) -> list[str]:
    if spec.startswith("-") or ".." not in spec:
        raise IntegrityError(f"--range needs A..B, got: {spec}")
    return git(["rev-list", "--reverse", spec, "--"]).decode().split()


OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def has_commit(oid: str) -> bool:
    return run_git(["cat-file", "-e", f"{oid}^{{commit}}"]).returncode == 0


def push_commits(refs: str) -> list[str]:
    """The commits a push brings, oldest first, from the lines git gives a pre-push hook:
    ``<local ref> <local sha> <remote ref> <remote sha>``. A deleted branch (local sha all
    zeros) brings none. A new branch (remote sha all zeros), or one whose remote sha this
    clone does not have, brings the commits of the local sha that no remote-tracking branch
    holds. Any other ref brings ``remote sha..local sha``."""
    seen: dict[str, None] = {}
    for line in refs.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4 or not all(OBJECT_ID.fullmatch(p) or set(p) == {"0"} for p in parts[1::2]):
            raise IntegrityError(f"unexpected line from git for a pre-push hook: {line[:80]!r}")
        local, remote = parts[1], parts[3]
        if set(local) == {"0"}:
            continue
        if set(remote) == {"0"} or not has_commit(remote):
            spec = [local, "--not", "--remotes"]
        else:
            spec = [f"{remote}..{local}"]
        for oid in git(["rev-list", "--reverse", *spec, "--"]).decode().split():
            seen.setdefault(oid)
    return list(seen)


# --- judging -------------------------------------------------------------------------


class Finding(NamedTuple):
    where: str  # short commit id or "staged"
    path: str
    line: int
    rule: str
    text: str
    justified: bool
    severity: str = BLOCK

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCK and not self.justified

    def __str__(self) -> str:
        path = CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", self.path)
        text = CONTROL_CHARS.sub(" ", self.text.strip())[:TEXT_WIDTH]
        mark = " note" if self.severity == NOTE else " justified" if self.justified else ""
        return f"{self.where} {path}:{self.line} {self.rule}{mark}  {text}"


class Hit(NamedTuple):
    path: str
    line: int
    rule: str
    text: str


def tested_sources(path: str) -> frozenset[str]:
    """File names of the source a test file tests, by the TEST_NAME conventions."""
    name = path.rpartition("/")[2]
    for pattern, shapes in TEST_NAME:
        found = pattern.match(name)
        if found:
            return frozenset(shape.format(*found.groups()) for shape in shapes)
    return frozenset()


def spared(diff: FileDiff, diffs: list[FileDiff]) -> bool:
    """True if a deleted test file's source file was deleted in the same change."""
    sources = tested_sources(diff.path)
    return any(
        other.deleted and other is not diff and other.path.rpartition("/")[2] in sources
        for other in diffs
    )


def gone(diff: FileDiff, rule: Rule, rules: RuleSet) -> bool:
    """True if a file of the rule's classes was deleted, or renamed out of those classes."""
    if diff.deleted:
        return bool(rules.classes_of(diff.path).intersection(rule.classes))
    if diff.status == "R" and diff.old_path is not None:
        before = rules.classes_of(diff.old_path).intersection(rule.classes)
        return bool(before) and not rules.classes_of(diff.path).intersection(rule.classes)
    return False


def weakens(old: str, new: str) -> bool:
    """For a line rewritten in place: True if the old line has a word the new one lacks, or
    the new line adds a conjunction to a CI condition (``if:``), which narrows when it runs.
    Only a reworded or widened line, with no word dropped, passes."""
    if set(WORD.findall(old)) - set(WORD.findall(new)):
        return True
    return bool(CONDITION.match(new)) and len(CONJUNCTION.findall(new)) > len(CONJUNCTION.findall(old))


def removed_hits(diff: FileDiff, rule: Rule) -> list[Hit]:
    """Removed lines the rule matches. Within a hunk that replaces as many lines as it adds
    (comment lines aside), each removed line is compared with the added line in its place
    and reported only if the rewrite weakens it."""
    hits = []
    for hunk in sorted({line.hunk for line in diff.removed}):
        gone_lines = [l for l in diff.removed if l.hunk == hunk and not rule.skips(l.text)]
        new_lines = [l for l in diff.added if l.hunk == hunk and not rule.skips(l.text)]
        pairs = zip(gone_lines, new_lines) if len(gone_lines) == len(new_lines) else None
        if pairs is None:
            hits += [Hit(diff.path, l.number, rule.id, l.text) for l in gone_lines if rule.matches(l.text)]
            continue
        hits += [
            Hit(diff.path, old.number, rule.id, old.text)
            for old, new in pairs
            if rule.matches(old.text) and weakens(old.text, new.text)
        ]
    return hits


def net_added_hits(diff: FileDiff, rule: Rule) -> list[Hit]:
    """Added lines with a marker the file did not lose on a removed line: a marker on a
    changed or moved line is the same marker, so only the net gain per marker is reported."""
    available = Counter(key for line in diff.removed for key in rule.markers(line.text))
    hits = []
    for line in diff.added:
        new = False
        for key in rule.markers(line.text):
            if available[key]:
                available[key] -= 1
            else:
                new = True
        if new:
            hits.append(Hit(diff.path, line.number, rule.id, line.text))
    return hits


def net_loss_hits(diffs: list[FileDiff], rule: Rule, rules: RuleSet) -> list[Hit]:
    """Counted over every file of the rule's classes in the change, so lines moved between
    files cancel out. When the change loses more than it gains, each file with a net loss of
    its own is reported at its first lost line. Deleted files are judged by deletion alone."""
    total = 0
    hits = []
    for diff in diffs:
        if diff.deleted or rule.exempts(diff) or not rules.classes_of(diff.path).intersection(rule.classes):
            continue
        lost = [l for l in diff.removed if rule.matches(l.text)]
        gained = sum(1 for l in diff.added if rule.matches(l.text))
        total += gained - len(lost)
        if len(lost) > gained:
            hits.append(Hit(diff.path, lost[0].number, rule.id, lost[0].text))
    return hits if total < 0 else []


def judge_file(diff: FileDiff, diffs: list[FileDiff], rules: RuleSet) -> list[Hit]:
    """Every per-file rule that fires on one file; ``diffs`` is the whole change."""
    if diff.unreadable:
        return []  # reported by unreadable_hits, not by the rules
    classes = rules.classes_of(diff.path)
    hits: list[Hit] = []
    for rule in rules.rules:
        if rule.exempts(diff):
            continue
        if rule.kind == "deleted":
            if gone(diff, rule, rules) and not (rule.spare_with_source and spared(diff, diffs)):
                hits.append(Hit(diff.path, 0, rule.id, "(file type changed)" if diff.typechange else "(file deleted)"))
            continue
        if diff.deleted or rule.kind == "net-loss" or not classes.intersection(rule.classes):
            continue  # a deleted file is judged by its deletion alone
        if rule.existing_only and diff.status == "A":
            continue
        if rule.kind == "added":
            hits += [Hit(diff.path, l.number, rule.id, l.text) for l in diff.added if rule.matches(l.text)]
        elif rule.kind == "net-added":
            hits += net_added_hits(diff, rule)
        else:  # removed
            hits += removed_hits(diff, rule)
    return hits


def overlay_hits(diffs: list[FileDiff]) -> list[Hit]:
    """One hit for each change to the overlay file at the top of the repository: a line added
    or removed, the file created, deleted or moved. The overlay is what the other rules are
    judged by, so changing it is never free."""
    hits = []
    for diff in diffs:
        if OVERLAY_NAME not in (diff.path, diff.old_path):
            continue
        lines = diff.added or diff.removed
        if lines or diff.status != "M":
            first = lines[0] if lines else Line(0, "(file added, deleted or moved)")
            hits.append(Hit(diff.path, first.number, OVERLAY_GUARD, first.text))
    return hits


def unreadable_hits(diffs: list[FileDiff]) -> list[Hit]:
    """One hit for each file whose change the scanner could not read. Such a change is never
    judged by the other rules, and never passes for an empty one."""
    return [Hit(diff.path, 0, UNREADABLE, diff.unreadable) for diff in diffs if diff.unreadable]


def judge_change(diffs: list[FileDiff], rules: RuleSet) -> list[Hit]:
    """Every rule that fires on one change, in diff order; a rule fires once per line."""
    hits = [hit for diff in diffs for hit in judge_file(diff, diffs, rules)]
    hits += overlay_hits(diffs) + unreadable_hits(diffs)
    for rule in rules.rules:
        if rule.kind == "net-loss":
            hits += net_loss_hits(diffs, rule, rules)
    order = {diff.path: number for number, diff in enumerate(diffs)}
    unique: dict[tuple[str, int, str], Hit] = {}
    for hit in hits:
        unique.setdefault((hit.path, hit.line, hit.rule), hit)
    return sorted(unique.values(), key=lambda hit: (order[hit.path], hit.line, hit.rule))


def justified(message: str) -> bool:
    """True if a top-level ``Check-change:`` line gives a reason of MIN_REASON characters."""
    for line in message.split("\n"):
        if SCISSORS.match(line):
            break
        found = JUSTIFICATION.fullmatch(line.rstrip("\r"))
        if found and len(found.group(1).strip()) >= MIN_REASON:
            return True
    return False


def judge(where: str, diffs: list[FileDiff], message: str, rules: RuleSet) -> list[Finding]:
    ok = justified(message)
    return [Finding(where, *hit, ok, rules.severity_of(hit.rule)) for hit in judge_change(diffs, rules)]


def read_message_file(path: Path | None) -> str:
    """A missing, unreadable or empty message file means no justification, never a pass."""
    if path is None:
        return ""
    try:
        return decode(path.read_bytes())
    except OSError:
        return ""


class Report(NamedTuple):
    findings: list[Finding]
    commits: int
    files: int
    staged: bool

    @property
    def blocking(self) -> int:
        return sum(1 for f in self.findings if f.blocking)

    @property
    def notes(self) -> int:
        return sum(1 for f in self.findings if f.severity == NOTE)


def run(args: argparse.Namespace, rules: RuleSet) -> Report:
    if args.staged:
        diffs = collect(["--cached"])
        message = read_message_file(args.message_file)
        return Report(judge("staged", diffs, message, rules), 0, len(diffs), True)
    if args.pre_push:
        commits = push_commits(sys.stdin.read())
    else:
        commits = [resolve(args.commit)] if args.commit else range_commits(args.range)
    findings: list[Finding] = []
    files = 0
    for oid in commits:
        diffs = collect_commit(oid)
        files += len(diffs)
        findings += judge(oid[:SHORT], diffs, commit_message(oid), rules)
    return Report(findings, len(commits), files, False)


# --- command line --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_integrity.py",
        description="Report changes that weaken tests, linters, hooks or CI. A finding is "
        "allowed when the commit message has a top-level line "
        f"'Check-change: <reason, {MIN_REASON}+ characters>'. Findings of a rule with severity "
        "'note' are printed and counted but never fail the run.",
        epilog="exit status: 0 ok, 1 unjustified blocking finding, 2 usage or git error",
        allow_abbrev=False,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan the staged diff")
    mode.add_argument(
        "--commit",
        metavar="REV",
        help="scan one commit against its parent; a merge commit against the automatic merge",
    )
    mode.add_argument("--range", metavar="A..B", help="scan each commit in the range")
    mode.add_argument(
        "--pre-push",
        action="store_true",
        help="scan the commits a push brings; reads git's pre-push lines from standard input",
    )
    parser.add_argument(
        "--message-file", type=Path, metavar="PATH", help="with --staged: the commit message file"
    )
    parser.add_argument("--rules", type=Path, metavar="PATH", default=DEFAULT_RULES)
    overlay = parser.add_mutually_exclusive_group()
    overlay.add_argument(
        "--overlay",
        type=Path,
        metavar="PATH",
        help=f"apply this file instead of {OVERLAY_NAME} at the top of the repository",
    )
    overlay.add_argument("--no-overlay", action="store_true", help=f"ignore {OVERLAY_NAME}")
    parser.add_argument("--json", action="store_true", help="print a JSON report on stdout")
    return parser


def summary(report: Report) -> str:
    scope = "staged" if report.staged else f"{report.commits} commits"
    return f"{scope}, {report.files} files"


def print_text(report: Report) -> int:
    for finding in report.findings:
        print(finding)
    notes = f" {report.notes} note(s) do not block." if report.notes else ""
    if report.blocking:
        print(
            f"{PROG}: {report.blocking} finding(s) without justification ({summary(report)}). If the "
            f"change is intended, add a top-level line 'Check-change: <why, {MIN_REASON}+ "
            f"characters>' to the commit message.{notes}"
        )
        return EXIT_FINDINGS
    parts = [summary(report)]
    justified_count = len(report.findings) - report.notes
    if justified_count:
        parts.append(f"{justified_count} justified finding(s)")
    if report.notes:
        parts.append(f"{report.notes} note(s), not blocking")
    print(f"{PROG}: ok ({', '.join(parts)})")
    return EXIT_OK


def print_json(report: Report) -> int:
    code = EXIT_FINDINGS if report.blocking else EXIT_OK
    document = {
        "status": "findings" if code else "ok",
        "commits": report.commits,
        "staged": report.staged,
        "files": report.files,
        "blocking": report.blocking,
        "notes": report.notes,
        "findings": [f._asdict() for f in report.findings],
    }
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.message_file is not None and not args.staged:
        parser.error("--message-file works only with --staged")
    sys.stdout.reconfigure(errors="backslashreplace")
    try:
        rules = load_rules(args.rules)
        overlay = None if args.no_overlay else args.overlay or repository_overlay()
        if overlay is not None:
            rules = load_overlay(overlay, rules)
        report = run(args, rules)
    except IntegrityError as exc:
        return fail(args, str(exc))
    except Exception as exc:  # a crash must be 2, never a pass or a plain finding
        return fail(args, f"internal error: {type(exc).__name__}: {exc}")
    return print_json(report) if args.json else print_text(report)


def fail(args: argparse.Namespace, message: str) -> int:
    if args.json:
        print(json.dumps({"status": "error", "error": message}))
    print(f"{PROG}: error: {message}", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

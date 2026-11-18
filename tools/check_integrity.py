#!/usr/bin/env python3
"""Check-integrity guard: find commits that weaken the checks instead of passing them.

It reads git diffs and commit messages and reports changes that skip or delete tests,
drop assertions, silence linters and type checkers, bypass hooks or CI, or remove lines
from check configuration. Such a change is allowed only when its commit message carries
a top-level line ``Check-change: <reason of 20 or more characters>``; one line covers
every finding of that commit. The rules live in ``check_integrity_rules.json``.

Exit status: 0 no findings or every finding justified, 1 an unjustified finding, 2 usage
or git error. The guard fails closed: an unreadable rules file, a failed git call or a
diff it cannot parse is an error, never a pass. Standard library only; one file, so it
can be copied into another repository together with its rules file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2
PROG = "check-integrity"
DEFAULT_RULES = Path(__file__).resolve().with_name("check_integrity_rules.json")
KINDS = frozenset({"added", "removed", "net-loss", "deleted"})
RULE_KEYS = frozenset({"id", "kind", "classes", "why", "patterns", "allow", "skip_lines"})
DOCUMENT_KEYS = frozenset({"version", "note", "classes", "rules"})

JUSTIFICATION = re.compile(r"Check-change:(.*)")
MIN_REASON = 20
SCISSORS = re.compile(r"^# -+ >8 -+$")
HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
SHORT = 7
TEXT_WIDTH = 80


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

    def matches(self, line: str) -> bool:
        """True if a pattern matches the line at a place no allow-regex claims."""
        if any(skip.search(line) for skip in self.skip_lines):
            return False
        for pattern in self.patterns:
            for found in pattern.finditer(line):
                if not any(allow.match(line, found.start()) for allow in self.allow):
                    return True
        return False


@dataclass(frozen=True)
class RuleSet:
    classes: dict[str, tuple[re.Pattern[str], ...]]
    rules: tuple[Rule, ...]

    def classes_of(self, path: str) -> frozenset[str]:
        return frozenset(
            name for name, regexes in self.classes.items() if any(r.search(path) for r in regexes)
        )


def compile_list(raw: object, where: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise IntegrityError(f"{where}: must be a list of non-empty strings")
    compiled = []
    for number, item in enumerate(raw, start=1):
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
    if raw["kind"] != "deleted" and not patterns:
        raise IntegrityError(f"{where}: kind {raw['kind']} needs patterns")
    return Rule(
        raw["id"],
        raw["kind"],
        tuple(classes),
        raw["why"],
        patterns,
        compile_list(raw.get("allow", []), f"{where} allow"),
        compile_list(raw.get("skip_lines", []), f"{where} skip_lines"),
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
    return RuleSet(classes, rules)


# --- diff parsing --------------------------------------------------------------------


class Line(NamedTuple):
    """One added or removed line; ``number`` is its place in the new file."""

    number: int
    text: str


@dataclass
class FileDiff:
    path: str
    deleted: bool = False
    binary: bool = False
    added: list[Line] = field(default_factory=list)
    removed: list[Line] = field(default_factory=list)


def decode(raw: bytes) -> str:
    """Text of one diff line; bytes that are not UTF-8 never stop the scan."""
    return raw.decode("utf-8", "replace")


def parse_patch(path: str, patch: bytes, deleted: bool) -> FileDiff:
    """Parse a ``-U0`` patch of a single file. Hunk lengths decide what is content."""
    diff = FileDiff(path, deleted)
    lines = patch.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    index = 0
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
        while old_left or new_left:
            if index >= len(lines):
                raise IntegrityError(f"cannot parse diff of {path}: hunk ends early")
            raw = lines[index]
            index += 1
            if raw.startswith(b"\\"):
                continue  # "\ No newline at end of file"
            tag, text = raw[:1], decode(raw[1:])
            if tag == b"+" and new_left:
                diff.added.append(Line(new_line, text))
                new_line += 1
                new_left -= 1
            elif tag == b"-" and old_left:
                diff.removed.append(Line(new_line, text))
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


def git(args: list[str]) -> bytes:
    """Run git with an argument list; any failure is an IntegrityError."""
    command = ["git", "--literal-pathspecs", "-c", "core.quotePath=false", *args]
    try:
        done = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:
        raise IntegrityError(f"cannot run git: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise IntegrityError(f"git {args[0]} failed: {detail}")
    return done.stdout


DIFF_FLAGS = ["--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none"]


def changed_files(base: list[str]) -> list[tuple[str, str]]:
    """``(status, path)`` for each changed file, from ``git diff --raw -z``."""
    fields = git(["diff", "--raw", "-z", *DIFF_FLAGS, *base]).split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise IntegrityError("cannot parse the list of changed files from git")
    files = []
    for meta, raw_path in zip(fields[0::2], fields[1::2]):
        parts = meta.split()
        if not meta.startswith(b":") or len(parts) < 5:
            raise IntegrityError("cannot parse the list of changed files from git")
        files.append((parts[4][:1].decode("ascii", "replace"), os.fsdecode(raw_path)))
    return files


def collect(base: list[str]) -> list[FileDiff]:
    """Parse every changed file of one diff, one git call per file."""
    diffs = []
    for status, path in changed_files(base):
        patch = git(["diff", "-p", "-U0", *DIFF_FLAGS, *base, "--", path])
        diffs.append(parse_patch(path, patch, deleted=status == "D"))
    return diffs


def empty_tree() -> str:
    """The id of the empty tree in this repository's hash (sha1 or sha256)."""
    try:
        done = subprocess.run(
            ["git", "hash-object", "-t", "tree", "--stdin"],
            input=b"",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise IntegrityError(f"cannot run git: {exc}") from exc
    if done.returncode != 0:
        raise IntegrityError(f"git hash-object failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("ascii").strip()


def resolve(rev: str) -> str:
    if rev.startswith("-"):
        raise IntegrityError(f"not a revision: {rev}")
    return git(["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"]).decode().strip()


def first_parent(oid: str) -> str:
    """The first parent of a commit, or the empty tree for a root commit."""
    parents = git(["rev-list", "--parents", "-n", "1", oid]).decode().split()[1:]
    return parents[0] if parents else empty_tree()


def commit_message(oid: str) -> str:
    return decode(git(["show", "-s", "--format=%B", oid]))


def range_commits(spec: str) -> list[str]:
    if spec.startswith("-") or ".." not in spec:
        raise IntegrityError(f"--range needs A..B, got: {spec}")
    return git(["rev-list", "--reverse", spec, "--"]).decode().split()


# --- judging -------------------------------------------------------------------------


class Finding(NamedTuple):
    where: str  # short commit id or "staged"
    path: str
    line: int
    rule: str
    text: str
    justified: bool

    def __str__(self) -> str:
        path = CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", self.path)
        text = CONTROL_CHARS.sub(" ", self.text.strip())[:TEXT_WIDTH]
        mark = " justified" if self.justified else ""
        return f"{self.where} {path}:{self.line} {self.rule}{mark}  {text}"


class Hit(NamedTuple):
    path: str
    line: int
    rule: str
    text: str


def judge_file(diff: FileDiff, rules: RuleSet) -> list[Hit]:
    """Every rule that fires on one file; a rule fires at most once per line."""
    classes = rules.classes_of(diff.path)
    hits: list[Hit] = []
    for rule in rules.rules:
        if not classes.intersection(rule.classes):
            continue
        if rule.kind == "deleted":
            if diff.deleted:
                hits.append(Hit(diff.path, 0, rule.id, "(file deleted)"))
        elif diff.deleted:
            continue  # a deleted file is judged by its deletion alone
        elif rule.kind == "added":
            hits += [Hit(diff.path, l.number, rule.id, l.text) for l in diff.added if rule.matches(l.text)]
        elif rule.kind == "removed":
            hits += [Hit(diff.path, l.number, rule.id, l.text) for l in diff.removed if rule.matches(l.text)]
        else:  # net-loss
            lost = [l for l in diff.removed if rule.matches(l.text)]
            gained = sum(1 for l in diff.added if rule.matches(l.text))
            if len(lost) > gained:
                hits.append(Hit(diff.path, lost[0].number, rule.id, lost[0].text))
    unique: dict[tuple[int, str], Hit] = {}
    for hit in hits:
        unique.setdefault((hit.line, hit.rule), hit)
    return sorted(unique.values(), key=lambda hit: (hit.line, hit.rule))


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
    return [
        Finding(where, *hit, ok) for diff in diffs for hit in judge_file(diff, rules)
    ]


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


def run(args: argparse.Namespace, rules: RuleSet) -> Report:
    if args.staged:
        diffs = collect(["--cached"])
        message = read_message_file(args.message_file)
        return Report(judge("staged", diffs, message, rules), 0, len(diffs), True)
    commits = [resolve(args.commit)] if args.commit else range_commits(args.range)
    findings: list[Finding] = []
    files = 0
    for oid in commits:
        diffs = collect([first_parent(oid), oid])
        files += len(diffs)
        findings += judge(oid[:SHORT], diffs, commit_message(oid), rules)
    return Report(findings, len(commits), files, False)


# --- command line --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_integrity.py",
        description="Report changes that weaken tests, linters, hooks or CI. A finding is "
        "allowed when the commit message has a top-level line "
        f"'Check-change: <reason, {MIN_REASON}+ characters>'.",
        epilog="exit status: 0 ok, 1 unjustified finding, 2 usage or git error",
        allow_abbrev=False,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan the staged diff")
    mode.add_argument("--commit", metavar="REV", help="scan one commit against its first parent")
    mode.add_argument("--range", metavar="A..B", help="scan each commit in the range")
    parser.add_argument(
        "--message-file", type=Path, metavar="PATH", help="with --staged: the commit message file"
    )
    parser.add_argument("--rules", type=Path, metavar="PATH", default=DEFAULT_RULES)
    parser.add_argument("--json", action="store_true", help="print a JSON report on stdout")
    return parser


def summary(report: Report) -> str:
    scope = "staged" if report.staged else f"{report.commits} commits"
    return f"{scope}, {report.files} files"


def print_text(report: Report) -> int:
    for finding in report.findings:
        print(finding)
    open_count = sum(1 for f in report.findings if not f.justified)
    if open_count:
        print(
            f"{PROG}: {open_count} finding(s) without justification ({summary(report)}). If the "
            f"change is intended, add a top-level line 'Check-change: <why, {MIN_REASON}+ "
            "characters>' to the commit message."
        )
        return EXIT_FINDINGS
    if report.findings:
        print(f"{PROG}: ok ({summary(report)}, {len(report.findings)} justified finding(s))")
    else:
        print(f"{PROG}: ok ({summary(report)})")
    return EXIT_OK


def print_json(report: Report) -> int:
    code = EXIT_FINDINGS if any(not f.justified for f in report.findings) else EXIT_OK
    document = {
        "status": "findings" if code else "ok",
        "commits": report.commits,
        "staged": report.staged,
        "files": report.files,
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

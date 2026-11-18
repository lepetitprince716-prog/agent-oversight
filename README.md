# agent-oversight

Run many coding agents without reading their transcripts. [中文说明](README.zh-CN.md)

When one person runs many agents (interactive sessions, background workers, scheduled runs),
reading along stops being possible. Reading along used to be the last check, and without it
four things go wrong:

- an agent reports work as done that is not done;
- bad news is said once, in a session nobody opens;
- the record of what is waiting, and of who holds what, stops matching reality;
- work is dropped when a session ends, and nothing notices.

This repository holds a small method for that situation, the templates that go with it, and
one read-only tool.

## The method

Six parts. The full text is in [docs/method.md](docs/method.md); what each part borrows, and
from whom, is in [docs/sources.md](docs/sources.md).

- Record. The tracker is the record and sessions are disposable.
- Evidence. Done is a claim with proof, and the maker is not the checker once the risk is
  more than low.
- Reconcile. A machine compares the record with the world and lists where they disagree.
- Escalate. Only a decision, a review or a notice reaches the human, each as one item in one
  inbox.
- Learn. Every problem that reached the human late becomes a check before it becomes a rule.
- Split work only where it pays. One writer per set of files; fan out for reading and checking.

## The tool

`oversight` reads a [beads](https://github.com/steveyegge/beads) task tracker, GitHub and
your local clones, and prints where the record has drifted from the truth. It never changes
anything: agents act on what it lists.

It needs Python 3.11 or later, the `bd` command, the GitHub CLI (`gh`) and `git`. There is
nothing to install.

```bash
mkdir -p ~/.config/agent-oversight
cp config.example.toml ~/.config/agent-oversight/config.toml   # then set the tracker folder and your repositories
bin/oversight reconcile
bin/oversight digest
```

To see it work without a tracker, run it on the recorded example:

```bash
bin/oversight --config tests/fixtures/drifty/config.toml --recorded tests/fixtures/drifty --now 2026-11-17T12:00:00Z reconcile
```

```text
BLOCKER_GONE  task-a1b2  Retry uploads on timeout
  PR #12 merged, last updated 2026-11-14
  -> The wait is over: close the task if nothing is left, otherwise reopen it for the queue.
...
QUIET_HOLDER  task-i9j0  Migrate the build script
  in progress, held by agent-3, no update for 7 days
  -> Check whether the holder is still running. If not, take the task over or return it to the queue.
SCHEDULE_RED  acme/widgets nightly  nightly
  failed 3 scheduled runs in a row, since 2026-11-15
  -> Find the failing step and fix it; file a task if none exists.
...
9 drift, 1 unchecked, 4 waiting
```

`digest` is the view for the human: what is waiting on them, by kind and age, then the drift,
in ten lines (`--max` changes that; `lang = "zh"` in the config puts the counts line and the
kind labels in Chinese).

```text
4 waiting on you, oldest 12 days. 9 drifts. 1 unchecked.
decide task-c3d4 Cache the schema lookup — 等你合并 PR #14 (6d)
review task-a1b2 Retry uploads on timeout — waiting for you to merge PR #12 (12d)
...
```

### What it checks

| Check | Drift |
|---|---|
| `blocker_gone` | A waiting task names a pull request that has merged or closed. |
| `blocker_stuck` | A waiting task names a pull request that is open but conflicting or failing its checks. |
| `silent_wait` | A waiting task whose notes do not say what it is waiting for. |
| `quiet_holder` | A task in progress with no update for `quiet_days`. |
| `schedule_red` | A scheduled CI workflow whose newest runs failed, with the length of the streak. |
| `pr_idle` | An open pull request not updated for `pr_days` that no task is waiting on: conflicting, failing, still a draft, or ready to merge and never merged. |
| `local_only` | A local branch, quiet for `quiet_days`, with commits that exist on no remote. Each commit is counted once, under the branch the others were built on. |
| `guard_off` | A repository marked `public` whose local clone has no executable pre-push hook. |

A task tells the tool which repository it belongs to with a `repo:<name>` label, and what it
waits for with a line in its notes after `blocker:` or `卡点：`. A pull request in that line
is looked up when it is written as `PR #12`, as `owner/name#12`, or as its URL. The kind of a
waiting item comes from a `wait:decision`, `wait:review` or `wait:notice` label
([templates/escalation.md](templates/escalation.md)).

The last two checks look at your disk and need the clone's folder in the config: `path` on a
repository turns on `local_only`, and `public = true` with it turns on `guard_off`
([PRIVACY.md](PRIVACY.md)).

### What to expect

- Exit code 0 means no drift and nothing unchecked, 1 means there is something to act on,
  2 means the tool could not do its job: the config or the tracker could not be read, or it
  hit a bug and the traceback is on stderr.
- Output is UTF-8, whatever the locale.
- A probe that fails is listed as `unchecked`. It is never counted as fine.
- GitHub computes whether a pull request can merge only when asked, so a first run can list a
  waiting task's pull request as unchecked. Run it again.
- `schedule_red` looks at the newest 20 scheduled runs of each active workflow. A disabled
  workflow is not looked at, and that includes one GitHub turned off because the repository
  was quiet for 60 days. A repository with 1000 or more open pull requests or workflows is
  listed as `unchecked`: the listing may be incomplete.
- A `path` in the config has to be the top folder of a clone, written as an absolute path or
  from `~`. A folder that is not the top of a clone is listed as `unchecked`; a relative path
  is refused when the config is read.
- `reconcile --json` prints the same report for a program.

## The commit check

`tools/check_integrity.py` reads commits and flags the ones that weaken a check instead of
passing it: a test skipped, deleted or renamed out of collection, assertions dropped, a linter or
type checker silenced, a hook or CI step bypassed, a line removed from a check config. It is one file with a rules file
next to it, standard library only.

A flagged commit passes when its message has a top-level line
`Check-change: <reason>` with a reason of 20 or more characters. One line covers every finding
of that commit. Only the commit message counts: a reason written next to the code does not.

Each rule has a level. `block`, the default, needs that line, otherwise the run exits 1. `note`
is printed and counted but never fails the run and needs no line. In the shipped rules only
`suppression-added` (a new `# noqa`, `# type: ignore`, `eslint-disable` and the like) is a note:
such a marker is usually a local judgment, and asking a reason for each one turns the line into
routine.

```bash
python3 tools/check_integrity.py --staged --message-file .git/COMMIT_EDITMSG   # what a commit-msg hook runs
python3 tools/check_integrity.py --commit HEAD                                 # one commit (a merge: see below)
python3 tools/check_integrity.py --range origin/main..HEAD                     # each commit in a range
python3 tools/check_integrity.py --pre-push < refs                             # the lines git gives a pre-push hook
```

Exit code 0 means no blocking finding or every one justified, 1 means an unjustified blocking
finding, 2 means the tool could not do its job (a usage error, a missing revision, a missing or
too old git, a failed git call, an unreadable rules file or a path given with `--overlay` that does not exist):
it never passes by default. `--json` prints the same report for a program, with a level on each
finding.

The diff the scanner reads does not depend on the clone it runs in. Every git call pins the
settings that change diff text, the file list or the automatic merge of a merge (prefixes,
algorithm, hunk context, renames, conflict style, file order, the blank-line spelling, replace refs,
message encoding, renormalizing and the like) and drops the environment
variables that do the same, and the patches are read as text, so a `-diff` attribute or a diff
driver cannot hide lines. A change the scanner cannot read is not an error and not a pass: it is
a blocking finding of the rule `unreadable-change`, and a `Check-change:` line waives it like any
other. That covers a patch that does not parse, a submodule change, an unmerged path and an
octopus merge. A file that changes type (a regular file becomes a symbolic link, or the reverse)
counts as the old file deleted. The scanner's own rules `unreadable-change` and `overlay-changed`
cannot be defined in a rules file or changed by an overlay.

The files that run the checks (hooks, CI workflows, `tools/check_integrity.py` and its rules
file, and the privacy scanner `tools/privacy_scan.py` with its rules `tools/privacy_rules.json`,
which the hooks and CI call as a second check; the class `machinery` of the rules file) are judged as a whole by the rule
`check-machinery-changed`, one finding per file: any line added or removed, a file created,
deleted or moved, other permission bits or another file type is blocking, whatever the lines say,
next to the line rules that also read those files. The loosening rule of `check-file-weakened`
for added lines covers lint and type-check configuration (the class `lint-config`: ruff, mypy,
flake8, pylint, pyright, pytest.ini, setup.cfg, tox.ini, coverage, eslint, biome, clippy, deny,
golangci, shellcheck, stylelint, markdownlint) as it does `pyproject.toml`.

A change that only moves version numbers in a hook or CI file is a note, not a block, because
dependency bots cannot write a reason line. The rules file lists the lines whose version a bot
may move in `version_lines`: a pinned or tagged `uses:`, `runs-on`, the version key of a runtime
or toolchain (`python-version`, `node-version` and the like), `image`, `container` and the `rev`
of a pre-commit hook. When a file of the class `bypass` is only modified in place and every
changed line pairs a removed with an added line (same hunk, same order) that is the same version
line once its version numbers and full hex digests are replaced by placeholders, the file reports
the note `version-bumped` instead of `check-file-weakened` and `check-machinery-changed`. Any other
changed line in the file, a version line outside the list, another number (a timeout, a coverage
floor), a different action, a moved or retyped file or new permission bits keeps the blocking
findings. Without `version_lines` nothing is a bump. The overlay may only set the severity of
`version-bumped`; `block` makes a bump need a reason line again.

Three rules catch a test that is switched off by editing it, not by deleting it:

- `test-short-circuited` fires on a bare `return` (or `return None`, or `return;`) that is the
  first statement of a test: the test ends before its assertions and still passes. A test is a
  Python `def test...` (async included), any Rust `fn`, a JS or TS `it(` or `test(` callback and a
  Go `func Test...`. The rule reads the line above the added line in the new file, so a return
  moved from one test to another is reported and one that was already there is not.
- `test-class-detached` fires when the change loses a class with a `TestCase` base over all test
  files (the base dropped, or the class deleted): unittest no longer collects its tests.
- `tautology-added` fires on an added assertion that cannot fail: `assertTrue(True)`,
  `assertFalse(False)`, `assert True`, `assertEqual` or `assertIs` of two identical sides,
  `assert x == x`, `expect(x).toBe(x)`, `assert_eq!(x, x)` (a side is one identifier, one
  attribute path or one literal), `assertIsNone(None)`, `assertIsNotNone` of a literal, and
  `assertTrue` or `assert` of a non-zero number or a non-empty string. A moved line is not new.

A `Check-change:` line waives each of the three like any other finding.

Four more cheap ways to switch a check off are covered, by rules that already existed and by one
new list in the rules file:

- `test-removed` also reports, once, a test file renamed from a name the test runners collect
  (`test_*.py`, `*_test.py`, `x.test.*` and `x.spec.*` of JavaScript and TypeScript, `*_test.go`)
  to a name they do not, such as `tests/x_cases.py`: the tests are still on disk and no longer
  run. The source of the old name spares it, as for a deletion.
- `skip-added` also reports a new condition that is always false in an existing test file
  (`if False:`, `if 0:`, `if (false) {`, `if false {`): what it guards never runs. A moved line is
  not new.
- A flag that makes a runner collect without running (`--co`, `--collect-only`,
  `--passWithNoTests`, `--exit-zero`) on an added line of a hook or CI file or of a script is
  reported by `bypass-added`, and on an added line of a lint or test configuration by
  `check-file-weakened`: the run is told to pass without running the tests.
- `guard_lines` in the rules file lists the lines that set a floor (`fail_under`, `fail-under`,
  `--cov-fail-under`, `coverageThreshold`). A floor line rewritten in place is reported by
  `check-file-weakened` when the new text differs at all, even when every word is kept (`90`
  to `.90`). Raising the floor is reported too and needs its reason line.

A merge commit is judged by what the merge changed beyond the automatic merge
(`git show --remerge-diff`), so a clean merge has no findings and the commits it brought in are
judged on their own, once. A resolution that adds a skip or drops a check line is reported on the
merge; the conflict markers a resolution removes are not. This needs git 2.36 or newer; with an
older git the run exits 2 and says so, instead of judging the merge some other way. An octopus
merge (three or more parents, which git cannot remerge-diff) is one `unreadable-change` finding.

`--staged` judges a merge the same way while it waits to be committed (a `MERGE_HEAD` exists,
whether after a conflict or `git merge --no-commit`): it compares the staged result with the
automatic merge (`git merge-tree --write-tree`, git 2.38 or newer, else exit 2) instead of with
`HEAD`, so a clean merge does not make the commit-msg hook ask for the reasons of the commits it
brings in, and only what the resolution changed is judged. Conflict markers that the resolution
removes are not findings. A merge in progress with more than two parents is one
`unreadable-change` finding.

### Adjusting the rules for one repository

A `.check-integrity.json` at the top of a repository is laid over the rules. Per rule id it may
change `severity`, append `allow` regexes and list `exempt_paths`, nothing else; any other key, or
an id the rules do not have, is an error (exit 2). `allow` regexes are tried at the start of each
pattern match, in every file the rule covers. `exempt_paths` are globs on the repository-relative
path (`fnmatch` rules: `*` also matches `/`); a file that matches one is not judged by that rule
at all. A renamed file is exempt only when its old and its new path both match.

```json
{
  "version": 1,
  "rules": {
    "skip-added": { "severity": "note" },
    "bypass-added": { "allow": ["\\|\\|\\s*true\\b.*# best effort"] }
  }
}
```

The file is itself guarded: any change to it (a line added or removed, the file created or
deleted) is a blocking finding of the scanner's own rule `overlay-changed` and needs a
`Check-change:` line, so a commit cannot loosen the rules it is judged by without saying why.
An overlay cannot name `overlay-changed` or `unreadable-change`, and `allow` is refused on a
rule id whose entries all judge whole files, such as `check-machinery-changed`: there is no line
to allow.

The overlay that judges a change is the one in that change's baseline commit, never the
working tree's and never the judged commit's own: an edit that was never staged cannot loosen the
judge. The baseline is `HEAD` for `--staged` (also while a merge is in progress), the first parent
for `--commit`, the left end of `--range` (the merge base for `A...B`) and the remote side for
`--pre-push`. A branch that is new on its remote is judged by the overlay at its merge base with
`refs/remotes/<remote>/HEAD`, and when that is not known by the first parent of its oldest new
commit; a root commit or a range without a common ancestor has no baseline and no overlay. A
baseline overlay that is not valid (not UTF-8, not a regular file, a bad key) is applied as
nothing: one line goes to standard error and every rule is judged as `block`, so the commit that
repairs the file is not held back by the broken one. `--overlay PATH` reads that file instead (the
CI step hands it the baseline's) and one that is there but not valid is treated the same way, so a
broken file cannot stop every run; a path that does not exist exits 2. `--no-overlay` reads none.

This repository's own file exempts `tests/test_check_integrity.py` from `skip-added`,
`suppression-added` and `bypass-added`: that file holds marker strings as data. Editing those
strings needs no reason line; deleting a test or an assertion there still does, and so does a
skip added in any other test file.

### Using it in another repository

Copy `tools/check_integrity.py` and `tools/check_integrity_rules.json`, then call it from the
repository's hooks. The first lines of this repository's `.githooks/commit-msg` and
`.githooks/pre-push` show how to run it next to another check so that either one refuses.

```sh
#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
python3 -I -B tools/check_integrity.py --staged --message-file "$1"
```

`-I` (isolated mode) keeps the scanner's own folder and the `PYTHON*` environment variables out of
the module search path, so a file dropped next to the scanner cannot stand in for a standard
library module it imports.

For a pre-push hook, pipe git's standard input into `--pre-push` and pass the remote's name
(`--remote "$1"`), which a new branch needs to find its baseline; a new branch is scanned for the
commits that no remote-tracking branch has.
[.github/workflows/ci.yml](.github/workflows/ci.yml) runs the same scan on every pull request and
on every push. The workflow has two jobs: `gates` holds the privacy scan and this scan and no
other step, so it runs no code from the change being judged, and `tests` runs the repository's own
tests. On a pull request `gates` checks out the head commit and scans it and its history, not the
merge commit GitHub makes for the run (that commit carries the web identity of the account that
opened the pull request, which the privacy scan would refuse). The scanners start in isolated mode (`python3 -I -B`). A newer run replaces an older one
only on a pull request: a push is scanned from its old tip to its new one, so a cancelled push
run would leave the commits it brought unscanned. The scanner, its rules and `.check-integrity.json` always come from a baseline, not
from the commits being judged: the base branch for a pull request, the default branch for a push
to another branch, the old tip for a push to the default branch (a baseline without a scanner is
judged by the copy in the change itself). A pull request scans the commits it adds; a new branch
or a force push scans every commit from its merge base with the default branch, and only the top
commit when there is none. A push to the default branch scans the commits it brings, but a
finding there is a warning and the step passes (the push has landed, and a squash merge leaves
commits that carry none of the `Check-change:` lines of the commits it was made of); an error of
the scanner (exit 2) still fails the step. The first push or a force push to the default branch
has nothing to scan and says so.

### Limits of the commit check

- The local hooks, and the scanner they call, come from the working tree. Anyone or any agent
  with write access can switch them off locally. The CI step is the stronger check, and it has
  limits of its own:
  - The `gates` job runs no code from the change being judged: the repository's tests run in the
    separate `tests` job, and the scanners start in isolated mode, so a file the change drops next
    to them cannot stand in for a standard library module. The step judges with the scanner, the
    rules and the overlay of the baseline.
  - When a pull request changes the workflow file itself, GitHub runs the pull request's own
    version of it, and that change can rewrite this step. Such a change has to be read by a
    person; the scan of the push to the default branch after the merge reports it as a warning.
  - When the rules file or the scanner on the default branch is itself broken (it cannot be
    loaded), this step fails for every pull request, the one that repairs it included, and the
    repair can only merge with a red check. Keeping a broken rules file from being merged in the
    first place is one of the things the `tests` job is for.
- A push to the default branch is scanned, but a finding there is only a warning: the step does
  not fail.
- When a pull request is merged with GitHub's merge button, GitHub documents that the commit that
  lands on the default branch is written with the web identity of the account that merges. If that
  account has not turned on "Keep my email addresses private", the address is not on the allowed
  list and the privacy scan of the default branch fails after the merge. Either turn the setting on
  in the account's GitHub email settings, or merge locally and push. This was checked only on the
  merge commit GitHub makes for a pull request run, not on a button merge in this repository.
- The rules are tripwires that read one line at a time, not a proof. Swapping an assertion for a
  weaker one that is not always true, or moving the checking logic into a script that is not in
  the `machinery` class, goes unseen. A repository can add such a script to the `machinery` class
  in its own rules file.
- A commit with a reason line passes. The line exists so that such a change can be found in
  `git log`, not to stop it.
- Each of the three rules for tests edited in place has forms it does not see.
  `test-short-circuited` sees only a bare `return` as the first statement of a test: a
  conditional return in the middle of a test, a docstring or a multi-line definition between the
  definition line and the `return` are not seen. `test-class-detached` looks for `TestCase` on one
  line, so a base changed to an indirect subclass of `TestCase` is reported although the tests
  are still collected (a false positive). `tautology-added` knows only the forms in its table:
  the same call written twice and compared is not one of them, because it cannot be told from a
  deterministic test, and other always-true assertions go unseen.
- More ways to switch a test or a check off that the rules do not see:
  - A `yield` added to a test function makes it a generator, so the runner no longer executes its
    body. A reader that goes line by line cannot tell it from the `yield` of an ordinary fixture,
    so it is not reported.
  - A base class written as an expression (`unittest.TestCase.__base__`, which is `object`) is not
    reported: the class line still holds the word `TestCase`, so no class counts as lost.
  - A file the change adds is not judged. A new `conftest.py`, or a new `tests/__init__.py`, that
    replaces the assertion methods when it is imported turns the whole suite green and is not
    reported.
  - A constant-false condition is recognized in four forms only: `if False:`, `if 0:`,
    `if (false) {` and `if false {`. `else if (false)`, `if (0)` and `if (False):` are not.
  - `coverageThreshold` in `package.json` and in `jest.config.*` is not watched. `package.json`
    is in the class `script`, which has no rule for a rewritten line, and `jest.config.*` is in no
    class.
  - The four flags above are reported on any added line of a hook, CI file, script or lint
    configuration, whatever that line runs, and on no line of a file outside those classes, such
    as a Python source file that builds a `pytest --collect-only` command.
- Changes that lose nothing but need a reason line:
  - Two sides of a merge that edited the same assertion in a test file: whichever side the
    resolution drops, `--ours`, `--theirs` or a hand edit, the dropped assertion is reported by
    `assertion-loss` on the merge commit. A merge made with `-X ours` or `-X theirs` is reported
    the same way.
  - A coverage floor that goes up, and a floor line where only a trailing comment or the
    indentation changed: `guard_lines` reports any rewrite of the line.
- False positives seen on real history, which a reason line clears:
  - `skip-added` reports each added `skipTest(...)` or `skipUnless(...)` line, also a skip that
    only guards the environment (running as root, a sandbox without semaphores) and a line whose
    only change is the wording of its message, because the rewritten line is not the same text.
  - The rules that read whole hook and CI files report a restructured workflow (jobs renamed,
    a step moved to another job, a changed matrix) once per removed line; in the cases read, no
    check was switched off.

## The templates

- [templates/handoff.md](templates/handoff.md): the handoff file and its writing rules
  ([中文模板](templates/handoff.zh-CN.md)).
- [templates/landing.md](templates/landing.md): how a session ends.
- [templates/escalation.md](templates/escalation.md): the three kinds of inbox item.
- [templates/worker-packet.md](templates/worker-packet.md): the brief for a worker and what it
  hands back.
- [templates/instructions.md](templates/instructions.md): a block for `AGENTS.md` or
  `CLAUDE.md`.

## Adopting it

1. Put work that outlives a turn into a tracker, and label tasks with `repo:<name>`.
2. Paste the block from [templates/instructions.md](templates/instructions.md) into your
   agents' instruction file and adjust it.
3. Have any session that coordinates work start with `oversight reconcile` and act on the list.
4. When you want to know what needs you, ask for `oversight digest`.

## Status and limits

Version 0.1. It comes from one person's practice with beads and GitHub; another tracker needs
an adapter in `oversight/tracker.py`. The method does not remove the human, and not reading has
a measured cost: see the Limits section of [docs/method.md](docs/method.md).

## Development

```bash
python3 -m unittest discover -s tests
```

The tests need neither `bd`, `gh`, `git` nor the network. Commits go through the privacy
guard in [PRIVACY.md](PRIVACY.md), and CI runs the tests and the same scan on every push and
pull request.

## License

[MIT](LICENSE)

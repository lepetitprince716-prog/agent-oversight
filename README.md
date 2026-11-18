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
passing it: a test skipped or deleted, assertions dropped, a linter or type checker silenced, a
hook or CI step bypassed, a line removed from a check config. It is one file with a rules file
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
python3 tools/check_integrity.py --commit HEAD                                 # one commit
python3 tools/check_integrity.py --range origin/main..HEAD                     # each commit in a range
python3 tools/check_integrity.py --pre-push < refs                             # the lines git gives a pre-push hook
```

Exit code 0 means no blocking finding or every one justified, 1 means an unjustified blocking
finding, 2 means the tool could not do its job (an unreadable rules file, a failed git call, a
diff it cannot parse): it never passes by default. `--json` prints the same report for a
program, with a level on each finding.

### Adjusting the rules for one repository

A `.check-integrity.json` at the top of a repository is laid over the rules. Per rule id it may
set `severity` and append `allow` regexes, nothing else; any other key, or an id the rules do
not have, is an error (exit 2). `allow` regexes are tried at the start of each pattern match,
in every file the rule covers.

```json
{
  "version": 1,
  "rules": {
    "skip-added": { "severity": "note" },
    "bypass-added": { "allow": ["\\|\\|\\s*true\\b.*# best effort"] }
  }
}
```

The file is itself a check file: removing a line from it needs a `Check-change:` line like any
other weakening. `--overlay PATH` reads another file, and `--no-overlay` reads none.

This repository's own file spares the marker strings that the scanner's tests hold as data: a
`skip-added` match on a line that also holds a literal `\n`, or that sits right after the
scanner's own output text. Editing those strings needs no reason line; deleting a test or an
assertion still does.

### Using it in another repository

Copy `tools/check_integrity.py` and `tools/check_integrity_rules.json`, then call it from the
repository's hooks. The first lines of this repository's `.githooks/commit-msg` and
`.githooks/pre-push` show how to run it next to another check so that either one refuses.

```sh
#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
python3 tools/check_integrity.py --staged --message-file "$1"
```

For a pre-push hook, pipe git's standard input into `--pre-push`; a new branch is scanned for
the commits that no remote-tracking branch has. [.github/workflows/ci.yml](.github/workflows/ci.yml)
runs the same scan on every push and pull request, taking the scanner, its rules and
`.check-integrity.json` from the base branch on a pull request.

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

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from oversight.config import Repo
from oversight.clones import GitCli, LocalBranch, LocalError, RecordedLocal

COUNT_ALL = "rev-list --count --branches --not --remotes"
LIST = "for-each-ref refs/heads --format=%(refname:lstrip=2)%09%(upstream:short)%09%(committerdate:iso-strict)"
HOOKS = "rev-parse --git-path hooks"
TOP = "rev-parse --show-toplevel"


def count_of(name, *earlier):
    refs = "".join(f" refs/heads/{each}" for each in earlier)
    return f"rev-list --count refs/heads/{name} --not --remotes{refs}"


# A stand-in for `git`: logs the command it was given and answers from script.json in the clone.
# A string is printed, a number is the exit code, a command the script does not know exits 99.
FAKE_GIT = (
    "import json, pathlib, sys\n"
    "args = sys.argv[1:]\n"
    "assert args[0] == '-C'\n"
    "clone = pathlib.Path(args[1])\n"
    "key = ' '.join(args[2:])\n"
    "with (clone / 'calls.log').open('a') as log:\n"
    "    log.write(key + '\\n')\n"
    "reply = json.loads((clone / 'script.json').read_text()).get(key)\n"
    "if reply is None:\n"
    "    sys.exit(99)\n"
    "if isinstance(reply, int):\n"
    "    print('fatal: simulated', file=sys.stderr)\n"
    "    sys.exit(reply)\n"
    "sys.stdout.write(reply)\n"
)


class Clone(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.repo = Repo("widgets", "acme/widgets", True, self.path, True)
        self.git = GitCli((sys.executable, "-c", FAKE_GIT), 20)

    def script(self, replies):
        # Unless a test says otherwise, git finds the clone at the configured path.
        replies = {TOP: f"{self.path}\n", **replies}
        (self.path / "script.json").write_text(json.dumps(replies), encoding="utf-8")

    def calls(self):
        """The commands after the first, which always asks where the clone's top folder is."""
        lines = (self.path / "calls.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], TOP)
        return lines[1:]


class BranchesTest(Clone):
    def test_only_branches_with_unpushed_commits_come_back(self):
        self.script({
            COUNT_ALL: "5\n",
            LIST: (
                "main\torigin/main\t2026-11-01T08:00:00+00:00\n"
                "feature/a/b\t\t2026-11-10T09:30:00+01:00\n"
                "fix/retry-budget\t\t2026-11-12T00:00:00-05:00\n"
            ),
            count_of("main"): "0\n",
            count_of("feature/a/b"): "2\n",
            count_of("fix/retry-budget"): "3\n",
            count_of("fix/retry-budget", "feature/a/b"): "3\n",
        })
        self.assertEqual(
            self.git.unpushed_branches(self.repo),
            (
                LocalBranch("feature/a/b", 2, datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc)),
                LocalBranch("fix/retry-budget", 3, datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)),
            ),
        )
        self.assertEqual(
            self.calls(),
            [
                COUNT_ALL,
                LIST,
                count_of("main"),
                count_of("feature/a/b"),
                count_of("fix/retry-budget"),
                count_of("fix/retry-budget", "feature/a/b"),
            ],
        )

    def test_a_branch_grown_from_another_reports_only_its_own_commits(self):
        self.script({
            COUNT_ALL: "7\n",
            LIST: (
                "main\torigin/main\t2026-11-01T08:00:00+00:00\n"
                "feature\t\t2026-11-02T08:00:00+00:00\n"
                "side\t\t2026-11-03T08:00:00+00:00\n"
            ),
            count_of("main"): "3\n",
            count_of("feature"): "5\n",
            count_of("side"): "1\n",
            count_of("main", "side"): "3\n",
            count_of("feature", "side", "main"): "2\n",
        })
        self.assertEqual(
            [(each.name, each.only_here) for each in self.git.unpushed_branches(self.repo)],
            [("main", 3), ("feature", 2), ("side", 1)],
        )
        # The second pass goes fewest first and names every branch already reported.
        self.assertEqual(
            self.calls()[5:],
            [count_of("main", "side"), count_of("feature", "side", "main")],
        )

    def test_two_branches_on_one_commit_report_the_one_that_tracks_a_remote(self):
        self.script({
            COUNT_ALL: "3\n",
            LIST: "copy\t\t2026-11-01T08:00:00+00:00\nmain\torigin/main\t2026-11-01T08:00:00+00:00\n",
            count_of("copy"): "3\n",
            count_of("main"): "3\n",
            count_of("copy", "main"): "0\n",
        })
        self.assertEqual(
            [(each.name, each.only_here) for each in self.git.unpushed_branches(self.repo)],
            [("main", 3)],
        )

    def test_two_untracked_branches_on_one_commit_report_the_first_by_name(self):
        self.script({
            COUNT_ALL: "3\n",
            LIST: "main\t\t2026-11-01T08:00:00+00:00\ncopy\t\t2026-11-01T08:00:00+00:00\n",
            count_of("main"): "3\n",
            count_of("copy"): "3\n",
            count_of("main", "copy"): "0\n",
        })
        self.assertEqual(
            [(each.name, each.only_here) for each in self.git.unpushed_branches(self.repo)],
            [("copy", 3)],
        )

    def test_a_tracked_branch_with_more_commits_still_goes_after_the_one_with_fewer(self):
        self.script({
            COUNT_ALL: "5\n",
            LIST: "main\torigin/main\t2026-11-01T08:00:00+00:00\nbase\t\t2026-11-01T08:00:00+00:00\n",
            count_of("main"): "5\n",
            count_of("base"): "2\n",
            count_of("main", "base"): "3\n",
        })
        self.assertEqual(
            [(each.name, each.only_here) for each in self.git.unpushed_branches(self.repo)],
            [("main", 3), ("base", 2)],
        )

    def test_unrelated_branches_keep_their_counts(self):
        self.script({
            COUNT_ALL: "5\n",
            LIST: "a\t\t2026-11-01T08:00:00+00:00\nb\t\t2026-11-01T08:00:00+00:00\n",
            count_of("a"): "2\n",
            count_of("b"): "3\n",
            count_of("b", "a"): "3\n",
        })
        self.assertEqual(
            [(each.name, each.only_here) for each in self.git.unpushed_branches(self.repo)],
            [("a", 2), ("b", 3)],
        )

    def test_a_failing_second_pass_is_a_local_error(self):
        self.script({
            COUNT_ALL: "5\n",
            LIST: "a\t\t2026-11-01T08:00:00+00:00\nb\t\t2026-11-01T08:00:00+00:00\n",
            count_of("a"): "2\n",
            count_of("b"): "3\n",
            count_of("b", "a"): 128,
        })
        with self.assertRaises(LocalError):
            self.git.unpushed_branches(self.repo)

    def test_a_total_of_zero_stops_before_the_per_branch_commands(self):
        self.script({COUNT_ALL: "0\n", LIST: "main\torigin/main\t2026-11-01T08:00:00+00:00\n"})
        self.assertEqual(self.git.unpushed_branches(self.repo), ())
        self.assertEqual(self.calls(), [COUNT_ALL])

    def test_a_repository_with_no_branches(self):
        self.script({COUNT_ALL: "0\n"})
        self.assertEqual(self.git.unpushed_branches(self.repo), ())

    def test_every_failure_is_a_local_error(self):
        listing = "main\t\t2026-11-01T08:00:00+00:00\n"
        cases = {
            "git exits non-zero": {COUNT_ALL: 128},
            "listing fails": {COUNT_ALL: "1\n", LIST: 128},
            "count fails": {COUNT_ALL: "1\n", LIST: listing, count_of("main"): 128},
            "total is not a number": {COUNT_ALL: "many\n"},
            "total is negative": {COUNT_ALL: "-1\n"},
            "count is not a number": {COUNT_ALL: "1\n", LIST: listing, count_of("main"): "\n"},
            "line without a tab": {COUNT_ALL: "1\n", LIST: "main 2026-11-01T08:00:00+00:00\n"},
            "line with two fields": {COUNT_ALL: "1\n", LIST: "main\t2026-11-01T08:00:00+00:00\n"},
            "line with four fields": {COUNT_ALL: "1\n", LIST: "main\t\t2026-11-01T08:00:00+00:00\tmore\n"},
            "line without a name": {COUNT_ALL: "1\n", LIST: "\t\t2026-11-01T08:00:00+00:00\n"},
            "time is not ISO": {COUNT_ALL: "1\n", LIST: "main\t\tyesterday\n"},
            "time has no zone": {COUNT_ALL: "1\n", LIST: "main\t\t2026-11-01T08:00:00\n"},
        }
        for name, replies in cases.items():
            with self.subTest(name):
                self.script(replies)
                with self.assertRaises(LocalError) as caught:
                    self.git.unpushed_branches(self.repo)
                self.assertEqual((caught.exception.repo, caught.exception.what), ("widgets", "local-only commits"))

    def test_the_reason_carries_gits_first_line(self):
        self.script({COUNT_ALL: 128})
        with self.assertRaises(LocalError) as caught:
            self.git.unpushed_branches(self.repo)
        self.assertIn("exited 128: fatal: simulated", str(caught.exception))

    def test_a_missing_or_slow_git_is_a_local_error(self):
        with self.assertRaises(LocalError):
            GitCli(("no-such-program-oversight",)).unpushed_branches(self.repo)
        with self.assertRaises(LocalError):
            GitCli((sys.executable, "-c", "import time; time.sleep(5)"), 0.2).unpushed_branches(self.repo)

    def test_a_folder_inside_another_clone_is_a_local_error(self):
        outer = self.path
        inner = outer / "plain-folder"
        inner.mkdir()
        (inner / "script.json").write_text(
            json.dumps({TOP: f"{outer}\n", COUNT_ALL: "1\n", HOOKS: f"{outer}/.git/hooks\n"}), encoding="utf-8"
        )
        (outer / ".git/hooks").mkdir(parents=True)
        (outer / ".git/hooks/pre-push").write_text("#!/bin/sh\n", encoding="utf-8")
        (outer / ".git/hooks/pre-push").chmod(0o755)
        repo = Repo("widgets", "acme/widgets", True, inner, True)
        for read in (self.git.unpushed_branches, self.git.has_pre_push_hook):
            with self.subTest(read.__name__):
                with self.assertRaises(LocalError) as caught:
                    read(repo)
                self.assertIn("not the top folder of a clone", str(caught.exception))
        # Nothing after the question about the top folder was asked.
        self.assertEqual((inner / "calls.log").read_text(encoding="utf-8").splitlines(), [TOP, TOP])

    def test_the_top_folder_may_be_spelled_another_way(self):
        link = self.path.parent / f"{self.path.name}-link"
        link.symlink_to(self.path, target_is_directory=True)
        self.addCleanup(link.unlink)
        self.script({TOP: f"{self.path.resolve()}\n", COUNT_ALL: "0\n"})
        self.assertEqual(self.git.unpushed_branches(Repo("widgets", "acme/widgets", True, link, True)), ())

    def test_a_top_folder_that_cannot_be_compared_is_a_local_error(self):
        cases = {
            "git cannot say": {TOP: 128},
            "the folder git names is gone": {TOP: f"{self.path}/no-such-folder\n"},
            "git prints nothing": {TOP: ""},
        }
        for name, replies in cases.items():
            with self.subTest(name):
                self.script(replies)
                with self.assertRaises(LocalError):
                    self.git.unpushed_branches(self.repo)
                with self.assertRaises(LocalError):
                    self.git.has_pre_push_hook(self.repo)

    def test_a_repository_without_a_path_is_a_local_error(self):
        with self.assertRaises(LocalError):
            self.git.unpushed_branches(Repo("widgets", "acme/widgets", True, None, True))
        with self.assertRaises(LocalError):
            self.git.has_pre_push_hook(Repo("widgets", "acme/widgets", True, None, True))


class HookTest(Clone):
    def hook(self, answer, mode=0o755):
        self.script({HOOKS: answer})
        target = (self.path / answer.strip()) if not answer.startswith("/") else Path(answer.strip())
        target.mkdir(parents=True, exist_ok=True)
        (target / "pre-push").write_text("#!/bin/sh\n", encoding="utf-8")
        (target / "pre-push").chmod(mode)

    def test_executable_hook_in_a_relative_hooks_directory(self):
        self.hook(".githooks\n")
        self.assertTrue(self.git.has_pre_push_hook(self.repo))
        self.assertEqual(self.calls(), [HOOKS])

    def test_executable_hook_in_an_absolute_hooks_directory(self):
        self.hook(f"{self.path}/elsewhere\n")
        self.assertTrue(self.git.has_pre_push_hook(self.repo))

    def test_a_hook_that_is_not_executable_counts_as_none(self):
        self.hook(".git/hooks\n", mode=0o644)
        self.assertFalse(self.git.has_pre_push_hook(self.repo))

    def test_no_hook_file(self):
        self.script({HOOKS: ".git/hooks\n"})
        self.assertFalse(self.git.has_pre_push_hook(self.repo))

    def test_a_sample_does_not_count(self):
        self.hook(".git/hooks\n")
        (self.path / ".git/hooks/pre-push").rename(self.path / ".git/hooks/pre-push.sample")
        self.assertFalse(self.git.has_pre_push_hook(self.repo))

    def test_a_directory_named_pre_push_does_not_count(self):
        self.script({HOOKS: ".git/hooks\n"})
        (self.path / ".git/hooks/pre-push").mkdir(parents=True)
        self.assertFalse(self.git.has_pre_push_hook(self.repo))

    def test_failure_is_a_local_error(self):
        self.script({HOOKS: 128})
        with self.assertRaises(LocalError) as caught:
            self.git.has_pre_push_hook(self.repo)
        self.assertEqual((caught.exception.repo, caught.exception.what), ("widgets", "pre-push hook"))


class RecordedTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.repo = Repo("widgets", "acme/widgets", True, None, True)
        self.source = RecordedLocal(self.path)

    def write(self, content):
        text = content if isinstance(content, str) else json.dumps(content)
        (self.path / "local-widgets.json").write_text(text, encoding="utf-8")

    def test_good_file(self):
        self.write({
            "branches": [
                {"name": "fix/retry-budget", "only_here": 3, "tip": "2026-11-10T08:00:00Z"},
                {"name": "main", "only_here": 0, "tip": "2026-11-09T08:00:00+02:00"},
            ],
            "pre_push_hook": True,
        })
        self.assertEqual(
            self.source.unpushed_branches(self.repo),
            (LocalBranch("fix/retry-budget", 3, datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)),),
        )
        self.assertTrue(self.source.has_pre_push_hook(self.repo))

    def test_hook_off(self):
        self.write({"branches": [], "pre_push_hook": False})
        self.assertFalse(self.source.has_pre_push_hook(self.repo))
        self.assertEqual(self.source.unpushed_branches(self.repo), ())

    def test_every_bad_file_is_a_local_error(self):
        good = {"name": "x", "only_here": 1, "tip": "2026-11-10T08:00:00Z"}
        branches, hook = self.source.unpushed_branches, self.source.has_pre_push_hook
        bad = {
            "not json": ("{", branches),
            "not json, hook": ("{", hook),
            "not an object": ("[]", branches),
            "no branches": ({"pre_push_hook": True}, branches),
            "branches not a list": ({"branches": {}}, branches),
            "branch missing name": ({"branches": [{**good, "name": None}]}, branches),
            "count is text": ({"branches": [{**good, "only_here": "1"}]}, branches),
            "count is a bool": ({"branches": [{**good, "only_here": True}]}, branches),
            "count is negative": ({"branches": [{**good, "only_here": -1}]}, branches),
            "tip has no zone": ({"branches": [{**good, "tip": "2026-11-10T08:00:00"}]}, branches),
            "hook is text": ({"pre_push_hook": "yes"}, hook),
            "no hook field": ({"branches": []}, hook),
        }
        for name, (content, read) in bad.items():
            with self.subTest(name):
                self.write(content)
                with self.assertRaises(LocalError) as caught:
                    read(self.repo)
                self.assertEqual(caught.exception.repo, "widgets")

    def test_a_missing_file_is_a_local_error(self):
        for read in (self.source.unpushed_branches, self.source.has_pre_push_hook):
            with self.assertRaises(LocalError) as caught:
                read(self.repo)
            self.assertIn("local-widgets.json", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

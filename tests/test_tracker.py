import json
import sys
import tempfile
import unittest
from pathlib import Path

from oversight.config import DEFAULT_MARKERS
from oversight.tracker import (
    BdCli,
    NoBlocker,
    PullRequestBlocker,
    RecordedTracker,
    Status,
    TextBlocker,
    TrackerError,
    WaitKind,
    parse_blocker,
)

from support import make_task


def parse(notes):
    return parse_blocker(notes, DEFAULT_MARKERS)


class BlockerParsingTest(unittest.TestCase):
    def test_pull_request_references(self):
        cases = {
            "blocker: waiting for you to merge PR #12": 12,
            "blocker: 等你合并 PR #12": 12,
            "blocker: 等你合并 #12": 12,
            "blocker: PR 12 must land first": 12,
            "blocker: pull request #12 is open": 12,
            "blocker: pr12": 12,
            "blocker: please merge #12": 12,
            "blocker: PR #3 and PR #4": 3,
        }
        for notes, number in cases.items():
            with self.subTest(notes=notes):
                blocker = parse(notes)
                self.assertIsInstance(blocker, PullRequestBlocker)
                self.assertEqual(blocker.number, number)

    def test_pull_request_in_another_repository(self):
        cases = {
            "blocker: waiting for PR acme/gadgets#30": (30, "acme/gadgets"),
            "blocker: merge acme/gadgets#30 first": (30, "acme/gadgets"),
            "blocker: acme/gadgets#30": (30, "acme/gadgets"),
            "blocker: 等你合并 PR acme/gadgets#30": (30, "acme/gadgets"),
            "blocker: waiting for you to merge https://github.com/acme/gadgets/pull/30": (30, "acme/gadgets"),
            "卡点：等你合并 https://github.com/acme/widgets/pull/30": (30, "acme/widgets"),
            "blocker: see https://github.com/acme/gadgets/pull/30/files please": (30, "acme/gadgets"),
            "blocker: acme/gadgets#30 and PR #4": (30, "acme/gadgets"),
            "blocker: PR #4 and acme/gadgets#30": (4, None),
            "blocker: PR #12": (12, None),
            "blocker: merge #12": (12, None),
        }
        for notes, (number, repo) in cases.items():
            with self.subTest(notes=notes):
                blocker = parse(notes)
                self.assertIsInstance(blocker, PullRequestBlocker)
                self.assertEqual((blocker.number, blocker.repo), (number, repo))

    def test_a_link_is_read_once(self):
        # "pull" in the link must not make the "#5" after it a pull request
        blocker = parse("blocker: https://github.com/acme/gadgets/pull/30#issuecomment-5 then #5")
        self.assertEqual((blocker.number, blocker.repo), (30, "acme/gadgets"))
        self.assertIsInstance(parse("blocker: see https://github.com/acme/gadgets/issues/30"), TextBlocker)

    def test_text_only(self):
        for notes in ("blocker: see issue #12", "blocker: improve 5 things", "blocker: decide on the API"):
            with self.subTest(notes=notes):
                self.assertIsInstance(parse(notes), TextBlocker)

    def test_last_marker_wins_and_text_stops_at_separator(self):
        blocker = parse("卡点：旧的\n…卡点：等你合并 PR #7｜下一步：x")
        self.assertEqual(blocker, PullRequestBlocker("等你合并 PR #7", 7))

    def test_text_ends_at_ascii_bar_or_newline(self):
        self.assertEqual(parse("blocker: one | two"), TextBlocker("one"))
        self.assertEqual(parse("blocker: one\nmore notes"), TextBlocker("one"))

    def test_no_blocker(self):
        for notes in ("", "nothing here", "blocker:", "blocker:   \nnext", "卡点：｜x"):
            with self.subTest(notes=notes):
                self.assertEqual(parse(notes), NoBlocker())

    def test_custom_markers(self):
        self.assertEqual(parse_blocker("waiting on: Dana", ("waiting on:",)), TextBlocker("Dana"))


class TaskPropertiesTest(unittest.TestCase):
    def test_repo_names(self):
        task = make_task(labels=["repo:widgets", "repo:gadgets", "wait:review", "repo:"])
        self.assertEqual(task.repo_names, ("gadgets", "widgets"))

    def test_wait_kind(self):
        self.assertIs(make_task(labels=["wait:decision"]).wait_kind, WaitKind.DECISION)
        self.assertIs(make_task(labels=["wait:review"]).wait_kind, WaitKind.REVIEW)
        self.assertIs(make_task(labels=["wait:notice"]).wait_kind, WaitKind.NOTICE)
        self.assertIs(make_task(labels=[]).wait_kind, WaitKind.UNTYPED)
        self.assertIs(make_task(labels=["wait:review", "wait:notice"]).wait_kind, WaitKind.UNTYPED)


def record(directory, status, items):
    (Path(directory) / f"tracker-{status}.json").write_text(json.dumps(items), encoding="utf-8")


GOOD_ITEM = {
    "id": "task-a1b2",
    "title": "Sample",
    "status": "blocked",
    "updated_at": "2026-11-10T08:30:00.123456789Z",
    "labels": ["repo:widgets"],
    "notes": "blocker: PR #5",
    "assignee": "agent-1",
    "unrelated": {"x": 1},
}


class RecordedTrackerTest(unittest.TestCase):
    def read(self, items, status="blocked"):
        with tempfile.TemporaryDirectory() as folder:
            record(folder, status, items)
            return RecordedTracker(Path(folder), DEFAULT_MARKERS).tasks(Status(status))

    def test_reads_a_task(self):
        (task,) = self.read([GOOD_ITEM])
        self.assertEqual((task.id, task.title, task.assignee), ("task-a1b2", "Sample", "agent-1"))
        self.assertEqual(task.updated_at.isoformat(), "2026-11-10T08:30:00.123456+00:00")
        self.assertEqual(task.blocker, PullRequestBlocker("PR #5", 5))

    def test_optional_fields_may_be_absent_or_null(self):
        item = {k: v for k, v in GOOD_ITEM.items() if k not in ("labels", "assignee", "notes")}
        (task,) = self.read([item])
        self.assertEqual((task.labels, task.assignee, task.blocker), (frozenset(), None, NoBlocker()))
        (task,) = self.read([{**item, "labels": None, "assignee": None, "notes": None}])
        self.assertIsNone(task.assignee)

    def assertTrackerError(self, items, status="blocked"):
        with self.assertRaises(TrackerError):
            self.read(items, status)

    def test_missing_required_field(self):
        for key in ("id", "title", "status", "updated_at"):
            with self.subTest(key=key):
                self.assertTrackerError([{k: v for k, v in GOOD_ITEM.items() if k != key}])

    def test_bad_shapes(self):
        self.assertTrackerError({"not": "a list"})
        self.assertTrackerError(["text"])
        self.assertTrackerError([{**GOOD_ITEM, "updated_at": "yesterday"}])
        self.assertTrackerError([{**GOOD_ITEM, "updated_at": "2026-11-10T08:30:00"}])
        self.assertTrackerError([{**GOOD_ITEM, "labels": "repo:x"}])
        self.assertTrackerError([{**GOOD_ITEM, "labels": [1]}])
        self.assertTrackerError([{**GOOD_ITEM, "status": "closed"}])
        self.assertTrackerError([GOOD_ITEM], status="in_progress")

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(TrackerError):
                RecordedTracker(Path(folder), DEFAULT_MARKERS).tasks(Status.BLOCKED)

    def test_file_that_is_not_utf_8(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "tracker-blocked.json").write_bytes(b'[{"title": "caf\xe9"}]')
            with self.assertRaises(TrackerError):
                RecordedTracker(Path(folder), DEFAULT_MARKERS).tasks(Status.BLOCKED)


# A stand-in for `bd`: python -c puts the arguments after the code into sys.argv.
FAKE_BD = (
    "import json, sys\n"
    "args = sys.argv[1:]\n"
    "print('warning: something odd', file=sys.stderr)\n"
    "if args[2] == 'fail':\n"
    "    sys.exit(3)\n"
    "if args[:2] == ['--readonly', '-C'] and args[3:] == ['list', '-s', 'blocked', '--limit', '0', '--json']:\n"
    "    print(json.dumps([{'id': 'task-z9', 'title': 'T', 'status': 'blocked',\n"
    "                       'updated_at': '2026-11-10T08:30:00Z'}]))\n"
    "else:\n"
    "    print('not json')\n"
)


# Prints its own arguments as the title, so a test can read what was asked for.
ECHO_BD = (
    "import json, sys\n"
    "print(json.dumps([{'id': 'task-z9', 'title': ' '.join(sys.argv[1:]), 'status': 'blocked',\n"
    "                   'updated_at': '2026-11-10T08:30:00Z'}]))\n"
)


class BdCliTest(unittest.TestCase):
    def test_asks_for_every_task_and_changes_nothing(self):
        tracker = BdCli((sys.executable, "-c", ECHO_BD), Path("~/tasks"), DEFAULT_MARKERS)
        (task,) = tracker.tasks(Status.BLOCKED)
        self.assertEqual(task.title, "--readonly -C ~/tasks list -s blocked --limit 0 --json")

    def test_output_that_is_not_utf_8_is_a_tracker_error(self):
        code = "import sys; sys.stdout.buffer.write(b'[\"caf\\xe9\"]')"
        tracker = BdCli((sys.executable, "-c", code), Path("x"), DEFAULT_MARKERS)
        with self.assertRaises(TrackerError):
            tracker.tasks(Status.BLOCKED)

    def test_runs_the_documented_command_and_ignores_stderr(self):
        tracker = BdCli((sys.executable, "-c", FAKE_BD), Path("~/tasks"), DEFAULT_MARKERS)
        (task,) = tracker.tasks(Status.BLOCKED)
        self.assertEqual(task.id, "task-z9")

    def test_wrong_status_arguments_give_invalid_json(self):
        tracker = BdCli((sys.executable, "-c", FAKE_BD), Path("~/tasks"), DEFAULT_MARKERS)
        with self.assertRaises(TrackerError):
            tracker.tasks(Status.IN_PROGRESS)

    def test_nonzero_exit_and_missing_program(self):
        failing = BdCli((sys.executable, "-c", FAKE_BD), Path("fail"), DEFAULT_MARKERS)
        with self.assertRaises(TrackerError):
            failing.tasks(Status.BLOCKED)
        missing = BdCli(("no-such-program-oversight",), Path("x"), DEFAULT_MARKERS)
        with self.assertRaises(TrackerError):
            missing.tasks(Status.BLOCKED)

    def test_timeout(self):
        slow = BdCli((sys.executable, "-c", "import time; time.sleep(5)"), Path("x"), DEFAULT_MARKERS, 0.2)
        with self.assertRaises(TrackerError):
            slow.tasks(Status.BLOCKED)


if __name__ == "__main__":
    unittest.main()

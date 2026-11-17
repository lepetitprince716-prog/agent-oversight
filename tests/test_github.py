import json
import sys
import tempfile
import unittest
from pathlib import Path

from oversight.github import (
    Cached,
    Checks,
    GhCli,
    GitHubError,
    Mergeability,
    PrState,
    Recorded,
    checks_from_rollup,
    Workflow,
    parse_pull_request,
)
from oversight.sources import SourceError

from support import FakeGitHub, make_pr, make_run


class MergeabilityTest(unittest.TestCase):
    def mergeability(self, status):
        raw = {"number": 1, "title": "t", "state": "OPEN", "mergeStateStatus": status,
               "updatedAt": "2026-11-10T00:00:00Z"}
        return parse_pull_request("acme/widgets", raw).mergeability

    def test_mapping(self):
        self.assertIs(self.mergeability("CLEAN"), Mergeability.CLEAN)
        self.assertIs(self.mergeability("DIRTY"), Mergeability.CONFLICTING)
        for other in ("BLOCKED", "BEHIND", "UNSTABLE", "HAS_HOOKS", "DRAFT"):
            with self.subTest(other=other):
                self.assertIs(self.mergeability(other), Mergeability.OTHER)

    def test_no_answer_yet_is_unknown_not_clean(self):
        for unanswered in ("UNKNOWN", ""):
            with self.subTest(unanswered=unanswered):
                self.assertIs(self.mergeability(unanswered), Mergeability.UNKNOWN)

    def test_draft_flag(self):
        raw = {"number": 1, "title": "t", "state": "OPEN", "updatedAt": "2026-11-10T00:00:00Z"}
        self.assertFalse(parse_pull_request("acme/widgets", raw).draft)
        self.assertTrue(parse_pull_request("acme/widgets", {**raw, "isDraft": True}).draft)
        self.assertFalse(parse_pull_request("acme/widgets", {**raw, "isDraft": False}).draft)
        self.assertFalse(parse_pull_request("acme/widgets", {**raw, "isDraft": None}).draft)
        with self.assertRaises(SourceError):
            parse_pull_request("acme/widgets", {**raw, "isDraft": "yes"})

    def test_missing_status_is_unknown(self):
        raw = {"number": 1, "title": "t", "state": "open", "updatedAt": "2026-11-10T00:00:00Z"}
        pr = parse_pull_request("acme/widgets", raw)
        self.assertIs(pr.mergeability, Mergeability.UNKNOWN)
        self.assertIs(pr.state, PrState.OPEN)
        self.assertIs(pr.checks, Checks.NONE)


class ChecksTest(unittest.TestCase):
    def rollup(self, *items):
        return checks_from_rollup(list(items))

    def test_mapping(self):
        ok, bad = {"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}
        self.assertIs(self.rollup(), Checks.NONE)
        self.assertIs(self.rollup(ok), Checks.PASSING)
        self.assertIs(self.rollup(ok, bad), Checks.FAILING)
        self.assertIs(self.rollup({"conclusion": "SKIPPED"}, {"conclusion": "NEUTRAL"}), Checks.NONE)
        self.assertIs(self.rollup({"conclusion": "SKIPPED"}, ok), Checks.PASSING)

    def test_every_failing_value(self):
        for value in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            with self.subTest(value=value):
                self.assertIs(self.rollup({"conclusion": value}), Checks.FAILING)

    def test_pending_values_and_precedence(self):
        ok = {"conclusion": "SUCCESS"}
        for item in ({"conclusion": ""}, {"conclusion": None}, {}, {"state": "PENDING"},
                     {"state": "QUEUED"}, {"state": "IN_PROGRESS"}, {"state": "EXPECTED"}):
            with self.subTest(item=item):
                self.assertIs(self.rollup(ok, item), Checks.PENDING)
        # a failure beats a pending item
        self.assertIs(self.rollup({"conclusion": ""}, {"state": "FAILURE"}), Checks.FAILING)

    def test_status_contexts_use_state(self):
        self.assertIs(self.rollup({"conclusion": "", "state": "SUCCESS"}), Checks.PASSING)
        self.assertIs(self.rollup({"conclusion": None, "state": "ERROR"}), Checks.FAILING)

    def test_bad_item(self):
        with self.assertRaises(SourceError):
            self.rollup("SUCCESS")


class RecordedTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)

    def write(self, name, data):
        (self.path / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def test_files_are_named_per_repository(self):
        pr = {"number": 12, "title": "t", "state": "MERGED", "mergeStateStatus": "DIRTY",
              "statusCheckRollup": [{"conclusion": "FAILURE"}], "updatedAt": "2026-11-10T00:00:00Z"}
        self.write("pr-view-acme__widgets-12.json", pr)
        self.write("pr-list-acme__widgets.json", [pr])
        self.write("workflow-list-acme__widgets.json", [
            {"id": 7, "name": "nightly", "state": "active"},
            {"id": 8, "name": "retired", "state": "disabled_manually"},
            {"id": 9, "name": "lint", "state": "active"},
        ])
        self.write("run-list-acme__widgets-7.json", [
            {"conclusion": "success", "status": "completed", "createdAt": "2026-11-10T02:00:00Z"},
            {"conclusion": "failure", "status": "in_progress", "createdAt": "2026-11-11T02:00:00Z"},
            {"conclusion": None, "status": "completed", "createdAt": "2026-11-12T02:00:00Z"},
        ])
        source = Recorded(self.path)
        got = source.pull_request("acme/widgets", 12)
        self.assertEqual((got.state, got.mergeability, got.checks), (PrState.MERGED, Mergeability.CONFLICTING, Checks.FAILING))
        self.assertEqual(source.open_pull_requests("acme/widgets"), (got,))
        # a workflow that is not active is skipped
        nightly, lint = source.active_workflows("acme/widgets")
        self.assertEqual((nightly, lint), (Workflow(7, "nightly"), Workflow(9, "lint")))
        runs = source.scheduled_runs("acme/widgets", nightly, 20)
        # the unfinished run is dropped; newest first; a null conclusion did not succeed
        self.assertEqual([run.succeeded for run in runs], [False, True])
        self.assertEqual(len(source.scheduled_runs("acme/widgets", nightly, 1)), 1)

    def test_run_conclusions(self):
        def run(conclusion, day):
            return {"conclusion": conclusion, "status": "completed", "createdAt": f"2026-11-{day:02d}T02:00:00Z"}

        self.write("run-list-acme__widgets-7.json", [
            run("success", 1), run("skipped", 2), run("neutral", 3), run("cancelled", 4),
            run("timed_out", 5), run("startup_failure", 6), run("action_required", 7), run("failure", 8),
            run("SKIPPED", 9),
        ])
        runs = Recorded(self.path).scheduled_runs("acme/widgets", Workflow(7, "nightly"), 20)
        # skipped and neutral did no work and are dropped; every other conclusion but success failed
        self.assertEqual([run.succeeded for run in runs], [False] * 5 + [True])
        self.assertEqual([run.created_at.day for run in runs], [8, 7, 6, 5, 4, 1])

    def test_a_workflow_listing_at_its_limit_may_be_cut_short(self):
        workflow = {"id": 1, "name": "ci", "state": "active"}
        source = Recorded(self.path)
        self.write("workflow-list-acme__widgets.json", [workflow] * 999)
        self.assertEqual(len(source.active_workflows("acme/widgets")), 999)
        self.write("workflow-list-acme__widgets.json", [workflow] * 1000)
        with self.assertRaises(GitHubError) as caught:
            source.active_workflows("acme/widgets")
        self.assertIn("limit of 1000", str(caught.exception))

    def test_a_listing_that_reaches_its_limit_may_be_cut_short(self):
        pr = {"number": 1, "title": "t", "state": "OPEN", "updatedAt": "2026-11-10T00:00:00Z"}
        source = Recorded(self.path)
        self.write("pr-list-acme__widgets.json", [pr] * 999)
        self.assertEqual(len(source.open_pull_requests("acme/widgets")), 999)
        self.write("pr-list-acme__widgets.json", [pr] * 1000)
        with self.assertRaises(GitHubError) as caught:
            source.open_pull_requests("acme/widgets")
        self.assertIn("limit of 1000", str(caught.exception))

    def test_a_blank_workflow_listing_is_no_workflows(self):
        source = Recorded(self.path)
        for blank in ("", "\n", "  \n"):
            with self.subTest(blank=blank):
                self.write("workflow-list-acme__widgets.json", blank)
                self.assertEqual(source.active_workflows("acme/widgets"), ())

    def test_other_blank_listings_are_errors(self):
        source = Recorded(self.path)
        self.write("pr-list-acme__widgets.json", "\n")
        with self.assertRaises(GitHubError):
            source.open_pull_requests("acme/widgets")
        self.write("pr-view-acme__widgets-1.json", "")
        with self.assertRaises(GitHubError):
            source.pull_request("acme/widgets", 1)
        self.write("run-list-acme__widgets-7.json", " ")
        with self.assertRaises(GitHubError):
            source.scheduled_runs("acme/widgets", Workflow(7, "nightly"), 20)

    def test_every_failure_is_a_github_error(self):
        source = Recorded(self.path)
        with self.assertRaises(GitHubError) as caught:
            source.pull_request("acme/widgets", 1)
        self.assertEqual((caught.exception.repo, caught.exception.what), ("acme/widgets", "pull request #1"))
        self.write("pr-list-acme__widgets.json", "not json")
        with self.assertRaises(GitHubError):
            source.open_pull_requests("acme/widgets")
        self.write("pr-list-acme__widgets.json", {"not": "a list"})
        with self.assertRaises(GitHubError):
            source.open_pull_requests("acme/widgets")
        self.write("run-list-acme__widgets-7.json", [{"conclusion": "success"}])
        with self.assertRaises(GitHubError):
            source.scheduled_runs("acme/widgets", Workflow(7, "nightly"), 20)
        with self.assertRaises(GitHubError):
            source.scheduled_runs("acme/widgets", Workflow(8, "no file"), 20)
        with self.assertRaises(GitHubError):
            source.active_workflows("acme/widgets")
        self.write("workflow-list-acme__widgets.json", [{"id": "7", "name": "x", "state": "active"}])
        with self.assertRaises(GitHubError):
            source.active_workflows("acme/widgets")
        self.write("pr-view-acme__widgets-2.json", {"number": 2, "title": "t", "state": "WEIRD", "updatedAt": "2026-11-10T00:00:00Z"})
        with self.assertRaises(GitHubError):
            source.pull_request("acme/widgets", 2)


# A stand-in for `gh`: echoes its own arguments back so the test can read them.
FAKE_GH = (
    "import json, sys\n"
    "args = sys.argv[1:]\n"
    "if args[0] == 'boom':\n"
    "    sys.exit(4)\n"
    "if 'acme/empty' in args and args[:2] in (['pr', 'list'], ['workflow', 'list']):\n"
    "    sys.exit(0)\n"
    "if args[:2] == ['pr', 'view']:\n"
    "    print(json.dumps({'number': 7, 'title': ' '.join(args), 'state': 'OPEN',\n"
    "                      'updatedAt': '2026-11-10T00:00:00Z'}))\n"
    "elif args[:2] == ['pr', 'list']:\n"
    "    print(json.dumps([{'number': 8, 'title': ' '.join(args), 'state': 'OPEN',\n"
    "                       'updatedAt': '2026-11-10T00:00:00Z'}]))\n"
    "elif args[:2] == ['workflow', 'list']:\n"
    "    print(json.dumps([{'id': 123, 'name': ' '.join(args), 'state': 'active'},\n"
    "                      {'id': 124, 'name': 'off', 'state': 'disabled_manually'}]))\n"
    "elif args[:2] == ['run', 'list']:\n"
    "    asked = ' '.join(args)\n"
    "    expected = ('run list --repo acme/widgets --workflow 123 --event schedule --limit 20 '\n"
    "                '--json conclusion,status,createdAt')\n"
    "    print(json.dumps([{'conclusion': 'success' if asked == expected else 'failure',\n"
    "                       'status': 'completed', 'createdAt': '2026-11-10T00:00:00Z'}]))\n"
    "else:\n"
    "    print('[]')\n"
)


class GhCliTest(unittest.TestCase):
    def setUp(self):
        self.gh = GhCli((sys.executable, "-c", FAKE_GH), 20)

    def test_commands_follow_the_contract(self):
        title = self.gh.pull_request("acme/widgets", 7).title
        self.assertEqual(
            title,
            "pr view 7 --repo acme/widgets --json number,title,state,isDraft,mergeStateStatus,statusCheckRollup,updatedAt",
        )
        (workflow,) = self.gh.active_workflows("acme/widgets")
        self.assertEqual(workflow.id, 123)
        self.assertEqual(workflow.name, "workflow list --repo acme/widgets --limit 1000 --json id,name,state")
        (run,) = self.gh.scheduled_runs("acme/widgets", workflow, 20)
        self.assertTrue(run.succeeded, "run list was not asked for the documented arguments")
        (pr,) = self.gh.open_pull_requests("acme/widgets")
        self.assertEqual(
            pr.title,
            "pr list --repo acme/widgets --state open --limit 1000 --json "
            "number,title,state,isDraft,mergeStateStatus,statusCheckRollup,updatedAt",
        )

    def test_no_output_for_workflows_means_none_but_for_pull_requests_an_error(self):
        self.assertEqual(self.gh.active_workflows("acme/empty"), ())
        with self.assertRaises(GitHubError):
            self.gh.open_pull_requests("acme/empty")

    def test_failure_is_a_github_error(self):
        with self.assertRaises(GitHubError):
            GhCli((sys.executable, "-c", FAKE_GH, "boom"), 20).pull_request("acme/widgets", 1)
        with self.assertRaises(GitHubError):
            GhCli(("no-such-program-oversight",), 20).pull_request("acme/widgets", 1)
        with self.assertRaises(GitHubError):
            GhCli((sys.executable, "-c", "import time; time.sleep(5)"), 0.2).open_pull_requests("acme/widgets")


class CountingGitHub(FakeGitHub):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    def pull_request(self, repo, number):
        self.calls += 1
        return super().pull_request(repo, number)


class CachedTest(unittest.TestCase):
    def test_each_call_is_made_once(self):
        inner = CountingGitHub(prs=[make_pr(12)])
        cached = Cached(inner)
        first = cached.pull_request("acme/widgets", 12)
        self.assertIs(cached.pull_request("acme/widgets", 12), first)
        self.assertEqual(inner.calls, 1)

    def test_workflows_and_their_runs_are_each_fetched_once(self):
        calls = []

        class Listing(FakeGitHub):
            def active_workflows(self, repo):
                calls.append(("workflows", repo))
                return super().active_workflows(repo)

            def scheduled_runs(self, repo, workflow, limit):
                calls.append(("runs", workflow.name))
                return super().scheduled_runs(repo, workflow, limit)

        cached = Cached(Listing(runs=[make_run("nightly", True, 0), make_run("lint", True, 0)]))
        for _ in range(2):
            nightly, lint = cached.active_workflows("acme/widgets")
            cached.scheduled_runs("acme/widgets", nightly, 20)
            cached.scheduled_runs("acme/widgets", lint, 20)
        self.assertEqual(calls, [("workflows", "acme/widgets"), ("runs", "nightly"), ("runs", "lint")])

    def test_a_failure_is_remembered_not_retried(self):
        inner = CountingGitHub(prs=[])
        cached = Cached(inner)
        for _ in range(3):
            with self.assertRaises(GitHubError):
                cached.pull_request("acme/widgets", 12)
        self.assertEqual(inner.calls, 1)


if __name__ == "__main__":
    unittest.main()

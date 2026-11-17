import unittest

from oversight.checks.schedule_red import RUNS_LOOKED_AT
from oversight.checks import (
    ALL_CHECKS, blocker_gone, blocker_stuck, guard_off, local_only, pr_idle, quiet_holder, schedule_red,
    silent_wait, run_checks,
)
from oversight.drift import Drift, DriftKind, Unchecked
from oversight.github import Checks, Mergeability, PrState, ScheduledRun, Workflow
from oversight.tracker import Status

from support import FakeGitHub, FakeLocal, days_ago, make_branch, make_config, make_pr, make_run, make_task, make_world


def findings(module, world):
    return list(module.check(world))


def blocked_on(number, **kwargs):
    return make_task(notes=f"blocker: waiting for PR #{number}", **kwargs)


class BlockerGoneTest(unittest.TestCase):
    def test_merged(self):
        world = make_world([blocked_on(12)], github=FakeGitHub(prs=[make_pr(12, state=PrState.MERGED, updated=days_ago(3))]))
        (drift,) = findings(blocker_gone, world)
        self.assertEqual(drift.kind, DriftKind.BLOCKER_GONE)
        self.assertEqual((drift.subject, drift.title), ("task-a1b2", "Sample task"))
        self.assertEqual(drift.evidence, "PR #12 merged, last updated 2026-11-14")
        self.assertEqual(drift.action, "The wait is over: close the task if nothing is left, otherwise reopen it for the queue.")

    def test_closed_without_merging(self):
        world = make_world([blocked_on(12)], github=FakeGitHub(prs=[make_pr(12, state=PrState.CLOSED, updated=days_ago(3))]))
        (drift,) = findings(blocker_gone, world)
        self.assertEqual(drift.evidence, "PR #12 closed without merging, last updated 2026-11-14")
        self.assertEqual(drift.action, "Decide whether the task still stands, then reopen or close it.")

    def test_open_is_no_drift(self):
        world = make_world([blocked_on(12)], github=FakeGitHub(prs=[make_pr(12)]))
        self.assertEqual(findings(blocker_gone, world), [])

    def test_other_blockers_are_ignored(self):
        world = make_world([make_task(notes="blocker: a decision"), make_task("task-b2", notes="")])
        self.assertEqual(findings(blocker_gone, world), [])

    def test_unresolved_repository_is_unchecked_with_its_own_reason(self):
        reasons = {
            (): "task has no repo: label",
            ("repo:nowhere",): "repo:nowhere is not in the config",
            ("repo:widgets", "repo:gadgets"): "task has several repo: labels (gadgets, widgets)",
        }
        for labels, reason in reasons.items():
            with self.subTest(labels=labels):
                world = make_world([blocked_on(12, labels=labels)], github=FakeGitHub(prs=[make_pr(12)]))
                (item,) = findings(blocker_gone, world)
                self.assertEqual(item, Unchecked(DriftKind.BLOCKER_GONE, "task-a1b2", reason))

    def test_github_failure_is_unchecked_never_a_pass(self):
        world = make_world(
            [blocked_on(12), blocked_on(13, task_id="task-b2", labels=["repo:gadgets"])],
            github=FakeGitHub(prs=[make_pr(13, repo="acme/gadgets", state=PrState.MERGED)], broken=["acme/widgets"]),
        )
        result = findings(blocker_gone, world)
        # the failing subject is Unchecked, and the check goes on with the next one
        self.assertIsInstance(result[0], Unchecked)
        self.assertEqual(result[0].subject, "task-a1b2")
        self.assertIn("simulated failure", result[0].reason)
        self.assertIsInstance(result[1], Drift)
        self.assertEqual(result[1].subject, "task-b2")


class BlockerInAnotherRepositoryTest(unittest.TestCase):
    def two_repositories(self, **github):
        return FakeGitHub(
            prs=[make_pr(30, repo="acme/widgets"), make_pr(30, repo="acme/gadgets", state=PrState.MERGED)],
            **github,
        )

    def test_the_named_repository_is_looked_up_not_the_tasks_own(self):
        for notes in ("blocker: waiting for PR acme/gadgets#30", "blocker: merge acme/gadgets#30 first"):
            with self.subTest(notes=notes):
                world = make_world([make_task(notes=notes)], github=self.two_repositories())
                (drift,) = findings(blocker_gone, world)
                self.assertEqual(drift.kind, DriftKind.BLOCKER_GONE)
                self.assertEqual(drift.evidence[:12], "PR #30 merge")
                self.assertTrue(run_checks(world).drifts)

    def test_a_link_names_the_repository_too(self):
        notes = "卡点：等你合并 https://github.com/acme/gadgets/pull/30"
        world = make_world([make_task(notes=notes)], github=self.two_repositories())
        (drift,) = findings(blocker_gone, world)
        self.assertEqual(drift.subject, "task-a1b2")

    def test_a_repository_outside_the_config_is_still_looked_up(self):
        github = FakeGitHub(prs=[make_pr(7, repo="other/elsewhere", state=PrState.CLOSED)])
        task = make_task(labels=[], notes="blocker: waiting for other/elsewhere#7")
        (drift,) = findings(blocker_gone, make_world([task], github=github))
        self.assertEqual(drift.evidence[:23], "PR #7 closed without me")

    def test_a_lookup_failure_in_the_named_repository_is_unchecked(self):
        github = FakeGitHub(broken=["other/elsewhere"])
        task = make_task(notes="blocker: waiting for other/elsewhere#7")
        (item,) = findings(blocker_gone, make_world([task], github=github))
        self.assertIsInstance(item, Unchecked)
        self.assertIn("other/elsewhere", item.reason)

    def test_blocker_stuck_looks_at_the_named_repository_too(self):
        github = FakeGitHub(prs=[
            make_pr(30, repo="acme/widgets"),
            make_pr(30, repo="acme/gadgets", mergeability=Mergeability.CONFLICTING),
        ])
        task = make_task(notes="blocker: PR acme/gadgets#30")
        (drift,) = findings(blocker_stuck, make_world([task], github=github))
        self.assertIn("conflicting", drift.evidence)


class BlockerStuckTest(unittest.TestCase):
    def run_with(self, **pr):
        world = make_world([blocked_on(12)], github=FakeGitHub(prs=[make_pr(12, **pr)]))
        return findings(blocker_stuck, world)

    def test_conflicting_and_failing(self):
        (drift,) = self.run_with(mergeability=Mergeability.CONFLICTING, checks=Checks.FAILING, updated=days_ago(2))
        self.assertEqual(drift.kind, DriftKind.BLOCKER_STUCK)
        self.assertEqual(drift.evidence, "PR #12 open, conflicting, checks failing, last updated 2026-11-15")
        self.assertEqual(drift.action, "The pull request cannot merge as it is: rebase or fix it. This does not need the human.")

    def test_names_only_what_applies(self):
        (drift,) = self.run_with(mergeability=Mergeability.CONFLICTING)
        self.assertIn("open, conflicting, last updated", drift.evidence)
        (drift,) = self.run_with(checks=Checks.FAILING)
        self.assertIn("open, checks failing, last updated", drift.evidence)

    def test_healthy_pending_and_finished_pull_requests_are_no_drift(self):
        self.assertEqual(self.run_with(), [])
        self.assertEqual(self.run_with(checks=Checks.PENDING, mergeability=Mergeability.OTHER), [])
        self.assertEqual(self.run_with(state=PrState.MERGED, mergeability=Mergeability.CONFLICTING), [])

    def test_lookup_problems_are_left_to_blocker_gone(self):
        world = make_world([blocked_on(12), blocked_on(13, labels=[])], github=FakeGitHub(broken=["acme/widgets"]))
        self.assertEqual(findings(blocker_stuck, world), [])


class SilentWaitTest(unittest.TestCase):
    def test_no_blocker(self):
        (drift,) = findings(silent_wait, make_world([make_task(notes="just parked", updated=days_ago(5))]))
        self.assertEqual(drift.kind, DriftKind.SILENT_WAIT)
        self.assertEqual(drift.evidence, "blocked, last updated 2026-11-12, notes name no blocker")
        self.assertEqual(drift.action, "Write the one thing being waited on, or reopen the task.")

    def test_written_blockers_are_no_drift(self):
        world = make_world([make_task(notes="blocker: a decision"), make_task("task-b2", notes="blocker: PR #3")])
        self.assertEqual(findings(silent_wait, world), [])


class QuietHolderTest(unittest.TestCase):
    def held(self, days, assignee="agent-1"):
        task = make_task(status=Status.IN_PROGRESS, updated=days_ago(days), assignee=assignee)
        return findings(quiet_holder, make_world(in_progress=[task]))

    def test_quiet_for_longer_than_the_threshold(self):
        (drift,) = self.held(3.5)
        self.assertEqual(drift.kind, DriftKind.QUIET_HOLDER)
        self.assertEqual(drift.evidence, "in progress, held by agent-1, no update for 3 days")
        self.assertEqual(
            drift.action,
            "Check whether the holder is still running. If not, take the task over or return it to the queue.",
        )

    def test_nobody(self):
        (drift,) = self.held(4, assignee=None)
        self.assertIn("held by nobody", drift.evidence)

    def test_within_the_threshold_is_no_drift(self):
        self.assertEqual(self.held(2), [])
        self.assertEqual(self.held(0.5), [])

    def test_threshold_comes_from_the_config(self):
        task = make_task(status=Status.IN_PROGRESS, updated=days_ago(3))
        world = make_world(in_progress=[task], config=make_config(thresholds={"quiet_days": 5}))
        self.assertEqual(findings(quiet_holder, world), [])


class ScheduleRedTest(unittest.TestCase):
    def test_streaks_are_counted_per_workflow(self):
        runs = [
            make_run("nightly", False, 0), make_run("lint", True, 0),
            make_run("nightly", False, 1), make_run("lint", False, 1), make_run("lint", False, 2),
            make_run("nightly", False, 2), make_run("nightly", True, 3), make_run("nightly", False, 4),
            make_run("lint", True, 3), make_run("lint", False, 4),
        ]
        world = make_world(github=FakeGitHub(runs=runs))
        (drift,) = findings(schedule_red, world)
        self.assertEqual(drift.kind, DriftKind.SCHEDULE_RED)
        self.assertEqual((drift.subject, drift.title), ("acme/widgets nightly", "nightly"))
        self.assertEqual(drift.evidence, "failed 3 scheduled runs in a row, since 2026-11-15")
        self.assertEqual(drift.action, "Find the failing step and fix it; file a task if none exists.")

    def test_two_red_workflows(self):
        runs = [make_run("nightly", False, 0), make_run("lint", False, 0), make_run("lint", False, 1)]
        drifts = findings(schedule_red, make_world(github=FakeGitHub(runs=runs)))
        self.assertEqual([d.subject for d in drifts], ["acme/widgets nightly", "acme/widgets lint"])
        self.assertEqual(
            [d.evidence for d in drifts],
            ["failed 1 scheduled runs in a row, since 2026-11-17", "failed 2 scheduled runs in a row, since 2026-11-16"],
        )

    def test_green_or_empty_is_no_drift(self):
        self.assertEqual(findings(schedule_red, make_world(github=FakeGitHub(runs=[make_run("nightly", True, 0), make_run("nightly", False, 1)]))), [])
        self.assertEqual(findings(schedule_red, make_world()), [])

    def test_unscheduled_repositories_are_skipped(self):
        runs = [make_run("nightly", False, 0, repo="acme/gadgets")]
        self.assertEqual(findings(schedule_red, make_world(github=FakeGitHub(runs=runs))), [])

    def test_github_failure_is_unchecked(self):
        (item,) = findings(schedule_red, make_world(github=FakeGitHub(broken=["acme/widgets"])))
        self.assertIsInstance(item, Unchecked)
        self.assertEqual((item.check, item.subject), (DriftKind.SCHEDULE_RED, "acme/widgets"))

    def test_a_busy_workflow_does_not_crowd_out_a_quiet_one(self):
        runs = [make_run("hourly", True, 0) for _ in range(25)]
        runs += [make_run("weekly", False, days) for days in (1, 8, 15)]
        (drift,) = findings(schedule_red, make_world(github=FakeGitHub(runs=runs)))
        self.assertEqual(drift.subject, "acme/widgets weekly")
        self.assertEqual(drift.evidence, "failed 3 scheduled runs in a row, since 2026-11-02")

    def test_each_workflow_is_asked_for_its_own_newest_runs(self):
        asked = []

        class Recording(FakeGitHub):
            def scheduled_runs(self, repo, workflow, limit):
                asked.append((workflow.name, limit))
                return super().scheduled_runs(repo, workflow, limit)

        runs = [make_run("nightly", True, 0), make_run("lint", True, 0)]
        findings(schedule_red, make_world(github=Recording(runs=runs)))
        self.assertEqual(asked, [("nightly", RUNS_LOOKED_AT), ("lint", RUNS_LOOKED_AT)])

    def test_a_streak_that_fills_the_window_may_be_longer(self):
        full = [make_run("nightly", False, days) for days in range(RUNS_LOOKED_AT)]
        (drift,) = findings(schedule_red, make_world(github=FakeGitHub(runs=full)))
        self.assertEqual(
            drift.evidence, "failed at least 20 scheduled runs in a row, since 2026-10-29 or earlier"
        )
        almost = full[:-1]
        (drift,) = findings(schedule_red, make_world(github=FakeGitHub(runs=almost)))
        self.assertEqual(drift.evidence, "failed 19 scheduled runs in a row, since 2026-10-30")

    def test_a_workflow_that_cannot_be_listed_is_the_only_unchecked_one(self):
        runs = [make_run("nightly", False, 0), make_run("lint", False, 0), make_run("docs", True, 0)]
        github = FakeGitHub(runs=runs, broken_workflows=["nightly"])
        nightly, lint = findings(schedule_red, make_world(github=github))
        self.assertEqual(nightly.__class__, Unchecked)
        self.assertEqual((nightly.check, nightly.subject), (DriftKind.SCHEDULE_RED, "acme/widgets nightly"))
        self.assertIn("simulated failure", nightly.reason)
        self.assertIsInstance(lint, Drift)
        self.assertEqual(lint.subject, "acme/widgets lint")

    def test_a_workflow_without_scheduled_runs_produces_nothing(self):
        world = make_world(github=FakeGitHub(quiet=[("acme/widgets", "ci")]))
        self.assertEqual(findings(schedule_red, world), [])


class SameNameWorkflowsTest(unittest.TestCase):
    class Twins(FakeGitHub):
        def active_workflows(self, repo):
            return (Workflow(1, "CI"), Workflow(5, "CI"), Workflow(9, "lint"))

        def scheduled_runs(self, repo, workflow, limit):
            return (ScheduledRun(days_ago(workflow.id), False),)

    def test_the_subject_tells_same_named_workflows_apart(self):
        drifts = findings(schedule_red, make_world(github=self.Twins()))
        self.assertEqual(
            [d.subject for d in drifts],
            ["acme/widgets CI (id 1)", "acme/widgets CI (id 5)", "acme/widgets lint"],
        )
        self.assertEqual([d.title for d in drifts], ["CI", "CI", "lint"])


class PrIdleTest(unittest.TestCase):
    ACTION = "Finish and merge it, close it, or file a task that says what it waits for."

    def idle(self, number=21, **kwargs):
        kwargs.setdefault("mergeability", Mergeability.CONFLICTING)
        kwargs.setdefault("updated", days_ago(5))
        return make_pr(number, title="Refactor the parser", **kwargs)

    def evidence(self, **kwargs):
        (drift,) = findings(pr_idle, make_world(github=FakeGitHub(open_prs=[self.idle(**kwargs)])))
        return drift.evidence

    def test_idle_and_stuck(self):
        (drift,) = findings(pr_idle, make_world(github=FakeGitHub(open_prs=[self.idle(checks=Checks.FAILING)])))
        self.assertEqual(drift.kind, DriftKind.PR_IDLE)
        self.assertEqual((drift.subject, drift.title), ("acme/widgets#21", "Refactor the parser"))
        self.assertEqual(drift.evidence, "PR #21 open, conflicting, checks failing, last updated 2026-11-12")
        self.assertEqual(drift.action, self.ACTION)

    def test_ready_to_merge_and_never_merged(self):
        self.assertEqual(
            self.evidence(mergeability=Mergeability.CLEAN, updated=days_ago(13)),
            "PR #21 open, ready to merge, last updated 2026-11-04",
        )

    def test_a_draft(self):
        self.assertEqual(
            self.evidence(draft=True, mergeability=Mergeability.OTHER),
            "PR #21 open, draft, last updated 2026-11-12",
        )
        self.assertEqual(
            self.evidence(draft=True, mergeability=Mergeability.CLEAN),
            "PR #21 open, draft, last updated 2026-11-12",
        )

    def test_state_words_come_in_a_fixed_order(self):
        self.assertEqual(
            self.evidence(draft=True, checks=Checks.FAILING),
            "PR #21 open, draft, conflicting, checks failing, last updated 2026-11-12",
        )

    def test_no_state_word_when_nothing_is_known(self):
        self.assertEqual(
            self.evidence(mergeability=Mergeability.UNKNOWN), "PR #21 open, last updated 2026-11-12"
        )
        self.assertEqual(
            self.evidence(mergeability=Mergeability.OTHER, checks=Checks.PENDING),
            "PR #21 open, last updated 2026-11-12",
        )

    def test_a_fresh_pull_request_is_no_drift(self):
        open_prs = [self.idle(1, updated=days_ago(1)), make_pr(2, updated=days_ago(3)), make_pr(3, draft=True)]
        self.assertEqual(findings(pr_idle, make_world(github=FakeGitHub(open_prs=open_prs))), [])

    def test_a_pull_request_a_blocked_task_names_is_left_out(self):
        github = FakeGitHub(open_prs=[self.idle(21), self.idle(22)])
        world = make_world([blocked_on(21)], github=github)
        self.assertEqual([d.subject for d in findings(pr_idle, world)], ["acme/widgets#22"])

    def test_a_task_in_another_repository_does_not_hide_it(self):
        github = FakeGitHub(open_prs=[self.idle(21)])
        world = make_world([blocked_on(21, labels=["repo:gadgets"])], github=github)
        self.assertEqual([d.subject for d in findings(pr_idle, world)], ["acme/widgets#21"])

    def test_a_task_naming_the_same_number_elsewhere_does_not_hide_it(self):
        github = FakeGitHub(open_prs=[self.idle(21)])
        task = make_task(notes="blocker: waiting for PR acme/gadgets#21")
        self.assertEqual([d.subject for d in findings(pr_idle, make_world([task], github=github))], ["acme/widgets#21"])

    def test_a_task_naming_the_pull_request_in_its_repository_hides_it(self):
        github = FakeGitHub(open_prs=[self.idle(21)])
        for notes, labels in (
            ("blocker: waiting for acme/widgets#21", ["repo:gadgets"]),
            ("blocker: https://github.com/Acme/Widgets/pull/21", []),
            ("blocker: waiting for PR #21", ["repo:widgets"]),
        ):
            with self.subTest(notes=notes):
                task = make_task(notes=notes, labels=labels)
                self.assertEqual(findings(pr_idle, make_world([task], github=github)), [])

    def test_github_failure_is_unchecked(self):
        world = make_world(github=FakeGitHub(broken=["acme/widgets"], open_prs=[self.idle()]))
        items = findings(pr_idle, world)
        self.assertEqual([type(item) for item in items], [Unchecked])
        self.assertEqual((items[0].check, items[0].subject), (DriftKind.PR_IDLE, "acme/widgets"))


def with_clones(**flags):
    """Two configured repositories; `widgets` has a local clone, and is public when asked."""
    return make_config(repos={
        "widgets": {"github": "acme/widgets", "path": "~/code/widgets", **flags},
        "gadgets": {"github": "acme/gadgets"},
    })


class LocalOnlyTest(unittest.TestCase):
    def test_quiet_branch_with_unpushed_commits(self):
        local = FakeLocal({"widgets": [make_branch("fix/retry-budget", 3, days=7)]})
        (drift,) = findings(local_only, make_world(config=with_clones(), clones=local))
        self.assertEqual(drift.kind, DriftKind.LOCAL_ONLY)
        self.assertEqual((drift.subject, drift.title), ("widgets fix/retry-budget", "fix/retry-budget"))
        self.assertEqual(drift.evidence, "3 commits exist only on this machine, newest 2026-11-10")
        self.assertEqual(drift.action, "Push the branch, or delete it if the work is abandoned.")

    def test_one_commit_is_singular(self):
        local = FakeLocal({"widgets": [make_branch(only_here=1)]})
        (drift,) = findings(local_only, make_world(config=with_clones(), clones=local))
        self.assertEqual(drift.evidence, "1 commit exists only on this machine, newest 2026-11-10")

    def test_a_branch_touched_within_the_threshold_is_no_drift(self):
        # quiet_days defaults to 2; exactly two days is not longer than the limit.
        local = FakeLocal({"widgets": [make_branch("a", days=1), make_branch("b", days=2)]})
        self.assertEqual(findings(local_only, make_world(config=with_clones(), clones=local)), [])

    def test_no_branches_is_no_drift(self):
        self.assertEqual(findings(local_only, make_world(config=with_clones())), [])

    def test_a_branch_with_nothing_unpushed_is_no_drift(self):
        local = FakeLocal({"widgets": [make_branch(only_here=0)]})
        self.assertEqual(findings(local_only, make_world(config=with_clones(), clones=local)), [])

    def test_the_threshold_comes_from_the_config(self):
        config = make_config(thresholds={"quiet_days": 10}, repos={"widgets": {"github": "acme/widgets", "path": "~/x"}})
        local = FakeLocal({"widgets": [make_branch(days=7)]})
        self.assertEqual(findings(local_only, make_world(config=config, clones=local)), [])

    def test_failure_is_unchecked_never_a_pass(self):
        world = make_world(config=with_clones(), clones=FakeLocal(broken=["widgets"]))
        (item,) = findings(local_only, world)
        self.assertEqual(item, Unchecked(DriftKind.LOCAL_ONLY, "widgets", "local-only commits for widgets: simulated failure"))

    def test_a_repository_without_a_path_is_not_looked_at(self):
        # gadgets has no path: its branches and its failure are both ignored.
        local = FakeLocal({"gadgets": [make_branch()]}, broken=["gadgets"])
        self.assertEqual(findings(local_only, make_world(config=with_clones(), clones=local)), [])


class GuardOffTest(unittest.TestCase):
    def test_public_clone_without_the_hook(self):
        (drift,) = findings(guard_off, make_world(config=with_clones(public=True)))
        self.assertEqual(drift.kind, DriftKind.GUARD_OFF)
        self.assertEqual((drift.subject, drift.title), ("widgets", "pre-push hook"))
        self.assertEqual(drift.evidence, "public repository, no executable pre-push hook in this clone")
        self.assertEqual(drift.action, "Turn the repository's hooks on before the next push.")

    def test_public_clone_with_the_hook_is_no_drift(self):
        world = make_world(config=with_clones(public=True), clones=FakeLocal(hooked=["widgets"]))
        self.assertEqual(findings(guard_off, world), [])

    def test_a_private_repository_is_not_looked_at(self):
        self.assertEqual(findings(guard_off, make_world(config=with_clones())), [])

    def test_failure_is_unchecked_never_a_pass(self):
        world = make_world(config=with_clones(public=True), clones=FakeLocal(broken=["widgets"]))
        (item,) = findings(guard_off, world)
        self.assertEqual(item, Unchecked(DriftKind.GUARD_OFF, "widgets", "pre-push hook for widgets: simulated failure"))

    def test_public_without_a_path_is_not_looked_at(self):
        config = make_config(repos={"widgets": {"github": "acme/widgets", "public": True}})
        self.assertEqual(findings(guard_off, make_world(config=config, clones=FakeLocal(broken=["widgets"]))), [])


class RunChecksTest(unittest.TestCase):
    def test_order_and_waiting(self):
        self.assertEqual(
            [module.NAME for module in ALL_CHECKS],
            [
                DriftKind.BLOCKER_GONE, DriftKind.BLOCKER_STUCK, DriftKind.SILENT_WAIT,
                DriftKind.QUIET_HOLDER, DriftKind.SCHEDULE_RED, DriftKind.PR_IDLE,
                DriftKind.LOCAL_ONLY, DriftKind.GUARD_OFF,
            ],
        )
        old, new = make_task("task-old", updated=days_ago(9), notes="blocker: x"), make_task("task-new", notes="blocker: y")
        report = run_checks(make_world([new, old]))
        self.assertEqual([task.id for task in report.waiting], ["task-old", "task-new"])
        self.assertTrue(report.clean)

    def test_unexpected_exceptions_are_not_swallowed(self):
        class Exploding(FakeGitHub):
            def pull_request(self, repo, number):
                raise RuntimeError("bug")

        with self.assertRaises(RuntimeError):
            run_checks(make_world([blocked_on(12)], github=Exploding()))


if __name__ == "__main__":
    unittest.main()

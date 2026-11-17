"""GitHub answers "unknown" until it has computed mergeability. That is never a pass."""

from __future__ import annotations

import unittest

from support import FakeGitHub, days_ago, make_pr, make_task, make_world

from oversight.checks import blocker_stuck, pr_idle
from oversight.checks.blocker_stuck import MERGEABILITY_UNKNOWN
from oversight.drift import Drift, DriftKind, Unchecked
from oversight.github import Checks, Mergeability


def unknown(number: int, **kwargs: object):
    return make_pr(number, mergeability=Mergeability.UNKNOWN, updated=days_ago(9), **kwargs)


class UnknownMergeabilityTest(unittest.TestCase):
    def test_a_blocking_pull_request_with_no_answer_is_unchecked(self):
        task = make_task(notes="blocker: waiting for PR #12")
        world = make_world([task], github=FakeGitHub(prs=[unknown(12)]))
        (item,) = list(blocker_stuck.check(world))
        self.assertEqual(item, Unchecked(DriftKind.BLOCKER_STUCK, task.id, MERGEABILITY_UNKNOWN))

    def test_an_idle_pull_request_does_not_need_an_answer_to_be_reported(self):
        world = make_world(github=FakeGitHub(open_prs=[unknown(21)]))
        (item,) = list(pr_idle.check(world))
        self.assertIsInstance(item, Drift)
        self.assertEqual(item.evidence, "PR #21 open, last updated 2026-11-08")

    def test_failing_checks_are_named_when_there_is_no_answer(self):
        world = make_world(github=FakeGitHub(open_prs=[unknown(21, checks=Checks.FAILING)]))
        (item,) = list(pr_idle.check(world))
        self.assertIsInstance(item, Drift)
        self.assertEqual(item.evidence, "PR #21 open, checks failing, last updated 2026-11-08")

    def test_a_fresh_pull_request_is_left_alone(self):
        fresh = make_pr(22, mergeability=Mergeability.UNKNOWN, updated=days_ago(1))
        self.assertEqual(list(pr_idle.check(make_world(github=FakeGitHub(open_prs=[fresh])))), [])


if __name__ == "__main__":
    unittest.main()

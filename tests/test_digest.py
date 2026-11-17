import unittest

from oversight.checks import run_checks
from oversight.config import Lang
from oversight.digest import render_digest
from oversight.drift import Drift, DriftKind, Report, Unchecked

from support import NOW, days_ago, make_task, make_world


def task(task_id, kind, days, notes="blocker: waiting for the reviewer", title="Sample task"):
    labels = ["repo:widgets"] + ([f"wait:{kind}"] if kind else [])
    return make_task(task_id, labels=labels, notes=notes, updated=days_ago(days), title=title)


def drift(n):
    return Drift(DriftKind.SILENT_WAIT, f"task-d{n}", "t", f"evidence {n}", "do it")


def report(waiting=(), drifts=(), unchecked=()):
    ordered = tuple(sorted(waiting, key=lambda t: t.updated_at))
    return Report(NOW, ordered, tuple(drifts), tuple(unchecked))


class DigestTest(unittest.TestCase):
    def lines(self, rep, lang=Lang.EN, budget=10):
        return render_digest(rep, lang, budget).split("\n")

    def test_english_counts_and_grouping(self):
        rep = report(
            waiting=[
                task("task-u1", None, 12, notes=""),
                task("task-n1", "notice", 4, notes="blocker: the nightly run is red"),
                task("task-r1", "review", 2),
                task("task-d1", "decision", 3, title="Pick a name"),
                task("task-d2", "decision", 8, notes="blocker: PR #4 | next: x"),
            ],
            drifts=[drift(1), drift(2)],
            unchecked=[Unchecked(DriftKind.BLOCKER_GONE, "task-x", "why")],
        )
        self.assertEqual(
            self.lines(rep),
            [
                "5 waiting on you, oldest 12 days. 2 drifts. 1 unchecked.",
                "decide task-d2 Sample task — PR #4 (8d)",
                "decide task-d1 Pick a name — waiting for the reviewer (3d)",
                "review task-r1 Sample task — waiting for the reviewer (2d)",
                "notice task-n1 Sample task — the nightly run is red (4d)",
                "untyped task-u1 Sample task — (no blocker written) (12d)",
                "silent_wait: task-d1 — evidence 1",
                "silent_wait: task-d2 — evidence 2",
            ],
        )

    def test_chinese(self):
        rep = report(
            waiting=[
                task("task-d1", "decision", 3),
                task("task-u1", None, 6, notes=""),
                task("task-n1", "notice", 1),
                task("task-r1", "review", 2),
            ],
            drifts=[drift(1)],
            unchecked=[Unchecked(DriftKind.PR_IDLE, "s", "r"), Unchecked(DriftKind.PR_IDLE, "s2", "r")],
        )
        self.assertEqual(
            self.lines(rep, Lang.ZH),
            [
                "等你 4 件，最久 6 天。对不上 1 处。没查到 2 项。",
                "要你定 task-d1 Sample task — waiting for the reviewer (3d)",
                "要你看 task-r1 Sample task — waiting for the reviewer (2d)",
                "出了事 task-n1 Sample task — waiting for the reviewer (1d)",
                "未分类 task-u1 Sample task — （没写卡点） (6d)",
                "silent_wait: task-d1 — evidence 1",
            ],
        )

    def test_nothing_waiting(self):
        self.assertEqual(
            self.lines(report(drifts=[drift(1)])),
            ["Nothing is waiting on you. 1 drift. 0 unchecked.", "silent_wait: task-d1 — evidence 1"],
        )
        self.assertEqual(
            self.lines(report(unchecked=[Unchecked(DriftKind.PR_IDLE, "s", "r")]), Lang.ZH)[0],
            "没有在等你的事。对不上 0 处。没查到 1 项。",
        )
        self.assertEqual(self.lines(report())[0], "Nothing is waiting on you. 0 drifts. 0 unchecked.")

    def test_one_waiting_one_day(self):
        self.assertEqual(
            self.lines(report(waiting=[task("task-d1", "decision", 1)]))[0],
            "1 waiting on you, oldest 1 day. 0 drifts. 0 unchecked.",
        )

    def test_the_oldest_does_not_depend_on_the_order_of_waiting(self):
        newest_first = Report(NOW, (task("task-w1", "decision", 1), task("task-w2", "decision", 9)), (), ())
        self.assertEqual(
            self.lines(newest_first)[:3],
            ["2 waiting on you, oldest 9 days. 0 drifts. 0 unchecked.",
             "decide task-w2 Sample task — waiting for the reviewer (9d)",
             "decide task-w1 Sample task — waiting for the reviewer (1d)"],
        )
        self.assertEqual(self.lines(newest_first, Lang.ZH)[0], "等你 2 件，最久 9 天。对不上 0 处。没查到 0 项。")

    def test_budget_cuts_and_says_how_many(self):
        rep = report(
            waiting=[task(f"task-w{i}", "decision", i) for i in range(1, 4)],
            drifts=[drift(i) for i in range(1, 8)],
        )
        lines = self.lines(rep, budget=6)
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[-1], "... and 6 more: run oversight reconcile")
        self.assertTrue(lines[1].startswith("decide task-w3"))
        self.assertEqual(self.lines(rep, Lang.ZH, budget=6)[-1], "……还有 6 条，见 oversight reconcile")

    def test_budget_that_just_fits_cuts_nothing(self):
        rep = report(waiting=[task("task-w1", "review", 1)], drifts=[drift(1), drift(2)])
        lines = self.lines(rep, budget=4)
        self.assertEqual(len(lines), 4)
        self.assertNotIn("more", lines[-1])
        tighter = self.lines(rep, budget=3)
        self.assertEqual(len(tighter), 3)
        self.assertIn("2 more", tighter[-1])

    def test_waiting_items_are_cut_too(self):
        rep = report(waiting=[task(f"task-w{i}", "decision", i) for i in range(1, 6)], drifts=[drift(1)])
        lines = self.lines(rep, budget=3)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[-1], "... and 5 more: run oversight reconcile")

    def test_default_budget_of_ten_lines(self):
        rep = report(drifts=[drift(i) for i in range(30)])
        self.assertEqual(len(render_digest(rep, Lang.EN, 10).split("\n")), 10)

    def test_a_clean_world_has_only_the_counts_line(self):
        rep = run_checks(make_world())
        self.assertEqual(render_digest(rep, Lang.EN, 10), "Nothing is waiting on you. 0 drifts. 0 unchecked.")


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oversight import cli

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-11-17T12:00:00Z"


def call(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def run(scenario, *command, config=None, recorded=None):
    return call([
        "--config", str(config or FIXTURES / scenario / "config.toml"),
        "--recorded", str(recorded or FIXTURES / scenario),
        "--now", NOW,
        *command,
    ])


class ReconcileTextTest(unittest.TestCase):
    def test_drifty_scenario(self):
        code, out, err = run("drifty", "reconcile")
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(
            out.splitlines(),
            [
                "BLOCKER_GONE  task-a1b2  Retry uploads on timeout",
                "  PR #12 merged, last updated 2026-11-14",
                "  -> The wait is over: close the task if nothing is left, otherwise reopen it for the queue.",
                "BLOCKER_STUCK  task-c3d4  Cache the schema lookup",
                "  PR #14 open, conflicting, checks failing, last updated 2026-11-11",
                "  -> The pull request cannot merge as it is: rebase or fix it. This does not need the human.",
                "SILENT_WAIT  task-e5f6  Rename the config keys",
                "  blocked, last updated 2026-11-12, notes name no blocker",
                "  -> Write the one thing being waited on, or reopen the task.",
                "QUIET_HOLDER  task-i9j0  Migrate the build script",
                "  in progress, held by agent-3, no update for 7 days",
                "  -> Check whether the holder is still running. If not, take the task over or return it to the queue.",
                "SCHEDULE_RED  acme/widgets nightly  nightly",
                "  failed 3 scheduled runs in a row, since 2026-11-15",
                "  -> Find the failing step and fix it; file a task if none exists.",
                "PR_IDLE  acme/widgets#21  Refactor the parser",
                "  PR #21 open, conflicting, checks failing, last updated 2026-11-01",
                "  -> Finish and merge it, close it, or file a task that says what it waits for.",
                "PR_IDLE  acme/widgets#23  Add a retry budget",
                "  PR #23 open, ready to merge, last updated 2026-11-10",
                "  -> Finish and merge it, close it, or file a task that says what it waits for.",
                "LOCAL_ONLY  widgets fix/retry-budget  fix/retry-budget",
                "  3 commits exist only on this machine, newest 2026-11-10",
                "  -> Push the branch, or delete it if the work is abandoned.",
                "GUARD_OFF  widgets  pre-push hook",
                "  public repository, no executable pre-push hook in this clone",
                "  -> Turn the repository's hooks on before the next push.",
                "unchecked  blocker_gone  task-g7h8  pull request #30 for acme/gadgets: "
                "cannot read pr-view-acme__gadgets-30.json: No such file or directory",
                "9 drift, 1 unchecked, 4 waiting",
            ],
        )

    def test_clean_scenario_exits_zero_even_with_waiting_tasks(self):
        code, out, err = run("clean", "reconcile")
        self.assertEqual((code, out, err), (0, "0 drift, 0 unchecked, 1 waiting\n", ""))


class ReconcileJsonTest(unittest.TestCase):
    def test_shape(self):
        code, out, _ = run("drifty", "reconcile", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(set(data), {"checked_at", "waiting", "drifts", "unchecked"})
        self.assertEqual(data["checked_at"], NOW)
        self.assertEqual(
            data["waiting"][0],
            {
                "id": "task-a1b2",
                "title": "Retry uploads on timeout",
                "kind": "review",
                "blocker": "waiting for you to merge PR #12",
                "days": 12,
            },
        )
        self.assertEqual([w["id"] for w in data["waiting"]], ["task-a1b2", "task-c3d4", "task-e5f6", "task-g7h8"])
        self.assertEqual([w["kind"] for w in data["waiting"]], ["review", "decision", "untyped", "notice"])
        self.assertIsNone(data["waiting"][2]["blocker"])
        self.assertEqual(set(data["drifts"][0]), {"kind", "subject", "title", "evidence", "action"})
        self.assertEqual(
            [d["kind"] for d in data["drifts"]],
            ["blocker_gone", "blocker_stuck", "silent_wait", "quiet_holder", "schedule_red", "pr_idle", "pr_idle",
             "local_only", "guard_off"],
        )
        self.assertEqual(set(data["unchecked"][0]), {"check", "subject", "reason"})
        self.assertEqual(data["unchecked"][0]["check"], "blocker_gone")

    def test_clean_json(self):
        code, out, _ = run("clean", "reconcile", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["drifts"], data["unchecked"]), (0, [], []))


class DigestCommandTest(unittest.TestCase):
    def test_default_budget(self):
        code, out, _ = run("drifty", "digest")
        lines = out.splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "4 waiting on you, oldest 12 days. 9 drifts. 1 unchecked.")
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[1], "decide task-c3d4 Cache the schema lookup — 等你合并 PR #14 (6d)")
        self.assertEqual(lines[-1], "... and 5 more: run oversight reconcile")

    def test_max_option_and_exit_code_of_clean_scenario(self):
        code, out, _ = run("clean", "digest", "--max", "2")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "1 waiting on you, oldest 1 day. 0 drifts. 0 unchecked.")

    def test_language_comes_from_the_config(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            text = (FIXTURES / "clean" / "config.toml").read_text(encoding="utf-8")
            config.write_text(text.replace('lang = "en"', 'lang = "zh"'), encoding="utf-8")
            _, out, _ = run("clean", "digest", config=config)
        self.assertEqual(out.splitlines()[0], "等你 1 件，最久 1 天。对不上 0 处。没查到 0 项。")


class Utf8OutputTest(unittest.TestCase):
    def test_output_is_utf_8_whatever_the_locale_encoding_is(self):
        raw = io.BytesIO()
        latin_1 = io.TextIOWrapper(raw, encoding="latin-1", write_through=True)
        with contextlib.redirect_stdout(latin_1):
            code = cli.main([
                "--config", str(FIXTURES / "drifty" / "config.toml"), "--recorded", str(FIXTURES / "drifty"),
                "--now", NOW, "digest",
            ])
        self.assertEqual(code, 1)
        self.assertIn("等你合并 PR #14 (6d)", raw.getvalue().decode("utf-8"))
        self.assertIn(" — ", raw.getvalue().decode("utf-8"))


class ErrorTest(unittest.TestCase):
    def test_config_error_exits_two_on_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            config.write_text('[tracker]\ndir = "x"\n[repos.w]\ngithub = "nope"\n', encoding="utf-8")
            code, out, err = run("clean", "reconcile", config=config)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("repos.w.github", err)

    def test_missing_config_file(self):
        code, _, err = run("clean", "reconcile", config="/nonexistent/oversight.toml")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_config_that_is_not_utf_8_exits_two(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            config.write_bytes(b'# r\xe9sum\xe9\n[tracker]\ndir = "x"\n')
            code, out, err = run("clean", "reconcile", config=config)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not UTF-8", err)

    def test_pull_request_listing_at_its_limit_is_unchecked(self):
        pr = {"number": 1, "title": "t", "state": "OPEN", "updatedAt": "2026-11-17T00:00:00Z"}
        with tempfile.TemporaryDirectory() as folder:
            for source in (FIXTURES / "clean").iterdir():
                shutil.copy(source, folder)
            (Path(folder) / "pr-list-acme__widgets.json").write_text(json.dumps([pr] * 1000), encoding="utf-8")
            code, out, err = run("clean", "reconcile", recorded=folder)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(
            out.splitlines(),
            [
                "unchecked  pr_idle  acme/widgets  open pull requests for acme/widgets: "
                "listing returned its limit of 1000; there may be more",
                "0 drift, 1 unchecked, 1 waiting",
            ],
        )

    def test_missing_local_facts_are_unchecked_not_clean(self):
        with tempfile.TemporaryDirectory() as folder:
            for source in (FIXTURES / "clean").iterdir():
                if source.name != "local-widgets.json":
                    shutil.copy(source, folder)
            code, out, err = run("clean", "reconcile", recorded=folder)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(
            out.splitlines(),
            [
                "unchecked  local_only  widgets  local-only commits for widgets: "
                "cannot read local-widgets.json: No such file or directory",
                "unchecked  guard_off  widgets  pre-push hook for widgets: "
                "cannot read local-widgets.json: No such file or directory",
                "0 drift, 2 unchecked, 1 waiting",
            ],
        )

    def test_a_clone_without_its_hook_alone_is_one_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            for source in (FIXTURES / "clean").iterdir():
                shutil.copy(source, folder)
            facts = Path(folder) / "local-widgets.json"
            facts.write_text(facts.read_text(encoding="utf-8").replace("true", "false"), encoding="utf-8")
            code, out, _ = run("clean", "reconcile", "--json", recorded=folder)
        self.assertEqual(code, 1)
        self.assertEqual([d["kind"] for d in json.loads(out)["drifts"]], ["guard_off"])

    def test_tracker_error_exits_two(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out, err = run("clean", "reconcile", recorded=empty)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("tracker", err)

    def test_a_crash_is_exit_two_not_the_findings_code(self):
        class Exploding:
            def tasks(self, status):
                raise RuntimeError("bug")

        err = io.StringIO()
        with mock.patch.object(cli, "_sources", return_value=(Exploding(), None)), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(FIXTURES / "clean" / "config.toml"), "reconcile"])
        self.assertEqual(code, 2)
        self.assertIn("Traceback", err.getvalue())
        self.assertTrue(err.getvalue().endswith("oversight: internal error: RuntimeError: bug\n"))

    def test_a_time_out_of_range_is_a_tracker_error(self):
        with tempfile.TemporaryDirectory() as folder:
            for source in (FIXTURES / "clean").iterdir():
                shutil.copy(source, folder)
            path = Path(folder) / "tracker-blocked.json"
            tasks = json.loads(path.read_text(encoding="utf-8"))
            tasks[0]["updated_at"] = "0001-01-01T00:00:00+01:00"
            path.write_text(json.dumps(tasks), encoding="utf-8")
            code, out, err = run("clean", "reconcile", recorded=folder)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("tracker", err)

    def test_bad_arguments_are_rejected_by_the_parser(self):
        for argv in (["digest", "--max", "1"], ["--now", "soon", "reconcile"], []):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(argv)
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

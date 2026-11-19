"""Tests for tools/check_integrity.py against real commits in throwaway repositories.

Every repository, file and message here is made up. The test files written into those
repositories hold skip markers and suppressions on purpose: they are the input.
"""

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "check_integrity.py"
RULES = SCRIPT.with_name("check_integrity_rules.json")
REASON = "Check-change: the flaky network test moves to the nightly suite"

_spec = importlib.util.spec_from_file_location("check_integrity", SCRIPT)
ci = importlib.util.module_from_spec(_spec)
sys.modules["check_integrity"] = ci  # dataclasses look their module up here
_spec.loader.exec_module(ci)


def isolated_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL="author@example.com",
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL="author@example.com",
    )
    return env


def file_name(key):
    """Keyword names stand for paths: "__" is "/", a leading "_" is ".", and when the last part
    has no dot its last "_" is one. So tests__test_a_py is tests/test_a.py."""
    path = key.replace("__", "/")
    if path.startswith("_"):
        path = "." + path[1:]
    head, _, last = path.rpartition("/")
    if "." not in last and "_" in last:
        stem, _, suffix = last.rpartition("_")
        last = f"{stem}.{suffix}"
    return f"{head}/{last}" if head else last


class Repo:
    """A throwaway git repository with helpers to write files and commit."""

    def __init__(self, root: Path):
        self.root = root
        self.env = isolated_env()
        self.git("init", "-q", "-b", "main")

    def git(self, *args, check=True):
        done = subprocess.run(
            ["git", *args], cwd=self.root, env=self.env, capture_output=True, check=False
        )
        if check and done.returncode:
            raise AssertionError(f"git {args} failed: {done.stderr.decode()}")
        return done.stdout.decode("utf-8", "replace").strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def commit(self, message="Change things", **files):
        """Write files (name -> text, bytes, or None to delete) and commit everything."""
        for name, content in files.items():
            self.change(name, content)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def change(self, name, content):
        path = file_name(name)
        if content is None:
            self.git("rm", "-q", "--", path)
        else:
            self.write(path, content)

    def scan(self, *args, stdin=None, cwd=None):
        done = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=cwd or self.root,
            env=self.env,
            input=None if stdin is None else stdin.encode(),
            capture_output=True,
            check=False,
        )
        return done.returncode, done.stdout.decode(), done.stderr.decode()


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repo(Path(self._tmp.name))

    def head_json(self, rev="HEAD"):
        code, out, err = self.repo.scan("--commit", rev, "--json")
        self.assertIn(code, (0, 1), err)
        return code, json.loads(out)

    def rules_at_head(self):
        """(path, line, rule, justified) of every finding of HEAD."""
        _, report = self.head_json()
        return {(f["path"], f["line"], f["rule"], f["justified"]) for f in report["findings"]}

    def rule_names(self):
        return {rule for _, _, rule, _ in self.rules_at_head()}


BASE_TEST = (
    "import unittest\n"
    "\n"
    "class TestThing(unittest.TestCase):\n"
    "    def test_one(self):\n"
    "        self.assertEqual(f(), 1)\n"
    "\n"
    "    def test_two(self):\n"
    "        assert 2 == 2\n"
)


class SkipAdded(RepoCase):
    def test_python_skip_is_red_with_line_and_text(self):
        self.repo.commit(tests__test_thing_py=BASE_TEST)
        changed = BASE_TEST.replace(
            "    def test_two", '    @unittest.skip("flaky")\n    def test_two'
        )
        oid = self.repo.commit(tests__test_thing_py=changed)
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1)
        self.assertIn(f'{oid[:7]} tests/test_thing.py:7 skip-added  @unittest.skip("flaky")', out)
        self.assertIn("Check-change:", out.splitlines()[-1])

    def test_every_language_marker_is_red(self):
        samples = {
            "tests/test_a.py": "    self.skipTest('later')\n",
            "tests/test_b.py": "raise unittest.SkipTest('x')\n",
            "test_c.py": "@pytest.mark.skipif(True, reason='x')\n",
            "tests/test_d.py": "@pytest.mark.xfail\n",
            "tests/test_e.py": "    pytest.skip('x')\n",
            "web/a.test.ts": "it.skip('works', () => {})\n",
            "web/b.spec.js": "describe.only('suite', () => {})\n",
            "web/c.test.tsx": "xit('works', () => {})\n",
            "web/d.test.jsx": "xdescribe('suite', () => {})\n",
            "web/e.spec.ts": "xtest('works', () => {})\n",
            "web/f.test.mjs": "test.todo('later')\n",
            "crate/tests/g.rs": "#[ignore]\n",
            "pkg/h_test.go": "\tt.Skip(\"later\")\n",
            "pkg/i_test.go": "\tt.Skipf(\"later %d\", 1)\n",
        }
        for path in samples:
            self.repo.write(path, "// seed\n")
        self.repo.commit()
        for path, line in samples.items():
            self.repo.write(path, "// seed\n" + line)
        self.repo.commit()
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        for path in samples:
            with self.subTest(path=path):
                self.assertIn((path, "skip-added"), found)

    def test_skip_outside_tests_or_removed_is_green(self):
        self.repo.commit(
            tests__test_thing_py='@unittest.skip("old")\ndef test_x():\n    assert 1\n'
        )
        self.repo.commit(
            tests__test_thing_py="def test_x():\n    assert 1\n",
            src__runner_py='SKIP_HELP = "use @unittest.skip to skip"\n',
            docs__guide_md="Use it.only( sparingly.\n",
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 3 files)"))

    def test_model_fit_is_not_a_focused_test(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(tests__test_model_py="def test_fit():\n    model.fit(data)\n    fit(data)\n    assert True\n")
        self.assertNotIn("skip-added", self.rule_names())


class SuppressionAdded(RepoCase):
    def test_each_marker_is_red(self):
        self.repo.commit(README_md="seed\n")
        samples = {
            "src/a.py": "x = f()  # type: ignore[arg-type]\n",
            "src/b.py": "import os  # noqa\n",
            "src/c.py": "y = 1  # pyright: ignore\n",
            "src/d.py": "if x:  # pragma: no cover\n",
            "src/e.py": "run(cmd, shell=True)  # nosec\n",
            "src/f.ts": "// @ts-ignore\n",
            "src/g.ts": "// @ts-expect-error\n",
            "src/h.ts": "// @ts-nocheck\n",
            "src/i.js": "/* eslint-disable no-console */\n",
            "src/j.js": "// biome-ignore lint: reason\n",
            "src/k.rs": "#[allow(dead_code)]\n",
            "src/l.rs": "#![allow(clippy::all)]\n",
            "src/m.go": "x := y //nolint\n",
            "src/n.sh": "# shellcheck disable=SC2086\n",
        }
        for path, line in samples.items():
            self.repo.write(path, line)
        self.repo.commit()
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        for path in samples:
            with self.subTest(path=path):
                self.assertIn((path, "suppression-added"), found)

    def test_noqa_e402_alone_is_allowed_but_not_with_more_codes(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            tests__test_a_py="import sys\nsys.path.insert(0, 'x')\nimport thing  # noqa: E402\n",
            tests__test_b_py="import thing  # noqa: E402, F811\n",
        )
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        self.assertNotIn(("tests/test_a.py", "suppression-added"), found)
        self.assertIn(("tests/test_b.py", "suppression-added"), found)

    def test_marker_in_prose_file_is_green(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(docs__notes_md="Never write # type: ignore or @ts-ignore.\n")
        code, _, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0)


class BypassAdded(RepoCase):
    def test_each_bypass_is_red(self):
        self.repo.commit(README_md="seed\n")
        samples = {
            ".githooks/pre-push": "git push --no-verify\n",
            "Makefile": "test:\n\tpytest || true\n",
            "scripts/check.sh": "ruff check . || :\n",
            ".github/workflows/ci.yml": "      - run: pytest\n        continue-on-error: true\n",
            ".github/workflows/lint.yml": "    if: false\n",
            "package.json": '{"scripts": {"test": "npm test || true"}}\n',
            ".husky/pre-commit": "HUSKY=0 git commit\n",
            ".githooks/post-checkout": "git config core.hooksPath /dev/null\n",
        }
        for path, line in samples.items():
            self.repo.write(path, line)
        self.repo.commit()
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        for path in samples:
            with self.subTest(path=path):
                self.assertIn((path, "bypass-added"), found)

    def test_bypass_outside_hooks_and_ci_is_green(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            docs__faq_md="Do not use --no-verify or `|| true`.\n",
            src__app_py="value = a or True\n",
        )
        code, _, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0)


class TestRemoved(RepoCase):
    def test_deleted_test_file_is_red(self):
        self.repo.commit(tests__test_thing_py=BASE_TEST)
        self.repo.commit(tests__test_thing_py=None)
        self.assertEqual(self.rules_at_head(), {("tests/test_thing.py", 0, "test-removed", False)})

    def test_fewer_test_definitions_is_red(self):
        self.repo.commit(tests__test_thing_py=BASE_TEST)
        shorter = BASE_TEST.replace("\n    def test_two(self):\n        assert 2 == 2\n", "")
        self.repo.commit(tests__test_thing_py=shorter)
        names = self.rule_names()
        self.assertIn("test-removed", names)
        self.assertIn("assertion-loss", names)

    def test_renamed_test_and_deleted_non_test_are_green(self):
        self.repo.commit(tests__test_thing_py=BASE_TEST, src__old_py="x = 1\n")
        self.repo.commit(
            tests__test_thing_py=BASE_TEST.replace("test_two", "test_second"), src__old_py=None
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_deleting_a_python_function_that_is_not_a_test_is_green_but_deleting_a_test_is_red(self):
        # none of the deleted bodies holds an assertion line, so only test-removed can fire
        with_helpers = (
            "import unittest\n"
            "\n"
            "class TestThing(unittest.TestCase):\n"
            "    def setUp(self):\n"
            "        self.value = 1\n"
            "\n"
            "    def helper(self):\n"
            "        return self.value\n"
            "\n"
            "    def test_one(self):\n"
            "        self.assertEqual(f(), 1)\n"
            "\n"
            "    def test_two(self):\n"
            "        pass\n"
        )
        self.repo.commit(tests__test_thing_py=with_helpers)
        without_helpers = with_helpers.replace(
            "    def setUp(self):\n        self.value = 1\n\n", ""
        ).replace("    def helper(self):\n        return self.value\n\n", "")
        self.assertNotEqual(without_helpers, with_helpers)
        self.assertNotIn("def helper", without_helpers)
        self.assertNotIn("def setUp", without_helpers)
        self.repo.commit(tests__test_thing_py=without_helpers)
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.repo.commit(
            tests__test_thing_py=without_helpers.replace("\n    def test_two(self):\n        pass\n", "")
        )
        self.assertEqual(
            {(path, rule) for path, _, rule, _ in self.rules_at_head()}, {("tests/test_thing.py", "test-removed")}
        )

    def test_deleting_a_go_function_that_is_not_a_test_is_green_but_deleting_a_test_is_red(self):
        with_helper = (
            "package pkg\n"
            "\n"
            "func helper() int {\n"
            "\treturn 1\n"
            "}\n"
            "\n"
            "func TestOne(t *testing.T) {}\n"
            "\n"
            "func TestTwo(t *testing.T) {}\n"
        )
        self.repo.commit(pkg__b_test_go=with_helper)
        without_helper = with_helper.replace("func helper() int {\n\treturn 1\n}\n\n", "")
        self.assertNotIn("func helper", without_helper)
        self.repo.commit(pkg__b_test_go=without_helper)
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.repo.commit(pkg__b_test_go=without_helper.replace("\nfunc TestTwo(t *testing.T) {}\n", ""))
        self.assertEqual(
            {(path, rule) for path, _, rule, _ in self.rules_at_head()}, {("pkg/b_test.go", "test-removed")}
        )

    def test_js_and_go_definitions_count(self):
        self.repo.commit(
            **{"web/a.test.ts": "it('one', () => {})\ntest('two', () => {})\n"},
            pkg__b_test_go="func TestOne(t *testing.T) {}\nfunc TestTwo(t *testing.T) {}\n",
        )
        self.repo.commit(
            **{"web/a.test.ts": "it('one', () => {})\n"},
            pkg__b_test_go="func TestOne(t *testing.T) {}\n",
        )
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        self.assertIn(("web/a.test.ts", "test-removed"), found)
        self.assertIn(("pkg/b_test.go", "test-removed"), found)


class AssertionLoss(RepoCase):
    def test_dropped_assertions_are_red(self):
        self.repo.commit(tests__test_thing_py=BASE_TEST)
        self.repo.commit(
            tests__test_thing_py=BASE_TEST.replace("        assert 2 == 2\n", "        pass\n")
        )
        self.assertEqual(self.rules_at_head(), {("tests/test_thing.py", 8, "assertion-loss", False)})

    def test_rewritten_or_added_assertions_are_green(self):
        self.repo.commit(tests__test_thing_py=BASE_TEST)
        self.repo.commit(
            tests__test_thing_py=BASE_TEST.replace("assert 2 == 2", "self.assertEqual(len('ab'), 2)")
            + "        self.assertIn(2, [2])\n"
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_rust_and_go_assertions_count(self):
        self.repo.commit(
            crate__tests__a_rs="#[test]\nfn a() {\n    assert_eq!(1, 1);\n}\n",
            pkg__a_test_go="func TestA(t *testing.T) {\n\tt.Fatal(\"x\")\n}\n",
        )
        self.repo.commit(
            crate__tests__a_rs="#[test]\nfn a() {\n}\n",
            pkg__a_test_go="func TestA(t *testing.T) {\n}\n",
        )
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        self.assertIn(("crate/tests/a.rs", "assertion-loss"), found)
        self.assertIn(("pkg/a_test.go", "assertion-loss"), found)


SHORT_CIRCUIT_RULE = "test-short-circuited"
DETACHED_RULE = "test-class-detached"
TAUTOLOGY_RULE = "tautology-added"


class ShortCircuitedTest(RepoCase):
    """A bare return in a test body ends the test before its assertions; the test still passes."""

    def commit_edit(self, path, base, anchor, line):
        self.repo.commit(**{path.replace("/", "__"): base})
        self.repo.commit(**{path.replace("/", "__"): base.replace(anchor, anchor + line, 1)})

    def test_a_bare_return_added_to_an_existing_test_is_red_in_each_language(self):
        cases = {
            "python return": ("tests/test_a.py", BASE_TEST, "    def test_one(self):\n", "        return\n", 5),
            "python return None": ("tests/test_a.py", BASE_TEST, "    def test_one(self):\n", "        return None\n", 5),
            "python return with a comment": ("tests/test_a.py", BASE_TEST, "    def test_one(self):\n", "        return  # later\n", 5),
            "js": ("web/a.test.ts", "it('one', () => {\n  expect(f()).toBe(1)\n})\n", "it('one', () => {\n", "  return;\n", 2),
            "rust": ("crate/tests/a.rs", "#[test]\nfn a() {\n    assert_eq!(f(), 1);\n}\n", "fn a() {\n", "    return;\n", 3),
            "go": ("pkg/a_test.go", "func TestA(t *testing.T) {\n\tt.Fatal(f())\n}\n", "func TestA(t *testing.T) {\n", "\treturn\n", 2),
        }
        for name, (path, base, anchor, line, number) in cases.items():
            with self.subTest(name):
                self.commit_edit(path, base, anchor, line)
                self.assertEqual(self.rules_at_head(), {(path, number, SHORT_CIRCUIT_RULE, False)})

    def test_a_return_with_a_value_and_a_return_outside_the_test_files_are_green(self):
        self.repo.commit(tests__test_a_py=BASE_TEST, src__a_py="def f():\n    pass\n")
        self.repo.commit(
            tests__test_a_py=BASE_TEST.replace("    def test_two(self):\n", "    def helper(self):\n        return 1\n\n    def test_two(self):\n"),
            src__a_py="def f():\n    return\n",
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_return_that_only_moves_inside_its_file_is_green(self):
        before = "def check(x):\n    if x:\n        return\n    use(x)\n"
        after = "def check(x):\n    use(x)\n    if not x:\n        note(x)\n    return\n"
        self.repo.commit(tests__test_a_py=BASE_TEST + "\n" + before)
        self.repo.commit(tests__test_a_py=BASE_TEST + "\n" + after)
        _, report = self.head_json()
        self.assertEqual(report["findings"], [])

    def test_a_reason_line_passes_it(self):
        self.repo.commit(tests__test_a_py=BASE_TEST)
        self.repo.commit(
            f"Park a test\n\n{REASON}",
            tests__test_a_py=BASE_TEST.replace("    def test_one(self):\n", "    def test_one(self):\n        return\n"),
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    HELPERS = (
        "\n\nclass Fake:\n"
        "    def close(self):\n"
        "        self.closed = True\n"
        "\n\ndef wait_for(items):\n"
        "    for item in items:\n"
        "        if item:\n"
        "            break\n"
    )
    VALUE_TEST = BASE_TEST.replace("        assert 2 == 2\n", "        value = f()\n        assert 2 == 2\n")
    JS_HELPER = "function wait(done) {\n  if (done) {\n    use();\n  }\n}\n"

    def test_a_return_that_is_not_the_first_statement_of_a_test_is_green(self):
        cases = {
            "in the loop of a helper": ("tests/test_a.py", BASE_TEST + self.HELPERS, "            break\n", "            return\n"),
            "first statement of a helper that is no test": (
                "tests/test_a.py",
                BASE_TEST + self.HELPERS,
                "    def close(self):\n",
                "    def close(self):\n        return None\n",
            ),
            "in the middle of a test": (
                "tests/test_a.py",
                self.VALUE_TEST,
                "        value = f()\n",
                "        value = f()\n        if value is None:\n            return\n",
            ),
            "js helper": ("web/a.test.ts", self.JS_HELPER, "    use();\n", "    use();\n    return;\n"),
        }
        for name, (path, base, anchor, line) in cases.items():
            with self.subTest(name):
                self.repo.commit(**{path.replace("/", "__"): base})
                self.repo.commit(**{path.replace("/", "__"): base.replace(anchor, line, 1)})
                self.assertEqual(self.rules_at_head(), set())

    def test_a_bare_return_as_the_first_statement_of_a_function_that_is_no_test_is_green(self):
        helpers = "\n    def helper(self):\n        use()\n\n    def setUp(self):\n        use()\n"
        cases = {
            "python helper": ("tests/test_a.py", BASE_TEST + helpers, "    def helper(self):\n"),
            "python setUp": ("tests/test_a.py", BASE_TEST + helpers, "    def setUp(self):\n"),
            "go helper": ("pkg/a_test.go", "func helper() {\n\tuse()\n}\n", "func helper() {\n"),
        }
        for name, (path, base, anchor) in cases.items():
            with self.subTest(name):
                bare = "\treturn\n" if path.endswith(".go") else "        return\n"
                self.repo.commit(**{path.replace("/", "__"): base})
                self.repo.commit(**{path.replace("/", "__"): base.replace(anchor, anchor + bare, 1)})
                self.assertEqual(self.rules_at_head(), set())

    def test_a_return_as_the_first_statement_is_red_in_each_form_of_a_test_definition(self):
        cases = {
            "async python test with a return type": (
                "tests/test_a.py",
                BASE_TEST.replace("    def test_one(self):", "    async def test_one(self) -> None:"),
                "    async def test_one(self) -> None:\n",
                "        return\n",
                5,
            ),
            "js it.only": ("web/a.test.ts", "it.only('one', () => {\n  expect(f()).toBe(1)\n})\n", "it.only('one', () => {\n", "  return\n", 2),
            "js test with an async callback": ("web/a.test.ts", "test('one', async () => {\n  expect(f()).toBe(1)\n})\n", "test('one', async () => {\n", "  return;\n", 2),
            "rust async function": ("crate/tests/a.rs", "pub async fn a() {\n    check().await;\n}\n", "pub async fn a() {\n", "    return;\n", 2),
        }
        for name, (path, base, anchor, line, number) in cases.items():
            with self.subTest(name):
                self.commit_edit(path, base, anchor, line)
                self.assertEqual(self.rules_at_head(), {(path, number, SHORT_CIRCUIT_RULE, False)})

    def test_a_new_test_file_is_judged_too(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(tests__test_new_py="def test_x():\n    return\n")
        self.assertEqual(self.rules_at_head(), {("tests/test_new.py", 2, SHORT_CIRCUIT_RULE, False)})

    def test_blank_and_comment_lines_between_the_definition_and_the_return_do_not_hide_it(self):
        self.commit_edit("tests/test_a.py", BASE_TEST, "    def test_one(self):\n", "        # parked\n\n        return\n")
        self.assertEqual(self.rules_at_head(), {("tests/test_a.py", 7, SHORT_CIRCUIT_RULE, False)})

    def test_a_return_that_moves_from_one_test_to_another_is_still_red(self):
        first = "    def test_one(self):\n"
        second = "    def test_two(self):\n"
        self.repo.commit(tests__test_a_py=BASE_TEST.replace(first, first + "        return\n"))
        self.repo.commit(tests__test_a_py=BASE_TEST.replace(second, second + "        return\n"))
        self.assertEqual(self.rules_at_head(), {("tests/test_a.py", 8, SHORT_CIRCUIT_RULE, False)})

    def test_a_return_that_was_there_already_is_not_reported_when_a_line_next_to_it_changes(self):
        parked = BASE_TEST.replace("    def test_one(self):\n", "    def test_one(self):\n        return\n")
        self.repo.commit(tests__test_a_py=parked)
        self.repo.commit(tests__test_a_py=parked.replace("assertEqual(f(), 1)", "assertEqual(f(), 2)"))
        self.assertNotIn(SHORT_CIRCUIT_RULE, self.rule_names())

    def test_a_definition_that_the_patch_does_not_show_is_not_seen(self):
        comments = "        # a\n        # b\n        # c\n        # d\n"
        base = BASE_TEST.replace("        self.assertEqual(f(), 1)\n", comments + "        self.assertEqual(f(), 1)\n")
        self.repo.commit(tests__test_a_py=base)
        self.repo.commit(tests__test_a_py=base.replace("        # d\n", "        # d\n        return\n"))
        self.assertNotIn(SHORT_CIRCUIT_RULE, self.rule_names())

    def rules_changed(self, **changes):
        document = json.loads(RULES.read_text())
        rule = next(r for r in document["rules"] if r["id"] == SHORT_CIRCUIT_RULE)
        for key, value in changes.items():
            if value is None:
                rule.pop(key, None)
            else:
                rule[key] = value
        path = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-short-rules.json"
        path.write_text(json.dumps(document))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_the_after_list_is_checked_when_the_table_is_loaded(self):
        self.repo.commit(README_md="seed\n")
        cases = {
            "no after list": ({"after": None}, "needs a non-empty after list"),
            "an empty after list": ({"after": []}, "needs a non-empty after list"),
            "after is not a list": ({"after": "def test"}, "list of non-empty strings"),
            "after holds an empty string": ({"after": [""]}, "list of non-empty strings"),
            "after holds a number": ({"after": [3]}, "list of non-empty strings"),
            "after does not compile": ({"after": ["("]}, "invalid pattern"),
            "an after list on another kind": ({"kind": "net-added"}, "after works only with kind added-after"),
            "a field no rule has": ({"afters": ["x"]}, "may add"),
            "a pattern that does not compile": ({"patterns": ["("]}, "invalid pattern"),
        }
        for name, (changes, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.repo.scan("--commit", "HEAD", "--rules", str(self.rules_changed(**changes)))
                self.assertEqual(code, 2, out + err)
                self.assertIn(message, err)


DETACHABLE = (
    "import unittest\n"
    "\n"
    "class TestOne(unittest.TestCase):\n"
    "    def test_a(self):\n"
    "        self.assertEqual(f(), 1)\n"
    "\n"
    "class TestTwo(Mixin, unittest.IsolatedAsyncioTestCase):\n"
    "    async def test_b(self):\n"
    "        self.assertEqual(f(), 2)\n"
)


class DetachedTestClass(RepoCase):
    """Without its TestCase base, or gone altogether, a class is no longer collected by unittest
    and its tests stop running without a test being deleted."""

    def test_a_class_that_loses_its_testcase_base_or_goes_away_is_red(self):
        cases = {
            "base dropped": ("class TestOne(unittest.TestCase):\n", "class TestOne:\n", 3),
            "base replaced by object": ("class TestOne(unittest.TestCase):\n", "class TestOne(object):\n", 3),
            "async base dropped from a class with a mixin": (
                "class TestTwo(Mixin, unittest.IsolatedAsyncioTestCase):\n",
                "class TestTwo(Mixin):\n",
                7,
            ),
            "base made a comment": ("class TestOne(unittest.TestCase):\n", "# class TestOne(unittest.TestCase):\n", 3),
        }
        for name, (old, new, number) in cases.items():
            with self.subTest(name):
                self.repo.commit(tests__test_a_py=DETACHABLE)
                self.repo.commit(tests__test_a_py=DETACHABLE.replace(old, new))
                self.assertEqual(self.rules_at_head(), {("tests/test_a.py", number, DETACHED_RULE, False)})

    def test_a_deleted_class_is_red_even_when_its_tests_move_into_another_class(self):
        self.repo.commit(tests__test_a_py=DETACHABLE)
        self.repo.commit(
            tests__test_a_py=DETACHABLE.split("class TestTwo")[0]
            + "    def test_b(self):\n        self.assertEqual(f(), 2)\n"
        )
        self.assertEqual(self.rules_at_head(), {("tests/test_a.py", 7, DETACHED_RULE, False)})

    def test_a_renamed_class_a_wider_base_list_and_a_class_moved_to_another_file_are_green(self):
        self.repo.commit(tests__test_a_py=DETACHABLE)
        self.repo.commit(tests__test_a_py=DETACHABLE.replace("class TestOne(", "class TestUno("))
        self.repo.commit(tests__test_a_py=DETACHABLE.replace("(unittest.TestCase)", "(Mixin, unittest.TestCase)"))
        code, out, _ = self.repo.scan("--range", "HEAD~2..HEAD")
        self.assertEqual(code, 0, out)
        first, second = DETACHABLE.split("class TestTwo")
        self.repo.commit(tests__test_a_py=first, tests__test_b_py="import unittest\n\nclass TestTwo" + second)
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_reason_line_passes_it(self):
        self.repo.commit(tests__test_a_py=DETACHABLE)
        self.repo.commit(f"Drop the base\n\n{REASON}", tests__test_a_py=DETACHABLE.replace("(unittest.TestCase)", ""))
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)


class TautologyAdded(RepoCase):
    """An assertion that cannot fail, added to a test file, replaces one that could."""

    RED = {
        "assertTrue(True)": ("tests/test_a.py", "        self.assertTrue(True)\n"),
        "assertTrue(True, msg)": ("tests/test_a.py", "        self.assertTrue(True, 'always')\n"),
        "assertFalse(False)": ("tests/test_a.py", "        self.assertFalse(False)\n"),
        "assert True": ("tests/test_a.py", "        assert True\n"),
        "assert True with a message": ("tests/test_a.py", "        assert True, 'fine'\n"),
        "assertEqual of one name": ("tests/test_a.py", "        self.assertEqual(result, result)\n"),
        "assertEqual of one attribute": ("tests/test_a.py", "        self.assertEqual(self.x, self.x)\n"),
        "assertEqual of one number": ("tests/test_a.py", "        self.assertEqual(2, 2)\n"),
        "assertEqual of one string": ("tests/test_a.py", "        self.assertEqual('a', 'a', 'same')\n"),
        "assertIs None None": ("tests/test_a.py", "        self.assertIs(None, None)\n"),
        "js expect true": ("web/a.test.ts", "  expect(true).toBe(true)\n"),
        "js expect same string": ("web/a.test.ts", "  expect('a').toEqual('a')\n"),
        "rust assert true": ("crate/tests/a.rs", "    assert!(true);\n"),
        "rust assert true with a message": ("crate/tests/a.rs", "    assert!(true, \"ok\");\n"),
        "assertIs of one attribute chain": ("tests/test_a.py", "        self.assertIs(a.b, a.b)\n"),
        "assertIs of one qualified name": ("tests/test_a.py", "        self.assertEqual(mod.Kind.X, mod.Kind.X)\n"),
        "assert of one name": ("tests/test_a.py", "        assert x == x\n"),
        "assert of one number": ("tests/test_a.py", "        assert 1 == 1\n"),
        "assert of one attribute chain with a message": ("tests/test_a.py", "        assert a.b == a.b, 'same'\n"),
        "assertIsNone of None": ("tests/test_a.py", "        self.assertIsNone(None)\n"),
        "assertIsNotNone of a number": ("tests/test_a.py", "        self.assertIsNotNone(5)\n"),
        "assertIsNotNone of a string": ("tests/test_a.py", "        self.assertIsNotNone('a')\n"),
        "assertIsNotNone of True": ("tests/test_a.py", "        self.assertIsNotNone(True, 'set')\n"),
        "assertTrue of a number": ("tests/test_a.py", "        self.assertTrue(1)\n"),
        "assertTrue of a decimal": ("tests/test_a.py", "        self.assertTrue(0.5)\n"),
        "assertTrue of a string": ("tests/test_a.py", "        self.assertTrue('ok', 'fine')\n"),
        "assert of a number": ("tests/test_a.py", "        assert 1\n"),
        "assert of a string with a message": ("tests/test_a.py", "        assert 'ok', 'fine'\n"),
        "js expect of one name": ("web/a.test.ts", "  expect(value).toEqual(value)\n"),
        "rust assert_eq of one name": ("crate/tests/a.rs", "    assert_eq!(x, x);\n"),
        "rust assert_eq of one name with a message": ("crate/tests/a.rs", "    assert_eq!(x, x, \"same\");\n"),
        "rust debug_assert_eq of one field": ("crate/tests/a.rs", "    debug_assert_eq!(s.a, s.a);\n"),
    }
    GREEN = {
        "assertTrue of a value": "        self.assertTrue(flag)\n",
        "assertTrue of a longer expression": "        self.assertTrue(True and flag)\n",
        "assertEqual of two names": "        self.assertEqual(result, expected)\n",
        "assertEqual of a name and its attribute": "        self.assertEqual(result, result.value)\n",
        "assertEqual of near names": "        self.assertEqual(a, a1)\n",
        "assertEqual of two numbers": "        self.assertEqual(2, 3)\n",
        "assertIs of None and a value": "        self.assertIs(found, None)\n",
        "assert of a comparison": "        assert x == x2\n",
        "assert of a name starting with True": "        assert Truthy\n",
        "assertEqual of a name and a near name": "        self.assertEqual(a, a2)\n",
        "assertEqual of two calls": "        self.assertEqual(f(1), f(2))\n",
        "assertEqual of an attribute and its owner": "        self.assertEqual(a.b, a)\n",
        "assertEqual of a name and an item of it": "        self.assertEqual(x, x[0])\n",
        "assert of two names": "        assert x == y\n",
        "assertEqual of one call written twice": "        self.assertEqual(f(x), f(x))\n",
        "assertEqual of one call with arguments written twice": "        self.assertEqual(f(1, 'a'), f(1, 'a'), 'same')\n",
        "assertEqual of one method call written twice": "        self.assertEqual(a.b(1), a.b(1))\n",
        "assert of one call written twice": "        assert g() == g()\n",
        "assert of one call written twice with a message": "        assert f() == f(), 'same'\n",
        "assert of a name and an item of it": "        assert x == x[0]\n",
        "assert of a comparison with a call": "        assert x == x.y()\n",
        "assert of equal names joined to another test": "        assert x == x and y\n",
        "assertTrue of a name": "        self.assertTrue(value)\n",
        "assertTrue of zero": "        self.assertTrue(0)\n",
        "assertTrue of a comparison with a number": "        self.assertTrue(1 == x)\n",
        "assertTrue of an empty string": "        self.assertTrue('')\n",
        "assertIsNone of a name": "        self.assertIsNone(result)\n",
        "assertIsNotNone of a name": "        self.assertIsNotNone(result)\n",
        "assertIsNotNone of None": "        self.assertIsNotNone(None)\n",
        "assert of a name": "        assert value\n",
        "assert of zero": "        assert 0\n",
        "assert of an empty string": "        assert ''\n",
    }
    GREEN_OTHER_LANGUAGES = {
        "js expect of a call and another call": ("web/a.test.ts", "  expect(f(1)).toBe(f(2))\n"),
        "js expect of a name and its item": ("web/a.test.ts", "  expect(x).toBe(x[0])\n"),
        "rust assert_eq of a name and a near name": ("crate/tests/a.rs", "    assert_eq!(x, x2);\n"),
        "rust assert_eq of two calls": ("crate/tests/a.rs", "    assert_eq!(f(1), f(2));\n"),
        "js expect of one call written twice": ("web/a.test.ts", "  expect(k(d)).toEqual(k(d))\n"),
        "js expect of one call written twice with toBe": ("web/a.test.ts", "  expect(f()).toBe(f())\n"),
        "rust assert_eq of one call written twice": ("crate/tests/a.rs", "    assert_eq!(h(a), h(a));\n"),
        "rust assert_eq of one call written twice with a message": ("crate/tests/a.rs", "    assert_eq!(f(), f(), \"same\");\n"),
    }
    HOST = {
        "tests/test_a.py": (BASE_TEST, "    def test_one(self):\n"),
        "web/a.test.ts": ("it('one', () => {\n  expect(f()).toBe(1)\n})\n", "it('one', () => {\n"),
        "crate/tests/a.rs": ("#[test]\nfn a() {\n    assert_eq!(f(), 1);\n}\n", "fn a() {\n"),
    }

    def add(self, path, line):
        base, anchor = self.HOST[path]
        name = path.replace("/", "__")
        self.repo.commit(**{name: base})
        self.repo.commit(**{name: base.replace(anchor, anchor + line, 1)})

    def test_each_assertion_that_cannot_fail_is_red(self):
        for name, (path, line) in self.RED.items():
            with self.subTest(name):
                self.add(path, line)
                found = self.rules_at_head()
                self.assertEqual({(p, rule) for p, _, rule, _ in found}, {(path, TAUTOLOGY_RULE)}, found)

    def test_assertions_that_can_fail_are_green(self):
        for name, line in self.GREEN.items():
            with self.subTest(name):
                self.add("tests/test_a.py", line)
                self.assertEqual(self.rules_at_head(), set())

    def test_assertions_of_other_languages_that_can_fail_are_green(self):
        for name, (path, line) in self.GREEN_OTHER_LANGUAGES.items():
            with self.subTest(name):
                self.add(path, line)
                self.assertEqual(self.rules_at_head(), set())

    def test_a_real_assertion_swapped_for_one_that_cannot_fail_is_red(self):
        self.repo.commit(tests__test_a_py=BASE_TEST)
        self.repo.commit(tests__test_a_py=BASE_TEST.replace("self.assertEqual(f(), 1)", "self.assertTrue(True)"))
        self.assertEqual({(p, rule) for p, _, rule, _ in self.rules_at_head()}, {("tests/test_a.py", TAUTOLOGY_RULE)})

    def test_one_that_is_only_moved_inside_its_file_is_green(self):
        base = BASE_TEST.replace("        assert 2 == 2\n", "        assert 2 == 2\n        self.assertTrue(True)\n")
        moved = base.replace("        self.assertTrue(True)\n", "").replace(
            "        self.assertEqual(f(), 1)\n", "        self.assertEqual(f(), 1)\n        self.assertTrue(True)\n"
        )
        self.repo.commit(tests__test_a_py=base)
        self.repo.commit(tests__test_a_py=moved)
        self.assertEqual(self.rules_at_head(), set())

    def test_a_new_test_file_is_judged_too_and_a_source_file_is_not(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(tests__test_new_py="def test_x():\n    assert True\n", src__a_py="assert True\n")
        self.assertEqual(self.rules_at_head(), {("tests/test_new.py", 2, TAUTOLOGY_RULE, False)})

    def test_a_reason_line_passes_it(self):
        self.repo.commit(tests__test_a_py=BASE_TEST)
        self.repo.commit(
            f"Stub a test\n\n{REASON}", tests__test_a_py=BASE_TEST + "\n    def test_three(self):\n        assert True\n"
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)


WORKFLOW = "# CI\njobs:\n  test:\n    steps:\n      - run: make lint\n      - run: make test\n"


class CheckFileWeakened(RepoCase):
    def test_deleted_workflow_is_red(self):
        self.repo.commit(_github__workflows__ci_yml=WORKFLOW)
        self.repo.commit(_github__workflows__ci_yml=None)
        self.assertEqual(
            self.rules_at_head(),
            {
                (".github/workflows/ci.yml", 0, "check-file-weakened", False),
                (".github/workflows/ci.yml", 0, "check-machinery-changed", False),
            },
        )

    def test_removed_step_is_weakened_but_comment_and_addition_only_change_the_machinery(self):
        self.repo.commit(_github__workflows__ci_yml=WORKFLOW)
        self.repo.commit(_github__workflows__ci_yml=WORKFLOW.replace("      - run: make lint\n", ""))
        self.assertEqual(
            self.rules_at_head(),
            {
                (".github/workflows/ci.yml", 5, "check-file-weakened", False),
                (".github/workflows/ci.yml", 5, "check-machinery-changed", False),
            },
        )
        self.repo.commit(
            _github__workflows__ci_yml=WORKFLOW.replace("# CI\n", "") + "      - run: make docs\n"
        )
        self.assertEqual(self.rule_names(), {"check-machinery-changed"})

    def test_lint_configs_and_own_files_are_check_files(self):
        files = {
            ".flake8": "[flake8]\nmax-line-length = 100\n",
            "mypy.ini": "[mypy]\nstrict = True\n",
            ".eslintrc.json": '{"rules": {"no-console": "error"}}\n',
            "tools/check_integrity_rules.json": '{"version": 1}\n',
            "tools/privacy_scan.py": "RULES = 1\n",
        }
        self.repo.commit(**{name.replace("/", "__"): text for name, text in files.items()})
        self.repo.commit(**{name.replace("/", "__"): "\n" for name in files})
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        for path in files:
            with self.subTest(path=path):
                self.assertIn((path, "check-file-weakened"), found)

    def test_mixed_config_flags_only_check_lines(self):
        pyproject = '[project]\nname = "demo"\nversion = "1"\n\n[tool.mypy]\nstrict = true\n'
        tsconfig = '{\n  "compilerOptions": {\n    "target": "es2022"\n  }\n}\n'
        self.repo.commit(pyproject_toml=pyproject, tsconfig_json=tsconfig)
        self.repo.commit(pyproject_toml=pyproject.replace('version = "1"\n', 'version = "2"\n'))
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.repo.commit(
            pyproject_toml=pyproject.replace("strict = true\n", ""),
            tsconfig_json=tsconfig.replace('"es2022"\n', '"es2022",\n    "strict": false\n'),
        )
        found = {(path, line, rule) for path, line, rule, _ in self.rules_at_head()}
        self.assertIn(("pyproject.toml", 6, "check-file-weakened"), found)
        self.assertIn(("tsconfig.json", 4, "check-file-weakened"), found)
        self.assertEqual(len(found), 2, found)


class Justification(RepoCase):
    def setUp(self):
        super().setUp()
        self.repo.commit(tests__test_thing_py=BASE_TEST)
        self.skipped = BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two")

    def test_reason_line_makes_findings_justified(self):
        self.repo.commit(f"Park a test\n\n{REASON}\n", tests__test_thing_py=self.skipped)
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0)
        self.assertIn("skip-added justified  @unittest.skip('x')", out)
        self.assertIn("1 justified finding(s)", out.splitlines()[-1])

    def test_bad_reason_lines_do_not_count(self):
        for message in (
            "Park\n\nCheck-change: too short\n",
            f"Park\n\n  {REASON}\n",
            f"Park\n\nSee: {REASON}\n",
            "Park\n\ncheck-change: lower case does not count either\n",
        ):
            with self.subTest(message=message):
                self.repo.git("reset", "-q", "--hard", "main~0")
                self.repo.commit(message, tests__test_thing_py=self.skipped)
                code, _, _ = self.repo.scan("--commit", "HEAD")
                self.assertEqual(code, 1)
                self.repo.git("reset", "-q", "--hard", "HEAD~1")

    def test_a_reason_of_twenty_characters_counts_and_one_of_nineteen_does_not(self):
        cases = (
            ("Check-change: " + "x" * 20, 0),
            ("Check-change: " + "x" * 19, 1),
            ("Check-change:   " + "x" * 20 + "   ", 0),
            ("Check-change:   " + "x" * 19 + "   ", 1),
            ("Check-change:" + "x" * 20, 0),
            ("Check-change:" + " " * 20, 1),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                self.repo.commit(f"Park\n\n{line}\n", tests__test_thing_py=self.skipped)
                code, out, _ = self.repo.scan("--commit", "HEAD")
                self.assertEqual(code, expected, out)
                self.repo.git("reset", "-q", "--hard", "HEAD~1")

    def test_staged_with_message_file(self):
        self.repo.write("tests/test_thing.py", self.skipped)
        self.repo.git("add", "-A")
        folder = Path(self._tmp.name)
        good = folder.parent / f"{folder.name}-good-msg"
        comment = folder.parent / f"{folder.name}-comment-msg"
        empty = folder.parent / f"{folder.name}-empty-msg"
        good.write_text(f"Park a test\n\n{REASON}\n# Please enter the commit message\n")
        comment.write_text(f"Park a test\n\n# {REASON}\n")
        empty.write_text("")
        for path in (good, comment, empty):
            self.addCleanup(path.unlink)
        cases = [
            ((), 1),
            (("--message-file", str(good)), 0),
            (("--message-file", str(comment)), 1),
            (("--message-file", str(empty)), 1),
            (("--message-file", str(folder.parent / "no-such-message-file")), 1),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                code, out, err = self.repo.scan("--staged", *extra)
                self.assertEqual(code, expected, out + err)
                self.assertIn("staged tests/test_thing.py:7 skip-added", out)

    def test_scissors_line_ends_the_message(self):
        message = "Park\n# ------------------------ >8 ------------------------\n" + REASON + "\n"
        self.assertFalse(ci.justified(message))
        self.assertTrue(ci.justified(f"Park\n\n{REASON}\r\n"))


class DiffEdgeCases(RepoCase):
    def test_line_numbers_over_several_hunks(self):
        body = "".join(f"x{n} = {n}\n" for n in range(1, 21))
        self.repo.commit(src__m_py=body)
        lines = body.splitlines(keepends=True)
        lines.insert(2, "a = f()  # type: ignore\n")
        lines.insert(16, "b = g()  # type: ignore\n")
        self.repo.commit(src__m_py="".join(lines))
        self.assertEqual(
            self.rules_at_head(),
            {("src/m.py", 3, "suppression-added", False), ("src/m.py", 17, "suppression-added", False)},
        )

    def test_plus_plus_content_and_file_header(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            src__loop_c="int f(int i) {\n++i; // NOLINT\n}\n",
            **{"web__t.only(1).test.js": "const ok = 1\n"},
        )
        self.assertEqual(self.rules_at_head(), {("src/loop.c", 2, "suppression-added", False)})

    def test_space_and_non_ascii_names(self):
        self.repo.commit(**{"tests__my tést ü.py": "def test_a():\n    assert 1\n"})
        self.repo.commit(**{"tests__my tést ü.py": "@unittest.skip('x')\ndef test_a():\n    assert 1\n"})
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1)
        self.assertIn(" tests/my tést ü.py:1 skip-added", out)

    def test_binary_files(self):
        blob = b"\x89PNG\r\n\x1a\n\x00\x00assert\x00skip(\xff\xfe"
        self.repo.commit(tests__fixtures__img_png=blob, README_md="seed\n")
        self.repo.commit(tests__fixtures__img_png=blob + b"\x00more")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 1 files)"))
        self.repo.commit(tests__fixtures__img_png=None)
        self.assertEqual(
            self.rules_at_head(), {("tests/fixtures/img.png", 0, "test-removed", False)}
        )

    def test_rename_is_one_change_not_a_deletion(self):
        self.repo.commit(tests__test_old_py=BASE_TEST)
        self.repo.git("mv", "tests/test_old.py", "tests/test_new.py")
        self.repo.commit("Rename")
        code, report = self.head_json()
        self.assertEqual((code, report["files"], report["findings"]), (0, 1, []))

    def test_non_utf8_content_is_still_scanned(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            src__latin_py=b"name = '\xe9t\xe9'  # type: ignore\n",
            src__other_py="y = 1  # noqa\n",
        )
        found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
        self.assertEqual(
            found, {("src/latin.py", "suppression-added"), ("src/other.py", "suppression-added")}
        )

    def test_root_commit(self):
        self.repo.commit(src__a_py="x = f()  # type: ignore\n")
        self.assertEqual(self.rules_at_head(), {("src/a.py", 1, "suppression-added", False)})

    def test_one_line_hitting_two_rules_reports_both(self):
        self.repo.commit(tests__test_a_py="def test_a():\n    assert 1\n")
        self.repo.commit(tests__test_a_py="@unittest.skip('x')  # noqa\ndef test_a():\n    assert 1\n")
        self.assertEqual(
            self.rules_at_head(),
            {("tests/test_a.py", 1, "skip-added", False), ("tests/test_a.py", 1, "suppression-added", False)},
        )

    def test_parser_rejects_broken_patches(self):
        for patch in (
            b"@@ -1 +1,2 @@\n+only one line\n",
            b"diff --git a/x b/x\n+stray\n@@ -0,0 +1 @@\n+a\n",
            b"@@ -0,0 +1 @@\n+a\nnot a hunk\n",
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(ci.IntegrityError):
                    ci.parse_patch("x", patch, deleted=False)

    # The two patches below are of one change, as git prints them for -U0 and -U3.
    WITH_CONTEXT = (
        b"@@ -1,4 +1,5 @@\n def test_a():\n+    return\n     x = 1\n-    y = 2\n+    y = 3\n     z = 4\n"
    )
    WITHOUT_CONTEXT = b"@@ -1,0 +2 @@\n+    return\n@@ -3 +4 @@\n-    y = 2\n+    y = 3\n"

    def test_the_context_of_a_patch_changes_neither_line_numbers_nor_hunks(self):
        shape = lambda diff: (
            [(l.number, l.text, l.hunk) for l in diff.added],
            [(l.number, l.text, l.hunk) for l in diff.removed],
        )
        with_context = ci.parse_patch("x", self.WITH_CONTEXT)
        self.assertEqual(shape(with_context), shape(ci.parse_patch("x", self.WITHOUT_CONTEXT)))
        self.assertEqual(shape(with_context), ([(2, "    return", 1), (4, "    y = 3", 2)], [(4, "    y = 2", 2)]))

    def test_an_added_line_knows_the_line_above_it_that_is_not_blank_or_a_comment(self):
        patch = (
            b"@@ -1,4 +1,7 @@\n def test_a():\n     # why\n+\n+    # more\n+    return\n     x = 1\n     y = 2\n"
        )
        before = lambda diff: [l.before for l in diff.added]
        self.assertEqual(before(ci.parse_patch("x", patch)), ["def test_a():"] * 3)
        self.assertEqual(before(ci.parse_patch("x", self.WITH_CONTEXT)), ["def test_a():", "    x = 1"])
        self.assertEqual(before(ci.parse_patch("x", self.WITHOUT_CONTEXT)), [None, None])


def skipped(text, name="test_one"):
    return text.replace(f"    def {name}", f"    @unittest.skip('x')\n    def {name}")


MERGE_ATTRIBUTES = ("-merge", "merge=binary")


def git_without_attr_source(case):
    """Put a git first on the PATH of case.repo that refuses --attr-source as an unknown option,
    the way a git older than 2.41 does, and passes everything else to the real git."""
    shim = Path(case._tmp.name).parent / f"{Path(case._tmp.name).name}-noattrsource"
    shim.mkdir()
    case.addCleanup(lambda: shutil.rmtree(shim, ignore_errors=True))
    (shim / "git").write_text(
        '#!/bin/sh\nfor arg in "$@"; do\n'
        '  case "$arg" in --attr-source*) echo "unknown option: $arg" >&2; exit 129 ;; esac\n'
        f'done\nexec {shlex.quote(shutil.which("git"))} "$@"\n'
    )
    (shim / "git").chmod(0o755)
    case.repo.env["PATH"] = f"{shim}{os.pathsep}{case.repo.env['PATH']}"


# The judged branch adds a test of its own at the end of the file; the other side adds an
# assertion to test_one. Both sides changed the file, so a merge attribute decides the merge.
ASSERTED_TEST = BASE_TEST.replace(
    "        self.assertEqual(f(), 1)\n", "        self.assertEqual(f(), 1)\n        self.assertTrue(f())\n"
)
EXTRA_TEST = BASE_TEST + "\n    def test_three(self):\n        assert 3 == 3\n"


class MergeCommits(RepoCase):
    """A merge commit is judged by what the merge changed beyond the automatic merge
    (git show --remerge-diff), not by everything the merged-in side brought."""

    def setUp(self):
        super().setUp()
        self.base = self.repo.commit(
            "Seed", README_md="seed\n", src__a_py="a = 1\n", tests__test_s_py=BASE_TEST, tests__test_t_py=BASE_TEST
        )

    def branch_off(self, name="side"):
        self.repo.git("checkout", "-q", "-b", name, self.base)

    def merge(self, branch="side", message="Merge side", **resolution):
        """Merge ``branch`` into main; a conflict is resolved with the given files."""
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", branch, check=False)
        return self.repo.commit(message, **resolution)

    def conflicting_sides(self, path="tests__test_s_py", text=BASE_TEST):
        """Two branches that change the same line of one file differently."""
        line = "        self.assertEqual(f(), 1)\n"
        self.branch_off()
        self.repo.commit("Side", **{path: text.replace(line, line.rstrip() + "  # side\n")})
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Main", **{path: text.replace(line, line.rstrip() + "  # main\n")})

    def test_a_clean_merge_has_no_findings_whatever_the_merged_side_brought(self):
        self.branch_off()
        self.repo.commit(f"Park a test\n\n{REASON}", tests__test_s_py=skipped(BASE_TEST))
        self.repo.git("checkout", "-q", "main")
        self.repo.commit(f"Park another\n\n{REASON}", tests__test_t_py=skipped(BASE_TEST))
        self.merge()
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 0 files)"), err)
        code, out, err = self.repo.scan("--range", f"{self.base}..HEAD")
        self.assertEqual(code, 0, out + err)
        self.assertIn("3 commits", out)
        self.assertIn("2 justified finding(s)", out)

    def test_an_unjustified_commit_on_the_merged_side_is_still_reported_once_on_its_own_commit(self):
        self.branch_off()
        bad = self.repo.commit("Park a test", tests__test_s_py=skipped(BASE_TEST))
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Touch readme", README_md="seed 2\n")
        self.merge()
        code, out, _ = self.repo.scan("--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertEqual([line.split()[0] for line in out.splitlines() if "skip-added" in line], [bad[:7]])

    def test_a_skip_added_while_resolving_a_conflict_is_reported_on_the_merge(self):
        self.conflicting_sides()
        resolved = skipped(BASE_TEST).replace(
            "        self.assertEqual(f(), 1)\n",
            "        self.assertEqual(f(), 1)  # side\n        self.assertEqual(f(), 1)  # main\n",
        )
        merge = self.merge(tests__test_s_py=resolved)
        code, report = self.head_json()
        found = {(f["where"], f["path"], f["rule"], f["justified"]) for f in report["findings"]}
        self.assertEqual((code, found), (1, {(merge[:7], "tests/test_s.py", "skip-added", False)}))
        self.repo.git("commit", "-q", "--amend", "-m", f"Merge side\n\n{REASON}")
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 justified finding(s)", out)

    def test_conflict_markers_that_resolution_removes_are_not_findings(self):
        self.repo.commit("Add the workflow", **{".github__workflows__ci_yml": "steps:\n  - run: make test\n"})
        self.base = self.repo.git("rev-parse", "HEAD")
        self.branch_off()
        self.repo.commit("Side", **{".github__workflows__ci_yml": "steps:\n  - run: make test-a\n"})
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Main", **{".github__workflows__ci_yml": "steps:\n  - run: make test-b\n"})
        # the merging branch's line comes first, as in the conflicted file
        self.merge(**{".github__workflows__ci_yml": "steps:\n  - run: make test-b\n  - run: make test-a\n"})
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 1 files)"), err)

    def test_dropping_a_line_of_a_check_file_while_resolving_is_still_reported(self):
        self.repo.commit("Add the workflow", **{".github__workflows__ci_yml": "steps:\n  - run: make test\n"})
        self.base = self.repo.git("rev-parse", "HEAD")
        self.branch_off()
        self.repo.commit("Side", **{".github__workflows__ci_yml": "steps:\n  - run: make test-a\n"})
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Main", **{".github__workflows__ci_yml": "steps:\n  - run: make test-b\n"})
        self.merge(**{".github__workflows__ci_yml": "steps:\n  - run: make test-a\n"})
        self.assertEqual(self.rule_names(), {"check-file-weakened", "check-machinery-changed"})

    def test_an_octopus_merge_is_an_unreadable_change_not_a_pass_and_not_a_crash(self):
        for name in ("one", "two"):
            self.branch_off(name)
            self.repo.commit(f"Add {name}", **{f"src__{name}_py": "x = 1\n"})
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Octopus", "one", "two")
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1, out + err)
        self.assertIn("(octopus merge):0 unreadable-change", out)
        self.assertIn("octopus", out)
        self.repo.git("commit", "-q", "--amend", "-m", f"Octopus\n\n{REASON}")
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 justified finding(s)", out)

    def test_a_git_without_remerge_diff_exits_2_and_names_the_version(self):
        self.branch_off()
        self.repo.commit("Side", src__b_py="b = 1\n")
        self.merge()
        shim = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-oldgit"
        shim.mkdir()
        self.addCleanup(lambda: shutil.rmtree(shim, ignore_errors=True))
        (shim / "git").write_text(
            '#!/bin/sh\nfor arg in "$@"; do\n'
            '  if [ "$arg" = "--version" ]; then echo "git version 2.30.0"; exit 0; fi\n'
            f'done\nexec {shlex.quote(shutil.which("git"))} "$@"\n'
        )
        (shim / "git").chmod(0o755)
        self.repo.env["PATH"] = f"{shim}{os.pathsep}{self.repo.env['PATH']}"
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 2, out)
        self.assertIn("--remerge-diff", err)
        self.assertIn("2.36", err)
        code, _, _ = self.repo.scan("--commit", "HEAD^2")  # the side commit: one parent
        self.assertEqual(code, 0, "a commit with one parent needs no remerge-diff")

    def test_a_merge_attribute_the_change_brings_cannot_hide_an_assertion_the_resolution_drops(self):
        """In CI the working tree is the judged change's head. A .gitattributes there that keeps
        one side of a file (-merge, merge=binary) must not shape the automatic merge the result is
        compared with: the resolution below drops main's assertion and must be reported."""
        for attribute in MERGE_ATTRIBUTES:
            with self.subTest(attribute=attribute):
                self.setUp()
                self.repo.commit("Assert more", tests__test_s_py=ASSERTED_TEST)
                self.branch_off("topic")
                self.repo.commit(
                    "Keep the tests of this branch",
                    tests___gitattributes=f"test_s.py {attribute}\n",
                    tests__test_s_py=EXTRA_TEST,
                )
                self.repo.git("merge", "-q", "--no-ff", "--no-commit", "main", check=False)
                self.repo.commit("Merge main", tests__test_s_py=EXTRA_TEST)
                self.assertTrue((self.repo.root / "tests" / ".gitattributes").exists(), "the working tree has it")
                code, report = self.head_json()
                found = {(f["path"], f["rule"], f["justified"]) for f in report["findings"]}
                self.assertEqual((code, found), (1, {("tests/test_s.py", "assertion-loss", False)}))

    def test_a_git_without_attr_source_exits_2_on_a_merge_and_names_the_option(self):
        self.branch_off()
        self.repo.commit("Side", src__b_py="b = 1\n")
        self.merge()
        git_without_attr_source(self)
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 2, out + err)
        self.assertIn("--attr-source", err)
        self.assertIn("2.41", err)  # the version check speaks, not git's own unknown-option error
        code, out, err = self.repo.scan("--commit", "HEAD^2")  # the side commit: one parent
        self.assertEqual(code, 0, "a commit with one parent merges nothing:\n" + out + err)


class StagedMerge(RepoCase):
    """While a merge waits to be committed (MERGE_HEAD exists), --staged judges what the
    resolution changed beyond the automatic merge, as --commit does for the merge commit."""

    CI = ".github__workflows__ci_yml"

    def setUp(self):
        super().setUp()
        self.base = self.repo.commit(
            "Seed", README_md="seed\n", src__a_py="a = 1\n", tests__test_s_py=BASE_TEST, tests__test_t_py=BASE_TEST
        )

    def start_merge(self, *branches):
        """Check out main and merge the branches without committing; a conflict stays open."""
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", *branches, check=False)

    def side(self, name="side", message=None, **files):
        self.repo.git("checkout", "-q", "-b", name, self.base)
        self.repo.commit(message or f"Work on {name}", **files)
        self.repo.git("checkout", "-q", "main")

    def conflicting_workflow(self):
        self.repo.commit("Add the workflow", **{self.CI: "steps:\n  - run: make test\n"})
        self.base = self.repo.git("rev-parse", "HEAD")
        self.side(**{self.CI: "steps:\n  - run: make test-a\n"})
        self.repo.commit("Main", **{self.CI: "steps:\n  - run: make test-b\n"})

    def staged(self, *extra, cwd=None):
        code, out, err = self.repo.scan("--staged", "--json", *extra, cwd=cwd)
        self.assertIn(code, (0, 1, 2), out + err)
        if code == 2:
            return code, err
        return code, {(f["path"], f["rule"], f["justified"]) for f in json.loads(out)["findings"]}

    def test_a_clean_merge_is_not_judged_by_what_the_merged_side_brought(self):
        self.side(message=f"Park a test\n\n{REASON}", tests__test_s_py=skipped(BASE_TEST))
        self.repo.commit(f"Park another\n\n{REASON}", tests__test_t_py=skipped(BASE_TEST))
        self.start_merge("side")
        self.assertTrue((self.repo.root / ".git" / "MERGE_HEAD").exists())
        code, out, err = self.repo.scan("--staged")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (staged, 0 files)"), err)

    def test_a_skip_added_while_resolving_a_conflict_is_a_finding_and_a_reason_line_clears_it(self):
        line = "        self.assertEqual(f(), 1)\n"
        self.side(tests__test_s_py=BASE_TEST.replace(line, line.rstrip() + "  # side\n"))
        self.repo.commit("Main", tests__test_s_py=BASE_TEST.replace(line, line.rstrip() + "  # main\n"))
        self.start_merge("side")
        self.repo.write(
            "tests/test_s.py",
            skipped(BASE_TEST).replace(line, line.rstrip() + "  # side\n" + line.rstrip() + "  # main\n"),
        )
        self.repo.git("add", "-A")
        self.assertEqual(self.staged(), (1, {("tests/test_s.py", "skip-added", False)}))
        message = Path(self._tmp.name) / "message.txt"
        message.write_text(f"Merge side\n\n{REASON}\n", encoding="utf-8")
        self.assertEqual(self.staged("--message-file", str(message)), (0, {("tests/test_s.py", "skip-added", True)}))

    def test_conflict_markers_that_the_resolution_removes_are_not_findings(self):
        self.conflicting_workflow()
        self.start_merge("side")
        self.repo.write(".github/workflows/ci.yml", "steps:\n  - run: make test-b\n  - run: make test-a\n")
        self.repo.git("add", "-A")
        code, out, err = self.repo.scan("--staged")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (staged, 1 files)"), err)

    def test_dropping_a_line_of_a_check_file_while_resolving_is_still_reported(self):
        self.conflicting_workflow()
        self.start_merge("side")
        self.repo.write(".github/workflows/ci.yml", "steps:\n  - run: make test-a\n")
        self.repo.git("add", "-A")
        found = self.staged()
        self.assertEqual(found[0], 1)
        self.assertEqual({rule for _, rule, _ in found[1]}, {"check-file-weakened", "check-machinery-changed"})

    def test_the_verdict_does_not_depend_on_the_conflict_style_of_the_user(self):
        for style in ("merge", "diff3", "zdiff3"):
            with self.subTest(style=style):
                self.setUp()
                self.repo.git("config", "merge.conflictStyle", style)
                self.conflicting_workflow()
                self.start_merge("side")
                self.repo.write(".github/workflows/ci.yml", "steps:\n  - run: make test-b\n  - run: make test-a\n")
                self.repo.git("add", "-A")
                code, out, err = self.repo.scan("--staged")
                self.assertEqual((code, out.strip()), (0, "check-integrity: ok (staged, 1 files)"), err)

    def test_a_merge_without_a_common_ancestor_is_judged_too(self):
        self.repo.git("checkout", "-q", "--orphan", "other")
        self.repo.git("rm", "-rfq", ".")
        self.repo.commit("Unrelated", other_txt="other\n")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "--allow-unrelated-histories", "other")
        code, out, err = self.repo.scan("--staged")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (staged, 0 files)"), err)

    def test_an_octopus_merge_in_progress_is_an_unreadable_change(self):
        self.side("one", src__one_py="x = 1\n")
        self.side("two", src__two_py="x = 1\n")
        self.start_merge("one", "two")
        code, out, err = self.repo.scan("--staged")
        self.assertEqual(code, 1, out + err)
        self.assertIn("(octopus merge):0 unreadable-change", out)
        message = Path(self._tmp.name) / "message.txt"
        message.write_text(f"Octopus\n\n{REASON}\n", encoding="utf-8")
        code, out, err = self.repo.scan("--staged", "--message-file", str(message))
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 justified finding(s)", out)

    def test_it_works_from_a_subfolder_and_from_a_linked_worktree(self):
        self.side(message=f"Park a test\n\n{REASON}", tests__test_s_py=skipped(BASE_TEST))
        self.repo.commit("Touch readme", README_md="seed 2\n")
        self.start_merge("side")
        self.assertEqual(self.staged(cwd=self.repo.root / "tests"), (0, set()))
        self.repo.git("merge", "--abort")
        linked = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-linked"
        self.repo.git("worktree", "add", "-q", "-b", "wt", str(linked), "main")
        self.addCleanup(lambda: shutil.rmtree(linked, ignore_errors=True))
        self.repo.git("-C", str(linked), "merge", "-q", "--no-ff", "--no-commit", "side")
        self.assertEqual(self.staged(cwd=linked), (0, set()))

    def test_a_staged_change_without_a_merge_in_progress_is_judged_against_head_as_before(self):
        self.side(message=f"Park a test\n\n{REASON}", tests__test_s_py=skipped(BASE_TEST))
        self.repo.write("tests/test_t.py", skipped(BASE_TEST))
        self.repo.git("add", "-A")
        self.assertEqual(self.staged(), (1, {("tests/test_t.py", "skip-added", False)}))

    def test_a_git_without_merge_tree_write_tree_exits_2_and_names_the_version(self):
        self.side(src__b_py="b = 1\n")
        self.start_merge("side")
        shim = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-oldgit"
        shim.mkdir()
        self.addCleanup(lambda: shutil.rmtree(shim, ignore_errors=True))
        (shim / "git").write_text(
            '#!/bin/sh\nfor arg in "$@"; do\n'
            '  if [ "$arg" = "--version" ]; then echo "git version 2.37.0"; exit 0; fi\n'
            f'done\nexec {shlex.quote(shutil.which("git"))} "$@"\n'
        )
        (shim / "git").chmod(0o755)
        self.repo.env["PATH"] = f"{shim}{os.pathsep}{self.repo.env['PATH']}"
        code, out, err = self.repo.scan("--staged")
        self.assertEqual(code, 2, out)
        self.assertIn("merge-tree", err)
        self.assertIn("2.38", err)

    def test_an_unreadable_merge_head_exits_2_instead_of_passing(self):
        self.side(src__b_py="b = 1\n")
        self.start_merge("side")
        (self.repo.root / ".git" / "MERGE_HEAD").write_text("not a commit id\n")
        code, out, err = self.repo.scan("--staged")
        self.assertEqual(code, 2, out)
        self.assertIn("MERGE_HEAD", err)

    def test_a_merge_attribute_in_the_working_tree_cannot_hide_an_assertion_the_resolution_drops(self):
        for attribute in MERGE_ATTRIBUTES:
            with self.subTest(attribute=attribute):
                self.setUp()
                self.repo.commit("Assert more", tests__test_s_py=ASSERTED_TEST)
                self.repo.git("checkout", "-q", "-b", "topic", self.base)
                self.repo.commit(
                    "Keep the tests of this branch",
                    tests___gitattributes=f"test_s.py {attribute}\n",
                    tests__test_s_py=EXTRA_TEST,
                )
                self.repo.git("merge", "-q", "--no-ff", "--no-commit", "main", check=False)
                self.repo.write("tests/test_s.py", EXTRA_TEST)
                self.repo.git("add", "-A")
                self.assertEqual(self.staged(), (1, {("tests/test_s.py", "assertion-loss", False)}))

    def test_a_git_without_attr_source_exits_2_on_a_merge_in_progress(self):
        self.side(src__b_py="b = 1\n")
        self.start_merge("side")
        git_without_attr_source(self)
        code, out, err = self.repo.scan("--staged")
        self.assertEqual(code, 2, out + err)
        self.assertIn("--attr-source", err)
        self.assertIn("2.41", err)  # the version check speaks, not git's own unknown-option error


class RangeAndErrors(RepoCase):
    def test_range_pairs_each_commit_with_its_message(self):
        base = self.repo.commit(tests__test_a_py=BASE_TEST)
        first = self.repo.commit(
            f"Park\n\n{REASON}\n", tests__test_a_py=BASE_TEST.replace("assert 2 == 2", "pass")
        )
        skipped = BASE_TEST.replace("assert 2 == 2", "pass").replace(
            "    def test_two", "    @unittest.skip('x')\n    def test_two"
        )
        second = self.repo.commit(tests__test_a_py=skipped)
        code, out, _ = self.repo.scan("--range", f"{base}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn(f"{first[:7]} tests/test_a.py:8 assertion-loss justified", out)
        self.assertIn(f"{second[:7]} tests/test_a.py:7 skip-added  @unittest.skip('x')", out)
        self.assertIn("1 finding(s) without justification (2 commits, 2 files)", out)

    def test_empty_range_is_ok(self):
        self.repo.commit(README_md="seed\n")
        code, out, _ = self.repo.scan("--range", "HEAD..HEAD")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (0 commits, 0 files)"))

    def test_errors_exit_2(self):
        self.repo.commit(README_md="seed\n")
        broken = Path(self._tmp.name) / "broken.json"
        broken.write_text("{not json")
        cases = [
            ("--commit", "no-such-rev"),
            ("--range", "HEAD"),
            ("--commit", "HEAD", "--rules", str(broken)),
            ("--commit", "HEAD", "--message-file", "x"),
            (),
            ("--staged", "--commit", "HEAD"),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, out, err = self.repo.scan(*args)
                self.assertEqual(code, 2, out + err)
                self.assertTrue(err.strip(), "an error must say why")

    def test_outside_a_repository_exits_2(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        done = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--staged", "--json"],
            cwd=outside.name,
            env=isolated_env(),
            capture_output=True,
            check=False,
        )
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stdout)["status"], "error")

    def test_rules_file_shapes_are_validated(self):
        bad = Path(self._tmp.name) / "bad.json"
        for document in (
            {"version": 1, "classes": {}, "rules": []},
            {"version": 1, "classes": {"t": ["x"]}, "rules": [{"id": "a", "kind": "odd", "classes": ["t"], "why": "w", "patterns": ["x"]}]},
            {"version": 1, "classes": {"t": ["x"]}, "rules": [{"id": "a", "kind": "added", "classes": ["nope"], "why": "w", "patterns": ["x"]}]},
            {"version": 1, "classes": {"t": ["("]}, "rules": [{"id": "a", "kind": "deleted", "classes": ["t"], "why": "w"}]},
        ):
            with self.subTest(document=document):
                bad.write_text(json.dumps(document))
                with self.assertRaises(ci.IntegrityError):
                    ci.load_rules(bad)
        self.assertTrue(ci.load_rules(RULES).rules)

class NarrowedRules(RepoCase):
    """Each narrowing of the rules: a case that used to be reported and no longer is, and a
    real weakening of the same kind that is still reported."""

    def findings(self):
        return {(path, rule) for path, _, rule, _ in self.rules_at_head()}

    # skip-added: only it/test/describe/context/suite count as the owner of .skip and friends

    def test_helper_methods_named_fails_or_only_are_green(self):
        self.repo.commit(tests__test_cli_py=BASE_TEST)
        self.repo.commit(
            tests__test_cli_py=BASE_TEST
            + "\n    def test_three(self):\n        self.fails('usage', code=2)\n        self.only(doc, 'E1')\n"
        )
        self.assertNotIn("skip-added", self.rule_names())

    def test_suite_and_context_skips_are_red(self):
        self.repo.commit(**{"web/a.test.ts": "// seed\n", "web/b.test.ts": "// seed\n"})
        self.repo.commit(
            **{
                "web/a.test.ts": "// seed\nsuite.skip('slow', () => {})\n",
                "web/b.test.ts": "// seed\ncontext.only('one', () => {})\n",
            }
        )
        self.assertEqual(
            self.findings(), {("web/a.test.ts", "skip-added"), ("web/b.test.ts", "skip-added")}
        )

    # skip-added: a new test file cannot weaken a check that existed before

    def test_skip_in_new_test_file_is_green(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            tests__test_new_py="@unittest.skipUnless(HAS_GPU, 'needs a GPU')\ndef test_a():\n    assert 1\n"
        )
        self.assertNotIn("skip-added", self.rule_names())

    def test_skip_added_to_existing_test_file_is_red(self):
        self.repo.commit(tests__test_old_py="def test_a():\n    assert 1\n")
        self.repo.commit(
            tests__test_old_py="@unittest.skipUnless(HAS_GPU, 'needs a GPU')\ndef test_a():\n    assert 1\n"
        )
        self.assertEqual(self.findings(), {("tests/test_old.py", "skip-added")})

    # suppression-added: F401, E402 and PLC0415 on an import line are allowed

    def test_import_noqa_with_import_codes_is_green(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            src__a_py="from pkg import plugin  # noqa: F401\n",
            src__b_py="import late  # noqa: PLC0415\n",
            src__c_py="from . import (  # noqa: E402, F401\n    thing,\n)\n",
            src__d_py="import extra  # noqa: F401 registers the plugin\n",
        )
        self.assertNotIn("suppression-added", self.rule_names())

    def test_other_noqa_shapes_are_red(self):
        self.repo.commit(README_md="seed\n")
        samples = {
            "src/a.py": "value = 1  # noqa: F401\n",
            "src/b.py": "import os  # noqa\n",
            "src/c.py": "import os  # noqa: F811\n",
            "src/d.py": "import os  # noqa: F401, BLE001\n",
            "src/e.py": "import os  # noqa: F401  # type: ignore\n",
        }
        for path, line in samples.items():
            self.repo.write(path, line)
        self.repo.commit()
        self.assertEqual(self.findings(), {(path, "suppression-added") for path in samples})

    # suppression-added: a marker the file also loses on a removed line is not new

    def test_changed_or_moved_suppressed_line_is_green(self):
        self.repo.commit(
            src__m_py="from lib import a  # pyright: ignore[reportMissingImports]\n"
            "x = 1\ny = g()  # type: ignore[arg-type]\nz = 3\n"
        )
        self.repo.commit(
            src__m_py="from lib import a, b  # pyright: ignore[reportMissingImports]\n"
            "x = 1\nz = 3\ny = g()  # type: ignore[arg-type]\n"
        )
        self.assertNotIn("suppression-added", self.rule_names())

    def test_net_gain_of_markers_is_reported_once(self):
        self.repo.commit(src__m_py="a = f()  # type: ignore\nb = 2\nc = 3\n")
        self.repo.commit(src__m_py="a = f()\nb = g()  # type: ignore\nc = h()  # type: ignore\n")
        self.assertEqual(
            [(p, rule) for p, _, rule, _ in self.rules_at_head()], [("src/m.py", "suppression-added")]
        )

    def test_widened_marker_and_marker_moved_to_another_file_are_red(self):
        self.repo.commit(src__a_py="y = g()  # type: ignore[arg-type]\n", src__b_py="z = 1\n")
        self.repo.commit(src__a_py="y = g()  # type: ignore\n")
        self.assertEqual(self.findings(), {("src/a.py", "suppression-added")})
        self.repo.commit(src__a_py="y = g()\n", src__b_py="z = h()  # type: ignore\n")
        self.assertEqual(self.findings(), {("src/b.py", "suppression-added")})

    # suppression-added: a reason written next to the marker does not stand in for Check-change

    def test_inline_reason_does_not_release_a_marker(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            src__a_py="x = f()  # type: ignore  # the upstream stub is wrong, see the tracker\n",
            **{"web/b.tsx": "// eslint-disable-next-line no-img-element -- remote images need a plain tag\n"},
        )
        self.assertEqual(
            self.findings(), {("src/a.py", "suppression-added"), ("web/b.tsx", "suppression-added")}
        )

    # check-file-weakened: a line rewritten in place counts only if it drops a word

    def test_reworded_or_widened_check_lines_are_not_weakened_but_still_change_the_machinery(self):
        self.repo.commit(
            tools__privacy_rules_json='{"why": "a private address"}\n',
            _github__workflows__ci_yml="jobs:\n  gate:\n    needs: [lint, test]\n",
            pyproject_toml='[tool.ruff.lint]\nselect = ["E9", "F"]\n',
        )
        self.repo.commit(
            tools__privacy_rules_json='{"why": "a private or link-local address"}\n',
            _github__workflows__ci_yml="jobs:\n  gate:\n    needs: [lint, test, chat]\n",
            pyproject_toml='[tool.ruff.lint]\nselect = ["E9", "F", "B"]\n',
        )
        self.assertEqual(
            {(path, rule) for path, _, rule, _ in self.rules_at_head()},
            {
                (".github/workflows/ci.yml", "check-machinery-changed"),
                ("tools/privacy_rules.json", "check-machinery-changed"),
            },
        )

    def test_narrowed_check_lines_are_red(self):
        self.repo.commit(
            pyproject_toml='[tool.ruff.lint]\nselect = ["E", "F"]\n',
            _github__workflows__ci_yml="jobs:\n  test:\n    if: ${{ needs.changes.outputs.python == 'true' }}\n"
            "    runs-on: ubuntu-24.04\n",
        )
        self.repo.commit(
            pyproject_toml='[tool.ruff.lint]\nselect = ["E"]\n',
            _github__workflows__ci_yml="jobs:\n  test:\n    if: ${{ github.event_name == 'schedule' && "
            "needs.changes.outputs.python == 'true' }}\n    runs-on: ubuntu-24.04\n",
        )
        found = {(path, line, rule) for path, line, rule, _ in self.rules_at_head()}
        self.assertEqual(
            found,
            {
                ("pyproject.toml", 2, "check-file-weakened"),
                (".github/workflows/ci.yml", 3, "check-file-weakened"),
                (".github/workflows/ci.yml", 3, "check-machinery-changed"),
            },
        )

    # check-file-weakened: a loosening line in a mixed config counts only if the file existed

    def test_new_config_with_loose_setting_is_green(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(tsconfig_json='{\n  "compilerOptions": {\n    "skipLibCheck": true\n  }\n}\n')
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_loose_setting_added_to_existing_config_is_red(self):
        self.repo.commit(tsconfig_json='{\n  "compilerOptions": {\n    "strict": true\n  }\n}\n')
        self.repo.commit(
            tsconfig_json='{\n  "compilerOptions": {\n    "strict": true,\n    "skipLibCheck": true\n  }\n}\n'
        )
        self.assertIn(("tsconfig.json", "check-file-weakened"), self.findings())

    # bypass-added: hooks and CI keep every pattern; other scripts only a swallowed check

    def test_plain_script_idioms_are_green(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(
            scripts__install_sh="set +e\nlaunch stop agent || true\nrm -f x.lock || :\n",
            bin__fetch="curl -fsS example.invalid/x || exit 0\n",
            Makefile="clean:\n\trm -rf build || true\n",
            package_json='{"scripts": {"commit": "HUSKY=0 git commit"}}\n',
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_swallowed_check_in_script_and_any_bypass_in_hook_are_red(self):
        self.repo.commit(README_md="seed\n")
        samples = {
            "scripts/check.sh": "uv run mypy src || exit 0\n",
            "Makefile": "lint:\n\tcargo test --all || :\n",
            "bin/verify": "go vet ./... || true\n",
            ".githooks/pre-commit": "make lint || true\n",
            ".pre-commit-config.yaml": "      entry: sh -c 'set +e; make lint'\n",
        }
        for path, line in samples.items():
            self.repo.write(path, line)
        self.repo.commit()
        machinery = {".githooks/pre-commit", ".pre-commit-config.yaml"}
        self.assertEqual(
            self.findings(),
            {(path, "bypass-added") for path in samples} | {(path, "check-machinery-changed") for path in machinery},
        )

    # test-removed and assertion-loss: counted over all test files of the commit

    def test_tests_moved_between_files_are_green(self):
        two = "def test_a():\n    assert f() == 1\n\ndef test_b():\n    assert f() == 2\n"
        self.repo.commit(tests__test_one_py=two, tests__test_two_py="def test_c():\n    assert 1\n")
        self.repo.commit(
            tests__test_one_py="def test_a():\n    assert f() == 1\n",
            tests__test_two_py="def test_c():\n    assert 1\n\ndef test_b():\n    assert f() == 2\n",
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_renamed_test_file_is_green_but_tests_dropped_on_the_way_are_red(self):
        self.repo.commit(tests__test_old_py=BASE_TEST)
        self.repo.git("mv", "tests/test_old.py", "tests/test_new.py")
        self.repo.commit("Rename")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.repo.git("mv", "tests/test_new.py", "tests/test_newer.py")
        self.repo.write("tests/test_newer.py", BASE_TEST.replace("        assert 2 == 2\n", "        pass\n"))
        self.repo.commit("Rename and drop an assertion")
        self.assertEqual(self.findings(), {("tests/test_newer.py", "assertion-loss")})

    def test_net_loss_with_no_test_added_elsewhere_is_red(self):
        self.repo.commit(tests__test_a_py=BASE_TEST, tests__test_b_py="def test_c():\n    assert 1\n")
        self.repo.commit(
            tests__test_a_py=BASE_TEST.replace("\n    def test_two(self):\n        assert 2 == 2\n", ""),
            tests__test_b_py="def test_c():\n    assert 1\n# touched\n",
        )
        self.assertEqual(
            self.findings(), {("tests/test_a.py", "test-removed"), ("tests/test_a.py", "assertion-loss")}
        )

    def test_test_file_moved_out_of_the_tests_is_red(self):
        self.repo.commit(tests__test_a_py=BASE_TEST)
        self.repo.git("mv", "tests/test_a.py", "notes.txt")
        self.repo.commit("Park")
        self.assertEqual(self.rules_at_head(), {("notes.txt", 0, "test-removed", False)})

    # test-removed: a test deleted together with the source it tests

    def test_test_deleted_with_its_source_is_green(self):
        self.repo.commit(
            src__x_py="def f():\n    return 1\n",
            tests__test_x_py="def test_f():\n    assert f() == 1\n",
            **{"web/y.ts": "export const y = 1\n", "web/y.test.ts": "test('y', () => {})\n"},
        )
        self.repo.commit(
            src__x_py=None, tests__test_x_py=None, **{"web/y.ts": None, "web/y.test.ts": None}
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_test_deleted_with_unrelated_source_is_red(self):
        self.repo.commit(
            src__x_py="def f():\n    return 1\n",
            src__y_py="def g():\n    return 2\n",
            tests__test_x_py="def test_f():\n    assert f() == 1\n",
        )
        self.repo.commit(src__y_py=None, tests__test_x_py=None)
        self.assertEqual(self.rules_at_head(), {("tests/test_x.py", 0, "test-removed", False)})

    def test_test_deleted_while_its_source_is_only_changed_is_red(self):
        self.repo.commit(
            src__x_py="def f():\n    return 1\n",
            tests__test_x_py="def test_f():\n    assert f() == 1\n",
        )
        self.repo.commit(src__x_py="def f():\n    return 2\n", tests__test_x_py=None)
        self.assertEqual(self.rules_at_head(), {("tests/test_x.py", 0, "test-removed", False)})


class Severity(RepoCase):
    """Two levels: a block finding needs a Check-change line, a note never fails the run."""

    def seed(self):
        self.repo.commit(tests__test_thing_py=BASE_TEST, src__a_py="x = 1\n")

    def test_default_table_has_one_note_rule_and_the_rest_block(self):
        rules = ci.load_rules(RULES)
        levels = {rule.id: rule.severity for rule in rules.rules}
        self.assertEqual({i for i, level in levels.items() if level == "note"}, {"suppression-added"})
        self.assertEqual({level for level in levels.values()}, {"block", "note"})

    def test_a_note_alone_exits_0_prints_it_and_counts_it(self):
        self.seed()
        self.repo.commit(src__a_py="x = f()  # noqa: BLE001\n")
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, err), (0, ""), out)
        self.assertRegex(out, r"src/a\.py:1 suppression-added note  x = f\(\)  # noqa: BLE001")
        self.assertEqual(
            out.splitlines()[-1], "check-integrity: ok (1 commits, 1 files, 1 note(s), not blocking)"
        )

    def test_json_carries_the_level_and_the_counts(self):
        self.seed()
        self.repo.commit(src__a_py="x = f()  # noqa: BLE001\n")
        code, report = self.head_json()
        self.assertEqual(code, 0)
        self.assertEqual(
            (report["status"], report["blocking"], report["notes"]), ("ok", 0, 1), report
        )
        self.assertEqual([f["severity"] for f in report["findings"]], ["note"])
        self.repo.commit(tests__test_thing_py=BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two"))
        code, report = self.head_json()
        self.assertEqual(code, 1)
        self.assertEqual((report["status"], report["blocking"], report["notes"]), ("findings", 1, 0))
        self.assertEqual([f["severity"] for f in report["findings"]], ["block"])

    def test_a_block_finding_still_exits_1_next_to_a_note(self):
        self.seed()
        self.repo.commit(
            src__a_py="x = f()  # noqa: BLE001\n",
            tests__test_thing_py=BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two"),
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("skip-added  @unittest.skip('x')", out)
        self.assertIn("suppression-added note", out)
        self.assertIn("1 finding(s) without justification", out.splitlines()[-1])
        self.assertIn("1 note(s) do not block", out.splitlines()[-1])

    def test_a_check_change_line_changes_nothing_for_a_note(self):
        self.seed()
        self.repo.commit(f"Quiet it\n\n{REASON}\n", src__a_py="x = f()  # noqa: BLE001\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.assertIn("suppression-added note", out)

    def test_staged_note_exits_0(self):
        self.seed()
        self.repo.write("src/a.py", "x = f()  # noqa: BLE001\n")
        self.repo.git("add", "-A")
        code, out, _ = self.repo.scan("--staged")
        self.assertEqual(code, 0, out)
        self.assertIn("staged src/a.py:1 suppression-added note", out)

    def test_rules_file_severity_is_validated(self):
        document = json.loads(RULES.read_text())
        bad = Path(self._tmp.name) / "bad.json"
        document["rules"][0]["severity"] = "warn"
        bad.write_text(json.dumps(document))
        with self.assertRaises(ci.IntegrityError):
            ci.load_rules(bad)
        document["rules"][0]["severity"] = "block"
        document["rules"].append(dict(document["rules"][0], severity="note"))
        bad.write_text(json.dumps(document))
        with self.assertRaisesRegex(ci.IntegrityError, "share one severity"):
            ci.load_rules(bad)

    def test_a_rule_without_severity_blocks(self):
        document = json.loads(RULES.read_text())
        for rule in document["rules"]:
            rule.pop("severity", None)
        custom = Path(self._tmp.name) / "custom.json"
        custom.write_text(json.dumps(document))
        self.seed()
        self.repo.commit(src__a_py="x = f()  # noqa: BLE001\n")
        code, out, _ = self.repo.scan("--commit", "HEAD", "--rules", str(custom))
        self.assertEqual(code, 1, out)
        self.assertNotIn(" note ", out)


BROKEN_OVERLAYS = {
    "{not json": "cannot read",
    "[]": "expected a JSON object",
    json.dumps({"version": 2, "rules": {}}): "must be 1",
    json.dumps({"version": 1, "rules": {}, "classes": {}}): "unknown key(s): classes",
    json.dumps({"version": 1, "rules": {"no-such-rule": {"severity": "note"}}}): "no-such-rule",
    json.dumps({"version": 1, "rules": {"skip-added": {"patterns": ["x"]}}}): "only ['allow', 'exempt_paths', 'severity']",
    json.dumps({"version": 1, "rules": {"skip-added": {"why": "w"}}}): "only ['allow', 'exempt_paths', 'severity']",
    json.dumps({"version": 1, "rules": {"skip-added": {"severity": "off"}}}): "severity must be",
    json.dumps({"version": 1, "rules": {"skip-added": {"allow": ["("]}}}): "invalid pattern",
    json.dumps({"version": 1, "rules": {"skip-added": {"allow": "x"}}}): "list of non-empty strings",
    json.dumps({"version": 1, "rules": []}): "keyed by rule id",
    json.dumps({"version": 1, "rules": {"skip-added": {"exempt_paths": "tests/x.py"}}}): "list of non-empty strings",
    json.dumps({"version": 1, "rules": {"skip-added": {"exempt_paths": [""]}}}): "list of non-empty strings",
    json.dumps({"version": 1, "rules": {"skip-added": {"exempt_paths": [3]}}}): "list of non-empty strings",
    json.dumps({"version": 1, "rules": {"check-machinery-changed": {"allow": ["x"]}}}): "no line to allow",
}


class Overlay(RepoCase):
    """A repository's .check-integrity.json sets severity and appends allow-regexes per rule id."""

    def setUp(self):
        super().setUp()
        self.repo.commit(tests__test_thing_py=BASE_TEST, src__a_py="x = 1\n")

    def overlay(self, document):
        """Write and commit the overlay, so that the commit under test holds only its change."""
        text = document if isinstance(document, str) else json.dumps(document)
        self.repo.commit("Set the overlay", **{".check-integrity.json": text})

    def skip_commit(self, reason="x"):
        return self.repo.commit(
            tests__test_thing_py=BASE_TEST.replace(
                "    def test_two", f"    @unittest.skip({reason!r})\n    def test_two"
            )
        )

    def test_overlay_can_turn_a_note_into_a_block(self):
        self.overlay({"version": 1, "rules": {"suppression-added": {"severity": "block"}}})
        self.repo.commit(src__a_py="x = f()  # noqa: BLE001\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1, out)
        self.assertNotIn(" note ", out)

    def test_overlay_can_turn_a_block_into_a_note(self):
        self.overlay({"version": 1, "rules": {"skip-added": {"severity": "note"}}})
        self.skip_commit()
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.assertIn("skip-added note  @unittest", out)

    def test_overlay_allow_is_appended_to_that_rule_only(self):
        self.overlay({"version": 1, "rules": {"skip-added": {"allow": ["@unittest\\.skip\\('waived"]}}})
        self.skip_commit("waived by the owner")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 1 files)"))
        self.skip_commit("not waived")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1, out)
        self.repo.commit(**{".githooks/pre-push": "git push --no-verify\n"})
        self.assertIn("bypass-added", self.rule_names())

    def test_exempt_paths_spare_the_matching_files_of_that_rule_only(self):
        self.repo.commit(tests__test_other_py=BASE_TEST)
        self.overlay({"version": 1, "rules": {"skip-added": {"exempt_paths": ["tests/test_thing.py"]}}})
        self.skip_commit()
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 1 files)"))
        self.repo.commit(tests__test_other_py=skipped(BASE_TEST))
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("tests/test_other.py:4 skip-added", out)
        # the file stays under every other rule
        self.repo.commit(tests__test_thing_py="import unittest\n")
        self.assertEqual(self.rule_names(), {"test-removed", "assertion-loss", "test-class-detached"})

    def test_exempt_paths_are_globs_and_a_rename_needs_both_paths_to_match(self):
        self.repo.commit(tests__test_other_py=BASE_TEST)
        self.overlay({"version": 1, "rules": {"skip-added": {"exempt_paths": ["tests/test_t*.py"]}}})
        self.repo.git("mv", "tests/test_thing.py", "tests/test_tx.py")
        self.repo.commit(tests__test_tx_py=skipped(BASE_TEST))
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, "both paths match the glob:\n" + out)
        self.repo.git("mv", "tests/test_tx.py", "tests/test_renamed.py")
        self.repo.commit(tests__test_renamed_py=skipped(BASE_TEST, "test_two"))
        self.assertEqual(self.rule_names(), {"skip-added"}, "the new path is not exempt")
        self.repo.git("mv", "tests/test_other.py", "tests/test_ty.py")
        self.repo.commit(tests__test_ty_py=skipped(BASE_TEST))
        self.assertEqual(self.rule_names(), {"skip-added"}, "the old path is not exempt")

    def test_exempt_paths_work_for_the_deletion_and_net_loss_rules(self):
        self.overlay(
            {
                "version": 1,
                "rules": {
                    "test-removed": {"exempt_paths": ["tests/test_thing.py"]},
                    "assertion-loss": {"exempt_paths": ["tests/test_thing.py"]},
                    "test-class-detached": {"exempt_paths": ["tests/test_thing.py"]},
                },
            }
        )
        self.repo.commit(tests__test_thing_py=None)
        self.assertEqual(self.rules_at_head(), set())
        self.repo.commit(tests__test_thing_py=BASE_TEST)
        self.repo.commit(tests__test_thing_py="import unittest\n")
        self.assertEqual(self.rules_at_head(), set())
        self.repo.commit(tests__test_other_py=BASE_TEST)
        self.repo.commit(tests__test_other_py="import unittest\n")
        self.assertEqual(self.rule_names(), {"test-removed", "assertion-loss", "test-class-detached"})

    def test_overlay_is_found_from_a_subfolder_of_the_repository(self):
        self.overlay({"rules": {"skip-added": {"severity": "note"}}})
        self.skip_commit()
        (self.repo.root / "sub").mkdir()
        done = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--commit", "HEAD"],
            cwd=self.repo.root / "sub",
            env=self.repo.env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_broken_explicit_overlay_warns_and_makes_every_rule_block(self):
        """--overlay is handed the baseline's file by CI, so a broken one is treated as a broken
        overlay in the baseline is: one warning, every rule blocks, the exit code is the findings'."""
        self.skip_commit()
        path = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-broken-overlay.json"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        for text, expected in BROKEN_OVERLAYS.items():
            with self.subTest(overlay=text):
                self.repo.git("commit", "-q", "--amend", "-m", "Park a test")
                path.write_text(text)
                code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(path))
                self.assertEqual(code, 1, out + err)  # the skip has no reason line
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertIn("warning", err)
                self.assertIn(expected, err)
                self.assertIn("every rule is judged as block", err)
                code, out, _ = self.repo.scan("--commit", "HEAD", "--overlay", str(path), "--json")
                report = json.loads(out)
                self.assertEqual((code, report["status"]), (1, "findings"))
                self.assertEqual([(f["rule"], f["severity"]) for f in report["findings"]], [("skip-added", "block")])
                self.repo.git("commit", "-q", "--amend", "-m", f"Park a test\n\n{REASON}")
                code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(path))
                self.assertEqual(code, 0, out + err)  # justified: the warning is not an error
                self.assertIn("warning", err)

    def test_an_explicit_overlay_that_is_not_a_regular_utf8_file_is_invalid_too(self):
        self.skip_commit()
        binary = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-binary-overlay.json"
        self.addCleanup(lambda: binary.unlink(missing_ok=True))
        binary.write_bytes(b'{"version": 1, "rules": {"skip-added": {"allow": ["\xff"]}}}')
        for what, path in (("a directory", Path(self._tmp.name)), ("bytes that are not UTF-8", binary)):
            with self.subTest(what):
                code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(path))
                self.assertEqual(code, 1, out + err)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertIn("warning", err)
                self.assertIn("every rule is judged as block", err)

    def test_an_explicit_overlay_that_cannot_be_read_is_a_usage_error(self):
        """A path that is not there, or that the process may not read, says nothing about the
        file CI means: that stays exit 2."""
        self.skip_commit()
        path = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-unreadable-overlay.json"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(path))
        self.assertEqual(code, 2, out + err)
        self.assertIn("cannot read", err)
        path.write_text(json.dumps(NOTE_SKIPS))
        path.chmod(0)
        try:
            path.read_bytes()
        except PermissionError:
            pass
        else:
            self.skipTest("this account can read a file with mode 000 (root?)")
        code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(path))
        self.assertEqual(code, 2, out + err)
        self.assertIn("cannot read", err)

    def test_no_overlay_and_explicit_overlay(self):
        self.overlay("{not json")
        self.skip_commit()
        code, out, _ = self.repo.scan("--commit", "HEAD", "--no-overlay")
        self.assertEqual(code, 1, out)  # the broken file is not read, the finding stands
        other = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-overlay.json"
        other.write_text(json.dumps({"rules": {"skip-added": {"severity": "note"}}}))
        self.addCleanup(other.unlink)
        code, out, _ = self.repo.scan("--commit", "HEAD", "--overlay", str(other))
        self.assertEqual(code, 0, out)
        code, _, err = self.repo.scan("--commit", "HEAD", "--overlay", str(other) + "-missing")
        self.assertEqual(code, 2, err)
        code, _, err = self.repo.scan("--commit", "HEAD", "--overlay", str(other), "--no-overlay")
        self.assertEqual(code, 2, err)

    def test_the_overlay_is_a_check_file_so_removing_a_line_from_it_is_reported(self):
        self.overlay('{\n  "rules": {\n    "skip-added": {\n      "allow": ["a"]\n    }\n  }\n}\n')
        self.overlay('{\n  "rules": {\n  }\n}\n')
        self.assertIn(".check-integrity.json", {path for path, _, _, _ in self.rules_at_head()})


class OwnOverlay(RepoCase):
    """This repository's own .check-integrity.json exempts the scanner's test file from the rules
    that look for marker strings, which that file holds as data, and nothing else of it."""

    OVERLAY = Path(__file__).resolve().parent.parent / ".check-integrity.json"
    TEST_FILE = Path(__file__).resolve()

    def setUp(self):
        super().setUp()
        self.text = self.TEST_FILE.read_text(encoding="utf-8")
        self.repo.commit(tests__test_check_integrity_py=self.text)

    def scan_head(self, overlay=True):
        flag = ("--overlay", str(self.OVERLAY)) if overlay else ("--no-overlay",)
        return self.repo.scan("--commit", "HEAD", *flag)

    def test_changing_the_marker_strings_needs_no_reason_line(self):
        changed = self.text.replace('"flaky"', '"flaky again"').replace("'later'", "'much later'")
        changed = changed.replace(
            "            \"tests/test_a.py\": \"    self.skipTest('later')\\n\",\n",
            "            \"tests/test_a.py\": \"    self.skipTest('later')\\n\",\n"
            "            \"tests/test_z.py\": \"    self.skipTest('new')\\n\",\n",
        )
        self.assertNotEqual(changed, self.text)
        self.repo.commit(tests__test_check_integrity_py=changed)
        code, out, _ = self.scan_head()
        self.assertEqual(code, 0, out)
        code, out, _ = self.scan_head(overlay=False)
        self.assertEqual(code, 1, "without the overlay the same change needs a reason line:\n" + out)
        self.assertIn("skip-added", out)

    def test_touching_every_line_of_the_file_needs_no_reason_line(self):
        touched = "".join(line.rstrip("\n") + "  \n" for line in self.text.splitlines(keepends=True))
        self.repo.commit(tests__test_check_integrity_py=touched)
        code, out, _ = self.scan_head()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.scan_head(overlay=False)[0] == 1, "the file does hold skip markers as data")

    def test_removing_a_test_function_or_an_assertion_is_still_reported(self):
        lines = self.text.splitlines(keepends=True)
        name = "def test_empty_range_" + "is_ok"
        start = next(n for n, line in enumerate(lines) if name in line)
        end = start + 1
        while lines[end].strip():
            end += 1
        without_test = "".join(lines[:start] + lines[end + 1 :])
        self.assertNotIn(name, without_test)
        self.repo.commit(tests__test_check_integrity_py=without_test)
        self.assertIn(("tests/test_check_integrity.py", "test-removed"), self.found())
        self.repo.git("reset", "-q", "--hard", "HEAD~1")
        target = "        self.assertEqual(code, 1)\n"
        self.assertIn(target, self.text)
        self.repo.commit(tests__test_check_integrity_py=self.text.replace(target, "", 1))
        self.assertIn(("tests/test_check_integrity.py", "assertion-loss"), self.found())
        for rule in ("test-removed", "assertion-loss"):
            self.assertEqual(self.scan_head()[0], 1, rule)

    def test_a_skip_in_another_test_file_is_reported_even_when_its_reason_has_a_newline_escape(self):
        self.repo.commit(tests__test_other_py=BASE_TEST)
        self.repo.commit(
            tests__test_other_py=BASE_TEST.replace("    def test_two", "    @unittest.skip('a\\nb')\n    def test_two")
        )
        self.assertIn(("tests/test_other.py", "skip-added"), self.found())
        self.assertEqual(self.scan_head()[0], 1)

    def test_the_overlay_sets_nothing_but_exempt_paths(self):
        document = json.loads(self.OVERLAY.read_text(encoding="utf-8"))
        self.assertEqual(
            {rule: sorted(change) for rule, change in document["rules"].items()},
            {rule: ["exempt_paths"] for rule in ("skip-added", "suppression-added", "bypass-added")},
        )
        for change in document["rules"].values():
            self.assertEqual(change["exempt_paths"], ["tests/test_check_integrity.py"])

    def found(self):
        code, out, _ = self.repo.scan("--commit", "HEAD", "--overlay", str(self.OVERLAY), "--json")
        return {(f["path"], f["rule"]) for f in json.loads(out)["findings"]}


class OverlayGuard(RepoCase):
    """A change to .check-integrity.json, the overlay itself, needs a reason line: any added or
    removed line, a new file or a deleted one. The overlay cannot waive that."""

    OVERLAY = ".check-integrity.json"

    def setUp(self):
        super().setUp()
        self.repo.commit(tests__test_thing_py=BASE_TEST, src__a_py="x = 1\n")

    def findings(self):
        code, report = self.head_json()
        return code, {(f["path"], f["rule"], f["justified"]) for f in report["findings"]}

    def test_a_new_overlay_needs_a_reason_line(self):
        self.repo.commit("Add the overlay", **{self.OVERLAY: '{\n  "version": 1\n}\n'})
        self.assertEqual(self.findings(), (1, {(self.OVERLAY, "overlay-changed", False)}))
        self.repo.git("commit", "-q", "--amend", "-m", f"Add the overlay\n\n{REASON}")
        self.assertEqual(self.findings(), (0, {(self.OVERLAY, "overlay-changed", True)}))

    def test_adding_a_line_to_an_overlay_needs_a_reason_line(self):
        self.repo.commit("Add the overlay", **{self.OVERLAY: '{\n  "version": 1,\n  "rules": {\n  }\n}\n'})
        self.repo.commit(
            "Exempt a file",
            **{
                self.OVERLAY: '{\n  "version": 1,\n  "rules": {\n'
                '    "skip-added": {"exempt_paths": ["tests/*"]}\n  }\n}\n'
            },
        )
        self.assertEqual(self.findings(), (1, {(self.OVERLAY, "overlay-changed", False)}))

    def test_removing_a_line_or_the_whole_file_needs_a_reason_line(self):
        self.repo.commit("Add the overlay", **{self.OVERLAY: '{\n  "version": 1,\n  "rules": {}\n}\n'})
        self.repo.commit("Shorten it", **{self.OVERLAY: '{\n  "version": 1\n}\n'})
        code, found = self.findings()
        self.assertEqual(code, 1)
        self.assertIn((self.OVERLAY, "overlay-changed", False), found)
        self.repo.commit("Drop it", **{self.OVERLAY: None})
        code, found = self.findings()
        self.assertEqual(code, 1)
        self.assertIn((self.OVERLAY, "overlay-changed", False), found)

    def test_an_empty_new_overlay_counts_too(self):
        self.repo.commit("Add an empty overlay", **{self.OVERLAY: ""})
        code, out, _ = self.repo.scan("--commit", "HEAD", "--no-overlay")
        self.assertEqual(code, 1, out)
        self.assertIn("overlay-changed", out)

    def test_only_the_overlay_at_the_top_of_the_repository_is_guarded(self):
        self.repo.commit("A file with that name elsewhere", **{f"sub/{self.OVERLAY}": "{}\n"})
        self.assertNotIn("overlay-changed", self.rule_names())

    def test_the_staged_overlay_is_judged_with_the_commit_message(self):
        self.repo.write(self.OVERLAY, '{\n  "version": 1\n}\n')
        self.repo.git("add", self.OVERLAY)
        code, out, _ = self.repo.scan("--staged")
        self.assertEqual(code, 1, out)
        self.assertIn("staged .check-integrity.json:1 overlay-changed", out)
        message = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-message"
        message.write_text(f"Add the overlay\n\n{REASON}\n")
        self.addCleanup(message.unlink)
        code, out, _ = self.repo.scan("--staged", "--message-file", str(message))
        self.assertEqual(code, 0, out)

    def test_the_overlay_cannot_change_that_rule(self):
        explicit = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-guard-overlay.json"
        self.addCleanup(lambda: explicit.unlink(missing_ok=True))
        seed = self.repo.git("rev-parse", "HEAD")  # its tautology is a finding whatever the overlay says
        for change in ({"severity": "note"}, {"allow": ["x"]}, {"exempt_paths": [".check-integrity.json"]}):
            with self.subTest(change=change):
                text = json.dumps({"rules": {"overlay-changed": change}})
                explicit.write_text(text)
                # the overlay is rejected, so nothing it says takes effect: the finding stands
                code, out, err = self.repo.scan("--commit", seed, "--overlay", str(explicit))
                self.assertEqual(code, 1, out)
                self.assertIn("tautology-added", out)
                self.assertIn("overlay-changed", err)
                self.assertIn("cannot", err)
                self.assertIn("every rule is judged as block", err)
                # committed, it is a broken overlay of the baseline of the next commit: warned
                # about, applied as nothing, and the next commit is still judged
                self.repo.commit("Set the overlay", **{self.OVERLAY: text})
                self.repo.commit("Touch a file", src__a_py=f"x = {len(text)}\n")
                code, out, err = self.repo.scan("--commit", "HEAD")
                self.assertEqual(code, 0, out + err)
                self.assertIn("cannot change this rule", err)


OVERLAY_FILE = ".check-integrity.json"
NOTE_SKIPS = {"version": 1, "rules": {"skip-added": {"severity": "note"}}}
# skip-added ignores files a change adds, so a root commit is probed with suppression-added,
# a note by default, which this overlay turns into a block
BLOCK_NOQA = {"version": 1, "rules": {"suppression-added": {"severity": "block"}}}
NOQA = "x = f()  # noqa: BLE001\n"


def noqa_severities(output):
    return {f["severity"] for f in json.loads(output)["findings"] if f["rule"] == "suppression-added"}


class BaselineOverlay(RepoCase):
    """The overlay that judges a change is the one in the baseline the change is judged against
    (HEAD for --staged, the first parent for --commit, the left end for --range), never the
    working tree's and never the judged commit's own: otherwise a change could loosen its own
    judge, or be judged by a file that is not the one its branch is governed by."""

    def setUp(self):
        super().setUp()
        self.base = self.repo.commit("Seed", tests__test_thing_py=BASE_TEST, src__a_py="x = 1\n")

    def set_overlay(self, document=NOTE_SKIPS):
        text = document if isinstance(document, (str, bytes)) else json.dumps(document)
        return self.repo.commit(f"Set the overlay\n\n{REASON}", **{OVERLAY_FILE: text})

    def drop_overlay(self):
        return self.repo.commit(f"Drop the overlay\n\n{REASON}", **{OVERLAY_FILE: None})

    def skip(self, name="test_one", message="Park a test"):
        return self.repo.commit(message, tests__test_thing_py=skipped(BASE_TEST, name))

    def stage_skip(self):
        self.repo.write("tests/test_thing.py", skipped(BASE_TEST))
        self.repo.git("add", "tests/test_thing.py")

    def skip_findings(self, *args, stdin=None):
        """(exit code, severities of the skip-added findings, standard error)."""
        code, out, err = self.repo.scan(*args, "--json", stdin=stdin)
        self.assertIn(code, (0, 1), out + err)
        found = json.loads(out)["findings"]
        return code, sorted({f["severity"] for f in found if f["rule"] == "skip-added"}), err

    def fresh_repo(self):
        root = Path(self._tmp.name) / "fresh"
        root.mkdir()
        return Repo(root)

    # --staged: HEAD

    def test_staged_ignores_a_working_tree_overlay_that_is_not_in_head(self):
        self.repo.write(OVERLAY_FILE, json.dumps(NOTE_SKIPS))  # never staged
        self.stage_skip()
        self.assertEqual(self.skip_findings("--staged")[:2], (1, ["block"]))

    def test_staged_uses_the_overlay_of_head_whatever_the_working_tree_holds(self):
        self.set_overlay()
        self.repo.write(OVERLAY_FILE, '{"version": 1}\n')  # edited, not staged
        self.stage_skip()
        self.assertEqual(self.skip_findings("--staged"), (0, ["note"], ""))

    def test_staged_does_not_apply_an_overlay_it_adds_itself(self):
        self.repo.write(OVERLAY_FILE, json.dumps(NOTE_SKIPS))
        self.repo.git("add", OVERLAY_FILE)
        self.stage_skip()
        self.assertEqual(self.skip_findings("--staged")[:2], (1, ["block"]))

    def test_staged_in_a_repository_without_commits_has_no_baseline_and_no_overlay(self):
        fresh = self.fresh_repo()
        fresh.write(OVERLAY_FILE, json.dumps(BLOCK_NOQA))
        fresh.write("src/a.py", NOQA)
        fresh.git("add", "-A")
        message = Path(self._tmp.name) / "fresh-message"
        message.write_text(f"Root\n\n{REASON}\n")
        code, out, err = fresh.scan("--staged", "--message-file", str(message), "--json")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(noqa_severities(out), {"note"})  # a block would mean the overlay was applied

    # --commit: the first parent

    def test_a_commit_is_judged_by_the_overlay_of_its_first_parent(self):
        self.set_overlay()
        parked = self.skip()
        self.drop_overlay()
        self.assertEqual(self.skip_findings("--commit", parked), (0, ["note"], ""))

    def test_a_commit_is_not_judged_by_the_overlay_it_brings(self):
        self.repo.commit(
            f"Both\n\n{REASON}", **{OVERLAY_FILE: json.dumps(NOTE_SKIPS)}, tests__test_thing_py=skipped(BASE_TEST)
        )
        self.assertEqual(self.skip_findings("--commit", "HEAD")[:2], (0, ["block"]))

    def test_a_root_commit_has_no_baseline_and_no_overlay(self):
        fresh = self.fresh_repo()
        fresh.commit(f"Root\n\n{REASON}", **{OVERLAY_FILE: json.dumps(BLOCK_NOQA)}, src__a_py=NOQA)
        code, out, err = fresh.scan("--commit", "HEAD", "--json")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(noqa_severities(out), {"note"})  # a block would mean the overlay was applied

    def test_a_merge_commit_is_judged_by_the_overlay_of_its_first_parent(self):
        self.set_overlay()
        self.repo.git("checkout", "-q", "-b", "side")
        self.drop_overlay()
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "side")
        merge = self.repo.commit("Merge side", tests__test_thing_py=skipped(BASE_TEST))
        self.assertEqual(self.skip_findings("--commit", merge), (0, ["note"], ""))

    # --range: the left end

    def test_a_range_is_judged_by_the_overlay_at_its_left_end(self):
        left = self.set_overlay()
        self.skip()
        self.drop_overlay()
        self.skip("test_two", "Park another")
        self.assertEqual(self.skip_findings("--range", f"{left}..HEAD"), (0, ["note"], ""))

    def test_a_symmetric_range_is_judged_by_the_merge_base_and_a_plain_one_by_the_left_tip(self):
        self.set_overlay()
        self.repo.git("checkout", "-q", "-b", "side")
        self.skip()
        self.repo.git("checkout", "-q", "main")
        self.drop_overlay()
        self.assertEqual(self.skip_findings("--range", "main...side"), (0, ["note"], ""))
        self.assertEqual(self.skip_findings("--range", "main..side")[:2], (1, ["block"]))

    # an invalid overlay in the baseline

    def test_an_invalid_overlay_in_the_baseline_warns_once_and_makes_every_rule_block(self):
        for number, (text, expected) in enumerate(BROKEN_OVERLAYS.items()):
            with self.subTest(overlay=text):
                # every case starts from the seed, so each one adds a marker net of its own
                self.repo.git("checkout", "-q", "-f", "-B", f"case{number}", self.base)
                self.set_overlay(text)
                self.repo.commit("Silence a line", src__a_py=f"x = f({number})  # noqa: BLE001\n")
                code, out, err = self.repo.scan("--commit", "HEAD", "--json")
                self.assertEqual(code, 1, out + err)
                found = json.loads(out)["findings"]
                self.assertEqual([(f["rule"], f["severity"]) for f in found], [("suppression-added", "block")])
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertIn("warning", err)
                self.assertIn(expected, err)

    def test_the_warning_is_printed_once_for_a_whole_range(self):
        left = self.set_overlay("{not json")
        self.skip()
        self.skip("test_two", "Park another")
        code, out, err = self.repo.scan("--range", f"{left}..HEAD")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(err.count("warning"), 1, err)

    def test_an_overlay_that_is_not_a_regular_utf8_file_is_invalid_too(self):
        def link():
            os.symlink("elsewhere.json", self.repo.root / OVERLAY_FILE)

        def folder():
            self.repo.write(f"{OVERLAY_FILE}/inner.json", "{}\n")

        def binary():
            self.repo.write(OVERLAY_FILE, b'{"version": 1, "rules": {"skip-added": {"allow": ["\xff"]}}}')

        for number, (what, make) in enumerate((("a symbolic link", link), ("a directory", folder), ("bytes that are not UTF-8", binary))):
            with self.subTest(what):
                self.repo.git("checkout", "-q", "-f", "-B", f"case{number}", self.base)
                make()
                self.repo.git("add", "-A")
                self.repo.git("commit", "-q", "-m", "Odd overlay")
                self.repo.commit("Silence a line", src__a_py="x = f()  # noqa: BLE001\n")
                code, out, err = self.repo.scan("--commit", "HEAD", "--json")
                self.assertEqual(code, 1, out + err)
                found = json.loads(out)["findings"]
                self.assertEqual([(f["rule"], f["severity"]) for f in found], [("suppression-added", "block")])
                self.assertIn("warning", err)

    def test_an_executable_overlay_file_is_read_like_any_other(self):
        self.repo.write(OVERLAY_FILE, json.dumps(NOTE_SKIPS))
        (self.repo.root / OVERLAY_FILE).chmod(0o755)
        self.repo.commit(f"Set the overlay\n\n{REASON}")
        self.skip()
        self.assertEqual(self.skip_findings("--commit", "HEAD"), (0, ["note"], ""))

    def test_the_commit_that_fixes_a_broken_overlay_is_not_blocked_by_it(self):
        self.set_overlay("{not json")
        self.repo.commit(f"Fix the overlay\n\n{REASON}", **{OVERLAY_FILE: json.dumps(NOTE_SKIPS)})
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out + err)
        self.assertIn("warning", err)
        self.repo.git("reset", "-q", "--hard", "HEAD~1")
        self.repo.write(OVERLAY_FILE, json.dumps(NOTE_SKIPS))
        self.repo.git("add", OVERLAY_FILE)
        message = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-fix-message"
        message.write_text(f"Fix the overlay\n\n{REASON}\n")
        self.addCleanup(lambda: message.unlink(missing_ok=True))
        code, out, err = self.repo.scan("--staged", "--message-file", str(message))
        self.assertEqual(code, 0, out + err)
        self.assertIn("warning", err)

    def test_an_overlay_given_with_overlay_or_no_overlay_is_not_read_from_the_baseline(self):
        self.set_overlay("{not json")
        self.skip()
        code, out, err = self.repo.scan("--commit", "HEAD", "--no-overlay")
        self.assertEqual((code, err), (1, ""), out)
        explicit = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-explicit-overlay.json"
        explicit.write_text(json.dumps(NOTE_SKIPS))
        self.addCleanup(lambda: explicit.unlink(missing_ok=True))
        code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(explicit))
        self.assertEqual((code, err), (0, ""), out)


class PushBaseline(RepoCase):
    """--pre-push judges each ref by the overlay at the remote side of the push; a new branch by
    the one at its merge base with the remote's default branch (--remote names the remote), and
    when that is not known by the one at the first parent of its oldest new commit."""

    ZERO = "0" * 40

    def setUp(self):
        super().setUp()
        remote = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-baseline-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=self.repo.env)
        self.addCleanup(lambda: shutil.rmtree(remote, ignore_errors=True))
        self.repo.git("remote", "add", "origin", str(remote))
        self.repo.commit("Seed", tests__test_thing_py=BASE_TEST)
        self.main = self.repo.commit(f"Set the overlay\n\n{REASON}", **{OVERLAY_FILE: json.dumps(NOTE_SKIPS)})
        self.repo.git("push", "-q", "origin", "main")

    def drop_overlay(self):
        return self.repo.commit(f"Drop the overlay\n\n{REASON}", **{OVERLAY_FILE: None})

    def skip(self, name="test_one"):
        return self.repo.commit(f"Park {name}", tests__test_thing_py=skipped(BASE_TEST, name))

    def push_scan(self, local, remote, branch, *flags):
        """(exit code, severities of the skip-added findings, standard error) of one ref line."""
        ref = f"refs/heads/{branch}"
        code, out, err = self.repo.scan("--pre-push", *flags, "--json", stdin=f"{ref} {local} {ref} {remote}\n")
        self.assertIn(code, (0, 1), out + err)
        found = json.loads(out)["findings"]
        return code, sorted({f["severity"] for f in found if f["rule"] == "skip-added"}), err

    def set_remote_head(self):
        self.repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    def fork_of_a_pushed_branch(self):
        """Branch b has one new commit and forks from branch a, which is on the remote and
        dropped the overlay that the remote's default branch has."""
        self.repo.git("checkout", "-q", "-b", "a")
        self.drop_overlay()
        self.repo.git("push", "-q", "origin", "a")
        self.repo.git("checkout", "-q", "-b", "b")
        return self.skip()

    def test_an_existing_branch_is_judged_by_the_overlay_of_the_remote_side(self):
        self.drop_overlay()
        local = self.skip()
        self.assertEqual(self.push_scan(local, self.main, "main"), (0, ["note"], ""))

    def test_a_new_branch_is_judged_by_the_overlay_at_its_merge_base_with_the_remote_default_branch(self):
        local = self.fork_of_a_pushed_branch()
        self.set_remote_head()
        self.assertEqual(self.push_scan(local, self.ZERO, "b", "--remote", "origin"), (0, ["note"], ""))

    def test_a_new_branch_falls_back_to_the_parent_of_its_oldest_new_commit(self):
        local = self.fork_of_a_pushed_branch()
        no_head = (1, ["block"])  # the parent is the tip of a, which has no overlay
        self.assertEqual(self.push_scan(local, self.ZERO, "b", "--remote", "origin")[:2], no_head)
        self.set_remote_head()
        self.assertEqual(self.push_scan(local, self.ZERO, "b")[:2], no_head)
        self.assertEqual(self.push_scan(local, self.ZERO, "b", "--remote", "https://example.invalid/x.git")[:2], no_head)

    def test_the_fallback_baseline_is_one_for_all_the_new_commits(self):
        self.repo.git("checkout", "-q", "-b", "c")
        self.drop_overlay()
        local = self.skip()
        self.assertEqual(self.push_scan(local, self.ZERO, "c"), (0, ["note"], ""))

    def test_a_new_branch_from_the_root_has_no_baseline(self):
        self.repo.git("checkout", "-q", "--orphan", "fresh")
        self.repo.git("rm", "-q", "-r", "-f", "--cached", ".")
        local = self.repo.commit(f"Root\n\n{REASON}", **{OVERLAY_FILE: json.dumps(BLOCK_NOQA)}, src__a_py=NOQA)
        ref = "refs/heads/fresh"
        code, out, err = self.repo.scan(
            "--pre-push", "--remote", "origin", "--json", stdin=f"{ref} {local} {ref} {self.ZERO}\n"
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(noqa_severities(out), {"note"})  # a block would mean the overlay was applied

    def test_a_push_that_fixes_a_broken_overlay_on_the_remote_is_not_blocked_by_it(self):
        broken = self.repo.commit(f"Break the overlay\n\n{REASON}", **{OVERLAY_FILE: "{not json"})
        fixed = self.repo.commit(f"Fix the overlay\n\n{REASON}", **{OVERLAY_FILE: json.dumps(NOTE_SKIPS)})
        code, severities, err = self.push_scan(fixed, broken, "main")
        self.assertEqual(code, 0, err)
        self.assertIn("warning", err)

    def test_remote_belongs_to_pre_push(self):
        code, out, err = self.repo.scan("--commit", "HEAD", "--remote", "origin")
        self.assertEqual(code, 2, out)
        self.assertIn("--remote works only with --pre-push", err)


class CiScanStep(unittest.TestCase):
    """The check-integrity step of .github/workflows/ci.yml, run as the shell script it is, in a
    throwaway repository. Pull requests and pushes are judged by the scanner, rules and overlay of
    the baseline (the base branch, the default branch, or the old tip of a push to the default
    branch), never by the ones the judged commits bring along. A push to the default branch has
    already landed: findings there are a warning, a scanner error still fails."""

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def step_script(cls, name="Check-integrity scan"):
        lines = (cls.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8").splitlines()
        start = next(n for n, line in enumerate(lines) if line.strip().startswith(f"- name: {name}"))
        run = next(n for n in range(start, len(lines)) if lines[n].strip() == "run: |")
        indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
        body = []
        for line in lines[run + 1 :]:
            if line.strip() and len(line) - len(line.lstrip()) < indent:
                break
            body.append(line[indent:])
        return "\n".join(body) + "\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        (base / "work").mkdir()
        (base / "runner").mkdir()
        self.runner_temp = base / "runner"
        self.repo = Repo(base / "work")
        for name in ("check_integrity.py", "check_integrity_rules.json"):
            self.repo.write(f"tools/{name}", (self.ROOT / "tools" / name).read_bytes())
        self.base_sha = self.repo.commit("Seed", tests__test_thing_py=BASE_TEST)
        self.bad = self.repo.commit("Park a test", tests__test_thing_py=skipped(BASE_TEST, "test_two"))
        self.repo.git("update-ref", "refs/remotes/origin/main", self.base_sha)

    def run_step(self, script=None, **variables):
        values = {
            "RUNNER_TEMP": str(self.runner_temp),
            "BASE_REF": "",
            "PR_HEAD": "",
            "BEFORE": self.base_sha,
            "AFTER": self.bad,
            "REF": "refs/heads/main",
            "DEFAULT_BRANCH": "main",
        }
        values.update(variables)
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script or self.step_script()],
            cwd=self.repo.root,
            env={**self.repo.env, **values},
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, done.stdout + done.stderr

    ZERO = "0" * 40
    UNKNOWN = "1" * 40  # a well-formed old tip that is not in the clone: a force push
    DEFAULT_PUSH = "a push to the default branch"
    OVERLAY = ".check-integrity.json"

    def seed_baseline(self, base):
        """Put origin/main, the baseline of pull requests and branch pushes, at base."""
        self.repo.git("update-ref", "refs/remotes/origin/main", base)

    def feature(self, *prelude, base=None):
        """A branch off base: the prelude commits (each with a reason line, so only the next
        commit is unjustified), a commit that parks a test with no reason, a harmless one on top.
        Returns the sha of the parking commit and the tip."""
        self.repo.git("checkout", "-q", "-b", "feature", base or self.base_sha)
        for message, files in prelude:
            self.repo.commit(f"{message}\n\n{REASON}", **files)
        park = self.repo.commit("Park a test", tests__test_thing_py=skipped(BASE_TEST, "test_two"))
        tip = self.repo.commit("Add a note", notes_txt="x\n")
        return park, tip

    def entries(self, tip, base):
        """The ways the commits up to tip reach the step, each as (where origin/main is, the step's
        variables), with base as the baseline: what origin/main is, and for the push to the default
        branch the old tip. Once that push has landed origin/main is the new tip."""
        return {
            "a pull request": (base, dict(BASE_REF="main", PR_HEAD=tip, REF="refs/pull/7/merge", BEFORE="", AFTER=tip)),
            "the first push of a branch": (base, dict(REF="refs/heads/feature", BEFORE=self.ZERO, AFTER=tip)),
            "a force push": (base, dict(REF="refs/heads/feature", BEFORE=self.UNKNOWN, AFTER=tip)),
            "an ordinary push": (base, dict(REF="refs/heads/feature", BEFORE=base, AFTER=tip)),
            self.DEFAULT_PUSH: (tip, dict(REF="refs/heads/main", BEFORE=base, AFTER=tip)),
        }

    def outcome(self, kind, origin_main, variables):
        """"blocked" when the scanner found an unjustified change (exit 1, or on a push to the default
        branch exit 0 with a warning), "passed" when it found none, else the raw exit code."""
        self.seed_baseline(origin_main)
        code, out = self.run_step(**variables)
        warned = "::warning::" in out
        if kind == self.DEFAULT_PUSH and code == 0 and warned:
            return "blocked", out
        if kind != self.DEFAULT_PUSH and code == 1 and not warned:
            return "blocked", out
        if code == 0 and not warned:
            return "passed", out
        return f"exit {code}, warning {warned}", out

    def assert_blocked_everywhere(self, tip, base):
        for kind, (origin_main, variables) in self.entries(tip, base).items():
            with self.subTest(kind):
                result, out = self.outcome(kind, origin_main, variables)
                self.assertEqual(result, "blocked", out)
                self.assertIn("skip-added", out)

    def test_a_push_to_the_default_branch_reports_findings_as_a_warning_and_passes(self):
        code, out = self.run_step()
        self.assertEqual(code, 0, out)
        self.assertIn("skip-added", out)
        self.assertIn("::warning::", out)

    def test_a_clean_push_to_the_default_branch_prints_no_warning(self):
        tip = self.repo.commit("Add a note", notes_txt="x\n")
        code, out = self.run_step(BEFORE=self.bad, AFTER=tip)
        self.assertEqual(code, 0, out)
        self.assertNotIn("::warning::", out)
        self.assertIn("check-integrity: ok (1 commits, 1 files)", out)

    def test_a_scanner_error_on_the_default_branch_still_fails_the_step(self):
        code, out = self.run_step(AFTER="f" * 40)
        self.assertEqual(code, 2, out)
        self.assertNotIn("::warning::", out)

    def assert_passes_everywhere(self, tip, base):
        """Exit 0 for every way in, with the scanner's warning about the overlay in the output."""
        for kind, (origin_main, variables) in self.entries(tip, base).items():
            with self.subTest(kind):
                result, out = self.outcome(kind, origin_main, variables)
                self.assertEqual(result, "passed", out)
                self.assertIn("warning", out)
                self.assertIn("every rule is judged as block", out)

    def judge_by_a_broken_overlay(self, break_it):
        """The default branch holds an overlay file that cannot be used (the step hands it to the
        scanner with --overlay). A change that repairs it, and one that does not touch it and has
        nothing to report, must still pass; a skip with no reason line must still be stopped."""
        self.repo.git("checkout", "-q", "-b", "trunk", self.base_sha)
        break_it()
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", f"Break the overlay\n\n{REASON}")
        trunk = self.repo.git("rev-parse", "HEAD")
        # the repair: the broken file (a link, too) goes, a valid one takes its place
        self.repo.git("checkout", "-q", "-b", "repair", trunk)
        self.repo.git("rm", "-q", "--", self.OVERLAY)
        repair = self.repo.commit(f"Fix the overlay\n\n{REASON}", **{self.OVERLAY: self.RELAXED})
        self.assert_passes_everywhere(repair, trunk)
        # a change that has nothing to report
        self.repo.git("checkout", "-q", "-b", "unrelated", trunk)
        note = self.repo.commit("Add a note", notes_txt="x\n")
        self.assert_passes_everywhere(note, trunk)
        # a skip with no reason line
        _, tip = self.feature(base=trunk)
        self.assert_blocked_everywhere(tip, trunk)
        for kind, (origin_main, variables) in self.entries(tip, trunk).items():
            with self.subTest(kind):
                _, out = self.outcome(kind, origin_main, variables)
                self.assertIn("every rule is judged as block", out)

    def test_an_overlay_with_a_misspelled_key_on_the_default_branch_does_not_block_the_step(self):
        text = json.dumps({"version": 1, "rules": {"skip-added": {"sevrity": "note"}}})
        self.judge_by_a_broken_overlay(lambda: self.repo.write(self.OVERLAY, text))

    def test_an_overlay_naming_a_rule_that_does_not_exist_on_the_default_branch_does_not_block_the_step(self):
        text = json.dumps({"version": 1, "rules": {"no-such-rule": {"severity": "note"}}})
        self.judge_by_a_broken_overlay(lambda: self.repo.write(self.OVERLAY, text))

    def test_an_overlay_that_is_a_symbolic_link_on_the_default_branch_does_not_block_the_step(self):
        self.judge_by_a_broken_overlay(lambda: os.symlink("elsewhere.json", self.repo.root / self.OVERLAY))

    def test_the_first_push_to_the_default_branch_has_nothing_to_scan(self):
        for label, before in (("first push", self.ZERO), ("force push", self.UNKNOWN)):
            with self.subTest(label):
                code, out = self.run_step(BEFORE=before)
                self.assertEqual(code, 0, out)
                self.assertNotIn("skip-added", out)
                self.assertNotIn("::warning::", out)
                self.assertIn("check-integrity:", out)

    def test_a_push_to_another_branch_is_scanned(self):
        code, out = self.run_step(REF="refs/heads/feature")
        self.assertEqual(code, 1, out)
        self.assertIn("skip-added", out)

    def test_the_default_branch_comes_from_the_repository_not_from_the_script(self):
        self.repo.git("update-ref", "refs/remotes/origin/trunk", self.base_sha)  # the clone has every branch
        code, out = self.run_step(DEFAULT_BRANCH="trunk")
        self.assertEqual(code, 1, "main is an ordinary branch here:\n" + out)
        code, out = self.run_step(DEFAULT_BRANCH="trunk", REF="refs/heads/trunk")
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::", out)

    def test_a_pull_request_is_scanned_even_when_it_targets_the_default_branch(self):
        code, out = self.run_step(BASE_REF="main", PR_HEAD=self.bad, REF="refs/pull/7/merge")
        self.assertEqual(code, 1, out)
        self.assertIn("skip-added", out)

    def test_the_first_push_of_a_branch_is_scanned_too(self):
        code, out = self.run_step(REF="refs/heads/feature", BEFORE=self.ZERO)
        self.assertEqual(code, 1, out)

    def test_every_commit_of_the_range_is_scanned_not_only_the_tip(self):
        _, tip = self.feature()
        self.assert_blocked_everywhere(tip, self.base_sha)

    def test_the_first_push_of_a_branch_and_a_force_push_scan_from_the_fork_point(self):
        park, tip = self.feature()
        self.seed_baseline(self.base_sha)
        for label, before in (("first push", self.ZERO), ("force push", self.UNKNOWN)):
            with self.subTest(label):
                code, out = self.run_step(REF="refs/heads/feature", BEFORE=before, AFTER=tip)
                self.assertEqual(code, 1, out)
                self.assertIn(f"{park[:7]} tests/test_thing.py", out)
                self.assertIn("(2 commits", out)

    def test_an_ordinary_push_scans_only_the_commits_it_adds(self):
        park, tip = self.feature()
        self.seed_baseline(self.base_sha)
        code, out = self.run_step(REF="refs/heads/feature", BEFORE=park, AFTER=tip)
        self.assertEqual(code, 0, out)
        self.assertIn("(1 commits", out)

    def test_a_branch_with_no_history_in_common_with_the_default_branch_scans_its_tip_and_says_why(self):
        self.repo.git("checkout", "-q", "--orphan", "island")
        self.repo.commit("Start over", tests__test_thing_py=BASE_TEST)
        tip = self.repo.commit("Park a test", tests__test_thing_py=skipped(BASE_TEST, "test_two"))
        self.seed_baseline(self.base_sha)
        code, out = self.run_step(REF="refs/heads/island", BEFORE=self.ZERO, AFTER=tip)
        self.assertEqual(code, 1, out)
        self.assertIn("skip-added", out)
        self.assertIn("only the top commit", out)

    def test_the_scanner_comes_from_the_baseline(self):
        _, tip = self.feature(("Replace the scanner", {"tools/check_integrity.py": "import sys\nsys.exit(0)\n"}))
        self.assert_blocked_everywhere(tip, self.base_sha)

    def test_the_rules_come_from_the_baseline(self):
        rules = json.loads((self.ROOT / "tools" / "check_integrity_rules.json").read_text(encoding="utf-8"))
        for rule in rules["rules"]:
            if rule["id"] == "skip-added":
                rule["severity"] = "note"
        _, tip = self.feature(("Relax the rules", {"tools/check_integrity_rules.json": json.dumps(rules)}))
        self.assert_blocked_everywhere(tip, self.base_sha)

    def test_an_overlay_the_commits_bring_is_not_applied(self):
        relaxed = json.dumps({"version": 1, "rules": {"skip-added": {"severity": "note"}}})
        _, tip = self.feature(("Relax the rules", {self.OVERLAY: relaxed}))
        self.assert_blocked_everywhere(tip, self.base_sha)

    def test_the_overlay_of_the_baseline_is_applied(self):
        relaxed = json.dumps({"version": 1, "rules": {"skip-added": {"severity": "note"}}})
        self.repo.git("checkout", "-q", "-b", "trunk", self.base_sha)
        trunk = self.repo.commit(f"Relax the rules\n\n{REASON}", **{self.OVERLAY: relaxed})
        _, tip = self.feature(("Drop the overlay", {self.OVERLAY: None}), base=trunk)
        for kind, (origin_main, variables) in self.entries(tip, trunk).items():
            with self.subTest(kind):
                result, out = self.outcome(kind, origin_main, variables)
                self.assertEqual(result, "passed", out)
                self.assertIn("skip-added note", out)

    RELAXED = json.dumps({"version": 1, "rules": {"skip-added": {"severity": "note"}}})

    def test_a_later_push_is_not_judged_by_an_overlay_an_earlier_push_of_the_branch_brought(self):
        # the range of an ordinary push starts at the old tip of the branch: its overlay is the branch's own
        self.repo.git("checkout", "-q", "-b", "feature", self.base_sha)
        old_tip = self.repo.commit(f"Relax the rules\n\n{REASON}", **{self.OVERLAY: self.RELAXED})
        park = self.repo.commit("Park a test", tests__test_thing_py=skipped(BASE_TEST, "test_two"))
        self.seed_baseline(self.base_sha)
        code, out = self.run_step(REF="refs/heads/feature", BEFORE=old_tip, AFTER=park)
        self.assertEqual(code, 1, out)
        self.assertIn("skip-added", out)
        self.assertNotIn("skip-added note", out)

    def test_a_later_push_is_judged_by_the_overlay_of_the_default_branch_not_the_one_the_branch_dropped(self):
        self.repo.git("checkout", "-q", "-b", "trunk", self.base_sha)
        trunk = self.repo.commit(f"Relax the rules\n\n{REASON}", **{self.OVERLAY: self.RELAXED})
        self.repo.git("checkout", "-q", "-b", "feature", trunk)
        old_tip = self.repo.commit(f"Drop the overlay\n\n{REASON}", **{self.OVERLAY: None})
        park = self.repo.commit("Park a test", tests__test_thing_py=skipped(BASE_TEST, "test_two"))
        self.seed_baseline(trunk)
        code, out = self.run_step(REF="refs/heads/feature", BEFORE=old_tip, AFTER=park)
        self.assertEqual(code, 0, out)
        self.assertIn("skip-added note", out)

    def test_a_new_branch_is_judged_by_the_overlay_of_the_default_branch_not_of_its_fork_point(self):
        self.repo.git("checkout", "-q", "-b", "trunk", self.base_sha)
        trunk = self.repo.commit(f"Relax the rules\n\n{REASON}", **{self.OVERLAY: self.RELAXED})
        self.repo.git("checkout", "-q", "-b", "feature", self.base_sha)
        tip = self.repo.commit("Park a test", tests__test_thing_py=skipped(BASE_TEST, "test_two"))
        self.seed_baseline(trunk)
        code, out = self.run_step(REF="refs/heads/feature", BEFORE=self.ZERO, AFTER=tip)
        self.assertEqual(code, 0, out)
        self.assertIn("skip-added note", out)

    def test_a_baseline_without_a_scanner_leaves_the_commits_to_their_own(self):
        self.repo.git("checkout", "-q", "-b", "bare", self.base_sha)
        bare = self.repo.commit(
            "Remove the scanner", **{"tools/check_integrity.py": None, "tools/check_integrity_rules.json": None}
        )
        scanner = {
            f"tools/{name}": (self.ROOT / "tools" / name).read_bytes()
            for name in ("check_integrity.py", "check_integrity_rules.json")
        }
        _, tip = self.feature(("Add the scanner", scanner), base=bare)
        self.assert_blocked_everywhere(tip, bare)

    def tag_as_the_base_branch(self, commit):
        """A tag named like the remote branch: git reads the short name origin/main as this tag."""
        self.repo.git("tag", "origin/main", commit)

    BY_THE_BRANCH = ("a pull request", "the first push of a branch", "a force push")

    def test_a_tag_named_like_the_base_branch_at_the_change_does_not_empty_the_range(self):
        _, tip = self.feature()
        self.tag_as_the_base_branch(tip)
        entries = self.entries(tip, self.base_sha)
        for kind in self.BY_THE_BRANCH:
            with self.subTest(kind):
                result, out = self.outcome(kind, *entries[kind])
                self.assertEqual(result, "blocked", out)
                self.assertIn("skip-added", out)

    def test_a_tag_named_like_the_base_branch_cannot_bring_a_scanner_of_its_own(self):
        self.repo.git("checkout", "-q", "-b", "decoy", self.base_sha)
        decoy = self.repo.commit("A scanner that passes everything", tools__check_integrity_py="import sys\nsys.exit(0)\n")
        _, tip = self.feature()
        self.tag_as_the_base_branch(decoy)
        entries = self.entries(tip, self.base_sha)
        for kind in self.BY_THE_BRANCH:
            with self.subTest(kind):
                result, out = self.outcome(kind, *entries[kind])
                self.assertEqual(result, "blocked", out)
                self.assertIn("skip-added", out)

    def test_a_pull_request_that_brings_no_commit_beyond_its_base_is_not_a_pass(self):
        for label, head in (("head is the base", self.bad), ("head is behind the base", self.base_sha)):
            with self.subTest(label):
                self.seed_baseline(self.bad)
                code, out = self.run_step(BASE_REF="main", PR_HEAD=head, REF="refs/pull/7/merge", BEFORE="", AFTER=head)
                self.assertEqual(code, 1, out)
                self.assertIn("brings no commit beyond its base", out)
                self.assertNotIn("ok (0 commits", out)

    def test_a_base_branch_missing_from_the_clone_exits_2_even_with_a_tag_of_its_name(self):
        _, tip = self.feature()
        # rev-parse looks even a full name up through refs/tags/ once the branch is missing, so a
        # tag called refs/remotes/origin/main would answer for it.
        tags = (
            ("no tag", None),
            ("a tag origin/main", "origin/main"),
            ("a tag refs/remotes/origin/main", "refs/remotes/origin/main"),
        )
        for label, tag in tags:
            with self.subTest(label):
                self.repo.git("update-ref", "-d", "refs/remotes/origin/main", check=False)
                if tag:
                    self.repo.git("tag", "-f", tag, tip)
                for kind, variables in (
                    ("a pull request", dict(BASE_REF="main", PR_HEAD=tip, REF="refs/pull/7/merge", BEFORE="", AFTER=tip)),
                    ("the first push of a branch", dict(REF="refs/heads/feature", BEFORE=self.ZERO, AFTER=tip)),
                ):
                    with self.subTest(kind):
                        code, out = self.run_step(**variables)
                        self.assertEqual(code, 2, out)
                        self.assertIn("refs/remotes/origin/main", out)

    WORKFLOW_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    @classmethod
    def jobs(cls):
        """The jobs of ci.yml, read as text: name -> (the lines of the job, its steps). A step is the
        lines from one "- " at the step indent to the next, labelled by that first line."""
        jobs = {}
        name = None
        in_jobs = False
        for line in cls.WORKFLOW_TEXT.splitlines():
            if line.rstrip() == "jobs:":
                in_jobs = True
            elif in_jobs and re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
                name = line.strip().rstrip(":")
                jobs[name] = ([], [])
            elif in_jobs and name is not None:
                jobs[name][0].append(line)
                if line.startswith("      - "):
                    jobs[name][1].append([line.strip()[2:]])
                elif jobs[name][1]:
                    jobs[name][1][-1].append(line)
        return jobs

    def test_the_scans_have_a_job_of_their_own_with_no_step_that_runs_the_repositorys_code(self):
        jobs = self.jobs()
        scanning = [n for n, (_, steps) in jobs.items() if any(s[0].startswith("name: Check-integrity scan") for s in steps)]
        testing = [n for n, (_, steps) in jobs.items() if any(s[0] == "name: Tests" for s in steps)]
        self.assertEqual(len(scanning), 1, list(jobs))
        self.assertEqual(len(testing), 1, list(jobs))
        self.assertNotEqual(scanning, testing, "the tests run in the job that holds the scans")
        labels = [step[0] for step in jobs[scanning[0]][1]]
        self.assertEqual(len(labels), 3, labels)
        self.assertTrue(labels[0].startswith("uses: actions/checkout@"), labels)
        self.assertTrue(labels[1].startswith("name: Check-integrity scan"), labels)
        self.assertTrue(labels[2].startswith("name: Privacy scan"), labels)
        checkout = "\n".join(jobs[scanning[0]][1][0])
        self.assertIn("fetch-depth: 0", checkout)
        self.assertIn("persist-credentials: false", checkout)

    def test_the_scans_judge_the_head_commit_of_a_pull_request_not_the_merge_commit_github_makes(self):
        """The default checkout of a pull request is a merge commit GitHub creates for the run, with the
        web identity of the account that opened it; the privacy scan reads every commit it can see, so
        that commit's address is judged and refused. The scanning job checks out the head commit."""
        jobs = self.jobs()
        scanning = next(n for n, (_, steps) in jobs.items() if any(s[0].startswith("name: Check-integrity scan") for s in steps))
        testing = next(n for n, (_, steps) in jobs.items() if any(s[0] == "name: Tests" for s in steps))
        gates_checkout = [line.strip() for line in jobs[scanning][1][0]]
        self.assertIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", gates_checkout)
        tests_checkout = [line.strip() for line in jobs[testing][1][0]]
        self.assertTrue(tests_checkout[0].startswith("uses: actions/checkout@"), tests_checkout)
        self.assertEqual([line for line in tests_checkout if line.startswith("ref:")], [])

    def test_both_jobs_keep_their_runner_and_time_limit(self):
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2, list(jobs))
        for name, (lines, _) in jobs.items():
            with self.subTest(name):
                self.assertIn("    runs-on: ubuntu-24.04", lines)
                self.assertIn("    timeout-minutes: 10", lines)

    def test_both_scanners_are_started_in_isolated_mode(self):
        lines = [line.strip() for line in self.WORKFLOW_TEXT.splitlines() if 'python3 ' in line and '"$scanner"' in line]
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertTrue(line.startswith("python3 -I -B "), line)

    def test_only_a_pull_request_run_is_cancelled_by_a_newer_one(self):
        lines = [line.strip() for line in self.WORKFLOW_TEXT.splitlines() if line.strip().startswith("cancel-in-progress:")]
        self.assertEqual(lines, ["cancel-in-progress: ${{ github.event_name == 'pull_request' }}"])

    @classmethod
    def gate_steps(cls):
        """The steps of the scanning job that run a script, in the order ci.yml lists them:
        (name, script, runs_after_a_failure). A step runs after a failed one only when it says
        `if: ${{ !cancelled() }}`, as GitHub decides it."""
        jobs = cls.jobs()
        steps = next(
            job_steps
            for _, job_steps in jobs.values()
            if any(step[0].startswith("name: Check-integrity scan") for step in job_steps)
        )
        found = []
        for step in steps:
            if step[0].startswith("name: "):
                name = step[0][len("name: ") :]
                always = any(line.strip() == "if: ${{ !cancelled() }}" for line in step)
                found.append((name, cls.step_script(name), always))
        return found

    def run_gates(self, **variables):
        """Run the steps of the scanning job one after another the way GitHub does: {name: (exit
        code, output)}, with no entry for a step that was skipped after a failure."""
        results = {}
        failed = False
        for name, script, always in self.gate_steps():
            if failed and not always:
                continue
            code, out = self.run_step(script=script, **variables)
            results[name] = (code, out)
            failed = failed or code != 0
        return results

    def test_the_commit_check_comes_before_the_privacy_scan_in_the_scanning_job(self):
        names = [name for name, _, _ in self.gate_steps()]
        self.assertEqual(len(names), 2, names)
        commit_check = next(n for n, name in enumerate(names) if name.startswith("Check-integrity scan"))
        privacy = next(n for n, name in enumerate(names) if name.startswith("Privacy scan"))
        self.assertLess(commit_check, privacy, names)
        always = {name: always for name, _, always in self.gate_steps()}
        self.assertTrue(always[names[privacy]], "a commit finding must not hide a privacy finding")

    PRIVACY_SCANNER_THAT_MOVES_THE_BASELINE = (
        "import subprocess\n"
        "subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)\n"
        "print('privacy: clean')\n"
    )

    def test_a_pushed_privacy_scanner_that_moves_the_baseline_cannot_hide_the_commit_finding(self):
        """The privacy step of a push runs tools/privacy_scan.py as the pushed commit has it. One
        that moves origin/main to HEAD leaves nothing between the two for a step that comes
        after it, so the commit check has to have run first. The order comes from ci.yml."""
        self.repo.git("checkout", "-q", "-b", "trunk", self.base_sha)
        trunk = self.repo.commit("Add a privacy scanner", tools__privacy_scan_py="print('privacy: clean')\n")
        self.repo.git("checkout", "-q", "-b", "feature", trunk)
        park = self.repo.commit("Park a test", tests__test_thing_py=skipped(BASE_TEST, "test_two"))
        tip = self.repo.commit(
            "Tidy the privacy scanner", tools__privacy_scan_py=self.PRIVACY_SCANNER_THAT_MOVES_THE_BASELINE
        )
        self.seed_baseline(trunk)
        results = self.run_gates(REF="refs/heads/feature", BEFORE=self.ZERO, AFTER=tip)
        commit_check = next(name for name in results if name.startswith("Check-integrity scan"))
        code, out = results[commit_check]
        self.assertEqual(code, 1, out)
        self.assertIn(f"{park[:7]} tests/test_thing.py", out)
        self.assertIn("skip-added", out)
        privacy = next(name for name in results if name.startswith("Privacy scan"))
        self.assertEqual(results[privacy][0], 0, "the scanner of the pushed commit passes whatever it is shown")

    def test_on_a_pull_request_the_privacy_scanner_is_the_base_branchs_not_the_one_the_change_brings(self):
        base = self.privacy_repo()
        self.seed_baseline(base)
        self.repo.git("checkout", "-q", "-b", "feature", base)
        self.repo.commit("Tidy the scanner", tools__privacy_scan_py="print('privacy: clean')\n")
        tip = self.repo.commit("Leak an address", src__a_py="host = '192.168." + "1.20'\n")
        script = self.step_script("Privacy scan")
        pull_request = dict(BASE_REF="main", PR_HEAD=tip, REF="refs/pull/7/merge", AFTER=tip)
        done = subprocess.run(
            ["python3", "-I", "-B", "tools/privacy_scan.py", "--tracked"],
            cwd=self.repo.root,
            env=self.repo.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "privacy: clean"), "control: the pushed scanner passes")
        code, out = self.run_step(script=script, **pull_request)
        self.assertEqual(code, 1, out)
        self.assertIn("src/a.py:1: lan_ip", out)

    @staticmethod
    def evaluate(expression, context):
        """The one shape of GitHub expression the concurrency group uses: `a == 'x' && b || c`,
        with context paths and quoted strings. `&&` and `||` give the value they stopped at, as
        GitHub's do, not a boolean."""

        def atom(text):
            text = text.strip()
            if "==" in text:
                left, right = text.split("==")
                return atom(left) == atom(right)
            return text[1:-1] if text.startswith("'") else context[text]

        def conjunction(text):
            value = None
            for part in text.split("&&"):
                value = atom(part)
                if not value:
                    break
            return value

        value = None
        for alternative in expression.split("||"):
            value = conjunction(alternative)
            if value:
                break
        return value

    def concurrency_group(self, **context):
        lines = [line.strip() for line in self.WORKFLOW_TEXT.splitlines() if line.startswith("  group:")]
        self.assertEqual(len(lines), 1, lines)
        match = re.fullmatch(r"group: (\S*)\$\{\{ (.+) \}\}", lines[0])
        self.assertIsNotNone(match, lines[0])
        return match.group(1) + str(self.evaluate(match.group(2), context))

    def test_every_push_run_has_a_concurrency_group_of_its_own_and_a_pull_request_shares_one_per_ref(self):
        def push(run_id, ref="refs/heads/feature"):
            return self.concurrency_group(**{"github.event_name": "push", "github.ref": ref, "github.run_id": run_id})

        def pull_request(run_id, ref="refs/pull/7/merge"):
            return self.concurrency_group(
                **{"github.event_name": "pull_request", "github.ref": ref, "github.run_id": run_id}
            )

        self.assertEqual(len({push("1001"), push("1002"), push("1003")}), 3, "three pushes to one branch")
        self.assertEqual(pull_request("1001"), pull_request("1002"), "a newer run of a pull request replaces the older")
        self.assertNotEqual(pull_request("1001"), pull_request("1002", ref="refs/pull/8/merge"))
        self.assertNotEqual(pull_request("1001"), push("1001"))

    def stand_in_for_the_library(self, module):
        """A file in the runner's temp folder, where the step puts the scanner, that ends the
        process with status 0 as soon as the scanner imports the module it is named after."""
        (self.runner_temp / f"{module}.py").write_text("import os; os._exit(0)\n", encoding="utf-8")

    def assert_the_stand_in_would_work(self, scanner, *args):
        """Control: started the plain way, the scanner copy the step left behind does exit 0 and say
        nothing, so the stand-in is live and a refusal in the step is because of isolated mode."""
        done = subprocess.run(
            ["python3", "-B", str(self.runner_temp / scanner), *args],
            cwd=self.repo.root,
            env=self.repo.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_a_module_planted_next_to_the_scanner_cannot_stand_in_for_the_standard_library(self):
        self.stand_in_for_the_library("bisect")
        pull_request = dict(BASE_REF="main", PR_HEAD=self.bad, REF="refs/pull/7/merge")
        code, out = self.run_step(**pull_request)
        self.assertEqual(code, 1, out)
        self.assertIn("skip-added", out)
        self.assert_the_stand_in_would_work("check_integrity.py", "--commit", "HEAD")
        code, out = self.run_step()  # a push to the default branch: the finding is a warning
        self.assertEqual(code, 0, out)
        self.assertIn("skip-added", out)
        self.assertIn("::warning::", out)

    def privacy_repo(self):
        """Swap self.repo for a repository of its own that holds the privacy scanner and its rules,
        committed: the privacy scan reads the author of every commit, and the one of setUp is not
        on the list of allowed addresses. Returns the sha of that commit."""
        (Path(self._tmp.name) / "privacy").mkdir()
        self.repo = Repo(Path(self._tmp.name) / "privacy")
        for name in ("privacy_scan.py", "privacy_rules.json"):
            self.repo.write(f"tools/{name}", (self.ROOT / "tools" / name).read_bytes())
        self.repo.env.update(
            GIT_AUTHOR_EMAIL="1+author@users.noreply.github.com",
            GIT_COMMITTER_EMAIL="1+author@users.noreply.github.com",
            AGENT_OVERSIGHT_PRIVATE_RULES=str(self.runner_temp / "no-private-rules.json"),
        )
        return self.repo.commit("Add the privacy scanner")

    def test_a_module_planted_next_to_the_privacy_scanner_cannot_stand_in_for_the_standard_library(self):
        clean = self.privacy_repo()
        self.seed_baseline(clean)
        script = self.step_script("Privacy scan")
        pull_request = dict(BASE_REF="main", PR_HEAD=clean, REF="refs/pull/7/merge")
        code, out = self.run_step(script=script, **pull_request)
        self.assertEqual(code, 0, "a clean history passes:\n" + out)
        self.repo.commit("Leak an address", src__a_py="host = '192.168." + "1.20'\n")
        self.stand_in_for_the_library("json")
        code, out = self.run_step(script=script, **pull_request)
        self.assertEqual(code, 1, out)
        self.assertIn("src/a.py:1: lan_ip", out)
        self.assert_the_stand_in_would_work("privacy_scan.py", "--rules", str(self.runner_temp / "privacy_rules.json"))

    def test_on_a_pull_request_a_tag_named_like_the_base_branch_cannot_bring_the_privacy_scanner(self):
        base = self.privacy_repo()
        self.seed_baseline(base)
        self.repo.git("checkout", "-q", "-b", "feature", base)
        self.repo.commit("Tidy the scanner", tools__privacy_scan_py="print('privacy: clean')\n")
        tip = self.repo.commit("Leak an address", src__a_py="host = '192.168." + "1.20'\n")
        self.repo.git("tag", "origin/main", tip)
        script = self.step_script("Privacy scan")
        pull_request = dict(BASE_REF="main", PR_HEAD=tip, REF="refs/pull/7/merge", AFTER=tip)
        code, out = self.run_step(script=script, **pull_request)
        self.assertEqual(code, 1, out)
        self.assertIn("src/a.py:1: lan_ip", out)
        self.repo.git("update-ref", "-d", "refs/remotes/origin/main")
        for tag in (None, "refs/remotes/origin/main"):  # the second is looked up when the branch is missing
            with self.subTest(tag=tag):
                if tag:
                    self.repo.git("tag", tag, tip)
                code, out = self.run_step(script=script, **pull_request)
                self.assertEqual(code, 2, out)
                self.assertIn("refs/remotes/origin/main", out)


class PushRefs(RepoCase):
    """--pre-push reads the lines git gives a pre-push hook and scans the commits they bring."""

    ZERO = "0" * 40

    def setUp(self):
        super().setUp()
        self.skipped = BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two")
        self.base = self.repo.commit(tests__test_thing_py=BASE_TEST)
        self.bad = self.repo.commit("Park a test", tests__test_thing_py=self.skipped)
        self.good = self.repo.commit(src__a_py="x = 1\n")
        remote = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=self.repo.env)
        self.addCleanup(lambda: __import__("shutil").rmtree(remote, ignore_errors=True))
        self.repo.git("remote", "add", "origin", str(remote))

    def push_scan(self, *lines):
        return self.repo.scan("--pre-push", stdin="".join(line + "\n" for line in lines))

    def line(self, local, remote, ref="refs/heads/main"):
        return f"{ref} {local} {ref} {remote}"

    def test_an_existing_branch_scans_remote_to_local(self):
        code, out, err = self.push_scan(self.line(self.good, self.base))
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"{self.bad[:7]} tests/test_thing.py:7 skip-added", out)
        self.assertIn("(2 commits, 2 files)", out)
        code, out, err = self.push_scan(self.line(self.good, self.bad))
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 1 files)"), err)

    def test_a_new_branch_scans_only_what_no_remote_branch_has(self):
        self.repo.git("push", "-q", "origin", "main")  # the unjustified commit is already there
        self.repo.git("checkout", "-q", "-b", "feat")
        also_bad = self.repo.commit(
            "Park another",
            tests__test_thing_py=self.skipped.replace("    def test_one", "    @unittest.skip('y')\n    def test_one"),
        )
        fine = self.repo.commit(src__b_py="y = 2\n")
        code, out, err = self.push_scan(self.line(fine, self.ZERO, "refs/heads/feat"))
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"{also_bad[:7]} tests/test_thing.py:4 skip-added", out)
        self.assertNotIn(self.bad[:7], out)
        self.assertIn("(2 commits, 2 files)", out)

    def test_a_remote_sha_this_clone_does_not_have_is_treated_as_a_new_branch(self):
        self.repo.git("push", "-q", "origin", "main")
        extra = self.repo.commit("More", src__b_py="y = 2\n")
        code, out, err = self.push_scan(self.line(extra, "1" * 40))
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 1 files)"), err)

    def test_a_deleted_branch_scans_nothing(self):
        code, out, err = self.push_scan(self.line(self.ZERO, self.good))
        self.assertEqual((code, out.strip()), (0, "check-integrity: ok (0 commits, 0 files)"), err)

    def test_no_lines_and_blank_lines_scan_nothing(self):
        for lines in ((), ("",)):
            with self.subTest(lines=lines):
                code, out, err = self.push_scan(*lines)
                self.assertEqual((code, out.strip()), (0, "check-integrity: ok (0 commits, 0 files)"), err)

    def test_two_refs_with_shared_commits_count_each_commit_once(self):
        code, out, _ = self.push_scan(
            self.line(self.good, self.base), self.line(self.bad, self.base, "refs/heads/other")
        )
        self.assertEqual(code, 1, out)
        self.assertIn("(2 commits, 2 files)", out)

    def test_malformed_lines_exit_2(self):
        for line in (
            "refs/heads/main",
            f"refs/heads/main {self.good} refs/heads/main",
            f"refs/heads/main --not refs/heads/main {self.base}",
            f"refs/heads/main {self.good} refs/heads/main -x",
        ):
            with self.subTest(line=line):
                code, out, err = self.push_scan(line)
                self.assertEqual(code, 2, out + err)
                self.assertIn("pre-push", err)


class RepositoryHooks(unittest.TestCase):
    """The real .githooks of this repository, run by git in a throwaway repository that holds
    copies of its tools. These are the three cases a human or agent meets at commit time."""

    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        (base / "work").mkdir()
        self.repo = Repo(base / "work")
        self.remote = base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, env=self.repo.env)
        self.repo.env.update(
            GIT_AUTHOR_EMAIL="1+author@users.noreply.github.com",
            GIT_COMMITTER_EMAIL="1+author@users.noreply.github.com",
            AGENT_OVERSIGHT_PRIVATE_RULES=str(base / "no-private-rules.json"),
        )
        for name in ("privacy_scan.py", "privacy_rules.json", "check_integrity.py", "check_integrity_rules.json"):
            self.repo.write(f"tools/{name}", (self.ROOT / "tools" / name).read_bytes())
        for name in ("pre-commit", "commit-msg", "pre-push"):
            self.repo.write(f".githooks/{name}", (self.ROOT / ".githooks" / name).read_bytes())
            (self.repo.root / ".githooks" / name).chmod(0o755)
        self.repo.git("config", "core.hooksPath", ".githooks")
        self.repo.git("remote", "add", "origin", str(self.remote))
        self.repo.write("tests/test_thing.py", BASE_TEST)
        self.repo.write("src/a.py", "x = 1\n")
        self.repo.git("add", "-A")
        self.commit(f"Seed the repository\n\n{REASON}")

    def commit(self, message, *extra):
        done = subprocess.run(
            ["git", "commit", "-q", "-m", message, *extra],
            cwd=self.repo.root,
            env=self.repo.env,
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, done.stdout + done.stderr

    def stage(self, path, content):
        self.repo.write(path, content)
        self.repo.git("add", "--", path)

    def head(self):
        return self.repo.git("rev-parse", "HEAD")

    def plumb_commit(self, message, parent="HEAD"):
        """Commit the index on top of ``parent`` and move main there, running no hook."""
        oid = self.repo.git("commit-tree", self.repo.git("write-tree"), "-p", parent, "-m", message)
        self.repo.git("update-ref", "refs/heads/main", oid)

    def test_a_skipped_test_without_a_reason_is_refused_and_with_one_is_accepted(self):
        before = self.head()
        self.stage(
            "tests/test_thing.py", BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two")
        )
        code, out = self.commit("Park a test")
        self.assertNotEqual(code, 0, out)
        self.assertIn("skip-added", out)
        self.assertEqual(self.head(), before)
        code, out = self.commit(f"Park a test\n\n{REASON}")
        self.assertEqual(code, 0, out)
        self.assertNotEqual(self.head(), before)

    def test_a_new_noqa_commits_and_prints_a_note(self):
        before = self.head()
        self.stage("src/a.py", "x = f()  # noqa: BLE001\n")
        code, out = self.commit("Silence one line")
        self.assertEqual(code, 0, out)
        self.assertIn("suppression-added note", out)
        self.assertNotEqual(self.head(), before)

    def test_the_hook_runs_the_privacy_scan_too(self):
        before = self.head()
        self.stage("src/a.py", "x = 2\n")
        code, out = self.commit("Contact 192.168." + "1.20 about it")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.head(), before)

    def test_a_module_dropped_next_to_the_scanner_cannot_stand_in_for_the_standard_library(self):
        """tools/bisect.py is not committed, so only the working tree has it; the scanner imports bisect."""
        self.repo.write("tools/bisect.py", "import os; os._exit(0)\n")
        before = self.head()
        self.stage(
            "tests/test_thing.py", BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two")
        )
        code, out = self.commit("Park a test")
        self.assertNotEqual(code, 0, out)
        self.assertIn("skip-added", out)
        self.assertEqual(self.head(), before)

    def test_pre_push_is_not_fooled_by_a_module_dropped_next_to_the_scanner_either(self):
        self.repo.git("push", "-q", "origin", "main")
        self.repo.write("tools/bisect.py", "import os; os._exit(0)\n")
        self.stage(
            "tests/test_thing.py", BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two")
        )
        self.plumb_commit("Park a test")
        pushed = subprocess.run(
            ["git", "push", "origin", "main"], cwd=self.repo.root, env=self.repo.env, capture_output=True, text=True
        )
        self.assertNotEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertIn("skip-added", pushed.stdout + pushed.stderr)

    LEAK = "192.168." + "1.20"

    def drop_a_stand_in_for_json(self):
        """tools/json.py is not committed, so only the working tree has it; privacy_scan.py imports json."""
        self.repo.write("tools/json.py", "import os; os._exit(0)\n")

    def assert_the_stand_in_would_work(self, *args):
        """Control: started the plain way, the privacy scanner exits 0 and says nothing, so the stand-in
        is live and a refusal by the hook is because of isolated mode."""
        done = subprocess.run(
            ["python3", "-B", "tools/privacy_scan.py", *args],
            cwd=self.repo.root,
            env=self.repo.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_pre_commit_privacy_scan_is_not_fooled_by_a_module_dropped_next_to_the_scanner(self):
        self.drop_a_stand_in_for_json()
        before = self.head()
        self.stage("src/a.py", f"host = '{self.LEAK}'\n")
        code, out = self.commit("Add a host")
        self.assertNotEqual(code, 0, out)
        self.assertIn("src/a.py:1: lan_ip", out)
        self.assertEqual(self.head(), before)
        self.assert_the_stand_in_would_work("--identity", "--staged")

    def test_commit_msg_privacy_scan_is_not_fooled_by_a_module_dropped_next_to_the_scanner(self):
        self.drop_a_stand_in_for_json()
        before = self.head()
        self.stage("src/a.py", "x = 2\n")
        message = self.repo.root / "message.txt"
        message.write_text(f"Contact {self.LEAK} about it\n", encoding="utf-8")
        code, out = self.commit(f"Contact {self.LEAK} about it")
        self.assertNotEqual(code, 0, out)
        self.assertIn("lan_ip", out)
        self.assertEqual(self.head(), before)
        self.assert_the_stand_in_would_work("--message", str(message))

    def test_pre_push_privacy_scan_is_not_fooled_by_a_module_dropped_next_to_the_scanner(self):
        self.repo.git("push", "-q", "origin", "main")
        self.drop_a_stand_in_for_json()
        self.stage("src/a.py", f"host = '{self.LEAK}'\n")
        self.plumb_commit("Add a host")  # plumbing runs no hook, so only the push check is under test
        pushed = subprocess.run(
            ["git", "push", "origin", "main"], cwd=self.repo.root, env=self.repo.env, capture_output=True, text=True
        )
        self.assertNotEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertIn("src/a.py:1: lan_ip", pushed.stdout + pushed.stderr)
        self.assert_the_stand_in_would_work("--history")

    def test_pre_push_refuses_an_unjustified_commit_and_accepts_it_once_justified(self):
        self.repo.git("push", "-q", "origin", "main")
        self.stage(
            "tests/test_thing.py", BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two")
        )
        # The commit-msg hook would refuse this commit; write it with plumbing, which runs no
        # hook, so that only the push check is under test.
        self.plumb_commit("Park a test")
        pushed = subprocess.run(
            ["git", "push", "origin", "main"], cwd=self.repo.root, env=self.repo.env, capture_output=True, text=True
        )
        self.assertNotEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertIn("skip-added", pushed.stdout + pushed.stderr)
        self.plumb_commit(f"Park a test\n\n{REASON}", parent="HEAD~1")
        pushed = subprocess.run(
            ["git", "push", "origin", "main"], cwd=self.repo.root, env=self.repo.env, capture_output=True, text=True
        )
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)


    def test_pre_push_of_a_new_branch_judges_it_by_the_overlay_of_the_remote_default_branch(self):
        """The hook passes the name of the remote to the scanner: a new branch is judged by the
        overlay at its merge base with that remote's default branch, not by the one at the
        parent of its oldest new commit (here the tip of a branch that dropped the overlay)."""
        overlay = ".check-integrity.json"
        note = json.dumps({"version": 1, "rules": {"skip-added": {"severity": "note"}}})
        self.stage(overlay, note)
        code, out = self.commit(f"Set the overlay\n\n{REASON}")
        self.assertEqual(code, 0, out)
        self.repo.git("push", "-q", "origin", "main")
        self.repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.repo.git("checkout", "-q", "-b", "a")
        self.repo.git("rm", "-q", "--", overlay)
        code, out = self.commit(f"Drop the overlay\n\n{REASON}")
        self.assertEqual(code, 0, out)
        self.repo.git("push", "-q", "origin", "a")
        self.repo.git("checkout", "-q", "-b", "b")
        self.stage(
            "tests/test_thing.py", BASE_TEST.replace("    def test_two", "    @unittest.skip('x')\n    def test_two")
        )
        # plumbing again: a's tip has no overlay, so the commit-msg hook would refuse this commit
        oid = self.repo.git("commit-tree", self.repo.git("write-tree"), "-p", "HEAD", "-m", "Park a test")
        self.repo.git("update-ref", "refs/heads/b", oid)
        pushed = subprocess.run(
            ["git", "push", "origin", "b"], cwd=self.repo.root, env=self.repo.env, capture_output=True, text=True
        )
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertIn("skip-added note", pushed.stdout + pushed.stderr)


class PinnedGit(RepoCase):
    """The diff the scanner reads must not depend on the clone it runs in: attributes, local
    configuration and environment of the user are not part of the change under judgment."""

    def setUp(self):
        super().setUp()
        self.repo.commit(tests__test_thing_py=BASE_TEST, tests__pkg__test_inner_py=BASE_TEST, src__a_py="x = 1\n")
        self.skipped = skipped(BASE_TEST)

    def findings(self, *scan_args, cwd=None):
        code, out, err = self.repo.scan(*scan_args, "--json", cwd=cwd)
        self.assertIn(code, (0, 1), out + err)
        return code, {(f["path"], f["line"], f["rule"]) for f in json.loads(out)["findings"]}

    SKIP_FOUND = (1, {("tests/test_thing.py", 4, "skip-added")})

    def test_a_committed_gitattributes_cannot_hide_a_change_with_minus_diff(self):
        self.repo.commit(**{"tests__.gitattributes": "*.py -diff\n"}, tests__test_thing_py=self.skipped)
        self.assertEqual(self.findings("--commit", "HEAD"), (1, {("tests/test_thing.py", 4, "skip-added")}))

    def test_attributes_only_in_the_local_info_file_cannot_hide_a_change_either(self):
        self.repo.write(".git/info/attributes", "* -diff\n")
        self.repo.write("tests/test_thing.py", self.skipped)
        self.repo.git("add", "-A")
        self.assertEqual(self.findings("--staged"), self.SKIP_FOUND)
        self.repo.git("commit", "-q", "-m", "Park")
        self.assertEqual(self.findings("--commit", "HEAD"), self.SKIP_FOUND)

    def test_a_driver_that_marks_a_file_binary_cannot_hide_it(self):
        self.repo.write(".git/info/attributes", "*.py diff=quiet\n")
        self.repo.git("config", "diff.quiet.binary", "true")
        self.repo.commit(tests__test_thing_py=self.skipped)
        self.assertEqual(self.findings("--commit", "HEAD"), self.SKIP_FOUND)

    def test_textconv_and_external_diff_drivers_cannot_hide_it(self):
        self.repo.write(".git/info/attributes", "*.py diff=quiet\n")
        for key in ("diff.quiet.textconv", "diff.quiet.command", "diff.external"):
            with self.subTest(key=key):
                self.repo.git("config", key, "true")
                self.repo.commit(tests__test_thing_py=self.skipped)
                self.assertEqual(self.findings("--commit", "HEAD"), self.SKIP_FOUND)
                self.repo.git("config", "--unset", key)
                self.repo.commit(tests__test_thing_py=BASE_TEST)

    def conflicted_workflow(self):
        """A merge of two sides that changed one CI line, resolved by keeping both."""
        path = ".github__workflows__ci_yml"
        self.repo.commit("Add the workflow", **{path: "steps:\n  - run: make test\n"})
        base = self.repo.git("rev-parse", "HEAD")
        self.repo.git("checkout", "-q", "-b", "side", base)
        self.repo.commit("Side", **{path: "steps:\n  - run: make test-a\n"})
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Main", **{path: "steps:\n  - run: make test-b\n"})
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "side", check=False)
        self.repo.commit("Merge side", **{path: "steps:\n  - run: make test-b\n  - run: make test-a\n"})

    def test_the_conflict_style_a_user_configures_does_not_change_the_verdict_on_a_merge(self):
        for style in ("merge", "diff3", "zdiff3"):
            with self.subTest(style=style):
                self.setUp()
                self.repo.git("config", "merge.conflictStyle", style)
                self.conflicted_workflow()
                code, out, err = self.repo.scan("--commit", "HEAD")
                self.assertEqual((code, out.strip()), (0, "check-integrity: ok (1 commits, 1 files)"), err)

    def test_relative_paths_asked_for_in_the_configuration_do_not_change_the_paths(self):
        self.repo.git("config", "diff.relative", "true")
        self.repo.commit(tests__test_thing_py=self.skipped, src__a_py="x = f()  # noqa\n")
        code, found = self.findings("--commit", "HEAD", cwd=self.repo.root / "tests")
        self.assertEqual(
            (code, {(path, rule) for path, _, rule in found}),
            (1, {("tests/test_thing.py", "skip-added"), ("src/a.py", "suppression-added")}),
        )

    def test_the_scan_judges_the_same_from_a_subfolder_of_the_repository(self):
        self.repo.commit(tests__test_thing_py=self.skipped)
        self.repo.git("mv", "tests/pkg/test_inner.py", "tests/pkg/test_moved.py")
        self.repo.write("tests/test_thing.py", BASE_TEST)
        self.repo.git("add", "-A")
        self.assertEqual(self.findings("--staged", cwd=self.repo.root / "tests" / "pkg"), (0, set()))
        self.repo.git("commit", "-q", "-m", "Tidy")
        self.repo.commit(tests__test_thing_py=self.skipped)
        self.assertEqual(self.findings("--commit", "HEAD", cwd=self.repo.root / "src"), self.SKIP_FOUND)
        self.assertEqual(self.findings("--range", "HEAD~1..HEAD", cwd=self.repo.root / "tests"), self.SKIP_FOUND)

    def test_prefix_algorithm_and_hunk_settings_do_not_change_the_findings(self):
        body = "".join(f"line {n}\n" for n in range(1, 30))
        self.repo.commit(src__b_py=body, tests__test_other_py=BASE_TEST)
        edited = body.replace("line 3\n", "line 3  # type: ignore\n").replace("line 6\n", "line 6  # noqa\n")
        self.repo.commit(src__b_py=edited, tests__test_other_py=skipped(BASE_TEST))
        expected = self.findings("--commit", "HEAD")
        self.assertEqual(expected[0], 1)
        for key, value in (
            ("diff.noprefix", "true"),
            ("diff.mnemonicPrefix", "true"),
            ("diff.srcPrefix", "x/"),
            ("diff.algorithm", "patience"),
            ("diff.algorithm", "histogram"),
            ("diff.interHunkContext", "8"),
            ("diff.indentHeuristic", "false"),
            ("core.quotePath", "true"),
            ("diff.renames", "copies"),
            ("log.showSignature", "true"),
            ("color.ui", "always"),
        ):
            with self.subTest(key=key, value=value):
                self.repo.git("config", key, value)
                self.assertEqual(self.findings("--commit", "HEAD"), expected)
                self.repo.git("config", "--unset", key)

    BLANKS_BEFORE = {
        "docs__a_md": "one\n\ntwo\n",
        "src__b_py": "".join(f"line {n}\n" for n in range(1, 13)),
        "src__ord_a_py": "a = 1\n",
        "src__ord_z_py": "z = 1\n",
    }

    def blank_context_change(self):
        """One commit with a skip (block), suppressions (notes) in several files, and unchanged
        blank lines inside the context of the changes; then a second such change staged on top."""
        self.repo.commit(**self.BLANKS_BEFORE)
        body = self.BLANKS_BEFORE["src__b_py"]
        self.repo.commit(
            docs__a_md="one\n\ntwo\nthree\n",
            src__b_py=body.replace("line 3\n", "line 3  # noqa\n\nline 3b\n"),
            src__ord_a_py="a = 1  # noqa\n",
            src__ord_z_py="z = 1  # noqa\n",
            tests__test_thing_py=self.skipped,
        )
        self.repo.write("tests/pkg/test_inner.py", self.skipped)
        self.repo.write("docs/a.md", "one\n\ntwo\nthree\nfour\n")
        self.repo.write("src/b.py", body.replace("line 3\n", "line 3  # noqa\n\nline 3b\n").replace("line 9\n", "\nline 9  # type: ignore\n"))
        self.repo.git("add", "-A")

    def reports(self):
        """The whole --json output and exit code of the last commit and of the staged change."""
        return {
            "commit": self.repo.scan("--commit", "HEAD", "--json"),
            "staged": self.repo.scan("--staged", "--json"),
        }

    def test_a_hostile_local_setting_does_not_change_the_json_report(self):
        order_file = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-order.txt"
        order_file.write_text("src/ord_z.py\nsrc/ord_a.py\n", encoding="utf-8")
        self.addCleanup(order_file.unlink)
        self.blank_context_change()
        expected = self.reports()
        for where, (code, out, err) in expected.items():
            self.assertEqual((code, err), (1, ""), where + out)
            self.assertEqual(
                {(f["rule"], f["severity"]) for f in json.loads(out)["findings"]},
                {("skip-added", "block"), ("suppression-added", "note")},
                where,
            )
        hostile = (
            ("diff.suppressBlankEmpty", "true"),
            ("diff.noprefix", "true"),
            ("diff.mnemonicPrefix", "true"),
            ("diff.srcPrefix", "x/"),
            ("diff.dstPrefix", "y/"),
            ("diff.context", "0"),
            ("diff.context", "20"),
            ("diff.interHunkContext", "5"),
            ("diff.renames", "false"),
            ("diff.renames", "copies"),
            ("diff.renameLimit", "1"),
            ("diff.algorithm", "patience"),
            ("diff.algorithm", "histogram"),
            ("diff.algorithm", "minimal"),
            ("diff.indentHeuristic", "false"),
            ("diff.orderFile", str(order_file)),
            ("diff.colorMoved", "zebra"),
            ("diff.wsErrorHighlight", "all"),
            ("diff.submodule", "log"),
            ("color.diff", "always"),
            ("color.ui", "always"),
            ("core.quotePath", "false"),
            ("core.quotePath", "true"),
            ("core.abbrev", "4"),
            ("core.bigFileThreshold", "1"),
            ("core.autocrlf", "true"),
            ("core.whitespace", "trailing-space"),
            ("core.pager", "false"),
            ("merge.conflictStyle", "zdiff3"),
            ("merge.renames", "false"),
            ("merge.renormalize", "true"),
            ("log.showSignature", "true"),
            ("log.diffMerges", "separate"),
            ("i18n.logOutputEncoding", "ISO-8859-1"),
            ("format.pretty", "oneline"),
        )
        for key, value in hostile:
            with self.subTest(key=key, value=value):
                self.repo.git("config", key, value)
                try:
                    self.assertEqual(self.reports(), expected)
                finally:
                    self.repo.git("config", "--unset", key)

    def test_git_prints_the_space_of_a_blank_context_line_whatever_the_configuration_says(self):
        self.repo.commit(docs__a_md="one\n\ntwo\n")
        self.repo.commit(docs__a_md="one\n\ntwo\nthree\n")
        self.repo.git("config", "diff.suppressBlankEmpty", "true")
        plain = subprocess.run(
            ["git", "-C", str(self.repo.root), "diff", "-U3", "HEAD~1", "HEAD"],
            env=self.repo.env, capture_output=True, check=True,
        ).stdout
        self.assertIn(b" one\n\n two\n", plain, "the setting must act on a plain git diff for this test to mean anything")
        pinned = ci.run_git(["-C", str(self.repo.root), "diff", "-U3", "HEAD~1", "HEAD"]).stdout
        self.assertIn(b" one\n \n two\n", pinned)

    def test_a_patch_with_blank_context_lines_that_lost_their_space_still_parses(self):
        header = b"diff --git a/docs/a.md b/docs/a.md\nindex 1111111..2222222 100644\n--- a/docs/a.md\n+++ b/docs/a.md\n"
        for label, hunk, added in (
            ("blank line in the middle", b"@@ -1,3 +1,4 @@\n one\n\n two\n+three\n", [(4, "three")]),
            ("blank line last in the hunk", b"@@ -1,3 +1,4 @@\n one\n+new\n two\n\n", [(2, "new")]),
            ("blank line first in the hunk", b"@@ -1,3 +1,4 @@\n\n one\n+new\n two\n", [(3, "new")]),
            ("a blank line with its space", b"@@ -1,3 +1,4 @@\n one\n \n two\n+three\n", [(4, "three")]),
        ):
            with self.subTest(label):
                diff = ci.parse_patch("docs/a.md", header + hunk)
                self.assertEqual([(line.number, line.text) for line in diff.added], added)
                self.assertEqual(diff.removed, [])

    def test_a_local_replace_ref_does_not_change_what_the_scanner_reads(self):
        self.repo.commit(src__b_py="x = f()  # noqa\n")
        expected = self.findings("--commit", "HEAD")
        self.assertEqual(expected, (0, {("src/b.py", 1, "suppression-added")}))
        self.repo.write("clean.txt", "x = f()\n")
        clean = self.repo.git("hash-object", "-w", "clean.txt")
        self.repo.git("replace", self.repo.git("rev-parse", "HEAD:src/b.py"), clean)
        self.assertEqual(self.findings("--commit", "HEAD"), expected)

    def test_the_reason_is_counted_on_the_message_git_stores_whatever_encoding_it_is_asked_to_print(self):
        reason = "é" * 12  # 12 characters, 20 are needed; as GBK bytes the same text is 24
        self.repo.commit(f"Park a test\n\nCheck-change: {reason}", tests__test_thing_py=self.skipped)
        self.assertEqual(self.findings("--commit", "HEAD"), self.SKIP_FOUND)
        stored = subprocess.run(
            ["git", "-C", str(self.repo.root), "log", "-1", "--format=%B"],
            env=self.repo.env, capture_output=True, check=True,
        ).stdout
        for encoding in ("GBK", "ISO-8859-1", "UTF-16"):
            with self.subTest(encoding):
                self.repo.git("config", "i18n.logOutputEncoding", encoding)
                printed = subprocess.run(
                    ["git", "-C", str(self.repo.root), "log", "-1", "--format=%B"],
                    env=self.repo.env, capture_output=True, check=True,
                ).stdout
                if printed == stored:
                    # without this the test would pass whatever the scanner does
                    self.skipTest(f"this git does not re-encode a log message as {encoding}")
                self.assertEqual(self.findings("--commit", "HEAD"), self.SKIP_FOUND)

    def test_renormalizing_merges_does_not_change_what_a_merge_is_compared_with(self):
        self.repo.commit(norm_txt="a\nb\nc\n", src__m_py="m = 1\n")
        base = self.repo.git("rev-parse", "HEAD")
        self.repo.git("checkout", "-q", "-b", "side", base)
        self.repo.commit("Side", norm_txt=b"a\r\nb\r\nc\r\n")
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Main", norm_txt="a\nB\nc\n")
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "side", check=False)
        self.repo.commit("Merge side", norm_txt="a\nB\nc\n", src__m_py="m = 1  # noqa\n")
        expected = self.repo.scan("--commit", "HEAD", "--json")
        self.assertEqual(expected[0], 0, expected)
        self.repo.git("config", "merge.renormalize", "true")
        self.repo.git("config", "core.autocrlf", "true")
        self.assertEqual(self.repo.scan("--commit", "HEAD", "--json"), expected)

    def test_environment_variables_that_change_git_diff_are_not_passed_on(self):
        self.repo.git("rm", "-q", "-r", "tests/pkg")
        self.repo.commit(tests__pkg="now a file\n", tests__test_thing_py=self.skipped)
        expected = self.findings("--commit", "HEAD")
        self.assertEqual(
            expected,
            (1, {("tests/pkg/test_inner.py", 0, "test-removed"), ("tests/test_thing.py", 4, "skip-added")}),
        )
        for name, value in (
            ("GIT_LITERAL_PATHSPECS", "1"),
            ("GIT_GLOB_PATHSPECS", "1"),
            ("GIT_NOGLOB_PATHSPECS", "1"),
            ("GIT_ICASE_PATHSPECS", "1"),
            ("GIT_EXTERNAL_DIFF", "true"),
            ("GIT_DIFF_OPTS", "--unified=9"),
        ):
            with self.subTest(name=name):
                self.repo.env[name] = value
                self.assertEqual(self.findings("--commit", "HEAD"), expected)
                del self.repo.env[name]


class UnreadableChanges(RepoCase):
    """What the scanner cannot read is a finding, never a crash (exit 2) and never a pass."""

    def setUp(self):
        super().setUp()
        self.repo.commit(
            tests__test_thing_py=BASE_TEST,
            tests__pkg__test_inner_py=BASE_TEST,
            docs__guide_md="# Guide\n",
            src__a_py="x = 1\n",
        )

    def findings(self, *scan_args):
        code, out, err = self.repo.scan(*scan_args, "--json")
        self.assertIn(code, (0, 1), out + err)
        return code, {(f["path"], f["rule"], f["justified"]) for f in json.loads(out)["findings"]}

    def symlink(self, path, target="README.md"):
        link = self.repo.root / path
        link.unlink()
        os.symlink(target, link)

    def test_a_test_file_replaced_by_a_symlink_counts_as_a_deleted_test_in_every_mode(self):
        self.symlink("tests/test_thing.py")
        self.repo.git("add", "-A")
        deleted = (1, {("tests/test_thing.py", "test-removed", False)})
        self.assertEqual(self.findings("--staged"), deleted)
        self.repo.git("commit", "-q", "-m", "Link the test")
        self.assertEqual(self.findings("--commit", "HEAD"), deleted)
        self.assertEqual(self.findings("--range", "HEAD~1..HEAD"), deleted)
        self.repo.git("commit", "-q", "--amend", "-m", f"Link the test\n\n{REASON}")
        self.assertEqual(self.findings("--commit", "HEAD"), (0, {("tests/test_thing.py", "test-removed", True)}))

    def test_a_document_swapped_for_a_symlink_and_back_has_no_finding(self):
        self.symlink("docs/guide.md")
        self.repo.commit("Link the guide")
        self.assertEqual(self.findings("--commit", "HEAD"), (0, set()))
        (self.repo.root / "docs/guide.md").unlink()
        self.repo.commit("A real guide again", docs__guide_md="# Guide\n")
        self.assertEqual(self.findings("--commit", "HEAD"), (0, set()))

    def test_a_file_replaced_by_a_directory_of_the_same_name_is_judged_file_by_file(self):
        (self.repo.root / "tests/test_thing.py").unlink()
        self.repo.write("tests/test_thing.py/inner.py", "y = f()  # type: ignore\n")
        self.repo.write("tests/test_thing.py/notes.txt", "n\n")
        self.repo.git("add", "-A")
        expected = (
            1,
            {
                ("tests/test_thing.py", "test-removed", False),
                ("tests/test_thing.py/inner.py", "suppression-added", False),
            },
        )
        self.assertEqual(self.findings("--staged"), expected)
        self.repo.git("commit", "-q", "-m", "Make it a folder")
        self.assertEqual(self.findings("--commit", "HEAD"), expected)

    def test_a_file_that_git_sees_moved_into_the_directory_that_replaces_it_is_a_rename(self):
        (self.repo.root / "tests/test_thing.py").unlink()
        self.repo.write("tests/test_thing.py/test_inner.py", skipped(BASE_TEST))
        self.repo.write("tests/test_thing.py/notes.txt", "n\n")
        self.repo.commit("Move it inside")
        self.assertEqual(
            self.findings("--commit", "HEAD"), (1, {("tests/test_thing.py/test_inner.py", "skip-added", False)})
        )

    def test_a_directory_replaced_by_a_file_of_the_same_name_is_judged_file_by_file(self):
        self.repo.git("rm", "-q", "-r", "tests/pkg")
        self.repo.commit("Make it a file", **{"tests__pkg": "text\n"})
        self.assertEqual(
            self.findings("--commit", "HEAD"), (1, {("tests/pkg/test_inner.py", "test-removed", False)})
        )

    def gitlink(self, path="libs/vendored"):
        self.repo.git("update-index", "--add", "--cacheinfo", f"160000,{self.repo.git('rev-parse', 'HEAD')},{path}")
        self.repo.git("commit", "-q", "-m", "Touch the submodule")

    def test_a_submodule_added_bumped_or_removed_is_an_unreadable_change(self):
        for step in ("add", "bump", "remove"):
            with self.subTest(step=step):
                if step == "add":
                    self.gitlink()
                elif step == "bump":
                    self.repo.commit("Another commit")
                    self.gitlink()
                else:
                    self.repo.git("rm", "-q", "--cached", "libs/vendored")
                    self.repo.git("commit", "-q", "-m", "Drop the submodule")
                self.assertEqual(
                    self.findings("--commit", "HEAD"), (1, {("libs/vendored", "unreadable-change", False)})
                )
        self.repo.git("commit", "-q", "--amend", "-m", f"Drop the submodule\n\n{REASON}")
        self.assertEqual(
            self.findings("--commit", "HEAD"), (0, {("libs/vendored", "unreadable-change", True)})
        )

    def git_shim(self, script):
        """A ``git`` ahead of the real one on PATH. It stands in for a git whose patch the
        scanner cannot read: the output below is a guess at such a patch, not a recording."""
        shim = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-shim"
        shim.mkdir()
        self.addCleanup(lambda: shutil.rmtree(shim, ignore_errors=True))
        (shim / "git").write_text(
            "#!/bin/sh\n"
            'case " $* " in\n'
            '  *" -p "*"literal)docs/guide.md"*)\n'
            f"{script}\n"
            "    exit 0;;\n"
            "esac\n"
            f'exec {shlex.quote(shutil.which("git"))} "$@"\n'
        )
        (shim / "git").chmod(0o755)
        self.repo.env["PATH"] = f"{shim}{os.pathsep}{self.repo.env['PATH']}"

    def test_a_patch_that_does_not_parse_is_a_finding_on_that_file_only(self):
        self.repo.commit(docs__guide_md="# Guide\nmore\n", src__a_py="x = f()  # noqa\n")
        self.git_shim("    printf 'diff --git a/docs/guide.md b/docs/guide.md\\n@@ nonsense @@\\n'")
        code, found = self.findings("--commit", "HEAD")
        self.assertEqual(
            (code, found),
            (1, {("docs/guide.md", "unreadable-change", False), ("src/a.py", "suppression-added", False)}),
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertIn("docs/guide.md:0 unreadable-change", out)
        self.assertIn("cannot parse diff of docs/guide.md", out)
        self.repo.git("commit", "-q", "--amend", "-m", f"Edit\n\n{REASON}")
        self.assertEqual(self.findings("--commit", "HEAD")[0], 0)

    def test_a_patch_holding_more_than_one_file_is_unreadable_even_if_it_would_parse(self):
        self.repo.commit(docs__guide_md="# Guide\nmore\n")
        self.git_shim(
            "    printf 'diff --git a/docs/guide.md b/docs/guide.md\\nold mode 100644\\nnew mode 100755\\n"
            "diff --git a/docs/other.md b/docs/other.md\\n@@ -0,0 +1 @@\\n+x\\n'"
        )
        self.assertEqual(
            self.findings("--commit", "HEAD"), (1, {("docs/guide.md", "unreadable-change", False)})
        )

    def test_the_overlay_cannot_lower_the_unreadable_change_rule(self):
        overlay = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-overlay.json"
        self.addCleanup(lambda: overlay.unlink(missing_ok=True))
        for change in ({"severity": "note"}, {"allow": ["x"]}, {"exempt_paths": ["*"]}):
            with self.subTest(change=change):
                overlay.write_text(json.dumps({"rules": {"unreadable-change": change}}))
                # the overlay is rejected, so nothing it says takes effect: the finding stands
                code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(overlay))
                self.assertEqual(code, 1, out)
                self.assertIn("tautology-added", out)
                self.assertIn("unreadable-change", err)
                self.assertIn("cannot", err)
                self.assertIn("every rule is judged as block", err)


MACHINERY = {
    ".githooks/pre-push": "#!/bin/sh\nset -eu\npython3 tools/check_integrity.py --pre-push\n",
    ".githooks/commit-msg": "#!/bin/sh\nset -eu\npython3 tools/check_integrity.py --staged\n",
    ".husky/pre-commit": "npm test\n",
    ".pre-commit-config.yaml": "repos: []\n",
    ".github/workflows/ci.yml": WORKFLOW,
    ".gitlab-ci.yml": "test:\n  script: make test\n",
    "tools/check_integrity.py": "def judge_change(diffs, rules):\n    return []\n",
    "tools/check_integrity_rules.json": '{"version": 1}\n',
    "tools/privacy_scan.py": "RULES = 1\n",
    "tools/privacy_rules.json": '{"rules": []}\n',
}
MACHINERY_RULE = "check-machinery-changed"
LOOSEN_RULE = "check-file-weakened"


class MachineryChanged(RepoCase):
    """The files that run the checks are judged as a whole: any change to one of them is a
    finding, whatever its lines look like."""

    def setUp(self):
        super().setUp()
        self.put("Seed", **MACHINERY)

    def put(self, message, **files):
        """Write files (path -> text, or None to delete) and commit them under that message."""
        for path, content in files.items():
            if content is None:
                self.repo.git("rm", "-q", "--", path)
            else:
                self.repo.write(path, content)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", message)

    def machinery_hits(self, *scan_args):
        code, out, err = self.repo.scan(*(scan_args or ("--commit", "HEAD")), "--json")
        self.assertIn(code, (0, 1), out + err)
        return code, [f for f in json.loads(out)["findings"] if f["rule"] == MACHINERY_RULE]

    def test_a_line_added_to_a_check_file_is_a_finding_in_every_machinery_file(self):
        for path, content in MACHINERY.items():
            with self.subTest(path=path):
                self.put("Add a line", **{path: content + "extra\n"})
                code, found = self.machinery_hits()
                self.assertEqual((code, [(f["path"], f["justified"]) for f in found]), (1, [(path, False)]))
                self.assertEqual(found[0]["severity"], "block")

    def test_the_ways_to_switch_a_hook_and_a_ci_step_off_are_all_findings(self):
        hook, workflow, scanner = ".githooks/commit-msg", ".github/workflows/ci.yml", "tools/check_integrity.py"
        edits = {
            "exit 0 inserted": {hook: MACHINERY[hook].replace("set -eu\n", "set -eu\nexit 0\n")},
            "a colon in front": {hook: MACHINERY[hook].replace("python3", ": python3")},
            "an if on the step": {
                workflow: WORKFLOW.replace("      - run: make test\n", "      - if: github.event_name == 'x'\n        run: make test\n")
            },
            "echo in front": {workflow: WORKFLOW.replace("make test", "echo make test")},
            "the scanner returns early": {scanner: MACHINERY[scanner].replace("    return []\n", "    return []\n    return 1\n")},
        }
        for name, change in edits.items():
            with self.subTest(name):
                self.put("Edit", **change)
                code, found = self.machinery_hits()
                self.assertEqual((code, [f["path"] for f in found]), (1, list(change)))
                self.put("Back", **{path: MACHINERY[path] for path in change})

    def test_a_removed_line_is_a_finding_next_to_the_rule_that_reports_it_by_line(self):
        self.put("Drop a step", **{".github/workflows/ci.yml": WORKFLOW.replace("      - run: make lint\n", "")})
        self.assertEqual(
            {(path, rule) for path, _, rule, _ in self.rules_at_head()},
            {(".github/workflows/ci.yml", LOOSEN_RULE), (".github/workflows/ci.yml", MACHINERY_RULE)},
        )

    def test_a_change_of_the_permission_bits_is_a_finding(self):
        for path in (".githooks/pre-push", ".githooks/commit-msg"):
            for mode, sign in (("100755", "+"), ("100644", "-")):
                with self.subTest(path=path, mode=mode):
                    self.repo.git("update-index", f"--chmod={sign}x", path)
                    self.repo.git("commit", "-q", "-m", "Change the mode")
                    code, found = self.machinery_hits()
                    self.assertEqual((code, [f["path"] for f in found]), (1, [path]))
                    self.assertIn(mode, found[0]["text"])

    def test_a_new_deleted_moved_or_retyped_check_file_is_a_finding(self):
        self.put("New", **{".githooks/post-commit": "#!/bin/sh\n"})
        self.assertEqual([f["path"] for f in self.machinery_hits()[1]], [".githooks/post-commit"])
        self.put("Deleted", **{".githooks/post-commit": None})
        self.assertEqual([f["path"] for f in self.machinery_hits()[1]], [".githooks/post-commit"])
        self.repo.git("mv", ".githooks/pre-push", ".githooks/pre-push.off")
        self.repo.git("commit", "-q", "-m", "Moved")
        self.assertEqual([f["path"] for f in self.machinery_hits()[1]], [".githooks/pre-push.off"])
        (self.repo.root / ".githooks/commit-msg").unlink()
        os.symlink("pre-push.off", self.repo.root / ".githooks/commit-msg")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "Linked")
        self.assertEqual([f["path"] for f in self.machinery_hits()[1]], [".githooks/commit-msg"])

    def test_a_file_moved_into_the_machinery_or_out_of_it_is_a_finding(self):
        self.put("A tool", **{"tools/runner.py": "print('x')\n"})
        self.repo.git("mv", "tools/runner.py", ".githooks/runner.py")
        self.repo.git("commit", "-q", "-m", "Into the hooks")
        self.assertEqual([f["path"] for f in self.machinery_hits()[1]], [".githooks/runner.py"])
        self.repo.git("mv", ".githooks/runner.py", "tools/runner.py")
        self.repo.git("commit", "-q", "-m", "Out of the hooks")
        self.assertEqual([f["path"] for f in self.machinery_hits()[1]], ["tools/runner.py"])

    def test_it_reports_each_file_once_however_many_lines_changed(self):
        self.put("Many", **{".githooks/pre-push": MACHINERY[".githooks/pre-push"] + "a\nb\nc\n", ".husky/pre-commit": "npm run lint\n"})
        _, found = self.machinery_hits()
        self.assertEqual(sorted(f["path"] for f in found), [".githooks/pre-push", ".husky/pre-commit"])

    def test_a_reason_line_makes_the_finding_pass(self):
        self.put(f"Tidy\n\n{REASON}", **{".githooks/pre-push": MACHINERY[".githooks/pre-push"] + "exit 0\n"})
        code, found = self.machinery_hits()
        self.assertEqual((code, [f["justified"] for f in found]), (0, [True]))

    def test_other_files_and_lint_configs_are_not_machinery(self):
        self.put("Configs", **{"ruff.toml": "line-length = 100\n", "pytest.ini": "[pytest]\n"})
        self.put("More", **{"ruff.toml": "line-length = 90\n", "pytest.ini": "[pytest]\naddopts = -q\n", "src/a.py": "x = 1\n", "docs/guide.md": "# g\n"})
        self.assertEqual(self.machinery_hits()[1], [])

    def test_the_overlay_can_lower_or_exempt_the_rule(self):
        hook = ".githooks/pre-push"
        cases = (
            ({"severity": "note"}, (0, ["note"])),
            ({"exempt_paths": [".githooks/*"]}, (0, [])),
        )
        for change, expected in cases:
            with self.subTest(change=change):
                self.put("Overlay", **{".check-integrity.json": json.dumps({"rules": {MACHINERY_RULE: change}})})
                self.put("Hook", **{hook: MACHINERY[hook] + "exit 0\n"})
                code, found = self.machinery_hits()
                self.assertEqual((code, [f["severity"] for f in found]), expected)
                self.put("Back", **{hook: MACHINERY[hook], ".check-integrity.json": None})

    def test_the_overlay_cannot_allow_lines_of_a_rule_that_judges_whole_files(self):
        """allow was accepted for such a rule and did nothing: a setting that looks like a
        relief and is none. It is an invalid overlay now, and the mixed rule test-removed, which
        also has a net-loss entry that reads lines, keeps its allow."""
        hook = ".githooks/pre-push"
        explicit = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-allow-overlay.json"
        self.addCleanup(lambda: explicit.unlink(missing_ok=True))
        explicit.write_text(json.dumps({"rules": {MACHINERY_RULE: {"allow": ["x"]}}}))
        self.put("Hook", **{hook: MACHINERY[hook] + "exit 0\n"})
        code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(explicit))
        self.assertEqual(code, 1, out)  # rejected, so the hook change is judged as a block
        self.assertIn(MACHINERY_RULE, out)
        self.assertIn(MACHINERY_RULE, err)
        self.assertIn("no line to allow", err)
        self.assertIn("every rule is judged as block", err)
        document = json.loads(RULES.read_text())
        document["rules"].append({"id": "gone", "kind": "deleted", "classes": ["test"], "why": "w"})
        custom = Path(self._tmp.name) / "custom-rules.json"
        custom.write_text(json.dumps(document))
        with self.assertRaisesRegex(ci.OverlayError, "no line to allow"):
            ci.parse_overlay('{"rules": {"gone": {"allow": ["x"]}}}', "overlay", ci.load_rules(custom))
        explicit.write_text(json.dumps({"rules": {"test-removed": {"allow": ["x"]}}}))
        code, out, err = self.repo.scan("--commit", "HEAD", "--overlay", str(explicit))
        self.assertEqual(code, 1, out + err)  # accepted: the hook change is still the finding
        self.put("Overlay", **{".check-integrity.json": json.dumps({"rules": {MACHINERY_RULE: {"allow": ["x"]}}})})
        self.put("Hook again", **{hook: MACHINERY[hook]})
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1, out + err)
        self.assertIn("no line to allow", err)

    def custom_rules(self, machinery):
        document = json.loads(RULES.read_text())
        document["classes"]["machinery"] = machinery
        path = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-rules.json"
        path.write_text(json.dumps(document))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_a_repository_defines_its_machinery_by_the_class_in_its_own_rules_table(self):
        rules = self.custom_rules(["^Makefile$"])
        self.put("Both", **{"Makefile": "all:\n\ttrue\n", ".githooks/pre-push": MACHINERY[".githooks/pre-push"] + "# x\n"})
        code, out, err = self.repo.scan("--commit", "HEAD", "--rules", str(rules), "--json")
        found = [f["path"] for f in json.loads(out)["findings"] if f["rule"] == MACHINERY_RULE]
        self.assertEqual(found, ["Makefile"], out + err)

    def test_existing_only_on_a_changed_rule_spares_the_files_the_change_adds(self):
        document = json.loads(RULES.read_text())
        next(r for r in document["rules"] if r["id"] == MACHINERY_RULE)["existing_only"] = True
        rules = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-existing.json"
        rules.write_text(json.dumps(document))
        self.addCleanup(lambda: rules.unlink(missing_ok=True))
        self.put("Both", **{".githooks/post-commit": "#!/bin/sh\n", ".husky/pre-commit": "npm run lint\n"})
        _, out, err = self.repo.scan("--commit", "HEAD", "--rules", str(rules), "--json")
        found = [f["path"] for f in json.loads(out)["findings"] if f["rule"] == MACHINERY_RULE]
        self.assertEqual(found, [".husky/pre-commit"], out + err)

    def test_a_rule_of_the_changed_kind_takes_no_patterns(self):
        document = json.loads(RULES.read_text())
        rule = next(r for r in document["rules"] if r["id"] == MACHINERY_RULE)
        rule["patterns"] = ["x"]
        bad = Path(self._tmp.name) / "bad.json"
        bad.write_text(json.dumps(document))
        with self.assertRaisesRegex(ci.IntegrityError, "changed"):
            ci.load_rules(bad)

    def test_the_default_table_ties_the_machinery_to_the_hook_and_ci_classes_and_the_scanner(self):
        rules = ci.load_rules(RULES)
        patterns = lambda name: {p.pattern for p in rules.classes[name]}
        scanner = {
            r"(^|/)tools/check_integrity\.py$",
            r"(^|/)tools/check_integrity_rules\.json$",
            r"(^|/)tools/privacy_scan\.py$",
            r"(^|/)tools/privacy_rules\.json$",
        }
        self.assertEqual(patterns("machinery"), patterns("bypass") | scanner)
        rule = next(r for r in rules.rules if r.id == MACHINERY_RULE)
        self.assertEqual((rule.kind, rule.severity, rule.classes), ("changed", "block", ("machinery",)))


class LintConfigLoosened(RepoCase):
    """The lint and test configuration files get the loosening check that pyproject.toml has."""

    FILES = {
        "ruff.toml": ("line-length = 100\n", 'ignore = ["E", "F"]\n'),
        ".ruff.toml": ("line-length = 100\n", 'extend-ignore = ["E"]\n'),
        "pytest.ini": ("[pytest]\ntestpaths = tests\n", "addopts = --ignore=tests\n"),
        "setup.cfg": ("[metadata]\nname = demo\n", "[flake8]\nignore = E501\n"),
        "tox.ini": ("[tox]\nenvlist = py\n", "[pytest]\naddopts = --deselect tests/a.py\n"),
        ".flake8": ("[flake8]\nmax-line-length = 100\n", "extend-ignore = E203\n"),
        "mypy.ini": ("[mypy]\nwarn_unused_ignores = True\n", "ignore_errors = True\n"),
        ".pylintrc": ("[MASTER]\njobs = 1\n", "ignore = tests\n"),
        ".coveragerc": ("[run]\nbranch = True\n", "fail_under = 0\n"),
        "deny.toml": ("[advisories]\n", 'ignore = ["RUSTSEC-0000-0000"]\n'),
        ".golangci.yml": ("run:\n  timeout: 5m\n", "  exclude: [vendor]\n"),
        "biome.json": ('{\n  "linter": {}\n}\n', '{\n  "linter": {},\n  "ignore": ["src"]\n}\n'),
        ".eslintrc.json": ('{\n  "rules": {}\n}\n', '{\n  "rules": {},\n  "ignorePatterns": ["src"]\n}\n'),
    }

    def put(self, message, path, text):
        self.repo.write(path, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", message)

    def test_a_loosening_line_added_to_an_existing_config_is_a_finding(self):
        for name, (base, loosened) in self.FILES.items():
            with self.subTest(name):
                self.put(f"Add {name}", name, base)
                self.put(f"Loosen {name}", name, loosened if loosened.startswith("{") else base + loosened)
                found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
                self.assertIn((name, LOOSEN_RULE), found)

    def test_a_loosening_key_with_a_hyphen_is_a_finding(self):
        cases = {
            ".golangci.yml": ("run:\n  timeout: 5m\n", ["  skip-dirs: [vendor]\n", "  exclude-dirs: [vendor]\n", "  skip-files: [gen.go]\n"]),
            ".pylintrc": ("[MASTER]\njobs = 1\n", ["ignore-paths = ^tests/\n", "ignore-patterns = test_\n"]),
        }
        for name, (base, lines) in cases.items():
            for line in lines:
                with self.subTest(name=name, line=line):
                    self.put(f"Add {name}", name, base)
                    self.put(f"Loosen {name}", name, base + line)
                    found = {(path, rule) for path, _, rule, _ in self.rules_at_head()}
                    self.assertIn((name, LOOSEN_RULE), found)

    def test_a_new_config_file_is_not_a_loosening(self):
        self.repo.commit(ruff_toml='ignore = ["E"]\n')
        self.assertEqual(self.rules_at_head(), set())

    def test_the_default_table_applies_the_loosening_rule_to_the_lint_class_too(self):
        rules = ci.load_rules(RULES)
        loosen = next(r for r in rules.rules if r.id == LOOSEN_RULE and r.kind == "added")
        self.assertEqual(set(loosen.classes), {"mixed", "lint-config"})
        for path in ("ruff.toml", "pytest.ini", "setup.cfg", "tox.ini", ".flake8", "mypy.ini"):
            self.assertIn("lint-config", rules.classes_of(path), path)
        self.assertNotIn("lint-config", rules.classes_of("pyproject.toml"))


SHA_OLD = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SHA_NEW = "0123456789abcdef0123456789abcdef01234567"
DIGEST_OLD = "a1" * 32
DIGEST_NEW = "b2" * 32
VERSIONED_WORKFLOW = (
    "name: ci\n"
    "on: push\n"
    "jobs:\n"
    "  test:\n"
    "    runs-on: ubuntu-24.04\n"
    "    container: python:3.12.1\n"
    "    timeout-minutes: 10\n"
    "    steps:\n"
    f"      - uses: actions/checkout@{SHA_OLD} # v7.0.1\n"
    "      - uses: actions/setup-python@v5\n"
    "        with:\n"
    "          python-version: ['3.11', '3.12']\n"
    "      - uses: actions/setup-node@v4\n"
    "        with:\n"
    "          node-version: 20\n"
    "      - uses: github/codeql-action/analyze@v3.25.1\n"
    "      - run: python -m pytest --cov-fail-under=90\n"
    "    services:\n"
    "      db:\n"
    f"        image: postgres:15@sha256:{DIGEST_OLD}\n"
)
VERSIONED_HOOKS = (
    "repos:\n"
    "  - repo: https://example.com/hooks\n"
    "    rev: v1.2.3\n"
    "    hooks:\n"
    "      - id: check\n"
    "  - repo: https://example.com/more\n"
    f"    rev: {SHA_OLD}\n"
    "    hooks:\n"
    "      - id: more\n"
)
VERSION_RULE = "version-bumped"
WORKFLOW_PATH = ".github/workflows/ci.yml"
HOOKS_PATH = ".pre-commit-config.yaml"


class VersionBumps(RepoCase):
    """A change that only moves version numbers on the version lines of a CI file is a note, not
    a block: bots (Dependabot, pre-commit autoupdate) cannot write a reason line. Every other
    change to the same lines, and every other number, stays a finding."""

    def setUp(self):
        super().setUp()
        self.put("Seed", **{WORKFLOW_PATH: VERSIONED_WORKFLOW, HOOKS_PATH: VERSIONED_HOOKS})

    def put(self, message, **files):
        for path, content in files.items():
            self.repo.write(path, content)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", message)

    @staticmethod
    def edited(text, *pairs):
        for old, new in pairs:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text

    def findings(self, *args):
        code, out, err = self.repo.scan(*(args or ("--commit", "HEAD")), "--json")
        self.assertIn(code, (0, 1), out + err)
        found = [(f["path"], f["line"], f["rule"], f["severity"], f["justified"]) for f in json.loads(out)["findings"]]
        return code, found

    def workflow_change(self, *pairs):
        self.put("Edit the workflow", **{WORKFLOW_PATH: self.edited(VERSIONED_WORKFLOW, *pairs)})
        return self.findings()

    def test_a_pinned_action_moved_to_a_new_sha_and_version_comment_is_a_note(self):
        code, found = self.workflow_change((f"actions/checkout@{SHA_OLD} # v7.0.1", f"actions/checkout@{SHA_NEW} # v7.0.2"))
        self.assertEqual((code, [(p, r, s) for p, _, r, s, _ in found]), (0, [(WORKFLOW_PATH, VERSION_RULE, "note")]))

    def test_a_runner_and_a_python_version_bump_together_are_one_note(self):
        code, found = self.workflow_change(
            ("runs-on: ubuntu-24.04", "runs-on: ubuntu-26.04"),
            ("['3.11', '3.12']", "['3.12', '3.13']"),
        )
        self.assertEqual((code, [(p, r, s) for p, _, r, s, _ in found]), (0, [(WORKFLOW_PATH, VERSION_RULE, "note")]))
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[-1], "check-integrity: ok (1 commits, 1 files, 1 note(s), not blocking)")

    def test_each_kind_of_version_line_bumped_alone_is_a_note(self):
        cases = {
            "uses tag": (WORKFLOW_PATH, VERSIONED_WORKFLOW, [("setup-python@v5", "setup-python@v6")]),
            "uses with a path": (WORKFLOW_PATH, VERSIONED_WORKFLOW, [("analyze@v3.25.1", "analyze@v3.26.0")]),
            "runs-on": (WORKFLOW_PATH, VERSIONED_WORKFLOW, [("ubuntu-24.04", "ubuntu-26.04")]),
            "container": (WORKFLOW_PATH, VERSIONED_WORKFLOW, [("python:3.12.1", "python:3.13.0")]),
            "image with a digest": (WORKFLOW_PATH, VERSIONED_WORKFLOW, [("postgres:15@sha256:" + DIGEST_OLD, "postgres:16@sha256:" + DIGEST_NEW)]),
            "a -version key with a number": (WORKFLOW_PATH, VERSIONED_WORKFLOW, [("node-version: 20", "node-version: 22")]),
            "a -version key with a list": (WORKFLOW_PATH, VERSIONED_WORKFLOW, [("['3.11', '3.12']", "['3.12', '3.13']")]),
            "pre-commit rev tag": (HOOKS_PATH, VERSIONED_HOOKS, [("rev: v1.2.3", "rev: v1.3.0")]),
            "pre-commit rev sha": (HOOKS_PATH, VERSIONED_HOOKS, [(f"rev: {SHA_OLD}", f"rev: {SHA_NEW}")]),
        }
        for name, (path, base, pairs) in cases.items():
            with self.subTest(name):
                self.put("Bump", **{path: self.edited(base, *pairs)})
                code, found = self.findings()
                self.assertEqual((code, [(p, r, s) for p, _, r, s, _ in found]), (0, [(path, VERSION_RULE, "note")]))
                self.put("Back", **{path: base})

    def test_the_version_key_of_each_runtime_and_toolchain_bumped_is_a_note(self):
        names = "python node go java ruby bun deno dotnet php pnpm uv rust gradle elixir otp terraform".split()
        for name in names:
            with self.subTest(name):
                base = VERSIONED_WORKFLOW + f"        {name}-version: '1.2'\n"
                self.put("Add the key", **{WORKFLOW_PATH: base})
                self.put("Bump", **{WORKFLOW_PATH: base.replace("'1.2'", "'1.3'")})
                code, found = self.findings()
                self.assertEqual((code, [(p, r, s) for p, _, r, s, _ in found]), (0, [(WORKFLOW_PATH, VERSION_RULE, "note")]))
                self.put("Back", **{WORKFLOW_PATH: VERSIONED_WORKFLOW})

    def test_any_other_key_ending_in_version_changed_stays_blocking(self):
        for key in ("min-tls-version", "api-version", "protocol-version", "ssl-version", "xpython-version"):
            with self.subTest(key):
                base = VERSIONED_WORKFLOW + f"        {key}: '1.3'\n"
                self.put("Add the key", **{WORKFLOW_PATH: base})
                self.put("Change", **{WORKFLOW_PATH: base.replace("'1.3'", "'1.0'")})
                code, found = self.findings()
                rules = {r for _, _, r, _, _ in found}
                self.assertEqual(code, 1, found)
                self.assertIn(MACHINERY_RULE, rules)
                self.assertNotIn(VERSION_RULE, rules)
                self.put("Back", **{WORKFLOW_PATH: VERSIONED_WORKFLOW})

    def test_two_files_of_bumps_give_one_note_each(self):
        self.put(
            "Bump both",
            **{
                WORKFLOW_PATH: self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-26.04")),
                HOOKS_PATH: self.edited(VERSIONED_HOOKS, ("rev: v1.2.3", "rev: v1.3.0")),
            },
        )
        code, found = self.findings()
        self.assertEqual((code, sorted((p, r) for p, _, r, _, _ in found)), (0, [(WORKFLOW_PATH, VERSION_RULE), (HOOKS_PATH, VERSION_RULE)]))

    def test_the_bump_is_a_note_on_the_hunks_it_has_even_when_far_apart(self):
        code, found = self.workflow_change(
            ("ubuntu-24.04", "ubuntu-26.04"), ("node-version: 20", "node-version: 22"), (f"postgres:15@sha256:{DIGEST_OLD}", f"postgres:16@sha256:{DIGEST_OLD}")
        )
        self.assertEqual((code, [r for _, _, r, _, _ in found]), (0, [VERSION_RULE]))

    def test_other_changes_to_the_same_lines_stay_blocking(self):
        cases = {
            "coverage floor to 0": [("--cov-fail-under=90", "--cov-fail-under=0")],
            "timeout to 0": [("timeout-minutes: 10", "timeout-minutes: 0")],
            "another repository's action": [(f"actions/checkout@{SHA_OLD} # v7.0.1", f"evil/checkout@{SHA_NEW} # v7.0.2")],
            "another path in the same repository": [("codeql-action/analyze@v3.25.1", "codeql-action/upload-sarif@v3.25.2")],
            "a matrix with a cell fewer": [("['3.11', '3.12']", "['3.12']")],
            "a matrix with a cell more": [("['3.11', '3.12']", "['3.11', '3.12', '3.13']")],
            "another key": [("python-version: ['3.11', '3.12']", "node-version: ['3.11', '3.12']")],
            "a runner that is not a version": [("ubuntu-24.04", "self-hosted")],
            "a tag that becomes a branch": [("setup-python@v5", "setup-python@main")],
            "a comment that grows": [(f"actions/checkout@{SHA_OLD} # v7.0.1", f"actions/checkout@{SHA_OLD} # v7.0.1 skip")],
            "a short sha": [(f"actions/checkout@{SHA_OLD}", "actions/checkout@0123456")],
            "an image with another name": [("postgres:15@", "mysql:15@")],
        }
        for name, pairs in cases.items():
            with self.subTest(name):
                code, found = self.workflow_change(*pairs)
                rules = {r for _, _, r, _, _ in found}
                self.assertEqual(code, 1, found)
                self.assertIn(MACHINERY_RULE, rules)
                self.assertNotIn(VERSION_RULE, rules)
                self.put("Back", **{WORKFLOW_PATH: VERSIONED_WORKFLOW})

    def test_a_bump_next_to_another_change_keeps_the_machinery_finding_and_reports_only_the_other_line(self):
        code, found = self.workflow_change(("ubuntu-24.04", "ubuntu-26.04"), ("timeout-minutes: 10", "timeout-minutes: 0"))
        self.assertEqual(code, 1)
        self.assertEqual({(path, line, rule) for path, line, rule, _, _ in found}, {(WORKFLOW_PATH, 5, MACHINERY_RULE), (WORKFLOW_PATH, 7, LOOSEN_RULE)})

    def test_a_bump_next_to_a_removed_line_in_the_same_hunk_is_not_reported_by_line(self):
        code, found = self.workflow_change(("ubuntu-24.04", "ubuntu-26.04"), ("    timeout-minutes: 10\n", ""))
        self.assertEqual(code, 1)
        self.assertEqual({(line, rule) for _, line, rule, _, _ in found}, {(5, MACHINERY_RULE), (7, LOOSEN_RULE)})

    def test_a_reason_line_still_clears_the_blocking_finding_next_to_a_bump(self):
        self.put(
            f"Bump and loosen\n\n{REASON}",
            **{WORKFLOW_PATH: self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-26.04"), ("timeout-minutes: 10", "timeout-minutes: 0"))},
        )
        code, found = self.findings()
        self.assertEqual((code, {j for *_, j in found}), (0, {True}))

    def test_a_bump_that_also_changes_the_permission_bits_or_the_path_is_not_a_bump(self):
        self.repo.write(WORKFLOW_PATH, self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-26.04")))
        self.repo.git("add", "-A")
        self.repo.git("update-index", "--chmod=+x", WORKFLOW_PATH)
        self.repo.git("commit", "-q", "-m", "Bump and chmod")
        code, found = self.findings()
        self.assertEqual((code, {r for _, _, r, _, _ in found}), (1, {MACHINERY_RULE, LOOSEN_RULE}))
        self.repo.git("mv", WORKFLOW_PATH, ".github/workflows/ci2.yml")
        self.repo.write(".github/workflows/ci2.yml", self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-22.04")))
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "Rename and bump")
        code, found = self.findings()
        self.assertEqual(code, 1, found)
        self.assertIn(MACHINERY_RULE, {r for _, _, r, _, _ in found})

    def test_a_version_line_outside_the_hook_and_ci_files_is_not_reported_at_all(self):
        self.put("Doc", **{"docs/example.yml": "runs-on: ubuntu-24.04\n"})
        self.put("Doc bump", **{"docs/example.yml": "runs-on: ubuntu-26.04\n"})
        self.assertEqual(self.findings(), (0, []))

    def test_staged_changes_are_judged_the_same_way(self):
        self.repo.write(WORKFLOW_PATH, self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-26.04")))
        self.repo.git("add", "-A")
        code, found = self.findings("--staged")
        self.assertEqual((code, [(r, s) for _, _, r, s, _ in found]), (0, [(VERSION_RULE, "note")]))

    def overlay_commit(self, rules):
        self.put("Overlay", **{".check-integrity.json": json.dumps({"rules": rules})})

    def test_the_overlay_can_make_the_note_blocking(self):
        self.overlay_commit({VERSION_RULE: {"severity": "block"}})
        code, found = self.workflow_change(("ubuntu-24.04", "ubuntu-26.04"))
        self.assertEqual((code, [(r, s, j) for _, _, r, s, j in found]), (1, [(VERSION_RULE, "block", False)]))
        self.repo.git("commit", "-q", "--amend", "-m", f"Bump\n\n{REASON}")
        code, found = self.findings()
        self.assertEqual((code, [(r, s, j) for _, _, r, s, j in found]), (0, [(VERSION_RULE, "block", True)]))

    def test_the_overlay_can_set_nothing_but_the_severity_of_the_note(self):
        for change in ({"allow": ["x"]}, {"exempt_paths": ["*"]}, {"severity": "loud"}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ci.OverlayError, VERSION_RULE):
                    ci.parse_overlay(json.dumps({"rules": {VERSION_RULE: change}}), "overlay", ci.load_rules(RULES))

    def test_a_broken_overlay_in_the_baseline_makes_the_note_block_too(self):
        self.overlay_commit({"no-such-rule": {"severity": "note"}})
        self.put("Bump", **{WORKFLOW_PATH: self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-26.04"))})
        code, out, err = self.repo.scan("--commit", "HEAD", "--json")
        self.assertEqual(code, 1, out + err)
        self.assertIn("warning", err)
        self.assertEqual([(f["rule"], f["severity"]) for f in json.loads(out)["findings"]], [(VERSION_RULE, "block")])

    def custom_rules(self, **changes):
        document = json.loads(RULES.read_text())
        for key, value in changes.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        path = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-version-rules.json"
        path.write_text(json.dumps(document))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_a_rules_table_without_the_list_blocks_every_bump(self):
        rules = self.custom_rules(version_lines=None)
        self.put("Bump", **{WORKFLOW_PATH: self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-26.04"))})
        code, out, err = self.repo.scan("--commit", "HEAD", "--rules", str(rules), "--json")
        self.assertEqual(code, 1, out + err)
        self.assertEqual({f["rule"] for f in json.loads(out)["findings"]}, {MACHINERY_RULE, LOOSEN_RULE})

    def test_a_repository_picks_its_own_version_lines(self):
        rules = self.custom_rules(version_lines=[r"^\s*runs-on:\s*\S.*$"])
        runner = self.edited(VERSIONED_WORKFLOW, ("ubuntu-24.04", "ubuntu-26.04"))
        self.put("Runner", **{WORKFLOW_PATH: runner})
        code, out, err = self.repo.scan("--commit", "HEAD", "--rules", str(rules), "--json")
        self.assertEqual((code, [f["rule"] for f in json.loads(out)["findings"]]), (0, [VERSION_RULE]), out + err)
        self.put("Node", **{WORKFLOW_PATH: self.edited(runner, ("node-version: 20", "node-version: 22"))})
        code, out, err = self.repo.scan("--commit", "HEAD", "--rules", str(rules), "--json")
        self.assertEqual(code, 1, out + err)

    def test_the_list_is_checked_when_the_table_is_loaded(self):
        for value, message in (("runs-on", "version_lines"), (["("], "version_lines"), ([""], "version_lines")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ci.IntegrityError, message):
                    ci.load_rules(self.custom_rules(version_lines=value))

    def test_the_rules_table_cannot_define_the_scanner_s_own_rule(self):
        document = json.loads(RULES.read_text())
        document["rules"].append({"id": VERSION_RULE, "kind": "changed", "classes": ["test"], "why": "w"})
        path = Path(self._tmp.name) / "own.json"
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ci.IntegrityError, VERSION_RULE):
            ci.load_rules(path)

    def test_the_default_table_lists_the_five_kinds_of_version_line(self):
        lines = {
            "uses": f"      - uses: actions/checkout@{SHA_OLD} # v7.0.1",
            "uses with a path": "      - uses: github/codeql-action/analyze@v3",
            "runs-on": "    runs-on: ubuntu-24.04",
            "a -version key": "          python-version: ['3.12']",
            "image": "        image: postgres:15",
            "container": "    container: python:3.12",
            "rev": "    rev: v1.2.3",
        }
        patterns = ci.load_rules(RULES).version_lines
        for name, line in lines.items():
            with self.subTest(name):
                self.assertTrue(any(p.fullmatch(line) for p in patterns), line)
        for line in ("    timeout-minutes: 10", "      - run: pytest --cov-fail-under=90", "    version: 1", "      - uses: ./local"):
            with self.subTest(line=line):
                self.assertFalse(any(p.fullmatch(line) for p in patterns), line)


class CheapSwitchOffs(RepoCase):
    """Four cheap ways to switch tests or a coverage floor off with one rename, one line or one
    rewritten line: a test file renamed out of the names the runners collect, an if with a
    constant false condition, a flag that makes a runner collect without running, a floor
    rewritten with every word kept."""

    def findings(self):
        _, report = self.head_json()
        return [(f["path"], f["line"], f["rule"]) for f in report["findings"]]

    def fresh_repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(Path(tmp.name))

    def rename(self, old, new, content=BASE_TEST, **others):
        self.repo.commit(**{old.replace("/", "__"): content}, **others)
        self.repo.git("mv", old, new)
        self.repo.commit("Rename")

    # test-removed: a rename out of the collected names

    COLLECTED = (
        ("tests/test_x.py", "tests/x_cases.py"),
        ("tests/x_test.py", "tests/x_cases.py"),
        ("tests/x.test.ts", "tests/x.cases.ts"),
        ("tests/x.spec.jsx", "tests/x.cases.jsx"),
        ("tests/x_test.go", "tests/x_cases.go"),
        ("web/x.test.ts", "web/x.cases.ts"),
    )

    def test_a_test_file_renamed_to_a_name_no_runner_collects_is_red_once(self):
        for old, new in self.COLLECTED:
            with self.subTest(old=old):
                self.fresh_repo()
                self.rename(old, new)
                self.assertEqual(self.findings(), [(new, 0, "test-removed")])

    def test_a_rename_between_collected_names_is_green(self):
        pairs = (
            ("tests/test_a.py", "tests/test_b.py"),
            ("tests/test_a.py", "tests/a_test.py"),
            ("web/a.test.ts", "web/b.spec.tsx"),
            ("pkg/a_test.go", "pkg/b_test.go"),
        )
        for old, new in pairs:
            with self.subTest(old=old, new=new):
                self.fresh_repo()
                self.rename(old, new)
                code, out, _ = self.repo.scan("--commit", "HEAD")
                self.assertEqual(code, 0, out)

    def test_a_file_that_was_never_a_collected_name_may_be_renamed_to_one_that_is_not(self):
        self.rename("tests/helpers.py", "tests/support.py", content="def helper():\n    return 1\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_test_renamed_out_of_the_names_with_its_source_deleted_is_green(self):
        self.repo.commit(src__x_py="def f():\n    return 1\n", tests__test_x_py=BASE_TEST)
        self.repo.git("mv", "tests/test_x.py", "tests/x_cases.py")
        self.repo.commit("Rename and delete the source", src__x_py=None)
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_test_renamed_out_of_the_names_while_an_unrelated_source_goes_is_red(self):
        self.repo.commit(src__y_py="def g():\n    return 2\n", tests__test_x_py=BASE_TEST)
        self.repo.git("mv", "tests/test_x.py", "tests/x_cases.py")
        self.repo.commit("Rename and delete another source", src__y_py=None)
        self.assertEqual(self.findings(), [("tests/x_cases.py", 0, "test-removed")])

    def test_a_deleted_test_file_is_still_reported_once(self):
        self.repo.commit(tests__test_x_py=BASE_TEST)
        self.repo.commit(tests__test_x_py=None)
        self.assertEqual(self.findings(), [("tests/test_x.py", 0, "test-removed")])

    # skip-added: a constant false condition

    FALSE_CONDITIONS = {
        "tests/test_a.py": "if False:\n",
        "tests/test_b.py": "    if 0:\n",
        "web/c.test.ts": "if (false) {\n",
        "web/d.spec.js": "  if ( false ) {\n",
        "pkg/e_test.go": "\tif false {\n",
        "crate/tests/f.rs": "    if false {\n",
    }

    def test_an_added_constant_false_condition_in_a_test_file_is_red(self):
        for path in self.FALSE_CONDITIONS:
            self.repo.write(path, "// seed\n")
        self.repo.commit()
        for path, line in self.FALSE_CONDITIONS.items():
            self.repo.write(path, "// seed\n" + line)
        self.repo.commit()
        found = set(self.findings())
        for path in self.FALSE_CONDITIONS:
            with self.subTest(path=path):
                self.assertIn((path, 2, "skip-added"), found)

    def test_other_conditions_files_and_comments_are_green(self):
        greens = {
            "tests/test_a.py": "if False_flag:\n    pass\nif 0 == x:\n    pass\nif 0.5:\n    pass\n# if False:\n",
            "web/b.test.ts": "if (falsey) {\n}\nif (!false) {\n}\n// if (false) {\n",
            "pkg/c_test.go": "\tif falsehood {\n\t}\n",
            "src/d.py": "if False:\n    pass\n",
        }
        for path in greens:
            self.repo.write(path, "seed\n")
        self.repo.commit()
        for path, text in greens.items():
            self.repo.write(path, "seed\n" + text)
        self.repo.commit()
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_new_test_file_is_not_an_existing_one(self):
        self.repo.commit(README_md="seed\n")
        self.repo.commit(tests__test_new_py="if False:\n    pass\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_constant_false_condition_that_was_there_and_only_moved_is_green(self):
        seed = "import unittest\n\nif False:\n    x = 1\n\ny = 2\n"
        self.repo.commit(tests__test_a_py=seed)
        self.repo.commit(tests__test_a_py="import unittest\n\ny = 2\n\nif False:\n    x = 1\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.repo.commit(tests__test_a_py="import unittest\n\ny = 2\n\nif True:\n    if False:\n        x = 1\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_second_copy_of_a_constant_false_condition_is_red(self):
        self.repo.commit(tests__test_a_py="if False:\n    x = 1\n")
        self.repo.commit(tests__test_a_py="if False:\n    x = 1\nif False:\n    z = 1\n")
        self.assertEqual(self.findings(), [("tests/test_a.py", 3, "skip-added")])

    # a flag that makes a runner collect without running

    FLAGS = ("--co", "--collect-only", "--passWithNoTests", "--exit-zero")

    def test_a_flag_that_stops_the_tests_running_is_red_in_each_kind_of_file(self):
        shapes = {
            "pytest.ini": ("[pytest]\ntestpaths = tests\n", "addopts = {flag}\n", "check-file-weakened"),
            "pyproject.toml": ('[project]\nname = "d"\n', 'addopts = "{flag}"\n', "check-file-weakened"),
            ".github/workflows/ci.yml": ("on: push\n", "      - run: pytest {flag}\n", "bypass-added"),
            ".githooks/pre-push": ("#!/bin/sh\n", "pytest {flag}\n", "bypass-added"),
            "Makefile": ("all:\n", "\tpytest {flag}\n", "bypass-added"),
            "package.json": ('{"name": "d"}\n', '{{"scripts": {{"test": "jest {flag}"}}}}\n', "bypass-added"),
            "scripts/check.sh": ("#!/bin/sh\n", "flake8 {flag} .\n", "bypass-added"),
        }
        for path, (seed, _, _) in shapes.items():
            self.repo.write(path, seed)
        self.repo.commit()
        for flag in self.FLAGS:
            for path, (seed, line, _) in shapes.items():
                self.repo.write(path, seed + line.format(flag=flag))
            self.repo.commit(f"Add {flag}")
            found = {(path, rule) for path, _, rule in self.findings()}
            for path, (_, _, rule) in shapes.items():
                with self.subTest(flag=flag, path=path):
                    self.assertIn((path, rule), found)
            for path, (seed, _, _) in shapes.items():
                self.repo.write(path, seed)
            self.repo.commit("Take it out")

    def test_the_flag_is_found_where_a_line_is_rewritten_in_place(self):
        self.repo.commit(pytest_ini="[pytest]\naddopts = --strict-markers\n")
        self.repo.commit(pytest_ini="[pytest]\naddopts = --strict-markers --co\n")
        self.assertEqual(self.findings(), [("pytest.ini", 2, "check-file-weakened")])

    def test_flags_with_the_same_start_and_other_files_are_green(self):
        self.repo.commit(pytest_ini="[pytest]\n", Makefile="all:\n", docs__usage_md="x\n")
        self.repo.commit(
            pytest_ini="[pytest]\naddopts = --cov=src --color=yes --collect-only-x --co-op\n",
            Makefile="all:\n\tgit commit --amend --config x --cov-report=term --colour\n",
            docs__usage_md="Run pytest --co or jest --passWithNoTests to list.\n",
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    # check-file-weakened: a floor rewritten with every word kept

    FLOORS = {
        ".coveragerc": ("[report]\nfail_under = 90\n", "[report]\nfail_under = .90\n"),
        "setup.cfg": ("[coverage:report]\nfail_under = 90\n", "[coverage:report]\nfail_under = 90.0\n"),
        "pyproject.toml": (
            '[tool.interrogate]\nfail-under = 80\n',
            '[tool.interrogate]\nfail-under = 80 # was 90\n',
        ),
        "tox.ini": ("[testenv]\ncommands = pytest --cov-fail-under=90\n", "[testenv]\ncommands = pytest --cov-fail-under=90.0\n"),
        ".github/workflows/ci.yml": (
            "      - run: pytest --cov-fail-under=90\n",
            "      - run: pytest --cov-fail-under=90.0\n",
        ),
        "deno.json": ('{\n  "coverageThreshold": 90\n}\n', '{\n  "coverageThreshold": 90.0\n}\n'),
    }

    def test_a_floor_rewritten_in_place_is_red_even_when_every_word_stays(self):
        for path, (seed, _) in self.FLOORS.items():
            self.repo.write(path, seed)
        self.repo.commit()
        for path, (_, new) in self.FLOORS.items():
            self.repo.write(path, new)
        self.repo.commit()
        found = {(path, rule) for path, _, rule in self.findings()}
        for path in self.FLOORS:
            with self.subTest(path=path):
                self.assertIn((path, "check-file-weakened"), found)

    def test_a_raised_floor_is_red_too_and_a_justified_one_passes(self):
        self.repo.commit(_coveragerc="[report]\nfail_under = 90\n")
        self.repo.commit(_coveragerc="[report]\nfail_under = 95\n")
        self.assertEqual(self.findings(), [(".coveragerc", 2, "check-file-weakened")])
        self.repo.commit(f"Raise the floor\n\n{REASON}\n", _coveragerc="[report]\nfail_under = 97\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_a_rewritten_line_that_is_not_a_floor_keeps_the_old_rule(self):
        self.repo.commit(_coveragerc="[report]\nfail_under = 90\nomit = tests/*\n")
        self.repo.commit(_coveragerc="[report]\nfail_under = 90\nomit = tests/* # ours\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_the_floor_of_the_other_lines_is_untouched_when_only_another_line_changes(self):
        self.repo.commit(_coveragerc="[report]\nfail_under = 90\nomit = tests/*\n")
        self.repo.commit(_coveragerc="[report]\nfail_under = 90\nomit = tests/*\nshow_missing = True\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

    def test_the_lines_that_count_are_the_tables_not_the_scanner_s(self):
        self.repo.commit(_coveragerc="[report]\nfail_under = 90\n")
        self.repo.commit(_coveragerc="[report]\nfail_under = .90\n")
        document = json.loads(RULES.read_text())
        self.assertEqual(self.findings(), [(".coveragerc", 2, "check-file-weakened")])
        document["guard_lines"] = []
        emptied = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-no-guard.json"
        emptied.write_text(json.dumps(document))
        self.addCleanup(lambda: emptied.unlink(missing_ok=True))
        code, out, err = self.repo.scan("--commit", "HEAD", "--rules", str(emptied))
        self.assertEqual(code, 0, out + err)

    def test_the_lines_are_checked_when_the_table_is_loaded(self):
        for value in ("fail_under", ["("], [""]):
            with self.subTest(value=value):
                document = json.loads(RULES.read_text())
                document["guard_lines"] = value
                path = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-bad-guard.json"
                path.write_text(json.dumps(document))
                self.addCleanup(lambda p=path: p.unlink(missing_ok=True))
                with self.assertRaisesRegex(ci.IntegrityError, "guard_lines"):
                    ci.load_rules(path)

    def test_the_floor_is_still_guarded_with_an_overlay_and_with_a_broken_one(self):
        self.repo.commit(_coveragerc="[report]\nfail_under = 90\n")
        self.repo.commit(
            "Overlay", **{".check-integrity.json": json.dumps({"version": 1, "rules": {"skip-added": {"severity": "note"}}})}
        )
        self.repo.commit(_coveragerc="[report]\nfail_under = .90\n")
        self.assertEqual(self.findings(), [(".coveragerc", 2, "check-file-weakened")])
        self.repo.commit("Break the overlay", **{".check-integrity.json": "{"})
        self.repo.commit(_coveragerc="[report]\nfail_under = .90 # tidy\n")
        code, out, err = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 1, out + err)
        self.assertIn("check-file-weakened", out)
        self.assertIn("warning", err)

    def test_a_floor_set_in_place_on_a_line_that_set_none_is_not_a_rewritten_floor(self):
        self.repo.commit(pytest_ini="[pytest]\naddopts = --cov=pkg\n")
        self.repo.commit(pytest_ini="[pytest]\naddopts = --cov=pkg --cov-fail-under=90\n")
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.findings(), [])

    def test_weakens_reads_the_guard_it_is_given(self):
        guard = ci.load_rules(RULES).guard_lines
        self.assertTrue(ci.weakens("fail_under = 90", "fail_under = .90", guard))
        self.assertFalse(ci.weakens("fail_under = 90", "fail_under = .90"))
        self.assertFalse(ci.weakens("fail_under = 90", "fail_under = 90", guard))
        self.assertFalse(ci.weakens("omit = a", "omit = a # b", guard))


if __name__ == "__main__":
    unittest.main()

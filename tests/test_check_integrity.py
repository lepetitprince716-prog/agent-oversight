"""Tests for tools/check_integrity.py against real commits in throwaway repositories.

Every repository, file and message here is made up. The test files written into those
repositories hold skip markers and suppressions on purpose: they are the input.
"""

import importlib.util
import json
import os
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

    def scan(self, *args):
        done = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=self.root,
            env=self.env,
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
    "        self.assertEqual(1, 1)\n"
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
            tests__test_thing_py=BASE_TEST.replace("assert 2 == 2", "self.assertEqual(2, 2)")
            + "        self.assertTrue(True)\n"
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


WORKFLOW = "# CI\njobs:\n  test:\n    steps:\n      - run: make lint\n      - run: make test\n"


class CheckFileWeakened(RepoCase):
    def test_deleted_workflow_is_red(self):
        self.repo.commit(_github__workflows__ci_yml=WORKFLOW)
        self.repo.commit(_github__workflows__ci_yml=None)
        self.assertEqual(
            self.rules_at_head(), {(".github/workflows/ci.yml", 0, "check-file-weakened", False)}
        )

    def test_removed_step_is_red_comment_and_addition_are_green(self):
        self.repo.commit(_github__workflows__ci_yml=WORKFLOW)
        self.repo.commit(_github__workflows__ci_yml=WORKFLOW.replace("      - run: make lint\n", ""))
        self.assertEqual(
            self.rules_at_head(), {(".github/workflows/ci.yml", 5, "check-file-weakened", False)}
        )
        self.repo.commit(
            _github__workflows__ci_yml=WORKFLOW.replace("# CI\n", "") + "      - run: make docs\n"
        )
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

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

    def test_merge_commit_uses_first_parent(self):
        self.repo.commit(README_md="seed\n", src__a_py="a = 1\n", tests__test_s_py="def test_s():\n    assert 1\n")
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.commit(tests__test_s_py="@unittest.skip('x')\ndef test_s():\n    assert 1\n")
        self.repo.git("checkout", "-q", "main")
        self.repo.commit(src__a_py="a = 1  # noqa\n")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge side", "side")
        self.assertEqual(self.rules_at_head(), {("tests/test_s.py", 1, "skip-added", False)})

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


class RangeAndErrors(RepoCase):
    def test_range_pairs_each_commit_with_its_message(self):
        base = self.repo.commit(tests__test_a_py=BASE_TEST)
        first = self.repo.commit(
            f"Park\n\n{REASON}\n", tests__test_a_py=BASE_TEST.replace("assert 2 == 2", "pass")
        )
        second = self.repo.commit(src__b_py="x = 1  # type: ignore\n")
        code, out, _ = self.repo.scan("--range", f"{base}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn(f"{first[:7]} tests/test_a.py:8 assertion-loss justified", out)
        self.assertIn(f"{second[:7]} src/b.py:1 suppression-added  x = 1", out)
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

    def test_reworded_or_widened_check_lines_are_green(self):
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
        code, out, _ = self.repo.scan("--commit", "HEAD")
        self.assertEqual(code, 0, out)

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
            {("pyproject.toml", 2, "check-file-weakened"), (".github/workflows/ci.yml", 3, "check-file-weakened")},
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
        self.assertEqual(self.findings(), {(path, "bypass-added") for path in samples})

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


if __name__ == "__main__":
    unittest.main()

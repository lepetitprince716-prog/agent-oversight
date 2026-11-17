import tempfile
import unittest
from pathlib import Path

from oversight.config import ConfigError, Lang, config_path, load_config, parse_config

GOOD = """
lang = "zh"

[tracker]
dir = "~/tasks"
command = ["bd", "--quiet"]
blocker_markers = ["wait:"]

[github]
command = ["gh"]
timeout_seconds = 10

[thresholds]
quiet_days = 4
pr_days = 5

[repos.widgets]
github = "acme/widgets"
path = "~/code/widgets"
public = true

[repos.gadgets]
github = "acme/gadgets"
scheduled = false
"""


def base(**overrides):
    data = {"tracker": {"dir": "~/tasks"}}
    data.update(overrides)
    return data


class ConfigTest(unittest.TestCase):
    def assertRejected(self, data, key_path):
        with self.assertRaises(ConfigError) as caught:
            parse_config(data)
        self.assertIn(key_path, str(caught.exception))

    def test_good_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(GOOD, encoding="utf-8")
            config = load_config(path)
        self.assertIs(config.lang, Lang.ZH)
        self.assertEqual(config.tracker_dir, Path("~/tasks").expanduser())
        self.assertEqual(config.tracker_command, ("bd", "--quiet"))
        self.assertEqual(config.blocker_markers, ("wait:",))
        self.assertEqual(config.github_timeout_seconds, 10)
        self.assertEqual((config.quiet_days, config.pr_days), (4, 5))
        self.assertEqual(config.github_for("widgets"), "acme/widgets")
        self.assertIsNone(config.github_for("nope"))
        self.assertEqual([repo.scheduled for repo in config.repos], [True, False])
        self.assertEqual([repo.path for repo in config.repos], [Path("~/code/widgets").expanduser(), None])
        self.assertEqual([repo.public for repo in config.repos], [True, False])

    def test_defaults(self):
        config = parse_config(base())
        self.assertIs(config.lang, Lang.EN)
        self.assertEqual(config.tracker_command, ("bd",))
        self.assertEqual(config.blocker_markers, ("blocker:", "卡点："))
        self.assertEqual(config.github_command, ("gh",))
        self.assertEqual((config.github_timeout_seconds, config.quiet_days, config.pr_days), (30, 2, 3))
        self.assertEqual(config.repos, ())

    def test_local_clone_keys_default_to_off(self):
        (repo,) = parse_config(base(repos={"w": {"github": "a/b"}})).repos
        self.assertEqual((repo.path, repo.public), (None, False))

    def test_path_is_expanded(self):
        (repo,) = parse_config(base(repos={"w": {"github": "a/b", "path": "~/code/w"}})).repos
        self.assertEqual(repo.path, Path("~/code/w").expanduser())
        self.assertNotEqual(repo.path, Path("~/code/w"))

    def test_local_clone_keys_are_checked(self):
        self.assertRejected(base(repos={"w": {"github": "a/b", "path": 5}}), "repos.w.path")
        self.assertRejected(base(repos={"w": {"github": "a/b", "path": ""}}), "repos.w.path")
        self.assertRejected(base(repos={"w": {"github": "a/b", "path": "code/w"}}), "repos.w.path")
        self.assertRejected(base(repos={"w": {"github": "a/b", "path": "./w"}}), "repos.w.path")
        self.assertRejected(base(repos={"w": {"github": "a/b", "public": "yes"}}), "repos.w.public")
        self.assertRejected(base(repos={"w": {"github": "a/b", "public": 1}}), "repos.w.public")

    def test_tracker_is_required(self):
        self.assertRejected({}, "tracker")
        self.assertRejected({"tracker": {}}, "tracker.dir")

    def test_unknown_keys(self):
        self.assertRejected(base(colour="red"), "colour")
        self.assertRejected({"tracker": {"dir": "x", "dri": "y"}}, "tracker.dri")
        self.assertRejected(base(github={"cmd": ["gh"]}), "github.cmd")
        self.assertRejected(base(thresholds={"days": 1}), "thresholds.days")
        self.assertRejected(base(repos={"w": {"github": "a/b", "extra": 1}}), "repos.w.extra")

    def test_wrong_types(self):
        self.assertRejected({"tracker": {"dir": 5}}, "tracker.dir")
        self.assertRejected(base(lang=1), "lang")
        self.assertRejected(base(thresholds={"quiet_days": "2"}), "thresholds.quiet_days")
        self.assertRejected(base(thresholds={"pr_days": True}), "thresholds.pr_days")
        self.assertRejected(base(github={"timeout_seconds": 1.5}), "github.timeout_seconds")
        self.assertRejected(base(repos={"w": {"github": "a/b", "scheduled": "yes"}}), "repos.w.scheduled")
        self.assertRejected(base(repos=[]), "repos")
        self.assertRejected(base(repos={"w": "a/b"}), "repos.w")

    def test_lang_outside_the_two(self):
        self.assertRejected(base(lang="fr"), "lang")

    def test_github_must_be_owner_slash_name(self):
        for bad in ("widgets", "a/b/c", "a b/c", "/c", "a/"):
            with self.subTest(bad=bad):
                self.assertRejected(base(repos={"w": {"github": bad}}), "repos.w.github")
        self.assertRejected(base(repos={"w": {}}), "repos.w.github")

    def test_thresholds_must_be_positive(self):
        self.assertRejected(base(thresholds={"quiet_days": 0}), "thresholds.quiet_days")
        self.assertRejected(base(thresholds={"pr_days": -1}), "thresholds.pr_days")
        self.assertRejected(base(github={"timeout_seconds": 0}), "github.timeout_seconds")

    def test_empty_lists(self):
        self.assertRejected({"tracker": {"dir": "x", "command": []}}, "tracker.command")
        self.assertRejected({"tracker": {"dir": "x", "blocker_markers": []}}, "tracker.blocker_markers")
        self.assertRejected({"tracker": {"dir": "x", "command": [""]}}, "tracker.command")
        self.assertRejected({"tracker": {"dir": "x", "command": [1]}}, "tracker.command")
        self.assertRejected(base(github={"command": []}), "github.command")

    def test_unreadable_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "absent.toml"
            with self.assertRaises(ConfigError):
                load_config(missing)
            broken = Path(folder) / "broken.toml"
            broken.write_text("lang = ", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(broken)
            self.assertIn("invalid TOML", str(caught.exception))

    def test_a_file_that_is_not_utf_8_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as folder:
            latin_1 = Path(folder) / "latin1.toml"
            latin_1.write_bytes(b'# r\xe9sum\xe9\n[tracker]\ndir = "x"\n')
            with self.assertRaises(ConfigError) as caught:
                load_config(latin_1)
            self.assertIn("not UTF-8", str(caught.exception))

    def test_path_resolution_order(self):
        self.assertEqual(config_path("/x/a.toml", {"OVERSIGHT_CONFIG": "/y/b.toml"}), Path("/x/a.toml"))
        self.assertEqual(config_path(None, {"OVERSIGHT_CONFIG": "/y/b.toml"}), Path("/y/b.toml"))
        self.assertEqual(
            config_path(None, {}), Path("~/.config/agent-oversight/config.toml").expanduser()
        )


if __name__ == "__main__":
    unittest.main()

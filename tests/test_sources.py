import sys
import tempfile
import unittest
from pathlib import Path

from oversight.sources import (
    SourceError,
    decode_json,
    parse_utc,
    read_json,
    read_text,
    require_list,
    run_json,
    run_text,
)

LATIN_1_BYTES = "import sys; sys.stdout.buffer.write(b'[\"caf\\xe9\"]')"
UTF_8_BYTES = "import sys; sys.stdout.buffer.write('[\"卡点：等用户定\"]'.encode('utf-8'))"
ECHO_ARGV = "import json, sys; print(json.dumps(sys.argv[1:]))"


class RunJsonTest(unittest.TestCase):
    def run_code(self, code):
        return run_json([sys.executable, "-c", code], 20)

    def test_output_is_read_as_utf_8(self):
        self.assertEqual(self.run_code(UTF_8_BYTES), ["卡点：等用户定"])

    def test_output_that_is_not_utf_8_is_a_source_error(self):
        with self.assertRaises(SourceError) as caught:
            self.run_code(LATIN_1_BYTES)
        self.assertIn("not UTF-8", str(caught.exception))

    def test_arguments_are_passed_through(self):
        self.assertEqual(run_json([sys.executable, "-c", ECHO_ARGV, "a", "b"], 20), ["a", "b"])


class TextTest(unittest.TestCase):
    def test_run_text_returns_the_output_undecoded(self):
        self.assertEqual(run_text([sys.executable, "-c", "print('  ')"], 20), "  \n")

    def test_read_text_returns_the_file_undecoded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.json"
            path.write_text("\n", encoding="utf-8")
            self.assertEqual(read_text(path), "\n")

    def test_decode_json(self):
        self.assertEqual(decode_json("[1]"), [1])
        with self.assertRaises(SourceError) as caught:
            decode_json("")
        self.assertIn("invalid JSON", str(caught.exception))


class ReadJsonTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "data.json"

    def test_a_file_that_is_not_utf_8_is_a_source_error(self):
        self.path.write_bytes(b'["caf\xe9"]')
        with self.assertRaises(SourceError) as caught:
            read_json(self.path)
        self.assertIn("data.json", str(caught.exception))
        self.assertIn("not UTF-8", str(caught.exception))

    def test_utf_8_is_read(self):
        self.path.write_text('["卡点："]', encoding="utf-8")
        self.assertEqual(read_json(self.path), ["卡点："])


class ParseUtcTest(unittest.TestCase):
    def test_a_time_python_cannot_represent_is_a_source_error(self):
        with self.assertRaises(SourceError):
            parse_utc("0001-01-01T00:00:00+01:00", "updated_at")

    def test_good_and_bad_times(self):
        self.assertEqual(parse_utc("2026-11-17T12:00:00+01:00", "t").hour, 11)
        for bad in ("yesterday", "2026-11-17T12:00:00"):
            with self.subTest(bad=bad), self.assertRaises(SourceError):
                parse_utc(bad, "t")


class RequireListTest(unittest.TestCase):
    def test_only_a_list_passes(self):
        self.assertEqual(require_list([1]), [1])
        for bad in ({}, "text", None, (1,)):
            with self.subTest(bad=bad), self.assertRaises(SourceError):
                require_list(bad)


if __name__ == "__main__":
    unittest.main()

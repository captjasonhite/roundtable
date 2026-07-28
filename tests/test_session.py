"""Reading a session directory: the file shapes across all three rounds."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import session as session_mod

RESULT = """---
model: "alpha-7B"
thinking: true
temperature: 1.0
seed: 42
tokens: 500
tokens_per_sec: 40.0
elapsed_sec: 12
error: null
---

## Output

hello
"""

ROUND3 = """---
model: "alpha-7B"
thinking: true
temperature: 1.0
seed: 42
tokens: 300
tokens_per_sec: 60.0
elapsed_sec: 5
error: null
---

## Output

The panel agreed on alpha-7B.
"""


class SessionShapeTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-session-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_output_survives_a_thinking_trace_that_names_the_heading(self):
        """A model planning its answer writes "## Output" inside its reasoning.

        Splitting on the first occurrence handed back half the trace as the
        model's answer -- which inflated every word count for thinking models
        by two to three times and put reasoning on the report page.
        """
        body = ("## Thinking\n\nFirst I will write PART 1, then under "
                "## Output I will put the draft.\n\n## Output\n\nThe draft.\n")
        self.assertEqual(session_mod.output_of(body).strip(), "The draft.")

    def test_output_of_a_body_with_no_heading_is_the_whole_body(self):
        self.assertEqual(session_mod.output_of("just text").strip(), "just text")

    def test_started_comes_from_the_directory_stamp(self):
        """Not the directory mtime: every finished run writes into the session
        and would push that forward."""
        sdir = os.path.join(self.dir, "20260723-211143")
        os.makedirs(sdir)
        self.assertEqual(session_mod.started(sdir),
                         time.mktime(time.strptime("20260723-211143",
                                                   "%Y%m%d-%H%M%S")))

    def test_touched_ignores_files_a_rebuild_rewrites(self):
        """report.html and scores.json are rewritten into the session every time
        the page is regenerated -- counting them would make a session that died
        last week look alive, and inflate how long it took to run."""
        self.write("01_x_alpha_thinking.md", RESULT)
        old = 1_700_000_000.0
        os.utime(os.path.join(self.dir, "01_x_alpha_thinking.md"), (old, old))
        self.write("report.html", "<html></html>")   # written just now
        self.write("scores.json", "{}")
        self.assertEqual(session_mod.touched(self.dir), old)

    def test_touched_follows_a_log_of_a_run_still_in_flight(self):
        """The run writing right now has no result file yet; its log is the only
        sign the queue is still alive."""
        self.write("01_x_alpha_thinking.md", RESULT)
        old = 1_700_000_000.0
        os.utime(os.path.join(self.dir, "01_x_alpha_thinking.md"), (old, old))
        os.makedirs(os.path.join(self.dir, "logs"))
        self.write(os.path.join("logs", "02_beta_thinking.log"), "loading…")
        self.assertGreater(session_mod.touched(self.dir), old)

    def test_expected_counts_are_none_when_the_runner_never_wrote_them(self):
        self.write("01_x_alpha_thinking.md", RESULT)
        data = session_mod.load(self.dir)
        self.assertIsNone(data["expected_runs"])
        self.assertIsNone(data["expected_judges"])

    def test_expected_counts_are_read(self):
        self.write("01_x_alpha_thinking.md", RESULT)
        self.write(".expected-runs", "6\n")
        self.write(".expected-judges", "3\n")
        data = session_mod.load(self.dir)
        self.assertEqual((data["expected_runs"], data["expected_judges"]), (6, 3))

    def test_round3_file_is_not_read_as_a_run(self):
        """A round3_*.md has the same frontmatter shape as a benchmark result --
        it must not silently become an eleventh Round 1 entry."""
        self.write("01_x_alpha_thinking.md", RESULT)
        self.write("round3_20260101-000000_alpha-7B.md", ROUND3)
        data = session_mod.load(self.dir)
        self.assertEqual(len(data["runs"]), 1)
        self.assertIsNotNone(data["meta_summary"])

    def test_meta_summary_absent_when_no_round3_file(self):
        self.write("01_x_alpha_thinking.md", RESULT)
        data = session_mod.load(self.dir)
        self.assertIsNone(data["meta_summary"])

    def test_meta_summary_fields(self):
        self.write("01_x_alpha_thinking.md", RESULT)
        self.write("round3_20260101-000000_alpha-7B.md", ROUND3)
        data = session_mod.load(self.dir)
        meta = data["meta_summary"]
        self.assertEqual(meta["model"], "alpha-7B")
        self.assertIn("panel agreed", meta["body"])
        self.assertIsNone(meta["error"])

    def test_newest_round3_file_wins_on_rerun(self):
        self.write("01_x_alpha_thinking.md", RESULT)
        self.write("round3_20260101-000000_alpha-7B.md",
                   ROUND3.replace("panel agreed", "FIRST attempt"))
        self.write("round3_20260102-000000_alpha-7B.md",
                   ROUND3.replace("panel agreed", "SECOND attempt"))
        data = session_mod.load(self.dir)
        self.assertIn("SECOND attempt", data["meta_summary"]["body"])

    def test_load_returns_none_without_any_runs(self):
        self.assertIsNone(session_mod.load(self.dir))


if __name__ == "__main__":
    unittest.main()

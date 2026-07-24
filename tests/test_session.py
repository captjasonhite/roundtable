"""Reading a session directory: the file shapes across all three rounds."""
import os
import shutil
import sys
import tempfile
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

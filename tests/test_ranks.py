"""Rank extraction has to survive whatever markdown a judge decides to write.

Every fixture here is a shape that showed up in a real verdict.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import ranks

LABELS = list("ABCDEFGHIJ")


class ExtractTests(unittest.TestCase):

    def test_ranking_line_is_preferred(self):
        body = ("| Rank | Output |\n| 1 | {{B}} |\n| 2 | {{A}} |\n\n"
                "RANKING: {{A}} > {{B}} > {{C}}")
        got, method, complete = ranks.extract(body, LABELS)
        self.assertEqual(method, "ranking-line")
        # The explicit line wins over the table above it.
        self.assertEqual(got, {"A": 1.0, "B": 2.0, "C": 3.0})
        self.assertFalse(complete)          # only 3 of 10 labels

    def test_ranking_line_without_braces(self):
        got, method, _ = ranks.extract("RANKING: E > C > J", LABELS)
        self.assertEqual(method, "ranking-line")
        self.assertEqual(got, {"E": 1.0, "C": 2.0, "J": 3.0})

    def test_bold_table(self):
        body = ("| Rank | Output | Verdict |\n|---|---|---|\n"
                "| 1 | **A** | best |\n| 2 | **C** | good |\n| 3 | **E** | fine |")
        got, method, _ = ranks.extract(body, LABELS)
        self.assertEqual(method, "table")
        self.assertEqual(got, {"A": 1.0, "C": 2.0, "E": 3.0})

    def test_braced_table(self):
        body = "| 1 | {{A}} | x |\n| 2 | {{C}} | y |"
        got, _, _ = ranks.extract(body, LABELS)
        self.assertEqual(got, {"A": 1.0, "C": 2.0})

    def test_shared_rank_row(self):
        """'| 9/10 | B & G |' -- two outputs declared tied across two places."""
        body = "| 1 | A | x |\n| 2 | C | y |\n| 9/10| B & G | identical |"
        got, _, _ = ranks.extract(body, LABELS)
        self.assertEqual(got["B"], 9.5)
        self.assertEqual(got["G"], 9.5)

    def test_repeated_rank_number(self):
        """Two rows both numbered 9 -- a judge's way of writing a tie."""
        body = "| 1 | A |\n| 9 | **B** |\n| 9 | **G** |"
        got, _, _ = ranks.extract(body, LABELS)
        self.assertEqual(got["B"], 9.0)
        self.assertEqual(got["G"], 9.0)

    def test_first_mention_wins(self):
        """A judge repeating its table in a conclusion must not overwrite it."""
        body = "| 1 | A |\n| 2 | C |\n\nlater...\n\n| 1 | C |\n| 2 | A |"
        got, _, _ = ranks.extract(body, LABELS)
        self.assertEqual(got, {"A": 1.0, "C": 2.0})

    def test_ignores_stats_tables(self):
        """A token/speed table must not be mistaken for a ranking."""
        body = "| tokens | model |\n|---|---|\n| 2168 | A |\n| 4445 | C |"
        got, method, _ = ranks.extract(body, LABELS)
        self.assertEqual(method, "none")
        self.assertEqual(got, {})

    def test_ignores_letters_not_in_the_session(self):
        body = "| 1 | Z | not a label |\n| 2 | A |\n| 3 | C |"
        got, _, _ = ranks.extract(body, LABELS)
        self.assertNotIn("Z", got)
        self.assertEqual(got, {"A": 2.0, "C": 3.0})

    def test_ordered_list_fallback(self):
        body = "### 1. Output {{E}}\nblah\n\n### 2. Output {{C}}\nblah"
        got, method, _ = ranks.extract(body, LABELS)
        self.assertEqual(method, "list")
        self.assertEqual(got, {"E": 1.0, "C": 2.0})

    def test_prose_alone_yields_nothing(self):
        """No table, no line -- better to report nothing than to guess."""
        body = ("A is genuinely superior to the others, and I think C is second. "
                "I would avoid B and G entirely.")
        got, method, _ = ranks.extract(body, LABELS)
        self.assertEqual(method, "none")
        self.assertEqual(got, {})

    def test_complete_flag(self):
        body = "RANKING: A > B > C"
        _, _, complete = ranks.extract(body, ["A", "B", "C"])
        self.assertTrue(complete)


if __name__ == "__main__":
    unittest.main()

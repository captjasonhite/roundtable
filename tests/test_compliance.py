"""The mechanical gate in front of the panel.

Every case here is a fault a judge demonstrably failed to catch: a session was
scored with an entry missing 300 words of the manuscript and nobody noticed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import compliance


SOURCE = """She met him on a week-long vacation in Mexico with a friend.
After a few days of margaritas she was craving the attention of a stranger.
She was walking on the beach, her eyes roaming, taking in the sights.
***CUT***
They had just finished getting the galley squared away for the evening.
It had been a long day on the water, but the guests had a wonderful time.
The captain speared a nice grouper for the dinner service that night.
***CUT***
They stood together on the sand, her hand finding his in the dark.
The moon hung low over the water, a silver crescent above the bay.
It was a moment of pure connection that would stay with them forever.
"""


def faithful(source, extra=" [She felt the deck shift beneath her bare feet.]"):
    """A compliant draft: source intact, markers intact, additions bracketed."""
    return "**PART 2: DRAFT**\n" + source.replace(
        "her eyes roaming, taking in the sights.",
        "her eyes roaming, taking in the sights." + extra)


class ComplianceTests(unittest.TestCase):

    def test_a_faithful_draft_passes(self):
        f = compliance.check(SOURCE, faithful(SOURCE))
        self.assertTrue(f["ok"], compliance.summary(f))
        self.assertEqual(f["markers"], {"source": 2, "output": 2})
        self.assertGreaterEqual(f["coverage"], 0.95)

    def test_a_deleted_section_is_a_fault(self):
        """The exact failure six judges missed in 20260727-200609."""
        gutted = faithful(SOURCE).replace(
            "It had been a long day on the water, but the guests had a wonderful time.\n"
            "The captain speared a nice grouper for the dinner service that night.\n", "")
        f = compliance.check(SOURCE, gutted)
        self.assertFalse(f["ok"])
        self.assertIn("coverage", [x["code"] for x in f["faults"]])

    def test_a_draft_that_stops_early_is_a_fault(self):
        half = faithful(SOURCE).split("***CUT***")[0]
        f = compliance.check(SOURCE, half)
        codes = [x["code"] for x in f["faults"]]
        self.assertIn("unfinished", codes)
        self.assertIn("markers", codes)

    def test_an_extra_marker_is_a_fault(self):
        f = compliance.check(SOURCE, faithful(SOURCE) + "\n***CUT***\n")
        self.assertFalse(f["ok"])
        self.assertEqual(f["markers"], {"source": 2, "output": 3})

    def test_rewriting_the_closing_line_is_not_truncation(self):
        """The last sentence is the likeliest in the book to be replaced on
        purpose -- the brief's own critique step asks for exactly that."""
        draft = faithful(SOURCE).replace(
            "It was a moment of pure connection that would stay with them forever.",
            "[She let her watch fall against the sand. No counting. Just him.]")
        f = compliance.check(SOURCE, draft)
        self.assertNotIn("unfinished", [x["code"] for x in f["faults"]],
                         compliance.summary(f))

    def test_the_critique_sections_do_not_count_as_coverage(self):
        """PART 3 quotes the source while reviewing it. Counting those quotes
        would let a model delete a passage and win the coverage back by
        mentioning it afterwards."""
        gutted = faithful(SOURCE).replace(
            "They stood together on the sand, her hand finding his in the dark.\n"
            "The moon hung low over the water, a silver crescent above the bay.\n"
            "It was a moment of pure connection that would stay with them forever.\n", "")
        alibi = gutted + ("\n**PART 3: CRITIQUE**\n" + SOURCE)
        f = compliance.check(SOURCE, alibi)
        self.assertFalse(f["ok"], "the quoted source must not paper over the cut")

    def test_per_section_coverage_is_reported(self):
        f = compliance.check(SOURCE, faithful(SOURCE))
        self.assertEqual([s["n"] for s in f["segments"]], [1, 2, 3])
        self.assertTrue(all(s["coverage"] >= 0.9 for s in f["segments"]))

    def test_bracketing_the_clients_own_prose_is_a_warning_not_a_fault(self):
        draft = faithful(SOURCE).replace(
            "The moon hung low over the water, a silver crescent above the bay.",
            "[The moon hung low over the water, a silver crescent above the bay.]")
        f = compliance.check(SOURCE, draft)
        self.assertTrue(f["ok"], "annotation defects must not fail a draft")
        self.assertIn("bracketed_source", [w["code"] for w in f["warnings"]])

    def test_a_source_without_markers_is_still_checked(self):
        plain = "\n".join(l for l in SOURCE.splitlines() if l != "***CUT***")
        f = compliance.check(plain, faithful(plain))
        self.assertTrue(f["ok"])
        self.assertEqual(f["segments"], [])

    def test_retry_message_names_the_actual_faults(self):
        half = faithful(SOURCE).split("***CUT***")[0]
        msg = compliance.retry_message(compliance.check(SOURCE, half))
        self.assertIn("***CUT***", msg)
        self.assertIn("stops early", msg)
        # Never asks permission: that costs a turn and the answer is always yes.
        self.assertNotIn("?", msg)

    def test_a_clean_draft_gets_no_retry_message(self):
        self.assertIsNone(
            compliance.retry_message(compliance.check(SOURCE, faithful(SOURCE))))

    def test_retry_message_does_not_blame_a_marker_the_source_never_had(self):
        """A plain creative-writing prompt has no ***CUT*** marker and no PART
        2/3 structure. Naming the marker anyway describes a document shape the
        model was never asked to produce -- the check should not fire this way
        at all in the real runner, but if it does, the message must not lie
        about why."""
        source = ("Write a short story about a lighthouse keeper who "
                  "discovers a message in a bottle.")
        output = "## Output\n\nThe old keeper walked the shore at dawn."
        f = compliance.check(source, output)
        self.assertEqual(f["markers"]["source"], 0)
        msg = compliance.retry_message(f)
        self.assertNotIn(compliance.MARKER, msg)


if __name__ == "__main__":
    unittest.main()

"""Round 3: picking the top model and building its synthesis prompt."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import consensus, digest, ranks

KEY = {
    "A": {"model": "alpha-7B", "mode": "thinking"},
    "B": {"model": "beta-7B", "mode": "thinking"},
    "C": {"model": "gamma-7B", "mode": "no thinking"},
}


def scored_session():
    session = {"blind": True, "key": KEY}
    rankings = [
        {"judge": "alpha-7B", "file": "a.md", "method": "ranking-line",
         "complete": True, "ranks": {"A": 1.0, "B": 2.0, "C": 3.0}},
        {"judge": "beta-7B", "file": "b.md", "method": "ranking-line",
         "complete": True, "ranks": {"A": 1.0, "B": 2.0, "C": 3.0}},
        {"judge": "gamma-7B", "file": "c.md", "method": "ranking-line",
         "complete": True, "ranks": {"A": 2.0, "B": 3.0, "C": 1.0}},
    ]
    return session, consensus.score(session, rankings)


class PickTopTests(unittest.TestCase):

    def test_picks_the_highest_scoring_row(self):
        _, result = scored_session()
        top = digest.pick_top(result)
        self.assertEqual(top["model"], "alpha-7B")

    def test_none_when_nothing_is_scored(self):
        session = {"blind": False, "key": KEY}
        result = consensus.score(session, [])
        self.assertIsNone(digest.pick_top(result))


class BuildTests(unittest.TestCase):

    def test_prompt_states_the_order_without_statistics_jargon(self):
        session, result = scored_session()
        system_prompt, user_prompt = digest.build(session, result)
        self.assertIsNotNone(system_prompt)
        self.assertIn("alpha-7B", user_prompt)
        # The synthesis is about the writing, not the coefficient — the metric
        # must not surface by name, and the model is told to drop statistics.
        self.assertNotIn("Kendall", user_prompt)
        self.assertIn("without statistics", system_prompt)
        # It must not instruct the model to re-derive a ranking.
        self.assertIn("Do not re-rank", user_prompt)

    def test_includes_judge_notes_on_winner_and_loser_by_name(self):
        session, result = scored_session()
        # Judges refer to entries by blind letter; A won, C is last here.
        session["judges"] = [
            {"judge": "alpha-7B", "body": "## Output\n\n"
             "**{{A}}** nails the structure and finishes cleanly.\n\n"
             "**{{C}}** rambles and never lands the ending."},
            {"judge": "beta-7B", "body": "## Output\n\n"
             "**{{A}}** has the sharpest, most disciplined prose here.\n\n"
             "**{{C}}** repeats a whole paragraph verbatim."},
        ]
        _, user_prompt = digest.build(session, result)
        # Letters are resolved to model names, and both ends are quoted.
        self.assertNotIn("{{A}}", user_prompt)
        self.assertIn("finishes cleanly", user_prompt)
        self.assertIn("repeats a whole paragraph", user_prompt)
        self.assertIn("top entry (alpha-7B)", user_prompt)
        self.assertIn("lowest-rated entry (gamma-7B)", user_prompt)

    def test_uses_short_names(self):
        session = {"blind": True, "key": {
            "A": {"model": "Qwen3.6-27B-Fable-Fus-711-UnHeretic-NM-DAU-NEO-MAX-NEO-MTP-IQ4_XS",
                 "mode": "thinking"},
            "B": {"model": "Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-APEX-I-Compact",
                 "mode": "thinking"},
        }}
        rankings = [{"judge": "j", "file": "j.md", "method": "ranking-line",
                    "complete": True, "ranks": {"A": 1.0, "B": 2.0}}]
        result = consensus.score(session, rankings)
        _, user_prompt = digest.build(session, result)
        self.assertNotIn("UnHeretic-NM-DAU-NEO-MAX-NEO-MTP-IQ4_XS", user_prompt)
        self.assertIn("Qwen3.6-27B-Fable", user_prompt)

    def test_none_when_session_is_not_scored(self):
        session = {"blind": False, "key": KEY}
        result = consensus.score(session, [])
        system_prompt, user_prompt = digest.build(session, result)
        self.assertIsNone(system_prompt)
        self.assertIsNone(user_prompt)


if __name__ == "__main__":
    unittest.main()

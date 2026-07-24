"""Scoring rules: self-votes never count, and agreement is measured honestly."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import consensus


def session(labels):
    return {"blind": True, "key": labels}


def ranking(judge, ranks, complete=True):
    return {"judge": judge, "file": judge + ".md", "ranks": ranks,
            "method": "ranking-line", "complete": complete}


KEY = {
    "A": {"model": "alpha-7B", "mode": "thinking"},
    "B": {"model": "beta-7B", "mode": "thinking"},
    "C": {"model": "gamma-7B", "mode": "thinking"},
}


class ScoringTests(unittest.TestCase):

    def test_self_vote_is_excluded_from_score(self):
        # alpha ranks itself first; the other two put it last.
        rankings = [
            ranking("alpha-7B", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("beta-7B", {"B": 1.0, "C": 2.0, "A": 3.0}),
            ranking("gamma-7B", {"C": 1.0, "B": 2.0, "A": 3.0}),
        ]
        result = consensus.score(session(KEY), rankings)
        by_label = {r["label"]: r for r in result["standings"]}
        # Only the two external votes count, both of which said 3rd.
        self.assertEqual(by_label["A"]["mean_rank"], 3.0)
        self.assertEqual(by_label["A"]["votes"], 2)
        self.assertEqual(by_label["A"]["self_rank"], 1.0)
        self.assertEqual(by_label["A"]["self_bias"], 2.0)   # flattered itself by 2

    def test_self_criticism_is_negative_bias(self):
        rankings = [
            ranking("alpha-7B", {"B": 1.0, "C": 2.0, "A": 3.0}),
            ranking("beta-7B", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("gamma-7B", {"A": 1.0, "C": 2.0, "B": 3.0}),
        ]
        result = consensus.score(session(KEY), rankings)
        by_label = {r["label"]: r for r in result["standings"]}
        self.assertEqual(by_label["A"]["self_bias"], -2.0)

    def test_head_to_head_skips_duels_the_judge_is_in(self):
        rankings = [
            ranking("alpha-7B", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("beta-7B", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("gamma-7B", {"A": 1.0, "B": 2.0, "C": 3.0}),
        ]
        result = consensus.score(session(KEY), rankings)
        by_label = {r["label"]: r for r in result["standings"]}
        # Only gamma's B-vs-C, beta's A-vs-C and alpha's B-vs-C style duels remain,
        # but the winner is unanimous either way.
        self.assertEqual(by_label["A"]["h2h"], 1.0)
        self.assertEqual(by_label["C"]["h2h"], 0.0)

    def test_perfect_agreement(self):
        r = {"A": 1.0, "B": 2.0, "C": 3.0}
        self.assertAlmostEqual(consensus.kendall_w([dict(r), dict(r), dict(r)]), 1.0)

    def test_opposed_judges_agree_less(self):
        w = consensus.kendall_w([{"A": 1.0, "B": 2.0, "C": 3.0},
                                 {"A": 3.0, "B": 2.0, "C": 1.0}])
        self.assertAlmostEqual(w, 0.0)

    def test_agreement_needs_two_full_rankings(self):
        self.assertIsNone(consensus.kendall_w([{"A": 1.0, "B": 2.0}]))
        # Judges that ranked different sets aren't comparable.
        self.assertIsNone(consensus.kendall_w([{"A": 1.0, "B": 2.0},
                                               {"A": 1.0, "C": 2.0}]))

    def test_ties_are_corrected_not_ignored(self):
        """Two judges who both tie everything agree trivially, not perfectly."""
        w = consensus.kendall_w([{"A": 1.5, "B": 1.5, "C": 3.0},
                                 {"A": 1.5, "B": 1.5, "C": 3.0}])
        self.assertIsNotNone(w)
        self.assertLessEqual(w, 1.0)

    def test_non_blind_session_is_not_scored(self):
        result = consensus.score({"blind": False, "key": KEY},
                                 [ranking("alpha-7B", {"A": 1.0, "B": 2.0})])
        self.assertFalse(result["scored"])


class JudgeDistanceTests(unittest.TestCase):

    def result_with(self, *rankings):
        return consensus.score(session(KEY), list(rankings))

    def test_matching_the_consensus_is_zero(self):
        result = self.result_with(
            ranking("j1", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("j2", {"A": 1.0, "B": 2.0, "C": 3.0}))
        self.assertEqual(
            consensus.judge_distance(result, {"A": 1.0, "B": 2.0, "C": 3.0}), 0.0)

    def test_opposite_ranking_is_far(self):
        result = self.result_with(
            ranking("j1", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("j2", {"A": 1.0, "B": 2.0, "C": 3.0}))
        near = consensus.judge_distance(result, {"A": 1.0, "B": 2.0, "C": 3.0})
        far = consensus.judge_distance(result, {"A": 3.0, "B": 2.0, "C": 1.0})
        self.assertLess(near, far)

    def test_partial_ranking_uses_only_shared_labels(self):
        result = self.result_with(
            ranking("j1", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("j2", {"A": 1.0, "B": 2.0, "C": 3.0}))
        d = consensus.judge_distance(result, {"A": 1.0})
        self.assertEqual(d, 0.0)

    def test_none_without_a_consensus_order(self):
        result = consensus.score({"blind": False, "key": KEY}, [])
        self.assertIsNone(consensus.judge_distance(result, {"A": 1.0}))

    def test_none_for_an_empty_ranking(self):
        result = self.result_with(
            ranking("j1", {"A": 1.0, "B": 2.0, "C": 3.0}),
            ranking("j2", {"A": 1.0, "B": 2.0, "C": 3.0}))
        self.assertIsNone(consensus.judge_distance(result, {}))


if __name__ == "__main__":
    unittest.main()

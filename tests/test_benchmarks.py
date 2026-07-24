"""Writing per-session scores.json and rolling it up by sampler profile."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import benchmarks


def _data(name, runs):
    return {"name": name, "dir": "/unused", "runs": runs}


def _result(standings, agreement=0.8, agreement_n=3, scored=True):
    return {"standings": standings, "agreement": agreement,
            "agreement_n": agreement_n, "scored": scored}


def _row(label, model, score, mode="thinking"):
    return {"label": label, "model": model, "mode": mode, "score": score,
            "mean_rank": 1.0, "votes": 3, "self_bias": None}


class SessionScoresTests(unittest.TestCase):

    def test_none_when_not_scored(self):
        data = _data("s1", [])
        self.assertIsNone(benchmarks.session_scores(data, _result([], scored=False)))

    def test_joins_sampler_profile_from_runs_by_label(self):
        data = _data("s1", [{"label": "A", "sampler_profile": "Gemma4 card",
                             "temperature": "1.0"}])
        result = _result([_row("A", "GemmaModel", 0.9)])
        payload = benchmarks.session_scores(data, result)
        self.assertEqual(payload["standings"][0]["sampler_profile"], "Gemma4 card")
        self.assertEqual(payload["standings"][0]["score"], 0.9)


class WriteAndAggregateTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-bench-root-")
        self.spool = tempfile.mkdtemp(prefix="rt-bench-spool-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.spool, ignore_errors=True)

    def _make_session(self, name, rows):
        sdir = os.path.join(self.root, name)
        os.makedirs(sdir, exist_ok=True)
        data = _data(name, [{"label": r["label"],
                             "sampler_profile": r["sampler_profile"],
                             "temperature": "1.0"} for r in rows])
        result = _result([_row(r["label"], r["model"], r["score"]) for r in rows])
        benchmarks.write_session_scores(sdir, data=data, result=result)
        return sdir

    def test_write_then_read_back(self):
        sdir = self._make_session("s1", [{"label": "A", "model": "M1",
                                          "score": 0.7, "sampler_profile": "Card X"}])
        with open(os.path.join(sdir, "scores.json"), encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["standings"][0]["model"], "M1")

    def test_aggregate_averages_across_sessions(self):
        self._make_session("s1", [{"label": "A", "model": "M1", "score": 0.6,
                                   "sampler_profile": "Card X"}])
        self._make_session("s2", [{"label": "A", "model": "M1", "score": 1.0,
                                   "sampler_profile": "Card X"}])
        history = benchmarks.aggregate(self.root, spool_dir=self.spool)
        self.assertEqual(history["Card X"]["runs"], 2)
        self.assertEqual(history["Card X"]["sessions"], 2)
        self.assertAlmostEqual(history["Card X"]["mean_score"], 0.8)

    def test_aggregate_writes_history_file(self):
        self._make_session("s1", [{"label": "A", "model": "M1", "score": 0.5,
                                   "sampler_profile": "Card X"}])
        benchmarks.aggregate(self.root, spool_dir=self.spool)
        loaded = benchmarks.load_history(spool_dir=self.spool)
        self.assertIn("Card X", loaded)

    def test_rows_without_a_profile_are_skipped(self):
        self._make_session("s1", [{"label": "A", "model": "M1", "score": 0.5,
                                   "sampler_profile": ""}])
        history = benchmarks.aggregate(self.root, spool_dir=self.spool)
        self.assertEqual(history, {})

    def test_missing_root_yields_empty_history(self):
        history = benchmarks.aggregate(os.path.join(self.root, "nope"),
                                       spool_dir=self.spool)
        self.assertEqual(history, {})


if __name__ == "__main__":
    unittest.main()

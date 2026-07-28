"""roundtable.chain: selection between stages, and an end-to-end run against
the stub runner (no GPU, no model loads).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import chain, consensus, ranks, session as session_mod

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_runner.sh")


def _run(model, label, rank, error=None):
    return {"model": model, "mode": "thinking", "label": label,
            "body": "## Output\n\nbody of %s" % label, "error": error}


class SelectRunsTests(unittest.TestCase):
    """Selection is decoupled from the next stage's roster -- it only
    narrows *which prior outputs* get shown, ranked by that stage's own
    blind judging.
    """

    def _scored(self, labels):
        standings = [{"label": l, "score": 1.0 - i * 0.1}
                     for i, l in enumerate(labels)]
        return {"standings": standings, "scored": True}

    def test_all_keeps_every_scored_run_in_rank_order(self):
        data = {"runs": [_run("c", "C", 3), _run("a", "A", 1), _run("b", "B", 2)]}
        result = self._scored(["A", "B", "C"])
        chosen = chain._select_runs(data, result, "all")
        self.assertEqual([r["label"] for r in chosen], ["A", "B", "C"])

    def test_top3_truncates_a_larger_field(self):
        data = {"runs": [_run(m, l, 0) for m, l in
                         zip("abcde", "ABCDE")]}
        result = self._scored(["B", "D", "A", "C", "E"])
        chosen = chain._select_runs(data, result, "top3")
        self.assertEqual([r["label"] for r in chosen], ["B", "D", "A"])

    def test_top1_keeps_only_the_winner(self):
        data = {"runs": [_run(m, l, 0) for m, l in zip("ab", "AB")]}
        result = self._scored(["B", "A"])
        chosen = chain._select_runs(data, result, "top1")
        self.assertEqual([r["label"] for r in chosen], ["B"])

    def test_errored_runs_are_never_carried_forward(self):
        data = {"runs": [_run("a", "A", 0, error="boom"), _run("b", "B", 0)]}
        result = self._scored(["A", "B"])
        chosen = chain._select_runs(data, result, "all")
        self.assertEqual([r["label"] for r in chosen], ["B"])

    def test_falls_back_to_every_successful_run_when_nothing_is_scored(self):
        # Not blind, or too few outputs to judge -- no ranking signal exists,
        # so "top N" degrades to "everything that produced usable output"
        # rather than fabricating an order.
        data = {"runs": [_run("a", "", 0), _run("b", "", 0, error="boom")]}
        result = {"standings": [], "scored": False}
        chosen = chain._select_runs(data, result, "top1")
        self.assertEqual([r["model"] for r in chosen], ["a"])


class PreviousBlockTests(unittest.TestCase):
    def test_empty_selection_says_so_rather_than_an_empty_block(self):
        self.assertIn("no usable output", chain._previous_block([]))

    def test_candidates_are_lettered_not_named(self):
        chosen = [_run("qwen-27b", "A", 0), _run("gemma-26b", "B", 0)]
        text = chain._previous_block(chosen)
        self.assertIn("**Candidate A:**", text)
        self.assertIn("**Candidate B:**", text)
        self.assertNotIn("qwen-27b", text)
        self.assertNotIn("gemma-26b", text)


class FillTests(unittest.TestCase):
    def test_previous_and_manuscript_placeholders(self):
        out = chain._fill("before {{PREVIOUS}} / {{MANUSCRIPT}} after",
                          "PREV", "MS")
        self.assertEqual(out, "before PREV / MS after")

    def test_manuscript_placeholder_left_alone_when_no_manuscript_given(self):
        out = chain._fill("{{MANUSCRIPT}} stays", "", None)
        self.assertEqual(out, "{{MANUSCRIPT}} stays")


class LoadSpecTests(unittest.TestCase):
    def test_rejects_an_unknown_use_previous_value(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "spec.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"stages": [{"name": "a", "use_previous": "top5"}]}, f)
            with self.assertRaises(ValueError):
                chain.load_spec(path)

    def test_rejects_an_empty_stage_list(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "spec.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"stages": []}, f)
            with self.assertRaises(ValueError):
                chain.load_spec(path)


class EndToEndTests(unittest.TestCase):
    """Two stages against the stub runner: does the winner from stage 1
    actually show up inside stage 2's prompt, and nothing else does?
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtchain-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_top1_handoff_carries_only_the_winning_output_forward(self):
        spec = {
            "stages": [
                {"name": "analyze", "models": ["stub-a-7B", "stub-b-7B"],
                 "system_prompt": "sys one",
                 "user_prompt": "analyze this"},
                {"name": "plan", "models": ["stub-c-7B"], "use_previous": "top1",
                 "system_prompt": "sys two",
                 "user_prompt": "**PREVIOUS STEP OUTPUT:**\n{{PREVIOUS}}"},
            ]
        }
        root = os.path.join(self.tmp, "out")
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None)

        self.assertEqual(len(manifest["stages"]), 2)
        self.assertEqual(len(manifest["stages"][0]["passed_forward"]), 1)

        stage2_dir = manifest["stages"][1]["session_dir"]
        with open(os.path.join(stage2_dir, "user-prompt.txt"),
                 encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("Candidate A", prompt)
        self.assertNotIn("Candidate B", prompt)
        self.assertIn("Stub output number", prompt)

        self.assertTrue(os.path.exists(os.path.join(root, "chain.json")))
        self.assertTrue(os.path.exists(os.path.join(root, "report.html")))
        for s in manifest["stages"]:
            self.assertTrue(os.path.exists(os.path.join(s["session_dir"], "report.html")))

    def test_all_handoff_carries_the_whole_field_forward(self):
        spec = {
            "stages": [
                {"name": "analyze", "models": ["stub-a-7B", "stub-b-7B"],
                 "system_prompt": "sys", "user_prompt": "go"},
                {"name": "plan", "models": ["stub-c-7B"], "use_previous": "all",
                 "system_prompt": "sys", "user_prompt": "{{PREVIOUS}}"},
            ]
        }
        root = os.path.join(self.tmp, "out")
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None)
        self.assertEqual(len(manifest["stages"][0]["passed_forward"]), 2)

    def test_manuscript_file_is_substituted_into_every_stage(self):
        ms_path = os.path.join(self.tmp, "manuscript.txt")
        with open(ms_path, "w", encoding="utf-8") as f:
            f.write("Once upon a time, THE_MANUSCRIPT_TEXT.")
        spec = {
            "manuscript": ms_path,
            "stages": [
                {"name": "analyze", "models": ["stub-a-7B"],
                 "system_prompt": "sys", "user_prompt": "{{MANUSCRIPT}}"},
            ]
        }
        root = os.path.join(self.tmp, "out")
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None)
        stage_dir = manifest["stages"][0]["session_dir"]
        with open(os.path.join(stage_dir, "user-prompt.txt"),
                 encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("THE_MANUSCRIPT_TEXT", prompt)

    def test_inline_manuscript_text_is_used_over_a_file_path(self):
        spec = {
            "manuscript_text": "INLINE_TEXT",
            "manuscript": os.path.join(self.tmp, "does-not-exist.txt"),
            "stages": [
                {"name": "analyze", "models": ["stub-a-7B"],
                 "system_prompt": "sys", "user_prompt": "{{MANUSCRIPT}}"},
            ]
        }
        root = os.path.join(self.tmp, "out")
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None)
        stage_dir = manifest["stages"][0]["session_dir"]
        with open(os.path.join(stage_dir, "user-prompt.txt"),
                 encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("INLINE_TEXT", prompt)


class OwnAndMetaSummaryTests(unittest.TestCase):
    """'own' (per-model threads) and 'meta_summary' (Round 3) end to end,
    against the stub runner.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtchain-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_own_stage_gives_each_model_its_own_previous_output(self):
        spec = {
            "stages": [
                {"name": "plan", "models": ["stub-a-7B", "stub-b-7B"],
                 "system_prompt": "sys", "user_prompt": "plan"},
                {"name": "rewrite", "use_previous": "own",
                 "system_prompt": "sys", "user_prompt": "{{PREVIOUS}}"},
            ]
        }
        root = os.path.join(self.tmp, "out")
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None)

        self.assertEqual(len(manifest["stages"]), 2)
        # Both of stage 1's models continued their own thread, not a merged one.
        self.assertEqual(len(manifest["stages"][0]["passed_forward"]), 2)

        stage2_dir = manifest["stages"][1]["session_dir"]
        data = session_mod.load(stage2_dir)
        self.assertEqual(len(data["runs"]), 2)   # one thread per stage-1 survivor
        self.assertTrue(os.path.exists(os.path.join(root, "chain.json")))

    def test_own_stage_can_be_restricted_by_a_models_filter(self):
        spec = {
            "stages": [
                {"name": "plan", "models": ["stub-a-7B", "stub-b-7B"],
                 "system_prompt": "sys", "user_prompt": "plan"},
                {"name": "rewrite", "use_previous": "own", "models": ["stub-a"],
                 "system_prompt": "sys", "user_prompt": "{{PREVIOUS}}"},
            ]
        }
        root = os.path.join(self.tmp, "out")
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None)
        stage2_dir = manifest["stages"][1]["session_dir"]
        data = session_mod.load(stage2_dir)
        self.assertEqual(len(data["runs"]), 1)

    def test_meta_summary_produces_a_round3_file(self):
        spec = {
            "stages": [
                {"name": "judge", "models": ["stub-a-7B", "stub-b-7B"],
                 "system_prompt": "sys", "user_prompt": "go",
                 "meta_summary": True},
            ]
        }
        root = os.path.join(self.tmp, "out")
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None)
        session_dir = manifest["stages"][0]["session_dir"]
        data = session_mod.load(session_dir)
        self.assertIsNotNone(data["meta_summary"])


class ShouldStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtchain-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_delay = os.environ.get("STUB_DELAY")
        os.environ["STUB_DELAY"] = "0"

    def tearDown(self):
        if self._old_delay is None:
            os.environ.pop("STUB_DELAY", None)
        else:
            os.environ["STUB_DELAY"] = self._old_delay

    def test_stopping_before_a_stage_keeps_earlier_stages_and_marks_stopped(self):
        spec = {
            "stages": [
                {"name": "one", "models": ["stub-a-7B"],
                 "system_prompt": "sys", "user_prompt": "go"},
                {"name": "two", "models": ["stub-b-7B"],
                 "system_prompt": "sys", "user_prompt": "go"},
            ]
        }
        root = os.path.join(self.tmp, "out")

        # should_stop() is now also polled inside a stage's own subprocess
        # loop (see the mid-stage test below), so real subprocess/poll timing
        # can't cleanly isolate "stopped between stages" from "stopped mid
        # stage" -- both call the same function. Stub _run_stage itself
        # instead: it's the one thing standing between "top of the stage
        # loop" and "inside a subprocess", so replacing it makes only the
        # between-stages check exercise should_stop() at all.
        calls = {"n": 0}

        def fake_run_stage(job, stage_root, **kwargs):
            os.makedirs(stage_root, exist_ok=True)
            with open(os.path.join(stage_root, "system-prompt.txt"), "w") as f:
                f.write(job["system_prompt"])
            with open(os.path.join(stage_root, "user-prompt.txt"), "w") as f:
                f.write(job["user_prompt"])
            with open(os.path.join(stage_root, "01_x_stub-a-7B_thinking.md"), "w") as f:
                f.write('---\nmodel: "stub-a-7B"\nthinking: true\nerror: null\n---'
                       '\n\n## Output\n\nstub\n')
            return stage_root

        def stop_after_first_stage():
            calls["n"] += 1
            return calls["n"] > 1

        with mock.patch("roundtable.chain._run_stage", side_effect=fake_run_stage):
            manifest = chain.run_chain(spec, root, log=lambda *a: None,
                                       should_stop=stop_after_first_stage)
        self.assertTrue(manifest.get("stopped"))
        self.assertEqual(len(manifest["stages"]), 1)
        self.assertTrue(os.path.exists(os.path.join(root, "chain.json")))

    def test_stopping_mid_stage_kills_the_subprocess_and_keeps_nothing_from_it(self):
        # Unlike the between-stages case above, this should_stop turns true
        # while stage one's own subprocess is still polling -- the same
        # kill-the-process-group path a cancelled plain job takes.
        os.environ["STUB_DELAY"] = "1"
        spec = {
            "stages": [
                {"name": "one", "models": ["stub-a-7B", "stub-b-7B"],
                 "system_prompt": "sys", "user_prompt": "go"},
            ]
        }
        root = os.path.join(self.tmp, "out")

        def stop_soon():
            return time.time() - started > 1.5

        started = time.time()
        manifest = chain.run_chain(spec, root, runner=STUB, log=lambda *a: None,
                                   should_stop=stop_soon)
        self.assertTrue(manifest.get("stopped"))
        self.assertEqual(manifest["stages"], [])


if __name__ == "__main__":
    unittest.main()

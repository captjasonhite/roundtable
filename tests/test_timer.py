"""The report rebuild cadence: event-driven first, adaptive fallback second."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import spool, worker

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_runner.sh")


class ArtifactCountTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-art-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_counts_results_and_verdicts_only(self):
        for name in ("01_x_model_thinking.md", "summary_x_model.md",
                     "SUMMARIZE.md", "SUMMARIZE-KEY.md", "report.html",
                     "system-prompt.txt"):
            open(os.path.join(self.dir, name), "w").close()
        # The two SUMMARIZE files are scaffolding, not progress.
        self.assertEqual(worker._artifacts(self.dir), 2)

    def test_missing_directory_is_zero(self):
        self.assertEqual(worker._artifacts("/does/not/exist"), 0)


class CadenceTests(unittest.TestCase):
    """Drive a real run and watch when rebuilds happen."""

    def setUp(self):
        self.spool = tempfile.mkdtemp(prefix="rt-spool-")
        self.sessions = tempfile.mkdtemp(prefix="rt-sessions-")
        spool.ensure(self.spool)

    def tearDown(self):
        shutil.rmtree(self.spool, ignore_errors=True)
        shutil.rmtree(self.sessions, ignore_errors=True)

    def test_rebuilds_track_results_not_the_clock(self):
        """Six artifacts appear ~1s apart; the fallback timer is 60s.

        With a clock-driven rebuild we would get one. Event-driven, we get one
        per artifact -- which is what makes the page fill in as runs land.
        """
        calls = []
        os.environ["STUB_DELAY"] = "1"
        spool.submit({"id": "cadence", "user_prompt": "u", "system_prompt": "s",
                      "mode": "thinking", "sessions_root": self.sessions,
                      "models": ["stub-a-7B-Q4_K_M", "stub-b-7B-Q4_K_M",
                                 "stub-c-7B-Q4_K_M"]}, self.spool)
        worker.loop(sp=self.spool, runner=STUB, once=True, report_every=60.0,
                    on_report=lambda d, running: calls.append(running),
                    log=lambda *a: None)
        # 3 runs + 3 verdicts = 6 events, plus the final static write.
        self.assertGreaterEqual(len(calls), 6)
        self.assertFalse(calls[-1], "last write must drop the refresh tag")
        self.assertTrue(all(calls[:-1]), "earlier writes must self-refresh")

    def test_interval_is_recorded_in_the_heartbeat(self):
        os.environ["STUB_DELAY"] = "1"
        spool.submit({"id": "beat", "user_prompt": "u", "system_prompt": "s",
                      "mode": "thinking", "sessions_root": self.sessions,
                      "models": ["stub-a-7B-Q4_K_M", "stub-b-7B-Q4_K_M"]},
                     self.spool)
        seen = {}

        def capture(session_dir, running):
            beat = spool.read_heartbeat(self.spool) or {}
            if "report_interval" in beat:
                seen.update(beat)

        worker.loop(sp=self.spool, runner=STUB, once=True, report_every=10.0,
                    on_report=capture, log=lambda *a: None)
        self.assertIn("report_interval", seen)
        # Artifacts land ~1s apart, so the tuned interval clamps to the floor.
        self.assertEqual(seen["report_interval"], worker.REPORT_MIN)
        self.assertGreaterEqual(seen.get("done", 0), 1)


if __name__ == "__main__":
    unittest.main()

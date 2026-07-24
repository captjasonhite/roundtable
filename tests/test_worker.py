"""The worker end to end, against a stub runner -- no GPU, no model loads.

tests/stub_runner.sh writes a session directory with the same shape the real
bench script produces, so this exercises the whole path: queue -> claim ->
subprocess -> live report rebuilds -> final report -> done/.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import consensus, ranks, report, session as session_mod
from roundtable import spool, worker

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_runner.sh")


def render(session_dir, running):
    """The same wiring bin/roundtable uses for its live rebuilds."""
    data = session_mod.load(session_dir)
    if not data:
        return None
    rankings = ranks.extract_all(data)
    result = consensus.score(data, rankings)
    html = report.render(data, result, rankings, running=running)
    return report.write(os.path.join(session_dir, "report.html"), html)


class DefaultRunnerTests(unittest.TestCase):
    """Which creative-bench.sh gets used when nothing explicit is passed."""

    def setUp(self):
        self._old_env = os.environ.get("ROUNDTABLE_RUNNER")
        os.environ.pop("ROUNDTABLE_RUNNER", None)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("ROUNDTABLE_RUNNER", None)
        else:
            os.environ["ROUNDTABLE_RUNNER"] = self._old_env

    def test_env_override_always_wins(self):
        os.environ["ROUNDTABLE_RUNNER"] = "/custom/path.sh"
        self.assertEqual(worker._default_runner(), "/custom/path.sh")

    def test_falls_back_to_the_bundled_copy_when_no_personal_one_exists(self):
        with mock.patch("os.path.exists", return_value=False):
            got = worker._default_runner()
        self.assertEqual(got, worker._BUNDLED_RUNNER)
        self.assertTrue(os.path.exists(worker._BUNDLED_RUNNER),
                        "the bundled copy this falls back to must actually exist")

    def test_prefers_a_personal_copy_over_the_bundled_one(self):
        with mock.patch("os.path.exists", return_value=True):
            got = worker._default_runner()
        self.assertEqual(got, os.path.expanduser("~/Apps/bin/creative-bench.sh"))


class BuildCommandTests(unittest.TestCase):

    def test_flags_and_env(self):
        job = {"system_prompt": "sys", "user_prompt": "usr", "temperature": 0.9,
               "mode": "both", "models": ["alpha.gguf", "beta.gguf"],
               "summarize": True, "seed": 7, "sessions_root": "/tmp/sessions"}
        tmp = tempfile.mkdtemp(prefix="rt-cmd-")
        try:
            argv, env = worker.build_command(job, runner="/bin/true", prompt_dir=tmp)
            self.assertEqual(argv[0], "/bin/true")
            self.assertIn("--yes", argv)
            self.assertIn("--summarize", argv)
            self.assertEqual(argv[argv.index("--temp") + 1], "0.9")
            self.assertEqual(argv[argv.index("--mode") + 1], "both")
            self.assertEqual(argv[argv.index("--models") + 1], "alpha.gguf,beta.gguf")
            self.assertEqual(env["OUTDIR"], "/tmp/sessions")
            self.assertEqual(env["SEED"], "7")
            # Prompts became files, because that is what the runner accepts.
            with open(argv[argv.index("--system") + 1]) as f:
                self.assertEqual(f.read(), "sys")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_summarize_and_no_blind(self):
        tmp = tempfile.mkdtemp(prefix="rt-cmd-")
        try:
            argv, _ = worker.build_command(
                {"summarize": False, "blind": False, "user_prompt": "u"},
                runner="/bin/true", prompt_dir=tmp)
            self.assertIn("--no-summarize", argv)
            self.assertIn("--no-blind", argv)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WorkerLoopTests(unittest.TestCase):

    def setUp(self):
        self.spool = tempfile.mkdtemp(prefix="rt-spool-")
        self.sessions = tempfile.mkdtemp(prefix="rt-sessions-")
        spool.ensure(self.spool)

    def tearDown(self):
        shutil.rmtree(self.spool, ignore_errors=True)
        shutil.rmtree(self.sessions, ignore_errors=True)

    def job(self, **extra):
        job = {"user_prompt": "write something", "system_prompt": "be brief",
               "temperature": 1.0, "mode": "thinking",
               "models": ["stub-alpha-7B-Q4_K_M", "stub-beta-7B-Q4_K_M"],
               "sessions_root": self.sessions}
        job.update(extra)
        return job

    def test_full_run_produces_a_report(self):
        spool.submit(self.job(id="e2e"), self.spool)
        os.environ["STUB_DELAY"] = "0"
        ran = worker.loop(sp=self.spool, runner=STUB, on_report=render,
                          once=True, report_every=0.1, log=lambda *a: None)
        self.assertEqual(ran, 1)

        with open(os.path.join(self.spool, "done", "e2e.json")) as f:
            record = json.load(f)
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["exit_code"], 0)
        sdir = record["session_dir"]
        self.assertTrue(sdir and os.path.isdir(sdir))

        # The report exists, and the final write dropped the refresh tag.
        with open(os.path.join(sdir, "report.html")) as f:
            html = f.read()
        self.assertNotIn("http-equiv=\"refresh\"", html)
        self.assertIn("Panel standings", html)
        # The stub's judges use the exact RANKING: line, so parsing is exact.
        data = session_mod.load(sdir)
        parsed = ranks.extract_all(data)
        self.assertTrue(parsed)
        self.assertTrue(all(r["method"] == "ranking-line" for r in parsed))

    def test_live_report_carries_refresh_tag(self):
        """Mid-run rebuilds must self-refresh; only the last write is static."""
        seen = []

        def spy(session_dir, running):
            seen.append(running)
            return render(session_dir, running)

        spool.submit(self.job(id="live"), self.spool)
        os.environ["STUB_DELAY"] = "1"
        worker.loop(sp=self.spool, runner=STUB, on_report=spy, once=True,
                    report_every=0.1, log=lambda *a: None)
        self.assertIn(True, seen)          # at least one live rebuild happened
        self.assertFalse(seen[-1])         # the last one was the final write

    def test_runner_failure_is_recorded_not_raised(self):
        spool.submit(self.job(id="boom"), self.spool)
        worker.loop(sp=self.spool, runner="/bin/false", on_report=render,
                    once=True, log=lambda *a: None)
        with open(os.path.join(self.spool, "failed", "boom.json")) as f:
            record = json.load(f)
        self.assertEqual(record["state"], "failed")
        self.assertIn("exited", record["error"])

    def test_missing_runner_is_recorded_not_raised(self):
        spool.submit(self.job(id="nope"), self.spool)
        worker.loop(sp=self.spool, runner="/does/not/exist", on_report=render,
                    once=True, log=lambda *a: None)
        self.assertEqual(spool.counts(self.spool)["failed"], 1)

    def test_round_3_runs_automatically_after_round_2(self):
        """meta_summary defaults True and Round 2 succeeded, so Round 3 must run.

        Every judge in the stub agrees A>B, so alpha is the unambiguous top pick
        and its slug must be what gets passed as --meta-model.
        """
        os.environ["STUB_DELAY"] = "0"
        spool.submit(self.job(id="r3"), self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "done", "r3.json")) as f:
            record = json.load(f)
        self.assertTrue(record["meta_summary"])
        sdir = record["session_dir"]
        data = session_mod.load(sdir)
        self.assertIsNotNone(data["meta_summary"])
        self.assertEqual(data["meta_summary"]["model"], "stub-alpha-7B-Q4_K_M")
        with open(os.path.join(sdir, "report.html")) as f:
            self.assertIn("Round 3", f.read())

    def test_round_3_is_skipped_when_not_requested(self):
        os.environ["STUB_DELAY"] = "0"
        spool.submit(self.job(id="no-r3", meta_summary=False), self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "done", "no-r3.json")) as f:
            record = json.load(f)
        self.assertFalse(record["meta_summary"])
        self.assertIsNone(session_mod.load(record["session_dir"])["meta_summary"])

    def test_round_3_is_skipped_when_round_2_did_not_run(self):
        """No judges, nothing to synthesise -- Round 3 must not invent one."""
        os.environ["STUB_DELAY"] = "0"
        spool.submit(self.job(id="no-judges", summarize=False), self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "done", "no-judges.json")) as f:
            record = json.load(f)
        self.assertFalse(record["meta_summary"])

    def test_worker_sessions_root_reaches_the_job(self):
        """Regression: a form job names no sessions_root; the worker's must win.

        Before the fix, the worker fell back to DEFAULT_SESSIONS while the HTTP
        server browsed the --sessions directory, so the job page never found
        the report.
        """
        job = self.job(id="rooted")
        del job["sessions_root"]                    # exactly what /submit produces
        spool.submit(job, self.spool)
        os.environ["STUB_DELAY"] = "0"
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, sessions_root=self.sessions,
                    log=lambda *a: None)
        with open(os.path.join(self.spool, "done", "rooted.json")) as f:
            record = json.load(f)
        self.assertTrue(record["session_dir"].startswith(self.sessions + os.sep),
                        record["session_dir"])

    def test_two_jobs_run_sequentially(self):
        os.environ["STUB_DELAY"] = "0"
        spool.submit(self.job(id="first"), self.spool)
        spool.submit(self.job(id="second"), self.spool)
        ran = worker.loop(sp=self.spool, runner=STUB, on_report=render,
                          drain=True, report_every=0.1, log=lambda *a: None)
        self.assertEqual(ran, 2)
        self.assertEqual(spool.counts(self.spool)["done"], 2)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)


if __name__ == "__main__":
    unittest.main()

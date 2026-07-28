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


def _listing_len(root):
    return len([n for n in os.listdir(root)
                if os.path.isdir(os.path.join(root, n))])


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


class StoppingSurvivesAHungKillTests(unittest.TestCase):
    """A stop/cancel whose SIGTERM doesn't land within the wait has to escalate
    and still raise Stopping/Cancelled, not let subprocess.TimeoutExpired --
    a plain Exception -- escape and get mistaken for an ordinary job failure
    by the caller's generic handler."""

    def setUp(self):
        self.spool = tempfile.mkdtemp(prefix="rt-spool-")
        self.sessions = tempfile.mkdtemp(prefix="rt-sessions-")
        spool.ensure(self.spool)

    def tearDown(self):
        shutil.rmtree(self.spool, ignore_errors=True)
        shutil.rmtree(self.sessions, ignore_errors=True)

    def test_a_hung_sigterm_escalates_to_sigkill_and_still_raises_stopping(self):
        import subprocess as subprocess_mod

        fake = mock.MagicMock()
        fake.pid = 4242
        fake.poll.return_value = None
        fake.wait.side_effect = [subprocess_mod.TimeoutExpired("stub", 30), None]

        job = {"user_prompt": "hi", "system_prompt": "be brief",
               "temperature": 1.0, "mode": "thinking", "models": ["stub-alpha"],
               "sessions_root": self.sessions}
        spool.submit(job, self.spool)
        claimed, running_path = spool.claim(self.spool)

        with mock.patch("roundtable.worker.subprocess.Popen", return_value=fake), \
             mock.patch("roundtable.worker.os.getpgid", return_value=99), \
             mock.patch("roundtable.worker.os.killpg") as killpg, \
             mock.patch("roundtable.worker.time.sleep"):
            with self.assertRaises(worker.Stopping):
                worker.run_job(claimed, running_path, sp=self.spool,
                               runner=STUB, should_stop=lambda: True)

        # SIGTERM, then a timed-out wait, then SIGKILL -- not silently dropped.
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(fake.wait.call_count, 2)


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

    def test_cancelling_a_running_job_stops_it_and_keeps_going(self):
        """The reason cancel exists: a run that is wrong from the first output."""
        spool.submit(self.job(id="cancel-me"), self.spool)
        spool.submit(self.job(id="next-one"), self.spool)
        os.environ["STUB_DELAY"] = "2"

        def spy(session_dir, running):
            # Once anything has been produced, ask for the run to stop.
            spool.request_cancel("cancel-me", self.spool)
            return render(session_dir, running)

        ran = worker.loop(sp=self.spool, runner=STUB, on_report=spy, drain=True,
                          report_every=0.1, log=lambda *a: None)
        self.assertEqual(ran, 1, "the second job must still run")

        with open(os.path.join(self.spool, "failed", "cancel-me.json")) as f:
            record = json.load(f)
        self.assertTrue(record["cancelled"])
        self.assertEqual(record["error"], "cancelled")
        self.assertTrue(os.path.exists(
            os.path.join(self.spool, "done", "next-one.json")))
        # The request must not survive to cancel the next job as well.
        self.assertFalse(spool.cancel_requested("next-one", self.spool))

    def test_cancelling_a_queued_job_stops_it_before_it_starts(self):
        spool.submit(self.job(id="never-runs"), self.spool)
        spool.request_cancel("never-runs", self.spool)
        os.environ["STUB_DELAY"] = "0"
        ran = worker.loop(sp=self.spool, runner=STUB, on_report=render,
                          drain=True, log=lambda *a: None)
        self.assertEqual(ran, 0)
        with open(os.path.join(self.spool, "failed", "never-runs.json")) as f:
            self.assertTrue(json.load(f)["cancelled"])
        self.assertEqual(_listing_len(self.sessions), 0)

    def test_a_cancelled_run_stops_advertising_itself_as_live(self):
        spool.submit(self.job(id="settle-me"), self.spool)
        os.environ["STUB_DELAY"] = "2"

        def spy(session_dir, running):
            written = render(session_dir, running)
            if written:          # only once there is a report to leave behind
                spool.request_cancel("settle-me", self.spool)
            return written

        worker.loop(sp=self.spool, runner=STUB, on_report=spy, drain=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "failed", "settle-me.json")) as f:
            sdir = json.load(f)["session_dir"]
        with open(os.path.join(sdir, "report.html")) as f:
            self.assertNotIn('http-equiv="refresh"', f.read())

    def test_a_worker_reaps_the_claim_a_reboot_left_behind(self):
        """What a rebooted machine leaves: a claim nobody will ever finish."""
        spool.submit(self.job(id="orphaned"), self.spool)
        _, path = spool.claim(self.spool)
        spool.note(path, session_dir=self.sessions)
        spool.heartbeat(self.spool, state="running", job="orphaned", pid=999999)

        settled = []
        worker.loop(sp=self.spool, runner=STUB, drain=True,
                    on_report=lambda sdir, running: settled.append(running),
                    log=lambda *a: None)
        self.assertEqual(spool.counts(self.spool)["running"], 0)
        with open(os.path.join(self.spool, "failed", "orphaned.json")) as f:
            record = json.load(f)
        self.assertTrue(record["reaped"])
        self.assertEqual(settled, [False])   # its report was rewritten as static

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

    def test_round_3_is_counted_as_a_pending_run_before_it_starts(self):
        """The bench script can't count Round 3 -- it's the worker's step. The
        worker records it, so the report doesn't read 'complete' while the
        synthesis model is still loading."""
        os.environ["STUB_DELAY"] = "0"
        spool.submit(self.job(id="r3-count"), self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "done", "r3-count.json")) as f:
            sdir = json.load(f)["session_dir"]
        self.assertEqual(session_mod.load(sdir)["expected_meta"], 1)
        counts = report._counts(session_mod.load(sdir))
        self.assertEqual(counts[4], counts[5])           # it did run, so it's done

    def test_round_3_is_on_the_count_from_the_first_report(self):
        """Not just before Round 3 starts -- from the moment the session exists.

        It used to be written only after the bench script exited, so the
        progress bar and the Judge runs tile grew a seventh unit two rounds in
        instead of showing '0/1 summary' from the start.
        """
        os.environ["STUB_DELAY"] = "1"
        seen = []

        def watch(session_dir, running):
            seen.append(session_mod.load(session_dir)["expected_meta"])
            return render(session_dir, running)

        spool.submit(self.job(id="r3-early"), self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=watch, once=True,
                    report_every=0.1, log=lambda *a: None)
        self.assertTrue(seen)
        self.assertEqual(seen[0], 1)

    def test_a_skipped_round_3_is_taken_back_off_the_count(self):
        """Opted out: the report must not sit one run short of complete."""
        os.environ["STUB_DELAY"] = "0"
        spool.submit(self.job(id="r3-off", meta_summary=False), self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "done", "r3-off.json")) as f:
            sdir = json.load(f)["session_dir"]
        data = session_mod.load(sdir)
        self.assertEqual(data["expected_meta"], 0)
        counts = report._counts(data)
        self.assertEqual(counts[4], counts[5])

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

    def _finished_session(self, job_id="orig"):
        """Run a normal job through and hand back its session dir."""
        os.environ["STUB_DELAY"] = "0"
        spool.submit(self.job(id=job_id, meta_summary=False), self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "done", job_id + ".json")) as f:
            return json.load(f)["session_dir"]

    def test_rejudging_replaces_the_panel_and_keeps_the_outputs(self):
        sdir = self._finished_session()
        before = session_mod.load(sdir)
        self.assertEqual([j["judge"] for j in before["judges"]],
                         ["stub-alpha-7B-Q4_K_M", "stub-beta-7B-Q4_K_M"])

        spool.submit({"id": "rejudge", "judge_only": sdir,
                      "judges": ["outsider-13B-Q4_K_M"], "meta_summary": False,
                      "sessions_root": self.sessions}, self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)

        after = session_mod.load(sdir)
        # The new panel, and only the new panel: two panels pooled into one set
        # of standings is the bug this archiving exists to prevent.
        self.assertEqual([j["judge"] for j in after["judges"]],
                         ["outsider-13B-Q4_K_M"])
        self.assertEqual(after["expected_judges"], 1)
        # The contest itself is untouched.
        self.assertEqual([r["model"] for r in after["runs"]],
                         [r["model"] for r in before["runs"]])
        self.assertEqual(after["user_prompt"], before["user_prompt"])

    def test_rejudging_keeps_the_old_verdicts_on_disk(self):
        """Archived, not deleted -- they cost GPU time to produce."""
        sdir = self._finished_session("orig2")
        spool.submit({"id": "rejudge2", "judge_only": sdir,
                      "judges": ["outsider-13B-Q4_K_M"], "meta_summary": False,
                      "sessions_root": self.sessions}, self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        archived = os.listdir(os.path.join(sdir, ".judges-1"))
        self.assertTrue([n for n in archived if n.startswith("summary_")], archived)

    def test_a_second_rejudge_archives_into_its_own_directory(self):
        sdir = self._finished_session("orig3")
        for n in (1, 2):
            spool.submit({"id": "rj%d" % n, "judge_only": sdir,
                          "judges": ["outsider-13B-Q4_K_M"], "meta_summary": False,
                          "sessions_root": self.sessions}, self.spool)
            worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                        report_every=0.1, log=lambda *a: None)
        self.assertTrue(os.path.isdir(os.path.join(sdir, ".judges-1")))
        self.assertTrue(os.path.isdir(os.path.join(sdir, ".judges-2")))

    def test_a_judge_only_job_names_its_session_without_creating_one(self):
        sdir = self._finished_session("orig4")
        before = len(os.listdir(self.sessions))
        spool.submit({"id": "rj-record", "judge_only": sdir,
                      "judges": ["outsider-13B-Q4_K_M"], "meta_summary": False,
                      "sessions_root": self.sessions}, self.spool)
        worker.loop(sp=self.spool, runner=STUB, on_report=render, once=True,
                    report_every=0.1, log=lambda *a: None)
        with open(os.path.join(self.spool, "done", "rj-record.json")) as f:
            record = json.load(f)
        self.assertEqual(record["session_dir"], sdir)
        self.assertEqual(len(os.listdir(self.sessions)), before)

    def test_a_chain_job_runs_through_the_normal_queue(self):
        os.environ["STUB_DELAY"] = "0"
        spec = {
            "stages": [
                {"name": "one", "models": ["stub-a-7B"],
                 "system_prompt": "sys", "user_prompt": "go"},
                {"name": "two", "models": ["stub-b-7B"], "use_previous": "top1",
                 "system_prompt": "sys", "user_prompt": "{{PREVIOUS}}"},
            ]
        }
        spool.submit({"id": "chain1", "chain_spec": spec, "chain_name": "test",
                     "sessions_root": self.sessions}, self.spool)
        ran = worker.loop(sp=self.spool, runner=STUB, on_report=render,
                          once=True, report_every=0.1, log=lambda *a: None)
        self.assertEqual(ran, 1)

        with open(os.path.join(self.spool, "done", "chain1.json")) as f:
            record = json.load(f)
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["stages"], 2)
        root = record["session_dir"]
        self.assertTrue(os.path.isdir(root))
        self.assertTrue(os.path.exists(os.path.join(root, "chain.json")))
        self.assertTrue(os.path.exists(os.path.join(root, "report.html")))

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

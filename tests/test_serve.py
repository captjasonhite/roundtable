"""The HTTP layer, driven over a real socket on an ephemeral port."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import serve, spool


class ResolveTests(unittest.TestCase):
    """The one security-relevant function: no URL may escape the root."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-root-")
        os.makedirs(os.path.join(self.root, "sess"))
        open(os.path.join(self.root, "sess", "report.html"), "w").close()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_normal_path_resolves(self):
        got = serve._resolve(self.root, "sess/report.html")
        self.assertEqual(got, os.path.join(os.path.realpath(self.root),
                                           "sess", "report.html"))

    def test_dot_dot_is_refused(self):
        for attempt in ("../../etc/passwd", "sess/../../etc/passwd",
                        "..%2f..%2fetc%2fpasswd"):
            self.assertIsNone(serve._resolve(self.root, attempt), attempt)

    def test_absolute_path_is_neutralised_into_the_root(self):
        """A leading slash is stripped, so it can only ever land inside root."""
        got = serve._resolve(self.root, "/etc/passwd")
        self.assertTrue(got.startswith(os.path.realpath(self.root) + os.sep))
        self.assertFalse(os.path.exists(got))

    def test_symlink_out_of_root_is_refused(self):
        link = os.path.join(self.root, "escape")
        os.symlink("/etc", link)
        self.assertIsNone(serve._resolve(self.root, "escape/passwd"))


class FormValidationTests(unittest.TestCase):

    def base(self, **extra):
        fields = {"user_prompt": "write something", "think_on": ["alpha"],
                  "think_off": [], "mode": "thinking", "temperature": "1.0"}
        fields.update(extra)
        return fields

    def test_valid_form_becomes_a_job(self):
        job, error = serve.job_from_form(self.base(
            system_prompt=" be brief ", preset="line-editor",
            summarize="1", seed="42", max_tokens="900"))
        self.assertIsNone(error)
        self.assertEqual(job["user_prompt"], "write something")
        self.assertEqual(job["system_prompt"], "be brief")
        self.assertEqual(job["models"], ["alpha:thinking"])
        self.assertEqual(job["seed"], 42)
        self.assertEqual(job["env"], {"MAX_TOKENS": "900"})
        self.assertEqual(job["preset"], "line-editor")
        self.assertTrue(job["summarize"])
        self.assertTrue(job["blind"])

    def test_prompt_is_required(self):
        job, error = serve.job_from_form(self.base(user_prompt="   "))
        self.assertIsNone(job)
        self.assertIn("prompt is required", error)

    def test_at_least_one_model(self):
        job, error = serve.job_from_form(self.base(think_on=[], think_off=[]))
        self.assertIsNone(job)
        self.assertIn("at least one model", error)

    def test_think_off_only(self):
        job, _ = serve.job_from_form(
            self.base(think_on=[], think_off=["Cydonia"]))
        self.assertEqual(job["models"], ["Cydonia:nothinking"])

    def test_both_boxes_checked_means_both_modes(self):
        job, _ = serve.job_from_form(
            self.base(think_on=["Fable"], think_off=["Fable"]))
        self.assertEqual(job["models"], ["Fable:both"])

    def test_multiple_models_each_keep_their_own_mode(self):
        job, _ = serve.job_from_form(self.base(
            think_on=["Qwen-heretic"], think_off=["Fable", "Cydonia"]))
        self.assertEqual(job["models"],
                         ["Cydonia:nothinking", "Fable:nothinking",
                          "Qwen-heretic:thinking"])

    def test_extra_patterns_count_as_models(self):
        job, error = serve.job_from_form(
            self.base(think_on=[], extra_models=" Qwen3.6 , Gemma4 "))
        self.assertIsNone(error)
        self.assertEqual(job["models"], ["Qwen3.6", "Gemma4"])

    def test_extra_patterns_do_not_duplicate_checkboxes(self):
        job, _ = serve.job_from_form(self.base(think_on=["Gemma4"],
                                               extra_models="Gemma4, Qwen"))
        self.assertEqual(job["models"], ["Gemma4:thinking", "Qwen"])

    def test_bad_numbers_are_rejected(self):
        for field, value, expect in (("temperature", "hot", "must be a number"),
                                     ("temperature", "9", "between 0 and 2"),
                                     ("seed", "abc", "whole number"),
                                     ("max_tokens", "lots", "whole number")):
            job, error = serve.job_from_form(self.base(**{field: value}))
            self.assertIsNone(job, field)
            self.assertIn(expect, error)

    def test_unchecked_summarize_is_false(self):
        job, _ = serve.job_from_form(self.base())
        self.assertFalse(job["summarize"])


class LiveServerTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-sessions-")
        self.spool = tempfile.mkdtemp(prefix="rt-spool-")
        spool.ensure(self.spool)
        handler = serve.make_handler(self.root, self.spool)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.spool, ignore_errors=True)

    def get(self, path, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if body is not None:
            headers = {"Content-Type": "application/x-www-form-urlencoded",
                       "Content-Length": str(len(body))}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read().decode("utf-8", "replace")
        result = (response.status, payload, dict(response.getheaders()))
        conn.close()
        return result

    def test_index_loads_empty(self):
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("No sessions yet", body)
        self.assertIn("New run", body)

    def test_index_carries_all_three_tabs_on_one_page(self):
        """Results, New run and Queue are panels of the index, not pages."""
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        for tab in ("results", "new", "queue"):
            self.assertIn('data-tab="%s"' % tab, body)
            self.assertIn('id="tab-%s"' % tab, body)
        # The form is really there, not a link to somewhere else.
        self.assertIn('action="/submit"', body)
        self.assertIn('id="user_prompt"', body)
        # Results is what you land on.
        self.assertIn('<div class="panel" id="tab-results"', body)
        self.assertIn('<div class="panel off" id="tab-new"', body)

    def test_tab_query_param_picks_the_open_panel(self):
        _, body, _ = self.get("/?tab=new")
        self.assertIn('<div class="panel" id="tab-new"', body)
        self.assertIn('<div class="panel off" id="tab-results"', body)
        self.assertIn('class="tab on" role="tab" data-tab="new"', body)

    def test_unknown_tab_falls_back_to_results(self):
        _, body, _ = self.get("/?tab=nonsense")
        self.assertIn('<div class="panel" id="tab-results"', body)

    def test_the_form_has_no_page_of_its_own(self):
        """It is a tab now. Nothing in the app links to /new any more."""
        self.assertEqual(self.get("/new")[0], 404)

    def test_a_rejected_submission_comes_back_on_the_new_tab(self):
        status, body, _ = self.get("/submit", "POST", "user_prompt=")
        self.assertEqual(status, 400)
        self.assertIn('<div class="panel" id="tab-new"', body)
        self.assertIn('class="err"', body)

    def test_index_and_new_page_reference_the_logo_and_favicon(self):
        for path in ("/", "/?tab=new"):
            status, body, _ = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn("/assets/logo.png", body)
            self.assertIn("/assets/favicon-32.png", body)

    def test_asset_is_served_with_a_long_cache_lifetime(self):
        status, body, headers = self.get("/assets/favicon-32.png")
        self.assertEqual(status, 200)
        self.assertTrue(len(body) > 0)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertIn("max-age", headers["Cache-Control"])
        self.assertNotEqual(headers["Cache-Control"], "no-store")

    def test_unknown_asset_404s(self):
        self.assertEqual(self.get("/assets/does-not-exist.png")[0], 404)

    def test_asset_traversal_is_refused(self):
        status, _, _ = self.get("/assets/../../../etc/passwd")
        self.assertIn(status, (403, 404))

    def test_form_lists_presets(self):
        status, body, _ = self.get("/?tab=new")
        self.assertEqual(status, 200)
        self.assertIn("Line &amp; Copy Editor", body)
        self.assertIn("Red Team", body)
        self.assertIn('value="structured-extractor"', body)
        # The dropdown must actually carry the prompt text to fill in.
        self.assertIn("meticulous line editor", body)

    def test_health(self):
        status, body, _ = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_unknown_page_404s(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_traversal_over_http_is_refused(self):
        status, _, _ = self.get("/s/../../etc/passwd")
        self.assertIn(status, (403, 404))

    def test_submit_queues_and_redirects(self):
        body = ("user_prompt=write+a+paragraph&think_on=alpha&mode=thinking"
                "&temperature=1.0&summarize=1")
        status, _, headers = self.get("/submit", "POST", body)
        self.assertEqual(status, 303)                 # POST -> GET, not a repeat
        location = headers["Location"]
        self.assertTrue(location.startswith("/job/"))
        self.assertEqual(spool.counts(self.spool)["queue"], 1)

        # The waiting page says it is queued and refreshes itself.
        status, page, _ = self.get(location)
        self.assertEqual(status, 200)
        self.assertIn("Queued", page)
        self.assertIn("http-equiv=\"refresh\"", page)

    def test_invalid_submit_returns_the_form_with_values(self):
        body = "user_prompt=&think_on=alpha&mode=thinking&temperature=1.0"
        status, page, _ = self.get("/submit", "POST", body)
        self.assertEqual(status, 400)
        self.assertIn("prompt is required", page)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_job_page_redirects_once_a_report_exists(self):
        session = os.path.join(self.root, "20260101-000000")
        os.makedirs(session)
        with open(os.path.join(session, "report.html"), "w") as f:
            f.write("<html>the report</html>")
        spool.write_atomic(
            os.path.join(self.spool, "done", "jobx.json"),
            json.dumps({"id": "jobx", "state": "done", "session_dir": session}))
        status, _, headers = self.get("/job/jobx")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/s/20260101-000000/report.html")

        status, body, headers = self.get(headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn("the report", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def _session_dir(self, name, self_refreshing):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "01_x_model_thinking.md"), "w") as f:
            f.write(self.RUN)          # a readable run, so the report can be rebuilt
        with open(os.path.join(path, "report.html"), "w") as f:
            f.write('<meta http-equiv="refresh" content="10">'
                    if self_refreshing else "x")
        return path

    def test_index_lists_sessions_and_flags_live_ones(self):
        self._session_dir("20260101-000001", False)
        live = self._session_dir("20260101-000002", True)
        spool.heartbeat(self.spool, state="running", job="j", session=live)
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("20260101-000001", body)
        self.assertIn("20260101-000002", body)
        self.assertIn('<span class="pill live">running</span>', body)

    def test_a_session_whose_run_died_is_not_called_live(self):
        """The refresh tag outlives the run that wrote it; the worker decides."""
        self._session_dir("20260101-000003", True)
        spool.heartbeat(self.spool, state="idle")
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertNotIn('<span class="pill live">running</span>', body)
        self.assertIn(">stopped</span>", body)
        # ...and the index itself must stop reloading every 15 seconds.
        self.assertNotIn('http-equiv="refresh"', body)
        self.assertIn("reloadMs = 0", body)

    def test_a_live_index_reloads_by_timer_not_by_meta_refresh(self):
        """The New run tab holds a draft, so the reload has to be skippable."""
        live = self._session_dir("20260101-000009", True)
        spool.heartbeat(self.spool, state="running", job="j", session=live)
        _, body, _ = self.get("/")
        self.assertNotIn('http-equiv="refresh"', body)
        self.assertIn("reloadMs = 15000", body)
        self.assertIn("do not eat a draft", body)

    def test_markdown_is_served_to_read_not_to_download(self):
        """text/markdown makes browsers save the file; the report links these
        judging documents expecting them to open."""
        path = self._session_dir("20260101-000010", False)
        with open(os.path.join(path, "SUMMARIZE.md"), "w") as f:
            f.write("# what the judges read\n")
        status, body, headers = self.get("/s/20260101-000010/SUMMARIZE.md")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("what the judges read", body)

    # --- cancelling and cleaning up -----------------------------------------

    def _orphaned_claim(self, job_id="stuck", session_dir=None):
        """A claim with no live worker behind it: what a reboot leaves."""
        job = {"id": job_id, "user_prompt": "hi"}
        if session_dir:
            job["session_dir"] = session_dir
        spool.write_atomic(os.path.join(self.spool, "running", job_id + ".json"),
                           json.dumps(job))
        spool.heartbeat(self.spool, state="running", job=job_id, pid=999999)

    def test_a_stuck_job_shows_as_stopped_not_running(self):
        self._orphaned_claim()
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("stuck", body)
        self.assertIn(">stopped</span>", body)
        self.assertIn('action="/cancel"', body)

        status, page, _ = self.get("/job/stuck")
        self.assertEqual(status, 200)
        self.assertIn("Stopped", page)
        self.assertIn("Clear this job", page)
        self.assertNotIn('http-equiv="refresh"', page)

    def test_clearing_a_stuck_job_files_it_as_failed(self):
        session = self._session_dir("20260101-000004", True)
        self._orphaned_claim(session_dir=session)
        status, _, headers = self.get("/cancel", "POST", "job=stuck")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/?notice="))
        self.assertEqual(spool.counts(self.spool)["running"], 0)
        self.assertEqual(spool.counts(self.spool)["failed"], 1)
        # Its report was rewritten, so it no longer reloads itself.
        with open(os.path.join(session, "report.html")) as f:
            self.assertNotIn('http-equiv="refresh"', f.read())

    def test_cancelling_a_queued_job_dequeues_it(self):
        spool.submit({"id": "waiting", "user_prompt": "hi"}, self.spool)
        status, _, _ = self.get("/cancel", "POST", "job=waiting")
        self.assertEqual(status, 303)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)
        with open(os.path.join(self.spool, "failed", "waiting.json")) as f:
            self.assertTrue(json.load(f)["cancelled"])

    def test_cancelling_a_live_run_asks_the_worker(self):
        """Only the worker can kill the runner, so this records a request."""
        spool.write_atomic(os.path.join(self.spool, "running", "live.json"),
                           json.dumps({"id": "live"}))
        spool.heartbeat(self.spool, state="running", job="live")  # our own pid
        status, _, _ = self.get("/cancel", "POST", "job=live")
        self.assertEqual(status, 303)
        self.assertTrue(spool.cancel_requested("live", self.spool))
        self.assertEqual(spool.counts(self.spool)["running"], 1)

    def test_cancelling_a_finished_job_is_refused(self):
        spool.write_atomic(os.path.join(self.spool, "done", "old.json"),
                           json.dumps({"id": "old"}))
        self.assertEqual(self.get("/cancel", "POST", "job=old")[0], 409)
        self.assertEqual(self.get("/cancel", "POST", "job=ghost")[0], 409)

    # --- removing a session ---------------------------------------------

    def test_remove_moves_the_session_to_trash(self):
        """One click, and moved rather than deleted: the trash is the undo."""
        self.session("20260101-000009")
        status, _, headers = self.get("/delete", "POST",
                                      "session=20260101-000009")
        self.assertEqual(status, 303)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "20260101-000009")))
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, ".trash", "20260101-000009")))

    def test_a_removed_session_leaves_the_listing(self):
        self.session("20260101-000010")
        self.get("/delete", "POST", "session=20260101-000010")
        status, body, _ = self.get("/")
        self.assertNotIn("20260101-000010", body)
        # The bin is a directory of complete sessions; it must not list as one.
        rows = [line for line in body.splitlines() if '<div class="row">' in line]
        self.assertFalse([r for r in rows if ".trash" in r], rows)

    def test_a_session_being_written_right_now_is_refused(self):
        path = self.session("20260101-000011")
        spool.heartbeat(self.spool, state="running", job="j", session=path)
        status, _, _ = self.get("/delete", "POST", "session=20260101-000011")
        self.assertEqual(status, 409)
        self.assertTrue(os.path.isdir(path))

    def test_remove_cannot_escape_the_sessions_root(self):
        for attempt in ("../../etc", "/etc", ".ssh", "..%2f..%2fetc"):
            status, _, _ = self.get("/delete", "POST",
                                    "session=%s" % urllib.parse.quote(attempt))
            self.assertIn(status, (404, 409), attempt)

    def test_removing_the_same_name_twice_keeps_both(self):
        self.session("20260101-000012")
        self.get("/delete", "POST", "session=20260101-000012")
        self.session("20260101-000012")
        self.get("/delete", "POST", "session=20260101-000012")
        binned = os.listdir(os.path.join(self.root, ".trash"))
        self.assertEqual(len([b for b in binned if b.startswith("20260101-000012")]), 2)

    def test_rerun_controls_carry_the_primary_style(self):
        self.session("20260101-000013")
        _, body, _ = self.get("/")
        self.assertIn('<a class="go" href="/?tab=new&amp;from=', body)
        self.assertIn('<button type="submit" class="del"', body)

    def test_remove_is_a_post_not_a_link(self):
        """A prefetcher following links must not be able to bin a session."""
        self.session("20260101-000014")
        _, body, _ = self.get("/")
        self.assertNotIn('href="/delete', body)
        self.assertIn('action="/delete"', body)
        self.assertEqual(self.get("/delete/20260101-000014")[0], 404)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "20260101-000014")))

    # --- what a failed job says ------------------------------------------

    def _failed(self, job_id, error, log_text=None, **extra):
        record = {"id": job_id, "state": "failed", "error": error}
        record.update(extra)
        spool.write_atomic(os.path.join(self.spool, "failed", job_id + ".json"),
                           json.dumps(record))
        if log_text is not None:
            spool.write_atomic(os.path.join(self.spool, "logs", job_id + ".log"),
                               log_text)

    def test_failure_page_names_the_cause(self):
        """Not 'see the job log' — the app has the log, it can read it."""
        self._failed("boom", "runner exited 1",
                     "loading model...\nTraceback (most recent call last):\n"
                     "  File \"<string>\", line 3\n"
                     "ImportError: cannot import name 'model_cards'\n")
        status, page, _ = self.get("/job/boom")
        self.assertEqual(status, 200)
        self.assertIn("ImportError: cannot import name", page)
        self.assertNotIn("See the job log under the spool directory", page)

    def test_failure_page_falls_back_to_the_tail(self):
        self._failed("quiet", "runner exited 2", "step one\nstep two\nstep three\n")
        _, page, _ = self.get("/job/quiet")
        self.assertIn("step three", page)

    def test_failure_page_copes_with_no_log(self):
        self._failed("nolog", "runner exited 127")
        _, page, _ = self.get("/job/nolog")
        self.assertIn("No log was written", page)

    def test_a_cancelled_job_does_not_read_as_a_crash(self):
        self._failed("stopped", "cancelled", "", cancelled=True)
        _, page, _ = self.get("/job/stopped")
        self.assertIn("Cancelled", page)
        self.assertNotIn("Last lines", page)

    # --- the trash --------------------------------------------------------

    def test_trash_count_appears_only_when_something_is_in_it(self):
        _, body, _ = self.get("/")
        self.assertNotIn("Empty trash", body)
        self.session("20260101-000020")
        self.get("/delete", "POST", "session=20260101-000020")
        _, body, _ = self.get("/")
        self.assertIn("Empty trash (1)", body)
        self.session("20260101-000021")
        self.get("/delete", "POST", "session=20260101-000021")
        _, body, _ = self.get("/")
        self.assertIn("Empty trash (2)", body)

    def test_trash_page_lists_what_would_go(self):
        self.session("20260101-000022")
        self.get("/delete", "POST", "session=20260101-000022")
        status, page, _ = self.get("/trash")
        self.assertEqual(status, 200)
        self.assertIn("20260101-000022", page)
        self.assertIn("Empty trash (1)", page)
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, ".trash", "20260101-000022")),
            "viewing the trash must not empty it")

    def test_emptying_deletes_for_good(self):
        self.session("20260101-000023")
        self.get("/delete", "POST", "session=20260101-000023")
        status, _, headers = self.get("/trash/empty", "POST", "")
        self.assertEqual(status, 303)
        self.assertIn("1", urllib.parse.unquote(headers["Location"]))
        self.assertFalse(os.path.exists(
            os.path.join(self.root, ".trash", "20260101-000023")))
        _, body, _ = self.get("/")
        self.assertNotIn("Empty trash", body)

    def test_emptying_an_empty_trash_is_harmless(self):
        status, _, headers = self.get("/trash/empty", "POST", "")
        self.assertEqual(status, 303)
        self.assertIn("already empty", urllib.parse.unquote(headers["Location"]))

    def test_emptying_never_touches_anything_outside_the_trash(self):
        keep = self.session("20260101-000024")
        os.makedirs(os.path.join(self.root, ".trash"), exist_ok=True)
        # A symlink out of the bin must not take the real session with it.
        os.symlink(keep, os.path.join(self.root, ".trash", "escape"))
        self.get("/trash/empty", "POST", "")
        self.assertTrue(os.path.isdir(keep), "a session outside the trash survived")
        self.assertTrue(os.path.exists(os.path.join(keep, "01_x_alpha_thinking.md")))

    def test_cleanup_clears_every_stuck_job(self):
        self._orphaned_claim("stuck-a")
        self._orphaned_claim("stuck-b")
        status, _, headers = self.get("/cleanup", "POST", "")
        self.assertEqual(status, 303)
        self.assertIn("2", urllib.parse.unquote(headers["Location"]))
        self.assertEqual(spool.counts(self.spool)["running"], 0)

    # --- rerun buttons ------------------------------------------------------

    RUN = ('---\nmodel: "alpha-7B"\nthinking: true\ntemperature: 1.0\nseed: 42\n'
           'tokens: 10\ntokens_per_sec: 5.0\nelapsed_sec: 2\nerror: null\n---\n\n'
           "## Output\n\nhello\n")

    def session(self, name="20260101-000001", record=None, prompts=True):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "01_x_alpha_thinking.md"), "w") as f:
            f.write(self.RUN)
        if prompts:
            with open(os.path.join(path, "system-prompt.txt"), "w") as f:
                f.write("be brief")
            with open(os.path.join(path, "user-prompt.txt"), "w") as f:
                f.write("write a haiku")
        if record is not None:
            record = dict(record, session_dir=path)
            spool.write_atomic(os.path.join(self.spool, "done", "j1.json"),
                               json.dumps(record))
        return path

    def test_index_offers_both_rerun_buttons(self):
        self.session()
        _, body, _ = self.get("/")
        self.assertIn('action="/rerun"', body)
        self.assertIn(">Rerun<", body)
        self.assertIn("/?tab=new&amp;from=20260101-000001", body)

    # --- re-judging ---------------------------------------------------------

    def test_index_offers_a_rejudge_link(self):
        self.session("20260101-000031")
        _, body, _ = self.get("/")
        self.assertIn("/rejudge?session=20260101-000031", body)
        self.assertIn(">Re-judge</a>", body)

    def test_rejudge_page_names_the_session_and_posts_back(self):
        self.session("20260101-000032")
        status, page, _ = self.get("/rejudge?session=20260101-000032")
        self.assertEqual(status, 200)
        self.assertIn("20260101-000032", page)
        self.assertIn('action="/rejudge"', page)
        self.assertIn('name="judges"', page)
        self.assertIn('name="meta_summary"', page)

    def test_rejudge_page_for_an_unknown_session_404s(self):
        self.assertEqual(self.get("/rejudge?session=nope")[0], 404)

    def test_rejudge_queues_a_judge_only_job(self):
        path = self.session("20260101-000033")
        status, _, headers = self.get(
            "/rejudge", "POST",
            "session=20260101-000033&judges=outsider-13B&judges=other-8B"
            "&meta_summary=1")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/job/"))
        queued = spool.pending(self.spool)
        self.assertEqual(len(queued), 1)
        with open(os.path.join(self.spool, "queue", queued[0])) as f:
            job = json.load(f)
        self.assertEqual(job["judge_only"], path)
        self.assertEqual(job["judges"], ["outsider-13B", "other-8B"])
        self.assertTrue(job["meta_summary"])
        self.assertEqual(job["rejudge_of"], "20260101-000033")
        # No prompts and no models: nothing is generated a second time.
        self.assertNotIn("user_prompt", job)
        self.assertNotIn("models", job)

    def test_rejudge_with_no_judges_picked_is_refused(self):
        self.session("20260101-000034")
        status, page, _ = self.get("/rejudge", "POST",
                                   "session=20260101-000034&meta_summary=1")
        self.assertEqual(status, 400)
        self.assertIn("at least one judge", page)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_rejudge_cannot_escape_the_sessions_root(self):
        for attempt in ("../../etc", "/etc", "..%2f..%2fetc"):
            status, _, _ = self.get(
                "/rejudge", "POST",
                "session=%s&judges=x" % urllib.parse.quote(attempt))
            self.assertEqual(status, 404, attempt)

    def test_rerun_queues_the_recorded_job(self):
        """The job record is the better source: it holds what was asked for,
        including settings the session dir cannot show."""
        self.session(record={"id": "j1", "state": "done", "user_prompt": "the ask",
                             "system_prompt": "be brief", "models": ["alpha:thinking"],
                             "mode": "thinking", "summarize": True, "blind": True,
                             "meta_summary": False, "meta_summary_requested": True,
                             "env": {"MAX_TOKENS": "4096"},
                             "exit_code": 0, "elapsed_sec": 12.3})
        status, _, headers = self.get("/rerun", "POST",
                                      "session=20260101-000001")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/job/"))
        queued = spool.pending(self.spool)
        self.assertEqual(len(queued), 1)
        with open(os.path.join(self.spool, "queue", queued[0])) as f:
            job = json.load(f)
        self.assertEqual(job["user_prompt"], "the ask")
        self.assertEqual(job["models"], ["alpha:thinking"])
        self.assertEqual(job["env"], {"MAX_TOKENS": "4096"})
        # A Round 3 that was wanted but skipped is still wanted on a rerun.
        self.assertTrue(job["meta_summary"])
        # None of the finished run's bookkeeping comes along.
        for key in ("exit_code", "elapsed_sec", "state", "session_dir", "finished"):
            self.assertNotIn(key, job)
        self.assertNotEqual(job["id"], "j1")          # a new job, not the old one

    def test_rerun_falls_back_to_reading_the_session(self):
        """A session started from the command line has no job record anywhere."""
        self.session()
        status, _, _ = self.get("/rerun", "POST", "session=20260101-000001")
        self.assertEqual(status, 303)
        with open(os.path.join(self.spool, "queue",
                               spool.pending(self.spool)[0])) as f:
            job = json.load(f)
        self.assertEqual(job["user_prompt"], "write a haiku")
        self.assertEqual(job["system_prompt"], "be brief")
        self.assertEqual(job["models"], ["alpha-7B:thinking"])
        self.assertEqual(job["seed"], 42)
        self.assertFalse(job["summarize"])            # this session had no judges

    def test_rerun_of_an_unknown_session_404s(self):
        status, _, _ = self.get("/rerun", "POST", "session=nope")
        self.assertEqual(status, 404)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_rerun_cannot_escape_the_sessions_root(self):
        status, _, _ = self.get("/rerun", "POST", "session=../../etc")
        self.assertEqual(status, 404)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_rerun_without_a_prompt_is_refused(self):
        self.session(prompts=False)
        status, page, _ = self.get("/rerun", "POST", "session=20260101-000001")
        self.assertEqual(status, 409)
        self.assertIn("no prompt", page)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_prompts_only_prefills_the_form_and_queues_nothing(self):
        self.session(record={"id": "j1", "state": "done",
                             "user_prompt": "write a haiku",
                             "system_prompt": "be brief",
                             "models": ["alpha:thinking"], "summarize": True})
        status, page, _ = self.get("/?tab=new&from=20260101-000001")
        self.assertEqual(status, 200)
        self.assertIn("write a haiku", page)
        self.assertIn("be brief", page)
        # The models it ran with must NOT be preselected -- picking them again
        # is the whole reason for this button.
        self.assertNotIn('value="alpha" checked', page)
        self.assertIn("Models and settings are back at their defaults", page)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_prompts_only_from_an_unknown_session_404s(self):
        status, _, _ = self.get("/?tab=new&from=nope")
        self.assertEqual(status, 404)


class ChainTabTests(unittest.TestCase):
    """The Multi-Prompt tab: a fourth panel, and the /chain/submit route."""

    setUp = LiveServerTests.setUp
    tearDown = LiveServerTests.tearDown
    get = LiveServerTests.get

    def test_index_carries_the_multi_prompt_tab(self):
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('data-tab="chain"', body)
        self.assertIn('id="tab-chain"', body)
        self.assertIn('action="/chain/submit"', body)
        # Fields, not a JSON blob: one prompt box per stage, one roster for
        # the whole chain (not per stage).
        self.assertIn('name="stage_0_system_prompt"', body)
        self.assertIn('name="stage_0_user_prompt"', body)
        self.assertIn('name="chain_think_on"', body)
        self.assertNotIn('name="stage_0_think_on"', body)
        self.assertNotIn('name="stage_0_use_previous"', body)
        self.assertIn('id="add_stage"', body)
        # Loads with the five-stage workflow already filled in, not blank.
        self.assertIn("Fichtean Curve", body)

    def _stage_fields(self, i, **over):
        fields = {"stage_%d_name" % i: "one", "stage_%d_system_prompt" % i: "sys",
                 "stage_%d_user_prompt" % i: "go"}
        for k, v in over.items():
            fields["stage_%d_%s" % (i, k)] = v
        return fields

    def _chain_fields(self, **over):
        fields = dict(self._stage_fields(0), chain_think_on="stub-a-7B")
        fields.update(over)
        return fields

    def test_a_stage_with_no_prompt_is_rejected_with_the_form_kept(self):
        body_fields = self._chain_fields()
        body_fields["stage_0_user_prompt"] = ""
        status, body, _ = self.get(
            "/chain/submit", method="POST",
            body=urllib.parse.urlencode(body_fields, doseq=True))
        self.assertEqual(status, 400)
        self.assertIn("needs a prompt", body)
        self.assertIn(">sys</textarea>", body)         # draft is kept, not lost

    def test_no_models_is_rejected(self):
        status, body, _ = self.get(
            "/chain/submit", method="POST",
            body=urllib.parse.urlencode(self._stage_fields(0), doseq=True))
        self.assertEqual(status, 400)
        self.assertIn("Pick at least one model", body)

    def test_a_valid_form_is_queued_and_redirects_to_the_job_page(self):
        body_fields = dict(self._chain_fields(), chain_name="my-story")
        status, _, headers = self.get(
            "/chain/submit", method="POST",
            body=urllib.parse.urlencode(body_fields, doseq=True))
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/job/"))
        self.assertEqual(spool.counts(self.spool)["queue"], 1)

    def test_pasted_manuscript_text_is_carried_as_inline_text_not_a_path(self):
        body_fields = dict(self._chain_fields(), chain_name="my-story",
                          chain_manuscript="Once upon a time.")
        status, _, headers = self.get(
            "/chain/submit", method="POST",
            body=urllib.parse.urlencode(body_fields, doseq=True))
        self.assertEqual(status, 303)
        _, job = spool.jobs("queue", self.spool)[0]
        self.assertEqual(job["chain_spec"]["manuscript_text"], "Once upon a time.")
        self.assertNotIn("manuscript", job["chain_spec"])

    def test_one_roster_lands_on_the_first_stage_and_later_stages_go_own(self):
        fields = {}
        fields.update(self._stage_fields(0, name="first"))
        fields.update(self._stage_fields(3, name="second"))
        fields["chain_name"] = "multi"
        fields["chain_think_on"] = "stub-a-7B"
        status, _, headers = self.get(
            "/chain/submit", method="POST",
            body=urllib.parse.urlencode(fields, doseq=True))
        self.assertEqual(status, 303)
        _, job = spool.jobs("queue", self.spool)[0]
        stages = job["chain_spec"]["stages"]
        self.assertEqual([s["name"] for s in stages], ["first", "second"])
        self.assertEqual(stages[0]["models"], ["stub-a-7B:thinking"])
        self.assertNotIn("use_previous", stages[0])
        self.assertEqual(stages[1]["use_previous"], "own")
        self.assertNotIn("models", stages[1])


class PresetRouteTests(unittest.TestCase):
    """The preset save/delete/reset routes, over real HTTP.

    Each test gets its own ROUNDTABLE_PRESETS file so these can never touch
    whatever presets a developer has actually saved on this machine.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-sessions-")
        self.spool = tempfile.mkdtemp(prefix="rt-spool-")
        self.preset_dir = tempfile.mkdtemp(prefix="rt-userpresets-")
        self.preset_path = os.path.join(self.preset_dir, "presets.json")
        spool.ensure(self.spool)
        self._old_env = os.environ.get("ROUNDTABLE_PRESETS")
        os.environ["ROUNDTABLE_PRESETS"] = self.preset_path
        handler = serve.make_handler(self.root, self.spool)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._old_env is None:
            os.environ.pop("ROUNDTABLE_PRESETS", None)
        else:
            os.environ["ROUNDTABLE_PRESETS"] = self._old_env
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.spool, ignore_errors=True)
        shutil.rmtree(self.preset_dir, ignore_errors=True)

    def get(self, path, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if body is not None:
            headers = {"Content-Type": "application/x-www-form-urlencoded",
                       "Content-Length": str(len(body))}
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        payload = r.read()
        result = (r.status, payload, dict(r.getheaders()))
        conn.close()
        return result

    def test_save_then_it_appears_in_the_form(self):
        status, body, _ = self.get(
            "/presets/save", "POST",
            "title=My+Custom+Role&system_prompt=be+concise")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["id"], "my-custom-role")

        status, page, _ = self.get("/?tab=new")
        self.assertIn(b"My Custom Role", page)

    def test_save_missing_prompt_is_rejected(self):
        status, body, _ = self.get("/presets/save", "POST", "title=Only+A+Title")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_new_page_with_preset_query_prefills_system_prompt(self):
        self.get("/presets/save", "POST",
                "title=My+Custom+Role&system_prompt=be+concise+please")
        status, page, _ = self.get("/?tab=new&preset=my-custom-role")
        self.assertEqual(status, 200)
        self.assertIn(b"be concise please", page)
        self.assertIn(b'value="my-custom-role" selected', page)

    def test_notice_query_param_is_shown(self):
        status, page, _ = self.get("/?tab=new&notice=Saved+it")
        self.assertIn(b"Saved it", page)
        self.assertIn(b'class="notice"', page)

    def test_delete_removes_it(self):
        self.get("/presets/save", "POST", "title=Temp&system_prompt=x")
        status, body, _ = self.get("/presets/delete", "POST", "id=temp")
        data = json.loads(body)
        self.assertTrue(data["deleted"])
        self.assertFalse(data["reverted"])            # not a bundled id

        status, page, _ = self.get("/?tab=new")
        self.assertNotIn(b">Temp<", page)

    def test_delete_missing_id_is_rejected(self):
        status, body, _ = self.get("/presets/delete", "POST", "")
        self.assertEqual(status, 400)

    def test_reset_clears_saved_presets(self):
        self.get("/presets/save", "POST", "title=Temp&system_prompt=x")
        status, body, _ = self.get("/presets/reset", "POST", "")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertFalse(os.path.exists(self.preset_path))


if __name__ == "__main__":
    unittest.main()

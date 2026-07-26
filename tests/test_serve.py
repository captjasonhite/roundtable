"""The HTTP layer, driven over a real socket on an ephemeral port."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
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

    def test_index_and_new_page_reference_the_logo_and_favicon(self):
        for path in ("/", "/new"):
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
        status, body, _ = self.get("/new")
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

    def test_index_lists_sessions_and_flags_live_ones(self):
        for name, running in (("20260101-000001", False), ("20260101-000002", True)):
            path = os.path.join(self.root, name)
            os.makedirs(path)
            open(os.path.join(path, "01_x_model_thinking.md"), "w").close()
            with open(os.path.join(path, "report.html"), "w") as f:
                f.write('<meta http-equiv="refresh" content="10">' if running else "x")
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("20260101-000001", body)
        self.assertIn("20260101-000002", body)
        self.assertIn("running", body)                # the live pill

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
        self.assertIn("/new?from=20260101-000001", body)

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
        status, page, _ = self.get("/new?from=20260101-000001")
        self.assertEqual(status, 200)
        self.assertIn("write a haiku", page)
        self.assertIn("be brief", page)
        # The models it ran with must NOT be preselected -- picking them again
        # is the whole reason for this button.
        self.assertNotIn('value="alpha" checked', page)
        self.assertIn("Models and settings are back at their defaults", page)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_prompts_only_from_an_unknown_session_404s(self):
        status, _, _ = self.get("/new?from=nope")
        self.assertEqual(status, 404)


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

        status, page, _ = self.get("/new")
        self.assertIn(b"My Custom Role", page)

    def test_save_missing_prompt_is_rejected(self):
        status, body, _ = self.get("/presets/save", "POST", "title=Only+A+Title")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_new_page_with_preset_query_prefills_system_prompt(self):
        self.get("/presets/save", "POST",
                "title=My+Custom+Role&system_prompt=be+concise+please")
        status, page, _ = self.get("/new?preset=my-custom-role")
        self.assertEqual(status, 200)
        self.assertIn(b"be concise please", page)
        self.assertIn(b'value="my-custom-role" selected', page)

    def test_notice_query_param_is_shown(self):
        status, page, _ = self.get("/new?notice=Saved+it")
        self.assertIn(b"Saved it", page)
        self.assertIn(b'class="notice"', page)

    def test_delete_removes_it(self):
        self.get("/presets/save", "POST", "title=Temp&system_prompt=x")
        status, body, _ = self.get("/presets/delete", "POST", "id=temp")
        data = json.loads(body)
        self.assertTrue(data["deleted"])
        self.assertFalse(data["reverted"])            # not a bundled id

        status, page, _ = self.get("/new")
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

"""The manual rebuild route -- the fix for 'a session's report is stale after
a Roundtable upgrade, and the always-running worker has no reason to touch it
again since it isn't the job owner anymore.'"""
import http.client
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import serve, spool

RUN = """---
model: "alpha-7B"
thinking: true
temperature: 1.0
seed: 42
tokens: 500
tokens_per_sec: 40.0
elapsed_sec: 12
error: null
---

## Output

Some text.
"""


class RebuildFunctionTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-root-")
        self.session = os.path.join(self.root, "20260101-000000")
        os.makedirs(self.session)
        with open(os.path.join(self.session, "01_x_alpha_thinking.md"), "w") as f:
            f.write(RUN)
        with open(os.path.join(self.session, "report.html"), "w") as f:
            f.write("<html>stale, from a previous Roundtable version</html>")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_rebuild_overwrites_the_stale_report(self):
        self.assertTrue(serve.rebuild_session(self.root, "20260101-000000"))
        with open(os.path.join(self.session, "report.html")) as f:
            html = f.read()
        self.assertNotIn("stale", html)
        self.assertIn("Judged outputs", html)

    def test_rebuild_missing_session_returns_false(self):
        self.assertFalse(serve.rebuild_session(self.root, "no-such-session"))

    def test_rebuild_refuses_to_escape_the_root(self):
        self.assertFalse(serve.rebuild_session(self.root, "../../etc"))


class RebuildRouteTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-root-")
        self.spool = tempfile.mkdtemp(prefix="rt-spool-")
        spool.ensure(self.spool)
        self.session = os.path.join(self.root, "20260101-000000")
        os.makedirs(self.session)
        with open(os.path.join(self.session, "01_x_alpha_thinking.md"), "w") as f:
            f.write(RUN)
        handler = serve.make_handler(self.root, self.spool)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.spool, ignore_errors=True)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        headers = dict(r.getheaders())
        conn.close()
        return r.status, body, headers

    def test_rebuild_redirects_to_the_report(self):
        status, _, headers = self.get("/rebuild/20260101-000000")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/s/20260101-000000/report.html")

    def test_report_page_carries_a_rebuild_link(self):
        self.get("/rebuild/20260101-000000")
        status, body, _ = self.get("/s/20260101-000000/report.html")
        self.assertEqual(status, 200)
        self.assertIn(b'/rebuild/20260101-000000', body)

    def test_rebuild_unknown_session_404s(self):
        self.assertEqual(self.get("/rebuild/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()

"""The judge's ranking line, and what happens when it doesn't write one.

The helper under test is the python heredoc inside creative-bench.sh, so this
extracts it and runs it against a stub server rather than a GPU. That is the
real code path: the same script the worker shells out to, on the same wire
format llama-server speaks.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "bin", "creative-bench.sh")
LABELS = "A,B,C"


def helper_source():
    """The .call.py the bench script writes, pulled out of its heredoc."""
    with open(SCRIPT, encoding="utf-8") as f:
        text = f.read()
    body = re.search(r"cat > \"\$HELPER\" <<'PYEOF'\n(.*?)\nPYEOF\n", text, re.S)
    assert body, "the helper heredoc moved -- this test needs updating"
    return body.group(1)


class StubModel(BaseHTTPRequestHandler):
    """Answers /v1/chat/completions with whatever the test queued up."""

    replies = []          # class-level: consumed in order, one per request
    seen = []             # the message lists each request carried

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or "{}")
        StubModel.seen.append(payload["messages"])
        reply = (StubModel.replies.pop(0) if StubModel.replies
                 else {"content": "(nothing left to say)"})
        body = json.dumps({
            "choices": [{"message": {
                "content": reply.get("content", ""),
                "reasoning_content": reply.get("reasoning", ""),
            }}],
            "usage": {"completion_tokens": reply.get("tokens", 10)},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RankingRetryTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-retry-")
        self.helper = os.path.join(self.dir, "call.py")
        with open(self.helper, "w", encoding="utf-8") as f:
            f.write(helper_source())
        for name, text in (("sys.txt", ""), ("user.txt", "judge these")):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        StubModel.replies, StubModel.seen = [], []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubModel)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_helper(self, require=LABELS):
        out = os.path.join(self.dir, "verdict.md")
        env = dict(os.environ)
        env.update({
            "OUT_FILE": out, "MODEL_NAME": "stub", "MODEL_PATH": "/stub.gguf",
            "MODE": "nothinking", "SYS_FILE": os.path.join(self.dir, "sys.txt"),
            "USER_FILE": os.path.join(self.dir, "user.txt"),
            "PORT": str(self.port), "SEED": "1", "MAX_TOKENS": "512",
            "TEMP": "0.7", "TOP_P": "0.95", "TOP_K": "40", "MIN_P": "0.05",
            "PRESENCE_PENALTY": "0", "FREQUENCY_PENALTY": "0",
            "REPEAT_PENALTY": "1.0", "DRY_MULTIPLIER": "0", "DRY_BASE": "1.75",
            "DRY_ALLOWED_LENGTH": "2", "DRY_PENALTY_LAST_N": "-1",
            "REQUEST_TIMEOUT": "10", "ACTUAL_CTX": "4096",
            "REQUIRE_LABELS": require,
        })
        proc = subprocess.run([sys.executable, self.helper], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_a_judge_that_writes_the_line_is_left_alone(self):
        StubModel.replies = [{"content": "my verdict\n\nRANKING: {{B}} > {{A}} > {{C}}"}]
        text = self.run_helper()
        self.assertIn("RANKING: {{B}} > {{A}} > {{C}}", text)
        self.assertNotIn("ranking_retry", text)
        self.assertEqual(len(StubModel.seen), 1, "no second call should be made")

    def test_a_missing_line_is_asked_for_again(self):
        StubModel.replies = [
            {"content": "1. {{B}} best\n2. {{A}} next\n3. {{C}} last"},
            {"content": "RANKING: {{B}} > {{A}} > {{C}}"},
        ]
        text = self.run_helper()
        self.assertIn("RANKING: {{B}} > {{A}} > {{C}}", text)
        self.assertIn("ranking_retry: true", text)
        # The follow-up must carry the judging it already did, or it is being
        # asked to rank from nothing.
        second = StubModel.seen[1]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertIn("{{B}} best", second[-2]["content"])

    def test_a_short_line_is_replaced_not_appended(self):
        """Two RANKING lines and the reader takes the first -- the broken one."""
        StubModel.replies = [
            {"content": "verdict\n\nRANKING: {{B}} > {{A}}"},      # {{C}} missing
            {"content": "RANKING: {{B}} > {{A}} > {{C}}"},
        ]
        text = self.run_helper()
        self.assertEqual(text.count("RANKING:"), 1)
        self.assertIn("RANKING: {{B}} > {{A}} > {{C}}", text)

    def test_the_line_is_taken_from_reasoning_when_the_answer_is_prose(self):
        StubModel.replies = [
            {"content": "no line here"},
            {"content": "I would put B first.",
             "reasoning": "RANKING: {{B}} > {{A}} > {{C}}"},
        ]
        self.assertIn("RANKING: {{B}} > {{A}} > {{C}}", self.run_helper())

    def test_a_failed_retry_leaves_the_verdict_as_it_was(self):
        StubModel.replies = [
            {"content": "no line here"},
            {"content": "still no line"},
        ]
        text = self.run_helper()
        self.assertIn("no line here", text)
        self.assertNotIn("ranking_retry", text)

    def test_no_retry_when_no_labels_are_required(self):
        """Round 1 and Round 3 write prose; they are not asked for a ranking."""
        StubModel.replies = [{"content": "a story, not a verdict"}]
        text = self.run_helper(require="")
        self.assertIn("a story, not a verdict", text)
        self.assertEqual(len(StubModel.seen), 1)


if __name__ == "__main__":
    unittest.main()

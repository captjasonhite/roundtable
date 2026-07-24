"""serve() can try to open a browser without ever crashing the server."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import serve


class OpenBrowserTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-root-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_serve(self, open_browser, webbrowser_open):
        logs = []
        with mock.patch("webbrowser.open", webbrowser_open):
            thread = threading.Thread(
                target=serve.serve,
                kwargs=dict(root=self.root, port=0, log=logs.append,
                           open_browser=open_browser),
                daemon=True)
            thread.start()
            time.sleep(0.3)
        return logs

    def test_off_by_default_does_not_touch_webbrowser(self):
        opened = mock.Mock()
        self.run_serve(False, opened)
        opened.assert_not_called()

    def test_on_calls_webbrowser_open_with_the_serving_url(self):
        opened = mock.Mock(return_value=True)
        logs = self.run_serve(True, opened)
        opened.assert_called_once()
        url = opened.call_args[0][0]
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertTrue(any("serving" in line for line in logs))

    def test_failure_to_open_is_logged_not_raised(self):
        """No display, no default browser, whatever -- must not take the
        server down with it."""
        opened = mock.Mock(side_effect=RuntimeError("no display"))
        logs = self.run_serve(True, opened)
        self.assertTrue(any("could not open a browser" in line for line in logs))

    def test_false_return_is_logged_not_raised(self):
        """webbrowser.open() returning False (no browser found) is not an
        exception -- must still be reported, not silently ignored."""
        opened = mock.Mock(return_value=False)
        logs = self.run_serve(True, opened)
        self.assertTrue(any("could not open a browser" in line for line in logs))


if __name__ == "__main__":
    unittest.main()

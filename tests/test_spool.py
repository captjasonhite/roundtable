"""Queue mechanics: atomic hand-off, no lost jobs, no double claims."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import spool


class SpoolTests(unittest.TestCase):

    def setUp(self):
        self.spool = tempfile.mkdtemp(prefix="rt-spool-")
        spool.ensure(self.spool)

    def tearDown(self):
        shutil.rmtree(self.spool, ignore_errors=True)

    def test_submit_then_claim(self):
        spool.submit({"id": "job-1", "user_prompt": "hi"}, self.spool)
        self.assertEqual(spool.counts(self.spool)["queue"], 1)
        job, path = spool.claim(self.spool)
        self.assertEqual(job["id"], "job-1")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(spool.counts(self.spool)["queue"], 0)
        self.assertEqual(spool.counts(self.spool)["running"], 1)

    def test_claim_is_exclusive(self):
        spool.submit({"id": "only-one"}, self.spool)
        first = spool.claim(self.spool)
        second = spool.claim(self.spool)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_empty_queue_claims_nothing(self):
        self.assertIsNone(spool.claim(self.spool))

    def test_fifo_order(self):
        for i in range(3):
            spool.submit({"id": "job-%d" % i}, self.spool)
            os.utime(os.path.join(self.spool, "queue", "job-%d.json" % i),
                     (1000 + i, 1000 + i))
        ids = []
        while True:
            claimed = spool.claim(self.spool)
            if not claimed:
                break
            ids.append(claimed[0]["id"])
            spool.finish(claimed[1], "done", spool=self.spool)
        self.assertEqual(ids, ["job-0", "job-1", "job-2"])

    def test_partial_write_is_invisible(self):
        """A dot-file is a job still being written; a scan must skip it."""
        with open(os.path.join(self.spool, "queue", ".half.json"), "w") as f:
            f.write('{"id": "half"')
        self.assertEqual(spool.pending(self.spool), [])
        self.assertIsNone(spool.claim(self.spool))

    def test_finish_records_outcome(self):
        spool.submit({"id": "job-x"}, self.spool)
        job, path = spool.claim(self.spool)
        spool.finish(path, "done", {"session_dir": "/tmp/s", "exit_code": 0},
                     spool=self.spool)
        self.assertFalse(os.path.exists(path))
        with open(os.path.join(self.spool, "done", "job-x.json")) as f:
            record = json.load(f)
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["session_dir"], "/tmp/s")
        self.assertIn("finished", record)

    def test_unreadable_job_goes_to_failed(self):
        """Corrupt JSON must not wedge the queue forever."""
        with open(os.path.join(self.spool, "queue", "bad.json"), "w") as f:
            f.write("not json")
        self.assertIsNone(spool.claim(self.spool))
        self.assertEqual(spool.counts(self.spool)["failed"], 1)
        self.assertEqual(spool.counts(self.spool)["queue"], 0)

    def test_requeue(self):
        spool.submit({"id": "job-r"}, self.spool)
        _, path = spool.claim(self.spool)
        spool.requeue(path, self.spool)
        self.assertEqual(spool.counts(self.spool)["queue"], 1)
        self.assertEqual(spool.counts(self.spool)["running"], 0)

    def test_heartbeat_roundtrip(self):
        spool.heartbeat(self.spool, state="running", job="job-9")
        beat = spool.read_heartbeat(self.spool)
        self.assertEqual(beat["state"], "running")
        self.assertEqual(beat["job"], "job-9")
        self.assertEqual(beat["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()

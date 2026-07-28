"""Model discovery: one entry per model, even a split GGUF."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import models


class DiscoverTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-models-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def touch(self, name, size=1024):
        with open(os.path.join(self.root, name), "wb") as f:
            f.write(b"0" * size)

    def test_a_four_part_split_collapses_to_one_model(self):
        for i in range(1, 5):
            self.touch("big-%05d-of-00004.gguf" % i)
        found = models.discover(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "big")

    def test_a_six_part_split_also_collapses_to_one_model(self):
        """A fixed list of "-00001-of-" .. "-00004-of-" markers left any split
        with 5+ parts unmatched, so each shard listed as its own model."""
        for i in range(1, 7):
            self.touch("huge-%05d-of-00006.gguf" % i)
        found = models.discover(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "huge")

    def test_unrelated_models_stay_separate(self):
        self.touch("alpha.gguf")
        self.touch("beta.gguf")
        found = models.discover(self.root)
        self.assertEqual(sorted(m["name"] for m in found), ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()

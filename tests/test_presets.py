"""Presets load from the repo, and users can add their own without editing it."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import presets


class BundledTests(unittest.TestCase):

    def setUp(self):
        self.presets = presets.load(user="/nonexistent")

    def test_all_ten_load(self):
        self.assertEqual(len(self.presets), 10)

    def test_ids_are_unique_and_titles_present(self):
        ids = [p["id"] for p in self.presets]
        self.assertEqual(len(ids), len(set(ids)))
        for p in self.presets:
            self.assertTrue(p["title"])
            self.assertTrue(p["system_prompt"])
            self.assertTrue(p.get("expects"), "each preset hints at its input")

    def test_find_by_id(self):
        p = presets.find("red-team", self.presets)
        self.assertIsNotNone(p)
        self.assertEqual(p["title"], "Red Team / Devil's Advocate")
        self.assertIn("red team analyst", p["system_prompt"])

    def test_find_missing_returns_none(self):
        self.assertIsNone(presets.find("no-such-preset", self.presets))


class UserFileTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-presets-")
        self.path = os.path.join(self.dir, "presets.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_user_preset_is_appended(self):
        self.write({"presets": [{"title": "My Own", "system_prompt": "be weird"}]})
        loaded = presets.load(user=self.path)
        self.assertEqual(len(loaded), 11)
        mine = presets.find("my-own", loaded)          # id derived from the title
        self.assertEqual(mine["system_prompt"], "be weird")

    def test_user_preset_overrides_by_id(self):
        self.write([{"id": "red-team", "title": "Red Team",
                     "system_prompt": "harsher"}])
        loaded = presets.load(user=self.path)
        self.assertEqual(len(loaded), 10)              # replaced, not added
        self.assertEqual(presets.find("red-team", loaded)["system_prompt"], "harsher")
        # Order is preserved: the override keeps the bundled slot.
        self.assertEqual([p["id"] for p in loaded][4], "red-team")

    def test_malformed_user_file_is_ignored(self):
        with open(self.path, "w") as f:
            f.write("{ this is not json")
        self.assertEqual(len(presets.load(user=self.path)), 10)

    def test_incomplete_presets_are_skipped(self):
        self.write({"presets": [{"title": "No Prompt"},
                                {"system_prompt": "no title"},
                                "not even a dict"]})
        self.assertEqual(len(presets.load(user=self.path)), 10)

    def test_env_var_points_at_the_user_file(self):
        os.environ["ROUNDTABLE_PRESETS"] = self.path
        try:
            self.assertEqual(presets.user_path(), self.path)
        finally:
            del os.environ["ROUNDTABLE_PRESETS"]


if __name__ == "__main__":
    unittest.main()

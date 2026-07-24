"""Saving, editing, deleting, and resetting user presets."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import presets


class SaveTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-presets-")
        self.path = os.path.join(self.dir, "presets.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_creates_the_file(self):
        preset, overwrote = presets.save("My Role", "be helpful", path=self.path)
        self.assertEqual(preset["id"], "my-role")
        self.assertFalse(overwrote)
        self.assertTrue(os.path.exists(self.path))
        loaded = presets.load(user=self.path)
        self.assertEqual(presets.find("my-role", loaded)["system_prompt"], "be helpful")

    def test_save_under_same_title_edits_in_place(self):
        presets.save("My Role", "version one", path=self.path)
        presets.save("My Role", "version two", path=self.path)
        loaded = presets.load(user=self.path)
        matches = [p for p in loaded if p["id"] == "my-role"]
        self.assertEqual(len(matches), 1)                # no duplicate
        self.assertEqual(matches[0]["system_prompt"], "version two")

    def test_save_requires_title_and_prompt(self):
        with self.assertRaises(ValueError):
            presets.save("", "be helpful", path=self.path)
        with self.assertRaises(ValueError):
            presets.save("My Role", "  ", path=self.path)

    def test_save_reports_bundled_collision_by_new_title(self):
        """A brand-new preset (no explicit id) whose derived slug happens to
        match a bundled id -- same warning path, different route in."""
        _, overwrote = presets.save("Developmental Editor", "be harsh", path=self.path)
        self.assertTrue(overwrote)   # slug(title) == "developmental-editor", bundled

    def test_editing_a_bundled_preset_by_its_real_id_is_flagged(self):
        """Bundled ids are hand-picked and don't always match slug(title) --
        "red-team" for "Red Team / Devil's Advocate". Editing it (dropdown
        already carries the real id) must still be recognised as a bundled
        override, not silently treated as an unrelated new preset."""
        _, overwrote = presets.save("Red Team / Devil's Advocate", "be harsher",
                                    preset_id="red-team", path=self.path)
        self.assertTrue(overwrote)

    def test_editing_by_id_does_not_duplicate_when_title_is_unchanged(self):
        presets.save("My Role", "v1", path=self.path)
        presets.save("My Role", "v2", preset_id="my-role", path=self.path)
        loaded = presets.load(user=self.path)
        self.assertEqual(len([p for p in loaded if p["id"] == "my-role"]), 1)
        self.assertEqual(presets.find("my-role", loaded)["system_prompt"], "v2")

    def test_editing_by_id_can_rename(self):
        presets.save("Old Name", "v1", path=self.path)
        presets.save("New Name", "v1", preset_id="old-name", path=self.path)
        loaded = presets.load(user=self.path)
        self.assertEqual(presets.find("old-name", loaded)["title"], "New Name")

    def test_save_preserves_bare_list_file_shape(self):
        with open(self.path, "w") as f:
            json.dump([{"id": "existing", "title": "Existing", "system_prompt": "x"}], f)
        presets.save("New One", "be new", path=self.path)
        with open(self.path) as f:
            data = json.load(f)
        self.assertIsInstance(data, list)           # shape preserved, not wrapped
        self.assertEqual(len(data), 2)

    def test_expects_hint_is_optional(self):
        preset, _ = presets.save("My Role", "be helpful", expects="", path=self.path)
        self.assertNotIn("expects", preset)
        preset, _ = presets.save("Other Role", "be helpful", expects="paste text",
                                 path=self.path)
        self.assertEqual(preset["expects"], "paste text")


class DeleteTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-presets-")
        self.path = os.path.join(self.dir, "presets.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_delete_removes_a_custom_preset(self):
        presets.save("My Role", "be helpful", path=self.path)
        self.assertTrue(presets.delete("my-role", path=self.path))
        self.assertIsNone(presets.find("my-role", presets.load(user=self.path)))

    def test_delete_missing_preset_returns_false(self):
        self.assertFalse(presets.delete("no-such-id", path=self.path))

    def test_deleting_an_override_reverts_to_the_bundled_original(self):
        presets.save("Red Team / Devil's Advocate", "be harsher",
                     preset_id="red-team", path=self.path)
        loaded = presets.load(user=self.path)
        self.assertEqual(presets.find("red-team", loaded)["system_prompt"], "be harsher")

        presets.delete("red-team", path=self.path)
        loaded = presets.load(user=self.path)
        # Back to the bundled text, not gone entirely.
        self.assertIn("red team analyst", presets.find("red-team", loaded)["system_prompt"])

    def test_deleting_other_presets_leaves_the_rest_alone(self):
        presets.save("Role A", "a", path=self.path)
        presets.save("Role B", "b", path=self.path)
        presets.delete("role-a", path=self.path)
        loaded = presets.load(user=self.path)
        self.assertIsNone(presets.find("role-a", loaded))
        self.assertIsNotNone(presets.find("role-b", loaded))


class ResetTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-presets-")
        self.path = os.path.join(self.dir, "presets.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reset_removes_the_user_file(self):
        presets.save("My Role", "be helpful", path=self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(presets.reset(path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_reset_with_nothing_to_reset_returns_false(self):
        self.assertFalse(presets.reset(path=self.path))

    def test_after_reset_only_bundled_presets_remain(self):
        presets.save("My Role", "be helpful", path=self.path)
        presets.save("Red Team / Devil's Advocate", "be harsher", path=self.path)
        presets.reset(path=self.path)
        loaded = presets.load(user=self.path)
        self.assertEqual(len(loaded), 10)            # exactly the bundled count
        self.assertIsNone(presets.find("my-role", loaded))
        self.assertIn("red team analyst", presets.find("red-team", loaded)["system_prompt"])


class BundledIdsTests(unittest.TestCase):

    def test_known_bundled_ids_are_present(self):
        ids = presets.bundled_ids()
        self.assertIn("red-team", ids)
        self.assertIn("line-editor", ids)
        self.assertEqual(len(ids), 10)


if __name__ == "__main__":
    unittest.main()

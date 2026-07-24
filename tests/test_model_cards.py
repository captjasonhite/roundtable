"""Loading, matching, saving and resetting per-model sampler cards."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import model_cards


class LoadAndMatchTests(unittest.TestCase):

    def test_bundled_cards_load(self):
        cards = model_cards.load(user="/nonexistent")
        ids = {c["id"] for c in cards}
        self.assertEqual(ids, {"gemma4", "apex", "qwen3.6", "cydonia"})

    def test_match_is_case_insensitive_substring(self):
        card = model_cards.match("Gemma4-26B-A4B-QAT-Uncensored")
        self.assertEqual(card["id"], "gemma4")

    def test_match_supports_multiple_substrings_on_one_card(self):
        self.assertEqual(model_cards.match("...heretic-v2-Native...")["id"], "qwen3.6")
        self.assertEqual(model_cards.match("...Fable-Fus-711...")["id"], "qwen3.6")

    def test_no_match_returns_none(self):
        self.assertIsNone(model_cards.match("SomeOtherModel-7B"))

    def test_cydonia_does_not_think(self):
        card = model_cards.find("cydonia")
        self.assertFalse(card["thinks"])
        self.assertEqual(card["thinking"], card["nothinking"])


class SaveResetTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-model-cards-")
        self.path = os.path.join(self.dir, "model-cards.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_overrides_only_the_numbers(self):
        model_cards.save("gemma4", {"temp": 0.5, "top_p": 0.9, "top_k": 64,
                                    "min_p": 0.05, "repeat": 1.1, "presence": 0},
                         {"temp": 0.5, "top_p": 0.9, "top_k": 64,
                          "min_p": 0.05, "repeat": 1.1, "presence": 0},
                         path=self.path)
        loaded = model_cards.load(user=self.path)
        card = model_cards.find("gemma4", loaded)
        self.assertEqual(card["thinking"]["temp"], 0.5)
        self.assertEqual(card["title"], "Gemma4")           # metadata carried over

    def test_save_unknown_card_id_raises(self):
        with self.assertRaises(ValueError):
            model_cards.save("not-a-card", {}, {}, path=self.path)

    def test_blank_field_is_stored_as_null(self):
        model_cards.save("cydonia",
                         {"temp": 1.0, "top_p": "", "top_k": "", "min_p": 0.03,
                          "repeat": 1.0, "presence": ""},
                         {"temp": 1.0, "top_p": "", "top_k": "", "min_p": 0.03,
                          "repeat": 1.0, "presence": ""},
                         path=self.path)
        card = model_cards.find("cydonia", model_cards.load(user=self.path))
        self.assertIsNone(card["thinking"]["top_p"])

    def test_reset_removes_the_user_override(self):
        model_cards.save("apex", {"temp": 2.0, "top_p": None, "top_k": None,
                                  "min_p": None, "repeat": None, "presence": None},
                         {"temp": 2.0, "top_p": None, "top_k": None,
                          "min_p": None, "repeat": None, "presence": None},
                         path=self.path)
        self.assertTrue(model_cards.reset("apex", path=self.path))
        card = model_cards.find("apex", model_cards.load(user=self.path))
        self.assertEqual(card["thinking"]["temp"], 1.0)      # back to bundled

    def test_reset_missing_override_returns_false(self):
        self.assertFalse(model_cards.reset("apex", path=self.path))

    def test_bad_number_raises(self):
        with self.assertRaises(ValueError):
            model_cards.save("gemma4", {"temp": "not-a-number"}, {}, path=self.path)


if __name__ == "__main__":
    unittest.main()

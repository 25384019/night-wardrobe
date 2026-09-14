import unittest

from tag_manager.style_units import build_style_unit, structure_prompt
from tag_manager.gallery import extract_structured_prompt


class StyleUnitTests(unittest.TestCase):
    def test_style_tokens_merge_trigger_artist_without_duplicates(self):
        unit = build_style_unit(["@musouzuki", "musouzuki"], ["1111"])
        self.assertEqual(["1111", "@musouzuki", "musouzuki"], unit["tokens"])

    def test_artist_equal_trigger_is_emitted_once(self):
        unit = build_style_unit(["@m8style"], ["@m8style"])
        self.assertEqual(["@m8style"], unit["tokens"])

    def test_character_and_unknown_tags_have_safe_buckets(self):
        result = structure_prompt(
            "rice shower (umamusume), 1girl, solo, looking at viewer, unknown_tag",
            character_tokens=["rice shower (umamusume)"],
        )
        self.assertEqual(["rice shower (umamusume)"], result["character_tokens"])
        self.assertEqual(["1girl", "solo", "looking at viewer", "unknown_tag"], result["other_tags"])

    def test_missing_lora_match_does_not_fail(self):
        result = structure_prompt("@artist, 1girl", artist_tokens=["@artist"])
        self.assertEqual(["@artist"], result["artist_tokens"])
        self.assertEqual([], result["style_unit"]["lora_refs"])

    def test_round_trip_keeps_token_order(self):
        result = structure_prompt("one, two, (three:1.2), four")
        self.assertEqual("one, two, (three:1.2), four", result["composed_prompt"])

    def test_gallery_exposes_additive_structured_fields(self):
        result = extract_structured_prompt({
            "parameters": "",
            "night_wardrobe_positive": "1111, @m8style, rice shower (umamusume), 1girl",
            "artist_tokens": "@m8style",
            "trigger_tokens": "1111",
            "character_tokens": "rice shower (umamusume)",
        })
        self.assertEqual("1111, @m8style, rice shower (umamusume), 1girl", result["original_prompt"])
        self.assertEqual(["rice shower (umamusume)"], result["character_tokens"])
        self.assertIn("style_unit_json", result)

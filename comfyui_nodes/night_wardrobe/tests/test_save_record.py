import unittest

from comfyui_nodes.night_wardrobe.nodes import NightWardrobeSaveAndRecord


class NightWardrobeSaveAndRecordTests(unittest.TestCase):
    def test_source_metadata_preserves_prompt_and_lora(self):
        metadata = NightWardrobeSaveAndRecord.source_metadata(
            "1girl, sunset",
            "blurry",
            [{"name": "style.safetensors", "model_strength": 1.12, "clip_strength": 1.0}],
        )
        self.assertEqual(metadata["night_wardrobe_positive"], "1girl, sunset")
        self.assertEqual(metadata["night_wardrobe_negative"], "blurry")
        self.assertEqual(metadata["night_wardrobe_loras"], "style.safetensors:1.12")

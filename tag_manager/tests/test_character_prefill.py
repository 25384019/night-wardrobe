import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from tag_manager import app as app_module


class CharacterPrefillTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)

    @patch("tag_manager.app.get_output_image_detail")
    def test_returns_reviewable_character_fields_without_saving(self, get_detail):
        get_detail.return_value = {
            "filename": "Alice_001.png",
            "negative_prompt": "lowres, blurry",
            "matched_loras": [{"name": "alice_style", "weight": 0.8}],
            "lora_trigger_words": "alice_style, blue eyes",
            "character_tags": "1girl, blue eyes, long hair",
        }
        response = self.client.get("/api/characters/prefill?image_id=12")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "image_id": 12,
            "name": "Alice_001",
            "lora": "alice_style",
            "lora_weight": 0.8,
            "trigger_words": "alice_style, blue eyes",
            "appearance": "1girl, blue eyes, long hair",
            "notes": "来源：输出图片元数据\n负向 Prompt：lowres, blurry",
        })
        get_detail.assert_called_once_with(image_id=12, include_workflow=False)

    @patch("tag_manager.app.get_output_image_detail", return_value=None)
    def test_unknown_image_returns_404(self, get_detail):
        response = self.client.get("/api/characters/prefill?image_id=999")
        self.assertEqual(response.status_code, 404)

    def test_invalid_image_id_returns_422(self):
        response = self.client.get("/api/characters/prefill?image_id=0")
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

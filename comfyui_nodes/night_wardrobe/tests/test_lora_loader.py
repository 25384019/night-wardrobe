import unittest
from unittest.mock import patch

from comfyui_nodes.night_wardrobe.nodes import NightWardrobeLoraLoader


class NightWardrobeLoraLoaderTests(unittest.TestCase):
    def test_empty_lora_stack_keeps_inputs(self):
        model = object()
        clip = object()
        self.assertEqual(NightWardrobeLoraLoader().load_loras(model, clip, []), (model, clip))

    def test_missing_lora_has_actionable_error(self):
        loader = NightWardrobeLoraLoader()
        with patch("comfyui_nodes.night_wardrobe.nodes.folder_paths") as paths:
            paths.get_full_path.return_value = None
            with patch("comfyui_nodes.night_wardrobe.nodes.comfy", object()):
                with self.assertRaisesRegex(FileNotFoundError, "未找到 LoRA：missing.safetensors"):
                    loader.load_loras(object(), object(), [{
                        "name": "missing.safetensors",
                        "model_strength": 1.0,
                        "clip_strength": 1.0,
                    }])

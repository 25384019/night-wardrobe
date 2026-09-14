from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from comfyui_nodes.night_wardrobe.nodes import NightWardrobePromptBuilder
from .test_database import make_wardrobe_db


class TestPromptBuilder(unittest.TestCase):
    def test_builds_character_outfit_and_extra_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_wardrobe_db(Path(tmpdir))
            positive, negative, loras = NightWardrobePromptBuilder().build(
                str(path), "可莉", "常服", "sunset", "blurry"
            )
        self.assertEqual(positive, "<lora:klee:1.0>, klee, 1girl, red dress, sunset")
        self.assertEqual(negative, "blurry")
        self.assertEqual(loras, [{"name": "klee.safetensors", "model_strength": 1.0, "clip_strength": 1.0}])

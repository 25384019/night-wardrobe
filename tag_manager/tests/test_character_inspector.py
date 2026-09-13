from pathlib import Path
import json
import tempfile
import unittest
from PIL import Image, PngImagePlugin
from tag_manager.character_inspector import inspect_image

class CharacterInspectorTests(unittest.TestCase):
    def test读取PNG参数和LoRA(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'Alice.png'
            info = PngImagePlugin.PngInfo(); info.add_text('parameters', '1girl, blue eyes\nNegative prompt: lowres\nSteps: 20, <lora:alice_style:0.7>')
            Image.new('RGB', (8, 8), 'white').save(path, pnginfo=info)
            result = inspect_image(path)
        self.assertEqual('Alice', result['name'])
        self.assertIn('1girl', result['positive_prompt'])
        self.assertEqual('alice_style', result['lora'])
        self.assertEqual(0.7, result['lora_weight'])

    def test读取ComfyUI工作流的提示词和LoRA(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'Comfy.png'
            workflow = {
                '1': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ['9', 0]}},
                '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'lowres, blurry'}},
                '3': {'class_type': 'KSampler', 'inputs': {'positive': ['1', 0], 'negative': ['2', 0]}},
                '4': {'class_type': 'LoraLoader', 'inputs': {'lora_name': 'anima\\style.safetensors', 'strength_model': 1.12}},
                '9': {'class_type': 'ShowText|pysssss', 'inputs': {'text': ['10', 0], 'text_0': '1girl, blue eyes'}},
                '10': {'class_type': 'Reroute', 'inputs': {}},
            }
            info = PngImagePlugin.PngInfo(); info.add_text('prompt', json.dumps(workflow))
            Image.new('RGB', (8, 8), 'white').save(path, pnginfo=info)
            result = inspect_image(path)
        self.assertEqual('1girl, blue eyes', result['positive_prompt'])
        self.assertEqual('lowres, blurry', result['negative_prompt'])
        self.assertEqual('style', result['lora'])
        self.assertEqual(1.12, result['lora_weight'])

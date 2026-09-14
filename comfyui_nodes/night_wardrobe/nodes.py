from __future__ import annotations

import json
import os
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
except ImportError:
    np = None
    Image = None
    PngInfo = None

try:
    import comfy.sd
    import comfy.utils
    import folder_paths
except ImportError:
    comfy = None
    folder_paths = None

from .database import WardrobeRepository


class NightWardrobePromptBuilder:
    CATEGORY = "夜之主衣柜"
    RETURN_TYPES = ("STRING", "STRING", "NIGHT_WARDROBE_LORAS")
    RETURN_NAMES = ("正面提示词", "负面提示词", "LoRA 配置")
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "衣柜数据库路径": ("STRING", {"default": ""}),
            "角色": ("STRING", {"default": ""}),
            "服装套组": ("STRING", {"default": ""}),
            "自定义正面": ("STRING", {"multiline": True, "default": ""}),
            "自定义负面": ("STRING", {"multiline": True, "default": ""}),
        }}

    def build(self, 衣柜数据库路径, 角色, 服装套组, 自定义正面, 自定义负面):
        parts = []
        loras = []
        if 角色:
            item = WardrobeRepository(衣柜数据库路径).get_character(角色)
            if item["lora"]:
                weight = float(item["lora_weight"])
                loras.append({"name": str(item["lora"]), "model_strength": weight, "clip_strength": weight})
                parts.append(f"<lora:{Path(str(item['lora'])).stem}:{weight}>")
            if item["appearance"]:
                parts.append(str(item["appearance"]))
            outfit_tags = WardrobeRepository(衣柜数据库路径).get_outfit(int(item["id"]), 服装套组)
            if outfit_tags:
                parts.append(outfit_tags)
        if 自定义正面.strip():
            parts.append(自定义正面.strip())
        return ", ".join(parts), 自定义负面.strip(), loras


class NightWardrobeLoraLoader:
    CATEGORY = "夜之主衣柜"
    RETURN_TYPES = ("MODEL", "CLIP")
    RETURN_NAMES = ("模型", "CLIP")
    FUNCTION = "load_loras"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "模型": ("MODEL",),
            "剪辑模型": ("CLIP",),
            "LoRA配置": ("NIGHT_WARDROBE_LORAS",),
        }}

    def load_loras(self, 模型, 剪辑模型, LoRA配置):
        if not LoRA配置:
            return 模型, 剪辑模型
        if folder_paths is None or comfy is None:
            raise RuntimeError("请在 ComfyUI 内运行夜之主衣柜 LoRA 加载节点")
        for item in LoRA配置:
            name = str(item["name"])
            path = folder_paths.get_full_path("loras", name)
            if path is None:
                raise FileNotFoundError(f"未找到 LoRA：{name}")
            state_dict = comfy.utils.load_torch_file(path, safe_load=True)
            模型, 剪辑模型 = comfy.sd.load_lora_for_models(
                模型,
                剪辑模型,
                state_dict,
                float(item["model_strength"]),
                float(item["clip_strength"]),
            )
        return 模型, 剪辑模型


class NightWardrobeSaveAndRecord:
    CATEGORY = "夜之主衣柜"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图片",)
    FUNCTION = "save_images"
    OUTPUT_NODE = True

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory() if folder_paths else ""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图片": ("IMAGE",),
                "文件名前缀": ("STRING", {"default": "NightWardrobe"}),
                "正面提示词": ("STRING", {"multiline": True, "default": ""}),
                "负面提示词": ("STRING", {"multiline": True, "default": ""}),
                "LoRA配置": ("NIGHT_WARDROBE_LORAS",),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    @staticmethod
    def source_metadata(positive, negative, loras):
        return {
            "night_wardrobe_positive": positive,
            "night_wardrobe_negative": negative,
            "night_wardrobe_loras": ", ".join(
                f"{item['name']}:{item['model_strength']}" for item in loras
            ),
        }

    def save_images(self, 图片, 文件名前缀, 正面提示词, 负面提示词, LoRA配置, prompt=None, extra_pnginfo=None):
        if folder_paths is None or np is None or Image is None or PngInfo is None:
            raise RuntimeError("请在 ComfyUI 内运行夜之主衣柜保存节点")
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            文件名前缀, self.output_dir, 图片[0].shape[1], 图片[0].shape[0]
        )
        source_metadata = self.source_metadata(正面提示词, 负面提示词, LoRA配置)
        results = []
        for batch_number, image in enumerate(图片):
            pixels = 255.0 * image.cpu().numpy()
            output = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))
            metadata = PngInfo()
            for key, value in source_metadata.items():
                metadata.add_text(key, value)
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for key, value in extra_pnginfo.items():
                    metadata.add_text(key, json.dumps(value))
            output_name = f"{filename.replace('%batch_num%', str(batch_number))}_{counter:05}_.png"
            output.save(os.path.join(full_output_folder, output_name), pnginfo=metadata, compress_level=4)
            results.append({"filename": output_name, "subfolder": subfolder, "type": "output"})
            counter += 1
        return {"ui": {"images": results}, "result": (图片,)}


NODE_CLASS_MAPPINGS = {
    "NightWardrobePromptBuilder": NightWardrobePromptBuilder,
    "NightWardrobeLoraLoader": NightWardrobeLoraLoader,
    "NightWardrobeSaveAndRecord": NightWardrobeSaveAndRecord,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "NightWardrobePromptBuilder": "夜之主衣柜 · 提示词组装",
    "NightWardrobeLoraLoader": "夜之主衣柜 · LoRA 加载",
    "NightWardrobeSaveAndRecord": "夜之主衣柜 · 保存并记录图源",
}

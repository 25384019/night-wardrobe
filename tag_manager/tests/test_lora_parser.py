"""v1.8.0 LoRA 解析器与 LoRA 卡页面测试：解析器单测 + 路由集成 + 模板契约。"""

from __future__ import annotations

import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tag_manager import app as app_module
from tag_manager import db
from tag_manager import lora_routes
from tag_manager.lora_parser import (
    LoraParseError,
    merge_trigger_words,
    normalize_base_model,
    parse_civitai_info,
    parse_safetensors_header,
    read_safetensors_header,
)


def make_header_bytes(metadata: dict) -> bytes:
    header = {"__metadata__": metadata, "lora.weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}
    raw = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def make_header_b64(metadata: dict) -> str:
    return base64.b64encode(make_header_bytes(metadata)).decode("ascii")


KOHYA_METADATA = {
    "ss_base_model_version": "sdxl_base_v1-0",
    "ss_network_dim": "32",
    "ss_output_name": "my-lora",
    "ss_tag_frequency": json.dumps({
        "dataset_a": {"1girl": 100, "sitting": 50, "smile": 30},
        "dataset_b": {"1girl": 20, "dress": 40},
    }),
}


class LoraParserTests(unittest.TestCase):
    def test读取header正常与异常(self) -> None:
        header = read_safetensors_header(make_header_bytes(KOHYA_METADATA))
        self.assertEqual("32", header["__metadata__"]["ss_network_dim"])
        with self.assertRaises(LoraParseError):
            read_safetensors_header(b"tiny")
        with self.assertRaises(LoraParseError):
            read_safetensors_header(struct.pack("<Q", 10) + b"not-json!!")

    def test基础模型归一化(self) -> None:
        self.assertEqual("SDXL", normalize_base_model("sdxl_base_v1-0"))
        self.assertEqual("Pony", normalize_base_model("pony_diffusion_v6"))
        self.assertEqual("Flux", normalize_base_model("flux1-dev"))
        self.assertEqual("SD1.5", normalize_base_model("sd_v1-5"))
        self.assertEqual("未知", normalize_base_model(""))

    def test解析kohya元数据(self) -> None:
        parsed = parse_safetensors_header({"__metadata__": KOHYA_METADATA})
        self.assertEqual("SDXL", parsed["base_model"])
        self.assertEqual("32", parsed["net_dim"])
        # 1girl 合并 120 居首，触发词 top 10
        self.assertTrue(parsed["trigger_words"].startswith("1girl"))
        freq = json.loads(parsed["tag_frequency"])
        self.assertEqual(120, freq["1girl"])
        self.assertEqual(50, freq["sitting"])
        self.assertEqual("my-lora", parsed["output_name"])

    def test无metadata兜底(self) -> None:
        parsed = parse_safetensors_header({"tensor": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}})
        self.assertEqual("未知", parsed["base_model"])
        self.assertEqual("", parsed["trigger_words"])
        self.assertEqual("{}", parsed["tag_frequency"])

    def test_civitai解析与合并(self) -> None:
        info = parse_civitai_info(json.dumps({
            "trainedWords": ["alice", "1girl"],
            "recommendedWeight": 0.7,
            "description": "<p>你好 <b>世界</b></p>",
        }))
        self.assertEqual(["alice", "1girl"], info["trained_words"])
        self.assertEqual(0.7, info["suggested_weight"])
        self.assertEqual("你好 世界", info["description"])
        with self.assertRaises(LoraParseError):
            parse_civitai_info("not-json")

    def test触发词合并优先级与去重(self) -> None:
        merged = merge_trigger_words(["Alice", " bob "], ["alice", "carol"])
        self.assertEqual("Alice, bob, carol", merged)


class LoraRouteTests(unittest.TestCase):
    """路由集成：临时 SQLite + 临时预览目录，不打真实库。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.preview_dir = Path(self.temp_dir.name) / "lora_previews"
        db.init_db(self.db_path)
        factory = lambda: db.connect(self.db_path)  # noqa: E731
        patchers = [
            mock.patch.object(lora_routes, "upsert_lora_card", lambda *a, **kw: db.upsert_lora_card(*a, connect_factory=factory, **kw)),
            mock.patch.object(lora_routes, "list_lora_cards", lambda *a, **kw: db.list_lora_cards(connect_factory=factory)),
            mock.patch.object(lora_routes, "get_lora_card", lambda *a, **kw: db.get_lora_card(*a, connect_factory=factory, **kw)),
            mock.patch.object(lora_routes, "get_lora_card_by_name", lambda *a, **kw: db.get_lora_card_by_name(*a, connect_factory=factory, **kw)),
            mock.patch.object(lora_routes, "update_lora_card_preview", lambda *a, **kw: db.update_lora_card_preview(*a, connect_factory=factory, **kw)),
            mock.patch.object(lora_routes, "update_lora_card_category", lambda *a, **kw: db.update_lora_card_category(*a, connect_factory=factory, **kw)),
            mock.patch.object(lora_routes, "delete_lora_card", lambda *a, **kw: db.delete_lora_card(*a, connect_factory=factory, **kw)),
            mock.patch.object(lora_routes, "LORA_PREVIEW_DIR", self.preview_dir),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def parse_one(self, filename: str = "my-lora.safetensors"):
        return self.client.post("/api/loras/parse", json={
            "filename": filename,
            "header_b64": make_header_b64(KOHYA_METADATA),
        })

    def test解析入库与页面展示(self) -> None:
        resp = self.parse_one()
        self.assertEqual(200, resp.status_code)
        card = resp.json()["card"]
        self.assertEqual("my-lora", card["name"])
        self.assertEqual("SDXL", card["base_model"])
        self.assertFalse(resp.json()["updated"])

        page = self.client.get("/loras")
        self.assertEqual(200, page.status_code)
        self.assertIn("my-lora", page.text)
        self.assertIn("SDXL", page.text)

    def test同名再解析为更新(self) -> None:
        self.parse_one()
        resp = self.parse_one()
        self.assertTrue(resp.json()["updated"])
        cards = db.list_lora_cards(connect_factory=lambda: db.connect(self.db_path))
        self.assertEqual(1, len(cards))

    def test非法header拒绝(self) -> None:
        resp = self.client.post("/api/loras/parse", json={
            "filename": "bad.safetensors",
            "header_b64": base64.b64encode(b"tiny").decode(),
        })
        self.assertEqual(422, resp.status_code)

    def test_civitai合并(self) -> None:
        self.parse_one()
        resp = self.client.post("/api/loras/civitai", json={
            "name": "my-lora",
            "civitai_text": json.dumps({"trainedWords": ["alice"], "recommendedWeight": 0.65, "description": "测试描述"}),
        })
        self.assertEqual(200, resp.status_code)
        card = resp.json()["card"]
        self.assertEqual(0.65, card["suggested_weight"])
        self.assertTrue(card["trigger_words"].startswith("alice"))
        self.assertEqual("测试描述", card["civitai_text"])

    def test_civitai无卡404(self) -> None:
        resp = self.client.post("/api/loras/civitai", json={"name": "ghost", "civitai_text": "{}"})
        self.assertEqual(404, resp.status_code)

    def test_lora分类可自动识别并手动修改(self) -> None:
        style_id = db.upsert_lora_card(
            "水彩画风",
            filename="watercolor_style.safetensors",
            connect_factory=lambda: db.connect(self.db_path),
        )
        character_id = db.upsert_lora_card(
            "小明",
            filename="xiaoming.safetensors",
            connect_factory=lambda: db.connect(self.db_path),
        )
        style = db.get_lora_card(style_id, connect_factory=lambda: db.connect(self.db_path))
        character = db.get_lora_card(character_id, connect_factory=lambda: db.connect(self.db_path))
        self.assertEqual("style", style["category"])
        self.assertEqual("character", character["category"])

        resp = self.client.patch(f"/api/loras/{character_id}/category", json={"category": "style"})
        self.assertEqual(200, resp.status_code)
        self.assertEqual("style", resp.json()["card"]["category"])

    def test预览图上传与删除(self) -> None:
        self.parse_one()
        card = db.get_lora_card_by_name("my-lora", connect_factory=lambda: db.connect(self.db_path))
        resp = self.client.post(
            f"/api/loras/{card['id']}/preview",
            files={"file": ("cover.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        self.assertEqual(200, resp.status_code)
        self.assertTrue((self.preview_dir / f"{card['id']}.png").exists())

        bad = self.client.post(
            f"/api/loras/{card['id']}/preview",
            files={"file": ("evil.exe", b"xx", "application/octet-stream")},
        )
        self.assertEqual(415, bad.status_code)

        resp = self.client.post(f"/loras/{card['id']}/delete", follow_redirects=False)
        self.assertEqual(303, resp.status_code)
        self.assertIsNone(db.get_lora_card(card["id"], connect_factory=lambda: db.connect(self.db_path)))
        self.assertFalse((self.preview_dir / f"{card['id']}.png").exists())


class LoraTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base = Path(app_module.BASE_DIR)
        cls.template = (base / "templates" / "loras.html").read_text(encoding="utf-8")
        cls.base_html = (base / "templates" / "base.html").read_text(encoding="utf-8")
        cls.style = (base / "static" / "style.css").read_text(encoding="utf-8")

    def test页面结构钩子(self) -> None:
        self.assertIn('id="loraUpload"', self.template)
        self.assertIn('id="loraFileInput"', self.template)
        self.assertIn("lora-card", self.template)
        self.assertIn("copyLoraTriggers", self.template)
        self.assertIn("uploadLoraPreview", self.template)
        self.assertIn('id="loraImageDialog"', self.template)
        self.assertIn("openLoraPreview", self.template)
        self.assertIn('id="loraCategoryTabs"', self.template)
        self.assertIn("saveLoraCategory", self.template)

    def test前端只传header不过整文件(self) -> None:
        self.assertIn("file.slice(0, 8)", self.template)
        self.assertIn("header_b64", self.template)
        self.assertNotIn('enctype="multipart/form-data"', self.template.split("</script>")[0])

    def test导航与样式(self) -> None:
        self.assertIn('href="/loras"', self.base_html)
        self.assertIn(".lora-grid", self.style)
        self.assertIn(".lora-badge", self.style)
        self.assertIn(".lora-upload", self.style)

    def test预览图居中并支持查看完整大图(self) -> None:
        self.assertIn(
            ".lora-card-preview img { width: 100%; height: 100%; object-fit: contain; object-position: center;",
            self.style,
        )
        self.assertIn(".lora-card-preview { position: relative; aspect-ratio: 2 / 3;", self.style)
        self.assertIn(".lora-image-dialog", self.style)

    def test触发词编辑保持保存与复制操作(self) -> None:
        self.assertIn('class="lora-trigger-actions"', self.template)
        self.assertIn("input.parentElement.querySelector('.lora-trigger-actions button')", self.template)
        self.assertIn("input.closest('.lora-card').dataset.triggers", self.template)
        self.assertIn("复制提示词", self.template)


if __name__ == "__main__":
    unittest.main()

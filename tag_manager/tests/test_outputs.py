import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from starlette.testclient import TestClient

from tag_manager.app import app
from tag_manager.db import connect, init_db
from tag_manager.gallery import GALLERY_DIR
from tag_manager.gallery import find_ksampler_connections
from tag_manager.outputs_service import (
    classify_prompt_safety,
    extract_file_date_and_time,
    favorite_to_gallery,
    get_configured_output_dir,
    get_default_output_dir,
    get_output_dates,
    get_output_image_detail,
    query_output_images,
    resolve_safe_output_file,
    scan_outputs,
    set_configured_output_dir,
)


class TestOutputsFeature(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test_outputs.sqlite3"
        init_db(cls.db_path)
        cls.patch1 = patch("tag_manager.outputs_service.connect", lambda: connect(cls.db_path))
        cls.patch2 = patch("tag_manager.outputs_routes.connect", lambda: connect(cls.db_path))
        cls.patch1.start()
        cls.patch2.start()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.patch1.stop()
        cls.patch2.stop()
        cls.temp_dir.cleanup()

    def test_default_output_dir_detected(self):
        default_dir = get_default_output_dir()
        self.assertTrue(default_dir.exists())

    def test_extract_file_date_from_folder_and_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            dated_folder = tmp_root / "26_09_13"
            dated_folder.mkdir()
            img_path = dated_folder / "test_sample.png"
            img = Image.new("RGB", (64, 64), color="blue")
            img.save(img_path)

            date_str, time_str, mtime = extract_file_date_and_time(img_path)
            self.assertEqual(date_str, "2026-09-13")
            self.assertTrue(len(time_str) >= 5)

    def test_scan_and_query_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            folder_a = tmp_root / "26_08_12"
            folder_a.mkdir()
            img1 = folder_a / "sample_1.png"
            Image.new("RGB", (128, 128), color="red").save(img1)

            res = scan_outputs(output_dir=tmp_root)
            self.assertGreaterEqual(res["scanned"], 1)
            self.assertGreaterEqual(res["ingested"], 1)

            # 再次扫描应为增量命中（ingested 为 0）
            res2 = scan_outputs(output_dir=tmp_root)
            self.assertEqual(res2["ingested"], 0)

            # 查询
            images, total = query_output_images(date="2026-08-12")
            self.assertGreaterEqual(total, 1)
            self.assertTrue(any(img["filename"] == "sample_1.png" for img in images))

            with connect() as conn:
                conn.execute("DELETE FROM output_images WHERE rel_path = '26_08_12/sample_1.png'")

    def test_outputs_routes_pages(self):
        resp = self.client.get("/outputs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("生图查看", resp.text)
        self.assertIn("日期归档", resp.text)
        self.assertIn("内容级别", resp.text)
        self.assertIn("评级设置", resp.text)
        self.assertIn("自定义敏感词", resp.text)
        self.assertIn("解析新增图源", resp.text)
        self.assertIn("WD14 评级", resp.text)
        self.assertIn("手动评级", resp.text)

    def test_manual_safety_override_keeps_user_choice(self):
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO output_images (rel_path, filename, file_date, safety_level, safety_source)
                VALUES ('manual_rating.png', 'manual_rating.png', '2026-09-14', 'normal', 'WD14+图源')
                """
            )
            image_id = conn.execute("SELECT id FROM output_images WHERE rel_path = 'manual_rating.png'").fetchone()[0]

        response = self.client.post(f"/api/outputs/{image_id}/safety", json={"level": "nsfw"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["safety_level"], "nsfw")

        detail = get_output_image_detail(image_id=image_id)
        self.assertEqual(detail["safety_level"], "nsfw")
        self.assertEqual(detail["safety_source"], "手动")

    def test_output_detail_uses_lora_library_for_copyable_tags(self):
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO lora_cards (name, filename, trigger_words)
                VALUES (?, ?, ?)
                """,
                ("星绘画风，触发词@stella", "stella_style.safetensors", "stella, stella style"),
            )
            conn.execute(
                """
                INSERT INTO output_images (rel_path, filename, file_date, positive_prompt, loras)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("lora_tags.png", "lora_tags.png", "2026-09-14", "stella, stella style, 1girl, blue eyes", "stella_style.safetensors:1.0"),
            )
            image_id = conn.execute("SELECT id FROM output_images WHERE rel_path = 'lora_tags.png'").fetchone()[0]

        detail = get_output_image_detail(image_id=image_id)
        self.assertEqual(detail["lora_trigger_words"], "stella, stella style")
        self.assertEqual(detail["lora_artist_strings"], "星绘画风")
        self.assertEqual(detail["style_prompt"], "stella, stella style")
        self.assertEqual(detail["image_tags"], "stella, stella style, 1girl, blue eyes")
        self.assertEqual(detail["character_prompt"], "1girl, blue eyes")

    def test_output_detail_uses_all_configured_style_triggers(self):
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO lora_cards (name, filename, trigger_words) VALUES (?, ?, ?)",
                ("翼画风，触发词@Yi", "翼画风，触发词@Yi.safetensors", "@tsubasa tsubasa, tsubasa_tsubasa, @Yi"),
            )
            conn.execute(
                "INSERT INTO output_images (rel_path, filename, file_date, positive_prompt, loras) VALUES (?, ?, ?, ?, ?)",
                ("yi_style.png", "yi_style.png", "2026-09-15", "nahida, 1girl, green eyes, @Yi", "翼画风，触发词@Yi:1.12"),
            )
            image_id = conn.execute("SELECT id FROM output_images WHERE rel_path='yi_style.png'").fetchone()[0]

        detail = get_output_image_detail(image_id=image_id)
        self.assertEqual(detail["lora_trigger_words"], "@tsubasa tsubasa, tsubasa_tsubasa, @Yi")
        self.assertEqual(detail["lora_artist_strings"], "翼画风")
        self.assertEqual(detail["style_prompt"], "@tsubasa tsubasa, tsubasa_tsubasa, @Yi")
        self.assertEqual(detail["character_prompt"], "nahida, 1girl, green eyes")

    def test_style_prompt_never_includes_chinese_trigger_descriptions(self):
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO lora_cards (name, filename, trigger_words) VALUES (?, ?, ?)",
                ("小麦画风，触发词@komugi 右斜杠(2212右斜杠)", "小麦画风.safetensors", "@komugi (2212), komugi_(2212), @komugi \\(2212\\)"),
            )
            conn.execute(
                "INSERT INTO output_images (rel_path, filename, file_date, positive_prompt, loras) VALUES (?, ?, ?, ?, ?)",
                ("komugi.png", "komugi.png", "2026-09-15", "nahida, 1girl", "小麦画风.safetensors:1.0"),
            )
            image_id = conn.execute("SELECT id FROM output_images WHERE rel_path='komugi.png'").fetchone()[0]

        detail = get_output_image_detail(image_id=image_id)
        self.assertEqual(detail["style_prompt"], "@komugi (2212), komugi_(2212), @komugi \\(2212\\)")
        self.assertNotIn("小麦", detail["style_prompt"])
        self.assertNotIn("右斜杠", detail["style_prompt"])

    def test_style_prompt_never_includes_ascii_lora_display_name(self):
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO lora_cards (name, filename, trigger_words) VALUES (?, ?, ?)",
                ("Tsubasa style, trigger @Yi", "tsubasa.safetensors", "@tsubasa tsubasa, tsubasa_tsubasa, @Yi"),
            )
            conn.execute(
                "INSERT INTO output_images (rel_path, filename, file_date, positive_prompt, loras) VALUES (?, ?, ?, ?, ?)",
                ("tsubasa.png", "tsubasa.png", "2026-09-15", "nahida, @Yi", "tsubasa.safetensors:1.0"),
            )
            image_id = conn.execute("SELECT id FROM output_images WHERE rel_path='tsubasa.png'").fetchone()[0]

        detail = get_output_image_detail(image_id=image_id)
        self.assertEqual(detail["style_prompt"], "@tsubasa tsubasa, tsubasa_tsubasa, @Yi")
        self.assertNotIn("Tsubasa style", detail["style_prompt"])

    def test_prompt_safety_uses_positive_prompt_only(self):
        self.assertEqual(classify_prompt_safety("1girl, school uniform, smile"), "normal")
        self.assertEqual(classify_prompt_safety("1girl, bikini, beach"), "suspicious")
        self.assertEqual(classify_prompt_safety("1girl, nude, nipples"), "nsfw")
        self.assertEqual(classify_prompt_safety(""), "suspicious")

    def test_gallery_source_prompt_flows_through_show_text_and_concat(self):
        graph = {
            "1": {"class_type": "KSampler", "inputs": {"positive": ["2", 0], "negative": ["3", 0]}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ["4", 0]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "worst quality, low quality"}},
            "4": {"class_type": "StringConcatenate", "inputs": {"string_a": ["5", 0], "string_b": ["7", 0], "delimiter": ", "}},
            "5": {"class_type": "ShowText|pysssss", "inputs": {"text": ["6", 1]}},
            "6": {"class_type": "DanbooruGalleryNode", "inputs": {"selection_data": '{"selections":[{"prompt":"1girl, completely nude, pussy"}]}'}},
            "7": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "masterpiece, best quality"}},
        }
        positive, negative = find_ksampler_connections(graph)
        self.assertEqual(positive, "1girl, completely nude, pussy, masterpiece, best quality")
        self.assertEqual(negative, "worst quality, low quality")

    def test_prompt_safety_handles_corrupt_source_delimiters(self):
        self.assertEqual(classify_prompt_safety("masterpiece��pussy��@mm"), "nsfw")
        self.assertEqual(classify_prompt_safety("masterpiece，pussy，@mm"), "nsfw")

    def test_show_text_prefers_longer_cached_tagger_output(self):
        graph = {
            "1": {"class_type": "KSampler", "inputs": {"positive": ["2", 0]}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ["3", 0]}},
            "3": {"class_type": "ShowText|pysssss", "inputs": {
                "text": ["4", 0], "text_0": "1girl, nude, pussy, spread legs"
            }},
            "4": {"class_type": "WD14Tagger|pysssss", "inputs": {"model": "wd-swinv2-tagger-v3"}},
        }
        positive, _ = find_ksampler_connections(graph)
        self.assertEqual(positive, "1girl, nude, pussy, spread legs")

    def test_outputs_file_streaming_and_security(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            test_img = tmp_root / "secure_sample.png"
            Image.new("RGB", (32, 32), color="green").save(test_img)

            # 正常访问
            path_safe = resolve_safe_output_file("secure_sample.png", output_dir=tmp_root)
            self.assertTrue(path_safe.is_file())

            # 路径越界拒绝
            with self.assertRaises(ValueError):
                resolve_safe_output_file("../outside.png", output_dir=tmp_root)

            with self.assertRaises(ValueError):
                resolve_safe_output_file("C:/windows/win.ini", output_dir=tmp_root)

    def test_outputs_favorite_to_gallery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            test_img = tmp_root / "favorite_me.png"
            Image.new("RGB", (64, 64), color="yellow").save(test_img)

            # 执行收藏
            res = favorite_to_gallery("favorite_me.png", category="测试收藏", output_dir=tmp_root)
            self.assertTrue(res["ok"])
            fav_path = GALLERY_DIR / res["gallery_path"]
            self.assertTrue(fav_path.exists())

            # 清理测试收藏文件（避免污染真实图库）
            if fav_path.exists():
                fav_path.unlink()
            fav_dir = GALLERY_DIR / "测试收藏"
            if fav_dir.exists() and not any(fav_dir.iterdir()):
                fav_dir.rmdir()

    def test_inspector_clears_hidden_or_stale_selection(self):
        template = (Path(__file__).parents[1] / "templates" / "outputs.html").read_text(encoding="utf-8")
        self.assertIn("function isVisibleImage(id)", template)
        self.assertIn("function validateCurrentSelection()", template)
        self.assertIn("cardEl.classList.contains('content-hidden')", template)
        self.assertIn("requestId !== inspectorRequestId", template)
        self.assertIn("validateCurrentSelection();", template)

    def test_output_card_hover_does_not_change_grid_row_height(self):
        css = (Path(__file__).parents[1] / "static" / "ui" / "pages" / "images.css").read_text(encoding="utf-8")
        self.assertNotIn(".output-card:hover .card-hover-submeta {\n  display: flex;", css)
        self.assertIn("top: auto;", css)

    def test_output_card_hides_timestamp_from_card_face(self):
        template = (Path(__file__).parents[1] / "templates" / "outputs.html").read_text(encoding="utf-8")
        self.assertNotIn('<span class="card-timestamp output-time-badge">', template)

    def test_inspector_has_separate_copyable_image_and_character_tags(self):
        template = (Path(__file__).parents[1] / "templates" / "outputs.html").read_text(encoding="utf-8")
        self.assertIn('id="inspector-image-tags"', template)
        self.assertIn('id="inspector-character-tags"', template)
        self.assertIn("copyInspectorTags", template)

    def test_shell_brand_does_not_wrap_when_sidebar_is_narrow(self):
        css = (Path(__file__).parents[1] / "static" / "ui" / "shell.css").read_text(encoding="utf-8")
        self.assertIn(".brand-text-col {", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn("text-overflow: ellipsis", css)

    def test_shell_stylesheet_version_refreshes_after_layout_fix(self):
        template = (Path(__file__).parents[1] / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn('/static/ui/shell.css?v=93', template)
        self.assertIn('/static/ui/pages/images.css?v=96', template)

    def test_fill_workbench_opens_workbench_after_staging_prompt(self):
        template = (Path(__file__).parents[1] / "templates" / "outputs.html").read_text(encoding="utf-8")
        start = template.index("window.sendSelectedToStudio")
        end = template.index("window.upscaleSelectedImage", start)
        self.assertIn("appendOutputPrompt(selectedImageId)", template[start:end])
        self.assertIn("window.location.href = '/workshop';", template[start:end])

    def test_modal_and_surgeon_fill_workbench_also_open_workbench(self):
        template = (Path(__file__).parents[1] / "templates" / "outputs.html").read_text(encoding="utf-8")
        modal_start = template.index("window.appendModalPrompt")
        modal_end = template.index("window.copyOutputPrompt", modal_start)
        surgeon_start = template.index("window.appendStudioPromptToWorkbench")
        surgeon_end = template.index("window.revealModalImage", surgeon_start)
        self.assertIn("window.location.href = '/workshop';", template[modal_start:modal_end])
        self.assertIn("window.location.href = '/workshop';", template[surgeon_start:surgeon_end])

    def test_fill_workbench_stages_prompt_in_custom_positive_field(self):
        template = (Path(__file__).parents[1] / "templates" / "outputs.html").read_text(encoding="utf-8")
        self.assertIn("function stagePromptForWorkbench(text)", template)
        self.assertIn("sessionStorage.setItem('_persist_ws-custom-pos', text.trim())", template)


if __name__ == "__main__":
    unittest.main()

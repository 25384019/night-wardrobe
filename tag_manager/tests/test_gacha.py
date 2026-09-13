from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tag_manager import app as app_module
from tag_manager import db, llm


def response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


class GachaApiTests(unittest.TestCase):
    """验证抽卡数据库、配置与模型代理的关键契约。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        db.init_db(self.db_path)
        self.connect_patch = mock.patch.object(app_module, "connect", lambda: db.connect(self.db_path))
        self.connect_patch.start()

    def tearDown(self) -> None:
        self.connect_patch.stop()
        self.temp_dir.cleanup()

    def update_settings(self, *, base_url="https://example.invalid/v1", api_key="测试密钥", model="test-model") -> None:
        app_module.api_gacha_settings_post({"base_url": base_url, "api_key": api_key, "model": model})

    def test_store支持写入删除并拒绝密钥(self) -> None:
        created = app_module.api_gacha_store_post({"key": "sd_oc_collections", "value": "[1]"})
        self.assertEqual({"ok": True}, response_json(created))
        self.assertEqual("[1]", response_json(app_module.api_gacha_store_get())["sd_oc_collections"])

        skipped = app_module.api_gacha_store_post({"key": "sd_api_keys", "value": "不应保存"})
        self.assertEqual({"ok": False, "skipped": True}, response_json(skipped))
        self.assertNotIn("sd_api_keys", response_json(app_module.api_gacha_store_get()))

        unrelated = app_module.api_gacha_store_post({"key": "other_key", "value": "不应保存"})
        self.assertEqual({"ok": False, "skipped": True}, response_json(unrelated))

        deleted = app_module.api_gacha_store_post({"key": "sd_oc_collections", "value": None})
        self.assertEqual({"ok": True}, response_json(deleted))
        self.assertNotIn("sd_oc_collections", response_json(app_module.api_gacha_store_get()))

    def test设置接口不返回明文密钥且可保留旧密钥(self) -> None:
        self.update_settings()
        public_settings = response_json(app_module.api_gacha_settings_get())
        self.assertEqual("https://example.invalid/v1", public_settings["base_url"])
        self.assertTrue(public_settings["has_key"])
        self.assertNotIn("api_key", public_settings)

        app_module.api_gacha_settings_post({"base_url": "https://new.invalid/v1", "model": "new-model"})
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        self.assertEqual("测试密钥", row["api_key"])
        self.assertEqual("new-model", row["model"])

    def test模型代理校验配置并透传默认参数(self) -> None:
        missing = app_module.api_gacha_llm({"messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual(400, missing.status_code)

        self.update_settings()
        with mock.patch.object(app_module, "chat_completion_messages", return_value="结果") as completion:
            response = app_module.api_gacha_llm({"messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual({"result": "结果"}, response_json(response))
        self.assertEqual(0.7, completion.call_args.kwargs["temperature"])
        self.assertEqual(8192, completion.call_args.kwargs["max_tokens"])

    def test代理错误会移除服务端密钥(self) -> None:
        self.update_settings(api_key="绝密值")
        with mock.patch.object(app_module, "chat_completion_messages", side_effect=RuntimeError("失败：绝密值")):
            response = app_module.api_gacha_llm({"messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual(500, response.status_code)
        self.assertNotIn("绝密值", response.body.decode("utf-8"))
        self.assertIn("***", response_json(response)["error"])

    def test模型列表使用同一服务端配置(self) -> None:
        self.update_settings()
        with mock.patch.object(app_module, "list_models", return_value=["a", "b"]) as list_models:
            response = app_module.api_gacha_models()
        self.assertEqual({"models": ["a", "b"]}, response_json(response))
        list_models.assert_called_once_with("https://example.invalid/v1", "测试密钥")


class GachaLlmTests(unittest.TestCase):
    def test消息请求只发送非空采样参数(self) -> None:
        fake_response = mock.MagicMock()
        fake_response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "完成"}}]
        }).encode("utf-8")
        with mock.patch("urllib.request.urlopen", return_value=fake_response) as urlopen:
            result = llm.chat_completion_messages(
                "https://example.invalid/v1/",
                "密钥",
                "模型",
                [{"role": "user", "content": "你好"}],
                temperature=None,
                top_p=0.9,
                max_tokens=100,
            )
        self.assertEqual("完成", result)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertNotIn("temperature", payload)
        self.assertEqual(0.9, payload["top_p"])
        self.assertEqual(100, payload["max_tokens"])
        self.assertEqual("https://example.invalid/v1/chat/completions", request.full_url)


class GachaStaticIntegrationTests(unittest.TestCase):
    def test抽卡页面和整页导航标记完整(self) -> None:
        base_dir = Path(app_module.BASE_DIR)
        gacha_html = (base_dir / "static" / "gacha" / "index.html").read_text(encoding="utf-8")
        base_html = (base_dir / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("/api/gacha/llm", gacha_html)
        self.assertIn("/api/gacha/store", gacha_html)
        self.assertIn("← 返回衣柜", gacha_html)
        self.assertNotIn("'Authorization'", gacha_html)
        self.assertIn('href="/gacha"', base_html)
        self.assertIn("data-full-load", base_html)
        self.assertIn("v1.24.1", base_html)
        self.assertIn("style.css?v=88", base_html)


if __name__ == "__main__":
    unittest.main()

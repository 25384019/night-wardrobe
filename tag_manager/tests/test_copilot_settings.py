from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tag_manager import app as app_module
from tag_manager import db
from tag_manager.copilot_service import SYSTEM_PROMPT, generate_suggestion


def response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


class CopilotSettingsApiTests(unittest.TestCase):
    """AI 提示词助手设置：复用 llm_settings，契约对齐抽卡。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "copilot-settings.sqlite3"
        db.init_db(self.db_path)
        self.connect_patch = mock.patch.object(app_module, "connect", lambda: db.connect(self.db_path))
        self.connect_patch.start()

    def tearDown(self) -> None:
        self.connect_patch.stop()
        self.temp_dir.cleanup()

    def test默认启用且不回传明文密钥(self) -> None:
        public = response_json(app_module.api_copilot_settings_get())
        self.assertTrue(public["enabled"])
        self.assertEqual("", public["base_url"])
        self.assertEqual("", public["model"])
        self.assertFalse(public["has_key"])
        self.assertNotIn("api_key", public)
        self.assertIn("default_system_prompt", public)
        self.assertEqual(60000, public["timeout"])
        self.assertEqual(3, public["retries"])

    def test保存后可保留旧密钥并写入启用与提示词(self) -> None:
        created = app_module.api_copilot_settings_post({
            "enabled": True,
            "base_url": "https://example.invalid/v1",
            "api_key": "测试密钥",
            "model": "test-model",
            "default_system_prompt": "保持猫耳",
        })
        self.assertEqual(True, response_json(created)["ok"])
        public = response_json(app_module.api_copilot_settings_get())
        self.assertEqual("https://example.invalid/v1", public["base_url"])
        self.assertEqual("test-model", public["model"])
        self.assertEqual("保持猫耳", public["default_system_prompt"])
        self.assertTrue(public["has_key"])
        self.assertNotIn("api_key", public)

        app_module.api_copilot_settings_post({
            "enabled": False,
            "base_url": "https://new.invalid/v1",
            "model": "new-model",
            "default_system_prompt": "追加构图",
        })
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        self.assertEqual("测试密钥", row["api_key"])
        self.assertEqual("new-model", row["model"])
        self.assertEqual(0, row["copilot_enabled"])
        self.assertEqual("追加构图", row["default_system_prompt"])
        self.assertFalse(response_json(app_module.api_copilot_settings_get())["enabled"])

    def test显式空字符串与空白密钥保留旧值(self) -> None:
        app_module.api_copilot_settings_post({
            "enabled": True,
            "base_url": "https://example.invalid/v1",
            "api_key": "测试密钥",
            "model": "test-model",
        })
        for blank in ("", "   ", "\t", "\n"):
            with self.subTest(api_key=repr(blank)):
                app_module.api_copilot_settings_post({
                    "enabled": True,
                    "base_url": "https://keep.invalid/v1",
                    "api_key": blank,
                    "model": "kept-model",
                })
                with db.connect(self.db_path) as conn:
                    row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
                self.assertEqual("测试密钥", row["api_key"])
                self.assertEqual("https://keep.invalid/v1", row["base_url"])
                self.assertEqual("kept-model", row["model"])
                public = response_json(app_module.api_copilot_settings_get())
                self.assertTrue(public["has_key"])
                self.assertNotIn("api_key", public)

        app_module.api_copilot_settings_post({"api_key": None, "model": "null-model"})
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        self.assertEqual("测试密钥", row["api_key"])
        self.assertEqual("null-model", row["model"])

        app_module.api_copilot_settings_post({"api_key": "  新密钥  "})
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        self.assertEqual("新密钥", row["api_key"])

    def test与抽卡共用同一行配置(self) -> None:
        app_module.api_gacha_settings_post({
            "base_url": "https://gacha.invalid/v1",
            "api_key": "抽卡密钥",
            "model": "gacha-model",
        })
        public = response_json(app_module.api_copilot_settings_get())
        self.assertEqual("https://gacha.invalid/v1", public["base_url"])
        self.assertEqual("gacha-model", public["model"])
        self.assertTrue(public["has_key"])

    def test关闭后助手端点返回400且不调用模型(self) -> None:
        app_module.api_copilot_settings_post({
            "enabled": False,
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
        })
        with mock.patch.object(app_module, "generate_suggestion", side_effect=AssertionError("不应调用 LLM")):
            response = app_module.api_workshop_copilot({
                "action": "diagnose",
                "instruction": "",
                "context": {
                    "positive": "1girl",
                    "negative": "lowres",
                    "recipe": {},
                    "enabled_contexts": ["positive", "negative", "recipe"],
                },
            })
        self.assertEqual(400, response.status_code)
        self.assertEqual(app_module.MSG_COPILOT_DISABLED, response_json(response)["error"])

    def test测试连接会脱敏密钥(self) -> None:
        app_module.api_copilot_settings_post({
            "base_url": "https://example.invalid/v1",
            "api_key": "绝密值",
            "model": "m",
        })
        with mock.patch.object(app_module, "chat_completion_messages", side_effect=RuntimeError("失败：绝密值")):
            response = app_module.api_copilot_test()
        self.assertEqual(500, response.status_code)
        self.assertNotIn("绝密值", response.body.decode("utf-8"))
        self.assertIn("***", response_json(response)["error"])

    def test模型列表使用同一服务端配置(self) -> None:
        app_module.api_copilot_settings_post({
            "base_url": "https://example.invalid/v1",
            "api_key": "测试密钥",
            "model": "m",
        })
        with mock.patch.object(app_module, "list_models", return_value=["a", "b"]) as list_models:
            response = app_module.api_copilot_models()
        self.assertEqual({"models": ["a", "b"]}, response_json(response))
        list_models.assert_called_once_with("https://example.invalid/v1", "测试密钥", timeout=60)

    def test超时与重试保存后持久化并回读(self) -> None:
        created = app_module.api_copilot_settings_post({
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
            "timeout": 120000,
            "retries": 5,
        })
        self.assertEqual(True, response_json(created)["ok"])
        settings = response_json(created)["settings"]
        self.assertEqual(120000, settings["timeout"])
        self.assertEqual(5, settings["retries"])
        public = response_json(app_module.api_copilot_settings_get())
        self.assertEqual(120000, public["timeout"])
        self.assertEqual(5, public["retries"])
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT timeout, retries FROM llm_settings WHERE id=1").fetchone()
        self.assertEqual(120000, row["timeout"])
        self.assertEqual(5, row["retries"])

    def test旧客户端不带新字段不报错且保留旧值(self) -> None:
        app_module.api_copilot_settings_post({
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
            "timeout": 90000,
            "retries": 7,
        })
        # 模拟旧客户端：只提交老三字段与启用状态，不带 timeout/retries
        response = app_module.api_copilot_settings_post({
            "enabled": True,
            "base_url": "https://old-client.invalid/v1",
            "api_key": "旧客户端密钥",
            "model": "old-model",
            "default_system_prompt": "旧提示词",
        })
        self.assertEqual(True, response_json(response)["ok"])
        public = response_json(app_module.api_copilot_settings_get())
        self.assertEqual("https://old-client.invalid/v1", public["base_url"])
        self.assertEqual(90000, public["timeout"])
        self.assertEqual(7, public["retries"])

    def test非法超时与重试回退旧值并钳制范围(self) -> None:
        app_module.api_copilot_settings_post({"timeout": 45000, "retries": 2})
        for bad in ("abc", None, {}):
            with self.subTest(timeout=repr(bad)):
                app_module.api_copilot_settings_post({"timeout": bad})
                self.assertEqual(45000, response_json(app_module.api_copilot_settings_get())["timeout"])
        app_module.api_copilot_settings_post({"timeout": 0, "retries": 99})
        public = response_json(app_module.api_copilot_settings_get())
        self.assertEqual(1000, public["timeout"])
        self.assertEqual(10, public["retries"])
        app_module.api_copilot_settings_post({"retries": -3})
        self.assertEqual(0, response_json(app_module.api_copilot_settings_get())["retries"])

    def test测试连接按配置超时与重试(self) -> None:
        app_module.api_copilot_settings_post({
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
            "timeout": 30000,
            "retries": 2,
        })
        calls = []

        def flaky(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) < 3:
                raise RuntimeError("网络抖动")
            return "ok"

        with mock.patch.object(app_module, "chat_completion_messages", side_effect=flaky):
            response = app_module.api_copilot_test()
        self.assertEqual(200, response.status_code)
        self.assertEqual(3, len(calls))
        for kwargs in calls:
            self.assertEqual(30, kwargs["timeout"])

    def test测试连接重试耗尽后仍脱敏密钥(self) -> None:
        app_module.api_copilot_settings_post({
            "base_url": "https://example.invalid/v1",
            "api_key": "绝密值",
            "model": "m",
            "retries": 1,
        })
        with mock.patch.object(app_module, "chat_completion_messages", side_effect=RuntimeError("失败：绝密值")) as call:
            response = app_module.api_copilot_test()
        self.assertEqual(500, response.status_code)
        self.assertEqual(2, call.call_count)
        self.assertNotIn("绝密值", response.body.decode("utf-8"))
        self.assertIn("***", response_json(response)["error"])

    def test老库迁移自动补齐超时与重试列(self) -> None:
        import sqlite3

        legacy_path = Path(self.temp_dir.name) / "legacy-llm.sqlite3"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute(
                """
                CREATE TABLE llm_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    base_url TEXT DEFAULT '',
                    api_key TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    default_system_prompt TEXT DEFAULT ''
                )
                """
            )
            conn.execute("INSERT INTO llm_settings (id, base_url) VALUES (1, 'https://legacy.invalid/v1')")
            db.ensure_llm_settings_columns(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_settings)").fetchall()}
            row = conn.execute("SELECT timeout, retries, copilot_enabled FROM llm_settings WHERE id=1").fetchone()
        finally:
            conn.close()
        self.assertIn("timeout", columns)
        self.assertIn("retries", columns)
        self.assertIn("copilot_enabled", columns)
        self.assertEqual(60000, row[0])
        self.assertEqual(3, row[1])
        self.assertEqual(1, row[2])

    def test附加提示词会传给助手生成(self) -> None:
        app_module.api_copilot_settings_post({
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
            "default_system_prompt": "保持猫耳",
        })
        captured = {}

        def fake_generate(request, **kwargs):
            captured.update(kwargs)
            return {
                "id": "llm-test",
                "action": "diagnose",
                "summary": "ok",
                "diagnostics": [],
                "operations": [],
                "stages": [],
            }

        with mock.patch.object(app_module, "generate_suggestion", side_effect=fake_generate):
            response = app_module.api_workshop_copilot({
                "action": "diagnose",
                "instruction": "",
                "context": {
                    "positive": "1girl",
                    "negative": "lowres",
                    "recipe": {},
                    "enabled_contexts": ["positive", "negative", "recipe"],
                },
            })
        self.assertEqual(200, response.status_code)
        self.assertEqual("保持猫耳", captured.get("extra_system_prompt"))


class CopilotSettingsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "copilot-settings-page.sqlite3"
        db.init_db(self.db_path)
        self.connect_patch = mock.patch.object(app_module, "connect", lambda: db.connect(self.db_path))
        self.connect_patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self) -> None:
        self.connect_patch.stop()
        self.temp_dir.cleanup()

    def test工坊页不回显密钥且绑定JSON设置(self) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE llm_settings SET base_url=?, api_key=?, model=? WHERE id=1",
                ("http://127.0.0.1:11434/v1", "super-secret-key", "workshop-model"),
            )
        response = self.client.get("/workshop")
        self.assertEqual(200, response.status_code)
        self.assertIn('id="wsLlmSettingsDialog"', response.text)
        self.assertIn('data-copilot-settings="1"', response.text)
        self.assertIn("/api/copilot/settings", response.text)
        self.assertIn("/static/copilot-settings.js?v=4", response.text)
        self.assertIn('id="wsCopilotSettingsBtn"', response.text)
        self.assertNotIn("super-secret-key", response.text)
        self.assertNotIn('action="/llm/settings"', response.text)

    def test兼容页可写入启用状态(self) -> None:
        response = self.client.post(
            "/llm/settings",
            data={
                "base_url": "https://example.invalid/v1",
                "api_key": "form-key",
                "model": "form-model",
                "default_system_prompt": "表单提示词",
                "copilot_enabled": "0",
                "next": "/llm",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        self.assertEqual(0, row["copilot_enabled"])
        self.assertEqual("表单提示词", row["default_system_prompt"])

    def test兼容页不回显明文密钥(self) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE llm_settings SET base_url=?, api_key=?, model=? WHERE id=1",
                ("http://127.0.0.1:11434/v1", "super-secret-key", "page-model"),
            )
        response = self.client.get("/llm")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("super-secret-key", response.text)
        self.assertIn('name="api_key"', response.text)
        self.assertIn('type="password"', response.text)
        self.assertIn("已保存，留空则保留原密钥", response.text)
        self.assertNotIn('value="super-secret-key"', response.text)

    def test兼容页留空或空白密钥保留原值(self) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE llm_settings SET base_url=?, api_key=?, model=? WHERE id=1",
                ("https://old.invalid/v1", "kept-secret", "old-model"),
            )
        for blank in ("", "   "):
            with self.subTest(api_key=repr(blank)):
                response = self.client.post(
                    "/llm/settings",
                    data={
                        "base_url": "https://example.invalid/v1",
                        "api_key": blank,
                        "model": "form-model",
                        "default_system_prompt": "表单提示词",
                        "copilot_enabled": "1",
                        "next": "/llm",
                    },
                    follow_redirects=False,
                )
                self.assertEqual(303, response.status_code)
                with db.connect(self.db_path) as conn:
                    row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
                self.assertEqual("kept-secret", row["api_key"])
                self.assertEqual("form-model", row["model"])


class CopilotSettingsStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.tpl = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.js = (base_dir / "static" / "copilot-settings.js").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")
        cls.base = (base_dir / "templates" / "base.html").read_text(encoding="utf-8")
        cls.llm = (base_dir / "templates" / "llm.html").read_text(encoding="utf-8")

    def test页面契约包含启用状态与模型操作(self) -> None:
        for needle in (
            'id="wsLlmEnabledBtn"',
            'id="wsLlmProvider"',
            'id="wsLlmBaseUrl"',
            'id="wsLlmApiKey"',
            'id="wsLlmModel"',
            'id="wsLlmTimeout"',
            'id="wsLlmRetries"',
            'id="wsLlmSystemPrompt"',
            'id="wsLlmTestBtn"',
            'id="wsLlmFetchModelsBtn"',
            'id="wsLlmSaveBtn"',
            'id="wsCopilotSettingsBtn"',
            'aria-label="AI 提示词助手设置"',
            'openCopilotSettingsDialog()',
        ):
            self.assertIn(needle, self.tpl)
        for provider in ("硅基流动", "DeepSeek", "OpenAI 兼容"):
            self.assertIn(provider, self.tpl)
        self.assertIn("v1.24.1", self.base)
        self.assertIn("style.css?v=88", self.base)

    def test脚本走JSON接口且不提交表单(self) -> None:
        for needle in ("/api/copilot/settings", "/api/copilot/models", "/api/copilot/test", "助手设置已保存"):
            self.assertIn(needle, self.js)
        self.assertNotIn("api_key=", self.js)
        self.assertIn(".ws-switch", self.style)
        self.assertIn(".ws-toast-success", self.style)

    def test脚本包含服务商预设与按服务商记忆(self) -> None:
        for needle in (
            "API_PROVIDERS",
            "siliconflow",
            "deepseek",
            "openai",
            "https://api.siliconflow.cn/v1",
            "https://api.deepseek.com/v1",
            "https://api.openai.com/v1",
            "copilot_api_provider",
            "copilot_api_base_",
            "copilot_api_model_",
            "copilot_api_keys",
            "wsLlmProvider",
            "wsLlmTimeout",
            "wsLlmRetries",
            '"connected"',
            "AbortController",
        ):
            self.assertIn(needle, self.js)
        self.assertIn("60000", self.js)
        self.assertIn(".ws-llm-conn-grid", self.style)
        self.assertIn('[data-state="connected"]', self.style)

    def test兼容页模板不回插明文密钥(self) -> None:
        self.assertNotIn("{{ settings.api_key }}", self.llm)
        self.assertIn('type="password"', self.llm)
        self.assertIn("留空则保留原密钥", self.llm)
        self.assertIn('autocomplete="new-password"', self.llm)

    def test测试连接busy不提前解锁(self) -> None:
        self.assertIn("var busyCount = 0;", self.js)
        self.assertIn("busyCount += 1", self.js)
        self.assertIn("busyCount -= 1", self.js)
        self.assertIn("saveSettings(false)", self.js)
        self.assertIn("function testConnection()", self.js)
        self.assertIn("function fetchModels()", self.js)
        self.assertIn(".finally(function () { setBusy(false); });", self.js)


class CopilotExtraPromptTests(unittest.TestCase):
    def test附加system_prompt会拼进系统消息(self) -> None:
        calls = []

        def fake_llm(*args, **kwargs):
            calls.append((args, kwargs))
            return json.dumps({
                "summary": "完成",
                "diagnostics": [],
                "operations": [],
                "stages": [],
            }, ensure_ascii=False)

        generate_suggestion(
            {
                "action": "diagnose",
                "instruction": "",
                "context": {
                    "positive": "1girl",
                    "negative": "lowres",
                    "recipe": {},
                    "enabled_contexts": ["positive", "negative", "recipe"],
                },
            },
            base_url="https://example.invalid/v1",
            api_key="k",
            model="m",
            extra_system_prompt="保持猫耳",
            llm_call=fake_llm,
            tool_registry={},
        )
        system = calls[0][0][3][0]["content"]
        self.assertIn(SYSTEM_PROMPT[:20], system)
        self.assertIn("用户附加要求", system)
        self.assertIn("保持猫耳", system)


if __name__ == "__main__":
    unittest.main()

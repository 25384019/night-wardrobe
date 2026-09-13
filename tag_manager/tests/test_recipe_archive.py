from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tag_manager import app as app_module
from tag_manager import db


class RecipeArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "recipe-archive.sqlite3"
        db.init_db(self.db_path)
        self.connect_patch = patch.object(app_module, "connect", lambda: db.connect(self.db_path))
        self.connect_patch.start()
        with db.connect(self.db_path) as conn:
            for index in range(3):
                conn.execute(
                    "INSERT INTO recipes (name, type, positive_prompt, source) VALUES (?, 'codex_prompt', ?, '法典.docx')",
                    (f"法典条目{index}", f"tag{index},"),
                )
            conn.execute("INSERT INTO recipes (name, type, positive_prompt) VALUES ('手工场景', 'scene', 'forest')")
        self.client = TestClient(app_module.app)

    def tearDown(self) -> None:
        self.connect_patch.stop()
        self.temp_dir.cleanup()

    def test法典配方可按页浏览(self) -> None:
        response = self.client.get("/recipes", params={"type": "codex_prompt", "limit": 1, "offset": 1})

        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("法典条目1", response.text)
        self.assertNotIn("法典条目0", response.text)
        self.assertIn("第 2 页", response.text)

    def test工作台默认排除法典归档但类型查询可获取(self) -> None:
        default_response = self.client.get("/api/recipes")
        codex_response = self.client.get("/api/recipes", params={"type": "codex_prompt"})

        self.assertEqual(["手工场景"], [row["name"] for row in default_response.json()])
        self.assertEqual(3, len(codex_response.json()))


from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tag_manager import db
from tag_manager.import_novelai_codex import extract_codex_recipes, import_novelai_codex


def write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        "<w:p><w:r><w:t>" + text.replace("&", "&amp;") + "</w:t></w:r></w:p>"
        for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


class NovelAiCodexImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "wardrobe.sqlite3"
        self.docx_path = self.root / "常规法典.docx"
        db.init_db(self.db_path)
        write_docx(
            self.docx_path,
            [
                "目录",
                "各种风格",
                "水彩风格",
                "artist:test, watercolor, 1girl, solo,",
                "场景组",
                "雨天街道",
                "rain, street, night, city lights,",
                "char1: umbrella, coat,",
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test提取中文标题与完整多段提示词(self) -> None:
        recipes = extract_codex_recipes(self.docx_path)

        self.assertEqual(2, len(recipes))
        self.assertEqual("水彩风格", recipes[0].title)
        self.assertEqual("artist:test, watercolor, 1girl, solo,", recipes[0].positive_prompt)
        self.assertEqual("雨天街道", recipes[1].title)
        self.assertEqual("rain, street, night, city lights,\nchar1: umbrella, coat,", recipes[1].positive_prompt)
        self.assertIn("各种风格", recipes[0].section_path)
        self.assertIn("场景组", recipes[1].section_path)

    def test重复导入更新配方而不写入标签(self) -> None:
        first = import_novelai_codex([self.docx_path], db_path=self.db_path)
        second = import_novelai_codex([self.docx_path], db_path=self.db_path)

        self.assertEqual(2, first["imported"])
        self.assertEqual(2, second["updated"])
        with db.connect(self.db_path) as conn:
            recipes = conn.execute("SELECT name, type, positive_prompt, source, notes FROM recipes ORDER BY id").fetchall()
            tag_count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        self.assertEqual(2, len(recipes))
        self.assertEqual(0, tag_count)
        self.assertEqual("codex_prompt", recipes[0]["type"])
        self.assertIn("常规法典.docx", recipes[0]["source"])
        self.assertIn("各种风格", recipes[0]["notes"])


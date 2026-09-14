from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from comfyui_nodes.night_wardrobe.database import WardrobeRepository, resolve_database_path


def make_wardrobe_db(root: Path) -> Path:
    path = root / "wardrobe.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE characters (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                lora TEXT DEFAULT '',
                lora_weight REAL DEFAULT 1.0,
                appearance TEXT DEFAULT ''
            );
            CREATE TABLE character_outfits (
                id INTEGER PRIMARY KEY,
                character_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                tags TEXT DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT INTO characters (id, name, lora, lora_weight, appearance) VALUES (1, '可莉', 'klee.safetensors', 1.0, 'klee, 1girl')"
        )
        conn.execute(
            "INSERT INTO character_outfits (character_id, name, tags) VALUES (1, '常服', 'red dress')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


class TestWardrobeRepository(unittest.TestCase):
    def test_reads_character_and_outfit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_wardrobe_db(Path(tmpdir))
            repo = WardrobeRepository(path)

            character = repo.get_character("可莉")

            self.assertEqual(character["appearance"], "klee, 1girl")
            self.assertEqual(repo.get_outfit(character["id"], "常服"), "red dress")

    def test_rejects_missing_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.sqlite3"

            with self.assertRaisesRegex(ValueError, "衣柜数据库不存在"):
                resolve_database_path(str(missing))


if __name__ == "__main__":
    unittest.main()

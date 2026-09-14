from __future__ import annotations

import sqlite3
from pathlib import Path


def resolve_database_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"衣柜数据库不存在：{path}")
    return path


class WardrobeRepository:
    def __init__(self, path: Path):
        self.path = resolve_database_path(str(path))

    def get_character(self, name: str) -> dict[str, object]:
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, name, lora, lora_weight, appearance FROM characters WHERE name = ?",
                (name,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ValueError(f"未找到角色：{name}")
        return dict(row)

    def get_outfit(self, character_id: int, name: str) -> str:
        if not name:
            return ""
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT tags FROM character_outfits WHERE character_id = ? AND name = ?",
                (character_id, name),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ValueError(f"未找到服装套组：{name}")
        return str(row[0] or "")

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .default_prompts import DEFAULT_NEGATIVE_TEMPLATE, DEFAULT_POSITIVE_TEMPLATE, DEFAULT_SYSTEM_PROMPT
from .style_units import build_style_unit

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
_DB_ENV = os.environ.get("WARDROBE_DB", "").strip()
DB_PATH = Path(_DB_ENV) if _DB_ENV else (BASE_DIR / "tag_wardrobe.sqlite3")
SQLITE_TIMEOUT_SECONDS = 15.0
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_TIMEOUT_SECONDS * 1000)


@contextmanager
def connect(db_path: Path | str | None = None):
    path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    path = db_path if db_path is not None else DB_PATH
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT DEFAULT 'tag',
                sort_order INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL UNIQUE,
                zh TEXT DEFAULT '',
                category TEXT DEFAULT '',
                subcategory TEXT DEFAULT '',
                source TEXT DEFAULT '',
                rating INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tag_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                positive_prompt TEXT DEFAULT '',
                negative_prompt TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                source TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS prompt_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                positive_template TEXT DEFAULT '',
                negative_template TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS gallery_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                title TEXT DEFAULT '',
                category TEXT DEFAULT '',
                positive_prompt TEXT DEFAULT '',
                negative_prompt TEXT DEFAULT '',
                workflow_json TEXT DEFAULT '',
                prompt_json TEXT DEFAULT '',
                parameters TEXT DEFAULT '',
                checkpoint TEXT DEFAULT '',
                loras TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '',
                metadata_source TEXT DEFAULT '',
                generation_params TEXT DEFAULT '',
                artist_tokens TEXT DEFAULT '',
                character_tokens TEXT DEFAULT '',
                other_tags TEXT DEFAULT '',
                style_unit_json TEXT DEFAULT '',
                original_prompt TEXT DEFAULT '',
                composed_prompt TEXT DEFAULT '',
                file_mtime REAL DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                rating INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                title TEXT DEFAULT '',
                category TEXT DEFAULT '',
                node_count INTEGER DEFAULT 0,
                checkpoint TEXT DEFAULT '',
                loras TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS llm_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                base_url TEXT DEFAULT '',
                api_key TEXT DEFAULT '',
                model TEXT DEFAULT '',
                default_system_prompt TEXT DEFAULT '',
                copilot_enabled INTEGER NOT NULL DEFAULT 1,
                timeout INTEGER NOT NULL DEFAULT 60000,
                retries INTEGER NOT NULL DEFAULT 3
            );

            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                lora TEXT DEFAULT '',
                lora_weight REAL DEFAULT 1.0,
                trigger_words TEXT DEFAULT '',
                appearance TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS character_outfits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                tags TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'scene',
                import_key TEXT,
                positive_prompt TEXT DEFAULT '',
                negative_prompt TEXT DEFAULT '',
                params_json TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                source TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS gacha_store (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS video_decrypt_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                input_path TEXT DEFAULT '',
                output_name TEXT NOT NULL,
                output_path TEXT DEFAULT '',
                input_size INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'uploading',
                error_code TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                progress_message TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT DEFAULT '',
                completed_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS manga_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'download',
                jmid TEXT DEFAULT '',
                title TEXT DEFAULT '',
                format TEXT NOT NULL DEFAULT 'pdf',
                params_json TEXT DEFAULT '',
                work_dir TEXT DEFAULT '',
                output_path TEXT DEFAULT '',
                output_size INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                progress_label TEXT DEFAULT '',
                error_code TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT DEFAULT '',
                completed_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS lora_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                filename TEXT DEFAULT '',
                base_model TEXT DEFAULT '',
                net_dim TEXT DEFAULT '',
                suggested_weight REAL DEFAULT 0.8,
                trigger_words TEXT DEFAULT '',
                tag_frequency TEXT DEFAULT '',
                civitai_text TEXT DEFAULT '',
                preview_image TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS copilot_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '新会话',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                context_snapshot_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                parent_session_id TEXT
            );

            CREATE TABLE IF NOT EXISTS copilot_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES copilot_sessions(id) ON DELETE CASCADE,
                UNIQUE (session_id, seq)
            );

            CREATE INDEX IF NOT EXISTS idx_video_decrypt_jobs_status
            ON video_decrypt_jobs(status);

            CREATE INDEX IF NOT EXISTS idx_manga_jobs_status
            ON manga_jobs(status);

            CREATE INDEX IF NOT EXISTS idx_tags_category_listing
            ON tags(category, rating DESC, subcategory, tag);

            CREATE INDEX IF NOT EXISTS idx_tags_rating_listing
            ON tags(rating DESC, category, subcategory, tag);

            CREATE INDEX IF NOT EXISTS idx_copilot_sessions_updated_at
            ON copilot_sessions(updated_at);

            CREATE INDEX IF NOT EXISTS idx_copilot_messages_session_id
            ON copilot_messages(session_id);

            CREATE TABLE IF NOT EXISTS output_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rel_path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                file_date TEXT NOT NULL,
                file_time TEXT DEFAULT '',
                file_mtime REAL DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                positive_prompt TEXT DEFAULT '',
                negative_prompt TEXT DEFAULT '',
                checkpoint TEXT DEFAULT '',
                loras TEXT DEFAULT '',
                workflow_json TEXT DEFAULT '',
                prompt_json TEXT DEFAULT '',
                parameters TEXT DEFAULT '',
                generation_params TEXT DEFAULT '',
                artist_tokens TEXT DEFAULT '',
                character_tokens TEXT DEFAULT '',
                other_tags TEXT DEFAULT '',
                style_unit_json TEXT DEFAULT '',
                original_prompt TEXT DEFAULT '',
                composed_prompt TEXT DEFAULT '',
                metadata_source TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_output_images_date
            ON output_images(file_date DESC, file_mtime DESC);

            CREATE INDEX IF NOT EXISTS idx_output_images_mtime
            ON output_images(file_mtime DESC);
            """
        )
        recipe_columns = {row["name"] for row in conn.execute("PRAGMA table_info(recipes)")}
        if "import_key" not in recipe_columns:
            conn.execute("ALTER TABLE recipes ADD COLUMN import_key TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_import_key ON recipes(import_key) WHERE import_key IS NOT NULL")
        character_columns = {row["name"] for row in conn.execute("PRAGMA table_info(characters)")}
        if "preview_image" not in character_columns:
            conn.execute("ALTER TABLE characters ADD COLUMN preview_image TEXT DEFAULT ''")
        output_columns = {row["name"] for row in conn.execute("PRAGMA table_info(output_images)")}
        if "safety_level" not in output_columns:
            conn.execute("ALTER TABLE output_images ADD COLUMN safety_level TEXT DEFAULT ''")
        if "safety_source" not in output_columns:
            conn.execute("ALTER TABLE output_images ADD COLUMN safety_source TEXT DEFAULT ''")
        if "prompt_version" not in output_columns:
            conn.execute("ALTER TABLE output_images ADD COLUMN prompt_version INTEGER NOT NULL DEFAULT 0")
        if "age_status" not in output_columns:
            conn.execute("ALTER TABLE output_images ADD COLUMN age_status TEXT NOT NULL DEFAULT 'unknown'")
        if "original_positive_prompt" not in output_columns:
            conn.execute("ALTER TABLE output_images ADD COLUMN original_positive_prompt TEXT DEFAULT ''")
        if "original_negative_prompt" not in output_columns:
            conn.execute("ALTER TABLE output_images ADD COLUMN original_negative_prompt TEXT DEFAULT ''")
        if "current_prompt_version_id" not in output_columns:
            conn.execute("ALTER TABLE output_images ADD COLUMN current_prompt_version_id INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_edit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                instruction TEXT DEFAULT '',
                edit_scopes TEXT DEFAULT '[]',
                positive_prompt_before TEXT NOT NULL,
                positive_prompt_after TEXT NOT NULL,
                negative_prompt_before TEXT DEFAULT '',
                negative_prompt_after TEXT DEFAULT '',
                diff_json TEXT NOT NULL DEFAULT '{}',
                applied_by TEXT DEFAULT 'user',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (image_id) REFERENCES output_images(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_prompt_edit_history_image_version
            ON prompt_edit_history(image_id, version DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                version_number INTEGER NOT NULL,
                parent_version_id INTEGER,
                positive_prompt TEXT NOT NULL,
                negative_prompt TEXT DEFAULT '',
                instruction TEXT DEFAULT '',
                remove_tags_json TEXT DEFAULT '[]',
                add_tags_json TEXT DEFAULT '[]',
                locked_tags_json TEXT DEFAULT '[]',
                diff_json TEXT DEFAULT '{}',
                model TEXT DEFAULT 'deepseek',
                applied_by TEXT DEFAULT 'user',
                is_original INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (image_id) REFERENCES output_images(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_version_id) REFERENCES prompt_versions(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_image_ver
            ON prompt_versions(image_id, version_number DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_parent
            ON prompt_versions(parent_version_id)
            """
        )
        try:
            from tag_manager.prompt_editor.migration import migrate_prompt_versions
            migrate_prompt_versions(conn)
        except Exception:
            pass
        default_categories = [
            ("1.镜头", "tag", 10),
            ("2.人物", "tag", 20),
            ("3.服饰", "tag", 30),
            ("4.表情", "tag", 40),
            ("4.角色/作品", "tag", 42),
            ("5.动作", "tag", 50),
            ("6.场景道具", "tag", 60),
            ("8.画风与质量", "tag", 66),
            ("0.可以涩涩", "tag", 70),
            ("tag组合", "group", 90),
            ("手工添加", "tag", 100),
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO categories (name, kind, sort_order)
            VALUES (?, ?, ?)
            """,
            default_categories,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO prompt_templates
                (name, positive_template, negative_template, notes)
            VALUES (?, ?, ?, ?)
            """,
            ("默认 SDXL 提示词模板", DEFAULT_POSITIVE_TEMPLATE, DEFAULT_NEGATIVE_TEMPLATE, "内置默认模板，可在页面中编辑。"),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO llm_settings
                (id, base_url, api_key, model, default_system_prompt)
            VALUES (1, '', '', '', ?)
            """,
            (DEFAULT_SYSTEM_PROMPT,),
        )
        ensure_gallery_image_columns(conn)
        ensure_output_images_columns(conn)
        ensure_video_decrypt_job_columns(conn)
        ensure_llm_settings_columns(conn)


def ensure_output_images_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(output_images)").fetchall()}
    columns = {
        "safety_level": "TEXT DEFAULT ''",
        "safety_source": "TEXT DEFAULT ''",
        "prompt_version": "INTEGER NOT NULL DEFAULT 1",
        "age_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "artist_tokens": "TEXT DEFAULT ''",
        "character_tokens": "TEXT DEFAULT ''",
        "other_tags": "TEXT DEFAULT ''",
        "style_unit_json": "TEXT DEFAULT ''",
        "original_prompt": "TEXT DEFAULT ''",
        "composed_prompt": "TEXT DEFAULT ''",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE output_images ADD COLUMN {name} {definition}")


def ensure_gallery_image_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(gallery_images)").fetchall()}
    columns = {
        "metadata_json": "TEXT DEFAULT ''",
        "metadata_source": "TEXT DEFAULT ''",
        "generation_params": "TEXT DEFAULT ''",
        "artist_tokens": "TEXT DEFAULT ''",
        "character_tokens": "TEXT DEFAULT ''",
        "other_tags": "TEXT DEFAULT ''",
        "style_unit_json": "TEXT DEFAULT ''",
        "original_prompt": "TEXT DEFAULT ''",
        "composed_prompt": "TEXT DEFAULT ''",
        "file_mtime": "REAL DEFAULT 0",
        "file_size": "INTEGER DEFAULT 0",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE gallery_images ADD COLUMN {name} {definition}")


def ensure_video_decrypt_job_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(video_decrypt_jobs)").fetchall()}
    columns = {
        "progress": "REAL NOT NULL DEFAULT 0",
        "progress_message": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE video_decrypt_jobs ADD COLUMN {name} {definition}")


def ensure_llm_settings_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(llm_settings)").fetchall()}
    columns = {
        "copilot_enabled": "INTEGER NOT NULL DEFAULT 1",
        "timeout": "INTEGER NOT NULL DEFAULT 60000",
        "retries": "INTEGER NOT NULL DEFAULT 3",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE llm_settings ADD COLUMN {name} {definition}")


def upsert_category(name: str, kind: str = "tag", sort_order: int = 0, notes: str = "") -> None:
    name = clean_text(name)
    if not name:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO categories (name, kind, sort_order, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                kind=excluded.kind,
                notes=COALESCE(NULLIF(excluded.notes, ''), categories.notes),
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, clean_text(kind) or "tag", sort_order, clean_text(notes)),
        )


def upsert_tag(tag: str, zh: str = "", category: str = "", subcategory: str = "", source: str = "", notes: str = "") -> None:
    tag = clean_text(tag)
    category = clean_text(category)
    if category:
        upsert_category(category)
    if not tag:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tags (tag, zh, category, subcategory, source, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET
                zh = COALESCE(NULLIF(excluded.zh, ''), tags.zh),
                category = COALESCE(NULLIF(excluded.category, ''), tags.category),
                subcategory = COALESCE(NULLIF(excluded.subcategory, ''), tags.subcategory),
                source = CASE
                    WHEN tags.source = '' THEN excluded.source
                    WHEN excluded.source = '' THEN tags.source
                    WHEN instr(tags.source, excluded.source) > 0 THEN tags.source
                    ELSE tags.source || '; ' || excluded.source
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (tag, clean_text(zh), clean_text(category), clean_text(subcategory), clean_text(source), clean_text(notes)),
        )


def upsert_many_tags(rows: Iterable[tuple[str, str, str, str, str]]) -> int:
    count = 0
    for tag, zh, category, subcategory, source in rows:
        if clean_text(tag):
            upsert_tag(tag, zh, category, subcategory, source)
            count += 1
    return count


def upsert_group(name: str, category: str = "", positive_prompt: str = "", negative_prompt: str = "", notes: str = "", source: str = "") -> None:
    name = clean_text(name)
    category = clean_text(category)
    if category:
        upsert_category(category, kind="group")
    if not name:
        return
    with connect() as conn:
        existing = conn.execute("SELECT id FROM tag_groups WHERE name = ? AND source = ?", (name, source)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE tag_groups SET category=?, positive_prompt=?, negative_prompt=?, notes=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (category, positive_prompt, negative_prompt, notes, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO tag_groups (name, category, positive_prompt, negative_prompt, notes, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, category, positive_prompt, negative_prompt, notes, source),
            )


def upsert_character(name: str, lora: str = "", lora_weight: float = 1.0, trigger_words: str = "", appearance: str = "", notes: str = "") -> int:
    name = clean_text(name)
    if not name:
        return 0
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO characters (name, lora, lora_weight, trigger_words, appearance, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                lora=COALESCE(NULLIF(excluded.lora, ''), characters.lora),
                lora_weight=excluded.lora_weight,
                trigger_words=COALESCE(NULLIF(excluded.trigger_words, ''), characters.trigger_words),
                appearance=COALESCE(NULLIF(excluded.appearance, ''), characters.appearance),
                notes=COALESCE(NULLIF(excluded.notes, ''), characters.notes),
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, clean_text(lora), lora_weight, clean_text(trigger_words), clean_text(appearance), clean_text(notes)),
        )
        row = conn.execute("SELECT id FROM characters WHERE name=?", (name,)).fetchone()
        return row["id"] if row else 0


def upsert_lora_card(
    name: str,
    filename: str = "",
    base_model: str = "",
    net_dim: str = "",
    suggested_weight: float = 0.8,
    trigger_words: str = "",
    tag_frequency: str = "",
    civitai_text: str = "",
    notes: str = "",
    connect_factory=connect,
) -> int:
    name = clean_text(name)
    if not name:
        return 0
    with connect_factory() as conn:
        conn.execute(
            """
            INSERT INTO lora_cards
                (name, filename, base_model, net_dim, suggested_weight, trigger_words, tag_frequency, civitai_text, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                filename=COALESCE(NULLIF(excluded.filename, ''), lora_cards.filename),
                base_model=COALESCE(NULLIF(excluded.base_model, ''), lora_cards.base_model),
                net_dim=COALESCE(NULLIF(excluded.net_dim, ''), lora_cards.net_dim),
                suggested_weight=excluded.suggested_weight,
                trigger_words=COALESCE(NULLIF(excluded.trigger_words, ''), lora_cards.trigger_words),
                tag_frequency=COALESCE(NULLIF(excluded.tag_frequency, ''), lora_cards.tag_frequency),
                civitai_text=COALESCE(NULLIF(excluded.civitai_text, ''), lora_cards.civitai_text),
                notes=COALESCE(NULLIF(excluded.notes, ''), lora_cards.notes),
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, clean_text(filename), clean_text(base_model), clean_text(net_dim),
             suggested_weight, clean_text(trigger_words), tag_frequency, civitai_text, clean_text(notes)),
        )
        row = conn.execute("SELECT id FROM lora_cards WHERE name=?", (name,)).fetchone()
        return row["id"] if row else 0


def list_lora_cards(connect_factory=connect) -> list[dict]:
    with connect_factory() as conn:
        rows = conn.execute(
            "SELECT * FROM lora_cards ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [_decorate_lora_card(dict(row)) for row in rows]


def get_lora_card(card_id: int, connect_factory=connect) -> dict | None:
    with connect_factory() as conn:
        row = conn.execute("SELECT * FROM lora_cards WHERE id=?", (card_id,)).fetchone()
    return _decorate_lora_card(dict(row)) if row else None


def get_lora_card_by_name(name: str, connect_factory=connect) -> dict | None:
    with connect_factory() as conn:
        row = conn.execute("SELECT * FROM lora_cards WHERE name=?", (name,)).fetchone()
    return _decorate_lora_card(dict(row)) if row else None


def _decorate_lora_card(card: dict) -> dict:
    triggers = [item.strip() for item in str(card.get("trigger_words", "")).split(",") if item.strip()]
    reference = str(card.get("filename") or card.get("name") or "")
    card["style_unit"] = build_style_unit(
        trigger_tokens=triggers,
        lora_refs=[reference] if reference else [],
        weight=card.get("suggested_weight"),
        source="lora_library",
    )
    return card


def update_lora_card_preview(card_id: int, preview_image: str, connect_factory=connect) -> None:
    with connect_factory() as conn:
        conn.execute(
            "UPDATE lora_cards SET preview_image=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (preview_image, card_id),
        )


def update_lora_card_triggers(card_id: int, trigger_words: str, connect_factory=connect) -> None:
    with connect_factory() as conn:
        conn.execute(
            "UPDATE lora_cards SET trigger_words=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (clean_text(trigger_words), card_id),
        )


def delete_lora_card(card_id: int, connect_factory=connect) -> dict | None:
    with connect_factory() as conn:
        row = conn.execute("SELECT * FROM lora_cards WHERE id=?", (card_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM lora_cards WHERE id=?", (card_id,))
    return dict(row)


def add_outfit(character_id: int, name: str, tags: str = "", notes: str = "") -> None:
    name = clean_text(name)
    if not name or not character_id:
        return
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM character_outfits WHERE character_id=? AND name=?", (character_id, name)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE character_outfits SET tags=?, notes=? WHERE id=?",
                (clean_text(tags), clean_text(notes), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO character_outfits (character_id, name, tags, notes) VALUES (?, ?, ?, ?)",
                (character_id, name, clean_text(tags), clean_text(notes)),
            )


def upsert_recipe(name: str, type: str = "scene", positive_prompt: str = "", negative_prompt: str = "", params_json: str = "", notes: str = "", source: str = "") -> None:
    name = clean_text(name)
    if not name:
        return
    with connect() as conn:
        existing = conn.execute("SELECT id FROM recipes WHERE name=? AND type=?", (name, type)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE recipes SET positive_prompt=?, negative_prompt=?, params_json=?, notes=?, source=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (positive_prompt, negative_prompt, params_json, clean_text(notes), clean_text(source), existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO recipes (name, type, positive_prompt, negative_prompt, params_json, notes, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, type, positive_prompt, negative_prompt, params_json, clean_text(notes), clean_text(source)),
            )


def clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


init_db()

from pathlib import Path

from PIL import Image

from tag_manager.db import connect, init_db
from tag_manager.outputs_service import reparse_new_outputs, scan_outputs
from tag_manager.prompt_editor.version_manager import VersionManager


def test_reparse_preserves_current_prompt_version_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "reparse.sqlite3"
    monkeypatch.setattr("tag_manager.db.DB_PATH", db_path)
    init_db(db_path)
    image_path = tmp_path / "image.png"
    Image.new("RGB", (8, 8), "white").save(image_path)

    with connect(db_path) as conn:
        image_id = conn.execute(
            "INSERT INTO output_images (rel_path, filename, file_date, positive_prompt, negative_prompt, original_positive_prompt, original_negative_prompt, prompt_version) VALUES (?, ?, '2026-09-15', ?, ?, ?, ?, 0)",
            ("image.png", "image.png", "working prompt", "working negative", "source prompt", "source negative"),
        ).lastrowid
    version = VersionManager.create_version(image_id, "edited prompt", negative_prompt="edited negative")

    with connect(db_path) as conn:
        conn.execute(
            "UPDATE output_images SET current_prompt_version_id = ?, prompt_version = ? WHERE id = ?",
            (version["id"], version["version_number"], image_id),
        )

    import tag_manager.outputs_service as service
    monkeypatch.setattr(service, "connect", lambda: connect(db_path))
    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "_get_incremental_rows", lambda conn, key, columns: ([{"id": image_id, "rel_path": "image.png", "updated_at": "2026-09-15 00:00:01"}], False, 0))
    monkeypatch.setattr(service, "_advance_processing_watermark", lambda conn, key, rows: None)
    monkeypatch.setattr(service, "read_image_metadata", lambda path: {})
    monkeypatch.setattr(service, "extract_structured_prompt", lambda meta: {"artist_tokens": [], "character_tokens": [], "other_tags": [], "style_unit_json": "", "original_prompt": "source prompt", "composed_prompt": "source prompt"})
    monkeypatch.setattr(service, "extract_prompts", lambda meta: ("source prompt", "source negative", "", "", "base.safetensors", "", "", "{}", "test", "{}"))

    result = reparse_new_outputs(image_path.parent)
    assert result["updated"] == 1
    with connect(db_path) as conn:
        row = conn.execute("SELECT positive_prompt, negative_prompt, original_positive_prompt FROM output_images WHERE id = ?", (image_id,)).fetchone()
    assert row["positive_prompt"] == "edited prompt"
    assert row["negative_prompt"] == "edited negative"
    assert row["original_positive_prompt"] == "source prompt"


def test_reparse_detects_new_file_without_separate_refresh(tmp_path, monkeypatch):
    db_path = tmp_path / "reparse.sqlite3"
    monkeypatch.setattr("tag_manager.db.DB_PATH", db_path)
    init_db(db_path)

    import tag_manager.outputs_service as service
    monkeypatch.setattr(service, "connect", lambda: connect(db_path))
    monkeypatch.setattr(service, "init_db", lambda: None)

    Image.new("RGB", (8, 8), "white").save(tmp_path / "old.png")
    scan_outputs(tmp_path)
    assert reparse_new_outputs(tmp_path)["baseline"] is True

    Image.new("RGB", (8, 8), "blue").save(tmp_path / "new.png")
    result = reparse_new_outputs(tmp_path)

    assert result["updated"] == 1
    with connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM output_images WHERE rel_path='new.png'").fetchone()[0] == 1

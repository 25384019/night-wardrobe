from __future__ import annotations

import os
import uuid
import pytest
from pathlib import Path

from tag_manager.db import connect, init_db
from tag_manager.prompt_editor.version_manager import VersionManager
from tag_manager.prompt_editor.schemas import (
    PromptVersionApplyRequest,
    PromptVersionSetCurrentRequest,
)
from tag_manager.prompt_editor.service import PromptEditorService


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_versions.sqlite3"
    monkeypatch.setattr("tag_manager.db.DB_PATH", test_db)
    init_db(test_db)
    return test_db


def create_dummy_image(positive_prompt: str = "1girl, solo, masterpiece, original_hat") -> int:
    unique_rel = f"test_{uuid.uuid4().hex}.png"
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO output_images (
                rel_path, filename, file_date, positive_prompt, negative_prompt,
                original_positive_prompt, original_negative_prompt, prompt_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (unique_rel, "test.png", "2026-09-14", positive_prompt, "worst quality", positive_prompt, "worst quality"),
        )
        img_id = cursor.lastrowid
        # Ensure v0 Original exists
        cursor.execute(
            """
            INSERT INTO prompt_versions (
                image_id, version_number, parent_version_id, positive_prompt, negative_prompt,
                instruction, is_original, applied_by
            ) VALUES (?, 0, NULL, ?, 'worst quality', 'Original from metadata', 1, 'original')
            """,
            (img_id, positive_prompt),
        )
        v0_id = cursor.lastrowid
        conn.execute("UPDATE output_images SET current_prompt_version_id = ? WHERE id = ?", (v0_id, img_id))
        return img_id


def test_1_v0_original_protection_and_immutable():
    """v0 Original is strictly immutable and cannot be deleted."""
    img_id = create_dummy_image("1girl, blonde hair, original_dress")
    versions = VersionManager.get_versions(img_id)

    assert len(versions) == 1
    v0 = versions[0]
    assert v0["version_number"] == 0
    assert v0["is_original"] is True
    assert v0["positive_prompt"] == "1girl, blonde hair, original_dress"
    assert v0["is_current"] is True

    # Attempt to delete v0 -> must raise PermissionError
    with pytest.raises(PermissionError, match="Cannot delete immutable v0 Original"):
        VersionManager.delete_version(v0["id"])


def test_2_five_consecutive_edits_all_inspectable():
    """5 consecutive edits create v1 through v5 without losing any history."""
    img_id = create_dummy_image("1girl, long hair, red dress, hat")

    v_ids = []
    current_prompt = "1girl, long hair, red dress, hat"
    for i in range(1, 6):
        new_prompt = f"{current_prompt}, edit_{i}"
        res = VersionManager.create_version(
            image_id=img_id,
            positive_prompt=new_prompt,
            instruction=f"Edit number {i}",
            diff_json={"added": [f"edit_{i}"]},
        )
        assert res["success"] is True
        assert res["version_number"] == i
        v_ids.append(res["id"])
        current_prompt = new_prompt

    versions = VersionManager.get_versions(img_id)
    assert len(versions) == 6  # v0 + v1..v5

    # Check v0 is untouched
    assert versions[0]["version_number"] == 0
    assert versions[0]["is_original"] is True

    # Check v5 is current
    assert versions[5]["version_number"] == 5
    assert versions[5]["is_current"] is True

    # Check parent linkage
    assert versions[1]["parent_version_id"] == versions[0]["id"]
    for i in range(2, 6):
        assert versions[i]["parent_version_id"] == versions[i - 1]["id"]


def test_3_branching_from_v2_does_not_overwrite_v3_to_v5():
    """Branching out from v2 creates a new child version without overwriting v3, v4, v5."""
    img_id = create_dummy_image("1girl, base")

    # Create v1, v2, v3, v4, v5
    for i in range(1, 6):
        VersionManager.create_version(
            image_id=img_id,
            positive_prompt=f"1girl, base, tag_{i}",
            instruction=f"Step {i}",
        )

    versions_before = VersionManager.get_versions(img_id)
    assert len(versions_before) == 6
    v2_id = versions_before[2]["id"]
    v3_id = versions_before[3]["id"]
    v4_id = versions_before[4]["id"]
    v5_id = versions_before[5]["id"]

    # Now branch from v2
    branched_res = VersionManager.create_version(
        image_id=img_id,
        positive_prompt="1girl, base, tag_2, branched_new_clothing",
        instruction="Branching from v2 with new clothing",
        parent_version_id=v2_id,
    )
    assert branched_res["success"] is True
    assert branched_res["version_number"] == 6
    assert branched_res["parent_version_id"] == v2_id

    # Verify v3, v4, v5 still exist completely unchanged!
    versions_after = VersionManager.get_versions(img_id)
    assert len(versions_after) == 7  # v0, v1, v2, v3, v4, v5, v6

    ids_after = [v["id"] for v in versions_after]
    assert v3_id in ids_after
    assert v4_id in ids_after
    assert v5_id in ids_after
    assert branched_res["id"] in ids_after


def test_4_switch_version_updates_working_prompt():
    """Switching active working version updates output_images without touching PNG or destroying versions."""
    img_id = create_dummy_image("1girl, v0_original_prompt")

    v1 = VersionManager.create_version(img_id, "1girl, v1_prompt", instruction="v1")
    v2 = VersionManager.create_version(img_id, "1girl, v2_prompt", instruction="v2")

    # Switch back to v1
    res1 = VersionManager.set_current_version(img_id, v1["id"])
    assert res1["success"] is True
    assert res1["current_version_id"] == v1["id"]
    assert res1["positive_prompt"] == "1girl, v1_prompt"

    with connect() as conn:
        row = conn.execute("SELECT positive_prompt, original_positive_prompt, current_prompt_version_id FROM output_images WHERE id = ?", (img_id,)).fetchone()
        assert row["positive_prompt"] == "1girl, v1_prompt"
        assert row["original_positive_prompt"] == "1girl, v0_original_prompt"
        assert row["current_prompt_version_id"] == v1["id"]

    # Restore original v0
    restore_res = VersionManager.restore_original(img_id)
    assert restore_res["success"] is True
    assert restore_res["is_original"] is True
    assert restore_res["positive_prompt"] == "1girl, v0_original_prompt"

    with connect() as conn:
        row = conn.execute("SELECT positive_prompt, current_prompt_version_id FROM output_images WHERE id = ?", (img_id,)).fetchone()
        assert row["positive_prompt"] == "1girl, v0_original_prompt"


def test_5_delete_normal_version_does_not_affect_v0():
    """Deleting a user version removes only that version and protects v0."""
    img_id = create_dummy_image("1girl, start")
    v1 = VersionManager.create_version(img_id, "1girl, start, v1", instruction="v1")
    v2 = VersionManager.create_version(img_id, "1girl, start, v2", instruction="v2")

    # Delete v2
    del_res = VersionManager.delete_version(v2["id"])
    assert del_res["success"] is True

    versions = VersionManager.get_versions(img_id)
    assert len(versions) == 2  # v0 and v1
    assert versions[0]["version_number"] == 0
    assert versions[0]["is_original"] is True
    assert versions[1]["id"] == v1["id"]

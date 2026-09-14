from __future__ import annotations

import pytest
from typing import List, Optional

from tag_manager.db import connect, init_db
from tag_manager.prompt_editor.classifier import classify_tag_scope
from tag_manager.prompt_editor.conflict_engine import ConflictEngine
from tag_manager.prompt_editor.diff import DiffEngine
from tag_manager.prompt_editor.history import HistoryManager
from tag_manager.prompt_editor.identity_guard import IdentityGuard
from tag_manager.prompt_editor.llm.base import PromptEditorLLMBase
from tag_manager.prompt_editor.normalizer import canonical_tag, normalize_tag, resolve_alias
from tag_manager.prompt_editor.parser import parse_prompt, reconstruct_prompt
from tag_manager.prompt_editor.safety import SafetyGuard
from tag_manager.prompt_editor.schemas import (
    LLMRawEditOutput,
    PromptEditApplyRequest,
    PromptEditRequest,
    PromptEditUndoRequest,
)
from tag_manager.prompt_editor.service import PromptEditorService


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_surgeon.sqlite3"
    monkeypatch.setattr("tag_manager.db.DB_PATH", test_db)
    init_db(test_db)
    return test_db



class MockLLMAdapter(PromptEditorLLMBase):
    def __init__(self, raw_output_map=None):
        self.raw_output_map = raw_output_map or {}

    def edit_prompt(
        self,
        prompt: str,
        instruction: str,
        locked_tags: Optional[List[str]] = None,
        allowed_scopes: Optional[List[str]] = None,
        preserve_identity: bool = True,
        age_status: str = "unknown",
        adult_mode: bool = False,
    ) -> LLMRawEditOutput:
        for key, output in self.raw_output_map.items():
            if key in instruction:
                return output
        # Default fallback mock
        return LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.9,
            edit_scopes=[],
            remove_tags=[],
            add_tags=[],
            locked_tags=locked_tags or [],
        )


@pytest.fixture(autouse=True)
def setup_test_database(tmp_path, monkeypatch):
    test_db = tmp_path / "test_prompt_surgeon.sqlite3"
    monkeypatch.setattr("tag_manager.db.DB_PATH", test_db)
    init_db(test_db)
    return test_db


def test_scene_1_hair_length():
    """Scene 1: Change long hair to short hair, strictly preserving all other attributes."""
    mock_llm = MockLLMAdapter({
        "短发": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.98,
            edit_scopes=["hair"],
            remove_tags=["long hair"],
            add_tags=["short hair"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "1girl, blonde hair, long hair, blue eyes, red jacket, skirt, boots, standing, outdoors"
    req = PromptEditRequest(
        prompt=orig,
        instruction="只把长发改成短发",
    )
    res = service.preview_edit(req)
    assert res.success is True
    assert "short hair" in res.diff_summary["added"]
    assert "long hair" in res.diff_summary["removed"]
    # Check that other attributes are strictly kept
    for kept in ["blonde hair", "blue eyes", "red jacket", "skirt", "boots", "standing", "outdoors"]:
        assert kept in res.diff_summary["kept"]
    assert "short hair" in res.edited_prompt
    assert "long hair" not in res.edited_prompt


def test_scene_2_hair_color_mutual_exclusion():
    """Scene 2: Change blonde hair to silver hair; mutually exclusive colors must not coexist."""
    mock_llm = MockLLMAdapter({
        "银色": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.98,
            edit_scopes=["hair"],
            remove_tags=["blonde hair"],
            add_tags=["silver hair"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "1girl, blonde hair, blue eyes"
    req = PromptEditRequest(prompt=orig, instruction="把头发改成银色")
    res = service.preview_edit(req)

    assert "silver hair" in res.diff_summary["added"]
    assert "blonde hair" in res.diff_summary["removed"]
    assert "silver hair" in res.edited_prompt
    assert "blonde hair" not in res.edited_prompt


def test_scene_3_pose_and_scene():
    """Scene 3: Change standing, outdoors, street to sitting, classroom, indoors."""
    mock_llm = MockLLMAdapter({
        "教室": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.95,
            edit_scopes=["pose", "background"],
            remove_tags=["standing", "outdoors", "street"],
            add_tags=["sitting", "classroom"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "standing, outdoors, street"
    req = PromptEditRequest(prompt=orig, instruction="让她坐在教室里")
    res = service.preview_edit(req)

    assert "standing" in res.diff_summary["removed"]
    assert "outdoors" in res.diff_summary["removed"]
    assert "sitting" in res.diff_summary["added"]
    assert "classroom" in res.diff_summary["added"]
    assert "indoors" in res.diff_summary["added"]


def test_scene_4_locked_tags_pin():
    """Scene 4: Pinned tags cannot be removed even if instruction demands it."""
    mock_llm = MockLLMAdapter({
        "红发": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.95,
            edit_scopes=["hair"],
            remove_tags=["blonde hair"],
            add_tags=["red hair"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "blonde hair, blue eyes, cat ears, school uniform"
    req = PromptEditRequest(
        prompt=orig,
        instruction="换成红发",
        locked_tags=["blonde hair", "blue eyes", "cat ears"],
    )
    res = service.preview_edit(req)

    # Locked tag conflict warning must be recorded
    assert any("locked_tag_conflict" in w for w in res.warnings)
    # Locked tag must NOT be in removed
    assert "blonde hair" not in res.diff_summary["removed"]
    assert "blonde hair" in res.diff_summary["locked"]


def test_scene_5_scope_isolation():
    """Scene 5: Changing background must not affect hair, clothing, or character."""
    mock_llm = MockLLMAdapter({
        "海边": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.95,
            edit_scopes=["background"],
            remove_tags=["classroom", "blonde hair"],  # LLM mistakenly attempted to remove hair
            add_tags=["beach"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "1girl, blonde hair, school uniform, classroom"
    req = PromptEditRequest(
        prompt=orig,
        instruction="只换背景成海边",
        allowed_scopes=["background"],
    )
    res = service.preview_edit(req)

    # Blonde hair must NOT be removed because scope is restricted to background
    assert "blonde hair" not in res.diff_summary["removed"]
    assert "blonde hair" in res.diff_summary["kept"]
    assert "classroom" in res.diff_summary["removed"]
    assert "beach" in res.diff_summary["added"]


def test_scene_6_primary_hairstyle_conflict():
    """Scene 6: Changing to short hair automatically removes incompatible styles like ponytail."""
    mock_llm = MockLLMAdapter({
        "短发": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.95,
            edit_scopes=["hair"],
            remove_tags=["long hair"],  # LLM forgot ponytail, rule engine must catch it
            add_tags=["short hair"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "1girl, long hair, ponytail, blue eyes"
    req = PromptEditRequest(prompt=orig, instruction="剪成短发")
    res = service.preview_edit(req)

    assert "long hair" in res.diff_summary["removed"]
    assert "ponytail" in res.diff_summary["removed"]
    assert "short hair" in res.diff_summary["added"]


def test_scene_7_minimal_modification_principle():
    """Scene 7: Changing clothes must preserve eye color and identity."""
    mock_llm = MockLLMAdapter({
        "换衣服": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.95,
            edit_scopes=["clothing"],
            remove_tags=["red dress"],
            add_tags=["white sweater"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "1girl, blue eyes, red dress"
    req = PromptEditRequest(prompt=orig, instruction="换衣服成白毛衣")
    res = service.preview_edit(req)

    assert "blue eyes" in res.diff_summary["kept"]
    assert "1girl" in res.diff_summary["kept"]
    assert "red dress" in res.diff_summary["removed"]
    assert "white sweater" in res.diff_summary["added"]


def test_scene_8_safety_restriction_revoked():
    """Scene 8: Safety restriction has been revoked, edits are allowed without unsafe_request."""
    mock_llm = MockLLMAdapter({
        "脱掉衣服全裸": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.9,
            edit_scopes=["clothing"],
            add_tags=["nude"],
            remove_tags=["dress"],
            locked_tags=[],
            user_notes="removed clothes",
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "1girl, loli, blonde hair, dress"
    req = PromptEditRequest(
        prompt=orig,
        instruction="脱掉衣服全裸",
        age_status="minor",
        adult_mode=True,
    )
    res = service.preview_edit(req)

    assert res.safe is True
    assert res.error is None
    assert res.success is True
    assert "nude" in res.diff_summary["added"]


def test_scene_9_ast_weight_preservation_and_lora():
    """Scene 9: Weights, nested brackets, LoRAs, and BREAK must be preserved."""
    mock_llm = MockLLMAdapter({
        "教室": LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.95,
            edit_scopes=["background"],
            remove_tags=["street"],
            add_tags=["classroom"],
        )
    })
    service = PromptEditorService(llm_adapter=mock_llm)
    orig = "1girl, (blonde hair:1.15), [short hair], <lora:detail:0.8>, BREAK, street"
    req = PromptEditRequest(prompt=orig, instruction="把街道换成教室")
    res = service.preview_edit(req)

    assert "(blonde hair:1.15)" in res.edited_prompt
    assert "[short hair]" in res.edited_prompt
    assert "<lora:detail:0.8>" in res.edited_prompt
    assert "BREAK" in res.edited_prompt
    assert "classroom" in res.edited_prompt
    assert "street" not in res.edited_prompt


def test_scene_10_apply_optimistic_locking_and_undo():
    """Scene 10: Apply with version conflict checks, and undo rollbacks."""
    mock_llm = MockLLMAdapter()
    service = PromptEditorService(llm_adapter=mock_llm)

    import uuid
    unique_path = f"test/{uuid.uuid4().hex}.png"
    # Insert a dummy record into output_images
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO output_images (
                rel_path, filename, file_date, positive_prompt, prompt_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (unique_path, "img1.png", "2026-09-14", "1girl, blonde hair, standing", 0),
        )
        image_id = cursor.lastrowid

    # 1. Apply edit with correct prompt_version (0)
    apply_req = PromptEditApplyRequest(
        image_id=image_id,
        edited_prompt="1girl, blonde hair, sitting",
        prompt_version=0,
        instruction="换成坐着",
    )
    apply_res = service.apply_edit(apply_req)
    assert apply_res["success"] is True
    assert apply_res["new_version"] == 1

    # Check output_images in DB
    with connect() as conn:
        row = conn.execute("SELECT positive_prompt, prompt_version FROM output_images WHERE id = ?", (image_id,)).fetchone()
        assert row["positive_prompt"] == "1girl, blonde hair, sitting"
        assert row["prompt_version"] == 1

    # 2. Stale prompt_version (0) must trigger optimistic lock failure (409)
    with pytest.raises(RuntimeError) as exc_info:
        service.apply_edit(apply_req)
    assert "409" in str(exc_info.value) or "prompt_changed" in str(exc_info.value)

    # 3. Undo edit
    undo_res = service.undo_edit(PromptEditUndoRequest(image_id=image_id))
    assert undo_res["success"] is True
    assert undo_res["restored_prompt"] == "1girl, blonde hair, standing"

    # Check output_images restored
    with connect() as conn:
        row = conn.execute("SELECT positive_prompt, prompt_version FROM output_images WHERE id = ?", (image_id,)).fetchone()
        assert row["positive_prompt"] == "1girl, blonde hair, standing"
        assert row["prompt_version"] == 0  # switched back to v0 without bumping new version

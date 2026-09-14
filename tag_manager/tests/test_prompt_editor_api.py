from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tag_manager.app import app
from tag_manager.db import connect, init_db
from tag_manager.prompt_editor.llm.base import PromptEditorLLMBase
from tag_manager.prompt_editor.schemas import LLMRawEditOutput
from tag_manager.prompt_editor import routes


class DummyApiLLM(PromptEditorLLMBase):
    def edit_prompt(self, prompt, instruction, locked_tags=None, allowed_scopes=None, preserve_identity=True, age_status="unknown", adult_mode=False):
        if "短发" in instruction:
            return LLMRawEditOutput(
                intent="edit_prompt",
                confidence=0.95,
                edit_scopes=["hair"],
                remove_tags=["long hair"],
                add_tags=["short hair"],
            )
        return LLMRawEditOutput(
            intent="edit_prompt",
            confidence=0.9,
            edit_scopes=[],
            remove_tags=[],
            add_tags=[],
        )


@pytest.fixture(autouse=True)
def setup_api_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_api.sqlite3"
    monkeypatch.setattr("tag_manager.db.DB_PATH", test_db)
    init_db(test_db)
    # Monkeypatch the service's LLM in routes
    routes._service.llm = DummyApiLLM()
    return test_db


def test_api_preview_and_apply_flow():
    client = TestClient(app)

    import uuid
    uid = uuid.uuid4().hex[:8]
    rel_path = f"2026-09-14/demo_{uid}.png"
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO output_images (
                rel_path, filename, file_date, positive_prompt, prompt_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (rel_path, f"demo_{uid}.png", "2026-09-14", "1girl, long hair, blue eyes", 0),
        )
        img_id = cur.lastrowid

    # 2. Preview edit
    resp_prev = client.post(
        "/api/prompt/edit/preview",
        json={
            "image_id": img_id,
            "prompt": "1girl, long hair, blue eyes",
            "instruction": "换成短发",
            "prompt_version": 0,
        },
    )
    assert resp_prev.status_code == 200
    data_prev = resp_prev.json()
    assert data_prev["success"] is True
    assert "short hair" in data_prev["diff_summary"]["added"]
    assert "long hair" in data_prev["diff_summary"]["removed"]
    assert data_prev["prompt_version"] == 0

    # 3. Apply edit with correct prompt_version
    resp_apply = client.post(
        "/api/prompt/edit/apply",
        json={
            "image_id": img_id,
            "edited_prompt": data_prev["edited_prompt"],
            "instruction": "换成短发",
            "diff_summary": data_prev["diff_summary"],
            "prompt_version": 0,
        },
    )
    assert resp_apply.status_code == 200
    data_apply = resp_apply.json()
    assert data_apply["success"] is True
    assert data_apply["new_version"] == 1

    # 4. Check edit history
    resp_hist = client.get(f"/api/prompt/edit/history/{img_id}")
    assert resp_hist.status_code == 200
    hist_list = resp_hist.json()
    assert len(hist_list) == 1
    assert hist_list[0]["instruction"] == "换成短发"
    assert hist_list[0]["version"] == 1

    # 5. Test optimistic locking conflict (version mismatch)
    resp_conflict = client.post(
        "/api/prompt/edit/apply",
        json={
            "image_id": img_id,
            "edited_prompt": "1girl, silver hair",
            "instruction": "换成银发",
            "prompt_version": 0,  # Stale version! Current is 1
        },
    )
    assert resp_conflict.status_code == 409

    # 6. Test Undo
    resp_undo = client.post(
        "/api/prompt/edit/undo",
        json={"image_id": img_id},
    )
    assert resp_undo.status_code == 200
    data_undo = resp_undo.json()
    assert data_undo["success"] is True
    assert data_undo["restored_prompt"] == "1girl, long hair, blue eyes"

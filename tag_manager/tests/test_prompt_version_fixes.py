import uuid
import pytest
from starlette.testclient import TestClient

from tag_manager.app import app
from tag_manager.db import connect, init_db
from tag_manager.prompt_editor.version_manager import VersionManager


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_version_fixes.sqlite3"
    monkeypatch.setattr("tag_manager.db.DB_PATH", test_db)
    init_db(test_db)
    return test_db


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def test_image():
    """Create an isolated test image row with v0 Original."""
    unique_path = f"test_ver_fixes_{uuid.uuid4().hex[:8]}.png"
    orig_pos = "masterpiece, best quality, 1girl, black hair, serafuku, outdoors"
    orig_neg = "lowres, bad anatomy"

    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO output_images (
                rel_path, filename, file_date,
                positive_prompt, negative_prompt,
                original_positive_prompt, original_negative_prompt,
                prompt_version
            ) VALUES (?, 'test.png', '2026-09-14', ?, ?, ?, ?, 0)
            """,
            (unique_path, orig_pos, orig_neg, orig_pos, orig_neg),
        )
        img_id = cursor.lastrowid

        # Insert v0 Original
        v0_cur = conn.execute(
            """
            INSERT INTO prompt_versions (
                image_id, version_number, parent_version_id,
                positive_prompt, negative_prompt, instruction,
                is_original, applied_by
            ) VALUES (?, 0, NULL, ?, ?, 'Original from metadata', 1, 'original')
            """,
            (img_id, orig_pos, orig_neg),
        )
        v0_id = v0_cur.lastrowid
        conn.execute(
            "UPDATE output_images SET current_prompt_version_id = ?, prompt_version = 0 WHERE id = ?",
            (v0_id, img_id),
        )

    yield {"image_id": img_id, "v0_id": v0_id, "orig_pos": orig_pos, "orig_neg": orig_neg}

    # Cleanup
    with connect() as conn:
        conn.execute("DELETE FROM prompt_versions WHERE image_id = ?", (img_id,))
        conn.execute("DELETE FROM output_images WHERE id = ?", (img_id,))


def test_1_new_image_initialization(client, test_image):
    """Test 1: 新图片初始化仅有 v0 Original，版本数 = 1"""
    img_id = test_image["image_id"]

    resp = client.get(f"/api/prompt/versions/{img_id}")
    assert resp.status_code == 200
    versions = resp.json()
    assert len(versions) == 1, f"Expected 1 version, got {len(versions)}"

    v0 = versions[0]
    assert v0["version_number"] == 0, f"Expected version_number 0, got {v0['version_number']}"
    assert v0["is_original"] is True
    assert v0["is_current"] is True

    # 验证 output_images 表
    with connect() as conn:
        img = conn.execute("SELECT prompt_version, current_prompt_version_id FROM output_images WHERE id = ?", (img_id,)).fetchone()
        assert img["prompt_version"] == 0
        assert img["current_prompt_version_id"] == v0["id"]


def test_2_first_apply_from_v0_creates_v1(client, test_image):
    """Test 2: 从 v0 Apply 一次 -> v0 Original + v1 Edit A，版本数 = 2，current = v1"""
    img_id = test_image["image_id"]
    v0_id = test_image["v0_id"]

    edit_a_prompt = test_image["orig_pos"] + ", short hair"
    apply_resp = client.post(
        "/api/prompt/versions/apply",
        json={
            "image_id": img_id,
            "positive_prompt": edit_a_prompt,
            "instruction": "Edit A",
            "parent_version_id": v0_id,
            "model": "deepseek",
        },
    )
    assert apply_resp.status_code == 200
    applied_data = apply_resp.json()
    assert applied_data["version_number"] == 1, f"First edit must be v1, got {applied_data['version_number']}"
    assert applied_data["parent_version_id"] == v0_id
    assert applied_data["is_current"] is True

    # 查版本列表
    v_resp = client.get(f"/api/prompt/versions/{img_id}")
    assert v_resp.status_code == 200
    versions = v_resp.json()
    assert len(versions) == 2, f"Expected 2 versions, got {len(versions)}"
    assert versions[0]["version_number"] == 0 and versions[0]["is_original"] is True
    assert versions[1]["version_number"] == 1 and versions[1]["instruction"] == "Edit A"
    assert versions[1]["is_current"] is True


def test_3_rollback_to_v0_does_not_create_new_version(client, test_image):
    """Test 3: 点击‘回滚至 v0’ -> 版本数仍然 = 2，current = v0，不能新增任何记录"""
    img_id = test_image["image_id"]
    v0_id = test_image["v0_id"]

    # 先做一次编辑生成 v1
    client.post(
        "/api/prompt/versions/apply",
        json={
            "image_id": img_id,
            "positive_prompt": test_image["orig_pos"] + ", short hair",
            "instruction": "Edit A",
            "parent_version_id": v0_id,
        },
    )

    # 记录当前版本数
    with connect() as conn:
        count_before = conn.execute("SELECT COUNT(*) FROM prompt_versions WHERE image_id = ?", (img_id,)).fetchone()[0]
    assert count_before == 2

    # 执行回滚至 v0
    set_resp = client.post(
        "/api/prompt/versions/set_current",
        json={"image_id": img_id, "version_id": v0_id},
    )
    assert set_resp.status_code == 200
    data = set_resp.json()
    assert data["success"] is True
    assert data["version_number"] == 0
    assert data["is_original"] is True

    # 验证版本数仍然是 2，没有创建任何 Undo to version X 记录！
    with connect() as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM prompt_versions WHERE image_id = ?", (img_id,)).fetchone()[0]
        undo_rows = conn.execute("SELECT * FROM prompt_versions WHERE image_id = ? AND instruction LIKE '%Undo%'", (img_id,)).fetchall()
        img_row = conn.execute("SELECT prompt_version, current_prompt_version_id FROM output_images WHERE id = ?", (img_id,)).fetchone()

    assert count_after == 2, f"Version count must remain 2, but became {count_after}"
    assert len(undo_rows) == 0, "Must not create any Undo records!"
    assert img_row["prompt_version"] == 0
    assert img_row["current_prompt_version_id"] == v0_id


def test_4_branching_from_v0_creates_v2(client, test_image):
    """Test 4: 从 v0 再 Apply Edit B -> v0 ├─ v1 Edit A, └─ v2 Edit B，v2.parent = v0.id，current = v2"""
    img_id = test_image["image_id"]
    v0_id = test_image["v0_id"]

    # 1. Edit A (v1)
    client.post(
        "/api/prompt/versions/apply",
        json={
            "image_id": img_id,
            "positive_prompt": test_image["orig_pos"] + ", short hair",
            "instruction": "Edit A",
            "parent_version_id": v0_id,
        },
    )

    # 2. 回滚到 v0
    client.post(
        "/api/prompt/versions/set_current",
        json={"image_id": img_id, "version_id": v0_id},
    )

    # 3. 从 v0 再 Apply Edit B
    b_resp = client.post(
        "/api/prompt/versions/apply",
        json={
            "image_id": img_id,
            "positive_prompt": test_image["orig_pos"] + ", long hair, dress",
            "instruction": "Edit B",
            "parent_version_id": v0_id,
        },
    )
    assert b_resp.status_code == 200
    b_data = b_resp.json()
    assert b_data["version_number"] == 2, f"Edit B should be v2, got {b_data['version_number']}"
    assert b_data["parent_version_id"] == v0_id
    assert b_data["is_current"] is True

    # 验证版本树
    v_resp = client.get(f"/api/prompt/versions/{img_id}")
    versions = v_resp.json()
    assert len(versions) == 3
    # v0, v1, v2
    assert [v["version_number"] for v in versions] == [0, 1, 2]
    # v1.parent == v0.id and v2.parent == v0.id
    assert versions[1]["parent_version_id"] == v0_id
    assert versions[2]["parent_version_id"] == v0_id
    assert versions[2]["is_current"] is True


def test_5_twenty_switches_do_not_increase_rows(client, test_image):
    """Test 5: 连续在 v0/v1/v2 之间切换 20 次，prompt_versions 表行数完全不增加"""
    img_id = test_image["image_id"]
    v0_id = test_image["v0_id"]

    # 创建 v1
    v1_res = client.post(
        "/api/prompt/versions/apply",
        json={"image_id": img_id, "positive_prompt": "prompt 1", "instruction": "v1", "parent_version_id": v0_id},
    ).json()
    v1_id = v1_res["id"]

    # 创建 v2
    v2_res = client.post(
        "/api/prompt/versions/apply",
        json={"image_id": img_id, "positive_prompt": "prompt 2", "instruction": "v2", "parent_version_id": v1_id},
    ).json()
    v2_id = v2_res["id"]

    with connect() as conn:
        initial_count = conn.execute("SELECT COUNT(*) FROM prompt_versions WHERE image_id = ?", (img_id,)).fetchone()[0]
    assert initial_count == 3

    # 连续切换 20 次
    targets = [v0_id, v1_id, v2_id]
    for i in range(20):
        target_id = targets[i % 3]
        resp = client.post(
            "/api/prompt/versions/set_current",
            json={"image_id": img_id, "version_id": target_id},
        )
        assert resp.status_code == 200

    with connect() as conn:
        final_count = conn.execute("SELECT COUNT(*) FROM prompt_versions WHERE image_id = ?", (img_id,)).fetchone()[0]

    assert final_count == 3, f"Expected row count to remain 3 after 20 switches, got {final_count}"


def test_6_v0_cannot_be_deleted(client, test_image):
    """Test 6: v0 无法删除，调用 DELETE API 返回 403 Forbidden"""
    v0_id = test_image["v0_id"]
    resp = client.delete(f"/api/prompt/versions/{v0_id}")
    assert resp.status_code == 403
    assert "v0 Original" in resp.json()["detail"]


def test_7_enter_surgeon_studio_from_any_version(client, test_image):
    """Test 7: 进入手术室，从 v0、v1、v2 分别点击‘进入手术室’，全部能够正常打开返回 200"""
    img_id = test_image["image_id"]
    v0_id = test_image["v0_id"]

    # 创建 v1
    v1_id = client.post(
        "/api/prompt/versions/apply",
        json={"image_id": img_id, "positive_prompt": "prompt 1", "instruction": "v1", "parent_version_id": v0_id},
    ).json()["id"]

    # 创建 v2
    v2_id = client.post(
        "/api/prompt/versions/apply",
        json={"image_id": img_id, "positive_prompt": "prompt 2", "instruction": "v2", "parent_version_id": v1_id},
    ).json()["id"]

    # 1. 默认无参数访问（加载当前版本）
    r1 = client.get(f"/prompt-surgeon/{img_id}")
    assert r1.status_code == 200
    assert "Prompt Surgeon 独立手术室" in r1.text
    assert f"window.SURGEON_IMAGE" in r1.text

    # 2. 指定 version_id = v0
    r_v0 = client.get(f"/prompt-surgeon/{img_id}?version_id={v0_id}")
    assert r_v0.status_code == 200
    assert "v0 原版" in r_v0.text or "v0 · Original" in r_v0.text

    # 3. 指定 version_id = v1
    r_v1 = client.get(f"/prompt-surgeon/{img_id}?version_id={v1_id}")
    assert r_v1.status_code == 200

    # 4. 指定 version_id = v2
    r_v2 = client.get(f"/prompt-surgeon/{img_id}?version_id={v2_id}")
    assert r_v2.status_code == 200


def test_8_persistence_and_refresh_state(client, test_image):
    """Test 8: 刷新页面 / 重启程序，current version、版本树和 parent 关系保持正确"""
    img_id = test_image["image_id"]
    v0_id = test_image["v0_id"]

    # 创建 v1
    v1_id = client.post(
        "/api/prompt/versions/apply",
        json={"image_id": img_id, "positive_prompt": "prompt v1", "instruction": "Edit 1", "parent_version_id": v0_id},
    ).json()["id"]

    # 创建 v2 (基于 v0 分支)
    v2_id = client.post(
        "/api/prompt/versions/apply",
        json={"image_id": img_id, "positive_prompt": "prompt v2", "instruction": "Edit 2", "parent_version_id": v0_id},
    ).json()["id"]

    # 设为当前为 v1
    client.post(
        "/api/prompt/versions/set_current",
        json={"image_id": img_id, "version_id": v1_id},
    )

    # 模拟重新连接数据库与获取详情
    with connect() as conn:
        img_row = conn.execute("SELECT * FROM output_images WHERE id = ?", (img_id,)).fetchone()
        assert img_row["prompt_version"] == 1
        assert img_row["current_prompt_version_id"] == v1_id
        assert img_row["positive_prompt"] == "prompt v1"

        versions = conn.execute("SELECT * FROM prompt_versions WHERE image_id = ? ORDER BY version_number ASC", (img_id,)).fetchall()
        assert len(versions) == 3
        # v0: parent=None
        assert versions[0]["version_number"] == 0 and versions[0]["parent_version_id"] is None
        # v1: parent=v0_id
        assert versions[1]["version_number"] == 1 and versions[1]["parent_version_id"] == v0_id
        # v2: parent=v0_id
        assert versions[2]["version_number"] == 2 and versions[2]["parent_version_id"] == v0_id

    # 模拟客户端再次 GET /api/prompt/versions/{image_id}
    v_resp = client.get(f"/api/prompt/versions/{img_id}").json()
    assert v_resp[1]["is_current"] is True
    assert v_resp[0]["is_current"] is False
    assert v_resp[2]["is_current"] is False

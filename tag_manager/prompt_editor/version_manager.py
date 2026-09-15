from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
import sqlite3

from tag_manager.db import connect

logger = logging.getLogger(__name__)


class VersionManager:
    """
    Manages non-destructive prompt versions, branching trees, and original metadata protection.
    v0 Original is strictly immutable and protected.
    """

    @staticmethod
    def get_versions(image_id: int) -> List[Dict[str, Any]]:
        with connect() as conn:
            img = conn.execute(
                "SELECT id, current_prompt_version_id, prompt_version, positive_prompt FROM output_images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if not img:
                return []

            current_v_id = img["current_prompt_version_id"]

            rows = conn.execute(
                """
                SELECT id, image_id, version_number, parent_version_id,
                       positive_prompt, negative_prompt, instruction,
                       remove_tags_json, add_tags_json, locked_tags_json, diff_json,
                       model, applied_by, is_original, created_at
                FROM prompt_versions
                WHERE image_id = ?
                ORDER BY version_number ASC, id ASC
                """,
                (image_id,),
            ).fetchall()

            result = []
            for r in rows:
                rem_tags = []
                try:
                    rem_tags = json.loads(r["remove_tags_json"] or "[]")
                except Exception:
                    pass

                add_tags = []
                try:
                    add_tags = json.loads(r["add_tags_json"] or "[]")
                except Exception:
                    pass

                locked_tags = []
                try:
                    locked_tags = json.loads(r["locked_tags_json"] or "[]")
                except Exception:
                    pass

                diff_data = {}
                try:
                    diff_data = json.loads(r["diff_json"] or "{}")
                except Exception:
                    pass

                is_curr = (r["id"] == current_v_id) if current_v_id else (r["version_number"] == 0)

                result.append({
                    "id": r["id"],
                    "image_id": r["image_id"],
                    "version_number": r["version_number"],
                    "parent_version_id": r["parent_version_id"],
                    "positive_prompt": r["positive_prompt"],
                    "negative_prompt": r["negative_prompt"] or "",
                    "instruction": r["instruction"] or "",
                    "remove_tags": rem_tags,
                    "add_tags": add_tags,
                    "locked_tags": locked_tags,
                    "diff_json": diff_data,
                    "model": r["model"] or "",
                    "applied_by": r["applied_by"] or "user",
                    "is_original": bool(r["is_original"]),
                    "is_current": is_curr,
                    "created_at": str(r["created_at"] or ""),
                })
            return result

    @staticmethod
    def create_version(
        image_id: int,
        positive_prompt: str,
        negative_prompt: str = "",
        instruction: str = "",
        parent_version_id: Optional[int] = None,
        remove_tags: Optional[List[str]] = None,
        add_tags: Optional[List[str]] = None,
        locked_tags: Optional[List[str]] = None,
        diff_json: Optional[Dict[str, Any]] = None,
        model: str = "deepseek",
        applied_by: str = "user",
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        def _do_create(c: sqlite3.Connection) -> Dict[str, Any]:
            img = c.execute(
                "SELECT id, current_prompt_version_id, prompt_version, positive_prompt, negative_prompt, original_positive_prompt, original_negative_prompt FROM output_images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if not img:
                raise ValueError(f"Image ID {image_id} not found")

            # Ensure v0 exists
            v0 = c.execute(
                "SELECT id FROM prompt_versions WHERE image_id = ? AND version_number = 0",
                (image_id,),
            ).fetchone()
            if not v0:
                orig_pos = img["original_positive_prompt"] or img["positive_prompt"] or ""
                orig_neg = img["original_negative_prompt"] or img["negative_prompt"] or ""
                v0_cur = c.execute(
                    """
                    INSERT INTO prompt_versions (
                        image_id, version_number, parent_version_id,
                        positive_prompt, negative_prompt, instruction,
                        is_original, applied_by
                    ) VALUES (?, 0, NULL, ?, ?, 'Original from metadata', 1, 'original')
                    """,
                    (image_id, orig_pos, orig_neg),
                )
                actual_parent_id = v0_cur.lastrowid
            else:
                actual_parent_id = parent_version_id or img["current_prompt_version_id"] or v0["id"]

            # Determine next version number: strictly max_v + 1
            max_row = c.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS max_v FROM prompt_versions WHERE image_id = ?",
                (image_id,),
            ).fetchone()
            max_v = max_row["max_v"] if max_row else 0
            next_version = max_v + 1

            neg_prompt = negative_prompt or (img["original_negative_prompt"] or "")

            cursor = c.execute(
                """
                INSERT INTO prompt_versions (
                    image_id, version_number, parent_version_id,
                    positive_prompt, negative_prompt, instruction,
                    remove_tags_json, add_tags_json, locked_tags_json, diff_json,
                    model, applied_by, is_original, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                """,
                (
                    image_id,
                    next_version,
                    actual_parent_id,
                    positive_prompt,
                    neg_prompt,
                    instruction,
                    json.dumps(remove_tags or []),
                    json.dumps(add_tags or []),
                    json.dumps(locked_tags or []),
                    json.dumps(diff_json or {}),
                    model,
                    applied_by,
                ),
            )
            new_id = cursor.lastrowid

            # Update working prompt in output_images
            c.execute(
                """
                UPDATE output_images
                SET current_prompt_version_id = ?, positive_prompt = ?, negative_prompt = ?, prompt_version = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_id, positive_prompt, negative_prompt or "", next_version, image_id),
            )

            return {
                "success": True,
                "id": new_id,
                "image_id": image_id,
                "version_number": next_version,
                "parent_version_id": actual_parent_id,
                "positive_prompt": positive_prompt,
                "negative_prompt": negative_prompt or "",
                "instruction": instruction,
                "is_current": True,
            }

        if conn is not None:
            return _do_create(conn)
        else:
            with connect() as c:
                return _do_create(c)

    @staticmethod
    def set_current_version(image_id: int, version_id: int) -> Dict[str, Any]:
        with connect() as conn:
            ver = conn.execute(
                "SELECT * FROM prompt_versions WHERE id = ? AND image_id = ?",
                (version_id, image_id),
            ).fetchone()
            if not ver:
                raise ValueError(f"Version ID {version_id} not found for image {image_id}")

            conn.execute(
                """
                UPDATE output_images
                SET current_prompt_version_id = ?, positive_prompt = ?, negative_prompt = ?, prompt_version = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (version_id, ver["positive_prompt"], ver["negative_prompt"] or "", ver["version_number"], image_id),
            )

            return {
                "success": True,
                "image_id": image_id,
                "current_version_id": version_id,
                "version_number": ver["version_number"],
                "positive_prompt": ver["positive_prompt"],
                "is_original": bool(ver["is_original"]),
            }

    @staticmethod
    def delete_version(version_id: int) -> Dict[str, Any]:
        with connect() as conn:
            ver = conn.execute(
                "SELECT id, image_id, version_number, parent_version_id, is_original FROM prompt_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
            if not ver:
                raise ValueError(f"Version ID {version_id} not found")

            if ver["is_original"] or ver["version_number"] == 0:
                raise PermissionError("Cannot delete immutable v0 Original version")

            image_id = ver["image_id"]
            img = conn.execute(
                "SELECT current_prompt_version_id FROM output_images WHERE id = ?",
                (image_id,),
            ).fetchone()

            # If the version being deleted is currently active, switch to parent or fallback
            if img and img["current_prompt_version_id"] == version_id:
                fallback_id = ver["parent_version_id"]
                if not fallback_id:
                    v0 = conn.execute(
                        "SELECT id FROM prompt_versions WHERE image_id = ? AND version_number = 0",
                        (image_id,),
                    ).fetchone()
                    fallback_id = v0["id"] if v0 else None

                if fallback_id:
                    fb_ver = conn.execute(
                        "SELECT * FROM prompt_versions WHERE id = ?",
                        (fallback_id,),
                    ).fetchone()
                    if fb_ver:
                        conn.execute(
                            """
                            UPDATE output_images
                            SET current_prompt_version_id = ?, positive_prompt = ?, prompt_version = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (fallback_id, fb_ver["positive_prompt"], fb_ver["version_number"], image_id),
                        )

            conn.execute("DELETE FROM prompt_versions WHERE id = ?", (version_id,))
            return {
                "success": True,
                "deleted_version_id": version_id,
                "image_id": image_id,
            }

    @staticmethod
    def restore_original(image_id: int) -> Dict[str, Any]:
        with connect() as conn:
            v0 = conn.execute(
                "SELECT * FROM prompt_versions WHERE image_id = ? AND (is_original = 1 OR version_number = 0) ORDER BY id ASC LIMIT 1",
                (image_id,),
            ).fetchone()
            if not v0:
                raise ValueError(f"No original v0 version found for image {image_id}")

            conn.execute(
                """
                UPDATE output_images
                SET current_prompt_version_id = ?, positive_prompt = ?, negative_prompt = ?, prompt_version = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (v0["id"], v0["positive_prompt"], v0["negative_prompt"] or "", image_id),
            )

            return {
                "success": True,
                "image_id": image_id,
                "current_version_id": v0["id"],
                "version_number": 0,
                "positive_prompt": v0["positive_prompt"],
                "is_original": True,
            }

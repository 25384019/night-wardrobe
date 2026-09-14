from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..db import connect
from .schemas import PromptEditHistoryItem

MAX_HISTORY_PER_IMAGE = 20


class HistoryManager:
    @staticmethod
    def record_history(
        image_id: int,
        version: int,
        instruction: str,
        edit_scopes: List[str],
        positive_prompt_before: str,
        positive_prompt_after: str,
        negative_prompt_before: str = "",
        negative_prompt_after: str = "",
        diff_json: Optional[Dict[str, Any]] = None,
        applied_by: str = "user",
        conn: Optional[Any] = None,
    ) -> int:
        """
        Record a prompt edit entry and prune older history beyond MAX_HISTORY_PER_IMAGE.
        """
        diff_str = json.dumps(diff_json or {}, ensure_ascii=False)
        scopes_str = json.dumps(edit_scopes or [], ensure_ascii=False)

        def _do_insert(c):
            cursor = c.execute(
                """
                INSERT INTO prompt_edit_history (
                    image_id, version, instruction, edit_scopes,
                    positive_prompt_before, positive_prompt_after,
                    negative_prompt_before, negative_prompt_after,
                    diff_json, applied_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    version,
                    instruction,
                    scopes_str,
                    positive_prompt_before,
                    positive_prompt_after,
                    negative_prompt_before,
                    negative_prompt_after,
                    diff_str,
                    applied_by,
                ),
            )
            h_id = cursor.lastrowid
            c.execute(
                """
                DELETE FROM prompt_edit_history
                WHERE image_id = ? AND id NOT IN (
                    SELECT id FROM prompt_edit_history
                    WHERE image_id = ?
                    ORDER BY version DESC, id DESC
                    LIMIT ?
                )
                """,
                (image_id, image_id, MAX_HISTORY_PER_IMAGE),
            )
            return h_id

        if conn is not None:
            return _do_insert(conn)
        else:
            with connect() as c:
                return _do_insert(c)

    @staticmethod
    def get_history(image_id: int) -> List[PromptEditHistoryItem]:
        """
        Retrieve edit history for an image.
        """
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, image_id, version, instruction, edit_scopes,
                       positive_prompt_before, positive_prompt_after,
                       negative_prompt_before, negative_prompt_after,
                       diff_json, applied_by, created_at
                FROM prompt_edit_history
                WHERE image_id = ?
                ORDER BY version DESC, id DESC
                LIMIT ?
                """,
                (image_id, MAX_HISTORY_PER_IMAGE),
            ).fetchall()

            result = []
            for row in rows:
                scopes = []
                try:
                    scopes = json.loads(row["edit_scopes"] or "[]")
                except Exception:
                    pass
                diff_data = {}
                try:
                    diff_data = json.loads(row["diff_json"] or "{}")
                except Exception:
                    pass

                result.append(
                    PromptEditHistoryItem(
                        id=row["id"],
                        image_id=row["image_id"],
                        version=row["version"],
                        instruction=row["instruction"] or "",
                        edit_scopes=scopes,
                        positive_prompt_before=row["positive_prompt_before"] or "",
                        positive_prompt_after=row["positive_prompt_after"] or "",
                        negative_prompt_before=row["negative_prompt_before"] or "",
                        negative_prompt_after=row["negative_prompt_after"] or "",
                        diff_json=diff_data,
                        applied_by=row["applied_by"] or "user",
                        created_at=str(row["created_at"] or ""),
                    )
                )
            return result

    @staticmethod
    def undo(image_id: int, target_version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Roll back to a previous prompt version.
        Returns the restored prompt details, or None if no history exists.
        """
        with connect() as conn:
            # Check current image record
            img = conn.execute(
                "SELECT id, prompt_version, positive_prompt FROM output_images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if not img:
                return None

            current_version = img["prompt_version"]

            # Find target history record
            if target_version is not None:
                record = conn.execute(
                    """
                    SELECT * FROM prompt_edit_history
                    WHERE image_id = ? AND version = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (image_id, target_version),
                ).fetchone()
                if not record:
                    # If target_version refers to the baseline version before an edit (e.g. revert to v1 before v2 edit)
                    record = conn.execute(
                        """
                        SELECT * FROM prompt_edit_history
                        WHERE image_id = ? AND version = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (image_id, target_version + 1),
                    ).fetchone()
            else:
                # Find most recent edit record to undo
                record = conn.execute(
                    """
                    SELECT * FROM prompt_edit_history
                    WHERE image_id = ? AND version <= ? AND applied_by != 'undo'
                    ORDER BY version DESC, id DESC LIMIT 1
                    """,
                    (image_id, current_version),
                ).fetchone()
                if not record:
                    # Fallback to latest history
                    record = conn.execute(
                        """
                        SELECT * FROM prompt_edit_history
                        WHERE image_id = ?
                        ORDER BY version DESC, id DESC LIMIT 1
                        """,
                        (image_id,),
                    ).fetchone()

            if not record:
                return None

            # Roll back positive prompt to positive_prompt_before
            restored_prompt = record["positive_prompt_before"]
            new_version = current_version + 1

            conn.execute(
                """
                UPDATE output_images
                SET positive_prompt = ?, prompt_version = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (restored_prompt, new_version, image_id),
            )

            # Record an undo history entry
            conn.execute(
                """
                INSERT INTO prompt_edit_history (
                    image_id, version, instruction, edit_scopes,
                    positive_prompt_before, positive_prompt_after,
                    diff_json, applied_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    new_version,
                    f"Undo to version {record['version']}",
                    "[]",
                    img["positive_prompt"],
                    restored_prompt,
                    json.dumps({"undo_from_version": current_version, "undo_to_version": record["version"]}),
                    "undo",
                ),
            )

            return {
                "image_id": image_id,
                "previous_version": current_version,
                "restored_version": new_version,
                "restored_prompt": restored_prompt,
            }

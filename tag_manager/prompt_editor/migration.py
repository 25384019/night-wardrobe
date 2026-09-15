from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional
from pathlib import Path

from tag_manager.db import connect

logger = logging.getLogger(__name__)


def migrate_prompt_versions(conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    Safely backfill output_images and migrate prompt_edit_history to prompt_versions.
    Guarantees:
    - True original metadata is extracted from PNG file if available and stored in v0 Original.
    - v0 Original is immutable (is_original=1, version_number=0).
    - Historical edits from prompt_edit_history are seamlessly migrated to prompt_versions.
    - current_prompt_version_id is set to the latest active version.
    - Idempotent: safe to run multiple times without duplicating data.
    """
    def _do_migrate(c: sqlite3.Connection) -> dict:
        from tag_manager.outputs_service import get_configured_output_dir, read_image_metadata, extract_prompts

        root_dir = get_configured_output_dir()
        stats = {"images_migrated": 0, "v0_created": 0, "history_migrated": 0}

        images = c.execute(
            """
            SELECT id, rel_path, positive_prompt, negative_prompt,
                   original_positive_prompt, original_negative_prompt,
                   current_prompt_version_id, created_at
            FROM output_images
            """
        ).fetchall()

        for img in images:
            img_id = img["id"]
            rel_path = img["rel_path"]
            orig_pos = (img["original_positive_prompt"] or "").strip()
            orig_neg = (img["original_negative_prompt"] or "").strip()

            # 1. Determine original metadata prompt
            if not orig_pos:
                disk_file = root_dir / rel_path if root_dir else None
                if disk_file and disk_file.is_file():
                    try:
                        meta = read_image_metadata(disk_file)
                        extracted_pos, extracted_neg, _, _, _, _, _, _, _, _ = extract_prompts(meta)
                        orig_pos = extracted_pos or img["positive_prompt"] or ""
                        orig_neg = extracted_neg or img["negative_prompt"] or ""
                    except Exception as e:
                        logger.warning(f"Failed to read metadata for {rel_path}: {e}")
                        orig_pos = img["positive_prompt"] or ""
                        orig_neg = img["negative_prompt"] or ""
                else:
                    # Fallback to earliest prompt_edit_history entry or current prompt
                    earliest_hist = c.execute(
                        "SELECT positive_prompt_before, negative_prompt_before FROM prompt_edit_history WHERE image_id = ? ORDER BY id ASC LIMIT 1",
                        (img_id,),
                    ).fetchone()
                    if earliest_hist and earliest_hist["positive_prompt_before"]:
                        orig_pos = earliest_hist["positive_prompt_before"]
                        orig_neg = earliest_hist["negative_prompt_before"] or ""
                    else:
                        orig_pos = img["positive_prompt"] or ""
                        orig_neg = img["negative_prompt"] or ""

                c.execute(
                    "UPDATE output_images SET original_positive_prompt = ?, original_negative_prompt = ? WHERE id = ?",
                    (orig_pos, orig_neg, img_id),
                )
                stats["images_migrated"] += 1

            # 2. Ensure v0 Original exists in prompt_versions
            v0 = c.execute(
                "SELECT id FROM prompt_versions WHERE image_id = ? AND version_number = 0",
                (img_id,),
            ).fetchone()

            if not v0:
                cursor = c.execute(
                    """
                    INSERT INTO prompt_versions (
                        image_id, version_number, parent_version_id,
                        positive_prompt, negative_prompt, instruction,
                        remove_tags_json, add_tags_json, locked_tags_json, diff_json,
                        model, applied_by, is_original, created_at
                    ) VALUES (?, 0, NULL, ?, ?, 'Original from metadata', '[]', '[]', '[]', '{}', '', 'original', 1, ?)
                    """,
                    (img_id, orig_pos, orig_neg, img["created_at"]),
                )
                v0_id = cursor.lastrowid
                stats["v0_created"] += 1
            else:
                v0_id = v0["id"]

            # 3. Migrate prompt_edit_history if prompt_versions has no subsequent versions
            non_v0_count = c.execute(
                "SELECT COUNT(*) as cnt FROM prompt_versions WHERE image_id = ? AND version_number > 0",
                (img_id,),
            ).fetchone()["cnt"]

            last_version_id = v0_id
            if non_v0_count == 0:
                hist_rows = c.execute(
                    "SELECT * FROM prompt_edit_history WHERE image_id = ? ORDER BY version ASC, id ASC",
                    (img_id,),
                ).fetchall()

                curr_ver = 0
                for h in hist_rows:
                    curr_ver += 1
                    diff_data = {}
                    try:
                        diff_data = json.loads(h["diff_json"] or "{}")
                    except Exception:
                        pass
                    rem_tags = diff_data.get("removed", [])
                    add_tags = diff_data.get("added", [])
                    lock_tags = diff_data.get("locked", [])

                    cursor = c.execute(
                        """
                        INSERT INTO prompt_versions (
                            image_id, version_number, parent_version_id,
                            positive_prompt, negative_prompt, instruction,
                            remove_tags_json, add_tags_json, locked_tags_json, diff_json,
                            model, applied_by, is_original, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'deepseek', ?, 0, ?)
                        """,
                        (
                            img_id,
                            curr_ver,
                            last_version_id,
                            h["positive_prompt_after"],
                            h["negative_prompt_after"] or orig_neg,
                            h["instruction"] or f"Edit v{curr_ver}",
                            json.dumps(rem_tags),
                            json.dumps(add_tags),
                            json.dumps(lock_tags),
                            h["diff_json"] or "{}",
                            h["applied_by"] or "user",
                            h["created_at"],
                        ),
                    )
                    last_version_id = cursor.lastrowid
                    stats["history_migrated"] += 1

            # 4. Initialize current only when the image has no valid current.
            # A user's explicit branch/rollback selection must survive restart.
            current = img["current_prompt_version_id"]
            if current:
                existing = c.execute(
                    "SELECT 1 FROM prompt_versions WHERE id = ? AND image_id = ?",
                    (current, img_id),
                ).fetchone()
                if existing:
                    continue

            latest_v = c.execute(
                "SELECT id, positive_prompt, version_number FROM prompt_versions WHERE image_id = ? ORDER BY version_number DESC, id DESC LIMIT 1",
                (img_id,),
            ).fetchone()

            if latest_v:
                c.execute(
                    """
                    UPDATE output_images
                    SET current_prompt_version_id = ?, prompt_version = ?
                    WHERE id = ?
                    """,
                    (latest_v["id"], latest_v["version_number"], img_id),
                )

        return stats

    if conn is not None:
        return _do_migrate(conn)
    else:
        with connect() as c:
            return _do_migrate(c)

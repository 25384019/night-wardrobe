from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..db import connect
from .classifier import classify_tag_scope
from .conflict_engine import ConflictEngine
from .diff import DiffEngine
from .history import HistoryManager
from .identity_guard import IdentityGuard
from .llm import DeepSeekAdapter, PromptEditorLLMBase
from .normalizer import canonical_tag, normalize_tag
from .parser import parse_prompt
from .safety import SafetyGuard
from .schemas import (
    PromptEditApplyRequest,
    PromptEditHistoryItem,
    PromptEditPreviewResponse,
    PromptEditRequest,
    PromptEditUndoRequest,
    PromptVersionApplyRequest,
    PromptVersionItem,
    PromptVersionSetCurrentRequest,
)
from .version_manager import VersionManager

logger = logging.getLogger(__name__)


class PromptEditorService:
    def __init__(self, llm_adapter: Optional[PromptEditorLLMBase] = None):
        self.llm = llm_adapter or DeepSeekAdapter()
        self.conflict_engine = ConflictEngine()
        self.identity_guard = IdentityGuard()
        self.diff_engine = DiffEngine()
        self.history_mgr = HistoryManager()

    def preview_edit(self, req: PromptEditRequest) -> PromptEditPreviewResponse:
        """
        Execute the full Prompt Surgeon compilation and conflict-resolution pipeline.
        Produces a preview diff without modifying the database.
        """
        original_prompt = req.prompt.strip()
        instruction = req.instruction.strip()
        warnings: List[str] = []

        # 0. Fetch current DB version and age status if image_id is provided
        current_version = req.prompt_version or 1
        age_status = req.age_status or "unknown"
        if req.image_id:
            try:
                with connect() as conn:
                    row = conn.execute(
                        "SELECT prompt_version, age_status, positive_prompt FROM output_images WHERE id = ?",
                        (req.image_id,),
                    ).fetchone()
                    if row:
                        current_version = row["prompt_version"]
                        if row["age_status"] and row["age_status"] != "unknown":
                            age_status = row["age_status"]
            except Exception as e:
                logger.warning("Failed to fetch image info for preview: %s", e)

        # 1. Safety Guard Check
        is_safe, safety_err = SafetyGuard.check_safety(
            prompt=original_prompt,
            instruction=instruction,
            proposed_adds=[],
            age_status=age_status,
            adult_mode=req.adult_mode,
        )
        if not is_safe:
            return PromptEditPreviewResponse(
                success=False,
                original_prompt=original_prompt,
                edited_prompt=original_prompt,
                diff_summary={"added": [], "removed": [], "kept": [], "locked": []},
                diff_tags=[],
                edit_scopes=[],
                prompt_version=current_version,
                safe=False,
                error=safety_err or "unsafe_request",
                message="Minor characters cannot be modified with adult or sexualized content.",
            )

        # 2. Parse original prompt into AST
        original_ast = parse_prompt(original_prompt)

        # Locked tags preparation
        locked_tags_input = list(req.locked_tags or [])
        # Mark AST items that are explicitly locked
        locked_set = {canonical_tag(t) for t in locked_tags_input}
        for item in original_ast:
            if canonical_tag(item.tag) in locked_set:
                item.is_locked = True

        # 3. Call LLM AST Compiler
        try:
            llm_result = self.llm.edit_prompt(
                prompt=original_prompt,
                instruction=instruction,
                locked_tags=locked_tags_input,
                allowed_scopes=req.allowed_scopes,
                preserve_identity=req.preserve_identity,
                age_status=age_status,
                adult_mode=req.adult_mode,
            )
        except Exception as exc:
            logger.error("LLM edit_prompt call failed: %s", exc)
            return PromptEditPreviewResponse(
                success=False,
                original_prompt=original_prompt,
                edited_prompt=original_prompt,
                diff_summary={"added": [], "removed": [], "kept": [], "locked": []},
                diff_tags=[],
                edit_scopes=[],
                prompt_version=current_version,
                safe=True,
                error="llm_error",
                message=f"AI service error: {str(exc)}",
            )

        # 4. Secondary Safety Guard Check on LLM proposed additions
        is_safe_after, safety_err_after = SafetyGuard.check_safety(
            prompt=original_prompt,
            instruction=instruction,
            proposed_adds=llm_result.add_tags,
            age_status=age_status,
            adult_mode=req.adult_mode,
        )
        if not is_safe_after:
            return PromptEditPreviewResponse(
                success=False,
                original_prompt=original_prompt,
                edited_prompt=original_prompt,
                diff_summary={"added": [], "removed": [], "kept": [], "locked": []},
                diff_tags=[],
                edit_scopes=[],
                prompt_version=current_version,
                safe=False,
                error=safety_err_after or "unsafe_request",
                message="Minor characters cannot be modified with adult or sexualized content.",
            )

        # 5. Deterministic Conflict Engine
        final_adds, final_removes, conflict_warnings = self.conflict_engine.resolve_conflicts(
            current_tags=original_ast,
            proposed_adds=llm_result.add_tags,
            proposed_removes=llm_result.remove_tags,
            locked_tags=locked_tags_input,
            allowed_scopes=req.allowed_scopes,
        )
        warnings.extend(conflict_warnings)

        # 6. Identity Guard
        safe_removes, protected_kept = self.identity_guard.filter_removals(
            remove_tags=final_removes,
            instruction=instruction,
            preserve_identity=req.preserve_identity,
        )
        if protected_kept:
            warnings.append(f"Identity Guard protected character tags: {protected_kept}")

        # 7. Tag Diff & Reassembly
        edited_prompt, diff_summary, diff_tags = self.diff_engine.calculate_diff(
            original_ast=original_ast,
            remove_tags=safe_removes,
            add_tags=final_adds,
            locked_tags=locked_tags_input,
            warnings=warnings,
        )

        return PromptEditPreviewResponse(
            success=True,
            original_prompt=original_prompt,
            edited_prompt=edited_prompt,
            diff_summary=diff_summary,
            diff_tags=diff_tags,
            edit_scopes=llm_result.edit_scopes,
            prompt_version=current_version,
            safe=True,
            warnings=warnings,
            message="Preview generated successfully",
        )

    def apply_edit(self, req: PromptEditApplyRequest) -> Dict[str, Any]:
        """
        Apply edited prompt to output_images with optimistic locking and record history.
        """
        with connect() as conn:
            row = conn.execute(
                "SELECT id, prompt_version, positive_prompt, negative_prompt FROM output_images WHERE id = ?",
                (req.image_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Image id {req.image_id} not found")

            db_version = row["prompt_version"]
            if db_version != req.prompt_version:
                # 409 Conflict
                raise RuntimeError(
                    f"409 prompt_changed: Database version is {db_version}, but request specified {req.prompt_version}. Please refresh."
                )

            new_version = db_version + 1
            prompt_before = row["positive_prompt"]
            prompt_after = req.edited_prompt

            # Also create non-destructive version in prompt_versions
            diff_dict = req.diff_summary or {}
            v_info = VersionManager.create_version(
                image_id=req.image_id,
                positive_prompt=prompt_after,
                negative_prompt=row["negative_prompt"] or "",
                instruction=req.instruction or "",
                remove_tags=diff_dict.get("removed", []),
                add_tags=diff_dict.get("added", []),
                locked_tags=diff_dict.get("locked", []),
                diff_json=diff_dict,
                model="deepseek",
                applied_by="user",
                conn=conn,
            )

            # Record history
            self.history_mgr.record_history(
                image_id=req.image_id,
                version=new_version,
                instruction=req.instruction or "",
                edit_scopes=req.diff_summary.get("edit_scopes", []) if req.diff_summary else [],
                positive_prompt_before=prompt_before,
                positive_prompt_after=prompt_after,
                negative_prompt_before=row["negative_prompt"] or "",
                negative_prompt_after=row["negative_prompt"] or "",
                diff_json=req.diff_summary or {},
                applied_by="user",
                conn=conn,
            )

            return {
                "success": True,
                "image_id": req.image_id,
                "new_version": new_version,
                "version_id": v_info.get("id"),
                "positive_prompt": prompt_after,
            }

    def undo_edit(self, req: PromptEditUndoRequest) -> Dict[str, Any]:
        """
        Undo last edit or rollback to target_version.
        Strictly switches current_prompt_version_id without creating any new version row.
        """
        with connect() as conn:
            if req.target_version is not None:
                ver = conn.execute(
                    "SELECT * FROM prompt_versions WHERE image_id = ? AND version_number = ?",
                    (req.image_id, req.target_version),
                ).fetchone()
                if not ver:
                    raise ValueError(f"Version {req.target_version} not found for image {req.image_id}")
                target_ver_id = ver["id"]
            else:
                img = conn.execute(
                    "SELECT current_prompt_version_id FROM output_images WHERE id = ?",
                    (req.image_id,),
                ).fetchone()
                cur_id = img["current_prompt_version_id"] if img else None
                cur_ver = None
                if cur_id:
                    cur_ver = conn.execute(
                        "SELECT * FROM prompt_versions WHERE id = ?",
                        (cur_id,),
                    ).fetchone()

                if cur_ver and cur_ver["parent_version_id"]:
                    target_ver_id = cur_ver["parent_version_id"]
                else:
                    v0 = conn.execute(
                        "SELECT id FROM prompt_versions WHERE image_id = ? AND (is_original = 1 OR version_number = 0) LIMIT 1",
                        (req.image_id,),
                    ).fetchone()
                    if not v0:
                        raise ValueError(f"No original version found for image {req.image_id}")
                    target_ver_id = v0["id"]

        result = VersionManager.set_current_version(req.image_id, target_ver_id)
        return {
            "success": True,
            "image_id": req.image_id,
            "restored_version": result["version_number"],
            "restored_prompt": result["positive_prompt"],
            "current_version_id": target_ver_id,
            "is_original": result["is_original"],
        }

    def get_history(self, image_id: int) -> List[PromptEditHistoryItem]:
        """
        Get up to 20 edit history entries for an image.
        """
        return self.history_mgr.get_history(image_id)

    # --- Version Manager API ---

    def get_versions(self, image_id: int) -> List[Dict[str, Any]]:
        """
        Retrieve all prompt versions for an image, ordered from v0 Original upwards.
        """
        return VersionManager.get_versions(image_id)

    def apply_version(self, req: PromptVersionApplyRequest) -> Dict[str, Any]:
        """
        Apply a new prompt version (non-destructive branching).
        """
        diff_dict = req.diff_summary or {}
        return VersionManager.create_version(
            image_id=req.image_id,
            positive_prompt=req.positive_prompt,
            negative_prompt=req.negative_prompt or "",
            instruction=req.instruction or "",
            parent_version_id=req.parent_version_id,
            remove_tags=diff_dict.get("removed", []),
            add_tags=diff_dict.get("added", []),
            locked_tags=diff_dict.get("locked", []),
            diff_json=diff_dict,
            model=req.model or "deepseek",
            applied_by="user",
        )

    def set_current_version(self, req: PromptVersionSetCurrentRequest) -> Dict[str, Any]:
        """
        Switch active working prompt version to an existing version.
        """
        return VersionManager.set_current_version(req.image_id, req.version_id)

    def delete_version(self, version_id: int) -> Dict[str, Any]:
        """
        Delete a non-original version. v0 is strictly protected and cannot be deleted.
        """
        return VersionManager.delete_version(version_id)

    def restore_original(self, image_id: int) -> Dict[str, Any]:
        """
        Restore the active working prompt to v0 Original.
        """
        return VersionManager.restore_original(image_id)


from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from tag_manager.db import connect
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
from .service import PromptEditorService

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["urlpath"] = lambda value: "/".join(quote(part) for part in str(value).split("/"))

router = APIRouter(tags=["Prompt Surgeon"])
_service = PromptEditorService()


@router.post("/api/prompt/edit/preview", response_model=PromptEditPreviewResponse)
def preview_prompt_edit(req: PromptEditRequest):
    """
    Preview prompt edit using Prompt Surgeon AST diff engine and LLM.
    Does not modify database records.
    """
    try:
        return _service.preview_edit(req)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Prompt preview failed: {str(exc)}",
        ) from exc


@router.post("/api/prompt/edit/apply")
def apply_prompt_edit(req: PromptEditApplyRequest):
    """
    Apply prompt edit with optimistic locking (prompt_version check) and record history.
    """
    try:
        return _service.apply_edit(req)
    except RuntimeError as exc:
        if "409" in str(exc) or "prompt_changed" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post("/api/prompt/edit/undo")
def undo_prompt_edit(req: PromptEditUndoRequest):
    """
    Undo the last prompt edit or roll back to a specified version.
    """
    try:
        return _service.undo_edit(req)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/api/prompt/edit/history/{image_id}", response_model=List[PromptEditHistoryItem])
def get_prompt_edit_history(image_id: int):
    """
    Retrieve up to 20 edit history entries for an image.
    """
    try:
        return _service.get_history(image_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ==========================================
# Prompt Versions & Non-destructive Branching
# ==========================================

@router.get("/api/prompt/versions/{image_id}", response_model=List[PromptVersionItem])
def get_prompt_versions(image_id: int):
    """
    Retrieve full prompt version tree / list for an image, including v0 Original.
    """
    try:
        return _service.get_versions(image_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post("/api/prompt/versions/apply")
def apply_prompt_version(req: PromptVersionApplyRequest):
    """
    Create and apply a new prompt version (non-destructive branching).
    """
    try:
        return _service.apply_version(req)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post("/api/prompt/versions/set_current")
def set_current_prompt_version(req: PromptVersionSetCurrentRequest):
    """
    Switch active working prompt version to an existing version.
    """
    try:
        return _service.set_current_version(req)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.delete("/api/prompt/versions/{version_id}")
def delete_prompt_version(version_id: int):
    """
    Delete a non-original prompt version. v0 Original is strictly protected.
    """
    try:
        return _service.delete_version(version_id)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post("/api/prompt/versions/restore_original/{image_id}")
def restore_original_prompt_version(image_id: int):
    """
    Restore the active working prompt to v0 Original.
    """
    try:
        return _service.restore_original(image_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/prompt-surgeon/{image_id}", response_class=HTMLResponse)
def prompt_surgeon_page(request: Request, image_id: int, version_id: Optional[int] = None):
    """
    Dedicated Prompt Surgeon Studio workspace page for a specific image.
    Supports optional ?version_id={version_id}.
    - Even if current version is v0 Original, allows full access.
    - If version_id is missing: loads image.current_prompt_version_id.
    - If image has no current version: falls back to v0 Original.
    """
    with connect() as conn:
        img_row = conn.execute("SELECT * FROM output_images WHERE id = ?", (image_id,)).fetchone()
        if not img_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Image {image_id} not found",
            )
        img = dict(img_row)

        # 1. Ensure v0 Original exists
        v0 = conn.execute(
            "SELECT * FROM prompt_versions WHERE image_id = ? AND (is_original = 1 OR version_number = 0) LIMIT 1",
            (image_id,),
        ).fetchone()
        if not v0:
            cursor = conn.execute(
                """
                INSERT INTO prompt_versions (
                    image_id, version_number, parent_version_id,
                    positive_prompt, negative_prompt, instruction,
                    is_original, applied_by
                ) VALUES (?, 0, NULL, ?, ?, 'Original from metadata', 1, 'original')
                """,
                (image_id, img.get("original_positive_prompt") or img.get("positive_prompt") or "", img.get("original_negative_prompt") or img.get("negative_prompt") or ""),
            )
            v0_id = cursor.lastrowid
            conn.execute(
                "UPDATE output_images SET current_prompt_version_id = ?, prompt_version = 0 WHERE id = ?",
                (v0_id, image_id),
            )
            v0 = conn.execute("SELECT * FROM prompt_versions WHERE id = ?", (v0_id,)).fetchone()

        # 2. Get all versions
        versions = _service.get_versions(image_id)

        # 3. Determine active target version
        active_ver = None
        if version_id is not None:
            active_ver = next((v for v in versions if v["id"] == version_id), None)

        if not active_ver:
            cur_id = img.get("current_prompt_version_id")
            if cur_id:
                active_ver = next((v for v in versions if v["id"] == cur_id), None)

        if not active_ver:
            active_ver = versions[-1] if versions else dict(v0)

    return templates.TemplateResponse(
        request,
        "prompt_surgeon.html",
        {
            "image": img,
            "versions": versions,
            "v0": dict(v0) if v0 else None,
            "active_version": active_ver,
            "initial_version_id": active_ver["id"] if active_ver else None,
        },
    )



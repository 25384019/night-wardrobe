from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .ai_tag_editor import get_deepseek_api_key, transform_tags_with_deepseek
from .db import BASE_DIR, connect
from .outputs_service import (
    favorite_to_gallery,
    get_configured_output_dir,
    get_default_output_dir,
    get_output_dates,
    get_output_image_detail,
    query_output_images,
    analyze_new_output_safety_with_wd14,
    reparse_new_outputs,
    resolve_safe_output_file,
    scan_outputs,
    set_configured_output_dir,
    set_output_safety_level,
)

router = APIRouter(tags=["Outputs"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["urlpath"] = lambda value: "/".join(quote(part) for part in str(value).split("/"))


class SetFolderPayload(BaseModel):
    folder: str


class FavoritePayload(BaseModel):
    rel_path: str
    category: str = "生图精选"


class SafetyLevelPayload(BaseModel):
    level: str


class AiModifyTagsPayload(BaseModel):
    prompt: str
    mode: str = "nsfw"
    instruction: str = ""
    api_key: str = ""
    save_key: bool = False



@router.get("/outputs", response_class=HTMLResponse)
def outputs_page(
    request: Request,
    date: str = Query("", description="按特定日期过滤 (YYYY-MM-DD)"),
    q: str = Query("", description="提示词或模型检索"),
    checkpoint: str = Query("", description="模型筛选"),
    lora: str = Query("", description="LoRA筛选"),
    order: str = Query("desc", description="排序方式：desc最新 / asc最早"),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=10, le=200),
):
    configured_dir = get_configured_output_dir()
    default_dir = get_default_output_dir()

    # 获取全部有生图记录的日期清单
    dates = get_output_dates()
    total_images_in_db = sum(d["count"] for d in dates)

    # 查库
    offset = (page - 1) * page_size
    images, total_filtered = query_output_images(
        date=date,
        q=q,
        checkpoint=checkpoint,
        lora=lora,
        order=order,
        limit=page_size,
        offset=offset,
    )
    total_pages = max(1, math.ceil(total_filtered / page_size))

    # 按日期聚合当前页图片（便于在页面上按日期划分区域）
    date_sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] | None = None
    for img in images:
        fdate = img.get("file_date", "")
        if not current_section or current_section["file_date"] != fdate:
            current_section = {
                "file_date": fdate,
                "images": [],
            }
            date_sections.append(current_section)
        current_section["images"].append(img)

    # 提取可用的 Checkpoint 与 LoRA 选项供筛选
    with connect() as conn:
        available_checkpoints = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT checkpoint FROM output_images WHERE checkpoint != '' ORDER BY checkpoint"
            ).fetchall()
        ]

    return templates.TemplateResponse(
        request,
        "outputs.html",
        {
            "configured_dir": str(configured_dir),
            "is_default_dir": configured_dir.resolve() == default_dir.resolve(),
            "dates": dates,
            "selected_date": date,
            "q": q,
            "checkpoint": checkpoint,
            "lora": lora,
            "order": order,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_filtered": total_filtered,
            "total_images_in_db": total_images_in_db,
            "images": images,
            "date_sections": date_sections,
            "available_checkpoints": available_checkpoints,
        },
    )


@router.get("/outputs/file")
def get_output_file(path: str = Query(..., description="相对路径")):
    try:
        file_path = resolve_safe_output_file(path)
        # 根据扩展名确定 media_type
        suffix = file_path.suffix.lower()
        media_type = "image/png"
        if suffix in (".jpg", ".jpeg"):
            media_type = "image/jpeg"
        elif suffix == ".webp":
            media_type = "image/webp"
        return FileResponse(
            file_path,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=604800, immutable",
            },
        )
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "图片不存在"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/outputs/detail")
def get_detail(
    id: int | None = Query(None),
    path: str | None = Query(None),
):
    detail = get_output_image_detail(image_id=id, rel_path=path)
    if not detail:
        return JSONResponse(status_code=404, content={"error": "未找到图片记录"})
    return detail


@router.post("/api/outputs/scan")
def api_scan_outputs():
    try:
        result = scan_outputs()
        return {"ok": True, "result": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@router.post("/api/outputs/reparse")
def api_reparse_outputs():
    try:
        return {"ok": True, "result": reparse_new_outputs()}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@router.post("/api/outputs/analyze-safety")
def api_analyze_output_safety():
    try:
        return {"ok": True, "result": analyze_new_output_safety_with_wd14()}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@router.post("/api/outputs/{image_id}/safety")
def api_set_output_safety(image_id: int, payload: SafetyLevelPayload):
    try:
        detail = set_output_safety_level(image_id, payload.level)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if not detail:
        return JSONResponse(status_code=404, content={"error": "未找到图片记录"})
    return {"ok": True, "safety_level": detail["safety_level"], "safety_source": detail["safety_source"]}


@router.post("/api/outputs/set-folder")
def api_set_folder(payload: SetFolderPayload):
    try:
        new_dir = set_configured_output_dir(payload.folder)
        scan_res = scan_outputs(new_dir)
        return {"ok": True, "output_dir": str(new_dir), "scan": scan_res}
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@router.post("/api/outputs/favorite")
def api_favorite(payload: FavoritePayload):
    try:
        res = favorite_to_gallery(payload.rel_path, payload.category)
        return res
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@router.get("/api/outputs/ai-tag-status")
def api_ai_tag_status():
    key = get_deepseek_api_key()
    return {
        "ok": True,
        "has_key": bool(key),
        "model": "deepseek-chat",
    }


@router.post("/api/outputs/ai-modify-tags")
def api_ai_modify_tags(payload: AiModifyTagsPayload):
    try:
        if payload.save_key and payload.api_key.strip():
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE llm_settings
                    SET api_key = ?,
                        base_url = CASE WHEN base_url = '' THEN 'https://api.deepseek.com/v1' ELSE base_url END,
                        model = CASE WHEN model = '' THEN 'deepseek-chat' ELSE model END
                    WHERE id = 1
                    """,
                    (payload.api_key.strip(),),
                )

        res = transform_tags_with_deepseek(
            source_prompt=payload.prompt,
            mode=payload.mode,
            instruction=payload.instruction,
            api_key=payload.api_key,
        )
        return res
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


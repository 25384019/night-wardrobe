"""LoRA 卡页面与解析路由。

LoRA 文件本体不落盘：前端用 File.slice 只读 safetensors header（前 8 字节长度 +
header JSON），base64 传给 /api/loras/parse；数据库只存解析结果。
预览图存 BASE_DIR/lora_previews/，经 /lora-previews 静态挂载服务。
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .db import (
    BASE_DIR,
    delete_lora_card,
    get_lora_card,
    get_lora_card_by_name,
    list_lora_cards,
    update_lora_card_preview,
    update_lora_card_triggers,
    upsert_lora_card,
)
from .lora_parser import (
    LoraParseError,
    merge_trigger_words,
    parse_civitai_info,
    parse_safetensors_header,
    read_safetensors_header,
)

router = APIRouter(tags=["LoRA"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

LORA_PREVIEW_DIR = BASE_DIR / "lora_previews"
PREVIEW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
HEADER_B64_LIMIT = 64 * 1024 * 1024  # header base64 上限 64MB，正常仅几 KB~几 MB


class LoraParsePayload(BaseModel):
    filename: str
    header_b64: str


class LoraCivitaiPayload(BaseModel):
    name: str
    civitai_text: str


class LoraTriggerPayload(BaseModel):
    trigger_words: str = ""


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _lora_name(filename: str) -> str:
    return Path(filename).stem.strip()


@router.get("/loras", response_class=HTMLResponse)
def loras_page(request: Request, message: str = "", message_type: str = ""):
    return templates.TemplateResponse(
        request,
        "loras.html",
        {"cards": list_lora_cards(), "message": message, "message_type": message_type},
    )


@router.post("/api/loras/parse")
def parse_lora(payload: LoraParsePayload):
    name = _lora_name(payload.filename)
    if not name:
        return _error("文件名无效")
    if len(payload.header_b64) > HEADER_B64_LIMIT:
        return _error("header 过大，拒绝解析", 413)
    try:
        raw = base64.b64decode(payload.header_b64, validate=True)
        header = read_safetensors_header(raw)
        parsed = parse_safetensors_header(header)
    except (binascii.Error, LoraParseError) as exc:
        return _error(f"解析失败: {exc}", 422)

    existing = get_lora_card_by_name(name)
    card_id = upsert_lora_card(
        name,
        filename=Path(payload.filename).name,
        base_model=parsed["base_model"],
        net_dim=parsed["net_dim"],
        suggested_weight=existing["suggested_weight"] if existing else 0.8,
        trigger_words=parsed["trigger_words"],
        tag_frequency=parsed["tag_frequency"],
        notes=f"输出名: {parsed['output_name']}" if parsed["output_name"] else "",
    )
    card = get_lora_card(card_id)
    return {"card": card, "updated": existing is not None}


@router.post("/api/loras/civitai")
def apply_civitai(payload: LoraCivitaiPayload):
    card = get_lora_card_by_name(payload.name.strip())
    if card is None:
        return _error("未找到对应 LoRA 卡，请先上传 .safetensors 解析", 404)
    try:
        info = parse_civitai_info(payload.civitai_text)
    except LoraParseError as exc:
        return _error(str(exc), 422)

    kohya_words = [w.strip() for w in card["trigger_words"].split(",") if w.strip()]
    trigger_words = merge_trigger_words(info["trained_words"], kohya_words)
    upsert_lora_card(
        card["name"],
        filename=card["filename"],
        base_model=card["base_model"],
        net_dim=card["net_dim"],
        suggested_weight=info["suggested_weight"] or card["suggested_weight"],
        trigger_words=trigger_words,
        tag_frequency=card["tag_frequency"],
        civitai_text=info["description"],
    )
    return {"card": get_lora_card(card["id"])}


@router.post("/api/loras/{card_id}/preview")
async def upload_lora_preview(card_id: int, file: UploadFile = File(...)):
    card = get_lora_card(card_id)
    if card is None:
        return _error("LoRA 卡不存在", 404)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in PREVIEW_EXTENSIONS:
        return _error("仅支持 png/jpg/webp 预览图", 415)
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        return _error("预览图过大", 413)

    LORA_PREVIEW_DIR.mkdir(exist_ok=True)
    for old in LORA_PREVIEW_DIR.glob(f"{card_id}.*"):
        old.unlink()
    (LORA_PREVIEW_DIR / f"{card_id}{ext}").write_bytes(data)
    update_lora_card_preview(card_id, f"{card_id}{ext}")
    return {"card": get_lora_card(card_id)}


@router.patch("/api/loras/{card_id}/triggers")
def update_lora_triggers(card_id: int, payload: LoraTriggerPayload):
    if get_lora_card(card_id) is None:
        return _error("LoRA 卡不存在", 404)
    update_lora_card_triggers(card_id, payload.trigger_words)
    return {"card": get_lora_card(card_id)}


@router.post("/loras/{card_id}/delete")
def delete_lora(card_id: int):
    card = delete_lora_card(card_id)
    if card is None:
        return RedirectResponse("/loras?message=LoRA 卡不存在&message_type=error", status_code=303)
    preview = card.get("preview_image") or ""
    if preview:
        path = (LORA_PREVIEW_DIR / preview).resolve()
        if path.parent == LORA_PREVIEW_DIR.resolve() and path.exists():
            path.unlink()
    return RedirectResponse(f"/loras?message=已删除 {card['name']}&message_type=success", status_code=303)

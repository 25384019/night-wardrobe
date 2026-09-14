from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware

from .copilot_history import (
    adapt_history_for_llm,
    build_assistant_content,
    build_error_content,
    build_user_content,
    snapshot_from_context,
)
from .copilot_service import CopilotError, generate_suggestion, user_visible_text
from .copilot_sessions import (
    SessionNotFound,
    append_message,
    create_session,
    delete_session,
    get_session_detail,
    list_sessions,
    patch_message_content,
    rename_session,
    resolve_or_create_session,
)
from .db import BASE_DIR, PROJECT_DIR, connect, init_db, upsert_category, upsert_character, upsert_recipe, upsert_tag, add_outfit
from .folder_ops import (
    FolderInventory,
    FolderOperationError,
    build_folder_inventory_map,
    delete_gallery_images,
    delete_managed_folder,
    list_folder_options,
    move_gallery_images,
    move_managed_folder,
)
from .gallery import GALLERY_DIR, IMAGE_EXTENSIONS, export_gallery_zip, import_gallery_zip, ingest_saved_paths, save_gallery_bytes, scan_gallery
from .import_magic_book import import_magic_book
from .llm import chat_completion, chat_completion_messages, list_models
from .lora_routes import LORA_PREVIEW_DIR
from .lora_routes import router as lora_router
from .manga_routes import router as manga_router
from .manga_service import manga_service
from .outputs_routes import router as outputs_router
from .outputs_service import scan_outputs
from .prompt_editor import prompt_editor_router
from .tag_api import router as tag_api_router
from .video_decrypt_routes import router as video_decrypt_router
from .video_decrypt_service import video_decrypt_service
from .workflows import WORKFLOW_DIR, WORKFLOW_EXTENSIONS, export_workflows_zip, import_workflows_zip, save_workflow_bytes, scan_workflows

DEV_MODE = os.environ.get("WARDROBE_DEV", "").lower() in ("1", "true", "yes")

app = FastAPI(title="夜之主衣柜", version="1.24.1")
app.include_router(tag_api_router)
app.include_router(video_decrypt_router)
app.include_router(lora_router)
app.include_router(manga_router)
app.include_router(outputs_router)
app.include_router(prompt_editor_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["urlpath"] = lambda value: "/".join(quote(part) for part in str(value).split("/"))
templates.env.globals["dev_mode"] = DEV_MODE
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
GALLERY_DIR.mkdir(exist_ok=True)
WORKFLOW_DIR.mkdir(exist_ok=True)
LORA_PREVIEW_DIR.mkdir(exist_ok=True)
app.mount("/gallery-files", StaticFiles(directory=str(GALLERY_DIR)), name="gallery_files")
app.mount("/workflow-files", StaticFiles(directory=str(WORKFLOW_DIR)), name="workflow_files")
app.mount("/lora-previews", StaticFiles(directory=str(LORA_PREVIEW_DIR)), name="lora_previews")


@app.on_event("startup")
def startup() -> None:
    init_db()
    scan_gallery(initialize_db=False)
    import threading
    threading.Thread(target=scan_outputs, daemon=True).start()
    video_decrypt_service.startup()
    manga_service.startup()


@app.on_event("shutdown")
def shutdown() -> None:
    video_decrypt_service.shutdown()
    manga_service.shutdown()


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _safe_local_next(value: str | None, default: str = "/llm") -> str:
    """只允许站内相对路径，拒绝协议相对与外链，避免开放跳转。"""
    if not value:
        return default
    path = str(value).strip()
    if not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return default
    if "\\" in path or "://" in path:
        return default
    if any(ord(ch) < 32 for ch in path):
        return default
    return path


def folder_url(base: str, folder: str = "", **params: str) -> str:
    query_params = {key: value for key, value in params.items() if value}
    folder = normalize_gallery_folder(folder)
    if folder:
        query_params["folder"] = folder
    query = urlencode(query_params)
    return f"{base}?{query}" if query else base


def workflows_redirect(message: str = "", folder: str = "") -> RedirectResponse:
    return redirect(folder_url("/workflows", folder, message=message))


def managed_folder_redirect(base: str, folder: str = "", message: str = "", message_type: str = "") -> RedirectResponse:
    return redirect(folder_url(base, folder, message=message, message_type=message_type))


def scan_gallery_with_feedback(folder: str, busy_message: str) -> RedirectResponse | None:
    try:
        scan_gallery(initialize_db=False)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        return managed_folder_redirect("/gallery", folder, busy_message, "warning")
    return None


def ingest_with_feedback(paths, folder: str, busy_message: str) -> RedirectResponse | None:
    """上传/导入后只入库本次新增文件；数据库被占用时降级提示手动扫描。"""
    try:
        ingest_saved_paths(paths, initialize_db=False)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        return managed_folder_redirect("/gallery", folder, busy_message, "warning")
    return None


def tag_url(**updates: str) -> str:
    params = {key: value for key, value in updates.items() if value}
    query = urlencode(params)
    return f"/tags?{query}" if query else "/tags"


def normalize_gallery_folder(folder: str) -> str:
    folder = (folder or "").replace("\\", "/").strip("/")
    parts = [part for part in folder.split("/") if part]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def folder_breadcrumbs(folder: str, root_name: str, root_url: str) -> list[dict[str, str]]:
    crumbs = [{"name": root_name, "url": root_url, "folder": ""}]
    current: list[str] = []
    for part in normalize_gallery_folder(folder).split("/"):
        if not part:
            continue
        current.append(part)
        crumbs.append({"name": part, "url": root_url + "?" + urlencode({"folder": "/".join(current)}), "folder": "/".join(current)})
    return crumbs


def gallery_breadcrumbs(folder: str) -> list[dict[str, str]]:
    return folder_breadcrumbs(folder, "图库", "/gallery")


def workflow_breadcrumbs(folder: str) -> list[dict[str, str]]:
    return folder_breadcrumbs(folder, "工作流", "/workflows")


def is_direct_child_path(path: str, folder: str) -> bool:
    folder = normalize_gallery_folder(folder)
    if not folder:
        return "/" not in path
    prefix = folder + "/"
    if not path.startswith(prefix):
        return False
    return "/" not in path[len(prefix):]


def is_direct_child_image(path: str, folder: str) -> bool:
    return is_direct_child_path(path, folder)


def build_path_folders(paths: list[str], folder: str, cover: bool = True) -> list[dict[str, str | int]]:
    folder = normalize_gallery_folder(folder)
    prefix = folder + "/" if folder else ""
    folders: dict[str, dict[str, str | int]] = {}
    for path in paths:
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if "/" not in rest:
            continue
        name = rest.split("/", 1)[0]
        full_path = f"{prefix}{name}" if prefix else name
        item = folders.setdefault(name, {"name": name, "path": full_path, "count": 0, "cover": path if cover else ""})
        item["count"] = int(item["count"]) + 1
    return sorted(folders.values(), key=lambda item: str(item["name"]).lower())


def build_gallery_folders(paths: list[str], folder: str) -> list[dict[str, str | int]]:
    return build_path_folders(paths, folder)


def build_workflow_folders(paths: list[str], folder: str) -> list[dict[str, str | int]]:
    return build_path_folders(paths, folder, cover=False)


def safe_child_folder_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("文件夹名称不合法")
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip("._ ")
    if not name:
        raise ValueError("文件夹名称不合法")
    return name


def safe_folder_target(root: Path, folder: str) -> Path:
    root = root.resolve()
    folder = normalize_gallery_folder(folder)
    target = (root / folder).resolve() if folder else root
    if target != root and root not in target.parents:
        raise ValueError("文件夹路径不合法")
    return target


def merge_filesystem_folders(
    root: Path,
    folder: str,
    folders: list[dict[str, str | int]],
    inventory_map: dict[str, FolderInventory] | None = None,
) -> list[dict[str, str | int | bool]]:
    folder = normalize_gallery_folder(folder)
    prefix = folder + "/" if folder else ""
    merged = {str(item["name"]): dict(item) for item in folders}
    current = safe_folder_target(root, folder)
    if current.exists():
        for child in current.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            merged.setdefault(name, {"name": name, "path": f"{prefix}{name}" if prefix else name, "count": 0, "cover": ""})
    inventory_map = inventory_map or build_folder_inventory_map(root, set())
    for item in merged.values():
        inventory = inventory_map.get(str(item["path"]), FolderInventory())
        item.update(inventory.as_dict())
        item["count"] = inventory.tracked_file_count
    return sorted(merged.values(), key=lambda item: str(item["name"]).lower())


def current_folder_info(folder: str, inventory_map: dict[str, FolderInventory]) -> dict[str, str | int | bool]:
    folder = normalize_gallery_folder(folder)
    inventory = inventory_map.get(folder, FolderInventory())
    parent = ""
    name = ""
    if folder:
        name = folder.rsplit("/", 1)[-1]
        parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
    return {"path": folder, "name": name, "parent": parent, **inventory.as_dict()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with connect() as conn:
        stats = {
            "tags": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
            "characters": conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0],
            "recipes": conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0],
            "images": conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0],
            "workflows": conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0],
        }
        recent_images = conn.execute("SELECT * FROM gallery_images ORDER BY updated_at DESC LIMIT 8").fetchall()
    return templates.TemplateResponse(request, "index.html", {"stats": stats, "recent_images": recent_images})


@app.post("/import-magic-book")
def import_magic_book_route():
    import_magic_book()
    return redirect("/tags")


def get_tag_rows(q: str = "", category: str = "", subcategory: str = "", offset: int = 0, limit: int = 80):
    limit = max(20, min(limit, 200))
    offset = max(0, offset)
    query = "SELECT * FROM tags WHERE 1=1"
    count_query = "SELECT COUNT(*) FROM tags WHERE 1=1"
    params: list[str | int] = []
    count_params: list[str] = []
    if q:
        query += " AND (tag LIKE ? OR zh LIKE ? OR notes LIKE ?)"
        count_query += " AND (tag LIKE ? OR zh LIKE ? OR notes LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like])
        count_params.extend([like, like, like])
    if category:
        query += " AND category = ?"
        count_query += " AND category = ?"
        params.append(category)
        count_params.append(category)
    if subcategory:
        query += " AND subcategory = ?"
        count_query += " AND subcategory = ?"
        params.append(subcategory)
        count_params.append(subcategory)
    query += " ORDER BY rating DESC, category, subcategory, tag LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return query, count_query, params, count_params, offset, limit


def get_tags_page_data(conn, q: str, category: str, subcategory: str, offset: int, limit: int):
    """装配 Tag 页首屏数据，/tags 页面与 /api/tags/filter 接口共用同一数据路径。"""
    query, count_query, params, count_params, offset, limit = get_tag_rows(q, category, subcategory, offset, limit)
    rows = conn.execute(query, params).fetchall()
    shown_count = conn.execute(count_query, count_params).fetchone()[0]
    total_count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    categories = conn.execute(
        """
        SELECT c.name AS category, COUNT(t.id) AS count
        FROM categories c
        LEFT JOIN tags t ON t.category = c.name
        WHERE c.kind IN ('tag', 'both')
        GROUP BY c.name, c.sort_order
        ORDER BY c.sort_order, c.name
        """
    ).fetchall()
    subcategories = conn.execute(
        """
        SELECT COALESCE(NULLIF(subcategory, ''), '未分类') AS subcategory, COUNT(*) AS count
        FROM tags
        WHERE (? = '' OR category = ?)
          AND (? = '' OR tag LIKE ? OR zh LIKE ? OR notes LIKE ?)
        GROUP BY COALESCE(NULLIF(subcategory, ''), '未分类')
        ORDER BY MIN(id)
        """,
        (category, category, q, f"%{q}%", f"%{q}%", f"%{q}%"),
    ).fetchall()
    return rows, shown_count, total_count, categories, subcategories


def get_category_subcategory_map(conn):
    """一级分类 → 该分类下全部二级分类（有序）的映射，供 Tag 编辑/新增表单级联。"""
    rows = conn.execute(
        """
        SELECT category, subcategory, MIN(id) AS first_id
        FROM tags
        WHERE category != '' AND subcategory != ''
        GROUP BY category, subcategory
        ORDER BY category, first_id
        """
    ).fetchall()
    mapping = {}
    for r in rows:
        mapping.setdefault(r["category"], []).append(r["subcategory"])
    return mapping


@app.get("/tags", response_class=HTMLResponse)
def tags(request: Request, q: str = "", category: str = "", subcategory: str = "", offset: int = 0, limit: int = 80):
    with connect() as conn:
        rows, shown_count, total_count, categories, subcategories = get_tags_page_data(
            conn, q, category, subcategory, offset, limit
        )
        category_sub_map = get_category_subcategory_map(conn)
    return templates.TemplateResponse(
        request,
        "tags.html",
        {
            "rows": rows,
            "q": q,
            "category": category,
            "subcategory": subcategory,
            "categories": categories,
            "subcategories": subcategories,
            "category_sub_map": category_sub_map,
            "shown_count": shown_count,
            "loaded_count": offset + len(rows),
            "next_offset": offset + len(rows),
            "page_limit": max(20, min(limit, 200)),
            "total_count": total_count,
            "tag_url": tag_url,
        },
    )


@app.post("/categories/add")
def add_category(name: str = Form(...), kind: str = Form("tag"), sort_order: int = Form(100), notes: str = Form("")):
    upsert_category(name=name, kind=kind, sort_order=sort_order, notes=notes)
    return redirect("/tags")


@app.post("/tags/add")
def add_tag(tag: str = Form(...), zh: str = Form(""), category: str = Form(""), subcategory: str = Form(""), notes: str = Form("")):
    upsert_tag(tag=tag, zh=zh, category=category, subcategory=subcategory, source="手工添加", notes=notes)
    return redirect("/tags")


@app.post("/tags/{tag_id}/update")
def update_tag(tag_id: int, tag: str = Form(...), zh: str = Form(""), category: str = Form(""), subcategory: str = Form(""), notes: str = Form("")):
    with connect() as conn:
        conn.execute(
            """
            UPDATE tags SET tag=?, zh=?, category=?, subcategory=?, notes=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (tag, zh, category, subcategory, notes, tag_id),
        )
    if category:
        upsert_category(category)
    return redirect("/tags")


@app.post("/tags/{tag_id}/delete")
def delete_tag(tag_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    return redirect("/tags")


@app.post("/categories/delete")
def delete_category(name: str = Form(...)):
    with connect() as conn:
        conn.execute("UPDATE tags SET category='' WHERE category=?", (name,))
        conn.execute("DELETE FROM categories WHERE name=?", (name,))
    return redirect("/tags")


@app.post("/subcategories/delete")
def delete_subcategory(category: str = Form(""), subcategory: str = Form("")):
    with connect() as conn:
        if category:
            conn.execute("UPDATE tags SET subcategory='' WHERE category=? AND subcategory=?", (category, subcategory))
        else:
            conn.execute("UPDATE tags SET subcategory='' WHERE subcategory=?", (subcategory,))
    return redirect("/tags")


TAG_LIBRARY_PATH = BASE_DIR / "tag_library.json"


@app.post("/export-tags")
def export_tags():
    with connect() as conn:
        categories = [dict(r) for r in conn.execute("SELECT name, kind, sort_order, notes FROM categories ORDER BY sort_order, name").fetchall()]
        tags = [dict(r) for r in conn.execute("SELECT tag, zh, category, subcategory, source, rating, notes FROM tags ORDER BY category, subcategory, tag").fetchall()]
    data = {"categories": categories, "tags": tags}
    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=tag_library.json"},
    )


@app.post("/import-tags")
async def import_tags(file: UploadFile = File(...)):
    content = await file.read()
    data = json.loads(content.decode("utf-8"))
    for cat in data.get("categories", []):
        upsert_category(name=cat["name"], kind=cat.get("kind", "tag"), sort_order=cat.get("sort_order", 0), notes=cat.get("notes", ""))
    for t in data.get("tags", []):
        upsert_tag(tag=t["tag"], zh=t.get("zh", ""), category=t.get("category", ""), subcategory=t.get("subcategory", ""), source=t.get("source", ""), notes=t.get("notes", ""))
    return redirect("/tags")


@app.get("/gallery", response_class=HTMLResponse)
def gallery(
    request: Request,
    q: str = "",
    category: str = "",
    checkpoint: str = "",
    folder: str = "",
    message: str = "",
    message_type: str = "",
):
    folder = normalize_gallery_folder(folder)
    inventory_map = build_folder_inventory_map(GALLERY_DIR, IMAGE_EXTENSIONS)
    query = "SELECT * FROM gallery_images WHERE 1=1"
    params: list[str] = []
    if q:
        query += " AND (title LIKE ? OR path LIKE ? OR category LIKE ? OR positive_prompt LIKE ? OR loras LIKE ? OR notes LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like, like, like])
    if category:
        query += " AND category = ?"
        params.append(category)
    if checkpoint:
        query += " AND checkpoint LIKE ?"
        params.append(f"%{checkpoint}%")
    query += " ORDER BY updated_at DESC LIMIT 200"
    with connect() as conn:
        all_paths = [r["path"] for r in conn.execute("SELECT path FROM gallery_images ORDER BY path").fetchall()]
        if q:
            rows = conn.execute(query, params).fetchall()
            folders = []
        else:
            rows = [r for r in conn.execute(query, params).fetchall() if is_direct_child_image(r["path"], folder)]
            folders = merge_filesystem_folders(GALLERY_DIR, folder, build_gallery_folders(all_paths, folder), inventory_map)
        categories = conn.execute("SELECT DISTINCT category FROM gallery_images WHERE category != '' ORDER BY category").fetchall()
        checkpoints = conn.execute("SELECT DISTINCT checkpoint FROM gallery_images WHERE checkpoint != '' ORDER BY checkpoint").fetchall()
    return templates.TemplateResponse(
        request,
        "gallery.html",
        {
            "rows": rows,
            "folders": folders,
            "folder": folder,
            "breadcrumbs": gallery_breadcrumbs(folder),
            "q": q,
            "category": category,
            "checkpoint": checkpoint,
            "categories": categories,
            "checkpoints": checkpoints,
            "folder_options": list_folder_options(GALLERY_DIR, inventory_map=inventory_map),
            "current_folder_info": current_folder_info(folder, inventory_map),
            "message": message,
            "message_type": message_type,
        },
    )


@app.post("/scan-gallery")
def scan_gallery_route(folder: str = Form("")):
    folder = normalize_gallery_folder(folder)
    busy_response = scan_gallery_with_feedback(folder, "图库数据库正被其他任务占用，请稍后重试。")
    if busy_response:
        return busy_response
    return redirect(folder_url("/gallery", folder))


@app.post("/gallery/folders")
def gallery_create_folder(folder: str = Form(""), name: str = Form("")):
    folder = normalize_gallery_folder(folder)
    child = safe_child_folder_name(name)
    target_folder = f"{folder}/{child}" if folder else child
    safe_folder_target(GALLERY_DIR, target_folder).mkdir(parents=True, exist_ok=True)
    return redirect(folder_url("/gallery", target_folder))


@app.post("/gallery/folders/move")
def gallery_move_folder(source: str = Form(...), destination: str = Form(""), current: str = Form("")):
    current = normalize_gallery_folder(current)
    try:
        result = move_managed_folder(
            GALLERY_DIR,
            "gallery_images",
            source,
            destination,
            tracked_extensions=IMAGE_EXTENSIONS,
        )
    except FolderOperationError as exc:
        return managed_folder_redirect("/gallery", current, f"移动失败：{exc}", "error")
    next_folder = result.target if current == result.source else current
    return managed_folder_redirect("/gallery", next_folder, f"已移动文件夹「{result.source}」", "success")


@app.post("/gallery/folders/delete")
def gallery_delete_folder(folder: str = Form(...), current: str = Form(""), recursive: bool = Form(False)):
    current = normalize_gallery_folder(current)
    try:
        result = delete_managed_folder(
            GALLERY_DIR,
            "gallery_images",
            folder,
            tracked_extensions=IMAGE_EXTENSIONS,
            recursive=recursive,
        )
    except FolderOperationError as exc:
        return managed_folder_redirect("/gallery", current, f"删除失败：{exc}", "error")
    next_folder = result.parent if current == result.source else current
    message_type = "warning" if result.warning else "success"
    message = result.warning or f"已删除文件夹「{result.source}」"
    return managed_folder_redirect("/gallery", next_folder, message, message_type)


@app.post("/gallery/images/move")
def gallery_move_images(ids: list[int] = Form(...), destination: str = Form(""), current: str = Form("")):
    current = normalize_gallery_folder(current)
    try:
        result = move_gallery_images(GALLERY_DIR, ids, destination)
    except FolderOperationError as exc:
        return managed_folder_redirect("/gallery", current, f"移动失败：{exc}", "error")
    return managed_folder_redirect("/gallery", current, f"已移动 {result['moved']} 张图片", "success")


@app.post("/gallery/images/delete")
def gallery_delete_images(ids: list[int] = Form(...), current: str = Form("")):
    current = normalize_gallery_folder(current)
    try:
        result = delete_gallery_images(GALLERY_DIR, ids)
    except FolderOperationError as exc:
        return managed_folder_redirect("/gallery", current, f"删除失败：{exc}", "error")
    message_type = "warning" if result["warning"] else "success"
    message = result["warning"] or f"已删除 {result['deleted']} 张图片"
    return managed_folder_redirect("/gallery", current, message, message_type)


@app.post("/gallery/upload")
async def gallery_upload(folder: str = Form(""), files: list[UploadFile] = File(...)):
    folder = normalize_gallery_folder(folder)
    saved = []
    for file in files:
        if not file.filename:
            continue
        data = await file.read()
        filename = f"{folder}/{file.filename}" if folder else file.filename
        saved.append(save_gallery_bytes(data, filename))
    busy_response = ingest_with_feedback(saved, folder, "图片已保存，但图库数据库正忙；请稍后点击“扫描图库”。")
    if busy_response:
        return busy_response
    return redirect(folder_url("/gallery", folder))


@app.post("/gallery/export")
def gallery_export():
    data = export_gallery_zip()
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=gallery_export.zip"})


@app.post("/gallery/import")
async def gallery_import(file: UploadFile = File(...), folder: str = Form("")):
    folder = normalize_gallery_folder(folder)
    data = await file.read()
    saved = import_gallery_zip(data, folder=folder)
    busy_response = ingest_with_feedback(saved, folder, "图片已导入，但图库数据库正忙；请稍后点击“扫描图库”。")
    if busy_response:
        return busy_response
    return redirect(folder_url("/gallery", folder))


@app.get("/workflows", response_class=HTMLResponse)
def workflows(
    request: Request,
    q: str = "",
    category: str = "",
    checkpoint: str = "",
    folder: str = "",
    message: str = "",
    message_type: str = "",
):
    folder = normalize_gallery_folder(folder)
    inventory_map = build_folder_inventory_map(WORKFLOW_DIR, WORKFLOW_EXTENSIONS)
    query = "SELECT * FROM workflows WHERE 1=1"
    params: list[str] = []
    if q:
        query += " AND (title LIKE ? OR path LIKE ? OR checkpoint LIKE ? OR loras LIKE ? OR notes LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if category:
        query += " AND category = ?"
        params.append(category)
    if checkpoint:
        query += " AND checkpoint LIKE ?"
        params.append(f"%{checkpoint}%")
    query += " ORDER BY updated_at DESC LIMIT 200"
    with connect() as conn:
        all_paths = [r["path"] for r in conn.execute("SELECT path FROM workflows ORDER BY path").fetchall()]
        if q:
            rows = conn.execute(query, params).fetchall()
            folders = []
        else:
            rows = [r for r in conn.execute(query, params).fetchall() if is_direct_child_path(r["path"], folder)]
            folders = merge_filesystem_folders(WORKFLOW_DIR, folder, build_workflow_folders(all_paths, folder), inventory_map)
        categories = conn.execute("SELECT DISTINCT category FROM workflows WHERE category != '' ORDER BY category").fetchall()
        checkpoints = conn.execute("SELECT DISTINCT checkpoint FROM workflows WHERE checkpoint != '' ORDER BY checkpoint").fetchall()
    return templates.TemplateResponse(
        request,
        "workflows.html",
        {
            "rows": rows,
            "folders": folders,
            "folder": folder,
            "breadcrumbs": workflow_breadcrumbs(folder),
            "q": q,
            "category": category,
            "checkpoint": checkpoint,
            "categories": categories,
            "checkpoints": checkpoints,
            "message": message,
            "message_type": message_type,
            "folder_options": list_folder_options(WORKFLOW_DIR, inventory_map=inventory_map),
            "current_folder_info": current_folder_info(folder, inventory_map),
        },
    )


@app.post("/workflows/folders")
def workflow_create_folder(folder: str = Form(""), name: str = Form("")):
    folder = normalize_gallery_folder(folder)
    child = safe_child_folder_name(name)
    target_folder = f"{folder}/{child}" if folder else child
    safe_folder_target(WORKFLOW_DIR, target_folder).mkdir(parents=True, exist_ok=True)
    return redirect(folder_url("/workflows", target_folder))


@app.post("/workflows/folders/move")
def workflow_move_folder(source: str = Form(...), destination: str = Form(""), current: str = Form("")):
    current = normalize_gallery_folder(current)
    try:
        result = move_managed_folder(
            WORKFLOW_DIR,
            "workflows",
            source,
            destination,
            tracked_extensions=WORKFLOW_EXTENSIONS,
        )
    except FolderOperationError as exc:
        return managed_folder_redirect("/workflows", current, f"移动失败：{exc}", "error")
    next_folder = result.target if current == result.source else current
    return managed_folder_redirect("/workflows", next_folder, f"已移动文件夹「{result.source}」", "success")


@app.post("/workflows/folders/delete")
def workflow_delete_folder(folder: str = Form(...), current: str = Form(""), recursive: bool = Form(False)):
    current = normalize_gallery_folder(current)
    try:
        result = delete_managed_folder(
            WORKFLOW_DIR,
            "workflows",
            folder,
            tracked_extensions=WORKFLOW_EXTENSIONS,
            recursive=recursive,
        )
    except FolderOperationError as exc:
        return managed_folder_redirect("/workflows", current, f"删除失败：{exc}", "error")
    next_folder = result.parent if current == result.source else current
    message_type = "warning" if result.warning else "success"
    message = result.warning or f"已删除文件夹「{result.source}」"
    return managed_folder_redirect("/workflows", next_folder, message, message_type)


@app.post("/workflows/upload")
async def workflow_upload(file: UploadFile = File(...), folder: str = Form(""), title: str = Form(""), category: str = Form(""), notes: str = Form("")):
    folder = normalize_gallery_folder(folder)
    if not file.filename or not file.filename.lower().endswith(".json"):
        return workflows_redirect("只能上传 JSON 工作流文件", folder)
    try:
        data = await file.read()
        filename = f"{folder}/{file.filename}" if folder else file.filename
        save_workflow_bytes(data, filename, title=title, category=category, notes=notes)
    except Exception as exc:
        return workflows_redirect(f"导入失败：{exc}", folder)
    return redirect(folder_url("/workflows", folder))


@app.post("/scan-workflows")
def scan_workflows_route(folder: str = Form("")):
    folder = normalize_gallery_folder(folder)
    scan_workflows()
    return redirect(folder_url("/workflows", folder))


@app.get("/workflow-download/{workflow_id}")
def workflow_download(workflow_id: int):
    with connect() as conn:
        row = conn.execute("SELECT path, title FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not row:
        return Response("工作流不存在", status_code=404)
    path = WORKFLOW_DIR / row["path"]
    if not path.exists() or not path.is_file():
        return Response("工作流文件不存在", status_code=404)
    filename = row["title"] or path.name
    if not filename.lower().endswith(".json"):
        filename += ".json"
    return FileResponse(path, media_type="application/json", filename=filename)


@app.post("/workflows/export")
def workflows_export():
    data = export_workflows_zip()
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=workflow_export.zip"})


@app.post("/workflows/import")
async def workflows_import(file: UploadFile = File(...), folder: str = Form("")):
    folder = normalize_gallery_folder(folder)
    try:
        data = await file.read()
        import_workflows_zip(data, folder=folder)
    except Exception as exc:
        return workflows_redirect(f"导入失败：{exc}", folder)
    return redirect(folder_url("/workflows", folder))


@app.post("/workflows/{workflow_id}/update")
def workflow_update(workflow_id: int, title: str = Form(...), category: str = Form(""), notes: str = Form("")):
    with connect() as conn:
        conn.execute(
            "UPDATE workflows SET title=?, category=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (title, category, notes, workflow_id),
        )
    return redirect("/workflows")


@app.post("/workflows/{workflow_id}/delete")
def workflow_delete(workflow_id: int):
    with connect() as conn:
        row = conn.execute("SELECT path FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row:
            path = WORKFLOW_DIR / row["path"]
            if path.exists() and path.is_file():
                path.unlink()
        conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
    return redirect("/workflows")


@app.get("/llm", response_class=HTMLResponse)
def llm_page(request: Request):
    with connect() as conn:
        settings = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    return templates.TemplateResponse(request, "llm.html", {"settings": settings, "answer": "", "message": ""})


@app.post("/llm/settings")
def save_llm_settings(
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    default_system_prompt: str = Form(""),
    copilot_enabled: str = Form("1"),
    next_path: str = Form("", alias="next"),
):
    with connect() as conn:
        row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        saved_key = _retain_blank_api_key(_row_value(row, "api_key") or "", api_key)
        conn.execute(
            """
            UPDATE llm_settings
            SET base_url=?, api_key=?, model=?, default_system_prompt=?, copilot_enabled=?
            WHERE id=1
            """,
            (base_url, saved_key, model, default_system_prompt, 1 if _truthy(copilot_enabled) else 0),
        )
    return redirect(_safe_local_next(next_path, default="/llm"))


@app.post("/llm/chat", response_class=HTMLResponse)
def llm_chat(request: Request, message: str = Form(...)):
    with connect() as conn:
        settings = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    try:
        answer = chat_completion(settings["base_url"], settings["api_key"], settings["model"], settings["default_system_prompt"], message)
    except Exception as exc:
        answer = f"请求失败：{exc}"
    return templates.TemplateResponse(request, "llm.html", {"settings": settings, "answer": answer, "message": message})


# ─── 配方库 ───────────────────────────────────────────────────────────────────

RECIPE_TYPES = [
    ("artist_mix", "画师串"),
    ("scene", "场景预设"),
    ("negative", "负面模板"),
    ("params", "绘图参数"),
]


@app.get("/recipes", response_class=HTMLResponse)
def recipes(request: Request, type: str = "", q: str = "", offset: int = 0, limit: int = 60):
    where_clauses = ["1=1"]
    params: list[Any] = []
    if type:
        where_clauses.append("type = ?")
        params.append(type)
    if q:
        where_clauses.append("(name LIKE ? OR positive_prompt LIKE ? OR negative_prompt LIKE ? OR notes LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])

    where_sql = " AND ".join(where_clauses)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM recipes WHERE {where_sql}", params).fetchone()[0]
        query = f"SELECT * FROM recipes WHERE {where_sql} ORDER BY type, updated_at DESC LIMIT ? OFFSET ?"
        rows = conn.execute(query, [*params, limit, offset]).fetchall()
        counts = conn.execute(
            "SELECT type, COUNT(*) as count FROM recipes GROUP BY type"
        ).fetchall()
    count_map = {r["type"]: r["count"] for r in counts}
    page = (offset // limit) + 1 if limit > 0 else 1
    return templates.TemplateResponse(
        request,
        "recipes.html",
        {
            "rows": rows,
            "type": type,
            "q": q,
            "recipe_types": RECIPE_TYPES,
            "count_map": count_map,
            "total": total,
            "limit": limit,
            "offset": offset,
            "page": page,
        },
    )


@app.post("/recipes/add")
def add_recipe(
    name: str = Form(...),
    type: str = Form("scene"),
    positive_prompt: str = Form(""),
    negative_prompt: str = Form(""),
    params_json: str = Form(""),
    notes: str = Form(""),
):
    upsert_recipe(name=name, type=type, positive_prompt=positive_prompt, negative_prompt=negative_prompt, params_json=params_json, notes=notes, source="手工添加")
    return redirect("/recipes")


@app.post("/recipes/{recipe_id}/update")
def update_recipe(
    recipe_id: int,
    name: str = Form(...),
    type: str = Form("scene"),
    positive_prompt: str = Form(""),
    negative_prompt: str = Form(""),
    params_json: str = Form(""),
    notes: str = Form(""),
):
    with connect() as conn:
        conn.execute(
            """
            UPDATE recipes SET name=?, type=?, positive_prompt=?, negative_prompt=?, params_json=?, notes=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (name, type, positive_prompt, negative_prompt, params_json, notes, recipe_id),
        )
    return redirect("/recipes")


@app.post("/recipes/{recipe_id}/delete")
def delete_recipe(recipe_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))
    return redirect("/recipes")


# ─── 角色卡 ───────────────────────────────────────────────────────────────────


@app.get("/characters", response_class=HTMLResponse)
def characters(request: Request, q: str = ""):
    with connect() as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM characters WHERE name LIKE ? OR lora LIKE ? OR trigger_words LIKE ? OR appearance LIKE ? OR notes LIKE ? ORDER BY updated_at DESC",
                (like, like, like, like, like),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM characters ORDER BY updated_at DESC").fetchall()
        outfits = conn.execute("SELECT * FROM character_outfits ORDER BY character_id, id").fetchall()
    outfit_map: dict[int, list] = {}
    for o in outfits:
        outfit_map.setdefault(o["character_id"], []).append(o)
    return templates.TemplateResponse(request, "characters.html", {"rows": rows, "outfit_map": outfit_map, "q": q})


@app.post("/characters/add")
def add_character(
    name: str = Form(...),
    lora: str = Form(""),
    lora_weight: float = Form(1.0),
    trigger_words: str = Form(""),
    appearance: str = Form(""),
    notes: str = Form(""),
):
    upsert_character(name=name, lora=lora, lora_weight=lora_weight, trigger_words=trigger_words, appearance=appearance, notes=notes)
    return redirect("/characters")


@app.post("/characters/{char_id}/update")
def update_character(
    char_id: int,
    name: str = Form(...),
    lora: str = Form(""),
    lora_weight: float = Form(1.0),
    trigger_words: str = Form(""),
    appearance: str = Form(""),
    notes: str = Form(""),
):
    with connect() as conn:
        conn.execute(
            """
            UPDATE characters SET name=?, lora=?, lora_weight=?, trigger_words=?, appearance=?, notes=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (name, lora, lora_weight, trigger_words, appearance, notes, char_id),
        )
    return redirect("/characters")


@app.post("/characters/{char_id}/delete")
def delete_character(char_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM characters WHERE id=?", (char_id,))
    return redirect("/characters")


@app.post("/characters/{char_id}/outfits/add")
def add_character_outfit(char_id: int, name: str = Form(...), tags: str = Form(""), notes: str = Form("")):
    add_outfit(char_id, name=name, tags=tags, notes=notes)
    return redirect("/characters")


@app.post("/characters/{char_id}/outfits/{outfit_id}/delete")
def delete_outfit(char_id: int, outfit_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM character_outfits WHERE id=? AND character_id=?", (outfit_id, char_id))
    return redirect("/characters")


# ─── 工坊 ─────────────────────────────────────────────────────────────────────


def get_characters_data() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM characters ORDER BY updated_at DESC").fetchall()
        outfits = conn.execute("SELECT * FROM character_outfits ORDER BY character_id, id").fetchall()
    outfit_map: dict[int, list[dict]] = {}
    for outfit in outfits:
        outfit_map.setdefault(outfit["character_id"], []).append(
            {"id": outfit["id"], "name": outfit["name"], "tags": outfit["tags"]}
        )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "lora": row["lora"],
            "lora_weight": row["lora_weight"],
            "trigger_words": row["trigger_words"],
            "appearance": row["appearance"],
            "outfits": outfit_map.get(row["id"], []),
        }
        for row in rows
    ]


def get_recipes_data(recipe_type: str = "") -> list[dict]:
    query = "SELECT * FROM recipes"
    params: list[str] = []
    if recipe_type:
        query += " WHERE type = ?"
        params.append(recipe_type)
    else:
        query += " WHERE type != 'codex_prompt'"
    query += " ORDER BY type, updated_at DESC"
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


@app.get("/workshop", response_class=HTMLResponse)
def workshop(request: Request):
    with connect() as conn:
        settings = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    return templates.TemplateResponse(
        request,
        "workshop.html",
        {
            "characters": get_characters_data(),
            "recipes": get_recipes_data(),
            "settings": settings,
        },
    )


# ─── 工坊 JSON API ────────────────────────────────────────────────────────────


def _copilot_connect():
    return connect()


def _session_snapshot_from_body(body: dict | None) -> dict:
    body = body if isinstance(body, dict) else {}
    extra = body.get("context_snapshot")
    context = body.get("context") if isinstance(body.get("context"), dict) else {}
    return snapshot_from_context(context, extra if isinstance(extra, dict) else None)


def _copilot_error_payload(message: str, session: dict | None = None, user_message=None, assistant_message=None) -> dict:
    payload = {"error": message}
    if session:
        payload["session_id"] = session["id"]
        payload["session"] = session
    if user_message:
        payload["user_message_id"] = user_message["id"]
    if assistant_message:
        payload["assistant_message_id"] = assistant_message["id"]
    return payload


@app.get("/api/workshop/copilot/sessions")
def api_list_copilot_sessions(q: str = ""):
    sessions = list_sessions(q=q, connect_factory=_copilot_connect)
    return JSONResponse({"sessions": sessions})


@app.post("/api/workshop/copilot/sessions")
def api_create_copilot_session(request_body: dict | None = None):
    body = request_body if isinstance(request_body, dict) else {}
    title = str(body.get("title") or "").strip()
    snapshot = _session_snapshot_from_body(body)
    session = create_session(
        title=title,
        context_snapshot=snapshot,
        connect_factory=_copilot_connect,
    )
    return JSONResponse(session)


@app.get("/api/workshop/copilot/sessions/{session_id}")
def api_get_copilot_session(session_id: str):
    try:
        detail = get_session_detail(session_id, connect_factory=_copilot_connect)
    except SessionNotFound:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse(detail)


@app.patch("/api/workshop/copilot/sessions/{session_id}")
def api_patch_copilot_session(session_id: str, request_body: dict | None = None):
    body = request_body if isinstance(request_body, dict) else {}
    if "title" not in body:
        return JSONResponse({"error": "缺少 title"}, status_code=400)
    try:
        session = rename_session(session_id, str(body.get("title") or ""), connect_factory=_copilot_connect)
    except SessionNotFound:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(session)


@app.delete("/api/workshop/copilot/sessions/{session_id}")
def api_delete_copilot_session(session_id: str):
    try:
        session = delete_session(session_id, connect_factory=_copilot_connect)
    except SessionNotFound:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    remaining = list_sessions(connect_factory=_copilot_connect)
    return JSONResponse({"ok": True, "id": session["id"], "sessions": remaining})


@app.patch("/api/workshop/copilot/sessions/{session_id}/messages/{message_id}")
def api_patch_copilot_message(session_id: str, message_id: str, request_body: dict | None = None):
    body = request_body if isinstance(request_body, dict) else {}
    try:
        message = patch_message_content(session_id, message_id, body, connect_factory=_copilot_connect)
    except SessionNotFound:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse(message)


@app.post("/api/workshop/copilot")
def api_workshop_copilot(request_body: dict):
    """工坊 Copilot：一次请求内校验 Session、落库 user/assistant、再调用 LLM。"""
    body = request_body if isinstance(request_body, dict) else {}
    snapshot = _session_snapshot_from_body(body)
    try:
        session = resolve_or_create_session(
            body.get("session_id"),
            context_snapshot=snapshot,
            connect_factory=_copilot_connect,
        )
    except SessionNotFound:
        return JSONResponse({"error": "会话不存在"}, status_code=404)

    stored = []
    try:
        stored = get_session_detail(session["id"], connect_factory=_copilot_connect)["messages"]
    except SessionNotFound:
        return JSONResponse({"error": "会话不存在"}, status_code=404)

    work_request = dict(body)
    work_request["history"] = adapt_history_for_llm(stored)
    action = str(work_request.get("action") or "").strip()
    instruction = "" if work_request.get("instruction") is None else str(work_request.get("instruction"))
    context = work_request.get("context") if isinstance(work_request.get("context"), dict) else {}
    raw_enabled = context.get("enabled_contexts")
    enabled = [item for item in raw_enabled if item in ("positive", "negative", "recipe")] if isinstance(raw_enabled, list) else []
    user_msg = append_message(
        session["id"],
        "user",
        build_user_content(
            text=user_visible_text(action, instruction),
            action=action,
            contexts=enabled,
        ),
        connect_factory=_copilot_connect,
    )
    session = get_session_detail(session["id"], connect_factory=_copilot_connect)["session"]

    settings = _llm_settings_row()
    settings_error = _copilot_settings_error(settings)
    if settings_error:
        err_msg = append_message(
            session["id"],
            "error",
            build_error_content(message=settings_error),
            connect_factory=_copilot_connect,
        )
        session = get_session_detail(session["id"], connect_factory=_copilot_connect)["session"]
        return JSONResponse(
            _copilot_error_payload(
                settings_error,
                session,
                user_msg,
                err_msg,
            ),
            status_code=400,
        )
    use_tools = bool(work_request.get("use_tools", True))
    try:
        result = generate_suggestion(
            work_request,
            base_url=settings["base_url"],
            api_key=settings["api_key"],
            model=settings["model"],
            extra_system_prompt=settings["default_system_prompt"] or "",
            **({} if use_tools else {"tool_registry": {}}),
        )
    except CopilotError as exc:
        status_code = getattr(exc, "status_code", 500) or 500
        message = _gacha_error_message(exc, settings["api_key"])
        err_msg = append_message(
            session["id"],
            "error",
            build_error_content(message=message),
            connect_factory=_copilot_connect,
        )
        session = get_session_detail(session["id"], connect_factory=_copilot_connect)["session"]
        return JSONResponse(_copilot_error_payload(message, session, user_msg, err_msg), status_code=status_code)

    asst = append_message(
        session["id"],
        "assistant",
        build_assistant_content(result, tools=result.get("tools") if isinstance(result.get("tools"), list) else []),
        connect_factory=_copilot_connect,
    )
    session = get_session_detail(session["id"], connect_factory=_copilot_connect)["session"]
    result["session_id"] = session["id"]
    result["session"] = session
    result["user_message_id"] = user_msg["id"]
    result["assistant_message_id"] = asst["id"]
    return JSONResponse(result)


MSG_LLM_UNCONFIGURED = "LLM 未配置，请先在工坊助手设置中填写 API"
MSG_COPILOT_DISABLED = "AI 提示词助手已关闭，请在设置中启用"
MSG_NEED_URL_KEY = "请先配置 base_url 和 API Key"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in ("1", "true", "yes", "on")


def _row_value(row, key, default=""):
    if row is None:
        return default
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _llm_settings_row():
    with connect() as conn:
        return conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()


def _copilot_enabled(row) -> bool:
    value = _row_value(row, "copilot_enabled", 1)
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return True


LLM_DEFAULT_TIMEOUT_MS = 60000
LLM_DEFAULT_RETRIES = 3
LLM_TIMEOUT_RANGE_MS = (1000, 600000)
LLM_RETRIES_RANGE = (0, 10)


def _coerce_int(value, default: int, low: int, high: int) -> int:
    """非法值回退缺省，合法值钳制到范围内。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def _llm_timeout_ms(row) -> int:
    return _coerce_int(_row_value(row, "timeout", None), LLM_DEFAULT_TIMEOUT_MS, *LLM_TIMEOUT_RANGE_MS)


def _llm_retries(row) -> int:
    return _coerce_int(_row_value(row, "retries", None), LLM_DEFAULT_RETRIES, *LLM_RETRIES_RANGE)


def _public_llm_settings(row) -> dict:
    return {
        "enabled": _copilot_enabled(row),
        "base_url": str(_row_value(row, "base_url") or ""),
        "model": str(_row_value(row, "model") or ""),
        "has_key": bool(_row_value(row, "api_key")),
        "default_system_prompt": str(_row_value(row, "default_system_prompt") or ""),
        "timeout": _llm_timeout_ms(row),
        "retries": _llm_retries(row),
    }


def _copilot_settings_error(settings) -> str | None:
    if not _copilot_enabled(settings):
        return MSG_COPILOT_DISABLED
    if not settings or not settings["base_url"] or not settings["api_key"] or not settings["model"]:
        return MSG_LLM_UNCONFIGURED
    return None


def _retain_blank_api_key(existing, incoming) -> str:
    """空白或未提供的 api_key 视为未改，保留已保存密钥。"""
    if incoming is None:
        return existing or ""
    text = str(incoming).strip()
    return text if text else (existing or "")


def _save_llm_settings_payload(body: dict) -> dict:
    body = body if isinstance(body, dict) else {}
    with connect() as conn:
        row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        base_url = str(body["base_url"]).strip() if "base_url" in body else (_row_value(row, "base_url") or "")
        model = str(body["model"]).strip() if "model" in body else (_row_value(row, "model") or "")
        prompt = str(body["default_system_prompt"]) if "default_system_prompt" in body else (_row_value(row, "default_system_prompt") or "")
        enabled = 1 if _copilot_enabled(row) else 0
        if "enabled" in body:
            enabled = 1 if _truthy(body.get("enabled")) else 0
        incoming_key = body.get("api_key") if "api_key" in body else None
        api_key = _retain_blank_api_key(_row_value(row, "api_key") or "", incoming_key)
        timeout = _llm_timeout_ms(row)
        retries = _llm_retries(row)
        if "timeout" in body:
            timeout = _coerce_int(body.get("timeout"), timeout, *LLM_TIMEOUT_RANGE_MS)
        if "retries" in body:
            retries = _coerce_int(body.get("retries"), retries, *LLM_RETRIES_RANGE)
        conn.execute(
            """
            UPDATE llm_settings
            SET base_url=?, model=?, api_key=?, default_system_prompt=?, copilot_enabled=?, timeout=?, retries=?
            WHERE id=1
            """,
            (base_url, model, api_key, prompt, enabled, timeout, retries),
        )
        row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    return _public_llm_settings(row)


@app.get("/api/copilot/settings")
def api_copilot_settings_get():
    return JSONResponse(_public_llm_settings(_llm_settings_row()))


@app.post("/api/copilot/settings")
def api_copilot_settings_post(request_body: dict | None = None):
    settings = _save_llm_settings_payload(request_body or {})
    return JSONResponse({"ok": True, "settings": settings})


@app.get("/api/copilot/models")
def api_copilot_models():
    settings = _llm_settings_row()
    if not settings or not settings["base_url"] or not settings["api_key"]:
        return JSONResponse({"error": MSG_NEED_URL_KEY}, status_code=400)
    timeout_seconds = max(1, _llm_timeout_ms(settings) // 1000)
    try:
        models = list_models(settings["base_url"], settings["api_key"], timeout=timeout_seconds)
    except Exception as exc:
        return JSONResponse({"error": _gacha_error_message(exc, settings["api_key"] or "")}, status_code=500)
    return JSONResponse({"models": models})


@app.post("/api/copilot/test")
def api_copilot_test():
    settings = _llm_settings_row()
    error = _copilot_settings_error(settings)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    timeout_seconds = max(1, _llm_timeout_ms(settings) // 1000)
    attempts = _llm_retries(settings) + 1
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            answer = chat_completion_messages(
                settings["base_url"],
                settings["api_key"],
                settings["model"],
                [{"role": "user", "content": "Hi"}],
                max_tokens=5,
                timeout=timeout_seconds,
            )
            return JSONResponse({"ok": True, "result": answer})
        except Exception as exc:
            last_exc = exc
    return JSONResponse({"error": _gacha_error_message(last_exc, settings["api_key"] or "")}, status_code=500)


@app.get("/api/tags/page")
def api_tags_page(
    q: str = "",
    category: str = "",
    subcategory: str = "",
    offset: int = 0,
    limit: int = 80,
    include_count: bool = False,
):
    query, count_query, params, count_params, offset, limit = get_tag_rows(q, category, subcategory, offset, limit)
    # 多取一行探测是否还有后续批次，分页默认不再重复执行 COUNT
    params[-2] = limit + 1
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        has_more = len(rows) > limit
        payload = {"rows": rows[:limit], "next_offset": offset + min(len(rows), limit), "has_more": has_more}
        if include_count:
            payload["shown_count"] = conn.execute(count_query, count_params).fetchone()[0]
    return JSONResponse(payload)


@app.get("/api/tags/filter")
def api_tags_filter(q: str = "", category: str = "", subcategory: str = "", limit: int = 80):
    """筛选局部刷新：一次返回首批行、分类统计与计数，前端不再解析整页 HTML。"""
    with connect() as conn:
        rows, shown_count, total_count, categories, subcategories = get_tags_page_data(
            conn, q, category, subcategory, 0, limit
        )
    return JSONResponse(
        {
            "rows": [dict(r) for r in rows],
            "next_offset": len(rows),
            "has_more": len(rows) < shown_count,
            "shown_count": shown_count,
            "total_count": total_count,
            "q": q,
            "category": category,
            "subcategory": subcategory,
            "categories": [{"category": r["category"], "count": r["count"]} for r in categories],
            "subcategories": [{"subcategory": r["subcategory"], "count": r["count"]} for r in subcategories],
        }
    )


@app.get("/api/tags/lookup")
def api_tags_lookup(q: str = ""):
    tags = [t.strip() for t in q.split(",") if t.strip()]
    if not tags:
        return {}
    placeholders = ",".join("?" for _ in tags)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT tag, zh, category, subcategory FROM tags WHERE tag IN ({placeholders})",
            tags,
        ).fetchall()
    return {r["tag"]: dict(r) for r in rows}


@app.get("/api/characters")
def api_characters():
    return JSONResponse(get_characters_data())


@app.get("/api/recipes")
def api_recipes(type: str = ""):
    return JSONResponse(get_recipes_data(type))


@app.post("/api/llm/optimize")
def api_llm_optimize(request_body: dict):
    prompt_text = request_body.get("prompt", "")
    with connect() as conn:
        settings = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    if not settings["base_url"] or not settings["model"]:
        return JSONResponse({"error": "LLM 未配置，请先在设置中配置 API"}, status_code=400)
    system = settings["default_system_prompt"] or "你是提示词优化助手，帮助用户优化 Stable Diffusion 提示词。保持英文 tag 格式，用逗号分隔。"
    user_msg = f"请优化以下提示词，使其更精确、更有表现力，保持相同风格和主题：\n\n{prompt_text}"
    try:
        answer = chat_completion(settings["base_url"], settings["api_key"], settings["model"], system, user_msg)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"result": answer})


# ─── 抽卡（AI 提示词生成器）───────────────────────────────────────────────────

GACHA_INDEX = BASE_DIR / "static" / "gacha" / "index.html"

# 抽卡键值存储的拒绝名单：API Key 相关只存服务端 llm_settings，绝不落 gacha_store。
GACHA_STORE_KEY_DENYLIST = ("sd_api_key", "sd_api_keys")


def _gacha_key_allowed(key: str) -> bool:
    key = (key or "").strip()
    if not key.startswith("sd_"):
        return False
    return not any(bad in key for bad in GACHA_STORE_KEY_DENYLIST)


def _gacha_error_message(exc: Exception, api_key: str = "") -> str:
    """返回可展示的错误文本，并兜底移除服务端密钥。"""
    message = str(exc)
    return message.replace(api_key, "***") if api_key else message


@app.get("/gacha", response_class=HTMLResponse)
def gacha_page():
    return FileResponse(GACHA_INDEX)


@app.get("/api/gacha/settings")
def api_gacha_settings_get():
    with connect() as conn:
        settings = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    return JSONResponse({
        "base_url": settings["base_url"] or "",
        "model": settings["model"] or "",
        "has_key": bool(settings["api_key"]),
    })


@app.post("/api/gacha/settings")
def api_gacha_settings_post(request_body: dict):
    base_url = str(request_body.get("base_url", "")).strip()
    model = str(request_body.get("model", "")).strip()
    api_key = request_body.get("api_key")
    with connect() as conn:
        if api_key is None:
            conn.execute("UPDATE llm_settings SET base_url=?, model=? WHERE id=1", (base_url, model))
        else:
            conn.execute(
                "UPDATE llm_settings SET base_url=?, model=?, api_key=? WHERE id=1",
                (base_url, model, str(api_key)),
            )
    return JSONResponse({"ok": True})


@app.post("/api/gacha/llm")
def api_gacha_llm(request_body: dict):
    messages = request_body.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or any(not isinstance(item, dict) or not item.get("role") or "content" not in item for item in messages)
    ):
        return JSONResponse({"error": "messages 不能为空"}, status_code=400)
    with connect() as conn:
        settings = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    if not settings["base_url"] or not settings["api_key"] or not settings["model"]:
        return JSONResponse({"error": "LLM 未配置，请先在抽卡设置或衣柜 LLM 页面填写 API"}, status_code=400)
    try:
        answer = chat_completion_messages(
            settings["base_url"],
            settings["api_key"],
            settings["model"],
            messages,
            temperature=request_body.get("temperature", 0.7),
            top_p=request_body.get("top_p"),
            frequency_penalty=request_body.get("frequency_penalty"),
            presence_penalty=request_body.get("presence_penalty"),
            max_tokens=request_body.get("max_tokens", 8192),
        )
    except Exception as exc:
        return JSONResponse({"error": _gacha_error_message(exc, settings["api_key"])}, status_code=500)
    return JSONResponse({"result": answer})


@app.get("/api/gacha/models")
def api_gacha_models():
    with connect() as conn:
        settings = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
    if not settings["base_url"] or not settings["api_key"]:
        return JSONResponse({"error": "请先配置 base_url 和 API Key"}, status_code=400)
    try:
        models = list_models(settings["base_url"], settings["api_key"])
    except Exception as exc:
        return JSONResponse({"error": _gacha_error_message(exc, settings["api_key"])}, status_code=500)
    return JSONResponse({"models": models})


@app.get("/api/gacha/store")
def api_gacha_store_get():
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM gacha_store").fetchall()
    return JSONResponse({r["key"]: r["value"] for r in rows if _gacha_key_allowed(r["key"])})


@app.post("/api/gacha/store")
def api_gacha_store_post(request_body: dict):
    key = str(request_body.get("key", "")).strip()
    value = request_body.get("value", "")
    if not _gacha_key_allowed(key):
        return JSONResponse({"ok": False, "skipped": True})
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM gacha_store WHERE key=?", (key,))
        else:
            conn.execute(
                """
                INSERT INTO gacha_store (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (key, str(value)),
            )
    return JSONResponse({"ok": True})

from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any

from PIL import Image

from .db import BASE_DIR, connect, init_db
from .gallery import GALLERY_DIR, IMAGE_EXTENSIONS, extract_prompts, ingest_saved_paths, read_image_metadata, safe_gallery_relative_path

_SCAN_LOCK = threading.Lock()


def get_default_output_dir() -> Path:
    """自动定位 ComfyUI 输出目录或系统输出目录。"""
    # 1. 环境变量优先
    env_dir = os.environ.get("COMFYUI_OUTPUT_DIR", "").strip()
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p

    # 2. 秋叶整合包目录结构探测：BASE_DIR.parents[2] / "ComfyUI" / "output"
    try:
        aki_output = BASE_DIR.parents[2] / "ComfyUI" / "output"
        if aki_output.is_dir():
            return aki_output
    except Exception:
        pass

    # 3. 降级为 tag_manager 自身的图库目录
    return GALLERY_DIR


def get_configured_output_dir() -> Path:
    """获取当前配置的生图输出目录（支持数据库持久化自定义目录）。"""
    with connect() as conn:
        row = conn.execute("SELECT value FROM gacha_store WHERE key = 'output_dir'").fetchone()
        if row and row["value"]:
            custom = Path(row["value"].strip())
            if custom.is_dir():
                return custom
    return get_default_output_dir()


def set_configured_output_dir(path_str: str) -> Path:
    """设置并持久化自定义生图输出目录。"""
    p = Path(path_str.strip()).resolve()
    if not p.is_dir():
        raise ValueError(f"指定的输出目录不存在：{path_str}")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO gacha_store (key, value, updated_at)
            VALUES ('output_dir', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (str(p),),
        )
    return p


def extract_file_date_and_time(path: Path) -> tuple[str, str, float]:
    """提取文件的生成日期与时间戳。优先从文件 mtime 提取，同时智能识别诸如 '26_07_27' 或 '2026-09-13' 等目录名。"""
    stat = path.stat()
    mtime = stat.st_mtime
    dt = datetime.fromtimestamp(mtime)
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H:%M:%S")

    # 检查父目录是否形如 26_07_27 (YY_MM_DD)
    parent_name = path.parent.name
    m_short = re.match(r"^(\d{2})_(\d{2})_(\d{2})$", parent_name)
    if m_short:
        yy, mm, dd = m_short.groups()
        year = int(yy) + 2000 if int(yy) < 70 else int(yy) + 1900
        date_str = f"{year:04d}-{int(mm):02d}-{int(dd):02d}"
    else:
        m_full = re.match(r"^(\d{4})[-_](\d{2})[-_](\d{2})$", parent_name)
        if m_full:
            y, m_val, d_val = m_full.groups()
            date_str = f"{int(y):04d}-{int(m_val):02d}-{int(d_val):02d}"

    return date_str, time_str, mtime


def resolve_safe_output_file(rel_path: str, output_dir: Path | None = None) -> Path:
    """严格校验并解析生图文件相对路径，防止路径遍历攻击。"""
    raw = str(rel_path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("图片路径不合法")
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError("图片路径不合法")

    root = (output_dir or get_configured_output_dir()).resolve()
    target = (root / Path(*parts)).resolve()
    if target != root and root not in target.parents:
        raise ValueError("禁止访问输出目录以外的文件")
    if not target.is_file():
        raise FileNotFoundError("图片文件不存在")
    if target.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("不支持的图片格式")
    return target


def ingest_output_image(conn, path: Path, root: Path) -> None:
    """单张生图文件解析入库。"""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return

    stat = path.stat()
    date_str, time_str, mtime = extract_file_date_and_time(path)

    # 提取分辨率
    width, height = 0, 0
    try:
        with Image.open(path) as img:
            width, height = img.size
    except Exception:
        pass

    # 读取元数据
    meta = read_image_metadata(path)
    positive, negative, workflow_raw, prompt_raw, checkpoint, loras, parameters, metadata_json, metadata_source, generation_params = extract_prompts(meta)

    conn.execute(
        """
        INSERT INTO output_images
            (rel_path, filename, file_date, file_time, file_mtime, file_size, width, height,
             positive_prompt, negative_prompt, checkpoint, loras, workflow_json, prompt_json,
             parameters, generation_params, metadata_source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(rel_path) DO UPDATE SET
            filename=excluded.filename,
            file_date=excluded.file_date,
            file_time=excluded.file_time,
            file_mtime=excluded.file_mtime,
            file_size=excluded.file_size,
            width=excluded.width,
            height=excluded.height,
            positive_prompt=excluded.positive_prompt,
            negative_prompt=excluded.negative_prompt,
            checkpoint=excluded.checkpoint,
            loras=excluded.loras,
            workflow_json=excluded.workflow_json,
            prompt_json=excluded.prompt_json,
            parameters=excluded.parameters,
            generation_params=excluded.generation_params,
            metadata_source=excluded.metadata_source,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            rel,
            path.name,
            date_str,
            time_str,
            mtime,
            stat.st_size,
            width,
            height,
            positive,
            negative,
            checkpoint,
            loras,
            workflow_raw,
            prompt_raw,
            parameters,
            generation_params,
            metadata_source,
        ),
    )


def scan_outputs(output_dir: Path | None = None, limit_new: int | None = None) -> dict[str, int]:
    """增量扫描生图输出目录，通过文件指纹快速跳过未变化的文件。"""
    root = (output_dir or get_configured_output_dir()).resolve()
    if not root.is_dir():
        return {"scanned": 0, "ingested": 0, "deleted": 0}

    with _SCAN_LOCK:
        init_db()
        with connect() as conn:
            fingerprints = {
                row["rel_path"]: (row["file_mtime"], row["file_size"])
                for row in conn.execute("SELECT rel_path, file_mtime, file_size FROM output_images").fetchall()
            }
            seen_rel_paths: set[str] = set()
            new_count = 0
            total_found = 0

            for file_path in root.rglob("*"):
                if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                total_found += 1
                try:
                    rel = file_path.relative_to(root).as_posix()
                except ValueError:
                    continue

                seen_rel_paths.add(rel)
                stat = file_path.stat()
                if fingerprints.get(rel) == (stat.st_mtime, stat.st_size):
                    continue

                ingest_output_image(conn, file_path, root)
                new_count += 1
                if limit_new is not None and new_count >= limit_new:
                    break

            # 清理已在磁盘上删除的文件（仅当扫描配置的根目录时）
            deleted_count = 0
            if root == get_configured_output_dir().resolve():
                known_paths = set(fingerprints)
                missing = known_paths - seen_rel_paths
                if missing:
                    conn.executemany("DELETE FROM output_images WHERE rel_path = ?", [(p,) for p in missing])
                    deleted_count = len(missing)

            return {"scanned": total_found, "ingested": new_count, "deleted": deleted_count}


def format_date_label(date_str: str) -> str:
    """生成友好的日期展示文本，如 '今天 (2026-09-13)'、'昨天'。"""
    today_str = date.today().strftime("%Y-%m-%d")
    if date_str == today_str:
        return f"今天 ({date_str})"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        delta = (date.today() - dt).days
        if delta == 1:
            return f"昨天 ({date_str})"
        elif delta == 2:
            return f"前天 ({date_str})"
        weekday_map = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}
        return f"{date_str} ({weekday_map.get(dt.weekday(), '')})"
    except Exception:
        return date_str


def get_output_dates() -> list[dict[str, Any]]:
    """获取所有生图日期列表及每日图片数量。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT file_date, count(*) as count, max(file_mtime) as latest_mtime
            FROM output_images
            GROUP BY file_date
            ORDER BY file_date DESC
            """
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["label"] = format_date_label(d["file_date"])
            result.append(d)
        return result


def query_output_images(
    date: str = "",
    q: str = "",
    checkpoint: str = "",
    lora: str = "",
    order: str = "desc",
    limit: int = 150,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """多维查询生图卡片，支持按日期精确过滤与关键词检索。"""
    where_clauses = ["1=1"]
    params: list[Any] = []

    if date:
        where_clauses.append("file_date = ?")
        params.append(date)

    if q:
        like = f"%{q.strip()}%"
        where_clauses.append("(filename LIKE ? OR positive_prompt LIKE ? OR checkpoint LIKE ? OR loras LIKE ?)")
        params.extend([like, like, like, like])

    if checkpoint:
        where_clauses.append("checkpoint LIKE ?")
        params.append(f"%{checkpoint.strip()}%")

    if lora:
        where_clauses.append("loras LIKE ?")
        params.append(f"%{lora.strip()}%")

    where_sql = " AND ".join(where_clauses)
    order_sql = "file_mtime DESC" if order.lower() == "desc" else "file_mtime ASC"

    with connect() as conn:
        total_count = conn.execute(f"SELECT count(*) FROM output_images WHERE {where_sql}", params).fetchone()[0]
        query_sql = f"""
            SELECT id, rel_path, filename, file_date, file_time, file_mtime, file_size,
                   width, height, positive_prompt, negative_prompt, checkpoint, loras,
                   parameters, generation_params, metadata_source,
                   CASE WHEN workflow_json != '' THEN 1 ELSE 0 END as has_workflow,
                   CASE WHEN prompt_json != '' THEN 1 ELSE 0 END as has_prompt_json
            FROM output_images
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, [*params, limit, offset]).fetchall()
        return [dict(r) for r in rows], total_count


def get_output_image_detail(image_id: int | None = None, rel_path: str | None = None) -> dict[str, Any] | None:
    """获取单张生图的完整元数据与工作流。"""
    with connect() as conn:
        if image_id is not None:
            row = conn.execute("SELECT * FROM output_images WHERE id = ?", (image_id,)).fetchone()
        elif rel_path:
            row = conn.execute("SELECT * FROM output_images WHERE rel_path = ?", (rel_path,)).fetchone()
        else:
            return None
        return dict(row) if row else None


def favorite_to_gallery(rel_path: str, category: str = "生图精选", output_dir: Path | None = None) -> dict[str, Any]:
    """将生图输出中的优质图片一键收藏到图库（tag_manager/gallery/），并保持提示词元数据完整。"""
    source_file = resolve_safe_output_file(rel_path, output_dir=output_dir)
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)

    category_clean = re.sub(r'[\\/:*?"<>|]+', "_", category).strip("._ ") or "生图精选"
    target_dir = GALLERY_DIR / category_clean
    target_dir.mkdir(parents=True, exist_ok=True)

    dest_name = safe_gallery_relative_path(f"{category_clean}/{source_file.name}")
    dest_path = GALLERY_DIR / dest_name

    # 若已存在同名文件，生成不冲突的命名
    if dest_path.exists():
        stem = source_file.stem
        suffix = source_file.suffix
        for idx in range(2, 1000):
            candidate = target_dir / f"{stem}_{idx}{suffix}"
            if not candidate.exists():
                dest_path = candidate
                break

    # 纯复制，绝不移动或删除源文件
    shutil.copy2(source_file, dest_path)

    # 录入图库数据库
    ingest_saved_paths([dest_path], initialize_db=True)
    return {
        "ok": True,
        "source": rel_path,
        "gallery_path": dest_path.relative_to(GALLERY_DIR).as_posix(),
        "category": category_clean,
    }


def reparse_all_outputs(output_dir: Path | None = None) -> dict[str, int]:
    """深度重新解析所有已收录的生图文件，强制提取精准的正负面提示词与 LoRA 列表并更新数据库。"""
    root = (output_dir or get_configured_output_dir()).resolve()
    if not root.is_dir():
        return {"total": 0, "updated": 0}

    with _SCAN_LOCK:
        init_db()
        with connect() as conn:
            rows = conn.execute("SELECT id, rel_path FROM output_images").fetchall()
            updated_count = 0
            for row in rows:
                image_id = row["id"]
                rel_path = row["rel_path"]
                try:
                    full_path = resolve_safe_output_file(rel_path, output_dir=root)
                except Exception:
                    continue

                meta = read_image_metadata(full_path)
                positive, negative, workflow_raw, prompt_raw, checkpoint, loras, parameters, metadata_json, metadata_source, generation_params = extract_prompts(meta)

                conn.execute(
                    """
                    UPDATE output_images
                    SET positive_prompt = ?,
                        negative_prompt = ?,
                        checkpoint = ?,
                        loras = ?,
                        parameters = ?,
                        generation_params = ?,
                        metadata_source = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (positive, negative, checkpoint, loras, parameters, generation_params, metadata_source, image_id),
                )
                updated_count += 1

            return {"total": len(rows), "updated": updated_count}

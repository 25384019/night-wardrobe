from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any

from PIL import Image

from .db import BASE_DIR, connect, init_db
from .gallery import GALLERY_DIR, IMAGE_EXTENSIONS, extract_prompts, extract_structured_prompt, ingest_saved_paths, read_image_metadata, safe_gallery_relative_path

_SCAN_LOCK = threading.Lock()
_COMFY_PYTHON = BASE_DIR.parents[2] / "python" / "python.exe"
_WD14_MODEL_DIR = BASE_DIR.parents[2] / "ComfyUI" / "custom_nodes" / "ComfyUI-WD14-Tagger" / "models"
_SAFETY_WORKER = BASE_DIR / "local_safety_worker.py"
_REPARSE_WATERMARK_KEY = "outputs_reparse_last_completed_at"
_SAFETY_WATERMARK_KEY = "outputs_wd14_last_completed_at"


def _get_processing_watermark(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM gacha_store WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row and row["value"] else ""


def _set_processing_watermark(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO gacha_store (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """,
        (key, value),
    )


def _get_incremental_rows(conn, watermark_key: str, columns: str):
    """返回上次完成后新入库或文件有变动的记录；首次启用只建立水位线。"""
    watermark = _get_processing_watermark(conn, watermark_key)
    if not watermark:
        baseline = conn.execute("SELECT max(updated_at) FROM output_images").fetchone()[0] or ""
        _set_processing_watermark(conn, watermark_key, baseline)
        existing = conn.execute("SELECT count(*) FROM output_images").fetchone()[0]
        return [], True, existing

    rows = conn.execute(
        f"SELECT {columns} FROM output_images WHERE updated_at > ? ORDER BY updated_at, id",
        (watermark,),
    ).fetchall()
    return rows, False, 0


def _advance_processing_watermark(conn, watermark_key: str, rows: list[Any]) -> None:
    if rows:
        _set_processing_watermark(conn, watermark_key, max(str(row["updated_at"]) for row in rows))


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
    structured = extract_structured_prompt(meta)
    positive, negative, workflow_raw, prompt_raw, checkpoint, loras, parameters, metadata_json, metadata_source, generation_params = extract_prompts(meta)

    cur = conn.execute(
        """
        INSERT INTO output_images
            (rel_path, filename, file_date, file_time, file_mtime, file_size, width, height,
             positive_prompt, negative_prompt, original_positive_prompt, original_negative_prompt,
             prompt_version,
             checkpoint, loras, workflow_json, prompt_json,
             parameters, generation_params, artist_tokens, character_tokens, other_tags, style_unit_json, original_prompt, composed_prompt, metadata_source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(rel_path) DO UPDATE SET
            filename=excluded.filename,
            file_date=excluded.file_date,
            file_time=excluded.file_time,
            file_mtime=excluded.file_mtime,
            file_size=excluded.file_size,
            width=excluded.width,
            height=excluded.height,
            original_positive_prompt=excluded.original_positive_prompt,
            original_negative_prompt=excluded.original_negative_prompt,
            positive_prompt=CASE WHEN output_images.current_prompt_version_id IS NOT NULL AND output_images.prompt_version > 0 THEN output_images.positive_prompt ELSE excluded.positive_prompt END,
            negative_prompt=CASE WHEN output_images.current_prompt_version_id IS NOT NULL AND output_images.prompt_version > 0 THEN output_images.negative_prompt ELSE excluded.negative_prompt END,
            checkpoint=excluded.checkpoint,
            loras=excluded.loras,
            workflow_json=excluded.workflow_json,
            prompt_json=excluded.prompt_json,
            parameters=excluded.parameters,
            generation_params=excluded.generation_params,
            artist_tokens=excluded.artist_tokens,
            character_tokens=excluded.character_tokens,
            other_tags=excluded.other_tags,
            style_unit_json=excluded.style_unit_json,
            composed_prompt=excluded.composed_prompt,
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
            positive,
            negative,
            0,
            checkpoint,
            loras,
            workflow_raw,
            prompt_raw,
            parameters,
            generation_params,
            ", ".join(structured["artist_tokens"]),
            ", ".join(structured["character_tokens"]),
            ", ".join(structured["other_tags"]),
            structured["style_unit_json"],
            structured["original_prompt"],
            structured["composed_prompt"],
            metadata_source,
        ),
    )

    img_id = cur.lastrowid
    if not img_id:
        r = conn.execute("SELECT id FROM output_images WHERE rel_path = ?", (rel,)).fetchone()
        if r:
            img_id = r[0]

    if img_id:
        v0 = conn.execute("SELECT id FROM prompt_versions WHERE image_id = ? AND version_number = 0", (img_id,)).fetchone()
        if not v0:
            v0_cur = conn.execute(
                """
                INSERT INTO prompt_versions (
                    image_id, version_number, parent_version_id,
                    positive_prompt, negative_prompt, instruction,
                    is_original, applied_by
                ) VALUES (?, 0, NULL, ?, ?, 'Original from metadata', 1, 'original')
                """,
                (img_id, positive, negative),
            )
            v0_id = v0_cur.lastrowid
            conn.execute(
                "UPDATE output_images SET current_prompt_version_id = ?, prompt_version = CASE WHEN prompt_version IS NULL OR prompt_version <= 0 THEN 0 ELSE prompt_version END WHERE id = ? AND (current_prompt_version_id IS NULL OR current_prompt_version_id = 0)",
                (v0_id, img_id),
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
                   parameters, generation_params, metadata_source, safety_level, safety_source,
                   CASE WHEN workflow_json != '' THEN 1 ELSE 0 END as has_workflow,
                   CASE WHEN prompt_json != '' THEN 1 ELSE 0 END as has_prompt_json
            FROM output_images
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, [*params, limit, offset]).fetchall()
        images = [dict(r) for r in rows]
        for image in images:
            image["safety_level"] = image.get("safety_level") or classify_prompt_safety(image.get("positive_prompt", ""))
        return images, total_count


def get_output_image_detail(image_id: int | None = None, rel_path: str | None = None) -> dict[str, Any] | None:
    """获取单张生图的完整元数据与工作流。"""
    with connect() as conn:
        if image_id is not None:
            row = conn.execute("SELECT * FROM output_images WHERE id = ?", (image_id,)).fetchone()
        elif rel_path:
            row = conn.execute("SELECT * FROM output_images WHERE rel_path = ?", (rel_path,)).fetchone()
        else:
            return None
        if not row:
            return None
        detail = dict(row)
        _attach_lora_library_tags(conn, detail)
        detail["safety_level"] = detail.get("safety_level") or classify_prompt_safety(detail.get("positive_prompt", ""))
        return detail


def _lora_key(value: str) -> str:
    clean = str(value or "").strip().casefold()
    clean = re.sub(r"^<lora:", "", clean)
    clean = re.sub(r":-?\d+(?:\.\d+)?>?$", "", clean)
    clean = re.sub(r"\.safetensors$", "", clean)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", clean)


def _comma_tokens(value: str) -> list[str]:
    return [token.strip() for token in str(value or "").split(",") if token.strip()]


def _unique_tokens(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _attach_lora_library_tags(conn, detail: dict[str, Any]) -> None:
    lora_keys = {_lora_key(value) for value in _comma_tokens(detail.get("loras", ""))}
    lora_keys.discard("")
    matched: list[dict[str, str]] = []
    if lora_keys:
        rows = conn.execute("SELECT name, filename, trigger_words FROM lora_cards").fetchall()
        for row in rows:
            card = dict(row)
            card_keys = {_lora_key(card.get("name", "")), _lora_key(card.get("filename", ""))}
            card_keys.discard("")
            if lora_keys.intersection(card_keys):
                matched.append(card)

    triggers = _unique_tokens([token for card in matched for token in _comma_tokens(card.get("trigger_words", ""))])
    artist_strings = _unique_tokens([str(card.get("name", "")).strip() for card in matched if str(card.get("name", "")).strip()])
    image_tokens = _comma_tokens(detail.get("positive_prompt", ""))
    trigger_keys = {token.casefold() for token in triggers}
    character_tokens = [token for token in image_tokens if token.casefold() not in trigger_keys]

    detail["matched_loras"] = matched
    detail["lora_trigger_words"] = ", ".join(triggers)
    detail["lora_artist_strings"] = "；".join(artist_strings)
    detail["lora_style_tags"] = ", ".join([*artist_strings, *triggers])
    detail["image_tags"] = ", ".join(image_tokens)
    detail["character_tags"] = ", ".join(character_tokens)


def set_output_safety_level(image_id: int, level: str) -> dict[str, Any] | None:
    """保存人工评级；后续自动分析不会覆盖人工确认的结果。"""
    if level not in {"normal", "suspicious", "nsfw"}:
        raise ValueError("评级只能是正常、可疑或 NSFW")
    with connect() as conn:
        updated = conn.execute(
            "UPDATE output_images SET safety_level = ?, safety_source = '手动' WHERE id = ?",
            (level, image_id),
        ).rowcount
    if not updated:
        return None
    return get_output_image_detail(image_id=image_id)


_NSFW_PROMPT_TAGS = {
    "nude", "naked", "nipples", "areola", "areolae", "pussy", "vagina",
    "penis", "genitals", "sex", "sexual intercourse", "masturbation", "cum",
    "oral", "fellatio", "cunnilingus", "anal", "spread pussy", "rating:explicit",
}
_SUSPICIOUS_PROMPT_TAGS = {
    "nsfw", "rating:questionable", "rating:sensitive", "suggestive", "bikini",
    "micro bikini", "swimsuit", "underwear", "panties", "bra", "lingerie",
    "cleavage", "sideboob", "underboob", "see-through", "midriff", "thighhighs",
}


def classify_prompt_safety(positive_prompt: str) -> str:
    """Classify embedded positive prompt metadata without inspecting the image."""
    if not positive_prompt or not positive_prompt.strip():
        return "suspicious"
    normalized = positive_prompt.lower().replace("_", " ")
    normalized = re.sub(r"[，、；;|/�]+", ",", normalized)
    tags = {re.sub(r"^[\s({\[]+|[\s)}\]]+$", "", tag).strip() for tag in normalized.split(",")}
    if any(tag in tags for tag in _NSFW_PROMPT_TAGS):
        return "nsfw"
    if any(tag in tags for tag in _SUSPICIOUS_PROMPT_TAGS):
        return "suspicious"
    return "normal"


def analyze_new_output_safety_with_wd14(output_dir: Path | None = None) -> dict[str, Any]:
    """仅用本机 WD14 识别上次完成后新增或变更的图片，图片不会离开本机。"""
    root = (output_dir or get_configured_output_dir()).resolve()
    if not _COMFY_PYTHON.is_file() or not _WD14_MODEL_DIR.is_dir() or not _SAFETY_WORKER.is_file():
        raise RuntimeError("未找到本机 WD14 离线模型")

    with _SCAN_LOCK:
        with connect() as conn:
            rows, baseline_created, skipped_existing = _get_incremental_rows(
                conn,
                _SAFETY_WATERMARK_KEY,
                "id, rel_path, positive_prompt, safety_source, updated_at",
            )
            if baseline_created:
                return {"total": 0, "updated": 0, "baseline": True, "skipped_existing": skipped_existing}
            if not rows:
                return {"total": 0, "updated": 0, "baseline": False, "skipped_existing": 0}
        valid_rows: list[tuple[Any, Path]] = []
        for row in rows:
            try:
                valid_rows.append((row, resolve_safe_output_file(row["rel_path"], output_dir=root)))
            except (FileNotFoundError, ValueError):
                continue

        payload = {"paths": [str(path) for _, path in valid_rows], "model_dir": str(_WD14_MODEL_DIR)}
        completed = subprocess.run(
            [str(_COMFY_PYTHON), str(_SAFETY_WORKER)],
            input=json.dumps(payload, ensure_ascii=False), text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("WD14 离线识别未完成")
        try:
            visual_results = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("WD14 返回的数据无效") from exc

        rank = {"normal": 0, "suspicious": 1, "nsfw": 2}
        updated = 0
        with connect() as conn:
            for row, path in valid_rows:
                if row["safety_source"] == "手动":
                    continue
                result = visual_results.get(str(path), {})
                visual_level = result.get("level")
                if visual_level not in rank:
                    continue
                source_level = classify_prompt_safety(row["positive_prompt"])
                final_level = visual_level if rank[visual_level] >= rank[source_level] else source_level
                conn.execute(
                    "UPDATE output_images SET safety_level = ?, safety_source = ? WHERE id = ?",
                    (final_level, "WD14+图源", row["id"]),
                )
                updated += 1
            _advance_processing_watermark(conn, _SAFETY_WATERMARK_KEY, rows)
        return {"total": len(valid_rows), "updated": updated, "baseline": False, "skipped_existing": 0}


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


def reparse_new_outputs(output_dir: Path | None = None) -> dict[str, Any]:
    """深度解析上次完成后新增或变更的生图文件，旧图库只保留为基线。"""
    root = (output_dir or get_configured_output_dir()).resolve()
    if not root.is_dir():
        return {"total": 0, "updated": 0, "baseline": False, "skipped_existing": 0}

    with _SCAN_LOCK:
        init_db()
        with connect() as conn:
            rows, baseline_created, skipped_existing = _get_incremental_rows(
                conn,
                _REPARSE_WATERMARK_KEY,
                "id, rel_path, updated_at",
            )
            if baseline_created:
                return {"total": 0, "updated": 0, "baseline": True, "skipped_existing": skipped_existing}
            updated_count = 0
            for row in rows:
                image_id = row["id"]
                rel_path = row["rel_path"]
                try:
                    full_path = resolve_safe_output_file(rel_path, output_dir=root)
                except Exception:
                    continue

                meta = read_image_metadata(full_path)
                structured = extract_structured_prompt(meta)
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
                        artist_tokens = ?,
                        character_tokens = ?,
                        other_tags = ?,
                        style_unit_json = ?,
                        original_prompt = ?,
                        composed_prompt = ?,
                        metadata_source = ?
                    WHERE id = ?
                    """,
                    (positive, negative, checkpoint, loras, parameters, generation_params,
                     ", ".join(structured["artist_tokens"]), ", ".join(structured["character_tokens"]),
                     ", ".join(structured["other_tags"]), structured["style_unit_json"],
                     structured["original_prompt"], structured["composed_prompt"], metadata_source, image_id),
                )
                updated_count += 1

            _advance_processing_watermark(conn, _REPARSE_WATERMARK_KEY, rows)
            return {"total": len(rows), "updated": updated_count, "baseline": False, "skipped_existing": 0}

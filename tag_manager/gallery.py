from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from .db import BASE_DIR, PROJECT_DIR, connect, init_db
from .style_units import structure_prompt, style_unit_json

GALLERY_DIR = BASE_DIR / "gallery"
OLD_GALLERY_DIR = PROJECT_DIR / "提示词图库"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
LORA_RE = re.compile(r"<lora:([^:>]+)(?::([^>]+))?>", re.IGNORECASE)
USER_COMMENT_TAG = 37510
_SCAN_LOCK = threading.Lock()


def ensure_gallery_dir() -> None:
    GALLERY_DIR.mkdir(exist_ok=True)


def iter_images(root: Path = GALLERY_DIR):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def clean_exif_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        prefixes = [
            (b"UNICODE\x00", "utf-16-be"),
            (b"ASCII\x00\x00\x00", "utf-8"),
            (b"JIS\x00\x00\x00\x00\x00", "shift_jis"),
        ]
        for prefix, encoding in prefixes:
            if value.startswith(prefix):
                value = value[len(prefix):]
                try:
                    return value.decode(encoding, errors="ignore").replace("\x00", "").strip()
                except Exception:
                    return ""
        for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be"):
            try:
                return value.decode(encoding, errors="ignore").replace("\x00", "").strip()
            except Exception:
                continue
        return ""
    return str(value).replace("\x00", "").strip()


def metadata_value_to_text(value: Any) -> str:
    if isinstance(value, bytes):
        text = clean_exif_text(value)
        return text if text else value.hex()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def read_image_metadata(path: Path) -> dict[str, str]:
    try:
        with Image.open(path) as img:
            meta = {str(k): metadata_value_to_text(v) for k, v in img.info.items() if k != "exif"}
            source = "Pillow"
            try:
                exif = img.getexif()
            except Exception:
                exif = None
            if exif:
                for key, value in exif.items():
                    label = str(key)
                    text = clean_exif_text(value)
                    if text:
                        meta.setdefault(label, text)
                    if key == USER_COMMENT_TAG and text:
                        meta["parameters"] = text
                        source = "EXIF UserComment"
            if "workflow" in meta or "prompt" in meta:
                source = "ComfyUI"
            elif "parameters" in meta and source != "EXIF UserComment":
                source = "SD WebUI"
            meta["_metadata_source"] = source
            meta["_metadata_json"] = json.dumps({k: v for k, v in meta.items() if not k.startswith("_")}, ensure_ascii=False, indent=2)
            return meta
    except Exception:
        return {}


def parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def get_node_text_from_workflow(workflow_data: dict | None, node_id: str | int) -> str:
    if not isinstance(workflow_data, dict):
        return ""
    target_id = str(node_id)
    for node in workflow_data.get("nodes", []):
        if str(node.get("id")) == target_id:
            wv = node.get("widgets_values")
            if isinstance(wv, list) and len(wv) > 0:
                first = wv[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
                if isinstance(first, list) and len(first) > 0 and isinstance(first[0], str) and first[0].strip():
                    return first[0].strip()
    return ""


def trace_node_text(prompt_data: dict, link: Any, workflow_data: dict | None = None, visited: set | None = None) -> str:
    if visited is None:
        visited = set()

    node_id = ""
    slot = 0
    if isinstance(link, (list, tuple)) and len(link) >= 1:
        node_id = str(link[0])
        slot = int(link[1]) if len(link) > 1 and isinstance(link[1], int) else 0
    elif isinstance(link, str):
        node_id = link
    else:
        return ""

    visit_key = f"{node_id}:{slot}"
    if visit_key in visited or node_id not in prompt_data:
        return ""
    visited.add(visit_key)

    node = prompt_data[node_id]
    if not isinstance(node, dict):
        return ""

    ctype = node.get("class_type", "")
    inputs = node.get("inputs", {})
    if not isinstance(inputs, dict):
        return ""

    # ShowText may cache only a partial value in text_0. Follow its source first.
    if "ShowText" in ctype:
        cached_values = [
            value.strip() for key, value in inputs.items()
            if key.startswith("text_") and isinstance(value, str) and value.strip()
        ]
        text_link = inputs.get("text")
        if isinstance(text_link, list):
            resolved = trace_node_text(prompt_data, text_link, workflow_data, visited)
            candidates = [value for value in [resolved, *cached_values] if value]
            if candidates:
                return max(candidates, key=len)
        if cached_values:
            return max(cached_values, key=len)

    # Gallery nodes preserve the selected source post and its original prompt.
    selection_data = inputs.get("selection_data")
    if isinstance(selection_data, str) and selection_data.strip():
        try:
            selections = json.loads(selection_data).get("selections", [])
        except (json.JSONDecodeError, AttributeError):
            selections = []
        prompts = [item.get("prompt", "").strip() for item in selections if isinstance(item, dict) and item.get("prompt")]
        if prompts:
            return ", ".join(prompts)

    # PromptBuilder: slot 0 is positive, slot 1 is negative
    if "PromptBuilder" in ctype:
        if slot == 0 and "positive_prompt" in inputs:
            return str(inputs["positive_prompt"]).strip()
        if slot == 1 and "negative_prompt" in inputs:
            return str(inputs["negative_prompt"]).strip()
        if "positive_prompt" in inputs and not slot:
            return str(inputs["positive_prompt"]).strip()
        if "text" in inputs:
            val = inputs["text"]
            if isinstance(val, str):
                return val.strip()
            return trace_node_text(prompt_data, val, workflow_data, visited)

    # PrimitiveStringMultiline, PrimitiveNode, Text, String, etc.
    for val_key in ("value", "text", "string", "prompt", "positive_prompt", "negative_prompt"):
        val = inputs.get(val_key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        elif isinstance(val, list) and len(val) >= 1:
            res = trace_node_text(prompt_data, val, workflow_data, visited)
            if res:
                return res

    # StringConcatenate: joins string_a and string_b
    if "StringConcatenate" in ctype or ("string_a" in inputs and "string_b" in inputs):
        delimiter = inputs.get("delimiter", ", ")
        part_a = trace_node_text(prompt_data, inputs.get("string_a"), workflow_data, visited) if "string_a" in inputs else ""
        part_b = trace_node_text(prompt_data, inputs.get("string_b"), workflow_data, visited) if "string_b" in inputs else ""
        combined = [p for p in (part_a, part_b) if p]
        if combined:
            return delimiter.join(combined)

    # If conditioning connects back to CLIPTextEncode or ConditioningConcat
    for cond_key in ("conditioning", "conditioning_to", "conditioning_from", "clip"):
        cond = inputs.get(cond_key)
        if cond:
            res = trace_node_text(prompt_data, cond, workflow_data, visited)
            if res:
                return res

    # If text is not in inputs or references a dynamic/tagger node (like WD14Tagger), check workflow widgets_values
    if workflow_data:
        wf_text = get_node_text_from_workflow(workflow_data, node_id)
        if wf_text:
            return wf_text

    return ""


def trace_pipe(prompt_data: dict, pipe_link: Any, visited: set | None = None) -> tuple[Any, Any]:
    if visited is None:
        visited = set()
    node_id = str(pipe_link[0]) if isinstance(pipe_link, (list, tuple)) and pipe_link else str(pipe_link)
    if node_id in visited or node_id not in prompt_data:
        return None, None
    visited.add(node_id)
    node = prompt_data[node_id]
    if not isinstance(node, dict):
        return None, None
    inputs = node.get("inputs", {})
    pos = inputs.get("positive") or inputs.get("pos") or inputs.get("positive_prompt")
    neg = inputs.get("negative") or inputs.get("neg") or inputs.get("negative_prompt")
    if pos or neg:
        return pos, neg
    if "pipe" in inputs:
        return trace_pipe(prompt_data, inputs["pipe"], visited)
    return None, None


def find_ksampler_connections(prompt_data: dict, workflow_data: dict | None = None) -> tuple[str, str]:
    if not isinstance(prompt_data, dict):
        return "", ""

    positive = ""
    negative = ""

    sampler_nodes = []
    for nid, node in prompt_data.items():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type", "")
        if "Sampler" in ctype or "Upscale" in ctype:
            sampler_nodes.append((nid, node))

    for nid, node in sampler_nodes:
        inputs = node.get("inputs", {})
        pos_link = inputs.get("positive") or inputs.get("pos")
        neg_link = inputs.get("negative") or inputs.get("neg")

        if not pos_link and not neg_link and "pipe" in inputs:
            p_pos, p_neg = trace_pipe(prompt_data, inputs["pipe"])
            pos_link = pos_link or p_pos
            neg_link = neg_link or p_neg

        if pos_link and not positive:
            positive = trace_node_text(prompt_data, pos_link, workflow_data)
        if neg_link and not negative:
            negative = trace_node_text(prompt_data, neg_link, workflow_data)
        if positive and negative:
            break

    # Fallback to easy pipe or easy preSampling
    if not positive or not negative:
        for nid, node in prompt_data.items():
            ctype = node.get("class_type", "")
            if "easy" in ctype:
                inputs = node.get("inputs", {})
                pos_link = inputs.get("positive") or inputs.get("pos")
                neg_link = inputs.get("negative") or inputs.get("neg")
                if not pos_link and not neg_link and "pipe" in inputs:
                    pos_link, neg_link = trace_pipe(prompt_data, inputs["pipe"])
                if not positive and pos_link:
                    positive = trace_node_text(prompt_data, pos_link, workflow_data)
                if not negative and neg_link:
                    negative = trace_node_text(prompt_data, neg_link, workflow_data)

    return positive, negative


def clean_lora_name(lora_raw: str) -> str:
    cleaned = Path(str(lora_raw).replace("\\", "/")).stem
    return cleaned


def find_checkpoints_and_loras(data: Any) -> tuple[str, list[str]]:
    checkpoint = ""
    loras: list[str] = []
    seen_loras: set[str] = set()

    def _extract_from_obj(obj: Any):
        nonlocal checkpoint
        if isinstance(obj, dict):
            ctype = obj.get("class_type", "")
            inputs = obj.get("inputs", {})
            if isinstance(inputs, dict):
                ckpt = inputs.get("ckpt_name") or inputs.get("model_name")
                if isinstance(ckpt, str) and ckpt and not checkpoint and ("Checkpoint" in ctype or "Loader" in ctype):
                    checkpoint = Path(ckpt.replace("\\", "/")).name

                # 1. Standard LoraLoader / LoraLoaderModelOnly
                if "lora_name" in inputs:
                    raw_lora = inputs.get("lora_name")
                    if isinstance(raw_lora, str) and raw_lora and raw_lora.lower() != "none":
                        cname = clean_lora_name(raw_lora)
                        if cname not in seen_loras:
                            seen_loras.add(cname)
                            sm = inputs.get("strength_model", 1.0)
                            loras.append(f"{cname}:{sm}" if sm != "" and sm != 1.0 else cname)

                # 2. Lora Stack / XYInputs
                for k, v in inputs.items():
                    if k.startswith("lora_name_") or (k.startswith("lora_") and not k.startswith("lora_name")):
                        if isinstance(v, str) and v and v.lower() != "none":
                            cname = clean_lora_name(v)
                            if cname not in seen_loras:
                                seen_loras.add(cname)
                                idx = k.split("_")[-1]
                                sm = inputs.get(f"model_str_{idx}", inputs.get(f"strength_{idx}", 1.0))
                                loras.append(f"{cname}:{sm}" if sm != "" and sm != 1.0 else cname)

            for value in obj.values():
                _extract_from_obj(value)
        elif isinstance(obj, list):
            for item in obj:
                _extract_from_obj(item)

    _extract_from_obj(data)
    return checkpoint, loras


def find_text_inputs(data: Any) -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        class_type = data.get("class_type", "")
        inputs = data.get("inputs", {})
        if isinstance(inputs, dict) and ("text" in inputs) and ("Text" in class_type or "CLIPTextEncode" in class_type):
            text = inputs.get("text")
            if isinstance(text, str) and text.strip():
                found.append(text.strip())
        for value in data.values():
            found.extend(find_text_inputs(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(find_text_inputs(item))
    return found


def split_a1111_parameters(parameters: str) -> tuple[str, str, str]:
    text = (parameters or "").strip()
    if not text:
        return "", "", ""
    steps_match = re.search(r"(?:^|\n)Steps:\s*", text)
    prompt_part = text[:steps_match.start()].strip() if steps_match else text
    params_part = text[steps_match.start():].strip() if steps_match else ""
    negative_marker = "Negative prompt:"
    if negative_marker in prompt_part:
        positive, negative = prompt_part.split(negative_marker, 1)
    else:
        positive, negative = prompt_part, ""
    return positive.strip(), negative.strip(), params_part.strip()


def parse_key_value_params(params_part: str) -> dict[str, str]:
    params: dict[str, str] = {}
    text = params_part.strip()
    if text.startswith("Steps:"):
        text = text[len("Steps:"):].strip()
        first, sep, rest = text.partition(",")
        if first.strip():
            params["Steps"] = first.strip()
        text = rest if sep else ""
    for part in re.split(r",\s*", text):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            params[key] = value
    return params


def find_loras_in_text(text: str) -> list[str]:
    loras: list[str] = []
    for name, strength in LORA_RE.findall(text or ""):
        loras.append(f"{name}:{strength}" if strength else name)
    return loras


def unique_join(items: list[str]) -> str:
    return ", ".join(dict.fromkeys(item for item in items if item))


def parse_a1111_parameters(parameters: str) -> dict[str, Any]:
    positive, negative, params_part = split_a1111_parameters(parameters)
    params = parse_key_value_params(params_part)
    loras = find_loras_in_text(positive) + find_loras_in_text(negative)
    checkpoint = params.get("Model", "") or params.get("Model hash", "")
    generation_params = json.dumps(params, ensure_ascii=False, indent=2) if params else params_part
    return {
        "positive": positive,
        "negative": negative,
        "params": params,
        "generation_params": generation_params,
        "checkpoint": checkpoint,
        "loras": loras,
    }


def extract_prompts(meta: dict[str, str]) -> tuple[str, str, str, str, str, str, str, str, str, str]:
    workflow_raw = meta.get("workflow", "")
    prompt_raw = meta.get("prompt", "")
    parameters = meta.get("parameters", "") or meta.get("Comment", "") or meta.get("Description", "")
    metadata_json = meta.get("_metadata_json", "")
    metadata_source = meta.get("_metadata_source", "")
    wardrobe_positive = meta.get("night_wardrobe_positive", "").strip()
    wardrobe_negative = meta.get("night_wardrobe_negative", "").strip()
    wardrobe_loras = [item.strip() for item in meta.get("night_wardrobe_loras", "").split(",") if item.strip()]
    if wardrobe_positive or wardrobe_negative or wardrobe_loras:
        metadata_source = "夜之主衣柜"

    prompt_data = parse_json(prompt_raw)
    workflow_data = parse_json(workflow_raw)
    data = prompt_data or workflow_data

    positive, negative = wardrobe_positive, wardrobe_negative
    generation_params = ""

    if prompt_data and isinstance(prompt_data, dict):
        positive, negative = find_ksampler_connections(prompt_data, workflow_data)

    if not positive and workflow_data and isinstance(workflow_data, dict):
        for node in workflow_data.get("nodes", []):
            ntype = str(node.get("type", ""))
            ntitle = str(node.get("title", ""))
            if "CLIPTextEncode" in ntype or "正面" in ntitle or "positive" in ntitle.lower():
                wv = node.get("widgets_values")
                if isinstance(wv, list) and len(wv) > 0 and isinstance(wv[0], str) and wv[0].strip():
                    positive = wv[0].strip()
                    break

    if not positive and data is not None:
        texts = find_text_inputs(data)
        for t in texts:
            if not any(bad in t.lower() for bad in ("worst quality", "low quality", "bad anatomy", "deformed")):
                positive = t
                break
        if not positive and texts:
            positive = texts[0]
        negative = negative or (texts[1] if len(texts) > 1 else "")

    checkpoint, loras = find_checkpoints_and_loras(data) if data is not None else ("", [])
    loras = wardrobe_loras + loras

    if parameters:
        a1111 = parse_a1111_parameters(parameters)
        positive = positive or a1111["positive"]
        negative = negative or a1111["negative"]
        checkpoint = checkpoint or a1111["checkpoint"]
        loras.extend(a1111["loras"])
        generation_params = a1111["generation_params"]
        if not metadata_source:
            metadata_source = "SD WebUI"

    if workflow_raw or prompt_raw:
        metadata_source = "ComfyUI"

    return positive, negative, workflow_raw, prompt_raw, checkpoint, unique_join(loras), parameters, metadata_json, metadata_source, generation_params


def extract_structured_prompt(meta: dict[str, str]) -> dict[str, object]:
    """Return additive Style/Character/Other buckets without changing legacy extraction."""
    positive, negative, workflow_raw, prompt_raw, checkpoint, loras, parameters, metadata_json, metadata_source, generation_params = extract_prompts(meta)
    def values(key: str) -> list[str]:
        raw = meta.get(key, "")
        parsed = parse_json(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [item.strip() for item in raw.split(",") if item.strip()]
    result = structure_prompt(
        positive,
        artist_tokens=values("artist_tokens"),
        trigger_tokens=values("trigger_tokens"),
        character_tokens=values("character_tokens"),
        lora_refs=values("lora_refs"),
    )
    result.update({
        "negative_prompt": negative,
        "checkpoint": checkpoint,
        "loras": loras,
        "workflow": workflow_raw,
        "prompt": prompt_raw,
        "parameters": parameters,
        "metadata_json": metadata_json,
        "metadata_source": metadata_source,
        "generation_params": generation_params,
        "style_unit_json": style_unit_json(result["style_unit"]),
    })
    return result


_UPSERT_SQL = """
    INSERT INTO gallery_images
        (path, title, category, positive_prompt, negative_prompt, workflow_json, prompt_json, parameters, checkpoint, loras, metadata_json, metadata_source, generation_params, file_mtime, file_size, artist_tokens, character_tokens, other_tags, style_unit_json, original_prompt, composed_prompt)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        title=excluded.title,
        category=excluded.category,
        positive_prompt=excluded.positive_prompt,
        negative_prompt=excluded.negative_prompt,
        workflow_json=excluded.workflow_json,
        prompt_json=excluded.prompt_json,
        parameters=excluded.parameters,
        checkpoint=excluded.checkpoint,
        loras=excluded.loras,
        metadata_json=excluded.metadata_json,
        metadata_source=excluded.metadata_source,
        generation_params=excluded.generation_params,
        artist_tokens=excluded.artist_tokens,
        character_tokens=excluded.character_tokens,
        other_tags=excluded.other_tags,
        style_unit_json=excluded.style_unit_json,
        original_prompt=excluded.original_prompt,
        composed_prompt=excluded.composed_prompt,
        file_mtime=excluded.file_mtime,
        file_size=excluded.file_size,
        updated_at=CURRENT_TIMESTAMP
"""


def ingest_image(conn, path: Path, root: Path = GALLERY_DIR) -> None:
    """单张图片解析入库（含文件指纹，供增量扫描跳过未变化文件）。"""
    rel = path.relative_to(GALLERY_DIR).as_posix()
    stat = path.stat()
    meta = read_image_metadata(path)
    structured = extract_structured_prompt(meta)
    positive = structured["original_prompt"]
    negative = structured["negative_prompt"]
    workflow_raw = structured["workflow"]
    prompt_raw = structured["prompt"]
    checkpoint = structured["checkpoint"]
    loras = structured["loras"]
    parameters = structured["parameters"]
    metadata_json = structured["metadata_json"]
    metadata_source = structured["metadata_source"]
    generation_params = structured["generation_params"]
    category = path.parent.name if path.parent != root else ""
    conn.execute(
        _UPSERT_SQL,
        (rel, path.stem, category, positive, negative, workflow_raw, prompt_raw, parameters, checkpoint, loras, metadata_json, metadata_source, generation_params, stat.st_mtime, stat.st_size,
         ", ".join(structured["artist_tokens"]), ", ".join(structured["character_tokens"]), ", ".join(structured["other_tags"]), structured["style_unit_json"], structured["original_prompt"], structured["composed_prompt"]),
    )


def ingest_saved_paths(paths: list[Path], *, initialize_db: bool = True) -> int:
    """定向入库：只处理给定的新增文件，上传/导入后无需全库扫描。"""
    if not paths:
        return 0
    with _SCAN_LOCK:
        ensure_gallery_dir()
        if initialize_db:
            init_db()
        count = 0
        with connect() as conn:
            for path in paths:
                path = Path(path)
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                    ingest_image(conn, path)
                    count += 1
        return count


def scan_gallery(root: Path = GALLERY_DIR, *, initialize_db: bool = True) -> int:
    root = Path(root)
    with _SCAN_LOCK:
        ensure_gallery_dir()
        if initialize_db:
            init_db()
        count = 0
        seen_paths: set[str] = set()
        with connect() as conn:
            fingerprints = {
                row["path"]: (row["file_mtime"], row["file_size"])
                for row in conn.execute("SELECT path, file_mtime, file_size FROM gallery_images")
            }
            for path in iter_images(root):
                rel = path.relative_to(GALLERY_DIR).as_posix()
                seen_paths.add(rel)
                stat = path.stat()
                if fingerprints.get(rel) == (stat.st_mtime, stat.st_size):
                    continue
                ingest_image(conn, path, root)
                count += 1

            if root.resolve() == GALLERY_DIR.resolve():
                known_paths = set(fingerprints)
                missing_paths = known_paths - seen_paths
                if missing_paths:
                    conn.executemany(
                        "DELETE FROM gallery_images WHERE path = ?",
                        ((path,) for path in missing_paths),
                    )
        return count


def export_gallery_zip() -> bytes:
    ensure_gallery_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in iter_images(GALLERY_DIR):
            arcname = path.relative_to(GALLERY_DIR).as_posix()
            zf.write(path, arcname)
    return buf.getvalue()


def safe_gallery_name(name: str) -> str:
    suffix = Path(name or "image.png").suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise ValueError("图片格式不支持")
    stem = Path(name or "image").stem.strip() or "image"
    stem = re.sub(r'[\\/:*?"<>|]+', "_", stem)
    stem = re.sub(r"\s+", "_", stem).strip("._ ") or "image"
    return f"{stem}{suffix}"


def safe_gallery_relative_path(path: str | Path) -> Path:
    raw = str(path or "image.png").replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("图片路径不合法")
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError("图片路径不合法")
    folders = [re.sub(r'[\\/:*?"<>|]+', "_", part).strip("._ ") for part in parts[:-1]]
    folders = [part for part in folders if part]
    return Path(*folders, safe_gallery_name(parts[-1]))


def unique_gallery_path(filename: str | Path) -> Path:
    ensure_gallery_dir()
    target = GALLERY_DIR / safe_gallery_relative_path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 10000):
        candidate = target.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法生成不重复的图片文件名")


def save_gallery_bytes(data: bytes, filename: str | Path) -> Path:
    target = unique_gallery_path(filename)
    Image.open(io.BytesIO(data)).verify()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def import_gallery_zip(data: bytes, folder: str = "") -> list[Path]:
    ensure_gallery_dir()
    folder = safe_gallery_relative_path(f"{folder}/placeholder.png").parent.as_posix() if folder else ""
    saved: list[Path] = []
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        for info in zf.infolist():
            if info.is_dir() or Path(info.filename).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            try:
                rel = safe_gallery_relative_path(info.filename)
            except ValueError:
                continue
            filename = Path(folder) / rel if folder else rel
            with zf.open(info) as src:
                payload = src.read()
            saved.append(save_gallery_bytes(payload, filename))
    return saved


if __name__ == "__main__":
    print(scan_gallery())

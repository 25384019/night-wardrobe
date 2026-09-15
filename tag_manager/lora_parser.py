"""LoRA safetensors header 与 Civitai 元数据解析（纯 stdlib）。

safetensors 布局：前 8 字节小端 uint64 为 header 长度，其后为 header JSON，
`__metadata__` 键内存放 kohya 训练元数据。只解析 header，不读取权重数据。
"""

from __future__ import annotations

import json
import re
import struct

HEADER_LEN_BYTES = 8
TRIGGER_WORD_LIMIT = 10
TAG_FREQUENCY_LIMIT = 50
CIVITAI_TEXT_LIMIT = 500

_BASE_MODEL_RULES = (
    ("pony", "Pony"),
    ("flux", "Flux"),
    ("sdxl", "SDXL"),
    ("sd3", "SD3"),
    ("v1-5", "SD1.5"),
    ("v1-4", "SD1.4"),
    ("sd1", "SD1.5"),
    ("v2", "SD2.x"),
)


class LoraParseError(ValueError):
    """LoRA 文件头或元数据解析失败。"""


def read_safetensors_header(raw: bytes) -> dict:
    """从 safetensors 文件开头字节中解析 header JSON。"""
    if len(raw) < HEADER_LEN_BYTES:
        raise LoraParseError("文件太小，不是有效的 safetensors")
    header_len = struct.unpack("<Q", raw[:HEADER_LEN_BYTES])[0]
    header_raw = raw[HEADER_LEN_BYTES:HEADER_LEN_BYTES + header_len]
    if len(header_raw) < header_len:
        raise LoraParseError("header 数据不完整")
    try:
        header = json.loads(header_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LoraParseError(f"header JSON 解析失败: {exc}") from exc
    if not isinstance(header, dict):
        raise LoraParseError("header 不是 JSON 对象")
    return header


def normalize_base_model(raw: str) -> str:
    """把 kohya ss_base_model_version 等原始值归一化为友好名称。"""
    text = (raw or "").lower()
    for needle, label in _BASE_MODEL_RULES:
        if needle in text:
            return label
    return raw.strip() if raw and raw.strip() else "未知"


def merge_tag_frequency(metadata: dict) -> dict[str, int]:
    """合并 ss_tag_frequency 各 dataset 分组的 tag 频次。"""
    raw = metadata.get("ss_tag_frequency") or ""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        grouped = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(grouped, dict):
        return {}
    merged: dict[str, int] = {}
    for group in grouped.values():
        if not isinstance(group, dict):
            continue
        for tag, count in group.items():
            try:
                merged[tag] = merged.get(tag, 0) + int(count)
            except (TypeError, ValueError):
                continue
    return merged


def parse_safetensors_header(header: dict) -> dict:
    """从 safetensors header dict 归一化出 LoRA 卡片字段。"""
    metadata = header.get("__metadata__")
    if not isinstance(metadata, dict):
        metadata = {}

    tag_frequency = merge_tag_frequency(metadata)
    top_tags = sorted(tag_frequency.items(), key=lambda kv: kv[1], reverse=True)
    trigger_words = [tag for tag, _ in top_tags[:TRIGGER_WORD_LIMIT]]

    return {
        "base_model": normalize_base_model(str(metadata.get("ss_base_model_version", ""))),
        "net_dim": str(metadata.get("ss_network_dim", "") or ""),
        "trigger_words": ", ".join(trigger_words),
        "training_tags": ", ".join(trigger_words),
        "tag_frequency": json.dumps(dict(top_tags[:TAG_FREQUENCY_LIMIT]), ensure_ascii=False),
        "output_name": str(metadata.get("ss_output_name", "") or ""),
    }


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def parse_civitai_info(text: str) -> dict:
    """解析 Civitai .civitai.info / 模型 JSON，提取触发词、推荐权重与描述。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LoraParseError(f"Civitai 元数据 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise LoraParseError("Civitai 元数据不是 JSON 对象")

    trained = data.get("trainedWords") or data.get("trained_words") or []
    if isinstance(trained, str):
        trained = [trained]
    trained_words = [str(w).strip() for w in trained if str(w).strip()]

    weight = None
    for key in ("recommendedWeight", "strength", "weight"):
        try:
            value = float(data[key])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < value <= 2:
            weight = value
            break

    description = _strip_html(str(data.get("description") or data.get("about") or ""))

    return {
        "trained_words": trained_words,
        "suggested_weight": weight,
        "description": description[:CIVITAI_TEXT_LIMIT],
    }


def merge_trigger_words(civitai_words: list[str], kohya_words: list[str]) -> str:
    """Civitai trainedWords 优先，kohya 高频 tag 补充，去重保序。"""
    seen: set[str] = set()
    merged: list[str] = []
    for word in [*civitai_words, *kohya_words]:
        key = word.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(word.strip())
    return ", ".join(merged)

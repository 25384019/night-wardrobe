"""工坊 Copilot 只读工具：供 function-calling 循环查询 tag / 角色卡 / 配方。

本模块不得 import app 层。execute_tool 永不抛异常，失败一律返回 error JSON。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .db import connect

TOOL_RESULT_MAX_CHARS = 4000
TOOL_SEARCH_MAX_LIMIT = 30
LOOKUP_TAGS_MAX = 50

_TOOL_PARAM = "object"


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {
        "type": _TOOL_PARAM,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _fn(
        "search_tags",
        "按关键词模糊检索 tag 库（英文 tag / 中文 / 备注），可再按一级、二级分类过滤。禁止全量拉取。",
        {
            "q": {"type": "string", "description": "搜索关键词"},
            "category": {"type": "string", "description": "一级分类精确匹配"},
            "subcategory": {"type": "string", "description": "二级分类精确匹配"},
            "limit": {
                "type": "integer",
                "description": "返回条数，默认 10，最大 30",
                "default": 10,
                "minimum": 1,
                "maximum": TOOL_SEARCH_MAX_LIMIT,
            },
        },
        ["q"],
    ),
    _fn(
        "lookup_tags",
        "按英文 tag 名称批量精确查询，返回命中项与缺失名单。",
        {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "英文 tag 列表，最多 50 个",
                "maxItems": LOOKUP_TAGS_MAX,
            },
        },
        ["tags"],
    ),
    _fn(
        "list_tag_categories",
        "列出 tag 一级分类、各分类 tag 数量及其二级分类名单。",
        {},
    ),
    _fn(
        "list_characters",
        "列出角色卡摘要（不含 appearance 外观正文）。",
        {},
    ),
    _fn(
        "get_character",
        "按角色名读取完整角色卡（含 outfits）。找不到时返回 error 与 available 名单。",
        {"name": {"type": "string", "description": "角色名称"}},
        ["name"],
    ),
    _fn(
        "list_recipes",
        "列出配方摘要（不含 positive_prompt / negative_prompt 正文）。可按 type 过滤。",
        {
            "type": {
                "type": "string",
                "description": "配方类型：artist_mix / negative / scene / params",
            },
        },
    ),
    _fn(
        "get_recipe",
        "按配方名读取完整配方（含 positive / negative prompt）。",
        {"name": {"type": "string", "description": "配方名称"}},
        ["name"],
    ),
]


def execute_tool(name: str, arguments: dict, *, connect_factory=connect) -> str:
    """分派只读工具并返回 JSON 字符串。未知工具、参数错误、SQL 异常均返回 error JSON。"""
    try:
        if not isinstance(name, str) or not name.strip():
            return _error_json("未知工具: (空)")
        if not isinstance(arguments, dict):
            keys = type(arguments).__name__
            return _error_json(f"工具 {name} 参数必须是对象，键: {keys}")
        handler = _HANDLERS.get(name)
        if handler is None:
            return _error_json(f"未知工具: {name}")
        data = handler(arguments, connect_factory)
        return _dumps_result(data)
    except Exception as exc:
        keys = ", ".join(str(k) for k in arguments.keys()) if isinstance(arguments, dict) else "(无)"
        return _error_json(f"工具 {name} 执行失败，键: {keys}：{type(exc).__name__}")


def _error_json(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _error(name: str, message: str, keys: list[str] | None = None) -> dict:
    suffix = f"，键: {', '.join(keys)}" if keys else ""
    return {"error": f"工具 {name} {message}{suffix}"}


def _clamp_limit(raw, default: int = 10) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("limit") from None
    return max(1, min(value, TOOL_SEARCH_MAX_LIMIT))


def _as_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _search_tags(arguments: dict, connect_factory: Callable):
    q = _as_text(arguments.get("q"))
    category = _as_text(arguments.get("category"))
    subcategory = _as_text(arguments.get("subcategory"))
    try:
        limit = _clamp_limit(arguments.get("limit", 10))
    except ValueError:
        return _error("search_tags", "参数校验失败", ["limit"])
    clauses = ["1=1"]
    params: list = []
    if q:
        clauses.append("(tag LIKE ? OR zh LIKE ? OR notes LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if category:
        clauses.append("category=?")
        params.append(category)
    if subcategory:
        clauses.append("subcategory=?")
        params.append(subcategory)
    where = " AND ".join(clauses)
    with connect_factory() as conn:
        rows = conn.execute(
            f"""
            SELECT tag, zh, category, subcategory, rating
            FROM tags
            WHERE {where}
            ORDER BY rating DESC, id ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [
        {
            "tag": row["tag"],
            "zh": row["zh"],
            "category": row["category"],
            "subcategory": row["subcategory"],
            "rating": row["rating"],
        }
        for row in rows
    ]


def _lookup_tags(arguments: dict, connect_factory: Callable):
    raw = arguments.get("tags")
    if not isinstance(raw, list):
        return _error("lookup_tags", "参数校验失败", ["tags"])
    names: list[str] = []
    seen: set[str] = set()
    for item in raw[:LOOKUP_TAGS_MAX]:
        name = _as_text(item)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return {"items": [], "missing": []}
    placeholders = ",".join("?" for _ in names)
    with connect_factory() as conn:
        rows = conn.execute(
            f"SELECT tag, zh, category, subcategory, rating FROM tags WHERE tag IN ({placeholders})",
            names,
        ).fetchall()
    by_name = {
        row["tag"]: {
            "tag": row["tag"],
            "zh": row["zh"],
            "category": row["category"],
            "subcategory": row["subcategory"],
            "rating": row["rating"],
        }
        for row in rows
    }
    return {
        "items": [by_name[name] for name in names if name in by_name],
        "missing": [name for name in names if name not in by_name],
    }


def _list_tag_categories(arguments: dict, connect_factory: Callable):
    with connect_factory() as conn:
        cat_rows = conn.execute(
            """
            SELECT name FROM categories
            WHERE kind IN ('tag', 'both') OR kind IS NULL OR kind = ''
            ORDER BY sort_order, name
            """
        ).fetchall()
        count_rows = conn.execute(
            "SELECT category AS name, COUNT(*) AS tag_count FROM tags GROUP BY category"
        ).fetchall()
        sub_rows = conn.execute(
            """
            SELECT category, subcategory
            FROM tags
            WHERE subcategory != ''
            GROUP BY category, subcategory
            ORDER BY MIN(id)
            """
        ).fetchall()
    counts = {row["name"] or "": row["tag_count"] for row in count_rows}
    subs: dict[str, list[str]] = {}
    for row in sub_rows:
        subs.setdefault(row["category"] or "", []).append(row["subcategory"])
    names: list[str] = []
    seen: set[str] = set()
    for row in cat_rows:
        name = row["name"] or ""
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    for name in counts:
        if name not in seen:
            seen.add(name)
            names.append(name)
    return [
        {
            "name": name,
            "tag_count": int(counts.get(name, 0)),
            "subcategories": subs.get(name, []),
        }
        for name in names
    ]


def _list_characters(arguments: dict, connect_factory: Callable):
    with connect_factory() as conn:
        rows = conn.execute(
            "SELECT id, name, lora, trigger_words FROM characters ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "lora": row["lora"],
            "trigger_words": row["trigger_words"],
        }
        for row in rows
    ]


def _get_character(arguments: dict, connect_factory: Callable):
    name = _as_text(arguments.get("name"))
    if not name:
        return _error("get_character", "参数校验失败", ["name"])
    with connect_factory() as conn:
        row = conn.execute("SELECT * FROM characters WHERE name=?", (name,)).fetchone()
        if row is None:
            available = [
                item["name"]
                for item in conn.execute("SELECT name FROM characters ORDER BY name").fetchall()
            ]
            return {"error": "角色不存在", "available": available}
        outfits = conn.execute(
            """
            SELECT id, name, tags, notes
            FROM character_outfits
            WHERE character_id=?
            ORDER BY id
            """,
            (row["id"],),
        ).fetchall()
    card = dict(row)
    card["outfits"] = [dict(item) for item in outfits]
    return card


def _list_recipes(arguments: dict, connect_factory: Callable):
    recipe_type = _as_text(arguments.get("type"))
    query = "SELECT id, name, type, notes FROM recipes"
    params: list[str] = []
    if recipe_type:
        query += " WHERE type=?"
        params.append(recipe_type)
    query += " ORDER BY type, updated_at DESC, id DESC"
    with connect_factory() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {"id": row["id"], "name": row["name"], "type": row["type"], "notes": row["notes"]}
        for row in rows
    ]


def _get_recipe(arguments: dict, connect_factory: Callable):
    name = _as_text(arguments.get("name"))
    if not name:
        return _error("get_recipe", "参数校验失败", ["name"])
    with connect_factory() as conn:
        row = conn.execute(
            "SELECT * FROM recipes WHERE name=? ORDER BY id LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            available = [
                item["name"]
                for item in conn.execute("SELECT name FROM recipes ORDER BY name").fetchall()
            ]
            return {"error": "配方不存在", "available": available}
    return dict(row)


_HANDLERS = {
    "search_tags": _search_tags,
    "lookup_tags": _lookup_tags,
    "list_tag_categories": _list_tag_categories,
    "list_characters": _list_characters,
    "get_character": _get_character,
    "list_recipes": _list_recipes,
    "get_recipe": _get_recipe,
}


def _truncation_sequence(data):
    if isinstance(data, list):
        return data, lambda kept: data[:kept]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
        return items, lambda kept: {**data, "items": items[:kept]}
    if isinstance(data, dict) and isinstance(data.get("outfits"), list):
        outfits = data["outfits"]
        return outfits, lambda kept: {**data, "outfits": outfits[:kept]}
    return None, None


def _dumps_result(data) -> str:
    text = json.dumps(data, ensure_ascii=False)
    if len(text) <= TOOL_RESULT_MAX_CHARS:
        return text
    sequence, rebuild = _truncation_sequence(data)
    if sequence is None:
        return text[:TOOL_RESULT_MAX_CHARS] + "[已截断，共 1 条，仅显示前 0 条]"
    total = len(sequence)
    low = 0
    high = total - 1
    best_kept = 0
    best_piece = None
    while low <= high:
        mid = (low + high) // 2
        piece = json.dumps(rebuild(mid), ensure_ascii=False)
        if len(piece) <= TOOL_RESULT_MAX_CHARS:
            best_kept = mid
            best_piece = piece
            low = mid + 1
        else:
            high = mid - 1
    note = f"[已截断，共 {total} 条，仅显示前 {best_kept} 条]"
    if best_piece is None:
        best_piece = json.dumps(rebuild(0), ensure_ascii=False)[:TOOL_RESULT_MAX_CHARS]
    return best_piece + note

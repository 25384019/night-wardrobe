"""面向图源 Tag 的 AI 提示词重构服务（默认调用 DeepSeek）。

提供：
1. 一键 NSFW 模式：保留角色特征，剔除所有服装与遮挡 Tag，规范注入裸露生理词。
2. 口语化描述修改：通过大模型智能分析自然语言意图，做语义对消与新增标准 Danbooru Tag。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .db import connect
from .llm import chat_completion_messages

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

AI_TAG_SYSTEM_PROMPT = """你是一个专门精通 Anime / Stable Diffusion / Danbooru 提示词（Prompt）工程的高级专家。
你的任务是对用户提供的原始图片提示词（图源 Tag 列表）进行精准的手术式修改。

【核心规则】
1. 语法规范：
   - 所有的输出词汇必须是标准的 Danbooru 规范 Tag（英文小写，词内单词以下划线或空格连接，如 `blonde_hair`, `school_swimsuit`）。
   - 绝不要输出任何散文句子或自然语言解释。
2. 角色特征守恒原则：
   - 除非用户明确要求更换角色或改变特定生理特征，否则【角色名】（如 `klee (genshin impact)`）、【发色/发型】（如 `blonde hair`, `twintails`）、【瞳色】（如 `red eyes`）、【种族特征】（如 `pointy ears`, `animal ears`, `horns`, `tail`）、【身材体型】（如 `flat chest`, `petite`）必须完整保留！
3. 冲突对消原则（语义互斥）：
   - 若指令要求替换某项特征（如“黑发换白发”），必须从原词中剔除旧特征（`black hair`），并加入新特征（`white hair`），绝对不能让互斥 Tag 同时存在。
4. 输出格式：
   - 必须严格返回如下 JSON 结构，不要包裹任何非 JSON 字符或额外解释：
   {
     "summary": "简述本次修改的要点",
     "removed_tags": ["从原词中删除的标签1", "从原词中删除的标签2"],
     "added_tags": ["本次新增的标签1", "本次新增的标签2"],
     "new_prompt": "经过修改后的完整逗号分隔 Prompt（不含被删除词，含保留词与新增词）"
   }
"""

NSFW_MODE_INSTRUCTION = """【任务：NSFW 脱衣与解构模式】
请对原始提示词进行 NSFW 转换：
1. 完整保留：角色身份名、作品名、发色、发型、眼睛颜色、种族生理特征（兽耳/精灵耳/尾巴等）、基本身材与面部表情。
2. 彻底剔除：
   - 所有衣物服饰（包括但不限于：hat, cap, coat, cardigan, shirt, dress, skirt, pants, belt, brooch, ribbon, gloves, sleeves, thighhighs, socks, shoes, boots 等）；
   - 所有遮挡物与手持负重（包括但不限于：backpack, bag, holding object, shield, umbrella 等）；
   - 所有审查与马赛克词汇（censor, censored, mosaic censoring, bar censor 等）。
3. 规范注入：
   - 基础裸露词：`completely nude, uncensored, nude`；
   - 生理与身体特征词：`breasts, nipples, pussy, navel, collarbone`；
   - 根据画面原有氛围合理补充（如 `blush`, `open mouth`, `spread legs`）。
4. 请保持角色特征在前，裸体特征与细节紧随其后。
"""

SWIMSUIT_MODE_INSTRUCTION = """【任务：清凉泳装模式】
请将角色的原有日常服装/厚重服饰替换为清凉比基尼/泳装：
1. 完整保留：角色名、发型发色、瞳色、种族特征与面部表情。
2. 剔除：原有外套、衬衫、帽子、厚重鞋袜、背包等。
3. 注入：`swimsuit, bikini, cleavage, bare shoulders, navel, bare legs`，适当补充海滩/泳池背景词。
"""


def get_deepseek_api_key(override_key: str | None = None) -> str:
    """按优先级获取 DeepSeek API Key：传参 > llm_settings 数据库 > 环境变量。"""
    if override_key and override_key.strip():
        return override_key.strip()

    # 查库
    try:
        with connect() as conn:
            row = conn.execute("SELECT api_key, base_url FROM llm_settings WHERE id = 1").fetchone()
            if row and row["api_key"] and row["api_key"].strip():
                return row["api_key"].strip()
    except Exception:
        pass

    # 查环境变量
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key

    return ""


def clean_json_response(raw_text: str) -> dict[str, Any]:
    """清洗大模型可能携带的 markdown 代码围栏并解析 JSON。"""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        text = match.group(0)

    try:
        return json.loads(text)
    except Exception as e:
        raise ValueError(f"大模型返回内容无法解析为 JSON: {raw_text[:200]}") from e


def transform_tags_with_deepseek(
    source_prompt: str,
    mode: str = "nsfw",
    instruction: str = "",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """调用 DeepSeek 对图源 Tag 进行重构。"""
    key = get_deepseek_api_key(api_key)
    if not key:
        raise ValueError("未检测到 DeepSeek API Key。请在下方填入 API Key 或在工坊 AI 设置中配置。")

    url = (base_url or "").strip() or DEEPSEEK_BASE_URL
    mod = (model or "").strip() or DEEPSEEK_MODEL

    clean_prompt = (source_prompt or "").strip()
    if not clean_prompt:
        raise ValueError("图源提示词为空，无法进行改写。")

    # 根据模式组装指令
    user_instruction = ""
    if mode == "nsfw":
        user_instruction = NSFW_MODE_INSTRUCTION
        if instruction.strip():
            user_instruction += f"\n补充口语要求：{instruction.strip()}"
    elif mode == "swimsuit":
        user_instruction = SWIMSUIT_MODE_INSTRUCTION
        if instruction.strip():
            user_instruction += f"\n补充口语要求：{instruction.strip()}"
    elif mode == "custom":
        if not instruction.strip():
            raise ValueError("口语化改写模式下，请输入你的修改需求。")
        user_instruction = f"""【任务：口语化自然语言改写】
用户希望对以下图源 Tag 进行定制化修改：
口语需求："{instruction.strip()}"

请严格按照语义对消原则处理：识别用户想修改的方面（如换衣服、换发色、改动作、换场景），删除冲突词，新增标准 Danbooru Tag，保留角色固有身份与未被要求修改的特征。
"""
    else:
        user_instruction = instruction.strip() or NSFW_MODE_INSTRUCTION

    user_content = f"""【图源原始 Prompt】：
{clean_prompt}

【修改要求】：
{user_instruction}

请只输出符合规范的 JSON 对象。"""

    messages = [
        {"role": "system", "content": AI_TAG_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        raw_reply = chat_completion_messages(
            base_url=url,
            api_key=key,
            model=mod,
            messages=messages,
            temperature=0.3,
            max_tokens=2048,
            response_format={"type": "json_object"},
            timeout=60,
        )
    except Exception as exc:
        if "response_format" in str(exc).lower():
            raw_reply = chat_completion_messages(
                base_url=url,
                api_key=key,
                model=mod,
                messages=messages,
                temperature=0.3,
                max_tokens=2048,
                timeout=60,
            )
        else:
            raise RuntimeError(f"调用 DeepSeek 接口失败：{exc}") from exc

    parsed = clean_json_response(raw_reply)

    return {
        "ok": True,
        "mode": mode,
        "summary": parsed.get("summary", "完成提示词改写"),
        "removed_tags": parsed.get("removed_tags", []),
        "added_tags": parsed.get("added_tags", []),
        "new_prompt": parsed.get("new_prompt", clean_prompt).strip(" ,"),
        "original_prompt": clean_prompt,
    }

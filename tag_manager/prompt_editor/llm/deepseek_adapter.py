from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from ...db import connect
from ...llm import chat_completion_messages
from ..schemas import LLMRawEditOutput
from .base import PromptEditorLLMBase
from .prompts import PROMPT_SURGEON_SYSTEM_PROMPT, build_user_message

logger = logging.getLogger(__name__)

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


def _clean_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        return match.group(0)
    return cleaned


class DeepSeekAdapter(PromptEditorLLMBase):
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _resolve_config(self) -> Tuple[str, str, str]:
        base_url = self.base_url
        api_key = self.api_key
        model = self.model

        # Query from llm_settings table
        try:
            with connect() as conn:
                row = conn.execute("SELECT base_url, api_key, model FROM llm_settings WHERE id = 1").fetchone()
                if row:
                    if not base_url and row["base_url"]:
                        base_url = row["base_url"].strip()
                    if not api_key and row["api_key"]:
                        api_key = row["api_key"].strip()
                    if not model and row["model"]:
                        model = row["model"].strip()
        except Exception:
            pass

        # Environment fallbacks
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()

        if not base_url:
            base_url = DEFAULT_DEEPSEEK_BASE_URL
        if not model:
            model = DEFAULT_DEEPSEEK_MODEL

        return base_url, api_key, model

    def edit_prompt(
        self,
        prompt: str,
        instruction: str,
        locked_tags: Optional[List[str]] = None,
        allowed_scopes: Optional[List[str]] = None,
        preserve_identity: bool = True,
        age_status: str = "unknown",
        adult_mode: bool = False,
    ) -> LLMRawEditOutput:
        base_url, api_key, model = self._resolve_config()
        if not api_key:
            raise RuntimeError("API Key not configured. Please set your API Key in Settings.")

        user_content = build_user_message(
            prompt=prompt,
            instruction=instruction,
            locked_tags=locked_tags,
            allowed_scopes=allowed_scopes,
            preserve_identity=preserve_identity,
            age_status=age_status,
        )

        messages = [
            {"role": "system", "content": PROMPT_SURGEON_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        # Call with temperature=0.2 and optional response_format
        raw_response = chat_completion_messages(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"} if "deepseek" in model.lower() or "gpt" in model.lower() else None,
            timeout=self.timeout,
        )

        # Attempt 1: parse JSON
        try:
            cleaned_text = _clean_json(raw_response)
            data = json.loads(cleaned_text)
            return LLMRawEditOutput(**data)
        except Exception as first_exc:
            logger.warning("First JSON parsing failed (%s), attempting 1 repair retry...", first_exc)

            # Repair retry (max 1 retry)
            repair_messages = list(messages)
            repair_messages.append({"role": "assistant", "content": raw_response})
            repair_messages.append({
                "role": "user",
                "content": "Your output was not valid JSON. Please fix it and return ONLY the valid JSON object according to the schema."
            })

            repair_response = chat_completion_messages(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=repair_messages,
                temperature=0.1,
                timeout=self.timeout,
            )

            try:
                repaired_text = _clean_json(repair_response)
                data = json.loads(repaired_text)
                return LLMRawEditOutput(**data)
            except Exception as repair_exc:
                logger.error("JSON repair retry failed: %s", repair_exc)
                raise ValueError(f"AI response is not valid JSON: {raw_response[:200]}") from repair_exc

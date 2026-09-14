from __future__ import annotations

from typing import Optional
from .deepseek_adapter import DeepSeekAdapter

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class OpenAIAdapter(DeepSeekAdapter):
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        super().__init__(
            base_url=base_url or DEFAULT_OPENAI_BASE_URL,
            api_key=api_key,
            model=model or DEFAULT_OPENAI_MODEL,
            timeout=timeout,
        )

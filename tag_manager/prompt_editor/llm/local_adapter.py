from __future__ import annotations

from typing import Optional
from .deepseek_adapter import DeepSeekAdapter

DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LOCAL_MODEL = "qwen2.5:7b"


class LocalAdapter(DeepSeekAdapter):
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        super().__init__(
            base_url=base_url or DEFAULT_LOCAL_BASE_URL,
            api_key="local",
            model=model or DEFAULT_LOCAL_MODEL,
            timeout=timeout,
        )

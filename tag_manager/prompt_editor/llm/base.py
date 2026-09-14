from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional
from ..schemas import LLMRawEditOutput


class PromptEditorLLMBase(ABC):
    @abstractmethod
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
        """
        Send prompt and instruction to LLM, return structured raw edit output.
        """
        pass

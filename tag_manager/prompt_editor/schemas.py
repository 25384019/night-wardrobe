from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

EDIT_SCOPES = [
    "identity",
    "hair",
    "eyes",
    "face",
    "expression",
    "body",
    "clothing",
    "accessories",
    "pose",
    "action",
    "camera",
    "background",
    "lighting",
    "style",
    "quality",
    "lora",
    "other",
]

EditScope = Literal[
    "identity",
    "hair",
    "eyes",
    "face",
    "expression",
    "body",
    "clothing",
    "accessories",
    "pose",
    "action",
    "camera",
    "background",
    "lighting",
    "style",
    "quality",
    "lora",
    "other",
]


class TagASTItem(BaseModel):
    tag: str
    raw: str
    weight: float = 1.0
    bracket_type: Optional[Literal["paren", "bracket"]] = None
    bracket_depth: int = 0
    kind: Literal["tag", "lora", "embedding", "wildcard", "break"] = "tag"
    scope: Optional[str] = None
    canonical: Optional[str] = None
    is_locked: bool = False


class LLMRawEditOutput(BaseModel):
    intent: str = "edit_prompt"
    confidence: float = 1.0
    edit_scopes: List[str] = Field(default_factory=list)
    preserve_identity: bool = True
    preserve_unmodified_scopes: bool = True
    remove_tags: List[str] = Field(default_factory=list)
    add_tags: List[str] = Field(default_factory=list)
    locked_tags: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    final_tags: Optional[List[str]] = None


class TagDiffItem(BaseModel):
    tag: str
    raw: str
    action: Literal["remove", "add", "keep", "lock"]
    scope: Optional[str] = None
    weight: float = 1.0
    reason: Optional[str] = None


class PromptEditRequest(BaseModel):
    image_id: Optional[int] = None
    prompt: str
    negative_prompt: Optional[str] = None
    instruction: str
    allowed_scopes: Optional[List[str]] = None
    locked_tags: Optional[List[str]] = None
    preserve_identity: bool = True
    adult_mode: bool = False
    age_status: str = "unknown"
    prompt_version: Optional[int] = 1


class PromptEditPreviewResponse(BaseModel):
    success: bool = True
    original_prompt: str
    edited_prompt: str
    diff_summary: Dict[str, List[str]] = Field(default_factory=dict)
    diff_tags: List[TagDiffItem] = Field(default_factory=list)
    edit_scopes: List[str] = Field(default_factory=list)
    prompt_version: int = 1
    safe: bool = True
    warnings: List[str] = Field(default_factory=list)
    message: Optional[str] = None
    error: Optional[str] = None


class PromptEditApplyRequest(BaseModel):
    image_id: int
    edited_prompt: str
    diff_summary: Optional[Dict[str, Any]] = None
    instruction: Optional[str] = None
    prompt_version: int


class PromptEditUndoRequest(BaseModel):
    image_id: int
    target_version: Optional[int] = None


class PromptEditHistoryItem(BaseModel):
    id: int
    image_id: int
    version: int
    instruction: str = ""
    edit_scopes: List[str] = Field(default_factory=list)
    positive_prompt_before: str = ""
    positive_prompt_after: str = ""
    negative_prompt_before: str = ""
    negative_prompt_after: str = ""
    diff_json: Dict[str, Any] = Field(default_factory=dict)
    applied_by: str = "user"
    created_at: str = ""


class PromptVersionItem(BaseModel):
    id: int
    image_id: int
    version_number: int
    parent_version_id: Optional[int] = None
    positive_prompt: str
    negative_prompt: str = ""
    instruction: str = ""
    remove_tags: List[str] = Field(default_factory=list)
    add_tags: List[str] = Field(default_factory=list)
    locked_tags: List[str] = Field(default_factory=list)
    diff_json: Dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    applied_by: str = "user"
    is_original: bool = False
    is_current: bool = False
    created_at: str = ""


class PromptVersionApplyRequest(BaseModel):
    image_id: int
    positive_prompt: str
    negative_prompt: Optional[str] = ""
    instruction: Optional[str] = ""
    parent_version_id: Optional[int] = None
    diff_summary: Optional[Dict[str, Any]] = None
    model: Optional[str] = "deepseek"


class PromptVersionSetCurrentRequest(BaseModel):
    image_id: int
    version_id: int


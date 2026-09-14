from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict

RULES_DIR = Path(__file__).resolve().parent / "rules"

_ALIASES: Dict[str, str] = {}


def load_aliases() -> Dict[str, str]:
    global _ALIASES
    if not _ALIASES:
        path = RULES_DIR / "aliases.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _ALIASES = json.load(f)
            except Exception:
                _ALIASES = {}
    return _ALIASES


def canonical_tag(tag: str) -> str:
    """
    Standardize a tag string for comparison:
    lowercase, replace underscores with spaces, collapse extra whitespace.
    """
    if not tag:
        return ""
    # Strip any enclosing weights if accidentally passed as raw
    cleaned = tag.strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        # check if it's (tag:weight)
        parts = cleaned[1:-1].split(":")
        if len(parts) == 2:
            try:
                float(parts[1])
                cleaned = parts[0]
            except ValueError:
                pass
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


def resolve_alias(tag: str) -> str:
    """
    Resolve colloquial or non-standard tag to standard Danbooru tag via aliases.json.
    """
    canon = canonical_tag(tag)
    aliases = load_aliases()
    return aliases.get(canon, canon)


def normalize_tag(tag: str) -> str:
    """
    Full normalization pipeline: canonicalize and resolve alias.
    """
    return resolve_alias(canonical_tag(tag))

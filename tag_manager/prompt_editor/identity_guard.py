from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from .normalizer import canonical_tag

RULES_DIR = Path(__file__).resolve().parent / "rules"

_RULES_CACHE: Dict[str, Any] = {}


def _get_protected_rules() -> dict:
    if "protected_tags.json" not in _RULES_CACHE:
        p = RULES_DIR / "protected_tags.json"
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    _RULES_CACHE["protected_tags.json"] = json.load(f)
            except Exception:
                _RULES_CACHE["protected_tags.json"] = {}
        else:
            _RULES_CACHE["protected_tags.json"] = {}
    return _RULES_CACHE["protected_tags.json"]


class IdentityGuard:
    def __init__(self):
        self.rules = _get_protected_rules()
        self.species_traits = set(self.rules.get("species_traits", []))

    def is_protected_tag(self, tag: str, kind: str = "tag") -> bool:
        """
        Check whether a tag belongs to protected character identity or species traits.
        """
        canon = canonical_tag(tag)

        # Character LoRA
        if kind == "lora":
            if any(k in canon for k in ["char", "character", "klee", "miku", "cosplay"]):
                return True

        # Franchise/character name pattern: name (series)
        if "(" in tag and ")" in tag:
            # check if it looks like character/series name
            inner = tag[tag.find("(") + 1:tag.rfind(")")].strip().lower()
            if any(kw in inner for kw in ["genshin", "impact", "touhou", "azur", "lane", "fate", "vocaloid", "project", "series", "anime", "game", "idolmaster", "blue archive"]):
                return True

        # Species traits
        if canon in {"ponytail", "twintails", "twintail", "side ponytail", "low twintails"}:
            return False

        if canon in self.species_traits:
            return True
        for trait in self.species_traits:
            if trait == "tail" and any(h in canon for h in ["ponytail", "twintail"]):
                continue
            if re.search(r"\b" + re.escape(trait) + r"\b", canon):
                return True

        # Solo / 1girl base identity anchors
        if canon in {"1girl", "1boy", "solo"}:
            return True

        return False

    def filter_removals(
        self,
        remove_tags: List[str],
        instruction: str,
        preserve_identity: bool = True,
    ) -> Tuple[List[str], List[str]]:
        """
        Filter out protected identity tags from the removal list unless the user explicitly requested it.
        Returns (safe_remove_tags, protected_tags_kept).
        """
        if not preserve_identity:
            return remove_tags, []

        inst_lower = instruction.lower()
        # Explicit intent to change character
        explicit_character_change = any(kw in inst_lower for kw in ["换角色", "换成另一个人", "change character", "remove character", "不同角色", "不是", "不要这个角色"])

        safe_removes: List[str] = []
        protected_kept: List[str] = []

        for tag in remove_tags:
            if self.is_protected_tag(tag):
                canon = canonical_tag(tag)
                # Check if explicitly mentioned in instruction (e.g. "去掉猫耳")
                if ("去掉" in inst_lower or "不要" in inst_lower or "remove" in inst_lower) and (canon in inst_lower or tag.lower() in inst_lower or "耳" in inst_lower):
                    safe_removes.append(tag)
                elif explicit_character_change:
                    safe_removes.append(tag)
                else:
                    protected_kept.append(tag)
            else:
                safe_removes.append(tag)

        return safe_removes, protected_kept

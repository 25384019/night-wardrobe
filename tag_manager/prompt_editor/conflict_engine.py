from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .classifier import classify_tag_scope
from .normalizer import canonical_tag, normalize_tag
from .schemas import TagASTItem

RULES_DIR = Path(__file__).resolve().parent / "rules"

_RULES_CACHE: Dict[str, Any] = {}


def _get_rule_file(filename: str) -> dict:
    if filename not in _RULES_CACHE:
        p = RULES_DIR / filename
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    _RULES_CACHE[filename] = json.load(f)
            except Exception:
                _RULES_CACHE[filename] = {}
        else:
            _RULES_CACHE[filename] = {}
    return _RULES_CACHE[filename]


class ConflictEngine:
    def __init__(self):
        self.exclusive_groups = _get_rule_file("exclusive_groups.json")
        self.clothing_slots = _get_rule_file("clothing_slots.json")
        self.dependencies = _get_rule_file("dependencies.json")

    def find_exclusive_group(self, norm_tag: str) -> Optional[Tuple[str, List[str]]]:
        """
        Find if a normalized tag belongs to any exclusive group.
        Returns (group_name, list_of_group_tags) or None.
        """
        for group_name, members in self.exclusive_groups.items():
            if norm_tag in members:
                return group_name, members
            # Partial check for hair/eye colors (e.g., 'blonde hair')
            for m in members:
                if norm_tag == m:
                    return group_name, members
        return None

    def find_clothing_slot(self, norm_tag: str) -> Optional[Tuple[str, List[str]]]:
        """
        Find if a normalized tag belongs to a clothing slot.
        Returns (slot_name, list_of_slot_tags) or None.
        """
        for slot_name, members in self.clothing_slots.items():
            if norm_tag in members:
                return slot_name, members
            for m in members:
                if norm_tag == m:
                    return slot_name, members
        return None

    def resolve_conflicts(
        self,
        current_tags: List[TagASTItem],
        proposed_adds: List[str],
        proposed_removes: List[str],
        locked_tags: Optional[List[str]] = None,
        allowed_scopes: Optional[List[str]] = None,
    ) -> Tuple[List[str], List[str], List[str]]:
        """
        Determine deterministic additions and removals.
        Returns (final_adds, final_removes, warnings).
        """
        locked_set = {canonical_tag(t) for t in (locked_tags or [])}
        allowed_scope_set = set(allowed_scopes) if allowed_scopes else None

        final_adds: List[str] = []
        final_removes: Set[str] = set()
        warnings: List[str] = []

        # Map current tags by canonical form
        current_map = {canonical_tag(item.tag): item for item in current_tags}

        # 1. Process proposed additions
        for add_raw in proposed_adds:
            add_norm = normalize_tag(add_raw)
            if not add_norm:
                continue

            add_scope = classify_tag_scope(add_norm)
            if allowed_scope_set and add_scope not in allowed_scope_set:
                # Discard additions outside allowed scope
                continue

            final_adds.append(add_raw)

            # Check exclusive groups conflict
            ex_info = self.find_exclusive_group(add_norm)
            if ex_info:
                group_name, members = ex_info
                for member in members:
                    m_canon = canonical_tag(member)
                    if m_canon in current_map and m_canon != canonical_tag(add_norm):
                        if m_canon in locked_set:
                            warnings.append(f"locked_tag_conflict: '{current_map[m_canon].tag}' is locked and conflicts with '{add_raw}'")
                        else:
                            final_removes.add(current_map[m_canon].tag)

            # Special case: short hair conflicts with ponytail, twintails, etc.
            if add_norm in {"short hair", "very short hair", "pixie cut"}:
                for hair_style in ["ponytail", "twintails", "side ponytail", "drill hair", "long hair", "very long hair"]:
                    if hair_style in current_map:
                        if hair_style in locked_set:
                            warnings.append(f"locked_tag_conflict: '{current_map[hair_style].tag}' is locked and conflicts with '{add_raw}'")
                        else:
                            final_removes.add(current_map[hair_style].tag)

            # Check clothing slots conflict
            slot_info = self.find_clothing_slot(add_norm)
            if slot_info:
                slot_name, members = slot_info
                # Same slot replacement
                for member in members:
                    m_canon = canonical_tag(member)
                    if m_canon in current_map and m_canon != canonical_tag(add_norm):
                        if m_canon in locked_set:
                            warnings.append(f"locked_tag_conflict: '{current_map[m_canon].tag}' is locked and conflicts with '{add_raw}'")
                        else:
                            final_removes.add(current_map[m_canon].tag)

                # Full-body replacements: dress or swimwear replaces top + bottom
                if slot_name in ("dress", "swimwear"):
                    for other_slot in ("top", "bottom"):
                        for member in self.clothing_slots.get(other_slot, []):
                            m_canon = canonical_tag(member)
                            if m_canon in current_map:
                                if m_canon in locked_set:
                                    warnings.append(f"locked_tag_conflict: '{current_map[m_canon].tag}' is locked and conflicts with '{add_raw}'")
                                else:
                                    final_removes.add(current_map[m_canon].tag)

                # Top or bottom replaces dress or swimwear
                if slot_name in ("top", "bottom"):
                    for other_slot in ("dress", "swimwear"):
                        for member in self.clothing_slots.get(other_slot, []):
                            m_canon = canonical_tag(member)
                            if m_canon in current_map:
                                if m_canon in locked_set:
                                    warnings.append(f"locked_tag_conflict: '{current_map[m_canon].tag}' is locked and conflicts with '{add_raw}'")
                                else:
                                    final_removes.add(current_map[m_canon].tag)

            # Check environment dependencies and mutual exclusions
            # e.g., classroom implies indoors, conflicts with outdoors, street, sky
            if add_norm == "classroom" or add_norm == "indoors":
                for outdoor_tag in ["outdoors", "street", "blue sky", "sky", "beach", "city"]:
                    if outdoor_tag in current_map:
                        if outdoor_tag in locked_set:
                            warnings.append(f"locked_tag_conflict: '{current_map[outdoor_tag].tag}' is locked and conflicts with '{add_raw}'")
                        else:
                            final_removes.add(current_map[outdoor_tag].tag)
                if "indoors" not in current_map and "indoors" not in [canonical_tag(a) for a in final_adds]:
                    final_adds.append("indoors")

            elif add_norm == "beach" or add_norm == "outdoors":
                for indoor_tag in ["indoors", "classroom", "bedroom", "room"]:
                    if indoor_tag in current_map:
                        if indoor_tag in locked_set:
                            warnings.append(f"locked_tag_conflict: '{current_map[indoor_tag].tag}' is locked and conflicts with '{add_raw}'")
                        else:
                            final_removes.add(current_map[indoor_tag].tag)
                if "outdoors" not in current_map and "outdoors" not in [canonical_tag(a) for a in final_adds]:
                    final_adds.append("outdoors")

        # 2. Process proposed removals from LLM
        for rem_raw in proposed_removes:
            rem_canon = canonical_tag(rem_raw)
            if not rem_canon:
                continue

            # Find matching tag in current_tags
            matched = False
            for item in current_tags:
                if canonical_tag(item.tag) == rem_canon or rem_canon in canonical_tag(item.tag):
                    matched = True
                    tag_scope = classify_tag_scope(item.tag)
                    if allowed_scope_set and tag_scope not in allowed_scope_set:
                        continue
                    if canonical_tag(item.tag) in locked_set:
                        warnings.append(f"locked_tag_conflict: Cannot remove locked tag '{item.tag}'")
                    else:
                        final_removes.add(item.tag)
            if not matched and rem_raw:
                # Try direct addition to final_removes
                if rem_canon in locked_set:
                    warnings.append(f"locked_tag_conflict: Cannot remove locked tag '{rem_raw}'")
                else:
                    final_removes.add(rem_raw)

        return final_adds, list(final_removes), warnings

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .classifier import classify_tag_scope
from .normalizer import canonical_tag, normalize_tag
from .parser import reconstruct_prompt
from .schemas import TagASTItem, TagDiffItem


class DiffEngine:
    @staticmethod
    def calculate_diff(
        original_ast: List[TagASTItem],
        remove_tags: List[str],
        add_tags: List[str],
        locked_tags: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, List[str]], List[TagDiffItem]]:
        """
        Calculate tag diff, clean intersection conflicts, reconstruct prompt,
        and generate structured diff items.
        """
        locked_canonical = {canonical_tag(t) for t in (locked_tags or [])}
        remove_canonical = {canonical_tag(t) for t in remove_tags}
        add_canonical = {canonical_tag(t) for t in add_tags}

        # Ensure remove ∩ add = ∅
        overlap = remove_canonical.intersection(add_canonical)
        if overlap:
            # Overlap conflict: addition takes precedence over removal
            remove_canonical = remove_canonical - overlap
            if warnings is not None:
                warnings.append(f"Overlap detected between remove and add tags: {list(overlap)}. Resolved in favor of addition.")

        diff_tags: List[TagDiffItem] = []
        kept_items: List[TagASTItem] = []
        added_items: List[TagASTItem] = []

        summary_added: List[str] = []
        summary_removed: List[str] = []
        summary_kept: List[str] = []
        summary_locked: List[str] = []

        # Process original items
        for item in original_ast:
            if item.kind == "break":
                kept_items.append(item)
                continue

            item_canon = canonical_tag(item.tag)
            item_scope = classify_tag_scope(item.tag, item.kind)
            item.scope = item_scope

            # Check if locked
            if item_canon in locked_canonical or item.is_locked:
                item.is_locked = True
                summary_locked.append(item.tag)
                kept_items.append(item)
                diff_tags.append(
                    TagDiffItem(
                        tag=item.tag,
                        raw=item.raw,
                        action="lock",
                        scope=item_scope,
                        weight=item.weight,
                        reason="Tag is pinned/locked",
                    )
                )
            elif item_canon in remove_canonical or item.tag in remove_tags:
                summary_removed.append(item.tag)
                diff_tags.append(
                    TagDiffItem(
                        tag=item.tag,
                        raw=item.raw,
                        action="remove",
                        scope=item_scope,
                        weight=item.weight,
                        reason="Removed by edit or conflict resolution",
                    )
                )
            else:
                summary_kept.append(item.tag)
                kept_items.append(item)
                diff_tags.append(
                    TagDiffItem(
                        tag=item.tag,
                        raw=item.raw,
                        action="keep",
                        scope=item_scope,
                        weight=item.weight,
                    )
                )

        # Process added items
        # Avoid adding tags that already exist in kept_items
        kept_canonical = {canonical_tag(item.tag) for item in kept_items if item.kind != "break"}

        for add_raw in add_tags:
            add_canon = canonical_tag(add_raw)
            if not add_canon or add_canon in kept_canonical:
                continue

            add_scope = classify_tag_scope(add_raw)
            new_item = TagASTItem(
                tag=add_raw,
                raw=add_raw,
                weight=1.0,
                scope=add_scope,
                kind="lora" if add_raw.startswith("<lora:") else "tag",
            )
            added_items.append(new_item)
            kept_canonical.add(add_canon)
            summary_added.append(add_raw)
            diff_tags.append(
                TagDiffItem(
                    tag=add_raw,
                    raw=add_raw,
                    action="add",
                    scope=add_scope,
                    weight=1.0,
                    reason="Added by user instruction",
                )
            )

        # Assemble final AST:
        # Separate LoRA tags and place them at the end if desired, or keep natural order
        # We preserve kept_items order and append added_items before any trailing LoRAs
        non_lora_kept: List[TagASTItem] = []
        lora_kept: List[TagASTItem] = []
        for item in kept_items:
            if item.kind == "lora":
                lora_kept.append(item)
            else:
                non_lora_kept.append(item)

        final_ast = non_lora_kept + added_items + lora_kept
        edited_prompt = reconstruct_prompt(final_ast)

        diff_summary = {
            "added": summary_added,
            "removed": summary_removed,
            "kept": summary_kept,
            "locked": summary_locked,
        }

        return edited_prompt, diff_summary, diff_tags

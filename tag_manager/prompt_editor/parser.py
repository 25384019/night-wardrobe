from __future__ import annotations

import re
from typing import List, Optional
from .schemas import TagASTItem

# Regex to detect weighted tag: (tag:1.2) or (tag:0.8)
RE_WEIGHTED_PAREN = re.compile(r"^\(\s*(.+?)\s*:\s*([0-9.]+)\s*\)$")
RE_WEIGHTED_BRACKET = re.compile(r"^\[\s*(.+?)\s*:\s*([0-9.]+)\s*\]$")
RE_LORA = re.compile(r"^<lora:([^:>]+)(?::([^:>]+))?(?::([^:>]+))?>$", re.IGNORECASE)
RE_EMBEDDING = re.compile(r"^(?:embedding:)(.+)$", re.IGNORECASE)
RE_WILDCARD = re.compile(r"^__([a-zA-Z0-9_\-]+)__$")


def split_prompt_tokens(prompt: str) -> List[str]:
    """
    Split prompt string by commas and line breaks, respecting `<...>` and outer parentheses/brackets.
    """
    if not prompt:
        return []

    tokens: List[str] = []
    current: List[str] = []
    paren_depth = 0
    bracket_depth = 0
    angle_depth = 0

    i = 0
    length = len(prompt)
    while i < length:
        ch = prompt[i]

        # Check for BREAK token on word boundary
        if ch == 'B' and (i == 0 or prompt[i - 1] in ' \t\n,;') and paren_depth == 0 and bracket_depth == 0 and angle_depth == 0:
            if prompt[i:i + 5] == "BREAK" and (i + 5 == length or prompt[i + 5] in ' \t\n,;'):
                if "".join(current).strip():
                    tokens.append("".join(current).strip())
                    current = []
                tokens.append("BREAK")
                i += 5
                continue

        if ch == '<':
            angle_depth += 1
            current.append(ch)
        elif ch == '>':
            if angle_depth > 0:
                angle_depth -= 1
            current.append(ch)
        elif ch == '(':
            paren_depth += 1
            current.append(ch)
        elif ch == ')':
            if paren_depth > 0:
                paren_depth -= 1
            current.append(ch)
        elif ch == '[':
            bracket_depth += 1
            current.append(ch)
        elif ch == ']':
            if bracket_depth > 0:
                bracket_depth -= 1
            current.append(ch)
        elif ch in (',', '\n', '\r', ';') and paren_depth == 0 and bracket_depth == 0 and angle_depth == 0:
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
        else:
            current.append(ch)
        i += 1

    remaining = "".join(current).strip()
    if remaining:
        tokens.append(remaining)

    return tokens


def parse_tag_token(raw_token: str) -> TagASTItem:
    """
    Parse a single token into a TagASTItem preserving weights, brackets, and kinds.
    """
    token = raw_token.strip()
    if not token:
        return TagASTItem(tag="", raw="", weight=1.0, kind="tag")

    if token.upper() == "BREAK":
        return TagASTItem(tag="BREAK", raw="BREAK", weight=1.0, kind="break")

    # LoRA: <lora:name:0.8> or <lora:name:0.8:0.8>
    lora_match = RE_LORA.match(token)
    if lora_match:
        lora_name = lora_match.group(1).strip()
        weight = 1.0
        if lora_match.group(2):
            try:
                weight = float(lora_match.group(2))
            except ValueError:
                pass
        return TagASTItem(
            tag=lora_name,
            raw=token,
            weight=weight,
            kind="lora",
            canonical=f"<lora:{lora_name.lower()}>",
        )

    # Embedding: embedding:EasyNegative
    emb_match = RE_EMBEDDING.match(token)
    if emb_match:
        emb_name = emb_match.group(1).strip()
        return TagASTItem(
            tag=token,
            raw=token,
            weight=1.0,
            kind="embedding",
            canonical=token.lower(),
        )

    # Wildcard: __background__
    wc_match = RE_WILDCARD.match(token)
    if wc_match:
        return TagASTItem(
            tag=token,
            raw=token,
            weight=1.0,
            kind="wildcard",
            canonical=token.lower(),
        )

    # Weighted: (tag:1.2)
    m_weighted = RE_WEIGHTED_PAREN.match(token)
    if m_weighted:
        inner_tag = m_weighted.group(1).strip()
        try:
            w = float(m_weighted.group(2))
            return TagASTItem(
                tag=inner_tag,
                raw=token,
                weight=round(w, 3),
                bracket_type="paren",
                bracket_depth=1,
                kind="tag",
            )
        except ValueError:
            pass

    # Weighted bracket: [tag:0.8]
    m_wb = RE_WEIGHTED_BRACKET.match(token)
    if m_wb:
        inner_tag = m_wb.group(1).strip()
        try:
            w = float(m_wb.group(2))
            return TagASTItem(
                tag=inner_tag,
                raw=token,
                weight=round(w, 3),
                bracket_type="bracket",
                bracket_depth=1,
                kind="tag",
            )
        except ValueError:
            pass

    # Nested parentheses: ((tag)), (((tag))) or single (tag)
    # Check if string starts with ( and ends with ) and balanced
    if token.startswith("(") and token.endswith(")"):
        # Count leading and trailing parens
        leading = len(token) - len(token.lstrip("("))
        trailing = len(token) - len(token.rstrip(")"))
        depth = min(leading, trailing)
        if depth > 0:
            inner = token[depth:-depth].strip()
            # Ensure inner parens are balanced
            # Check if this isn't something like (a) and (b)
            if inner and not inner.startswith(":") and not inner.endswith(":"):
                # If inner has unbalanced parens, fallback
                calculated_weight = round(1.1 ** depth, 3)
                return TagASTItem(
                    tag=inner,
                    raw=token,
                    weight=calculated_weight,
                    bracket_type="paren",
                    bracket_depth=depth,
                    kind="tag",
                )

    # Nested brackets: [tag], [[tag]]
    if token.startswith("[") and token.endswith("]"):
        leading = len(token) - len(token.lstrip("["))
        trailing = len(token) - len(token.rstrip("]"))
        depth = min(leading, trailing)
        if depth > 0:
            inner = token[depth:-depth].strip()
            if inner:
                calculated_weight = round(0.9 ** depth, 3)
                return TagASTItem(
                    tag=inner,
                    raw=token,
                    weight=calculated_weight,
                    bracket_type="bracket",
                    bracket_depth=depth,
                    kind="tag",
                )

    # Plain tag
    return TagASTItem(
        tag=token,
        raw=token,
        weight=1.0,
        bracket_type=None,
        bracket_depth=0,
        kind="tag",
    )


def parse_prompt(prompt: str) -> List[TagASTItem]:
    """
    Parse a full prompt into a list of TagASTItem.
    """
    tokens = split_prompt_tokens(prompt)
    items: List[TagASTItem] = []
    for tok in tokens:
        item = parse_tag_token(tok)
        if item.tag or item.kind == "break":
            items.append(item)
    return items


def reconstruct_prompt(items: List[TagASTItem]) -> str:
    """
    Reconstruct prompt from AST items preserving formatting and weights.
    """
    parts: List[str] = []
    for item in items:
        if item.kind == "break":
            parts.append("BREAK")
        elif item.kind == "lora":
            # If raw exists, use raw; otherwise reconstruct
            if item.raw and item.raw.startswith("<lora:"):
                parts.append(item.raw)
            else:
                parts.append(f"<lora:{item.tag}:{item.weight}>")
        elif item.raw and item.raw != item.tag:
            # Has specific raw formatting preserved
            parts.append(item.raw)
        else:
            # Standard formatting
            if item.weight != 1.0:
                if item.bracket_type == "paren" and item.bracket_depth > 1:
                    parens_o = "(" * item.bracket_depth
                    parens_c = ")" * item.bracket_depth
                    parts.append(f"{parens_o}{item.tag}{parens_c}")
                elif item.bracket_type == "bracket" and item.bracket_depth > 0:
                    br_o = "[" * item.bracket_depth
                    br_c = "]" * item.bracket_depth
                    parts.append(f"{br_o}{item.tag}{br_c}")
                else:
                    parts.append(f"({item.tag}:{item.weight})")
            else:
                parts.append(item.tag)

    # Join nicely: BREAK shouldn't look awkward
    result = ""
    for i, p in enumerate(parts):
        if i == 0:
            result = p
        elif p == "BREAK":
            result += f", BREAK"
        elif parts[i - 1] == "BREAK":
            result += f", {p}"
        else:
            result += f", {p}"
    return result

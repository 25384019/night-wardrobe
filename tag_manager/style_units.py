from __future__ import annotations

import json
from typing import Iterable


def stable_dedupe(tokens: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        clean = " ".join(str(token).strip().split())
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def build_style_unit(
    artist_tokens: Iterable[str] = (),
    trigger_tokens: Iterable[str] = (),
    lora_refs: Iterable[str] = (),
    weight: float | None = None,
    style_extras: Iterable[str] = (),
    source: str = "",
    confidence: float = 1.0,
) -> dict[str, object]:
    artists = stable_dedupe(artist_tokens)
    triggers = stable_dedupe(trigger_tokens)
    extras = stable_dedupe(style_extras)
    refs = stable_dedupe(lora_refs)
    return {
        "id": refs[0] if refs else "",
        "name": refs[0] if refs else "",
        "display_name": refs[0] if refs else "",
        "tokens": stable_dedupe([*triggers, *artists, *extras]),
        "artist_tokens": artists,
        "trigger_tokens": triggers,
        "lora_refs": refs,
        "weight": weight,
        "style_extras": extras,
        "source": source,
        "confidence": confidence,
    }


def structure_prompt(
    prompt: str,
    *,
    artist_tokens: Iterable[str] = (),
    trigger_tokens: Iterable[str] = (),
    character_tokens: Iterable[str] = (),
    lora_refs: Iterable[str] = (),
    weight: float | None = None,
) -> dict[str, object]:
    raw_tokens = [part.strip() for part in str(prompt or "").split(",") if part.strip()]
    characters = stable_dedupe(character_tokens)
    character_keys = {token.casefold() for token in characters}
    artists = stable_dedupe(artist_tokens)
    triggers = stable_dedupe(trigger_tokens)
    style_keys = {token.casefold() for token in [*artists, *triggers]}
    style = stable_dedupe([token for token in raw_tokens if token.casefold() in style_keys])
    found_characters = stable_dedupe([token for token in raw_tokens if token.casefold() in character_keys])
    other = [token for token in raw_tokens if token.casefold() not in style_keys and token.casefold() not in character_keys]
    unit = build_style_unit(artists, triggers, lora_refs, weight, source="danbooru+lora_library")
    return {
        "original_prompt": str(prompt or ""),
        "composed_prompt": ", ".join(raw_tokens),
        "artist_tokens": style or artists,
        "character_tokens": found_characters,
        "other_tags": stable_dedupe(other),
        "style_unit": unit,
        "structured": True,
    }


def style_unit_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

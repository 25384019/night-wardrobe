from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Set

from .normalizer import canonical_tag, normalize_tag
from .schemas import EDIT_SCOPES, EditScope

RULES_DIR = Path(__file__).resolve().parent / "rules"

_RULES_CACHE: Dict[str, Any] = {}


def _get_rule_data(filename: str) -> dict:
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


def classify_tag_scope(tag: str, kind: str = "tag") -> EditScope:
    """
    Classify a tag into one of the 17 standard edit scopes.
    """
    if kind == "lora":
        return "lora"
    if kind == "break":
        return "other"
    if kind == "embedding":
        return "quality"
    if kind == "wildcard":
        return "other"

    norm = normalize_tag(tag)
    canon = canonical_tag(tag)

    # 1. Quality
    quality_keywords = {
        "masterpiece", "best quality", "highres", "absurdres", "ultra detailed",
        "extremely detailed", "8k", "4k", "high resolution", "very aesthetic",
        "newest", "sensitive", "general", "rating:general"
    }
    if norm in quality_keywords or any(q in norm for q in ["masterpiece", "best quality", "absurdres", "highres"]):
        return "quality"

    # 2. Identity (characters, series, counts)
    # Character with series in parens: e.g. klee (genshin impact)
    if re.search(r"\([^)]+\)", tag) and any(kw in tag for kw in ["impact", "game", "anime", "project", "touhou", "azur lane", "fate", "vocaloid", "idolmaster", "blue archive"]):
        return "identity"
    if norm in {"1girl", "2girls", "3girls", "1boy", "2boys", "solo", "multiple girls", "multiple boys", "nobody"}:
        return "identity"

    protected_data = _get_rule_data("protected_tags.json")
    species_traits = set(protected_data.get("species_traits", []))
    if norm in species_traits or any(trait in norm for trait in species_traits):
        # Species traits like cat ears, animal ears, pointy ears, wings, horns belong to identity/species
        return "identity"

    # 3. Hair
    ex_groups = _get_rule_data("exclusive_groups.json")
    hair_colors = set(ex_groups.get("hair_color", []))
    hair_lengths = set(ex_groups.get("hair_length", []))
    hair_styles = set(ex_groups.get("hair_style_primary", []))
    if norm in hair_colors or norm in hair_lengths or norm in hair_styles or "hair" in norm or "bangs" in norm or "braid" in norm or "ahoge" in norm or "bun" in norm:
        return "hair"

    # 4. Eyes
    eye_colors = set(ex_groups.get("eye_color", []))
    if norm in eye_colors or "eyes" in norm or "pupil" in norm or norm in {"heterochromia", "cross-eyed"}:
        return "eyes"

    # 5. Expression
    expression_keywords = {
        "smile", "smiling", "happy", "sad", "angry", "crying", "blush", "blushing",
        "tears", "frown", "open mouth", "closed mouth", "pout", "smug", "shy",
        "embarrassed", "surprised", "scared", "nervous", "screaming", "grin",
        "parted lips", "tongue out", "winking", "wink"
    }
    if norm in expression_keywords or any(e in norm for e in ["blush", "smile", "crying", "frown", "pout", "smug"]):
        return "expression"

    # 6. Face
    face_keywords = {"face", "freckles", "mole", "beauty mark", "scar", "makeup", "lipstick", "eyeshadow"}
    if norm in face_keywords or any(f in norm for f in face_keywords):
        return "face"

    # 7. Clothing
    clothing_slots = _get_rule_data("clothing_slots.json")
    for slot_name, items in clothing_slots.items():
        if norm in items or any(item in norm for item in items):
            if slot_name in ("headwear", "gloves"):
                return "accessories"
            return "clothing"
    if any(cw in norm for cw in ["dress", "skirt", "shirt", "pants", "suit", "uniform", "swimsuit", "bikini", "sweater", "hoodie", "jacket", "coat", "boots", "shoes", "socks", "lingerie", "bra", "panties", "trousers", "shorts"]):
        return "clothing"

    # 8. Accessories
    acc_keywords = {"hat", "cap", "glasses", "sunglasses", "ribbon", "bow", "necklace", "jewelry", "earrings", "ring", "belt", "hair ornament", "headdress", "choker", "backpack", "bag", "bandaid", "mask", "gloves", "mittens"}
    if norm in acc_keywords or any(a in norm for a in acc_keywords):
        return "accessories"

    # 9. Pose
    base_poses = set(ex_groups.get("base_pose", []))
    pose_keywords = {"standing", "sitting", "lying", "kneeling", "squatting", "crawling", "leaning", "from behind", "from side", "profile", "back", "all fours", "on stomach", "on back"}
    if norm in base_poses or norm in pose_keywords or any(p in norm for p in pose_keywords):
        return "pose"

    # 10. Action
    action_keywords = {"running", "walking", "jumping", "flying", "holding", "eating", "drinking", "reading", "sleeping", "hands up", "arms behind back", "hands on hips", "peace sign", "salute"}
    if norm in action_keywords or any(a in norm for a in action_keywords):
        return "action"

    # 11. Camera
    camera_keywords = {"close-up", "portrait", "cowboy shot", "full body", "upper body", "dutch angle", "wide shot", "from above", "from below", "fisheye", "straight-on", "side view", "back view", "looking at viewer", "looking away"}
    if norm in camera_keywords or any(c in norm for c in camera_keywords):
        return "camera"

    # 12. Background & Environment
    environments = set(ex_groups.get("environment", []))
    bg_keywords = {
        "outdoors", "indoors", "classroom", "beach", "street", "room", "window",
        "sky", "nature", "city", "sea", "ocean", "forest", "mountain", "ruins",
        "desk", "chair", "blackboard", "bed", "bedroom", "kitchen", "scenery",
        "cloud", "water", "tree", "flowers", "simple background", "white background"
    }
    if norm in environments or norm in bg_keywords or any(b in norm for b in bg_keywords):
        return "background"

    # 13. Lighting & Time
    times = set(ex_groups.get("time", []))
    lighting_keywords = {"day", "night", "sunrise", "sunset", "dusk", "dawn", "morning", "sunlight", "moonlight", "shadow", "cinematic lighting", "dim lighting", "backlighting", "glow", "lens flare", "god rays", "neon"}
    if norm in times or norm in lighting_keywords or any(l in norm for l in lighting_keywords):
        return "lighting"

    # 14. Style
    style_keywords = {"anime", "manga", "watercolor", "oil painting", "retro", "flat color", "monochrome", "lineart", "sketch", "comic", "realistic", "photorealistic", "pixel art"}
    if norm in style_keywords or any(s in norm for s in style_keywords):
        return "style"

    # 15. Body
    breast_sizes = set(ex_groups.get("breast_size", []))
    body_keywords = {"flat chest", "small breasts", "medium breasts", "large breasts", "huge breasts", "petite", "tall", "curvy", "muscular", "slender", "cleavage", "navel", "thighs", "collarbone", "bare shoulders", "bare legs"}
    if norm in breast_sizes or norm in body_keywords or any(b in norm for b in body_keywords):
        return "body"

    return "other"

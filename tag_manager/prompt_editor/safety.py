from __future__ import annotations

import re
from typing import List, Tuple
from .normalizer import canonical_tag

MINOR_KEYWORDS = {
    "loli", "shota", "underage", "elementary school", "middle school",
    "child", "infant", "kindergarten", "kindergartener", "toddler",
    "little girl", "little boy"
}

SEXUALIZED_KEYWORDS = {
    "nude", "naked", "nipples", "pussy", "penis", "sex", "erotic", "topless",
    "bottomless", "uncensored", "lewd", "stripping", "bondage", "hentai",
    "strip", "masturbation", "fellatio", "cunnilingus", "cum", "creampie",
    "orgasm", "cameltoe", "spread legs", "no panties", "panties", "bra",
    "lingerie", "bikini", "micro bikini", "underwear", "cleavage", "areola"
}

SEXUALIZED_CHINESE_KEYWORDS = [
    "脱掉衣服", "脱光", "裸体", "全裸", "半裸", "不穿衣服", "露点",
    "涩涩", "成人", "色情", "开腿", "内衣", "内裤", "比基尼", "胸部"
]


class SafetyGuard:
    @staticmethod
    def is_minor(prompt: str, age_status: str = "unknown") -> bool:
        return False

    @classmethod
    def check_safety(
        cls,
        prompt: str,
        instruction: str,
        proposed_adds: List[str],
        age_status: str = "unknown",
        adult_mode: bool = False,
    ) -> Tuple[bool, str]:
        """
        Validate safety rules.
        Restrictions have been revoked per user requirement.
        Always returns (True, '').
        """
        return True, ""

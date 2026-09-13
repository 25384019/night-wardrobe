from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .db import DB_PATH, connect, init_db

CODEX_RECIPE_TYPE = "codex_prompt"
DOCX_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PROMPT_MARKERS = re.compile(r"[A-Za-z][A-Za-z0-9_ :()\[\]{}'’./&!?-]*,")
TOC_END_MARKER = "以下正文"


@dataclass(frozen=True)
class CodexRecipe:
    title: str
    positive_prompt: str
    source: str
    section_path: str
    ordinal: int

    @property
    def import_key(self) -> str:
        raw = "\x1f".join((self.source, self.section_path, self.title, str(self.ordinal)))
        return "novelai-codex:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml")
    root = ElementTree.fromstring(document)
    paragraphs: list[str] = []
    for paragraph in root.iter(DOCX_NAMESPACE + "p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == DOCX_NAMESPACE + "t" and node.text:
                parts.append(node.text)
            elif node.tag == DOCX_NAMESPACE + "tab":
                parts.append("\t")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def looks_like_prompt(text: str) -> bool:
    return bool(PROMPT_MARKERS.search(text))


def looks_like_heading(text: str) -> bool:
    if looks_like_prompt(text) or len(text) > 160:
        return False
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def extract_codex_recipes(path: Path) -> list[CodexRecipe]:
    source = path.name
    paragraphs = docx_paragraphs(path)
    started = not any(TOC_END_MARKER in text for text in paragraphs)
    current_section: list[str] = []
    heading_buffer: list[str] = []
    prompt_lines: list[str] = []
    recipes: list[CodexRecipe] = []

    def flush() -> None:
        nonlocal current_section, heading_buffer, prompt_lines
        if not prompt_lines:
            prompt_lines = []
            return
        title = heading_buffer[-1] if heading_buffer else "未命名条目"
        if len(heading_buffer) > 1:
            current_section = heading_buffer[:-1]
        recipes.append(
            CodexRecipe(
                title=title,
                positive_prompt="\n".join(prompt_lines),
                source=source,
                section_path=" > ".join(current_section),
                ordinal=len(recipes) + 1,
            )
        )
        prompt_lines = []
        heading_buffer = []

    for text in paragraphs:
        if not started:
            if TOC_END_MARKER in text:
                started = True
            continue
        if looks_like_prompt(text):
            prompt_lines.append(text)
            continue
        if not looks_like_heading(text):
            continue
        flush()
        heading_buffer.append(text)

    flush()
    return recipes


def import_novelai_codex(paths: list[Path], db_path: Path = DB_PATH) -> dict[str, int]:
    init_db(db_path)
    imported = 0
    updated = 0
    skipped = 0
    with connect(db_path) as conn:
        for path in paths:
            for recipe in extract_codex_recipes(path):
                existing = conn.execute("SELECT id FROM recipes WHERE import_key=?", (recipe.import_key,)).fetchone()
                values = (
                    recipe.title,
                    CODEX_RECIPE_TYPE,
                    recipe.positive_prompt,
                    recipe.section_path,
                    recipe.source,
                    recipe.import_key,
                )
                if existing:
                    conn.execute(
                        """
                        UPDATE recipes
                        SET name=?, type=?, positive_prompt=?, notes=?, source=?, updated_at=CURRENT_TIMESTAMP
                        WHERE import_key=?
                        """,
                        values,
                    )
                    updated += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO recipes (name, type, positive_prompt, notes, source, import_key)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    imported += 1
    return {"imported": imported, "updated": updated, "skipped": skipped}

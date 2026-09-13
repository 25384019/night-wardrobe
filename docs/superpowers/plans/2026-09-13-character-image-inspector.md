# Character Image Inspector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add local Stable Diffusion/ComfyUI image metadata parsing and one-click character-card import.

**Architecture:** Reuse `gallery.read_image_metadata` and the existing SQLite character table. Add a focused parser/service module, JSON preview/import endpoints, and a themed panel in `characters.html`; store imported images under `character_previews`.

**Tech Stack:** FastAPI, Pillow, SQLite, Jinja2, vanilla JavaScript/CSS.

## Tasks

1. Add failing parser/API tests covering PNG parameters, ComfyUI workflow text, LoRA extraction, parse failures, and import persistence.
2. Implement parser helpers and character preview schema/storage with 20 MB and extension validation.
3. Add `/api/characters/inspect` and `/api/characters/import-inspection` endpoints, preserving full prompt text and metadata in character fields/notes.
4. Add the themed upload/inspection/import panel and preview styling to `characters.html` and `style.css`.
5. Run focused and full relevant tests, then verify the live `/characters` page and endpoint behavior.

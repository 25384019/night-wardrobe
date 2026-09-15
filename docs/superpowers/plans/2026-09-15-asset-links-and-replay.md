# Asset Links and Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add lightweight image-to-asset links, replay actions, and safe Outputs-page shortcuts.

**Architecture:** Reuse existing `output_images`, `characters`, `lora_cards`, and `output_loras`. Add read-only aggregation in the service/API boundary, then render it in the existing inspector; keyboard handlers remain page-local JavaScript.

**Tech Stack:** FastAPI, SQLite, Jinja2, vanilla JavaScript/CSS, pytest.

## Global Constraints

- Preserve navigation, SQLite, ComfyUI metadata parsing, Prompt Version behavior, and generation flow.
- No new dependencies, network requests, telemetry, or ComfyUI core changes.
- Migrations must preserve existing data and tolerate old databases.

### Task 1: Asset usage aggregation

**Files:** Modify `tag_manager/outputs_service.py`, `tag_manager/outputs_routes.py`; test `tag_manager/tests/test_asset_usage.py`.

- Add `get_output_asset_usage(image_id)` returning `{"characters": [], "loras": [], "workflows": [], "suggestions": []}`. Match LoRA IDs through `output_loras`; match characters by normalized LoRA name and return `id`, `name`, `usage_count`, `last_used`.
- Add `GET /api/outputs/assets?id=<positive integer>` returning the aggregate or 404 for missing image.
- Add tests for matched and unmatched records and exact JSON shape.

### Task 2: Inspector replay panel

**Files:** Modify `tag_manager/templates/outputs.html`, `tag_manager/static/ui/pages/images.css`; test API integration in `tag_manager/tests/test_asset_usage.py`.

- Fetch `/api/outputs/assets` after selecting an image and render character/LoRA usage plus a compact replay suggestion.
- Add buttons for copy prompt, copy workflow, fill workbench, and save character card; reuse existing functions and endpoints.
- Render empty and error states without breaking detail loading.

### Task 3: Character-card prefill

**Files:** Modify `tag_manager/characters_routes.py`, `tag_manager/templates/outputs.html`; test `tag_manager/tests/test_character_prefill.py`.

- Add `GET /api/characters/prefill?image_id=<id>` returning parsed name, LoRA, weight, trigger words, appearance, and notes.
- Use the response to open the existing character form with prefilled values; never auto-save.
- Return 404 for unknown image and 4xx for invalid IDs.

### Task 4: Keyboard shortcuts

**Files:** Modify `tag_manager/templates/outputs.html`; test `tag_manager/tests/test_outputs_contract.py`.

- Register a page-local `keydown` handler: Ctrl+C copies positive prompt, Ctrl+Enter fills workbench, Space opens preview.
- Ignore events from input, textarea, select, contenteditable, and modal dialogs; prevent default only when an action succeeds.
- Add visible shortcut hints to the existing buttons.

### Task 5: Verification and delivery

- Run `python -m compileall -q tag_manager`.
- Run `python -m pytest comfyui_nodes/night_wardrobe/tests tag_manager/tests -q`.
- Verify OpenAPI contains `/api/outputs/assets` and `/api/characters/prefill`.
- Commit each task separately and push `feature/style-unit` to the configured GitHub remote.

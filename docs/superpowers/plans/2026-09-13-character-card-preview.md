# Character Card Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make character-card previews use the same upright 2:3 visual treatment as LoRA cards.

**Architecture:** The existing character template and image URL remain unchanged. A narrowly scoped CSS change gives the preview container an aspect ratio and constrains the grid card width; `object-fit: contain` continues preserving the entire uploaded image.

**Tech Stack:** Jinja2 templates, static CSS, FastAPI static serving.

## Global Constraints

- Change only presentation styles; do not alter character records, image files, or import endpoints.
- Preserve complete image visibility with `object-fit: contain`.

---

### Task 1: Make character cards upright

**Files:**
- Modify: `tag_manager/static/style.css:920-921`

**Interfaces:**
- Consumes: `.char-card-preview` and `.char-card-preview img` emitted by `tag_manager/templates/characters.html`.
- Produces: A 2:3 preview area and bounded character-card width on desktop, with responsive width on narrow screens.

- [ ] **Step 1: Write the failing browser-style assertion**

Use a request to the local static CSS endpoint and assert it contains the 2:3 declaration:

```powershell
$css = (Invoke-WebRequest 'http://127.0.0.1:8765/static/style.css?v=87').Content
if ($css -match 'aspect-ratio:\s*2 / 3') { throw 'Style already changed' }
```

- [ ] **Step 2: Run the assertion to verify it fails**

Run the command above. Expected: it exits successfully because the existing preview uses a fixed 220px height rather than `aspect-ratio: 2 / 3`.

- [ ] **Step 3: Apply the minimal CSS change**

Replace the fixed preview height with `aspect-ratio: 2 / 3`, and constrain `.char-card` to the same readable card width used for the vertical presentation. Keep the image rule as `object-fit: contain`.

- [ ] **Step 4: Verify the served page and CSS**

```powershell
$page = Invoke-WebRequest 'http://127.0.0.1:8765/characters'
$css = (Invoke-WebRequest 'http://127.0.0.1:8765/static/style.css?v=87').Content
if ($page.StatusCode -ne 200 -or $css -notmatch 'aspect-ratio:\s*2 / 3' -or $css -notmatch 'object-fit:\s*contain') { throw '角色卡预览样式验证失败' }
```

Expected: the page returns HTTP 200 and its stylesheet includes the 2:3, full-image presentation rules.

- [ ] **Step 5: Commit**

```powershell
git add tag_manager/static/style.css
git commit -m 'Make character previews upright'
```

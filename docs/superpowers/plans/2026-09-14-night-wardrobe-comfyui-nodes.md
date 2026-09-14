# 夜之主衣柜 ComfyUI 节点 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供可在 ComfyUI 工作流中组装衣柜提示词、载入 LoRA 并保存/记录图源的本地三节点包。

**Architecture:** 节点包位于 `comfyui_nodes/night_wardrobe`，用一个 SQLite 访问层读取衣柜数据库。提示词节点输出文本及专用 LoRA 配置类型；LoRA 节点只调用 ComfyUI 原生加载能力；保存节点继承 ComfyUI 的安全输出目录、PNG 元数据与输出节点语义，再将成功图片增量写回衣柜。

**Tech Stack:** Python 3.11、SQLite、Pillow、ComfyUI 节点 API、`folder_paths`、`comfy.sd`。

## Global Constraints

- 仅访问本机数据库和输出目录；不增加任何网络请求、遥测或后台扫描。
- 保持节点名称、端口类型与输出顺序稳定；后续只增加可选输入。
- 不覆盖既有图片；保存失败或记录失败必须明确报告。
- 先写失败测试，再写最小实现；每项完成后执行对应测试。

---

### Task 1: 节点注册与衣柜数据库读取

**Files:**
- Create: `comfyui_nodes/night_wardrobe/__init__.py`
- Create: `comfyui_nodes/night_wardrobe/database.py`
- Create: `comfyui_nodes/night_wardrobe/nodes.py`
- Create: `comfyui_nodes/night_wardrobe/tests/test_database.py`

**Interfaces:**
- Produces: `resolve_database_path(path: str) -> Path`、`WardrobeRepository(path: Path)`、`NODE_CLASS_MAPPINGS`。
- Consumes: SQLite `characters`、`character_outfits`、`recipes`、`prompt_templates` 表。

- [ ] **Step 1: 写失败测试**

```python
def test_repository_reads_character_and_outfit(tmp_path):
    db_path = make_wardrobe_db(tmp_path)
    repo = WardrobeRepository(db_path)
    character = repo.get_character("可莉")
    assert character["appearance"] == "klee, 1girl"
    assert repo.get_outfit(character["id"], "常服") == "red dress"

def test_missing_database_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="衣柜数据库不存在"):
        resolve_database_path(str(tmp_path / "missing.sqlite3"))
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests/test_database.py -q`

- [ ] **Step 3: 实现最小只读仓储与节点映射**

```python
def resolve_database_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"衣柜数据库不存在：{path}")
    return path

class WardrobeRepository:
    def __init__(self, path: Path):
        self.path = path
```

- [ ] **Step 4: 运行测试确认通过并提交**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests/test_database.py -q`

### Task 2: 提示词组装节点

**Files:**
- Modify: `comfyui_nodes/night_wardrobe/database.py`
- Modify: `comfyui_nodes/night_wardrobe/nodes.py`
- Create: `comfyui_nodes/night_wardrobe/tests/test_prompt_builder.py`

**Interfaces:**
- Produces: `NightWardrobePromptBuilder.build(...) -> tuple[str, str, list[dict]]`。
- LoRA 配置元素：`{"name": str, "model_strength": float, "clip_strength": float}`。

- [ ] **Step 1: 写失败测试**

```python
def test_prompt_builder_keeps_workbench_order_and_deduplicates_lora(repo):
    positive, negative, loras = NightWardrobePromptBuilder().build(
        database_path=str(repo.path), character="可莉", outfit="常服",
        artist="画师串", scene="海边", negative_template="通用负面",
        positive_extra="sunset", negative_extra="blurry",
    )
    assert positive == "<lora:klee:1.0>, klee, 1girl, red dress, artist style, beach, sunset"
    assert negative == "lowres, blurry"
    assert loras == [{"name": "klee.safetensors", "model_strength": 1.0, "clip_strength": 1.0}]
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests/test_prompt_builder.py -q`

- [ ] **Step 3: 实现顺序拼接、空项忽略及 LoRA 去重**

```python
def merge_loras(items: list[dict]) -> list[dict]:
    merged = {}
    for item in items:
        merged[item["name"]] = item
    return list(merged.values())
```

- [ ] **Step 4: 运行测试确认通过并提交**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests/test_prompt_builder.py -q`

### Task 3: LoRA 载入节点

**Files:**
- Modify: `comfyui_nodes/night_wardrobe/nodes.py`
- Create: `comfyui_nodes/night_wardrobe/tests/test_lora_loader.py`

**Interfaces:**
- Consumes: `MODEL`、`CLIP`、`NIGHT_WARDROBE_LORAS`。
- Produces: `(MODEL, CLIP)`。

- [ ] **Step 1: 写失败测试**

```python
def test_lora_loader_returns_inputs_without_loras():
    model, clip = object(), object()
    assert NightWardrobeLoraLoader().load(model, clip, []) == (model, clip)

def test_lora_loader_reports_missing_file(monkeypatch):
    with pytest.raises(FileNotFoundError, match="klee.safetensors"):
        NightWardrobeLoraLoader().load(object(), object(), [{"name": "klee.safetensors", "model_strength": 1, "clip_strength": 1}])
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests/test_lora_loader.py -q`

- [ ] **Step 3: 调用 ComfyUI 原生 LoRA 加载接口**

```python
for item in loras:
    path = folder_paths.get_full_path("loras", item["name"])
    if path is None:
        raise FileNotFoundError(f"未找到 LoRA：{item['name']}")
    state = comfy.utils.load_torch_file(path, safe_load=True)
    model, clip = comfy.sd.load_lora_for_models(model, clip, state, item["model_strength"], item["clip_strength"])
```

- [ ] **Step 4: 运行测试确认通过并提交**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests/test_lora_loader.py -q`

### Task 4: 保存并记录图源节点

**Files:**
- Modify: `comfyui_nodes/night_wardrobe/database.py`
- Modify: `comfyui_nodes/night_wardrobe/nodes.py`
- Create: `comfyui_nodes/night_wardrobe/tests/test_save_record.py`
- Create: `comfyui_nodes/night_wardrobe/README.md`

**Interfaces:**
- Consumes: `IMAGE`、正面/负面提示词、`NIGHT_WARDROBE_LORAS`、Checkpoint、文件名前缀、数据库路径、可选 `PROMPT` / `EXTRA_PNGINFO`。
- Produces: `{"ui": {"images": [...]}}`，并只在 PNG 保存成功后创建输出记录。

- [ ] **Step 1: 写失败测试**

```python
def test_save_node_writes_png_metadata_then_records_source(tmp_path, image_batch):
    result = NightWardrobeSaveAndRecord(output_dir=tmp_path).save_images(
        image_batch, "1girl", "blurry", [], "base.safetensors", "Wardrobe", db_path
    )
    assert (tmp_path / result["ui"]["images"][0]["filename"]).is_file()
    assert read_record(db_path)["positive_prompt"] == "1girl"

def test_record_failure_keeps_saved_png(tmp_path, image_batch, broken_db):
    with pytest.raises(RuntimeError, match="图片已保存，图源记录失败"):
        NightWardrobeSaveAndRecord(output_dir=tmp_path).save_images(
            image_batch, "1girl", "", [], "", "Wardrobe", broken_db
        )
    assert list(tmp_path.glob("*.png"))
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests/test_save_record.py -q`

- [ ] **Step 3: 按 ComfyUI SaveImage 语义保存 PNG 并调用衣柜记录器**

```python
metadata = PngInfo()
metadata.add_text("prompt", json.dumps(prompt, ensure_ascii=False))
metadata.add_text("workflow", json.dumps(extra_pnginfo.get("workflow", {}), ensure_ascii=False))
image.save(path, pnginfo=metadata, compress_level=4)
record_output(db_path, path, positive, negative, checkpoint, loras)
```

- [ ] **Step 4: 运行节点全部测试与既有衣柜测试，检查安装说明后提交**

Run: `python -m pytest comfyui_nodes/night_wardrobe/tests tag_manager/tests/test_outputs.py -q`

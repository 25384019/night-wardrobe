from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tag_manager import app as app_module
from tag_manager import db


class StartupGalleryScanTests(unittest.TestCase):
    def test启动时先初始化数据库再扫描图库且各执行一次(self) -> None:
        calls: list[str] = []
        with (
            patch.object(app_module, "init_db", side_effect=lambda: calls.append("init_db")) as init_db,
            patch.object(
                app_module,
                "scan_gallery",
                side_effect=lambda **_kwargs: calls.append("scan_gallery"),
            ) as scan_gallery,
        ):
            app_module.startup()

        self.assertEqual(["init_db", "scan_gallery"], calls)
        init_db.assert_called_once_with()
        scan_gallery.assert_called_once_with(initialize_db=False)


class WorkshopFirstVisitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        db.init_db(self.db_path)
        with db.connect(self.db_path) as conn:
            character_id = conn.execute(
                "INSERT INTO characters (name, lora, trigger_words, appearance) VALUES (?, ?, ?, ?)",
                ("首访角色", "first.safetensors", "first tag", "外观"),
            ).lastrowid
            conn.execute(
                "INSERT INTO character_outfits (character_id, name, tags) VALUES (?, ?, ?)",
                (character_id, "首访服装", "dress"),
            )
            conn.executemany(
                "INSERT INTO recipes (name, type, positive_prompt) VALUES (?, ?, ?)",
                [
                    ("首访画师", "artist_mix", "artist tag"),
                    ("首访场景", "scene", "scene tag"),
                    ("首访负面", "negative", "negative tag"),
                ],
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test工坊首个响应已包含全部下拉选项(self) -> None:
        with patch.object(app_module, "connect", lambda: db.connect(self.db_path)):
            response = TestClient(app_module.app).get("/workshop")

        self.assertEqual(200, response.status_code)
        for expected in ("首访角色", "首访画师", "首访场景", "首访负面"):
            self.assertIn(expected, response.text)
        self.assertNotIn("fetch('/api/characters')", response.text)
        self.assertNotIn("fetch('/api/recipes')", response.text)

    def test工坊与接口复用同一数据序列化函数(self) -> None:
        with patch.object(app_module, "connect", lambda: db.connect(self.db_path)):
            characters = app_module.get_characters_data()
            recipes = app_module.get_recipes_data()

        self.assertEqual("首访角色", characters[0]["name"])
        self.assertEqual("首访服装", characters[0]["outfits"][0]["name"])
        self.assertEqual({"artist_mix", "scene", "negative"}, {recipe["type"] for recipe in recipes})


class WorkshopHighlightContractTests(unittest.TestCase):
    """v1.6.8 工坊提示词分类高亮契约（v1.15.0 工作台化后保留并扩展）。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.template = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

    def test渲染函数与类别标记(self) -> None:
        self.assertIn("function renderPreview(el, parts)", self.template)
        self.assertIn("createElement('span')", self.template)
        self.assertIn("'ws-hl ws-hl-' + p.cat", self.template)
        # v1.15.0：着色段带 data-cat 供分类过滤与诊断定位
        self.assertIn("setAttribute('data-cat', p.cat)", self.template)
        for cat in ("char", "outfit", "artist", "scene", "custom", "scene-neg", "neg", "custom-neg"):
            self.assertIn(f"cat: '{cat}'", self.template)

    def test分类色样式与chips图例(self) -> None:
        self.assertIn(".ws-hl {", self.style)
        for cat in ("char", "outfit", "artist", "scene", "custom", "scene-neg", "neg", "custom-neg"):
            self.assertIn(f".ws-hl-{cat}", self.style)
        # v1.15.0：静态图例改为可点击过滤 chips（兼作图例）
        self.assertIn('class="ws-filter-chips"', self.template)
        for cat in ("char", "outfit", "artist", "scene", "custom", "negative"):
            self.assertIn(f'data-filter-cat="{cat}"', self.template)
            self.assertIn(f".ws-chip-{cat}", self.style)
        self.assertIn(".ws-dim", self.style)
        self.assertIn("activeFilters", self.template)
        self.assertNotIn('class="ws-legend"', self.template)

    def test提示词不走innerHTML拼接(self) -> None:
        # 提示词含 <lora:...> 尖括号，必须 DOM textContent 赋值
        render_section = self.template.split("function renderPreview", 1)[1].split("function buildPrompt", 1)[0]
        self.assertNotIn("innerHTML", render_section)

    def test选项应用对应高亮色(self) -> None:
        # v1.6.9：左侧选项控件带与预览分段同色的 ws-cat-* 类
        for element_id, cat in (("ws-char", "char"), ("ws-outfit", "outfit"), ("ws-artist", "artist"),
                                ("ws-scene", "scene"), ("ws-negative", "neg"),
                                ("ws-custom-pos", "custom"), ("ws-custom-neg", "custom-neg")):
            self.assertIn(f'id="{element_id}" class="ws-cat-{cat}"', self.template)
            self.assertIn(f".ws-cat-{cat}", self.style)

    def test下拉框带清除按钮(self) -> None:
        # v1.7.0：每个下拉框 × 清除按钮，有值才显示；清除回到占位项
        for element_id in ("ws-char", "ws-outfit", "ws-artist", "ws-scene", "ws-negative"):
            self.assertIn(f'data-clear="{element_id}"', self.template)
        # v1.15.0：× 从独占列改为内嵌 select 右侧的 ws-select-wrap 容器
        self.assertEqual(5, self.template.count('class="ws-select-wrap"'))
        self.assertIn('id="ws-outfit-row"', self.template)
        self.assertIn("function clearWsSelect(id)", self.template)
        self.assertIn("function updateClearButtons()", self.template)
        self.assertIn("sessionStorage.removeItem('_persist_' + id)", self.template)
        # 服装行整体显隐（hidden 属性），避免 select 隐藏后 × 按钮单独残留
        self.assertIn("outfitRow.hidden", self.template)
        self.assertNotIn("outfitSel.style.display", self.template)
        self.assertIn(".ws-select-wrap", self.style)
        self.assertIn(".ws-clear", self.style)
        # v1.7.1：选中后隐藏占位项，清除后恢复
        self.assertIn("placeholder.hidden = !!sel.value", self.template)
        # v1.7.2：占位项 disabled 不可点选，只作空态显示（模板 5 处 + 服装行 JS 重建 1 处）
        self.assertGreaterEqual(self.template.count('<option value="" disabled>'), 6)


class WorkshopWorkbenchLayoutTests(unittest.TestCase):
    """v1.15.0 工坊页紧凑 Prompt 工作台布局契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.tpl = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

    def test紧凑Header与操作区(self) -> None:
        tpl = self.tpl
        self.assertIn('class="ws-header"', tpl)
        self.assertIn('class="ws-header-actions"', tpl)
        # v1.20.0：AI 助手/复制全部/更多操作均为纯图标按钮，契约锚点是 id 与 aria-label
        self.assertIn('aria-label="AI 助手"', tpl)
        self.assertIn('id="wsCopilotBtn"', tpl)
        self.assertIn("copyBoth()", tpl)
        self.assertIn('aria-label="复制全部"', tpl)
        # v1.23.0：助手设置齿轮入口与 JSON 弹层，提交不再整页刷新
        self.assertIn('id="wsLlmSettingsBtn"', tpl)
        self.assertIn('aria-label="AI 提示词助手设置"', tpl)
        self.assertIn('class="folder-dialog" id="wsLlmSettingsDialog"', tpl)
        self.assertIn("/api/copilot/settings", tpl)
        self.assertIn("/static/copilot-settings.js?v=4", tpl)
        # ··· 菜单：重置自定义/重置 Prompt/清除全部（danger）
        self.assertIn('id="wsHeaderMenu"', tpl)
        self.assertIn('data-ws-menu="reset-custom"', tpl)
        self.assertIn('data-ws-menu="reset-prompt"', tpl)
        self.assertIn('data-ws-menu="clear-all"', tpl)
        # v1.20.0：死占位（格式化/排序/清理重复）已移除，编辑块只留复制图标
        self.assertNotIn("规划中", tpl)
        self.assertNotIn("data-ws-action=\"format\"", tpl)
        self.assertNotIn("data-ws-action=\"sort\"", tpl)
        self.assertNotIn("data-ws-action=\"dedupe\"", tpl)

    def test清除全部二次确认弹窗(self) -> None:
        tpl = self.tpl
        self.assertIn('class="folder-dialog" id="wsClearDialog"', tpl)
        self.assertIn("confirmClearAll()", tpl)
        self.assertIn("showModal()", tpl)
        # 清除全部不再平级直出按钮
        self.assertNotIn("onclick=\"clearAll()\"", tpl)
        # .folder-dialog 自身 padding:0，内边距挂在子容器上；无 form 的弹窗需 folder-dialog-body
        self.assertIn('class="folder-dialog-body"', tpl)
        self.assertIn(".folder-dialog-body { display: grid; gap: 16px; padding: 20px; }", self.style)
        # v1.20.0：··· 菜单按钮改为图标按钮，复用图库 .icon-btn 视觉（工坊作用域）
        self.assertIn(".ws-header-actions .icon-btn", self.style)
        self.assertIn("background: rgba(30,41,59,.82)", self.style.split(".ws-header-actions .icon-btn", 1)[1][:300])

    def test三栏骨架与配方区(self) -> None:
        tpl = self.tpl
        self.assertIn('class="ws-workbench"', tpl)
        self.assertIn('class="ws-layout"', tpl)
        self.assertIn('class="ws-recipe-panel"', tpl)
        self.assertIn('class="ws-workspace"', tpl)
        self.assertIn('class="ws-copilot-pane"', tpl)
        for element_id in ("ws-char", "ws-outfit", "ws-artist", "ws-scene", "ws-negative"):
            self.assertIn(f'id="{element_id}"', tpl)
        # 自定义补充 progressive disclosure
        self.assertIn('data-custom-toggle="pos"', tpl)
        self.assertIn('data-custom-toggle="neg"', tpl)
        self.assertIn('id="ws-custom-pos-area" hidden', tpl)
        self.assertIn('id="ws-custom-neg-area" hidden', tpl)
        self.assertIn("_persist_ws-custom-open", tpl)

    def test编辑器块结构(self) -> None:
        tpl = self.tpl
        self.assertEqual(2, tpl.count('class="ws-editor-block"'))
        for target in ("positive", "negative"):
            self.assertIn(f'data-stats="{target}"', tpl)
            self.assertIn(f'data-edited-badge="{target}"', tpl)
            self.assertIn(f'data-edited-banner="{target}"', tpl)
        self.assertIn("已编辑", tpl)
        self.assertIn("配方已变化，当前为手工编辑版本", tpl)
        # v1.20.0：恢复自动入口收敛到编辑横幅（头部 ··· 死占位菜单已移除）
        self.assertIn("恢复自动", tpl)
        self.assertIn('data-ws-action="restore"', tpl)
        self.assertIn('id="ws-positive-preview"', tpl)
        self.assertIn('id="ws-negative-preview"', tpl)
        # 点击即编辑，不再有「编辑」按钮
        self.assertIn("startEdit(", tpl)
        self.assertIn("finishEdit(", tpl)

    def test旧AI入口与悬浮窗已删除(self) -> None:
        tpl = self.tpl
        for removed in ("btn-llm", "llm-panel", "optimizeWithLLM", "applyLLMResult", "toggleEditPreview", "floating-panel"):
            self.assertNotIn(removed, tpl)
        self.assertNotIn(".btn-llm", self.style)

    def test布局样式存在(self) -> None:
        for sel in (".ws-header {", ".ws-workbench {", ".ws-layout {", ".ws-recipe-panel {", ".ws-editor-block {",
                    ".ws-edited-badge", ".ws-edited-banner", ".ws-hl-edited", ".ws-flash", ".ws-toast"):
            self.assertIn(sel, self.style)

    def test清理钩子与脚本加载(self) -> None:
        tpl = self.tpl
        self.assertIn("__wardrobePageCleanup", tpl)
        self.assertIn("WorkshopCopilot.destroy", tpl)
        self.assertIn('<script src="/static/workshop-copilot.js?v=77"></script>', tpl)
        self.assertIn("/static/copilot/copilot.js?v=84", tpl)


class WorkshopDirtyTrackContractTests(unittest.TestCase):
    """v1.15.0 双轨 dirty 手改保护契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tpl = (Path(app_module.BASE_DIR) / "templates" / "workshop.html").read_text(encoding="utf-8")

    def test手改状态与持久化键(self) -> None:
        tpl = self.tpl
        self.assertIn("editedTrack", tpl)
        self.assertIn("editedBase", tpl)
        self.assertIn("_persist_ws-edited-pos", tpl)
        self.assertIn("_persist_ws-edited-neg", tpl)
        self.assertIn("function renderTrack(type)", tpl)
        self.assertIn("function restoreAuto(type)", tpl)
        self.assertIn("function joinParts(parts)", tpl)

    def test手改完成条件与恢复路径(self) -> None:
        tpl = self.tpl
        # 与自动生成完全一致则不落手改轨
        self.assertIn("if (text !== generated)", tpl)
        # 清除全部/重置 Prompt 同步清手改键
        clear_section = tpl.split("function confirmClearAll()", 1)[1]
        self.assertIn("sessionStorage.removeItem('_persist_ws-edited-pos')", clear_section)
        reset_section = tpl.split("function handleWsMenu(action)", 1)[1]
        self.assertIn("sessionStorage.removeItem('_persist_ws-edited-neg')", reset_section)

    def testWorkshopAPI供Copilot对接(self) -> None:
        tpl = self.tpl
        self.assertIn("window.WorkshopAPI", tpl)
        self.assertIn("getPromptPayload()", tpl)
        self.assertIn("applyEdited(target, text)", tpl)
        self.assertIn("highlightTag: highlightTag", tpl)
        # AI Apply 成功后该块置手改态
        apply_section = tpl.split("applyEdited(target, text)", 1)[1]
        self.assertIn("editedTrack[type] = text", apply_section)

    def test取值统一走promptText而非读占位提示(self) -> None:
        """空态 pre 里是占位提示文案，统计/复制/Copilot 载荷都不能把它当内容读。"""
        tpl = self.tpl
        self.assertIn("function promptText(type)", tpl)
        for caller in ("const text = promptText(type);",
                       "navigator.clipboard.writeText(promptText(type))",
                       "promptText('positive')",
                       "promptText('negative')",
                       "ta.value = promptText(type);"):
            self.assertIn(caller, tpl)
        # 占位提示只由 renderEmptyHint 写入，读侧不再碰 pre.textContent
        self.assertNotIn("preOf(type).textContent", tpl)
        self.assertNotIn("preOf('positive').textContent", tpl)
        self.assertNotIn("preOf('negative').textContent", tpl)

    def test手工改回自动结果即退出手改轨(self) -> None:
        section = self.tpl.split("function finishEdit(type, ta)", 1)[1].split("function restoreAuto", 1)[0]
        self.assertIn("} else if (editedTrack[type] !== null) {", section)
        self.assertIn("editedTrack[type] = null;", section)
        self.assertIn("sessionStorage.removeItem(editedKey(type));", section)


class TagPreviewAnimationTests(unittest.TestCase):
    def test预览拖动包含实时重排和移动挤压动画契约(self) -> None:
        base_dir = Path(app_module.BASE_DIR)
        template = (base_dir / "templates" / "tags.html").read_text(encoding="utf-8")
        style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn("on(preview, 'dragover', movePreviewTagDrag)", template)
        self.assertIn("previewPositions(previewEl)", template)
        self.assertIn("getBoundingClientRect()", template)
        self.assertIn("requestAnimationFrame", template)
        self.assertIn("target.after(dragged)", template)
        self.assertIn("target.before(dragged)", template)
        self.assertIn("transition: transform .18s", style)
        self.assertIn("prefers-reduced-motion", style)


class TagSubcategoryCascadeTests(unittest.TestCase):
    def test编辑与新增表单二级分类随一级分类级联(self) -> None:
        base_dir = Path(app_module.BASE_DIR)
        template = (base_dir / "templates" / "tags.html").read_text(encoding="utf-8")
        app_src = (base_dir / "app.py").read_text(encoding="utf-8")

        # 服务端提供完整 一级→二级 映射并传入模板
        self.assertIn("def get_category_subcategory_map", app_src)
        self.assertIn('"category_sub_map": category_sub_map', app_src)
        self.assertIn("var SUBCATEGORY_MAP = {{ category_sub_map|tojson }};", template)
        # 两个一级分类下拉都接级联
        self.assertEqual(template.count('onchange="handleCategoryCascade(this)"'), 2)
        # 级联重建函数与编辑弹窗接入
        self.assertIn("function buildSubcategoryOptions(", template)
        self.assertIn("function handleCategoryCascade(", template)
        self.assertIn("buildSubcategoryOptions(fresh, btn.dataset.category", template)


class RecipeCopyContractTests(unittest.TestCase):
    def test配方复制改为按id读map不再内联拼接(self) -> None:
        base_dir = Path(app_module.BASE_DIR)
        template = (base_dir / "templates" / "recipes.html").read_text(encoding="utf-8")

        # 复制按钮按 id + 字段从 map 取，不再把 prompt 内联进 onclick
        self.assertIn("copyRecipeField(this, {{ row.id }}, 'positive_prompt')", template)
        self.assertIn("copyRecipeField(this, {{ row.id }}, 'negative_prompt')", template)
        self.assertIn("function copyRecipeField(", template)
        # recipesData 各字段一律走 tojson，杜绝引号/反斜杠/反引号/${ 注入
        for f in ("name", "type", "positive_prompt", "negative_prompt", "params_json", "notes"):
            self.assertIn(f"{f}: {{{{ row.{f}|tojson }}}}", template)
        # 旧的脆弱写法已移除
        self.assertNotIn('|replace("\'", "\\\\\'")', template)
        self.assertNotIn("copyText(this, '", template)


class GalleryIconToolbarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.tpl = (base_dir / "templates" / "gallery.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

    def test工具栏右侧改为纯图标按钮且带文字提示(self) -> None:
        tpl = self.tpl
        self.assertIn('class="icon-actions"', tpl)
        self.assertGreaterEqual(tpl.count('class="icon-btn"'), 5)
        for title in ("新建文件夹", "上传图片", "导入 ZIP", "扫描图库", "导出 ZIP", "当前文件夹操作"):
            self.assertIn(f'title="{title}', tpl)
            self.assertIn(f'aria-label="{title}', tpl)
        # 旧的文字按钮形态已移除
        self.assertNotIn("folder-inline-form", tpl)
        self.assertNotIn("btn-upload", tpl)
        self.assertNotIn("current-folder-actions", tpl)
        self.assertNotIn(">新建文件夹</button>", tpl)
        self.assertNotIn(">扫描图库</button>", tpl)

    def test表单动作与JS钩子契约保留(self) -> None:
        tpl = self.tpl
        for action in ('action="/gallery/folders"', 'action="/gallery/upload"', 'action="/gallery/import"', 'action="/scan-gallery"', 'action="/gallery/export"'):
            self.assertIn(action, tpl)
        self.assertIn('name="files"', tpl)
        self.assertIn('name="folder" value="{{ folder }}"', tpl)
        # 文件夹管理弹窗的事件委托钩子与数据属性一个不能少
        self.assertIn("data-folder-move", tpl)
        self.assertIn("data-folder-delete", tpl)
        self.assertIn("data-folder-tracked-count", tpl)
        self.assertIn("data-drop-folder", tpl)
        # 气泡开关与新建文件夹气泡表单
        self.assertIn('data-pop-toggle="newFolderPop"', tpl)
        self.assertIn('id="newFolderPop" hidden', tpl)

    def test图标按钮与气泡样式存在(self) -> None:
        for sel in (".icon-actions {", ".toolbar .icon-btn {", ".icon-pop-wrap {", ".icon-pop {", ".icon-menu {"):
            self.assertIn(sel, self.style)


class GalleryIncrementalScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.gallery_root = self.base / "gallery"
        self.gallery_root.mkdir()
        self.db_path = self.base / "test.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def scan_patches(self):
        from tag_manager import gallery

        return (
            patch.object(gallery, "GALLERY_DIR", self.gallery_root),
            patch.object(gallery, "connect", lambda: db.connect(self.db_path)),
        )

    def test二次扫描跳过未变化文件且变更后重新解析(self) -> None:
        from tag_manager import gallery

        target = self.gallery_root / "样本.png"
        target.write_bytes(b"fake-png-v1")
        gallery_dir_patch, connect_patch = self.scan_patches()
        with gallery_dir_patch, connect_patch:
            self.assertEqual(1, gallery.scan_gallery(self.gallery_root, initialize_db=False))
            # 指纹未变：二次扫描完全跳过
            self.assertEqual(0, gallery.scan_gallery(self.gallery_root, initialize_db=False))
            # 文件内容变化（大小变化）：重新解析
            target.write_bytes(b"fake-png-v2-longer")
            self.assertEqual(1, gallery.scan_gallery(self.gallery_root, initialize_db=False))
            row = conn_row(self.db_path, "SELECT file_size FROM gallery_images WHERE path = '样本.png'")
            self.assertEqual(len(b"fake-png-v2-longer"), row["file_size"])

    def test定向入库只处理给定文件(self) -> None:
        from tag_manager import gallery

        first = self.gallery_root / "已上传.png"
        second = self.gallery_root / "未处理.png"
        first.write_bytes(b"fake-a")
        second.write_bytes(b"fake-b")
        gallery_dir_patch, connect_patch = self.scan_patches()
        with gallery_dir_patch, connect_patch:
            count = gallery.ingest_saved_paths([first], initialize_db=False)

        self.assertEqual(1, count)
        with db.connect(self.db_path) as conn:
            paths = [row["path"] for row in conn.execute("SELECT path FROM gallery_images")]
        self.assertEqual(["已上传.png"], paths)

    def testZIP导入返回保存路径供定向入库(self) -> None:
        import io
        import zipfile

        from PIL import Image

        from tag_manager import gallery

        png_buf = io.BytesIO()
        Image.new("RGB", (2, 2), (255, 0, 0)).save(png_buf, format="PNG")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("打包/图一.png", png_buf.getvalue())
        gallery_dir_patch, connect_patch = self.scan_patches()
        with gallery_dir_patch, connect_patch:
            saved = gallery.import_gallery_zip(buf.getvalue())
            self.assertEqual(1, len(saved))
            count = gallery.ingest_saved_paths(saved, initialize_db=False)

        self.assertEqual(1, count)
        with db.connect(self.db_path) as conn:
            paths = [row["path"] for row in conn.execute("SELECT path FROM gallery_images")]
        self.assertEqual(["打包/图一.png"], paths)


def conn_row(db_path, sql):
    with db.connect(db_path) as conn:
        return conn.execute(sql).fetchone()


class GalleryCardMenuIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.tpl = (base_dir / "templates" / "gallery.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

    def test卡片菜单改为图标按钮且带文字提示(self) -> None:
        tpl = self.tpl
        # 文件夹卡与图片卡两个菜单均为图标形态
        self.assertEqual(2, tpl.count('class="folder-card-menu is-icons"'))
        self.assertEqual(2, tpl.count('class="menu-icon-btn" title="移动到…"'))
        self.assertIn('class="menu-icon-btn danger" title="删除文件夹"', tpl)
        self.assertIn('class="menu-icon-btn danger" title="删除图片"', tpl)
        # 卡片菜单的文字菜单项已移除（底部批量选择栏的文字按钮不在本契约范围）
        self.assertNotIn('data-image-move>移动到…</button>', tpl)
        self.assertNotIn('data-image-delete>删除图片</button>', tpl)
        self.assertNotIn('data-folder-other-count="{{ item.other_file_count }}">移动到…</button>', tpl)
        self.assertNotIn('data-folder-other-count="{{ item.other_file_count }}">删除文件夹</button>', tpl)

    def test卡片菜单JS钩子与hidden优先级保留(self) -> None:
        tpl = self.tpl
        for hook in ("data-folder-move", "data-folder-delete", "data-image-move", "data-image-delete", "data-folder-menu-toggle", "data-image-menu-toggle"):
            self.assertIn(hook, tpl)
        # is-icons 的 display:flex 不得盖掉 hidden
        self.assertIn(".folder-card-menu.is-icons:not([hidden])", self.style)
        self.assertIn(".folder-card-menu .menu-icon-btn {", self.style)


class TagEditModeToggleMoveTests(unittest.TestCase):
    """v1.12.1：「Tag修改」开关从抽屉工作台移到页面头部。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tpl = (Path(app_module.BASE_DIR) / "templates" / "tags.html").read_text(encoding="utf-8")

    def test修改开关在页面头部且位于密度切换之前(self) -> None:
        head_start = self.tpl.index('<div class="page-head-actions">')
        head_end = self.tpl.index("</div>\n  </div>", head_start)
        head_block = self.tpl[head_start:head_end]
        self.assertIn('id="editModeToggle"', head_block)
        self.assertIn("toggleEditMode()", head_block)
        self.assertLess(head_block.index("editModeToggle"), head_block.index("tag-density-toggle"))

    def test工作台只保留Tag预览开关(self) -> None:
        group = self.tpl.split('<div class="tag-mode-group">', 1)[1].split("</div>", 1)[0]
        self.assertIn('id="previewModeToggle"', group)
        self.assertNotIn("editModeToggle", group)
        # 全文只剩页头一处 Tag修改 开关
        self.assertEqual(1, self.tpl.count('id="editModeToggle"'))


class WorkflowIconToolbarTests(unittest.TestCase):
    """v1.12.1：工作流页工具栏按图库风格纯图标化。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.tpl = (base_dir / "templates" / "workflows.html").read_text(encoding="utf-8")
        cls.gallery = (base_dir / "templates" / "gallery.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")
        cls.folder_js = (base_dir / "static" / "folder-management.js").read_text(encoding="utf-8")

    def test工具栏右侧改为纯图标按钮且带文字提示(self) -> None:
        tpl = self.tpl
        self.assertIn('class="icon-actions"', tpl)
        self.assertGreaterEqual(tpl.count('class="icon-btn"'), 5)
        for title in ("新建文件夹", "上传工作流", "导入 ZIP", "扫描工作流", "导出 ZIP", "当前文件夹操作"):
            self.assertIn(f'title="{title}', tpl)
            self.assertIn(f'aria-label="{title}', tpl)
        # 旧的文字按钮与上传折叠面板形态已移除
        self.assertNotIn("folder-inline-form", tpl)
        self.assertNotIn("btn-upload", tpl)
        self.assertNotIn("current-folder-actions", tpl)
        self.assertNotIn("workflow-upload-panel", tpl)
        self.assertNotIn("workflow-dropzone", tpl)
        self.assertNotIn(">新建文件夹</button>", tpl)
        self.assertNotIn(">扫描工作流</button>", tpl)

    def test表单动作与JS钩子契约保留(self) -> None:
        tpl = self.tpl
        for action in ('action="/workflows/folders"', 'action="/workflows/upload"', 'action="/workflows/import"',
                       'action="/scan-workflows"', 'action="/workflows/export"'):
            self.assertIn(action, tpl)
        self.assertIn('name="folder" value="{{ folder }}"', tpl)
        self.assertIn("data-folder-move", tpl)
        self.assertIn("data-folder-delete", tpl)
        self.assertIn("data-folder-tracked-count", tpl)
        self.assertIn('data-pop-toggle="newFolderPop"', tpl)
        self.assertIn('id="newFolderPop" hidden', tpl)
        self.assertIn('data-pop-toggle="folderOpsPop"', tpl)
        # 拖到 ComfyUI 导入的 DownloadURL 钩子不动
        self.assertIn("setData('DownloadURL'", tpl)

    def test工作流文件夹卡菜单同步图标化(self) -> None:
        tpl = self.tpl
        self.assertIn('class="folder-card-menu is-icons"', tpl)
        self.assertIn('class="menu-icon-btn" title="移动到…"', tpl)
        self.assertIn('class="menu-icon-btn danger" title="删除文件夹"', tpl)
        self.assertNotIn('data-folder-other-count="{{ item.other_file_count }}">移动到…</button>', tpl)
        self.assertNotIn('data-folder-other-count="{{ item.other_file_count }}">删除文件夹</button>', tpl)

    def test气泡切换逻辑共享到folder_management且图库不再内联(self) -> None:
        self.assertIn("[data-pop-toggle]", self.folder_js)
        self.assertIn("closePops", self.folder_js)
        self.assertNotIn("closeAllPops", self.gallery)

    def test被替换的旧工具栏样式已清除(self) -> None:
        for sel in (".btn-upload", ".folder-inline-form", ".current-folder-actions", ".workflow-upload-panel", ".workflow-dropzone"):
            self.assertNotIn(sel, self.style)


class ScanIconSpinTests(unittest.TestCase):
    """v1.12.2：扫描刷新图标旋转动效（悬停半圈，点击获焦持续旋转）。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

    def test两页扫描按钮带icon_scan类(self) -> None:
        for name, action in (("gallery", "/scan-gallery"), ("workflows", "/scan-workflows")):
            tpl = (Path(app_module.BASE_DIR) / "templates" / f"{name}.html").read_text(encoding="utf-8")
            block = tpl.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]
            self.assertIn('class="icon-btn icon-scan"', block, name)

    def test旋转动效样式与降级(self) -> None:
        self.assertIn(".icon-btn.icon-scan:hover svg { transform: rotate(180deg); }", self.style)
        self.assertIn(".icon-btn.icon-scan:focus svg { animation: icon-scan-spin", self.style)
        self.assertIn("@keyframes icon-scan-spin", self.style)
        # 减弱动态偏好下不转
        reduce = self.style.split("prefers-reduced-motion: reduce", 1)[1]
        self.assertIn("icon-scan", reduce)




class LlmSettingsEmbedTests(unittest.TestCase):
    """v1.20.1：工坊内嵌 LLM 设置与 /llm/settings 安全回跳。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "llm-settings-test.sqlite3"
        db.init_db(self.db_path)
        self.connect_patch = patch.object(app_module, "connect", lambda: db.connect(self.db_path))
        self.connect_patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self) -> None:
        self.connect_patch.stop()
        self.temp_dir.cleanup()

    def testOpenAPI版本与v1操作数(self) -> None:
        schema = app_module.app.openapi()
        self.assertEqual("1.24.1", app_module.app.version)
        self.assertEqual("1.24.1", schema["info"]["version"])
        count = sum(len(v) for key, v in schema["paths"].items() if key.startswith("/api/v1"))
        self.assertEqual(20, count)

    def test保存后可重定向回工坊(self) -> None:
        response = self.client.post(
            "/llm/settings",
            data={
                "base_url": "https://example.invalid/v1",
                "api_key": "test-key",
                "model": "test-model",
                "default_system_prompt": "测试 system prompt",
                "copilot_enabled": "1",
                "next": "/workshop",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual("/workshop", response.headers["location"])
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM llm_settings WHERE id=1").fetchone()
        self.assertEqual("https://example.invalid/v1", row["base_url"])
        self.assertEqual("test-key", row["api_key"])
        self.assertEqual("test-model", row["model"])
        self.assertEqual("测试 system prompt", row["default_system_prompt"])
        self.assertEqual(1, row["copilot_enabled"])

    def test缺省next仍回到兼容页(self) -> None:
        response = self.client.post(
            "/llm/settings",
            data={"base_url": "http://127.0.0.1:11434/v1", "model": "local"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual("/llm", response.headers["location"])

    def test拒绝开放跳转(self) -> None:
        for bad in ("https://evil.example/", "//evil.example", "workshop", "/\\evil", "/http://evil.example"):
            with self.subTest(next=bad):
                response = self.client.post(
                    "/llm/settings",
                    data={"base_url": "http://127.0.0.1:1", "next": bad},
                    follow_redirects=False,
                )
                self.assertEqual(303, response.status_code)
                self.assertEqual("/llm", response.headers["location"])

    def test工坊页渲染内嵌设置弹层(self) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE llm_settings SET base_url=?, api_key=?, model=? WHERE id=1",
                ("http://127.0.0.1:11434/v1", "workshop-secret", "workshop-model"),
            )
        response = self.client.get("/workshop")
        self.assertEqual(200, response.status_code)
        self.assertIn('id="wsLlmSettingsDialog"', response.text)
        self.assertIn("/api/copilot/settings", response.text)
        self.assertIn('id="wsLlmEnabledBtn"', response.text)
        self.assertNotIn("workshop-secret", response.text)
        self.assertNotIn('name="next" value="/workshop"', response.text)

    def test兼容页不再暴露对话表单(self) -> None:
        response = self.client.get("/llm")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("对话 / 辅助分类", response.text)
        self.assertNotIn('action="/llm/chat"', response.text)
        self.assertIn('action="/llm/settings"', response.text)

    def test页面渲染不再触发TemplateResponse弃用警告(self) -> None:
        pages = ("/", "/tags", "/gallery", "/workflows", "/llm", "/characters", "/recipes", "/workshop")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            for path in pages:
                response = self.client.get(path)
                self.assertEqual(200, response.status_code, path)
        texts = [str(item.message) for item in caught]
        self.assertFalse(any("TemplateResponse" in text for text in texts), texts)


class SafeLocalNextTests(unittest.TestCase):
    def test允许站内相对路径(self) -> None:
        self.assertEqual("/workshop", app_module._safe_local_next("/workshop"))
        self.assertEqual("/llm", app_module._safe_local_next("/llm"))
        self.assertEqual("/gacha", app_module._safe_local_next("/gacha"))
        self.assertEqual("/llm", app_module._safe_local_next(""))
        self.assertEqual("/llm", app_module._safe_local_next(None))
        self.assertEqual("/workshop", app_module._safe_local_next(" /workshop "))

    def test拒绝外链与协议相对路径(self) -> None:
        self.assertEqual("/llm", app_module._safe_local_next("https://evil.example/phish"))
        self.assertEqual("/llm", app_module._safe_local_next("//evil.example"))
        self.assertEqual("/llm", app_module._safe_local_next("workshop"))
        self.assertEqual("/llm", app_module._safe_local_next("/\\evil"))
        self.assertEqual("/llm", app_module._safe_local_next("/foo\n/bar"))


if __name__ == "__main__":
    unittest.main()

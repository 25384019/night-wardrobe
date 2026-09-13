"""v1.5.0 Tag 库页面布局契约测试：左抽屉工作台、密度切换、断点合并。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tag_manager import app as app_module


class TagDrawerTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base = Path(app_module.BASE_DIR)
        cls.template = (base / "templates" / "tags.html").read_text(encoding="utf-8")
        cls.style = (base / "static" / "style.css").read_text(encoding="utf-8")

    def test模板具备抽屉结构(self) -> None:
        self.assertIn('<aside class="tag-drawer" id="tagDrawer"', self.template)
        self.assertIn('class="tag-drawer-rail" id="drawerRailToggle"', self.template)
        self.assertIn('class="tag-drawer-fab" id="drawerFab"', self.template)
        self.assertIn('id="drawerFabCount"', self.template)
        # 已选工作台原有功能 id 全部保留
        for element_id in ["selectedTags", "selectedTagPreview", "selectedCountBadge",
                           "newKeyword", "previewModeToggle", "editModeToggle"]:
            self.assertIn(f'id="{element_id}"', self.template)

    def test模板具备密度切换与抽屉持久化(self) -> None:
        self.assertIn('id="tagPage"', self.template)
        self.assertIn("setTagDensity", self.template)
        self.assertIn("wardrobe_tag_density", self.template)
        self.assertIn("wardrobe_tag_drawer", self.template)
        self.assertIn("toggleTagDrawer", self.template)
        self.assertIn("toggleTagDrawerOpen", self.template)
        self.assertIn("tag-drawer-expanded", self.template)

    def test模板已移除流内控制台(self) -> None:
        self.assertNotIn("tagConsole", self.template)
        self.assertNotIn("toggleConsole", self.template)
        self.assertNotIn("wardrobe_console_collapsed", self.template)
        self.assertNotIn("tag-console-toggle", self.template)

    def test分类栏不再持久化手动拖高(self) -> None:
        self.assertNotIn("catBarHeight", self.template)
        self.assertNotIn("subBarHeight", self.template)
        # 已选文本框高度持久化保留
        self.assertIn("selectedTagsHeight", self.template)

    def test二级分类栏在吸顶区内(self) -> None:
        dock_start = self.template.index('<div class="filter-dock">')
        dock_end = self.template.index("</section>", dock_start)
        sub_nav = self.template.index('<nav class="subcategory-bar">')
        self.assertLess(dock_start, sub_nav)
        self.assertLess(sub_nav, dock_end)

    def test离开页面时清理抽屉让位类(self) -> None:
        cleanup = self.template.split("window.__wardrobePageCleanup = function()", 1)[1]
        self.assertIn("classList.remove('tag-drawer-expanded')", cleanup)

    def test样式具备抽屉与密度规则(self) -> None:
        self.assertIn(".tag-drawer {", self.style)
        self.assertIn(".tag-drawer.collapsed", self.style)
        self.assertIn(".tag-drawer-rail", self.style)
        self.assertIn(".tag-drawer-fab", self.style)
        self.assertIn("body.tag-drawer-expanded .app-main", self.style)
        self.assertIn(".tag-page.dense .tag-grid", self.style)
        self.assertIn("minmax(88px, 1fr)", self.style)

    def test分类栏不再写死高度手动拉伸(self) -> None:
        bar_rule = re.search(r"\.category-bar, \.subcategory-bar \{[^}]*\}", self.style)
        self.assertIsNotNone(bar_rule)
        self.assertNotIn("height: 48px", bar_rule.group(0))
        self.assertNotIn("resize: vertical", bar_rule.group(0))
        self.assertIn("max-height", bar_rule.group(0))

    def test窄屏断点合并(self) -> None:
        # 900px 一档：抽屉退化覆盖层 + 悬浮球
        narrow = re.search(r"@media \(max-width: 900px\) \{.*?^\}", self.style, re.S | re.M)
        self.assertIsNotNone(narrow)
        self.assertIn(".tag-drawer.open", narrow.group(0))
        self.assertIn(".tag-drawer-fab", narrow.group(0))
        # 600px 一档存在
        self.assertIn("@media (max-width: 600px)", self.style)
        # 720px 段不再重复 900px 段的 Tag 规则
        shell = re.search(r"@media \(max-width: 720px\) \{.*\}", self.style)
        self.assertIsNotNone(shell)
        self.assertNotIn(".tag-grid", shell.group(0))
        self.assertNotIn(".prompt-ui-actions", shell.group(0))

    def test抽屉宽度可拖拽调整(self) -> None:
        self.assertIn('class="tag-drawer-resizer" id="drawerResizer"', self.template)
        self.assertIn("setDrawerWidth", self.template)
        self.assertIn("wardrobe_tag_drawer_width", self.template)
        self.assertIn("--tag-drawer-width", self.template)
        # 上下限存在
        self.assertIn("DRAWER_MIN_WIDTH", self.template)
        self.assertIn("DRAWER_MAX_WIDTH", self.template)
        # 宽度经 CSS 变量同时驱动抽屉本体与主内容让位；收起态完全推出屏幕
        self.assertIn("width: var(--tag-drawer-width", self.style)
        self.assertIn("calc(-100% - 130px)", self.style)
        self.assertIn("calc(var(--tag-drawer-width", self.style)
        self.assertIn(".tag-drawer-resizer", self.style)

    def test版本面(self) -> None:
        base_html = (Path(app_module.BASE_DIR) / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("v1.24.1", base_html)
        self.assertIn("style.css?v=88", base_html)
        self.assertEqual(app_module.app.version, "1.24.1")


if __name__ == "__main__":
    unittest.main()

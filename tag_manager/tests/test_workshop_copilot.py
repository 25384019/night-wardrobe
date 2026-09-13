from __future__ import annotations

import unittest
from pathlib import Path

from tag_manager import app as app_module


class WorkshopCopilotContractTests(unittest.TestCase):
    """Copilot 壳 + Island 契约（v1.20.0：React Island + 真实后端 + 工具循环；产物缓存 v80）。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.js = (base_dir / "static" / "workshop-copilot.js").read_text(encoding="utf-8")
        cls.tpl = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")
        cls.island_js = (base_dir / "static" / "copilot" / "copilot.js").read_text(encoding="utf-8")
        cls.island_css = (base_dir / "static" / "copilot" / "copilot.css").read_text(encoding="utf-8")
        src = base_dir / "frontend" / "copilot" / "src"
        cls.app_tsx = (src / "App.tsx").read_text(encoding="utf-8")
        cls.mock_ts = (src / "mock.ts").read_text(encoding="utf-8")
        cls.prompt_ts = (src / "components" / "ai-elements" / "prompt-input.tsx").read_text(encoding="utf-8")
        cls.styles_css = (src / "styles.css").read_text(encoding="utf-8")

    def test停靠面板与漫画抽屉解耦(self) -> None:
        tpl = self.tpl
        self.assertIn('class="ws-copilot-pane" id="copilotDrawer"', tpl)
        self.assertIn('class="ws-copilot-mask" id="copilotMask"', tpl)
        self.assertIn('id="copilotClose"', tpl)
        self.assertIn('aria-label="收起助手面板"', tpl)
        self.assertNotIn('class="manga-drawer-mask" id="copilotMask"', tpl)
        pane = self.style.split(".ws-copilot-pane {", 1)[1][:620]
        self.assertIn("color-mix(in srgb, var(--bg-midnight)", pane)
        self.assertIn("border-left: 1px solid rgba(148, 163, 184, 0.10);", pane)
        self.assertIn("box-shadow: none;", pane)
        self.assertIn("padding: 0 18px 14px;", pane)

    def testIsland挂载点与产物(self) -> None:
        self.assertIn('id="workshop-copilot-root"', self.tpl)
        self.assertIn("/static/copilot/copilot.js?v=84", self.tpl)
        self.assertIn("/static/copilot/copilot.css?v=84", self.tpl)
        self.assertIn("e.mount=", self.island_js)
        self.assertIn("e.unmount=", self.island_js)
        self.assertIn("var WorkshopCopilotIsland=", self.island_js)
        self.assertNotIn("preflight.css", self.island_css)
        self.assertIn("tailwindcss/theme.css", self.styles_css)
        self.assertIn("tailwindcss/utilities.css", self.styles_css)
        self.assertNotIn("preflight.css", self.styles_css)

    def test七个快捷操作齐全(self) -> None:
        actions = ("diagnose", "reduce_conflicts", "dedupe", "improve_pose",
                   "improve_composition", "enrich_environment", "optimize_negative")
        for action in actions:
            self.assertIn(f'"{action}"', self.app_tsx)
        self.assertIn("data-copilot-action={action}", self.app_tsx)
        self.assertIn("freeform", self.app_tsx)
        self.assertIn("data-copilot-input", self.app_tsx)
        self.assertIn("data-copilot-send", self.app_tsx)

    def test数据契约注释完整(self) -> None:
        js = self.js
        for snippet in ("PromptRequest", "PromptSuggestion", "PromptOperation", "Diagnostic",
                        "ExecutionStage", "customInstruction", "operations", "diagnostics",
                        "contexts", "history"):
            self.assertIn(snippet, js)
        self.assertIn("'add'", self.js)
        self.assertIn("'remove'", self.js)
        self.assertIn("'replace'", self.js)
        for level in ("success", "info", "warning", "error"):
            self.assertIn(level, self.mock_ts)

    def test服务层与mock后端(self) -> None:
        self.assertIn("MockCopilotBackend", self.mock_ts)
        self.assertIn("function buildMockSuggestion", self.mock_ts)
        self.assertIn("function buildMockStages", self.mock_ts)
        self.assertNotIn("fetch(", self.mock_ts)
        self.assertNotIn("XMLHttpRequest", self.mock_ts)
        self.assertNotIn("fetch(", self.js)
        self.assertNotIn("XMLHttpRequest", self.js)
        self.assertNotIn("streamdown", self.app_tsx.lower())
        self.assertNotIn("from \"next", self.app_tsx)
        self.assertNotIn("@xyflow", self.app_tsx)

    def testmock数据只在后端实现内不散落模板(self) -> None:
        self.assertNotIn("MockCopilotBackend", self.tpl)
        self.assertNotIn("MockCopilotBackend", self.style)
        self.assertEqual(1, self.mock_ts.count("export function buildMockSuggestion"))
        self.assertEqual(1, self.mock_ts.count("export function buildMockStages"))

    def testdiff闭环与应用确认(self) -> None:
        self.assertIn("function applyOperations(text, operations)", self.js)
        self.assertIn("workshop:apply-prompt-operations", self.js)
        diff = (Path(app_module.BASE_DIR) / "frontend" / "copilot" / "src" / "components" / "workshop" / "DiffCard.tsx").read_text(encoding="utf-8")
        for label in ("应用选中项", "应用全部", "放弃"):
            self.assertIn(label, diff)
        self.assertIn("window.WorkshopAPI.applyEdited(target, result.text)", self.js)
        self.assertNotIn("currentSuggestion", self.js)
        self.assertIn("applied: true", self.app_tsx)

    def testdiff与诊断样式(self) -> None:
        # 业务卡改在 Island 内用 Tailwind；衣柜主样式仍保留诊断/diff 色值以免其它页回归
        self.assertIn("rgba(134,239,172,.1)", self.style)
        diff = (Path(app_module.BASE_DIR) / "frontend" / "copilot" / "src" / "components" / "workshop" / "DiffCard.tsx").read_text(encoding="utf-8")
        island_css = (Path(app_module.BASE_DIR) / "frontend" / "copilot" / "src" / "styles.css").read_text(encoding="utf-8")
        # v1.20.0 紧凑化：不再有整行色块背景，类型色收敛到符号与左侧细条；checkbox 显式压过全局 input 样式
        self.assertNotIn("bg-emerald-400/10", diff)
        self.assertIn("text-emerald-400", diff)
        self.assertIn('data-kind={op.kind}', diff)
        self.assertIn('#workshop-copilot-root input[type="checkbox"]', island_css)
        self.assertIn("accent-color: #a855f7", island_css)

    def test诊断点击定位高亮(self) -> None:
        self.assertIn("workshop:highlight-tag", self.js)
        self.assertIn("window.WorkshopAPI.highlightTag", self.js)
        self.assertIn("ws-flash", self.tpl)
        self.assertIn(".ws-flash", self.style)

    def test清理钩子暴露(self) -> None:
        self.assertIn("window.WorkshopCopilot = {", self.js)
        self.assertIn("destroy: destroy", self.js)
        self.assertIn("document.removeEventListener('keydown', onEsc)", self.js)
        self.assertIn("unmountIsland()", self.js)


class WorkshopCopilotConversationTests(unittest.TestCase):
    """对话式契约改由 Island 源码钉住；壳只保留 composer 无 id 与 IME 守卫。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.js = (base_dir / "static" / "workshop-copilot.js").read_text(encoding="utf-8")
        cls.tpl = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.base = (base_dir / "templates" / "base.html").read_text(encoding="utf-8")
        src = base_dir / "frontend" / "copilot" / "src"
        cls.app = (src / "App.tsx").read_text(encoding="utf-8")
        cls.prompt = (src / "components" / "ai-elements" / "prompt-input.tsx").read_text(encoding="utf-8")
        cls.mock = (src / "mock.ts").read_text(encoding="utf-8")
        cls.conv = (src / "components" / "ai-elements" / "conversation.tsx").read_text(encoding="utf-8")

    def test会话容器与空态结构(self) -> None:
        self.assertIn("Conversation", self.app)
        self.assertIn("ConversationEmptyState", self.app)
        self.assertIn("role=\"log\"", self.conv)
        self.assertIn("要我怎么改这段 Prompt？", self.app)
        self.assertIn("描述修改目标，我会先给建议，确认后再写回工作区。", self.app)
        for removed in ("copilot-body", "copilot-action-grid", "copilot-result", "copilot-input-row"):
            self.assertNotIn(removed, self.tpl)
            self.assertNotIn(removed, self.js)

    def test空态四枚chips与更多展开其余三个(self) -> None:
        for action in ("diagnose", "improve_pose", "reduce_conflicts", "improve_composition"):
            self.assertIn(action, self.app.split("EXTRA", 1)[0])
        for action in ("dedupe", "enrich_environment", "optimize_negative"):
            self.assertIn(action, self.app.split("EXTRA", 1)[1][:800])
        self.assertIn("data-copilot-more", self.app)
        self.assertIn("data-copilot-extra", self.app)
        self.assertIn("empty &&", self.app)

    def testcomposer输入框不带id避免跨页复活(self) -> None:
        self.assertIn("textarea[id], input[id], select[id]", self.base)
        self.assertNotIn('id="copilotInput"', self.tpl)
        self.assertNotIn('id="copilotSend"', self.tpl)
        self.assertIn("data-copilot-input", self.app)

    def testcomposer自增长与内嵌发送(self) -> None:
        self.assertIn("COMPOSER_MAX_PX = 220", self.prompt)
        self.assertIn("min-h-[108px] max-h-[220px]", self.prompt)
        self.assertIn("PromptInputSubmit", self.app)
        self.assertIn("PromptInputBody", self.app)
        self.assertIn("PromptInputFooter", self.app)
        self.assertIn("data-copilot-composer", self.prompt)
        composer = self.app.split("<PromptInput", 1)[1].split("</PromptInput>", 1)[0]
        self.assertIn("PromptContext", composer)
        self.assertNotIn("border-t border-border", self.app)

    def testEnter发送与输入法守卫(self) -> None:
        self.assertIn('e.key !== "Enter" || e.shiftKey', self.prompt)
        self.assertIn("e.nativeEvent.isComposing || e.keyCode === 229", self.prompt)
        self.assertIn("Enter 发送 · Shift+Enter 换行", self.app)

    def test上下文开关真实参与载荷(self) -> None:
        ctx = (Path(app_module.BASE_DIR) / "frontend" / "copilot" / "src" / "components" / "workshop" / "PromptContext.tsx").read_text(encoding="utf-8")
        self.assertIn("data-copilot-context={key}", ctx)
        self.assertIn('positive: ctxOn.positive ? prompt.positive : ""', self.app)
        self.assertIn('negative: ctxOn.negative ? prompt.negative : ""', self.app)
        self.assertIn("recipe: ctxOn.recipe ? prompt.recipe : {}", self.app)
        self.assertIn("未选择上下文", self.app)

    def test单一渲染入口覆盖六种消息类型(self) -> None:
        self.assertIn("from={turn.role}", self.app)
        self.assertIn("DiagnosisCard", self.app)
        self.assertIn("DiffCard", self.app)
        self.assertIn("Loader", self.app)
        self.assertIn("TaskTrigger", self.app)

    def test执行阶段不承载模型思维过程(self) -> None:
        self.assertIn("chain-of-thought", self.mock)
        self.assertIn("Positive \" + pos.length + \" 段 · Negative \" + neg.length + \" 段", self.mock)

    def test建议卡应用后转只读(self) -> None:
        self.assertIn("turn.applied || turn.discarded", self.app)
        self.assertIn("已应用的修改", Path(app_module.BASE_DIR, "frontend", "copilot", "src", "components", "workshop", "DiffCard.tsx").read_text(encoding="utf-8"))

    def test异步守卫与会话持久化到服务端(self) -> None:
        self.assertIn("activeSessionIdRef.current !== requestSessionId", self.app)
        self.assertIn("session_id: requestSessionId", self.app)
        self.assertIn("unmountIsland()", self.js)
        self.assertNotIn("sessionStorage", self.js)
        self.assertNotIn("localStorage", self.app)
        self.assertNotIn("indexedDB", self.app)
        close_section = self.js.split("function closeDrawer()", 1)[1].split("function onResizePointerDown", 1)[0]
        self.assertNotIn("unmountIsland();", close_section)

    def test收起不销毁会话(self) -> None:
        close_section = self.js.split("function closeDrawer()", 1)[1].split("function onResizePointerDown", 1)[0]
        self.assertNotIn("unmountIsland", close_section)

    def test离开页面才unmount且打开即同步上下文(self) -> None:
        destroy_section = self.js.split("function destroy()", 1)[1].split("function toggleDrawer()", 1)[0]
        self.assertIn("unmountIsland();", destroy_section)
        open_section = self.js.split("function openDrawer()", 1)[1].split("function closeDrawer()", 1)[0]
        self.assertIn("mountIsland();", open_section)
        self.assertIn("emitContext();", open_section)

    def test派生状态不落库(self) -> None:
        self.assertIn("t.role === \"assistant\" && t.status === \"pending\"", self.app)

    def test贴底跟随滚动(self) -> None:
        self.assertIn("use-stick-to-bottom", self.conv)
        self.assertIn("ConversationScrollButton", self.app)

    def test参考来源与许可写入源码(self) -> None:
        self.assertIn("vercel/ai-elements", self.conv)
        self.assertIn("Apache-2.0", self.conv)
        self.assertIn("vercel/ai-elements", self.styles_css if False else Path(app_module.BASE_DIR, "frontend", "copilot", "src", "styles.css").read_text(encoding="utf-8"))


class WorkshopCopilotDockLayoutTests(unittest.TestCase):
    """v1.17.0 起：Copilot 是 docked tool panel，不是 modal overlay。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.js = (base_dir / "static" / "workshop-copilot.js").read_text(encoding="utf-8")
        cls.tpl = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

    def test工作台三栏同一层级(self) -> None:
        tpl = self.tpl
        workbench = tpl.split('id="wsWorkbench"', 1)[1].split('id="copilotMask"', 1)[0]
        self.assertIn('class="ws-layout"', workbench)
        self.assertIn('class="ws-recipe-panel"', workbench)
        self.assertIn('class="ws-workspace"', workbench)
        self.assertIn('id="copilotDrawer"', workbench)
        self.assertIn('id="workshop-copilot-root"', workbench)

    def test宽屏停靠窄屏才overlay(self) -> None:
        self.assertIn(".ws-workbench.is-copilot-open .ws-copilot-pane { display: flex; }", self.style)
        self.assertIn("@media (max-width: 1199px)", self.style)
        self.assertIn("var OVERLAY_MQ = '(max-width: 1199px)';", self.js)
        self.assertIn(".ws-copilot-mask { display: none; }", self.style)
        overlay = self.style.split("@media (max-width: 1199px)", 1)[1][:900]
        self.assertIn("position: fixed;", overlay)
        self.assertIn(".ws-workbench { display: flex;", self.style)

    def test拖拽分割线与宽度约束(self) -> None:
        self.assertIn("var COPILOT_MIN_PX = 340;", self.js)
        self.assertIn("var COPILOT_MAX_PX = 520;", self.js)
        self.assertIn("wardrobe_ws_copilot_width", self.js)
        self.assertIn('id="copilotResizer"', self.tpl)
        self.assertIn(".ws-workbench.is-copilot-open .ws-workspace { min-width: 480px; }", self.style)
        self.assertIn("max-height: calc(100vh - 108px);", self.style)
        self.assertIn("height: 44px;", self.style.split(".ws-copilot-head {", 1)[1][:220])
        self.assertIn("font-size: 14px;", self.style.split(".ws-copilot-head h2 {", 1)[1][:180])

    def test打开后主区仍可交互且Esc不抢编辑(self) -> None:
        self.assertIn("if (e.key === 'Escape' && isOverlayMode() && isOpen()) closeDrawer();", self.js)
        docked = self.style.split(".ws-copilot-pane {", 1)[1].split("@media", 1)[0]
        self.assertIn("box-shadow: none;", docked)

    def test工坊页解除主栏最大宽度以便宽屏分栏(self) -> None:
        self.assertIn(".app-main:has(.ws-workbench) { max-width: none; }", self.style)
        self.assertIn("minmax(300px, 330px)", self.style)


class ApplyOperationsSemanticsTests(unittest.TestCase):
    """applyOperations 的整段匹配语义仍由原生壳负责。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (Path(app_module.BASE_DIR) / "static" / "workshop-copilot.js").read_text(encoding="utf-8")

    @staticmethod
    def apply(text: str, operations: list[dict]) -> tuple[str, list[dict]]:
        segments = [s.strip() for s in text.split(",") if s.strip()]
        skipped: list[dict] = []
        for op in operations:
            if op["kind"] == "add":
                if op["tag"] not in segments:
                    segments.append(op["tag"])
                else:
                    skipped.append(op)
            elif op["kind"] == "remove":
                if op["tag"] in segments:
                    segments.remove(op["tag"])
                else:
                    skipped.append(op)
            elif op["kind"] == "replace":
                if op["from"] in segments:
                    segments[segments.index(op["from"])] = op["to"]
                else:
                    skipped.append(op)
        return ", ".join(segments), skipped

    def test权重段不被字符串裁剪(self) -> None:
        text = "(artist:0.8), 1girl, masterpiece"
        result, skipped = self.apply(text, [{"kind": "remove", "tag": "artist"}])
        self.assertIn("(artist:0.8)", result)
        self.assertEqual(1, len(skipped))
        result, _ = self.apply(text, [{"kind": "remove", "tag": "(artist:0.8)"}])
        self.assertEqual("1girl, masterpiece", result)

    def test新增去重且应用幂等(self) -> None:
        ops = [{"kind": "add", "tag": "masterpiece"}]
        first, _ = self.apply("1girl", ops)
        self.assertEqual("1girl, masterpiece", first)
        second, skipped = self.apply(first, ops)
        self.assertEqual(first, second)
        self.assertEqual(1, len(skipped))

    def test替换未命中整体跳过(self) -> None:
        result, skipped = self.apply("1girl, smile", [{"kind": "replace", "from": "smil", "to": "grin"}])
        self.assertEqual("1girl, smile", result)
        self.assertEqual(1, len(skipped))

    def testJS实现关键点与Python复刻一致(self) -> None:
        js = self.js
        self.assertIn("text.split(',').map(function (s) { return s.trim(); }).filter(Boolean)", js)
        self.assertIn("segments.indexOf(op.tag) === -1", js)
        self.assertIn("segments.join(', ')", js)
        self.assertIn("skipped.push(op)", js)


class RealBackendWiringTests(unittest.TestCase):
    """工坊 Copilot Island 切到真实 POST /api/workshop/copilot（不改契约形状）。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        src = base_dir / "frontend" / "copilot" / "src"
        cls.real_path = src / "realBackend.ts"
        cls.real = cls.real_path.read_text(encoding="utf-8")
        cls.mock = (src / "mock.ts").read_text(encoding="utf-8")
        cls.app = (src / "App.tsx").read_text(encoding="utf-8")
        cls.island_js = (base_dir / "static" / "copilot" / "copilot.js").read_text(encoding="utf-8")

    def test真实后端源码存在且无密钥(self) -> None:
        self.assertTrue(self.real_path.is_file())
        self.assertIn("/api/workshop/copilot", self.real)
        self.assertIn("AbortController", self.real)
        for needle in ("api_key", "base_url", "llm_settings", "sk-"):
            self.assertNotIn(needle, self.real)

    def test服务层切到真实后端并保留mock(self) -> None:
        self.assertIn("backend: RealCopilotBackend", self.mock)
        self.assertIn("MockCopilotBackend", self.mock)

    def testmock不含fetch(self) -> None:
        self.assertNotIn("fetch(", self.mock)

    def test构建产物不含密钥(self) -> None:
        js = self.island_js
        for needle in ("api_key", "llm_settings", "base_url"):
            self.assertNotIn(needle, js)
        # Tailwind 工具类 mask-* 会偶然包含子串 sk-；去掉后产物不得再出现密钥前缀
        self.assertNotIn("sk-", js.replace("mask-", ""))

    def testApp错误路径改写pending为error(self) -> None:
        self.assertIn("requestSuggestion(request)", self.app)
        self.assertIn('role: "error"', self.app)
        self.assertIn("try {", self.app)
        self.assertIn("catch (err)", self.app)
        self.assertIn('{ id: pending.id, role: "error", text: message }', self.app)


class WorkshopCopilotSessionUiTests(unittest.TestCase):
    """v1.21.0：Session 控件、恢复契约与 pending 隔离。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        src = base_dir / "frontend" / "copilot" / "src"
        cls.app = (src / "App.tsx").read_text(encoding="utf-8")
        cls.real = (src / "realBackend.ts").read_text(encoding="utf-8")
        cls.types = (src / "types.ts").read_text(encoding="utf-8")
        cls.bridge = (src / "bridge.ts").read_text(encoding="utf-8")
        cls.bar = (src / "components" / "workshop" / "SessionBar.tsx").read_text(encoding="utf-8")
        cls.switcher = (src / "components" / "workshop" / "SessionSwitcher.tsx").read_text(encoding="utf-8")
        cls.notice = (src / "components" / "workshop" / "OldContextNotice.tsx").read_text(encoding="utf-8")
        cls.tpl = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.js = (base_dir / "static" / "workshop-copilot.js").read_text(encoding="utf-8")
        cls.api = (src / "sessionApi.ts").read_text(encoding="utf-8")

    def test会话控件与历史视图(self) -> None:
        self.assertIn("data-copilot-session-current", self.bar)
        self.assertIn("data-copilot-session-new", self.bar)
        self.assertIn("查看全部历史", self.switcher)
        self.assertIn("重命名", self.switcher)
        self.assertIn("确认删除？", self.switcher)
        self.assertIn("historyMode", self.app)
        self.assertIn("sessionSearch", self.app)
        self.assertIn("activeSessionId", self.app)

    def test会话管理入口收敛到历史视图(self) -> None:
        # 搜索框只存在于 Popover 与 History View，不常驻 Conversation 顶部
        self.assertIn('data-copilot-session-search=""', self.switcher)
        self.assertNotIn("data-copilot-session-search", self.app.split("return (", 1)[1].split("{historyMode ? (", 1)[0])
        # Popover 只做导航与切换，管理操作（重命名/删除/⋯）只在 History View
        popover = self.switcher.split("export function SessionHistoryView", 1)[0]
        history = self.switcher.split("export function SessionHistoryView", 1)[1]
        self.assertNotIn("data-copilot-session-rename-start", popover)
        self.assertNotIn("data-copilot-session-delete", popover.replace("data-copilot-session-delete-confirm", ""))
        self.assertIn("data-copilot-session-menu", history)
        self.assertIn("data-copilot-session-rename-start", history)
        self.assertIn("data-copilot-session-delete", history)
        # Popover 页脚“查看全部历史”是导航动作
        self.assertIn("data-copilot-history-all", popover)

    def test历史Diff过期禁止直接Apply(self) -> None:
        diff = (Path(app_module.BASE_DIR) / "frontend" / "copilot" / "src" / "components" / "workshop" / "DiffCard.tsx").read_text(encoding="utf-8")
        self.assertIn('"stale"', diff)
        self.assertIn("此建议基于较早的 Prompt", diff)
        self.assertIn("使用当前 Prompt 重新检查", diff)
        self.assertIn("data-copilot-recheck", diff)
        self.assertIn("data-copilot-diff-stale", diff)
        # 已应用/已放弃有明确 readonly 标识
        self.assertIn("已应用", diff)
        self.assertIn("已放弃", diff)
        self.assertIn("copilot-diff-badge", diff)
        # App 层兜底：快照偏离时 applyTurn 直接拒绝写回
        self.assertIn("此建议基于较早的 Prompt，请先使用当前 Prompt 重新检查", self.app)
        self.assertIn("stale={showOldContext}", self.app)
        self.assertIn("onRecheck", self.app)

    def test旧上下文提示不自动回滚(self) -> None:
        self.assertIn("此会话基于较早的 Prompt 上下文", self.notice)
        self.assertIn("查看旧上下文", self.notice)
        self.assertNotIn("恢复会话时的 Prompt", self.app)
        self.assertNotIn("恢复会话时的 Prompt", self.notice)
        self.assertIn("getContextSnapshot", self.tpl)
        self.assertIn("getContextSnapshot", self.bridge)
        self.assertIn("data-copilot-diff-state", (Path(app_module.BASE_DIR) / "frontend" / "copilot" / "src" / "components" / "workshop" / "DiffCard.tsx").read_text(encoding="utf-8"))

    def test请求携带session_id且不回传UI历史(self) -> None:
        self.assertIn("session_id", self.real)
        self.assertIn("if (request.session_id)", self.real)
        self.assertIn("body.session_id = request.session_id", self.real)

    def test收起不卸载与离开才恢复(self) -> None:
        close_section = self.js.split("function closeDrawer()", 1)[1].split("function onResizePointerDown", 1)[0]
        self.assertNotIn("unmountIsland", close_section)
        destroy_section = self.js.split("function destroy()", 1)[1].split("function toggleDrawer()", 1)[0]
        self.assertIn("unmountIsland();", destroy_section)

    def testpending跨会话隔离(self) -> None:
        self.assertIn("pendingSessionsRef", self.app)
        self.assertIn("requestSessionId", self.app)
        self.assertIn("owned !== requestSessionId", self.app)
        self.assertIn("hydrateRequestSession", self.app)
        self.assertIn("loadSession(requestSessionId)", self.app)
        self.assertIn('"pending-" + requestSessionId', self.app)
        self.assertIn("patchMessage", self.app)
        self.assertIn("data-session-id", self.app)

    def test搜索请求携带q参数(self) -> None:
        self.assertIn("/api/workshop/copilot/sessions?q=", self.api)
        self.assertIn("encodeURIComponent(q)", self.api)
        self.assertIn("listSessions(q)", self.app)
        self.assertIn("sessionSearch.trim()", self.app)
        self.assertIn("window.setTimeout", self.app)
        island = (Path(app_module.BASE_DIR) / "static" / "copilot" / "copilot.js").read_text(encoding="utf-8")
        self.assertIn("sessions?q=", island)

    def test产物与源码不以浏览器存储作为会话源(self) -> None:
        island = (Path(app_module.BASE_DIR) / "static" / "copilot" / "copilot.js").read_text(encoding="utf-8")
        src_dir = Path(app_module.BASE_DIR) / "frontend" / "copilot" / "src"
        sources = "\n".join(path.read_text(encoding="utf-8") for path in src_dir.rglob("*.ts*"))
        for blob in (island, sources, self.app):
            self.assertNotIn("localStorage", blob)
            self.assertNotIn("sessionStorage", blob)
            self.assertNotIn("indexedDB", blob)
            self.assertNotIn("IndexedDB", blob)


class WorkshopLlmSettingsContractTests(unittest.TestCase):
    """v1.20.1：LLM 设置内嵌工坊，侧栏不再暴露独立 AI 设置页。"""

    @classmethod
    def setUpClass(cls) -> None:
        base_dir = Path(app_module.BASE_DIR)
        cls.base = (base_dir / "templates" / "base.html").read_text(encoding="utf-8")
        cls.tpl = (base_dir / "templates" / "workshop.html").read_text(encoding="utf-8")
        cls.llm = (base_dir / "templates" / "llm.html").read_text(encoding="utf-8")
        cls.style = (base_dir / "static" / "style.css").read_text(encoding="utf-8")

    def test侧栏不再包含AI设置入口(self) -> None:
        self.assertNotIn('href="/llm"', self.base)
        self.assertNotIn("AI 设置", self.base)
        self.assertIn("v1.24.1", self.base)
        self.assertIn("style.css?v=88", self.base)

    def test工坊header有设置按钮与dialog(self) -> None:
        self.assertIn('id="wsLlmSettingsBtn"', self.tpl)
        self.assertIn('title="AI 提示词助手设置"', self.tpl)
        self.assertIn('aria-label="AI 提示词助手设置"', self.tpl)
        self.assertIn('class="folder-dialog" id="wsLlmSettingsDialog"', self.tpl)
        self.assertIn('id="wsCopilotSettingsBtn"', self.tpl)

    def test工坊设置走JSON接口(self) -> None:
        self.assertIn("/api/copilot/settings", self.tpl)
        self.assertIn("/static/copilot-settings.js?v=4", self.tpl)
        for field in ("base_url", "api_key", "model", "default_system_prompt"):
            self.assertIn(f'name="{field}"', self.tpl)
        self.assertNotIn('action="/llm/settings"', self.tpl)
        self.assertNotIn('action="/llm/chat"', self.tpl)

    def test兼容页去掉对话辅助分类主入口(self) -> None:
        self.assertIn('action="/llm/settings"', self.llm)
        self.assertIn('name="next" value="/llm"', self.llm)
        self.assertNotIn("对话 / 辅助分类", self.llm)
        self.assertNotIn('action="/llm/chat"', self.llm)
        self.assertIn("工坊 Copilot", self.llm)

    def test弹层复用folder_dialog样式(self) -> None:
        self.assertIn("#wsLlmSettingsDialog textarea", self.style)
        self.assertIn(".folder-dialog textarea", self.style)
        self.assertIn("/static/copilot/copilot.js?v=84", self.tpl)
        self.assertIn("/static/workshop-copilot.js?v=77", self.tpl)
        self.assertIn("/static/copilot-settings.js?v=4", self.tpl)


if __name__ == "__main__":
    unittest.main()

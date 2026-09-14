from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LoraPreviewUiTests(unittest.TestCase):
    def test_preview_dialog_stays_inside_viewport(self):
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("max-width: min(90vw, 1100px)", css)
        self.assertIn("inset: 0", css)
        self.assertIn("place-items: center", css)
        self.assertIn("max-height: calc(100vh - 48px)", css)
        template = (ROOT / "templates" / "loras.html").read_text(encoding="utf-8")
        self.assertIn("closeLoraPreview", template)
        self.assertIn("aria-label=\"关闭预览\"", template)


if __name__ == "__main__":
    unittest.main()

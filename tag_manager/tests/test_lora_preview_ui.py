from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LoraPreviewUiTests(unittest.TestCase):
    def test_preview_dialog_stays_inside_viewport(self):
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("width: min(92vw, 1100px)", css)
        self.assertIn("max-height: calc(100vh - 32px)", css)
        self.assertIn("max-height: calc(100vh - 96px)", css)
        self.assertIn(".lora-image-dialog-toolbar", css)
        template = (ROOT / "templates" / "loras.html").read_text(encoding="utf-8")
        self.assertIn("关闭预览 ×", template)


if __name__ == "__main__":
    unittest.main()

"""Regression coverage for viewport-sized, touch-scrollable product navigation."""
import unittest
from pathlib import Path


CSS = (Path(__file__).resolve().parent.parent / "webui/src/index.css").read_text(
    encoding="utf-8")


class SidebarLayoutTests(unittest.TestCase):
    def test_shell_tracks_safaris_dynamic_viewport(self):
        self.assertRegex(CSS, r"\.u-shell\s*\{[^}]*height:\s*100dvh")

    def test_sidebar_is_touch_scrollable_and_safe_area_aware(self):
        start = CSS.index(".u-sidebar {")
        sidebar = CSS[start:CSS.index("}\n", start)]
        self.assertIn("min-height:0", sidebar)
        self.assertIn("overflow-y:auto", sidebar)
        self.assertIn("touch-action:pan-y", sidebar)
        self.assertIn("-webkit-overflow-scrolling:touch", sidebar)
        self.assertIn("env(safe-area-inset-bottom)", sidebar)

    def test_overlay_sidebar_uses_dynamic_viewport_height(self):
        media = CSS.index("@media(max-width:900px)")
        sidebar = CSS.index(".u-sidebar {", media)
        rule = CSS[sidebar:CSS.index("}", sidebar)]
        self.assertIn("height:100dvh", rule)


if __name__ == "__main__":
    unittest.main()

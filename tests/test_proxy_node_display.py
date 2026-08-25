"""A node name must not pick up emoji metrics for its digits.

A subscription node like "WG-<flag> 英国 01" appeared to contain stray spaces around the
number. The name is plain ASCII — the cause is the font stack. Option lists cannot wrap their
flag in a child span, so they take an emoji stack for their whole text, and the platform emoji
fonts carry the ASCII digits that keycap sequences are built from. The digits were therefore
drawn with emoji metrics (~50% too wide) while the CJK text beside them fell through to the UI
font and looked normal — which is exactly what made it read as a spacing bug rather than a
font one.
"""
import re
import unittest
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "webui" / "src"
       / "index.css").read_text(encoding="utf-8")

FLAGS = "U+1F1E6-1F1FF"          # regional indicators; a flag is a pair of them


def _font_faces() -> list[str]:
    return re.findall(r"@font-face\s*\{[^}]*\}", CSS)


class ProxyNodeFontTests(unittest.TestCase):
    def test_every_emoji_face_is_limited_to_flags(self):
        # A face without unicode-range can serve any codepoint it happens to contain, which is
        # how the digits were captured in the first place.
        faces = [f for f in _font_faces() if "emoji" in f.lower() or "twemoji" in f.lower()]
        self.assertTrue(faces, "expected the emoji faces to still be declared")
        for face in faces:
            self.assertIn(FLAGS, face, face)

    def test_no_unrestricted_platform_emoji_font_in_a_text_stack(self):
        # Naming a platform emoji font directly re-introduces the defect: those faces are
        # outside our control and are not range-limited.
        for line in CSS.splitlines():
            if "font-family" not in line or line.lstrip().startswith("src:"):
                continue
            for raw in ("Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji"):
                self.assertNotIn(raw, line, line.strip())

    def test_font_variant_emoji_is_confined_to_flag_only_elements(self):
        """Measured in the browser: the same "01" renders 28px with the property and 15px
        without it, against 15px for the plain UI font. It forces emoji presentation onto the
        ASCII digits a keycap sequence is built from, and it does so past the unicode-range
        above — so range-limiting the fonts is not enough on its own. Only an element whose
        entire content is a flag may carry it."""
        for line in CSS.splitlines():
            # A rule carries a selector and a brace; prose about the property does not.
            if "font-variant-emoji" not in line or "{" not in line:
                continue
            selector = line.split("{")[0]
            self.assertIn("u-proxy-node-flag", selector,
                          "font-variant-emoji on an element that also carries text: " + selector)

    def test_the_ui_font_still_backs_the_option_lists(self):
        # With the emoji faces range-limited, everything that is not a flag has to land
        # somewhere: the stack must still end in the ordinary UI font.
        stack = next(l for l in CSS.splitlines() if ".u-proxy-node-select," in l and "font-family" in l)
        self.assertIn("ui-sans-serif", stack)
        self.assertLess(stack.index("flag emoji"), stack.index("ui-sans-serif"))


if __name__ == "__main__":
    unittest.main()

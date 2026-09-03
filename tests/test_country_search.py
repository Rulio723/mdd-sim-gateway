"""Regression coverage for the searchable country-exit picker."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
CSS = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")


class CountrySearchTests(unittest.TestCase):
    def test_search_indexes_both_languages_and_iso_code(self):
        start = SOURCE.index("function countryMatchesSearch")
        block = SOURCE[start:SOURCE.index("function SearchableCountrySelect", start)]
        self.assertIn("code,", block)
        self.assertIn("countryName(code, 'zh')", block)
        self.assertIn("countryName(code, 'en')", block)
        self.assertIn("tokens.every(token => haystack.includes(token))", block)

    def test_picker_supports_combobox_keyboard_interaction(self):
        start = SOURCE.index("function SearchableCountrySelect")
        block = SOURCE[start:SOURCE.index("function formatBytes", start)]
        self.assertIn('role="combobox"', block)
        self.assertIn('role="listbox"', block)
        self.assertIn('role="option"', block)
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(f"event.key === '{key}'", block)

    def test_add_exit_uses_searchable_picker(self):
        self.assertIn("<SearchableCountrySelect countries={available}", SOURCE)
        self.assertNotIn('<div className="u-inline u-add-exit"><select', SOURCE)

    def test_results_are_scrollable_and_mobile_picker_can_shrink(self):
        self.assertIn(".u-country-picker-list", CSS)
        self.assertIn("max-height:280px", CSS)
        self.assertIn("overflow-y:auto", CSS)
        self.assertIn(".u-country-picker { min-width:0; }", CSS)


if __name__ == "__main__":
    unittest.main()

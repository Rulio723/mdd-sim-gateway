"""Regression coverage for the three notification-channel template editors."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")


class NotificationTemplateUiTests(unittest.TestCase):
    def test_every_outbound_channel_has_an_event_template_editor(self):
        for channel in ("Webhook", "Telegram", "PushPlus"):
            self.assertIn(f'<MessageTemplateEditor channel="{channel}"', SOURCE)

    def test_editor_exposes_only_the_backend_template_fields(self):
        expected = "{{title}} {{content}} {{event}} {{sim_name}} {{msisdn}} {{from}} {{text}} {{instance}} {{iccid}}"
        self.assertIn(expected, SOURCE)

    def test_each_channel_can_test_the_selected_event(self):
        for method in ("testWebhook", "testTelegram", "testPushPlus"):
            self.assertIn(f"api.{method}({{", SOURCE)
        self.assertGreaterEqual(SOURCE.count("_test_event: event"), 3)


if __name__ == "__main__":
    unittest.main()

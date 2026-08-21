import unittest
import tempfile
import os
from unittest.mock import MagicMock, patch

import yaml

from control.app import config, notify_push

from control.app.notify_push import (
    EV_INCOMING_CALL,
    EV_INCOMING_SMS,
    build_payload,
    build_notification_message,
    build_webhook_request,
    send_pushplus,
    telegram_session,
    _deliver_with_retry,
    delivery_status,
)


class NotificationChannelTests(unittest.TestCase):
    def test_legacy_private_preset_migrates_to_standard_custom_webhook(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), patch.object(
                config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            with open(config.CONFIG_PATH, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"settings": {"webhook": {
                    "enabled": True, "format": "universal_push", "url": "https://example.test",
                    "source": "gateway", "token": "secret"}}}, handle)
            webhook = config.get_settings()["webhook"]
        self.assertEqual(webhook["format"], "custom")
        self.assertNotIn("source", webhook)
        self.assertNotIn("token", webhook)
        self.assertIn("X-App-Token", webhook["headers_json"])
        self.assertIn('"source": "gateway"', webhook["payload_template"])

    def test_sms_message_contains_title_content_and_sender(self):
        canonical = build_payload(
            EV_INCOMING_SMS,
            {"id": 1, "name": "UK SIM", "msisdn": "+44123"},
            "+44700",
            "hello",
        )
        actual = build_notification_message(canonical)
        self.assertIn("短信", actual["title"])
        self.assertIn("hello", actual["content"])
        self.assertIn("+44700", actual["content"])

    def test_call_payload_does_not_include_sms_text(self):
        canonical = build_payload(EV_INCOMING_CALL, {"id": 2}, "+86150", None)
        actual = build_notification_message(canonical)
        self.assertIn("来电", actual["title"])
        self.assertNotIn("None", actual["content"])

    def test_private_adapter_is_an_ordinary_custom_webhook(self):
        canonical = build_payload(EV_INCOMING_SMS, {"id": 1}, "+100", "hello")
        method, url, kwargs = build_webhook_request({
            "format": "custom", "method": "POST", "body_mode": "json",
            "url": "https://example.test/hook",
            "headers_json": '{"X-App-Token":"private"}',
            "payload_template": '{"source":"gateway","title":"{{title}}","content":"{{content}}"}',
        }, canonical)
        self.assertEqual((method, url), ("POST", "https://example.test/hook"))
        self.assertEqual(kwargs["headers"]["X-App-Token"], "private")
        self.assertEqual(kwargs["json"]["source"], "gateway")
        self.assertIn("hello", kwargs["json"]["content"])

    @patch("control.app.notify_push.requests.post")
    def test_pushplus_uses_official_json_contract(self, post):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 200, "msg": "success"}
        post.return_value = response
        result = send_pushplus({"token": "secret", "topic": "family", "template": "html",
                                "channel": "wechat"},
                               build_payload(EV_INCOMING_CALL, {"id": 1}, "+100", None))
        self.assertTrue(result["ok"])
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["token"], "secret")
        self.assertEqual(request["topic"], "family")
        self.assertIn("title", request)
        self.assertIn("content", request)

    def test_manual_telegram_proxy_is_applied_without_environment_proxy(self):
        session = telegram_session({"proxy_mode": "manual",
                                     "proxy_url": "socks5h://127.0.0.1:1080"})
        try:
            self.assertFalse(session.trust_env)
            self.assertEqual(session.proxies["https"], "socks5h://127.0.0.1:1080")
        finally:
            session.close()

    def test_country_telegram_proxy_uses_remote_dns_through_verified_exit(self):
        with patch("control.app.notify_push.egress.status", return_value={"exits": {
                "gb": {"ready": True, "interface": "mdd-gb",
                       "proxy_host": "172.17.0.1", "proxy_port": 22027}}}):
            session = telegram_session({"proxy_mode": "country", "proxy_country": "GB"})
            try:
                self.assertEqual(session.proxies["http"], "socks5h://172.17.0.1:22027")
                self.assertEqual(session.proxies["https"], "socks5h://172.17.0.1:22027")
            finally:
                session.close()
        with patch("control.app.notify_push.egress.status", return_value={"exits": {}}):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                telegram_session({"proxy_mode": "country", "proxy_country": "gb"})

    def test_delivery_history_retries_and_never_stores_message_body(self):
        calls = []
        def sender(_cfg, _payload):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("contains a secret response")
            return {"ok": True, "status_code": 204}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
                "os.environ", {"MDD_DATA": temp}), patch(
                    "control.app.notify_push.time.sleep", return_value=None):
            _deliver_with_retry("webhook", sender, {}, {
                "event": EV_INCOMING_SMS, "instance": "line", "text": "private text"})
            history = delivery_status()["history"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(history[0]["status"], "delivered")
        self.assertEqual(history[0]["attempts"], 3)
        self.assertNotIn("private text", str(history))


if __name__ == "__main__":
    unittest.main()


class HostAlertNotificationTests(unittest.TestCase):
    """The host alert is not a SIM event. Rendering it through the call/SMS path produced
    "📞 Incoming call — SIM: Raspberry Pi 3 Model B, From: Raspberry Pi 3 Model B"."""

    def _payload(self):
        return notify_push.build_payload(
            notify_push.EV_HOST_ALERT,
            {"id": "host", "name": "Raspberry Pi 3 Model B"},
            "Raspberry Pi 3 Model B",
            "[warning] 检测到历史欠压事件。")

    def test_telegram_does_not_render_it_as_a_call(self):
        text = notify_push._telegram_text(self._payload())
        self.assertNotIn("Incoming call", text)
        self.assertNotIn("SIM:", text)
        self.assertIn("网关主机异常", text)
        self.assertIn("欠压", text)

    def test_the_shared_message_builder_uses_host_wording(self):
        message = notify_push.build_notification_message(self._payload())
        self.assertIn("主机", message["title"])
        self.assertNotIn("来电", message["title"])
        self.assertIn("欠压", message["content"])

    def test_a_channel_can_switch_host_alerts_off_independently(self):
        enabled = notify_push._events_enabled({"events": {"host_alert": False}})
        self.assertFalse(enabled[notify_push.EV_HOST_ALERT])
        # The other categories are unaffected by that choice.
        self.assertTrue(enabled[notify_push.EV_INCOMING_SMS])
        self.assertTrue(enabled[notify_push.EV_INCOMING_CALL])

    def test_a_freshly_enabled_channel_gets_host_alerts(self):
        self.assertTrue(notify_push._events_enabled({})[notify_push.EV_HOST_ALERT])


class ActivationReminderNotificationTests(unittest.TestCase):
    def test_it_is_enabled_by_default_and_can_be_disabled_per_channel(self):
        self.assertTrue(notify_push._events_enabled({})[
            notify_push.EV_ACTIVATION_REMINDER])
        self.assertFalse(notify_push._events_enabled({"events": {
            notify_push.EV_ACTIVATION_REMINDER: False,
        }})[notify_push.EV_ACTIVATION_REMINDER])

    def test_it_has_expiry_wording_not_call_or_sms_wording(self):
        payload = notify_push.build_payload(
            notify_push.EV_ACTIVATION_REMINDER,
            {"id": "1", "name": "US SIM"}, "2026-08-28",
            "线路 US SIM 将于 2026-08-28 到期，还剩 3 天。")
        built = notify_push.build_notification_message(payload)
        telegram = notify_push._telegram_text(payload)
        self.assertIn("即将到期", built["title"])
        self.assertIn("还剩 3 天", built["content"])
        self.assertIn("即将到期", telegram)
        self.assertNotIn("Incoming call", telegram)
        self.assertNotIn("Incoming SMS", telegram)

    def test_enabled_channel_detection_honours_category_toggle(self):
        settings = {"telegram": {"enabled": True, "events": {
            notify_push.EV_ACTIVATION_REMINDER: False}}}
        self.assertFalse(notify_push.has_enabled_channel(
            settings, notify_push.EV_ACTIVATION_REMINDER))
        settings["telegram"]["events"][notify_push.EV_ACTIVATION_REMINDER] = True
        self.assertTrue(notify_push.has_enabled_channel(
            settings, notify_push.EV_ACTIVATION_REMINDER))


class NumberChangeNotificationTests(unittest.TestCase):
    """A ported number changes the line's caller identity, so it is announced rather than
    silently corrected."""

    def _payload(self):
        return notify_push.build_payload(
            notify_push.EV_NUMBER_CHANGED,
            {"id": "5", "name": "voxi", "msisdn": "+447700900123"},
            "+447700900123", "+447700900456 → +447700900123")

    def test_it_is_not_rendered_as_a_call_or_an_sms(self):
        text = notify_push._telegram_text(self._payload())
        self.assertNotIn("Incoming call", text)
        self.assertNotIn("Incoming SMS", text)
        self.assertIn("号码已变更", text)
        self.assertIn("+447700900123", text)

    def test_the_shared_builder_names_the_line(self):
        message = notify_push.build_notification_message(self._payload())
        self.assertIn("voxi", message["title"])
        self.assertIn("+447700900456", message["content"])

    def test_it_has_its_own_per_channel_toggle(self):
        enabled = notify_push._events_enabled({"events": {"number_changed": False}})
        self.assertFalse(enabled[notify_push.EV_NUMBER_CHANGED])
        self.assertTrue(enabled[notify_push.EV_HOST_ALERT])
        self.assertTrue(enabled[notify_push.EV_INCOMING_SMS])


class UnrecoverableLineNotificationTests(unittest.TestCase):
    """A gateway that has stopped trying must say so, and must not be mistaken for a call."""

    PAYLOAD = {"event": notify_push.EV_LINE_UNRECOVERABLE, "sim_name": "voxi",
               "instance": "5", "from": "SD-US", "text": "所有候选出口都试过了。"}

    def test_it_is_not_rendered_as_a_call_or_an_sms(self):
        text = notify_push._telegram_text(self.PAYLOAD)
        self.assertIn("线路无法自动恢复", text)
        self.assertNotIn("Incoming call", text)
        self.assertNotIn("Incoming SMS", text)

    def test_the_shared_builder_names_the_line_and_carries_the_reason(self):
        built = notify_push.build_notification_message(self.PAYLOAD)
        self.assertIn("voxi", built["title"])
        self.assertEqual(built["content"], self.PAYLOAD["text"])

    def test_it_has_its_own_per_channel_toggle(self):
        self.assertFalse(notify_push._events_enabled(
            {"events": {notify_push.EV_LINE_UNRECOVERABLE: False}}
        )[notify_push.EV_LINE_UNRECOVERABLE])
        self.assertTrue(notify_push._events_enabled({})[notify_push.EV_LINE_UNRECOVERABLE])

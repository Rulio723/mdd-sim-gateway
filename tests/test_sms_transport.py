import unittest
from unittest.mock import AsyncMock, Mock, patch

from control.app import main


def _message(instance, direction, peer, body, status="ok", transport="vowifi"):
    return {
        "id": 71,
        "instance": str(instance),
        "direction": direction,
        "peer": peer,
        "body": body,
        "status": status,
        "error": None,
        "ts": 123,
        "transport": transport,
    }


class SmsTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        main.hub.sms_send_locks.clear()

    async def test_auto_prefers_confirmed_registered_vowifi(self):
        ami = Mock(connected=True)
        ami.registration_state = AsyncMock(return_value="Registered")
        ami.send_sms = AsyncMock(return_value={"ok": True})

        def close_task(coro):
            coro.close()
            return Mock()

        with patch.object(main.hub, "ami_for", new=AsyncMock(return_value=ami)), \
                patch.object(main.cellular_sms, "send") as cellular_send, \
                patch.object(main.store, "add_message", side_effect=_message) as add, \
                patch.object(main.store, "set_message_status"), \
                patch.object(main.hub, "broadcast", new=AsyncMock()), \
                patch.object(main.asyncio, "create_task", side_effect=close_task):
            result = await main.send_sms_on_line("3", "6700", "DATA", "auto")

        self.assertTrue(result["ok"])
        self.assertEqual(result["transport"], "vowifi")
        self.assertEqual(result["requested_transport"], "auto")
        ami.registration_state.assert_awaited_once()
        ami.send_sms.assert_awaited_once_with("6700", "DATA")
        cellular_send.assert_not_called()
        self.assertEqual(add.call_args.kwargs["transport"], "vowifi")

    async def test_auto_selects_cellular_before_submit_when_vowifi_is_not_registered(self):
        ami = Mock(connected=True)
        ami.registration_state = AsyncMock(return_value="Unregistered")
        ami.send_sms = AsyncMock()
        cellular_result = {
            "ok": True, "status": "sent", "error": None, "stage": "send",
            "transport": "cellular", "unavailable": False, "uncertain": False,
            "modem_path": "/org/freedesktop/ModemManager1/Modem/0",
            "sms_path": "/org/freedesktop/ModemManager1/SMS/1",
        }

        with patch.object(main.hub, "ami_for", new=AsyncMock(return_value=ami)), \
                patch.object(main.cfg, "list_instances", return_value=[
                    {"id": "3", "iccid": "card-a"}]), \
                patch.object(main.cellular_sms, "send", return_value=cellular_result) as send, \
                patch.object(main.store, "add_message", side_effect=_message) as add, \
                patch.object(main.store, "set_message_status"), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.send_sms_on_line("3", "888", "BAL", "auto")

        self.assertTrue(result["ok"])
        self.assertEqual(result["transport"], "cellular")
        ami.send_sms.assert_not_awaited()
        send.assert_called_once()
        self.assertEqual(add.call_args.kwargs["transport"], "cellular")

    async def test_auto_never_retries_cellular_after_vowifi_submission_failure(self):
        ami = Mock(connected=True)
        ami.registration_state = AsyncMock(return_value="Registered")
        ami.send_sms = AsyncMock(return_value={"ok": False, "error": "AMI timeout"})

        with patch.object(main.hub, "ami_for", new=AsyncMock(return_value=ami)), \
                patch.object(main.cellular_sms, "send") as cellular_send, \
                patch.object(main.store, "add_message", side_effect=_message), \
                patch.object(main.store, "set_message_status") as set_status, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.send_sms_on_line("3", "+15551234567", "hello", "auto")

        self.assertFalse(result["ok"])
        self.assertEqual(result["transport"], "vowifi")
        cellular_send.assert_not_called()
        set_status.assert_called_once_with(71, "failed", "AMI timeout")

    async def test_explicit_cellular_does_not_probe_or_use_vowifi(self):
        cellular_result = {
            "ok": True, "status": "sent", "error": None, "stage": "send",
            "transport": "cellular", "unavailable": False, "uncertain": False,
            "modem_path": "/org/freedesktop/ModemManager1/Modem/0",
            "sms_path": "/org/freedesktop/ModemManager1/SMS/2",
        }
        with patch.object(main.hub, "ami_for", new=AsyncMock()) as ami_for, \
                patch.object(main.cfg, "list_instances", return_value=[]), \
                patch.object(main.cellular_sms, "send", return_value=cellular_result), \
                patch.object(main.store, "add_message", side_effect=_message), \
                patch.object(main.store, "set_message_status"), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.send_sms_on_line("4", "6700", "BAL", "cellular")

        self.assertTrue(result["ok"])
        self.assertEqual(result["transport"], "cellular")
        ami_for.assert_not_awaited()

    async def test_cellular_timeout_is_recorded_as_unknown_not_failed(self):
        cellular_result = {
            "ok": False, "status": "unknown",
            "error": "Cellular SMS send timed out; delivery is unknown and was not retried.",
            "stage": "send", "transport": "cellular", "unavailable": False,
            "uncertain": True, "modem_path": "/org/freedesktop/ModemManager1/Modem/0",
            "sms_path": "/org/freedesktop/ModemManager1/SMS/3",
            "message_id": 901,
        }
        reserved = _message("5", "out", "888", "BAL", status="pending",
                            transport="cellular")
        with patch.object(main.cfg, "list_instances", return_value=[]), \
                patch.object(main.cellular_sms, "send", return_value=cellular_result), \
                patch.object(main.store, "get_message",
                             return_value=reserved) as lookup, \
                patch.object(main.store, "add_message") as add, \
                patch.object(main.store, "set_message_status") as set_status, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.send_sms_on_line("5", "888", "BAL", "cellular")

        self.assertTrue(result["uncertain"])
        self.assertEqual(result["message"]["status"], "unknown")
        self.assertNotIn("message_id", result)
        lookup.assert_called_once_with(901)
        add.assert_not_called()
        set_status.assert_called_once_with(71, "unknown", cellular_result["error"])

    async def test_auto_reports_both_routes_unavailable_without_creating_message(self):
        cellular_result = {
            "ok": False, "status": "unavailable", "error": "No matching modem.",
            "stage": "lookup", "transport": "cellular", "unavailable": True,
            "uncertain": False, "modem_path": None, "sms_path": None,
        }
        with patch.object(main.hub, "ami_for", new=AsyncMock(return_value=None)), \
                patch.object(main.cfg, "list_instances", return_value=[]), \
                patch.object(main.cellular_sms, "send", return_value=cellular_result), \
                patch.object(main.store, "add_message") as add:
            result = await main.send_sms_on_line("6", "6700", "DATA", "auto")

        self.assertFalse(result["ok"])
        self.assertTrue(result["unavailable"])
        self.assertIn("VoWiFi is not registered", result["error"])
        self.assertIn("No matching modem", result["error"])
        add.assert_not_called()

    async def test_api_rejects_invalid_transport_before_sending(self):
        with patch.object(main, "send_sms_on_line", new=AsyncMock()) as send:
            with self.assertRaises(main.HTTPException) as raised:
                await main.api_sms_send("3", {
                    "to": "6700", "body": "DATA", "transport": "satellite"})

        self.assertEqual(raised.exception.status_code, 422)
        send.assert_not_awaited()

    async def test_allowance_query_sends_only_after_resolved_rule(self):
        inst = {"id": "1", "mcc": "310", "mnc": "240",
                "carrier_identity": {"gid1": "value"}}
        sent = {"ok": True, "transport": "vowifi", "message": _message(
            "1", "out", "6700", "BAL", status="sent")}
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.carrier_id, "lookup",
                             return_value={"name": "Ultra/Univision", "specific": True}), \
                patch.object(main.store, "get_allowance_query_rule", return_value=None), \
                patch.object(main.store, "start_allowance_query",
                             return_value={"id": 4, "started_ts": 100}) as start, \
                patch.object(main.store, "set_allowance_query_status") as status, \
                patch.object(main, "send_sms_on_line", new=AsyncMock(return_value=sent)) as send:
            result = await main.api_allowance_query("1", {"transport": "auto"})

        self.assertTrue(result["ok"])
        start.assert_called_once_with("1", "6700", "BAL", "ultramobile", "auto")
        send.assert_awaited_once_with("1", "6700", "BAL", "auto")
        status.assert_called_once_with(4, "sent")

    async def test_allowance_query_unknown_carrier_never_sends(self):
        inst = {"id": "2", "name": "ultramobile", "mcc": "310", "mnc": "240"}
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.carrier_id, "lookup",
                             return_value={"name": "T-Mobile - US", "specific": False}), \
                patch.object(main.store, "get_allowance_query_rule", return_value=None), \
                patch.object(main, "send_sms_on_line", new=AsyncMock()) as send:
            with self.assertRaises(main.HTTPException) as raised:
                await main.api_allowance_query("2", {"transport": "auto"})

        self.assertEqual(raised.exception.status_code, 409)
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

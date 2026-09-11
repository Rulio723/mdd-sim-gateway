"""Unique device-binding fixes observed on CORIG ML307X."""
import unittest
from unittest.mock import patch

from control.app import main


ICCID = "89000000000000000000"
DEVICE_ID = "2c91-0002-123456789012"
LINE_IMEI = "350000000000006"


class ModemSlotCapacityTests(unittest.TestCase):
    def test_active_slot_capacity_ignores_extra_enumerated_readers(self):
        identity = {
            "hardware_id": DEVICE_ID,
            "slots": 3,
            "channel_allocated": 3,
            "channel_capacity": 3,
        }
        with patch.object(main, "_device_identities", return_value={DEVICE_ID: identity}):
            self.assertEqual(main._modem_active_slot_capacity(DEVICE_ID, 4), 3)


class ModemImeiFallbackTests(unittest.TestCase):
    def test_keeps_configured_line_imei_when_modem_has_none(self):
        inst = {"id": "2", "iccid": ICCID, "imei": LINE_IMEI}
        card = {"name": f"VoWiFi Modem {DEVICE_ID} 00 00", "present": True,
                "iccid": ICCID, "hardware_id": DEVICE_ID}
        with patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main, "_hardware_imei_for_card",
                             return_value=("", DEVICE_ID, "modem")):
            out = main._apply_current_hardware_imei(inst)
        self.assertIs(out, inst)
        self.assertEqual(out["imei"], LINE_IMEI)

    def test_still_requires_imei_when_line_has_none(self):
        inst = {"id": "2", "iccid": ICCID, "imei": ""}
        card = {"name": f"VoWiFi Modem {DEVICE_ID} 00 00", "present": True,
                "iccid": ICCID, "hardware_id": DEVICE_ID}
        with patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main, "_hardware_imei_for_card",
                             return_value=("", DEVICE_ID, "modem")):
            with self.assertRaises(main.HTTPException) as raised:
                main._apply_current_hardware_imei(inst)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "hardware_imei_required")


if __name__ == "__main__":
    unittest.main()

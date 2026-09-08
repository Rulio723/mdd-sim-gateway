"""Bridge identity records must survive a modem that reports no IMEI.

The VPCD bridge publishes an identity whose ``imei`` is empty whenever the module never
answered the AT query -- it prints "imei=unavailable" and carries on, because the IMEI is
not what identifies the modem (the hardware id in the reader name is). The control plane
used to discard the whole record over that empty field and fall through to a synthetic
``{"hardware_id": ..., "slots": 1}``, which took two things down with it: the bridge's
ICCID, so the card never matched a line and the reader binding never migrated, and the
slot count, so PIN/SWu/IMS collapsed onto a single VPCD reader.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import main


DEVICE_ID = "2c91-0002-000000000001"
READER = f"VoWiFi Modem {DEVICE_ID} 00 00"
ICCID = "8900000000000000001"
IMEI = "350000000000006"


class ModemIdentityMetadataTests(unittest.TestCase):
    def _identity(self, **overrides):
        record = {"hardware_id": DEVICE_ID, "imei": "", "iccid": ICCID, "slots": 3,
                  "channel_allocated": 3, "channel_capacity": 3}
        record.update(overrides)
        with tempfile.TemporaryDirectory() as temp:
            modems = Path(temp) / "modems"
            modems.mkdir()
            (modems / f"{DEVICE_ID}.json").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(main.cfg, "DATA_DIR", temp):
                return main._modem_identity_for_reader(READER)

    def test_identity_without_an_imei_keeps_its_iccid_and_slots(self):
        identity = self._identity()
        self.assertEqual(identity["hardware_id"], DEVICE_ID)
        self.assertEqual(identity["iccid"], ICCID)
        self.assertEqual(identity["slots"], 3)
        self.assertEqual(identity["imei"], "")

    def test_a_valid_imei_is_still_normalised_and_returned(self):
        self.assertEqual(self._identity(imei=IMEI)["imei"], IMEI)

    def test_an_unusable_imei_is_reported_as_absent_not_as_itself(self):
        # Callers gate on a 15-digit IMEI; handing them a partial one would let it reach a
        # line configuration as though the modem had really reported it.
        for value in ("12345", "not-an-imei", None):
            with self.subTest(value=value):
                self.assertEqual(self._identity(imei=value)["imei"], "")

    def test_an_unknown_reader_name_is_still_unidentified(self):
        self.assertIsNone(main._modem_identity_for_reader("Some USB Reader 00 00"))

    def test_a_modem_with_no_published_record_keeps_the_offline_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "modems").mkdir()
            with patch.object(main.cfg, "DATA_DIR", temp):
                identity = main._modem_identity_for_reader(READER)
        self.assertEqual(identity, {"hardware_id": DEVICE_ID, "slots": 1})


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config


class ProductBoundaryTests(unittest.TestCase):
    def temp_config(self):
        temp = tempfile.TemporaryDirectory()
        paths = patch.multiple(
            config,
            DATA_DIR=temp.name,
            CONFIG_PATH=str(Path(temp.name) / "config.yaml"),
        )
        return temp, paths

    def test_sixth_sim_line_is_refused_but_existing_lines_remain_editable(self):
        temp, paths = self.temp_config()
        with temp, paths:
            for iid in range(1, config.MAX_SIM_LINES + 1):
                config.upsert_instance({"id": str(iid), "name": f"SIM {iid}"})
            with self.assertRaises(config.LineLimitError):
                config.upsert_instance({"id": "6", "name": "SIM 6"})
            edited = config.upsert_instance({"id": "5", "name": "kept"})
            self.assertEqual(edited["name"], "kept")

    def test_stale_remote_controls_are_removed_on_load_and_save(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({
                "settings": {"telegram": {"commands": {"enabled": True}}},
                "instances": {"1": {"id": "1", "sip": {
                    "external": [{"username": "remote", "password": "secret"}]}}},
            })
            loaded = config.load()
            self.assertNotIn("commands", loaded["settings"]["telegram"])
            self.assertEqual(loaded["instances"]["1"]["sip"]["external"], [])

            saved = config.upsert_instance({"id": "1", "sip": {
                "external": [{"username": "remote", "password": "secret"}]}})
            self.assertEqual(saved["sip"]["external"], [])

    def test_retired_activation_event_is_removed_from_every_notification_channel(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({
                "settings": {key: {"events": {"activation_reminder": True}}
                             for key in ("webhook", "telegram", "pushplus")},
                "instances": {},
            })
            settings = config.load()["settings"]
            for key in ("webhook", "telegram", "pushplus"):
                self.assertNotIn("activation_reminder", settings[key]["events"])
                self.assertTrue(settings[key]["events"]["software_update"])

    def test_only_first_five_legacy_lines_are_startable(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({"instances": {
                str(iid): {"id": str(iid), "index": iid}
                for iid in range(1, 8)
            }})
            self.assertTrue(config.line_allowed("5"))
            self.assertFalse(config.line_allowed("6"))


if __name__ == "__main__":
    unittest.main()

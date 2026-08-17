import tempfile
import unittest
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from control.app import main
from host import mdd_orchestrator
from host.mdd_orchestrator import Orchestrator


class BridgeRestartHandshakeTests(unittest.TestCase):
    def test_restart_completes_only_for_new_pid_ready_channels_and_target_iccid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root)
            old = Mock()
            old.poll.return_value = None
            other = Mock()
            other.poll.return_value = None
            app.bridges = {"modem-1": old, "modem-2": other}
            app.bridge_ports = {"modem-1": 15360, "modem-2": 15616}
            request_id = "switch-1"
            mdd_orchestrator.atomic_json(
                app.bridge_restart_request_dir / f"{request_id}.json", {
                    "request_id": request_id,
                    "device_id": "modem-1",
                    "expected_iccid_sha256": hashlib.sha256(
                        b"profile-target").hexdigest(),
                    "requested_at": 100,
                })

            app.process_bridge_restart_requests()

            old.terminate.assert_called_once()
            old.wait.assert_called_once_with(8)
            other.terminate.assert_not_called()
            self.assertNotIn("modem-1", app.bridges)
            status_path = app.bridge_restart_status_dir / f"{request_id}.json"
            self.assertEqual(mdd_orchestrator.read_json(status_path)["state"], "stopped")

            replacement = SimpleNamespace(pid=22, poll=lambda: None)
            app.bridges["modem-1"] = replacement
            identity_path = app.data / "modems" / "modem-1.json"
            mdd_orchestrator.atomic_json(identity_path, {
                "bridge_pid": 11, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-target",
            })
            app.finish_bridge_restart_requests({"modem-1", "modem-2"})
            self.assertEqual(mdd_orchestrator.read_json(status_path)["state"], "spawned")

            mdd_orchestrator.atomic_json(identity_path, {
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-old",
            })
            app.finish_bridge_restart_requests({"modem-1", "modem-2"})
            self.assertEqual(mdd_orchestrator.read_json(status_path)["state"], "spawned")

            mdd_orchestrator.atomic_json(identity_path, {
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-target",
            })
            app.finish_bridge_restart_requests({"modem-1", "modem-2"})
            status = mdd_orchestrator.read_json(status_path)
            self.assertEqual(status["state"], "channels_ready")
            self.assertEqual(status["bridge_pid"], 22)
            self.assertIs(app.bridges["modem-2"], other)

    def test_requests_for_two_modems_have_independent_status_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root)
            first, second = Mock(), Mock()
            first.poll.return_value = second.poll.return_value = None
            app.bridges = {"modem-1": first, "modem-2": second}
            for request_id, device_id in (("switch-1", "modem-1"),
                                          ("switch-2", "modem-2")):
                mdd_orchestrator.atomic_json(
                    app.bridge_restart_request_dir / f"{request_id}.json", {
                        "request_id": request_id, "device_id": device_id,
                        "expected_iccid_sha256": hashlib.sha256(
                            f"profile-{device_id[-1]}".encode()).hexdigest(),
                    })

            app.process_bridge_restart_requests()

            self.assertEqual(
                mdd_orchestrator.read_json(
                    app.bridge_restart_status_dir / "switch-1.json")["device_id"],
                "modem-1")
            self.assertEqual(
                mdd_orchestrator.read_json(
                    app.bridge_restart_status_dir / "switch-2.json")["device_id"],
                "modem-2")


class ESimProfileSwitchControlTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_line_is_scoped_from_its_live_modem_match(self):
        reader = "VoWiFi Modem modem-1 00 00"
        with patch.object(main.hub, "cards", {
                reader: {"name": reader, "present": True, "matched": "legacy"}}):
            self.assertTrue(main._esim_instance_uses_modem(
                {"id": "legacy"}, "modem-1"))

    async def test_refresh_requires_every_virtual_reader_to_report_target_iccid(self):
        readers = [
            "VoWiFi Modem modem-1 00 00",
            "VoWiFi Modem modem-1 00 01",
            "VoWiFi Modem modem-1 00 02",
        ]
        target = SimpleNamespace(
            iccid="profile-target", imsi="imsi-target", mcc="234", mnc="15",
            mnc_len=2, pin_enabled=False, pin_tries=3, smsc="",
            carrier_identity={})
        cards = {reader: {"name": reader, "index": index, "present": True,
                          "iccid": "profile-old"}
                 for index, reader in enumerate(readers)}
        instance = {"id": "2", "iccid": "profile-target"}
        with patch.object(main.sim, "list_readers", return_value=readers), \
                patch.object(main.sim, "read_card", return_value=target), \
                patch.object(main, "_match_instance_by_iccid", return_value=instance), \
                patch.object(main, "_carrier_identity_update", return_value={}), \
                patch.object(main.hub, "cards", cards), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            info, refreshed = await main._esim_refresh_modem_readers(
                readers[0], "modem-1", "profile-target")

        self.assertEqual(info["iccid"], "profile-target")
        self.assertEqual(refreshed, readers)
        self.assertEqual({card["iccid"] for card in cards.values()}, {"profile-target"})

    async def test_prepare_and_restore_preserve_the_exact_running_snapshot(self):
        lines = {
            "1": {"id": "1", "enabled": True, "imei_source_device_id": "modem-1"},
            "2": {"id": "2", "enabled": True, "imei_source_device_id": "modem-1"},
            "3": {"id": "3", "enabled": True, "imei_source_device_id": "modem-2"},
        }

        def get_instance(iid):
            return dict(lines[str(iid)])

        def upsert(value):
            iid = str(value["id"])
            lines[iid].update(value)
            return dict(lines[iid])

        with patch.object(main.cfg, "list_instances",
                          side_effect=lambda: [dict(line) for line in lines.values()]), \
                patch.object(main.cfg, "get_instance", side_effect=get_instance), \
                patch.object(main.cfg, "upsert_instance", side_effect=upsert), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.engine, "is_running",
                             side_effect=lambda iid: str(iid) == "1"), \
                patch.object(main.engine, "stop") as stop, \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.egress, "publish"):
            previous = await main._esim_prepare_profile_switch("modem-1")
            self.assertFalse(lines["1"]["enabled"])
            self.assertFalse(lines["2"]["enabled"])
            self.assertTrue(lines["3"]["enabled"])
            await main._esim_restore_profile_switch(previous)

        stop.assert_called_once_with("1")
        self.assertTrue(lines["1"]["enabled"])
        self.assertTrue(lines["2"]["enabled"])
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0]["id"], "1")

    async def test_successful_lpa_with_failed_recovery_stays_fail_closed(self):
        error = main.HTTPException(503, "bridge failed")
        with patch.object(main, "_esim_resolve_reader", return_value=("reader", 0)), \
                patch.object(main, "_esim_switch_identity",
                             return_value=("modem-1", "modem-1")), \
                patch.object(main, "_esim_prepare_profile_switch",
                             new=AsyncMock(return_value={"1": {"enabled": True,
                                                                "running": True}})), \
                patch.object(main, "_esim_modem_reader_names", return_value=["reader"]), \
                patch.object(main, "_esim_resolve_se", return_value={"id": "se", "aid": "a"}), \
                patch.object(main.lpa, "profile_enable", new=lambda *_a, **_k: object()), \
                patch.object(main, "_esim_run", new=AsyncMock()), \
                patch.object(main, "_esim_cache_update_profile"), \
                patch.object(main, "_esim_recover_profile_switch",
                             new=AsyncMock(side_effect=error)), \
                patch.object(main, "_esim_restore_profile_switch",
                             new=AsyncMock()) as restore:
            with self.assertRaises(main.HTTPException):
                await main.api_esim_enable("profile-target", {})

        restore.assert_not_awaited()
        self.assertNotIn("reader", main.hub.lpa_busy)

    async def test_lpa_failure_restores_previous_line_snapshot(self):
        previous = {"1": {"enabled": True, "running": True}}
        error = main.HTTPException(400, "lpac failed")
        with patch.object(main, "_esim_resolve_reader", return_value=("reader", 0)), \
                patch.object(main, "_esim_switch_identity",
                             return_value=("modem-1", "modem-1")), \
                patch.object(main, "_esim_prepare_profile_switch",
                             new=AsyncMock(return_value=previous)), \
                patch.object(main, "_esim_modem_reader_names", return_value=["reader"]), \
                patch.object(main, "_esim_resolve_se", return_value={"id": "se", "aid": "a"}), \
                patch.object(main.lpa, "profile_enable", new=lambda *_a, **_k: object()), \
                patch.object(main, "_esim_run", new=AsyncMock(side_effect=error)), \
                patch.object(main, "_esim_restore_profile_switch",
                             new=AsyncMock()) as restore:
            with self.assertRaises(main.HTTPException):
                await main.api_esim_enable("profile-target", {})

        restore.assert_awaited_once_with(previous)
        self.assertNotIn("reader", main.hub.lpa_busy)


if __name__ == "__main__":
    unittest.main()

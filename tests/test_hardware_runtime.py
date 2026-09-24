import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from runtime.hardware import HardwareSupervisor, kernel_objects


class HardwareRuntimeTests(unittest.TestCase):
    def test_loop_retries_a_transient_reconcile_failure_without_stopping_services(self):
        app = HardwareSupervisor(interval=0)
        app.start = Mock()
        app.dbus = app.modemmanager = app.networkmanager = Mock()
        app.dbus.poll.return_value = None
        app.publish_reconcile_error = Mock()

        attempts = iter((RuntimeError("mmcli unavailable"), None))
        def reconcile():
            outcome = next(attempts)
            if outcome:
                raise outcome
            app.stop = True
        app.reconcile = Mock(side_effect=reconcile)
        app.loop()

        self.assertEqual(app.reconcile.call_count, 2)
        app.publish_reconcile_error.assert_called_once()

    def test_close_marks_published_devices_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            root = data / "orchestrator"
            root.mkdir()
            (root / "devices-status.json").write_text(json.dumps({
                "version": 2,
                "devices": {"modem-1": {"present": True, "transitioning": True,
                                           "actual": {"vowifi_bridge_active": True,
                                                      "cellular_backend_active": True}}},
                "shared": {"modemmanager_active": True}}))
            app = HardwareSupervisor(status_path=data / "status.json", data_path=data)
            app.close()

            state = json.loads((root / "devices-status.json").read_text())
            self.assertFalse(state["devices"]["modem-1"]["present"])
            self.assertFalse(state["shared"]["modemmanager_active"])
    def test_esim_bridge_restart_waits_for_new_ready_pid_and_target_iccid(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            app = HardwareSupervisor(data_path=data)
            old = Mock()
            old.poll.return_value = None
            app.bridges = {"modem-1": old}
            request_id = "switch-1"
            app.bridge_restart_request_dir.mkdir(parents=True)
            (app.bridge_restart_request_dir / f"{request_id}.json").write_text(json.dumps({
                "request_id": request_id, "device_id": "modem-1",
                "expected_iccid_sha256": hashlib.sha256(b"profile-target").hexdigest(),
                "requested_at": 100,
            }))

            app.process_bridge_restart_requests()

            old.terminate.assert_called_once()
            old.wait.assert_called_once_with(8)
            status_path = app.bridge_restart_status_dir / f"{request_id}.json"
            self.assertEqual(json.loads(status_path.read_text())["state"], "stopped")

            replacement = Mock(pid=22)
            replacement.poll.return_value = None
            app.bridges["modem-1"] = replacement
            identity_path = data / "modems" / "modem-1.json"
            identity_path.parent.mkdir(parents=True)
            identity_path.write_text(json.dumps({
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-old"}))
            app.finish_bridge_restart_requests({"modem-1"})
            self.assertEqual(json.loads(status_path.read_text())["state"], "spawned")

            identity_path.write_text(json.dumps({
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-target"}))
            app.finish_bridge_restart_requests({"modem-1"})
            status = json.loads(status_path.read_text())
            self.assertEqual(status["state"], "channels_ready")
            self.assertEqual(status["bridge_pid"], 22)

    def test_invalid_esim_bridge_restart_is_rejected_without_stopping_bridge(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            process = Mock()
            process.poll.return_value = None
            app.bridges = {"modem-1": process}
            app.bridge_restart_request_dir.mkdir(parents=True)
            (app.bridge_restart_request_dir / "switch-1.json").write_text(json.dumps({
                "request_id": "switch-1", "device_id": "../modem-1",
                "expected_iccid_sha256": "bad"}))

            app.process_bridge_restart_requests()

            process.terminate.assert_not_called()
            status = json.loads((app.bridge_restart_status_dir /
                                 "switch-1.json").read_text())
            self.assertEqual(status["state"], "failed")

    def test_replugged_serialless_modem_migrates_saved_device_id(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            root = data / "orchestrator"
            identities = data / "modems"
            root.mkdir()
            identities.mkdir()
            old_id = "2c7c-0125-1-3"
            new_id = "2c7c-0125-1-3.1"
            wanted = {"cellular_enabled": False, "vowifi_enabled": True,
                      "flight_mode": False}
            (root / "devices-desired.json").write_text(json.dumps({
                "version": 2, "devices": {old_id: wanted}}))
            (root / "hardware-state.json").write_text(json.dumps({
                "assignments": {old_id: {"usb_path": "1-3"}}}))
            (root / "devices-status.json").write_text(json.dumps({
                "devices": {old_id: {"present": False}}}))
            for device_id in (old_id, new_id):
                (identities / f"{device_id}.json").write_text(json.dumps({
                    "hardware_id": device_id, "imei": "350000000000036"}))

            app = HardwareSupervisor(data_path=data)
            moved = app.migrate_device_ids([{
                "id": new_id, "tty": "/dev/ttyUSB2", "usb_path": "1-3.1",
                "vid": "2c7c", "pid": "0125"}])

            self.assertEqual(moved, [(old_id, new_id)])
            desired = json.loads((root / "devices-desired.json").read_text())["devices"]
            self.assertEqual(desired, {new_id: wanted})
            self.assertFalse((identities / f"{old_id}.json").exists())
            self.assertTrue((identities / f"{new_id}.json").exists())
            assignments = json.loads((root / "hardware-state.json").read_text())["assignments"]
            self.assertNotIn(old_id, assignments)

    def test_modem_id_migration_waits_for_matching_hardware_imei(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            root = data / "orchestrator"
            identities = data / "modems"
            root.mkdir()
            identities.mkdir()
            old_id = "2c7c-0125-1-3"
            new_id = "2c7c-0125-1-3.1"
            (root / "devices-desired.json").write_text(json.dumps({
                "version": 2, "devices": {old_id: {"vowifi_enabled": True}}}))
            (identities / f"{old_id}.json").write_text(json.dumps({
                "imei": "350000000000036"}))
            app = HardwareSupervisor(data_path=data)
            modem = {"id": new_id, "vid": "2c7c", "pid": "0125"}

            self.assertEqual(app.migrate_device_ids([modem]), [])
            (identities / f"{new_id}.json").write_text(json.dumps({
                "imei": "490154203237518"}))
            self.assertEqual(app.migrate_device_ids([modem]), [])
            desired = json.loads((root / "devices-desired.json").read_text())["devices"]
            self.assertIn(old_id, desired)

    def test_control_status_reports_present_cellular_modem(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            modem = {"id": "2c7c-0125-port", "tty": "/dev/ttyUSB2",
                     "usb_path": "1-3", "vid": "2c7c", "pid": "0125"}
            app.publish_control_state([modem], {modem["id"]})
            status = json.loads((Path(temp) / "orchestrator" /
                                 "devices-status.json").read_text())
            observed = status["devices"][modem["id"]]
            self.assertTrue(observed["present"])
            self.assertTrue(observed["actual"]["vowifi_bridge_active"])
            self.assertTrue(observed["actual"]["cellular_supported"])
            hardware = json.loads((Path(temp) / "orchestrator" /
                                   "hardware-state.json").read_text())
            self.assertIn(modem["id"], hardware["assignments"])

    def test_container_hardware_publishes_redacted_support_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            app = HardwareSupervisor(data_path=data)
            modem = {"id": "2c7c-0125-port", "tty": "/dev/ttyUSB2",
                     "usb_path": "1-3", "vid": "2c7c", "pid": "0125",
                     "base_port": 15360}
            process = Mock(pid=42)
            process.poll.return_value = None
            app.bridges = {modem["id"]: process}
            app.modemmanager = Mock()
            app.modemmanager.poll.return_value = None
            identity = data / "modems" / f'{modem["id"]}.json'
            identity.parent.mkdir(parents=True)
            identity.write_text(json.dumps({
                "imei": "350000000000036", "iccid": "8900000000000000022",
                "updated_at": int(__import__("time").time()),
                "channel_status": "ready", "channel_requested": 3,
                "channel_allocated": 3}))
            app.listening_tcp_ports = Mock(return_value={15360, 15361, 15362})

            app.publish_host_diagnostics(
                [modem], ["/org/freedesktop/ModemManager1/Modem/0"])

            path = data / "orchestrator" / "host-diagnostics.json"
            diagnostic = json.loads(path.read_text())
            bridge = diagnostic["bridges"][modem["id"]]
            self.assertTrue(bridge["channels_ready"])
            self.assertTrue(bridge["imei_valid"])
            self.assertTrue(bridge["iccid_valid"])
            self.assertEqual(diagnostic["virtualization"], "docker")
            self.assertNotIn("350000000000036", path.read_text())
            self.assertNotIn("8900000000000000022", path.read_text())

    def test_kernel_objects_are_restricted_to_modem_names(self):
        class Root:
            def __init__(self, names):
                self.names = names
            def glob(self, _):
                return [Path(name) for name in self.names]
        roots = (("tty", Root(["ttyUSB2", "tty0", "ttyACM1"]),
                  re.compile(r"tty(?:USB|ACM)\d+")),)
        with patch("runtime.hardware.EVENT_ROOTS", roots):
            self.assertEqual(kernel_objects(), {("tty", "ttyUSB2"), ("tty", "ttyACM1")})

    def test_reconcile_reports_add_remove_and_publishes_no_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(Path(temp) / "status.json", data_path=Path(temp))
            app.reported = {("tty", "ttyUSB9")}
            events = []
            app.report_event = lambda action, subsystem, name: events.append((action, subsystem, name))
            app.command = Mock(return_value=Mock(
                returncode=0,
                stdout="/org/freedesktop/ModemManager1/Modem/0\n"
                       "/org/freedesktop/ModemManager1/Modem/0\n"))
            app.discover_modems = Mock(return_value=[])
            app.reconcile_pcsc = Mock()
            with patch("runtime.hardware.kernel_objects", return_value={
                    ("tty", "ttyUSB2"), ("usbmisc", "cdc-wdm0"), ("net", "wwan0")}):
                app.reconcile()
            self.assertEqual(events[0], ("remove", "tty", "ttyUSB9"))
            self.assertEqual(set(events[1:]), {
                ("add", "tty", "ttyUSB2"), ("add", "usbmisc", "cdc-wdm0"),
                ("add", "net", "wwan0")})
            status = json.loads(app.status_path.read_text())
            self.assertEqual(status["modem_count"], 1)
            self.assertEqual(status["modems"], ["/org/freedesktop/ModemManager1/Modem/0"])
            self.assertEqual(status["hardware_count"], 0)
            self.assertEqual(status["pcsc_reader_count"], 0)
            self.assertEqual(status["ready_bridge_count"], 0)
            self.assertNotIn("imei", app.status_path.read_text().lower())

    def test_modem_object_is_matched_by_owned_tty(self):
        app = HardwareSupervisor()
        details = {
            "/org/freedesktop/ModemManager1/Modem/0": "modem.generic.ports : ttyUSB9 (at)",
            "/org/freedesktop/ModemManager1/Modem/1": "modem.generic.ports : ttyUSB2 (at)",
        }
        app.command = Mock(side_effect=lambda *args: Mock(
            returncode=0, stdout=details[args[2]]))
        self.assertEqual(app.modem_object_for_tty(
            "/dev/ttyUSB2", list(details)), "/org/freedesktop/ModemManager1/Modem/1")

    def test_rejected_event_fails_closed(self):
        app = HardwareSupervisor()
        app.command = Mock(return_value=Mock(returncode=1, stdout="denied"))
        with self.assertRaisesRegex(RuntimeError, "rejected add event"):
            app.report_event("add", "tty", "ttyUSB2")

    def test_networkmanager_refuses_to_continue_if_it_claims_a_nas_interface(self):
        app = HardwareSupervisor()
        app.command = Mock(return_value=Mock(
            returncode=0, stdout="wwan0:disconnected\novs_eth0:connected\nlo:unmanaged\n"))
        with self.assertRaisesRegex(RuntimeError, "ovs_eth0"):
            app.assert_networkmanager_isolated()

    def test_cellular_profile_can_never_autoconnect_or_become_default(self):
        app = HardwareSupervisor()
        calls = []

        def command(*args, **_kwargs):
            calls.append(list(args))
            if args[:3] == ("nmcli", "connection", "show"):
                return Mock(returncode=1, stdout="")
            return Mock(returncode=0, stdout="")

        app.command = command
        app.ensure_modem_data(
            {"id": "modem-a"},
            {"powered": True, "data_active": False, "registration": "home",
             "primary_port": "cdc-wdm0", "apn": "internet"})

        add = next(call for call in calls if call[:3] == ["nmcli", "connection", "add"])
        self.assertEqual(add[add.index("connection.autoconnect") + 1], "no")
        self.assertEqual(add[add.index("ipv4.never-default") + 1], "yes")
        self.assertEqual(add[add.index("ipv6.never-default") + 1], "yes")
        self.assertIn(["nmcli", "connection", "up", app.cellular_profile_name("modem-a")],
                      calls)

    def test_at_only_modem_with_qmi_kernel_ports_is_reset_for_recovery(self):
        app = HardwareSupervisor()
        app.assert_networkmanager_isolated = Mock()
        app.desired_devices = Mock(return_value={
            "modem-a": {"cellular_enabled": True, "vowifi_enabled": True,
                        "flight_mode": False}})
        app.modem_snapshot = Mock(return_value={
            "available": True, "mm_object": "/org/freedesktop/ModemManager1/Modem/0",
            "network_interface": "", "registration": "roaming", "data_active": False})
        app.command = Mock(return_value=Mock(returncode=0, stdout=""))
        app.terminate_qmi_proxy = Mock()
        fake_paths = [Mock()]
        fake_paths[0].exists.return_value = True
        with patch("runtime.hardware.Path.glob", return_value=fake_paths):
            app.reconcile_cellular([{"id": "modem-a", "tty": "/dev/ttyUSB2"}],
                                   ["/org/freedesktop/ModemManager1/Modem/0"])
        app.command.assert_called_once_with(
            "mmcli", "-m", "/org/freedesktop/ModemManager1/Modem/0", "--reset")
        app.terminate_qmi_proxy.assert_called_once_with()

    def test_qmi_recovery_is_not_suppressed_during_the_first_minutes_of_uptime(self):
        """The rate limit must not treat "never reset" as "reset at monotonic zero".

        A freshly booted host reports a small monotonic clock, and a restarted Hardware
        container is exactly when a stale QMI session needs the reset.
        """
        app = HardwareSupervisor()
        app.assert_networkmanager_isolated = Mock()
        app.desired_devices = Mock(return_value={
            "modem-a": {"cellular_enabled": True, "vowifi_enabled": True,
                        "flight_mode": False}})
        app.modem_snapshot = Mock(return_value={
            "available": True, "mm_object": "/org/freedesktop/ModemManager1/Modem/0",
            "network_interface": "", "registration": "roaming", "data_active": False})
        app.command = Mock(return_value=Mock(returncode=0, stdout=""))
        app.terminate_qmi_proxy = Mock()
        fake_paths = [Mock()]
        fake_paths[0].exists.return_value = True

        with patch("runtime.hardware.Path.glob", return_value=fake_paths), \
                patch("runtime.hardware.time.monotonic", return_value=5.0):
            app.reconcile_cellular([{"id": "modem-a", "tty": "/dev/ttyUSB2"}],
                                   ["/org/freedesktop/ModemManager1/Modem/0"])

        app.command.assert_called_once_with(
            "mmcli", "-m", "/org/freedesktop/ModemManager1/Modem/0", "--reset")
        app.terminate_qmi_proxy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

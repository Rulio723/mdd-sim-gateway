import json
import os
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from control.app import config, operations

try:
    from control.app import main
except ImportError:      # the Docker SDK is a manager runtime dependency this does not need
    main = None


class OperationsTests(unittest.TestCase):
    def test_engine_sources_do_not_log_authentication_secrets(self):
        root = Path(__file__).resolve().parents[1]
        sources = "\n".join(
            (root / name).read_text(errors="replace")
            for name in ("engine/swu_ike.py", "engine/ami_usim.py",
                         "host/vpcd_modem_bridge.py", "control/app/lpa.py")
        )
        forbidden = (
            "print('CK'", "print('IK'", "print('MSK'", "print('EMSK'",
            "print('KENCR'", "print('KAUT'", "DIFFIE-HELLMAN KEY",
            "IKEv2 DECRYPTION TABLE INFO", "ESP SA INFO (wireshark)",
            "AuthResponse sent: RES=",
            "print(a.get_imsi())", "device identity set: IMEI=",
            'AT <-- %s" % response.hex()',
            '" ".join(cmd)', 'lpac non-json stdout: %s',
        )
        self.assertEqual([item for item in forbidden if item in sources], [])

    def test_redaction_removes_identities_credentials_and_key_material(self):
        value = operations.redact({
            "pin": "1234",
            "nested": {"token": "secret"},
            "note": "call +441234567890",
            "subscription_url": "https://example.test/sub?token=secret",
            "headers_json": '{"Authorization":"Bearer secret"}',
            "activation_code": "LPA:1$smdp.example$MATCHING-ID",
        })
        self.assertEqual(value["pin"], "<redacted>")
        self.assertEqual(value["nested"]["token"], "<redacted>")
        self.assertNotIn("441234567890", value["note"])
        self.assertTrue(all("secret" not in str(value[key]).lower()
                            for key in ("subscription_url", "headers_json")))
        self.assertEqual(value["activation_code"], "<redacted>")
        log = operations.redact_log(
            "IKEv2 DECRYPTION TABLE INFO (Wireshark):\n"
            "aabbccddeeff00112233445566778899\n"
            "00112233445566778899aabbccddeeff\n"
            "CK=00112233445566778899aabbccddeeff\nnormal"
        )
        self.assertNotIn("001122", log)
        self.assertTrue(log.endswith("normal"))

    def test_redaction_preserves_non_secret_eap_aka_diagnostics(self):
        diagnostic = (
            "IKE_AUTH rejected with AUTHENTICATION_FAILED before any EAP-AKA challenge "
            "(SIM not queried); the SIM may not be provisioned for VoWiFi"
        )
        self.assertEqual(operations.redact_log(diagnostic), diagnostic)

    def test_apdu_trace_fallback_does_not_repeat_failed_unpack(self):
        source = (Path(__file__).resolve().parents[1] / "engine/swu_ike.py").read_text(
            errors="replace")
        self.assertNotIn("_data, _sw1, _sw2 = res", source)
        self.assertIn("unexpected response type=%s", source)

    def test_local_backup_is_not_exposed_as_file_contents(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            Path(temp, "config.yaml").write_text("settings: {}\ninstances: {}\n")
            result = operations.create_local_backup("Test Gateway")
            self.assertEqual(result["location"], "gateway-local")
            self.assertNotIn("path", result)
            self.assertTrue(Path(temp, "backups", result["name"]).is_file())

    def test_support_bundle_contains_only_redacted_documents(self):
        settings_value = {
            "telegram": {"bot_token": "secret"},
            "proxy": {"subscription_url": "https://example.test/sub?token=url-secret"},
            "webhook": {"headers_json": '{"Authorization":"Bearer header-secret"}'},
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), patch.object(
                config, "get_settings", return_value=settings_value):
            run = Path(temp, "instances", "sim1", "run")
            run.mkdir(parents=True)
            run.joinpath("charon.log").write_text(
                "ESP SA INFO (wireshark):\nsecret-table-row-1\nsecret-table-row-2\n"
                "CK=00112233445566778899aabbccddeeff\n")
            content = operations.support_bundle({"imei": "123456789012345"})
            with zipfile.ZipFile(BytesIO(content)) as archive:
                settings = archive.read("settings-redacted.yaml").decode()
                status = json.loads(archive.read("status-redacted.json"))
                log = archive.read("logs/sim1-charon.log").decode()
            self.assertNotIn("secret", settings)
            self.assertNotIn("001122", log)
            self.assertEqual(status["imei"], "<redacted>")

    def test_support_bundle_carries_the_host_view_the_control_plane_cannot_observe(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            orchestrator = Path(temp, "orchestrator")
            orchestrator.mkdir(parents=True)
            orchestrator.joinpath("host-diagnostics.json").write_text(json.dumps({
                "virtualization": "lxc",
                "modemmanager": {"unit_active": False,
                                 "unclaimed": {"unclaimed_ttys": ["/dev/ttyUSB2"]}},
                "recent_log": ["2026-08-14 12:09:16 waiting for ModemManager to claim "
                               "/dev/ttyUSB2"],
                "imei": "123456789012345",
            }))

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
            self.assertTrue(host["available"])
            self.assertEqual(host["virtualization"], "lxc")
            self.assertFalse(host["modemmanager"]["unit_active"])
            self.assertEqual(host["modemmanager"]["unclaimed"]["unclaimed_ttys"],
                             ["/dev/ttyUSB2"])
            self.assertIn("waiting for ModemManager", host["recent_log"][0])
            self.assertEqual(host["imei"], "<redacted>")

    def test_support_bundle_host_view_carries_bridge_activity_and_port_truth(self):
        """The three facts every card-path report needed by hand: what command each bridge
        runs, what it printed last, and whether pcscd actually listens on its ports."""
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            orchestrator = Path(temp, "orchestrator")
            orchestrator.mkdir(parents=True)
            orchestrator.joinpath("host-diagnostics.json").write_text(json.dumps({
                "modem_backend": "serial",
                "modemmanager": {"unit_active": False},
                "bridges": {"a": {"pid": 7, "running": True,
                                  "command": ["python", "bridge", "--modem", "/dev/ttyUSB2"],
                                  "log_tail": ["[bridge] allocated logical channels [1, 2, 3]"]}},
                "vpcd_ports_listening": {"a": {"15360": True, "15361": True, "15362": False}},
                "reader_definitions": ["libccidtwin", "mdd-sim-gateway-modems"],
            }))

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
            self.assertEqual(host["modem_backend"], "serial")
            self.assertIn("--modem", host["bridges"]["a"]["command"])
            self.assertIn("allocated logical channels",
                          host["bridges"]["a"]["log_tail"][0])
            self.assertFalse(host["vpcd_ports_listening"]["a"]["15362"])
            self.assertIn("mdd-sim-gateway-modems", host["reader_definitions"])

    def test_support_bundle_reports_a_missing_host_view_instead_of_omitting_it(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            content = operations.support_bundle({})
            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
        # Silence here would read as a healthy host; a stopped orchestrator is a finding.
        self.assertFalse(host["available"])
        self.assertIn("orchestrator", host["note"])

    def test_support_bundle_includes_retained_ike_segment_tail(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            archive_dir = Path(temp) / "instances" / "sim1" / "logs" / "ike"
            archive_dir.mkdir(parents=True)
            archive_dir.joinpath("charon-20260807-110000.log").write_text(
                "[2026-08-07 11:00:00+0800] STATE 1\n"
                "[2026-08-07 11:00:01+0800] STATE 2\n")

            content = operations.support_bundle({}, log_lines=50)

            with zipfile.ZipFile(BytesIO(content)) as bundle:
                retained = bundle.read(
                    "logs/sim1-charon-20260807-110000.log").decode()
            self.assertIn("STATE 2", retained)

    def test_diagnostic_records_survive_redaction_instead_of_being_blanked(self):
        # engine.capture_diagnostics embeds the tunnel log tail in each record, and swu_ike
        # prints "received decoded message" on every fragmented exchange. Under the line
        # rules that one phrase blanked the record and, via the two-line lookahead, the two
        # records after it — so every record of every bundle came out empty.
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            logs = Path(temp) / "instances" / "sim1" / "logs"
            logs.mkdir(parents=True)
            records = [
                {"ts": 1786894085, "reason": "health-freeze:reg_unanswered",
                 "registration": "unregistered", "pcscf": "fd00:976a:2:147::5",
                 "charon": {"retransmits": 3, "tail": ["received decoded message",
                                                       "SK_ei: 0011223344556677",
                                                       "tunnel CONNECTED"]},
                 "sip": ["SIP/2.0 401 Unauthorized"]},
                {"ts": 1786894200, "reason": "health-freeze:registering",
                 "registration": "unregistered", "charon": {"tail": ["STATE 4"]},
                 "sip": []},
                {"ts": 1786894300, "reason": "health-freeze:registering",
                 "registration": "unregistered", "charon": {"tail": ["STATE 4"]},
                 "sip": []},
            ]
            logs.joinpath("diagnostics.jsonl").write_text(
                "\n".join(json.dumps(item) for item in records) + "\n")

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                text = archive.read("logs/sim1-diagnostics.jsonl").decode()
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(parsed), 3)
        # The evidence that names the failure is what has to survive.
        self.assertEqual(parsed[0]["registration"], "unregistered")
        self.assertEqual(parsed[0]["reason"], "health-freeze:reg_unanswered")
        self.assertEqual(parsed[0]["sip"], ["SIP/2.0 401 Unauthorized"])
        self.assertEqual(parsed[0]["charon"]["retransmits"], 3)
        # Only the offending tail lines go, and the lookahead never reaches later records.
        tail = parsed[0]["charon"]["tail"]
        self.assertEqual(tail[0], "<redacted cryptographic material>")
        self.assertEqual(tail[1], "<redacted cryptographic material>")
        self.assertEqual(tail[2], "tunnel CONNECTED")
        self.assertEqual(parsed[1]["charon"]["tail"], ["STATE 4"])
        self.assertEqual(parsed[2]["charon"]["tail"], ["STATE 4"])

    def test_diagnostic_redaction_still_removes_identifiers_and_bad_records(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            logs = Path(temp) / "instances" / "sim1" / "logs"
            logs.mkdir(parents=True)
            logs.joinpath("diagnostics.jsonl").write_text(
                json.dumps({"usim": {"imsi": "001010123456789", "iccid": "8944000000000000000"},
                            "note": "peer 0011223344556677889900aabbccddeeff"}) + "\n"
                "not json at all: SKEYSEED: 00112233445566778899\n")

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                text = archive.read("logs/sim1-diagnostics.jsonl").decode()
        self.assertNotIn("001010123456789", text)
        self.assertNotIn("8944000000000000000", text)
        self.assertNotIn("0011223344556677889900aabbccddeeff", text)
        # A line that will not parse must not become a hole in the redaction.
        self.assertNotIn("00112233445566778899", text)
        self.assertIn("<redacted cryptographic material>", text.splitlines()[-1])

    def test_plain_logs_keep_the_strict_whole_line_rules(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            run = Path(temp) / "instances" / "sim1" / "run"
            run.mkdir(parents=True)
            run.joinpath("charon.log").write_text(
                "IKEv2 DECRYPTION TABLE\n"
                "0011223344556677\n"
                "8899aabbccddeeff\n"
                "tunnel CONNECTED\n")

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                lines = archive.read("logs/sim1-charon.log").decode().splitlines()
        self.assertEqual(lines[:3], ["<redacted cryptographic material>"] * 3)
        self.assertEqual(lines[3], "tunnel CONNECTED")

    def test_sensitive_config_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            config.save({"settings": {}, "instances": {}, "internal": {}})
            self.assertEqual(os.stat(config.CONFIG_PATH).st_mode & 0o777, 0o600)
            inst = {
                "id": "sim1", "imsi": "001010123456789", "mcc": "001", "mnc": "01",
                "ami_secret": "random-ami-secret",
                "sip": {"webrtc": {"enable": True, "password": "random-web-secret"}},
            }
            path = config.write_instance_json(inst, {})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(temp).st_mode & 0o777, 0o700)

    def test_engine_configuration_fails_closed_without_generated_credentials(self):
        inst = {"id": "sim1", "imsi": "001010123456789", "mcc": "001", "mnc": "01"}
        with self.assertRaisesRegex(ValueError, "AMI credential"):
            config.render_instance_json(inst, {})
        inst["ami_secret"] = "random-ami-secret"
        with self.assertRaisesRegex(ValueError, "WebRTC credential"):
            config.render_instance_json(inst, {})


@unittest.skipIf(main is None, "manager runtime dependencies are unavailable")
class LineDiagnosticsTests(unittest.TestCase):
    """The per-line verdict the bundle carries beside the logs."""

    def setUp(self):
        main.hub.status_cache.clear()
        main.hub.health.clear()
        main.hub.ok_since.clear()

    def test_summary_names_the_reason_and_how_far_the_retry_budget_has_run(self):
        main.hub.status_cache["1"] = {
            "state": "REGISTERING", "reason_code": "reg_unanswered",
            "reason": "The carrier's IMS stopped answering registration.",
            "retry": {"count": 2, "max": 3},
            "detail": {"registration": "unregistered", "active_channels": 0},
        }
        main.hub.health["1"] = {"fail_start": time.monotonic() - 61.0, "frozen_code": None}

        entry = main._line_diagnostics({"id": "1", "name": "T-MOBILE"})

        self.assertEqual(entry["reason_code"], "reg_unanswered")
        self.assertEqual(entry["registration"], "unregistered")
        self.assertEqual(entry["active_channels"], 0)
        self.assertEqual(entry["retry"], {"count": 2, "max": 3})
        # Monotonic readings mean nothing to a reader; their distance from now is the story.
        self.assertGreaterEqual(entry["failing_for_seconds"], 61.0)
        self.assertIsNone(entry["ok_for_seconds"])

    def test_summary_carries_no_subscriber_identifiers(self):
        main.hub.status_cache["1"] = {
            "state": "OK", "reason_code": "ok", "reason": "", "retry": {},
            "detail": {"registration": "registered", "msisdn": "+12025550123",
                       "imsi": "001010123456789"},
        }

        entry = operations.redact(main._line_diagnostics({"id": "1", "name": "T-MOBILE"}))

        self.assertNotIn("+12025550123", json.dumps(entry))
        self.assertNotIn("001010123456789", json.dumps(entry))
        self.assertEqual(entry["registration"], "registered")

    def test_a_line_the_sampler_has_not_reached_says_so(self):
        entry = main._line_diagnostics({"id": "9", "name": "NEW"})
        # An absent verdict must not read as a healthy one.
        self.assertNotIn("state", entry)
        self.assertIn("no status sampled", entry["note"])


if __name__ == "__main__":
    unittest.main()

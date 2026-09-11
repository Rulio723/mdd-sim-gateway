"""SIM matching for modems whose ModemManager SIM object exposes no ICCID.

Observed on the CMIOT ML307X: the module rejects the EF_ICCID read (+CRSM: 106,130),
so ``sim.properties.iccid`` stays empty while the IMSI is reported normally. Both the
sender and the receive scanner must then fall back to the IMSI the line has on record.
"""
import json
import types
import unittest

from control.app import cellular_sms


MODEM = "/org/freedesktop/ModemManager1/Modem/0"
SIM = "/org/freedesktop/ModemManager1/SIM/0"
IMSI = "001010000000000"
INSTANCES = [{"id": "1", "iccid": "8949000000000000001", "imsi": IMSI}]


def _result(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _runner(sim_properties, sms_paths=None, sms_objects=None):
    sms_paths = sms_paths or []
    sms_objects = sms_objects or {}

    def run(argv, **_kwargs):
        args = [a for a in argv if a != "--output-json"]
        if args[:2] == ["mmcli", "-L"]:
            return _result(stdout=MODEM + " [CMCC] ML307X\n")
        if args[:3] == ["mmcli", "-m", MODEM] and "--messaging-list-sms" in args:
            return _result(stdout=json.dumps({"modem.messaging.sms": sms_paths}))
        if args[:3] == ["mmcli", "-m", MODEM]:
            return _result(stdout=json.dumps({"modem": {"generic": {"sim": SIM}}}))
        if args[:3] == ["mmcli", "-i", SIM]:
            return _result(stdout=json.dumps({"sim": {"properties": sim_properties}}))
        if args[:3] == ["mmcli", "-s", args[2] if len(args) > 2 else ""]:
            return _result(stdout=json.dumps({"sms": sms_objects.get(args[2], {})}))
        return _result(returncode=1)

    return run


class FindModemFallbackTests(unittest.TestCase):
    def test_missing_modem_iccid_falls_back_to_imsi(self):
        runner = _runner({"iccid": "--", "imsi": IMSI})
        path, problem = cellular_sms._find_modem(
            INSTANCES[0]["iccid"], runner, 10.0, imsi=IMSI)
        self.assertEqual((path, problem), (MODEM, None))

    def test_a_present_but_different_iccid_never_matches_by_imsi(self):
        runner = _runner({"iccid": "8988000000000000009", "imsi": IMSI})
        path, problem = cellular_sms._find_modem(
            INSTANCES[0]["iccid"], runner, 10.0, imsi=IMSI)
        self.assertIsNone(path)
        self.assertIn("ICCID or IMSI", problem)

    def test_no_identifiers_reports_no_match(self):
        runner = _runner({"iccid": "--", "imsi": ""})
        path, problem = cellular_sms._find_modem(
            INSTANCES[0]["iccid"], runner, 10.0, imsi=IMSI)
        self.assertIsNone(path)


class ScannerFallbackTests(unittest.TestCase):
    def test_inbound_sms_is_attributed_through_the_imsi_fallback(self):
        sms_path = "/org/freedesktop/ModemManager1/SMS/7"
        runner = _runner(
            {"iccid": "--", "imsi": IMSI},
            sms_paths=[sms_path],
            sms_objects={sms_path: {
                "content": {"text": "hello", "number": "+8613800000000"},
                "properties": {"pdu-type": "deliver",
                               "timestamp": "2026-09-03T13:00:00+08:00"},
            }},
        )
        scanner = cellular_sms.Scanner(runner)
        records = scanner.discover(INSTANCES)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["instance"], "1")
        self.assertEqual(records[0]["direction"], "in")

    def test_topology_keeps_modems_without_iccid(self):
        scanner = cellular_sms.Scanner(_runner({"iccid": "--", "imsi": IMSI}))
        scanner._refresh_topology(0.0)
        self.assertEqual(scanner._topology, [(MODEM, "", IMSI)])


if __name__ == "__main__":
    unittest.main()

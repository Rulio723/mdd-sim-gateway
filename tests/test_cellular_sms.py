import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import cellular_sms, store

TEST_EPOCH = "a" * 64


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def is_create_call(args, modem_path=None):
    prefix = [
        "busctl", "--system", "call", "org.freedesktop.ModemManager1",
        modem_path, "org.freedesktop.ModemManager1.Modem.Messaging", "Create",
    ]
    return len(args) >= len(prefix) and args[:len(prefix)] == prefix


class MemoryTracker:
    """Small successful durable-tracker stand-in for command-focused tests."""

    def __init__(self, *, bind=True, reserve_error=None):
        self.bound = bind
        self.reserve_error = reserve_error
        self.calls = []

    def reserve_local_modem_sms(self, instance, iccid, content_hash, daemon_epoch,
                                recipient, body):
        self.calls.append(("reserve", instance, iccid, content_hash, daemon_epoch,
                           recipient, body))
        if self.reserve_error:
            raise self.reserve_error
        return 1

    def bind_local_modem_sms(self, reservation_id, daemon_epoch, modem_path, sms_path):
        self.calls.append(("bind", reservation_id, daemon_epoch, modem_path, sms_path))
        return self.bound

    def cancel_local_modem_sms(self, reservation_id):
        self.calls.append(("cancel", reservation_id))


class CellularSmsTests(unittest.TestCase):
    def setUp(self):
        with cellular_sms._local_sms_lock:
            cellular_sms._local_sms_paths.clear()

    def send(self, *args, **kwargs):
        kwargs.setdefault("local_sms_tracker", MemoryTracker())
        kwargs.setdefault("epoch_getter", lambda: TEST_EPOCH)
        return cellular_sms.send(*args, **kwargs)

    def test_received_sms_is_mapped_to_instance_by_case_insensitive_iccid(self):
        modem = "/org/freedesktop/ModemManager1/Modem/0"
        sim = "/org/freedesktop/ModemManager1/SIM/0"
        sms = "/org/freedesktop/ModemManager1/SMS/7"
        responses = {
            ("mmcli", "-L"): Result(modem),
            ("mmcli", "-m", modem, "--output-json"): Result(json.dumps({
                "modem": {"generic": {"sim": sim}}})),
            ("mmcli", "-i", sim, "--output-json"): Result(json.dumps({
                "sim": {"properties": {"iccid": "card-a"}}})),
            ("mmcli", "-m", modem, "--messaging-list-sms", "--output-json"): Result(
                json.dumps({"modem.messaging.sms": [sms]})),
            ("mmcli", "-s", sms, "--output-json"): Result(json.dumps({"sms": {
                "content": {"number": "+44123", "text": "hello"},
                "properties": {"pdu-type": "deliver", "timestamp": "2026-08-03T00:00:00+08:00"},
            }})),
        }

        def runner(args, **_kwargs):
            return responses.get(tuple(args), Result(returncode=1))

        rows = cellular_sms.discover([{"id": "3", "iccid": "CARD-A"}], runner=runner)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance"], "3")
        self.assertEqual(rows[0]["direction"], "in")
        self.assertEqual(rows[0]["transport"], "cellular")

    def test_unknown_sim_and_empty_body_are_ignored(self):
        self.assertEqual(cellular_sms.discover([], runner=lambda *_a, **_k: Result()), [])

    def test_scanner_caches_topology_and_sms_details_but_keeps_listing_live(self):
        modem = "/org/freedesktop/ModemManager1/Modem/0"
        sim = "/org/freedesktop/ModemManager1/SIM/0"
        sms = "/org/freedesktop/ModemManager1/SMS/7"
        calls = []
        responses = {
            ("mmcli", "-L"): Result(modem),
            ("mmcli", "-m", modem, "--output-json"): Result(json.dumps({
                "modem": {"generic": {"sim": sim}}})),
            ("mmcli", "-i", sim, "--output-json"): Result(json.dumps({
                "sim": {"properties": {"iccid": "card-a"}}})),
            ("mmcli", "-m", modem, "--messaging-list-sms", "--output-json"): Result(
                json.dumps({"modem.messaging.sms": [sms]})),
            ("mmcli", "-s", sms, "--output-json"): Result(json.dumps({"sms": {
                "content": {"number": "+44123", "text": "hello"},
                "properties": {"pdu-type": "deliver", "timestamp": "2026-08-03T00:00:00+08:00"},
            }})),
        }

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            return responses.get(tuple(args), Result(returncode=1))

        now = [10.0]
        scanner = cellular_sms.Scanner(runner, clock=lambda: now[0])
        first = scanner.discover([{"id": "3", "iccid": "card-a"}])
        now[0] += 5
        second = scanner.discover([{"id": "3", "iccid": "card-a"}])

        self.assertEqual(first, second)
        self.assertEqual(calls.count(("mmcli", "-L")), 1)
        self.assertEqual(calls.count(("mmcli", "-m", modem, "--output-json")), 1)
        self.assertEqual(calls.count(("mmcli", "-i", sim, "--output-json")), 1)
        self.assertEqual(calls.count(("mmcli", "-s", sms, "--output-json")), 1)
        self.assertEqual(calls.count(
            ("mmcli", "-m", modem, "--messaging-list-sms", "--output-json")), 2)

    def test_scanner_refreshes_stable_objects_after_ttl(self):
        modem = "/org/freedesktop/ModemManager1/Modem/0"
        sim = "/org/freedesktop/ModemManager1/SIM/0"
        calls = []

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args[:3] == ["mmcli", "-m", modem] and "--messaging-list-sms" not in args:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args[:3] == ["mmcli", "-i", sim]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-a"}}}))
            if "--messaging-list-sms" in args:
                return Result(json.dumps({"modem.messaging.sms": []}))
            return Result(returncode=1)

        now = [10.0]
        scanner = cellular_sms.Scanner(runner, topology_ttl=60, clock=lambda: now[0])
        scanner.discover([{"id": "3", "iccid": "card-a"}])
        now[0] = 71.0
        scanner.discover([{"id": "3", "iccid": "card-a"}])
        self.assertEqual(calls.count(("mmcli", "-L")), 2)

    def test_scanner_never_prunes_after_failed_or_malformed_listing(self):
        modem = "/org/freedesktop/ModemManager1/Modem/0"
        sim = "/org/freedesktop/ModemManager1/SIM/0"

        class Tracker:
            def __init__(self):
                self.prunes = []

            def prune_local_modem_sms(self, *args):
                self.prunes.append(args)

        listing = [{}]

        def runner(args, **_kwargs):
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-a"}}}))
            if args == ["mmcli", "-m", modem, "--messaging-list-sms", "--output-json"]:
                return Result(json.dumps(listing[0]))
            return Result(returncode=1)

        tracker = Tracker()
        scanner = cellular_sms.Scanner(
            runner, local_sms_tracker=tracker, epoch_getter=lambda: TEST_EPOCH)
        scanner.discover([{"id": "3", "iccid": "card-a"}])
        listing[0] = {"modem.messaging.sms": ["not-an-object-path"]}
        scanner.discover([{"id": "3", "iccid": "card-a"}])
        self.assertEqual(tracker.prunes, [])

    def test_send_matches_case_insensitive_iccid_and_passes_typed_dbus_text(self):
        modem = "/org/freedesktop/ModemManager1/Modem/2"
        sim = "/org/freedesktop/ModemManager1/SIM/2"
        sms = "/org/freedesktop/ModemManager1/SMS/41"
        body = "BAL, it's safe; $(touch never)"
        calls, captured = [], {}

        def runner(args, **kwargs):
            calls.append(tuple(args))
            self.assertIsInstance(args, list)
            self.assertNotIn("shell", kwargs)
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-b"}}}))
            if args == ["mmcli", "-m", modem, "--messaging-status", "--output-json"]:
                return Result(json.dumps({"modem": {"messaging": {
                    "supported-storages": ["me"]}}}))
            if is_create_call(args, modem):
                captured["body"] = args[-1]
                captured["properties"] = args[7:-1]
                return Result(f'o "{sms}"\n')
            if args == ["mmcli", "-s", sms, "--send", "--output-json"]:
                return Result("{}")
            return Result(returncode=1)

        result = self.send(
            [{"id": "3", "iccid": "CARD-B"}], "3", "6700", body, runner=runner)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["modem_path"], modem)
        self.assertEqual(result["sms_path"], sms)
        self.assertEqual(result["transport"], "cellular")
        self.assertEqual(captured["body"], body)
        self.assertEqual(captured["properties"],
                         ["a{sv}", "2", "number", "s", "6700", "text", "s"])
        self.assertFalse(any(any(arg.startswith("--messaging-create-sms") for arg in call)
                             for call in calls))

    def test_scanner_suppresses_sms_object_created_by_send(self):
        modem = "/org/freedesktop/ModemManager1/Modem/3"
        sim = "/org/freedesktop/ModemManager1/SIM/3"
        sms = "/org/freedesktop/ModemManager1/SMS/42"
        calls = []

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-c"}}}))
            if args == ["mmcli", "-m", modem, "--messaging-status", "--output-json"]:
                return Result("{}")
            if is_create_call(args, modem):
                return Result(f'o "{sms}"\n')
            if args == ["mmcli", "-s", sms, "--send", "--output-json"]:
                return Result("{}")
            if args == ["mmcli", "-m", modem, "--messaging-list-sms", "--output-json"]:
                return Result(json.dumps({"modem.messaging.sms": [sms]}))
            if args == ["mmcli", "-s", sms, "--output-json"]:
                return Result(json.dumps({"sms": {
                    "content": {"number": "888", "text": "BAL"},
                    "properties": {"pdu-type": "submit"},
                }}))
            return Result(returncode=1)

        instances = [{"id": "4", "iccid": "card-c"}]
        self.assertTrue(self.send(instances, "4", "888", "BAL", runner=runner)["ok"])
        self.assertEqual(cellular_sms.Scanner(runner).discover(instances), [])
        self.assertNotIn(("mmcli", "-s", sms, "--output-json"), calls)

    def test_invalid_created_sms_path_is_never_sent(self):
        modem = "/org/freedesktop/ModemManager1/Modem/4"
        sim = "/org/freedesktop/ModemManager1/SIM/4"
        calls = []

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-d"}}}))
            if "--messaging-status" in args:
                return Result("{}")
            if is_create_call(args, modem):
                return Result('o "/tmp/not-an-sms"\n')
            return Result(returncode=1)

        result = self.send(
            [{"id": "5", "iccid": "card-d"}], "5", "+44123", "hello", runner=runner)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "create")
        self.assertIsNone(result["sms_path"])
        self.assertFalse(any("--send" in call for call in calls))

    def test_send_timeout_is_unknown_and_not_retried(self):
        modem = "/org/freedesktop/ModemManager1/Modem/5"
        sim = "/org/freedesktop/ModemManager1/SIM/5"
        sms = "/org/freedesktop/ModemManager1/SMS/43"
        send_calls = []

        def runner(args, **kwargs):
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-e"}}}))
            if "--messaging-status" in args:
                return Result("{}")
            if is_create_call(args, modem):
                return Result(f'o "{sms}"\n')
            if "--send" in args:
                send_calls.append(tuple(args))
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return Result(returncode=1)

        result = self.send(
            [{"id": "6", "iccid": "card-e"}], "6", "6700", "DATA", runner=runner)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["sms_path"], sms)
        self.assertEqual(len(send_calls), 1)

    def test_lookup_timeout_and_invalid_recipient_are_structured(self):
        def timeout_runner(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        timeout = self.send(
            [{"id": "7", "iccid": "card-f"}], "7", "6700", "DATA",
            runner=timeout_runner)
        self.assertFalse(timeout["ok"])
        self.assertEqual(timeout["status"], "unavailable")
        self.assertEqual(timeout["stage"], "lookup")

        called = []
        invalid = self.send(
            [{"id": "7", "iccid": "card-f"}], "7", "6700; reboot", "DATA",
            runner=lambda *args, **kwargs: called.append((args, kwargs)))
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["stage"], "validate")
        self.assertEqual(called, [])

    def test_send_is_refused_before_create_when_durable_reservation_fails(self):
        modem = "/org/freedesktop/ModemManager1/Modem/6"
        sim = "/org/freedesktop/ModemManager1/SIM/6"
        calls = []

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-g"}}}))
            if "--messaging-status" in args:
                return Result("{}")
            return Result(returncode=1)

        tracker = MemoryTracker(reserve_error=OSError("read only"))
        result = cellular_sms.send(
            [{"id": "8", "iccid": "card-g"}], "8", "6700", "DATA", runner=runner,
            local_sms_tracker=tracker, epoch_getter=lambda: TEST_EPOCH)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "track")
        self.assertFalse(any(is_create_call(list(call), modem) for call in calls))
        self.assertFalse(any("--send" in call for call in calls))

    def test_send_is_refused_when_created_path_cannot_be_durably_bound(self):
        modem = "/org/freedesktop/ModemManager1/Modem/7"
        sim = "/org/freedesktop/ModemManager1/SIM/7"
        sms = "/org/freedesktop/ModemManager1/SMS/47"
        calls = []

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-h"}}}))
            if "--messaging-status" in args:
                return Result("{}")
            if is_create_call(args, modem):
                return Result(f'o "{sms}"\n')
            return Result(returncode=1)

        result = cellular_sms.send(
            [{"id": "9", "iccid": "card-h"}], "9", "888", "BAL", runner=runner,
            local_sms_tracker=MemoryTracker(bind=False),
            epoch_getter=lambda: TEST_EPOCH)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "track")
        self.assertEqual(result["sms_path"], sms)
        self.assertFalse(any("--send" in call for call in calls))

    def test_create_timeout_keeps_the_reservation_for_later_scanner_claim(self):
        modem = "/org/freedesktop/ModemManager1/Modem/9"
        sim = "/org/freedesktop/ModemManager1/SIM/9"
        tracker = MemoryTracker()

        def runner(args, **kwargs):
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-j"}}}))
            if "--messaging-status" in args:
                return Result("{}")
            if is_create_call(args, modem):
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return Result(returncode=1)

        result = cellular_sms.send(
            [{"id": "11", "iccid": "card-j"}], "11", "6700", "DATA",
            runner=runner, local_sms_tracker=tracker,
            epoch_getter=lambda: TEST_EPOCH)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "create")
        self.assertIsNotNone(result.get("_reservation_id"))
        self.assertFalse(any(call[0] == "cancel" for call in tracker.calls))

    def test_pending_durable_reservation_is_claimed_and_keeps_its_history_row(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "mdd-sim-gateway.sqlite"
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(db_path),
                                PREVIOUS_DB_PATH=str(root / "vowifi.sqlite")):
                store.init()
                digest = cellular_sms._content_hash("6700", "DATA")
                reservation = store.reserve_local_modem_sms(
                    "11", "card-j", digest, TEST_EPOCH, "6700", "DATA")
                self.assertTrue(store.is_local_modem_sms(
                    TEST_EPOCH, "card-j", "/org/freedesktop/ModemManager1/Modem/9",
                    "/org/freedesktop/ModemManager1/SMS/49", digest))
                message = store.local_modem_sms_message(reservation)
                self.assertEqual(message["status"], "pending")
                self.assertEqual(message["transport"], "cellular")
                with sqlite3.connect(db_path) as connection:
                    bound = connection.execute(
                        "SELECT sms_path FROM local_modem_sms WHERE id=?", (reservation,)
                    ).fetchone()[0]
                self.assertEqual(bound, "/org/freedesktop/ModemManager1/SMS/49")
                store.init()  # simulated process restart
                interrupted = store.local_modem_sms_message(reservation)
                self.assertEqual(interrupted["status"], "unknown")
                self.assertIn("delivery is unknown", interrupted["error"])

    def test_cancelled_reservation_keeps_history_but_cannot_claim_an_sms_object(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "mdd-sim-gateway.sqlite"
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(db_path),
                                PREVIOUS_DB_PATH=str(root / "vowifi.sqlite")):
                store.init()
                digest = cellular_sms._content_hash("888", "BAL")
                reservation = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                store.cancel_local_modem_sms(reservation)

                message = store.local_modem_sms_message(reservation)
                self.assertIsNotNone(message)
                self.assertEqual(message["peer"], "888")
                self.assertFalse(store.is_local_modem_sms(
                    TEST_EPOCH, "card-g", "/org/freedesktop/ModemManager1/Modem/7",
                    "/org/freedesktop/ModemManager1/SMS/47", digest))

    def test_old_or_timestamp_mismatched_reservation_cannot_claim_external_sms(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "mdd-sim-gateway.sqlite"
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(db_path),
                                PREVIOUS_DB_PATH=str(root / "vowifi.sqlite")):
                store.init()
                digest = cellular_sms._content_hash("888", "BAL")
                reservation = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                with sqlite3.connect(db_path) as connection:
                    created_ts = connection.execute(
                        "SELECT created_ts FROM local_modem_sms WHERE id=?", (reservation,)
                    ).fetchone()[0]

                # A real object timestamp that is far from the reservation cannot match.
                self.assertFalse(store.is_local_modem_sms(
                    TEST_EPOCH, "card-g", "/org/freedesktop/ModemManager1/Modem/7",
                    "/org/freedesktop/ModemManager1/SMS/47", digest,
                    created_ts + store.LOCAL_MODEM_SMS_CLAIM_SECONDS + 1))

                # With no object timestamp, an old create timeout is no longer eligible.
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "UPDATE local_modem_sms SET created_ts=? WHERE id=?",
                        (created_ts - store.LOCAL_MODEM_SMS_CLAIM_SECONDS - 1, reservation))
                self.assertFalse(store.is_local_modem_sms(
                    TEST_EPOCH, "card-g", "/org/freedesktop/ModemManager1/Modem/7",
                    "/org/freedesktop/ModemManager1/SMS/47", digest))

    def test_scanner_claim_then_sender_bind_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "mdd-sim-gateway.sqlite"
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(db_path),
                                PREVIOUS_DB_PATH=str(root / "vowifi.sqlite")):
                store.init()
                modem = "/org/freedesktop/ModemManager1/Modem/7"
                sms = "/org/freedesktop/ModemManager1/SMS/47"
                digest = cellular_sms._content_hash("888", "BAL")
                reservation = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                self.assertTrue(store.is_local_modem_sms(
                    TEST_EPOCH, "card-g", modem, sms, digest))
                self.assertTrue(store.bind_local_modem_sms(
                    reservation, TEST_EPOCH, modem, sms))
                self.assertFalse(store.bind_local_modem_sms(
                    reservation, TEST_EPOCH, modem,
                    "/org/freedesktop/ModemManager1/SMS/99"))

    def test_prune_removes_only_stale_nonlive_markers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "mdd-sim-gateway.sqlite"
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(db_path),
                                PREVIOUS_DB_PATH=str(root / "vowifi.sqlite")):
                store.init()
                digest = cellular_sms._content_hash("888", "BAL")
                live = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                stale = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                cancelled = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                unbound = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                other_modem = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                self.assertTrue(store.bind_local_modem_sms(
                    live, TEST_EPOCH, "/org/freedesktop/ModemManager1/Modem/7",
                    "/org/freedesktop/ModemManager1/SMS/1"))
                self.assertTrue(store.bind_local_modem_sms(
                    stale, TEST_EPOCH, "/org/freedesktop/ModemManager1/Modem/7",
                    "/org/freedesktop/ModemManager1/SMS/2"))
                self.assertTrue(store.bind_local_modem_sms(
                    other_modem, TEST_EPOCH, "/org/freedesktop/ModemManager1/Modem/8",
                    "/org/freedesktop/ModemManager1/SMS/3"))
                store.cancel_local_modem_sms(cancelled)
                fresh_cancelled = store.reserve_local_modem_sms(
                    "7", "card-g", digest, TEST_EPOCH, "888", "BAL")
                store.cancel_local_modem_sms(fresh_cancelled)
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "UPDATE local_modem_sms SET created_ts=created_ts-? WHERE id<>?",
                        (store.LOCAL_MODEM_SMS_RETENTION_SECONDS + 1, fresh_cancelled))
                    connection.execute(
                        "UPDATE local_modem_sms SET bound_ts=bound_ts-? "
                        "WHERE bound_ts IS NOT NULL",
                        (store.LOCAL_MODEM_SMS_RETENTION_SECONDS + 1,))

                removed = store.prune_local_modem_sms(
                    TEST_EPOCH, "card-g", "/org/freedesktop/ModemManager1/Modem/7",
                    {"/org/freedesktop/ModemManager1/SMS/1"})
                self.assertEqual(removed, 3)
                with sqlite3.connect(db_path) as connection:
                    remaining = connection.execute(
                        "SELECT id FROM local_modem_sms ORDER BY id").fetchall()
                self.assertEqual(remaining, [(live,), (other_modem,), (fresh_cancelled,)])

    def test_durable_marker_survives_scanner_restart_and_allows_path_reuse(self):
        modem = "/org/freedesktop/ModemManager1/Modem/8"
        sim = "/org/freedesktop/ModemManager1/SIM/8"
        sms = "/org/freedesktop/ModemManager1/SMS/48"
        current_text = ["BAL"]
        current_epoch = [TEST_EPOCH]

        def runner(args, **_kwargs):
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args == ["mmcli", "-m", modem, "--output-json"]:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args == ["mmcli", "-i", sim, "--output-json"]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-i"}}}))
            if "--messaging-status" in args:
                return Result("{}")
            if is_create_call(args, modem):
                return Result(f'o "{sms}"\n')
            if args == ["mmcli", "-s", sms, "--send", "--output-json"]:
                return Result("{}")
            if args == ["mmcli", "-m", modem, "--messaging-list-sms", "--output-json"]:
                return Result(json.dumps({"modem.messaging.sms": [sms]}))
            if args == ["mmcli", "-s", sms, "--output-json"]:
                return Result(json.dumps({"sms": {
                    "content": {"number": "6700", "text": current_text[0]},
                    "properties": {"pdu-type": "submit"},
                }}))
            return Result(returncode=1)

        instances = [{"id": "10", "iccid": "card-i"}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(store, DATA_DIR=str(root),
                                DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                PREVIOUS_DB_PATH=str(root / "vowifi.sqlite")):
                store.init()
                result = cellular_sms.send(
                    instances, "10", "6700", "BAL", runner=runner,
                    local_sms_tracker=store, epoch_getter=lambda: current_epoch[0])
                self.assertTrue(result["ok"])

                # Clear every process-local hint and construct a new scanner: only SQLite can
                # identify the still-live ModemManager submit object after this simulated restart.
                with cellular_sms._local_sms_lock:
                    cellular_sms._local_sms_paths.clear()
                restarted = cellular_sms.Scanner(
                    runner, local_sms_tracker=store, epoch_getter=lambda: current_epoch[0])
                self.assertEqual(restarted.discover(instances), [])

                # Reusing the same numeric object path for different content remains importable.
                with cellular_sms._local_sms_lock:
                    cellular_sms._local_sms_paths.clear()
                current_text[0] = "MANUAL"
                reused = cellular_sms.Scanner(
                    runner, local_sms_tracker=store,
                    epoch_getter=lambda: current_epoch[0]).discover(instances)
                self.assertEqual(len(reused), 1)
                self.assertEqual(reused[0]["body"], "MANUAL")
                self.assertEqual(reused[0]["direction"], "out")

                # The same external object identity after a daemon restart receives a distinct
                # import fingerprint even when ModemManager supplies no timestamp.
                current_epoch[0] = "b" * 64
                same_after_restart = cellular_sms.Scanner(
                    runner, local_sms_tracker=store,
                    epoch_getter=lambda: current_epoch[0]).discover(instances)
                self.assertEqual(len(same_after_restart), 1)
                self.assertNotEqual(
                    reused[0]["fingerprint"], same_after_restart[0]["fingerprint"])

                # A new ModemManager D-Bus owner may reuse both the numeric path and content.
                # The old marker must not hide that new object.
                current_text[0] = "BAL"
                after_daemon_restart = cellular_sms.Scanner(
                    runner, local_sms_tracker=store,
                    epoch_getter=lambda: current_epoch[0]).discover(instances)
                self.assertEqual(len(after_daemon_restart), 1)
                self.assertEqual(after_daemon_restart[0]["body"], "BAL")

    def test_modemmanager_epoch_combines_boot_and_unique_dbus_owner(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return Result('s ":1.14"\n')

        first = cellular_sms._modemmanager_epoch(
            runner, boot_id_reader=lambda: "12345678-1234-1234-1234-123456789abc")
        second = cellular_sms._modemmanager_epoch(
            lambda *_a, **_k: Result('s ":1.15"\n'),
            boot_id_reader=lambda: "12345678-1234-1234-1234-123456789abc")

        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)
        self.assertEqual(calls[0][0][0], "busctl")
        self.assertEqual(calls[0][1]["timeout"], 3)


if __name__ == "__main__":
    unittest.main()

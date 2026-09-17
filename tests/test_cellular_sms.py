import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import cellular_sms, store

MODEM = "/org/freedesktop/ModemManager1/Modem/0"
SIM = "/org/freedesktop/ModemManager1/SIM/0"
LINE = [{"id": "3", "iccid": "card-a"}]


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def sms_object(number="+447700900123", text="hello", *, pdu_type="deliver", state="received",
               timestamp="2026-09-12T09:00:00+08:00", storage="me", data="--"):
    return {"content": {"number": number, "text": text, "data": data},
            "properties": {"pdu-type": pdu_type, "state": state, "timestamp": timestamp,
                           "storage": storage}}


def wap_push_sms() -> dict:
    """A carrier MMS notification as ModemManager reports it: no text, binary WSP payload."""
    payload = b"\x23\x06\x24" + b"application/vnd.wap.mms-message" + b"\x00"
    return sms_object("+447700900999", "--", data=" ".join(f"{b:02X}" for b in payload))


class FakeModemManager:
    """Just enough of mmcli/busctl: one modem, one SIM, a mutable set of SMS objects."""

    def __init__(self, iccid="card-a", objects=None, *, cpms=None, delete_ok=True):
        self.iccid = iccid
        self.objects = dict(objects or {})
        self.cpms = cpms
        self.delete_ok = delete_ok
        self.calls = []
        self.next_id = 100
        self.send_hook = None

    def path(self, n):
        return f"/org/freedesktop/ModemManager1/SMS/{n}"

    def add(self, n, detail):
        self.objects[self.path(n)] = detail
        return self.path(n)

    def deletes(self):
        return [c[3].split("=", 1)[1] for c in self.calls
                if len(c) == 4 and c[3].startswith("--messaging-delete-sms=")]

    def __call__(self, args, **kwargs):
        self.calls.append(tuple(args))
        assert isinstance(args, list) and "shell" not in kwargs
        if args == ["mmcli", "-L"]:
            return Result(MODEM)
        if args == ["mmcli", "-m", MODEM, "--output-json"]:
            return Result(json.dumps({"modem": {"generic": {"sim": SIM}}}))
        if args == ["mmcli", "-i", SIM, "--output-json"]:
            return Result(json.dumps({"sim": {"properties": {"iccid": self.iccid}}}))
        if args == ["mmcli", "-m", MODEM, "--messaging-list-sms", "--output-json"]:
            return Result(json.dumps({"modem.messaging.sms": sorted(self.objects)}))
        if args == ["mmcli", "-m", MODEM, "--messaging-status", "--output-json"]:
            return Result("{}")
        if args == ["mmcli", "-m", MODEM, "--command=AT+CPMS?"]:
            if self.cpms is None:
                return Result(returncode=1, stderr="command not allowed")
            used, total = self.cpms
            return Result(f"response: '+CPMS: \"ME\",{used},{total},\"ME\",{used},{total}'")
        if len(args) == 4 and args[:3] == ["mmcli", "-m", MODEM] and \
                args[3].startswith("--messaging-delete-sms="):
            path = args[3].split("=", 1)[1]
            if not self.delete_ok or path not in self.objects:
                return Result(returncode=1)
            del self.objects[path]
            if self.cpms:
                self.cpms = (self.cpms[0] - 1, self.cpms[1])
            return Result()
        if args[:3] == ["busctl", "--system", "call"] and args[6] == "Create":
            self.next_id += 1
            path = self.add(self.next_id, sms_object(args[11], args[14], pdu_type="submit",
                                                     state="stored", timestamp="--"))
            return Result(f'o "{path}"\n')
        if len(args) == 5 and args[1] == "-s" and args[3] == "--send":
            if self.send_hook:
                return self.send_hook(args, kwargs)
            self.objects[args[2]]["properties"]["state"] = "sent"
            return Result("{}")
        if len(args) == 4 and args[1] == "-s" and args[3] == "--output-json":
            if args[2] in self.objects:
                return Result(json.dumps({"sms": self.objects[args[2]]}))
            return Result(returncode=1)
        return Result(returncode=1)


class MemoryTracker:
    def __init__(self, *, begin_error=None, bind=True, own=False):
        self.begin_error = begin_error
        self.bind = bind
        self.own = own
        self.calls = []

    def begin_local_modem_sms(self, instance, recipient, body):
        self.calls.append(("begin", instance, recipient, body))
        if self.begin_error:
            raise self.begin_error
        return 9

    def bind_local_modem_sms(self, message_id, modem_path, sms_path):
        self.calls.append(("bind", message_id, modem_path, sms_path))
        return self.bind

    def owns_local_modem_sms(self, instance, modem_path, sms_path, peer, body):
        self.calls.append(("owns", instance, sms_path, peer, body))
        return self.own


def scanner(mm, **kwargs):
    kwargs.setdefault("epoch_getter", lambda: "epoch-1")
    return cellular_sms.Scanner(mm, **kwargs)


class Ingest:
    def __init__(self, error=None):
        self.records = []
        self.error = error

    def __call__(self, record):
        if self.error:
            raise self.error
        self.records.append(record)
        return {"id": len(self.records), **record}


class DiscoverTests(unittest.TestCase):
    def test_received_sms_is_mapped_to_instance_by_case_insensitive_iccid(self):
        mm = FakeModemManager("CARD-A", cpms=(1, 20))
        mm.add(7, sms_object(text="hello"))
        rows = cellular_sms.discover(LINE, runner=mm)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance"], "3")
        self.assertEqual(rows[0]["peer"], "+447700900123")
        self.assertEqual(rows[0]["direction"], "in")
        self.assertEqual(rows[0]["ts"], 1789174800)
        self.assertEqual(mm.deletes(), [], "a diagnostic read never deletes")

    def test_unknown_sim_is_ignored(self):
        mm = FakeModemManager("card-z")
        mm.add(7, sms_object())
        self.assertEqual(cellular_sms.discover(LINE, runner=mm), [])

    def test_scanner_caches_topology_and_details_but_keeps_listing_live(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        now = [10.0]
        s = scanner(mm, clock=lambda: now[0])
        first = s.discover(LINE)
        now[0] += 5
        second = s.discover(LINE)
        self.assertEqual(first, second)
        self.assertEqual(mm.calls.count(("mmcli", "-L")), 1)
        self.assertEqual(mm.calls.count(("mmcli", "-s", path, "--output-json")), 1)
        self.assertEqual(mm.calls.count(
            ("mmcli", "-m", MODEM, "--messaging-list-sms", "--output-json")), 2)

    def test_topology_is_refreshed_after_ttl(self):
        mm = FakeModemManager()
        now = [10.0]
        s = scanner(mm, topology_ttl=60, clock=lambda: now[0])
        s.discover(LINE)
        now[0] = 71.0
        s.discover(LINE)
        self.assertEqual(mm.calls.count(("mmcli", "-L")), 2)

    def test_incomplete_multipart_sms_is_never_imported_as_its_placeholder(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="--", state="receiving"))
        now = [10.0]
        s = scanner(mm, clock=lambda: now[0])
        self.assertEqual(s.discover(LINE), [])
        # A ModemManager build that reports no state must not import the placeholder either.
        mm.objects[path]["properties"].pop("state")
        now[0] += 1
        self.assertEqual(s.discover(LINE), [])
        mm.objects[path] = sms_object(text="part one and part two")
        now[0] += 100
        self.assertEqual([r["body"] for r in s.discover(LINE)], ["part one and part two"])

    def test_sms_binary_payload_decoding_tolerates_mmcli_renderings(self):
        self.assertEqual(cellular_sms._sms_data("23 06 24"), b"\x23\x06\x24")
        self.assertEqual(cellular_sms._sms_data("230624"), b"\x23\x06\x24")
        self.assertEqual(cellular_sms._sms_data([35, 6, 36]), b"\x23\x06\x24")
        self.assertEqual(cellular_sms._sms_data("--"), b"")
        self.assertEqual(cellular_sms._sms_data(None), b"")
        self.assertEqual(cellular_sms._sms_data("23 06 2"), b"")


class StoragePolicyTests(unittest.TestCase):
    def test_policy_resolution_prefers_settings_then_environment(self):
        env = {cellular_sms.STORAGE_POLICY_ENV: "when-full"}
        self.assertEqual(cellular_sms.storage_policy({}, env), "when_full")
        self.assertEqual(cellular_sms.storage_policy({"cellular_sms_storage": "keep"}, env),
                         "keep")
        self.assertEqual(cellular_sms.storage_policy({"cellular_sms_storage": "bogus"}, {}),
                         "delete")
        self.assertEqual(cellular_sms.storage_limit({cellular_sms.STORAGE_LIMIT_ENV: "2"}),
                         cellular_sms._DEFAULT_STORAGE_LIMIT)
        self.assertEqual(cellular_sms.storage_limit({cellular_sms.STORAGE_LIMIT_ENV: "50"}), 50)

    def test_delete_policy_removes_object_only_after_it_is_stored(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        failing = Ingest(error=RuntimeError("database is locked"))
        s = scanner(mm)
        self.assertEqual(s.poll(LINE, failing, policy="delete"), [])
        self.assertEqual(mm.deletes(), [], "an object whose message is not stored stays")

        ingest = Ingest()
        stored = s.poll(LINE, ingest, policy="delete")
        self.assertEqual([r["body"] for r in stored], ["hello"])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(mm.objects, {})

    def test_duplicate_is_still_deleted(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        s = scanner(mm)
        self.assertEqual(s.poll(LINE, lambda record: None, policy="delete"), [])
        self.assertEqual(mm.deletes(), [path])

    def test_keep_policy_never_deletes_and_ingests_once(self):
        mm = FakeModemManager()
        mm.add(7, sms_object())
        ingest = Ingest()
        now = [0.0]
        s = scanner(mm, clock=lambda: now[0])
        for _ in range(3):
            s.poll(LINE, ingest, policy="keep")
            now[0] += 100
        self.assertEqual(len(ingest.records), 1)
        self.assertEqual(mm.deletes(), [])

    def test_delete_verifies_the_path_still_holds_the_stored_message(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="first"))
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="keep")
        # ModemManager restarted under the same D-Bus name and reused the number.
        mm.objects[path] = sms_object(text="second, not imported yet")
        s._delete((MODEM, path), s._settled[(MODEM, path)])
        self.assertEqual(mm.deletes(), [])
        self.assertIn(path, mm.objects)

    def test_when_full_deletes_oldest_imported_objects_from_modem_capacity(self):
        mm = FakeModemManager(cpms=(21, 23))
        paths = [mm.add(n, sms_object(text=f"m{n}", timestamp=f"2026-09-0{n}T09:00:00+08:00"))
                 for n in range(1, 6)]
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="when_full")
        # 21 used of 23 while 3 slots stay free: one deletion brings it to 20.
        self.assertEqual(mm.deletes(), [paths[0]])

    def test_when_full_without_command_channel_uses_the_configured_limit(self):
        mm = FakeModemManager(cpms=None)
        paths = [mm.add(n, sms_object(text=f"m{n}", timestamp=f"2026-09-0{n}T09:00:00+08:00"))
                 for n in range(1, 8)]
        s = scanner(mm, environ={cellular_sms.STORAGE_LIMIT_ENV: "8"})
        s.poll(LINE, Ingest(), policy="when_full")
        self.assertEqual(mm.deletes(), paths[:2])

    def test_undeletable_object_stops_retrying_after_its_attempt_budget(self):
        mm = FakeModemManager(delete_ok=False)
        mm.add(7, sms_object())
        now = [0.0]
        s = scanner(mm, clock=lambda: now[0])
        for _ in range(6):
            s.poll(LINE, Ingest(), policy="delete")
            now[0] += 5
        self.assertEqual(len(mm.deletes()), cellular_sms._DELETE_ATTEMPTS)


class LegacyFingerprintTests(unittest.TestCase):
    def test_inbound_record_carries_the_1_9_marker_fingerprint(self):
        import hashlib
        mm = FakeModemManager("CARD-A")
        path = mm.add(7, sms_object(text="hello", timestamp="2026-09-12T09:00:00+08:00"))
        record = scanner(mm).discover(LINE)[0]
        expected = hashlib.sha256("\0".join(
            ("card-a", path, "in", "+447700900123", "hello", "2026-09-12T09:00:00+08:00")
        ).encode()).hexdigest()
        self.assertEqual(record["legacy_fingerprint"], expected)


class BinaryObjectTests(unittest.TestCase):
    def test_binary_payload_is_handed_to_ingest_and_then_removed(self):
        mm = FakeModemManager()
        path = mm.add(7, wap_push_sms())
        ingest = Ingest()
        stored = scanner(mm).poll(LINE, ingest)
        self.assertEqual(len(stored), 1)
        self.assertEqual(ingest.records[0]["body"], "")
        self.assertTrue(ingest.records[0]["data"].startswith(b"\x23\x06\x24"))
        self.assertEqual(mm.deletes(), [path])

    def test_object_with_neither_text_nor_payload_is_left_alone(self):
        mm = FakeModemManager()
        mm.add(7, sms_object(text="--", data="--"))
        ingest = Ingest()
        self.assertEqual(scanner(mm).poll(LINE, ingest), [])
        self.assertEqual(ingest.records, [])
        self.assertEqual(mm.deletes(), [])


class SendTests(unittest.TestCase):
    def send(self, mm, recipient="6700", text="BAL", **kwargs):
        kwargs.setdefault("local_sms_tracker", MemoryTracker())
        return cellular_sms.send(LINE, "3", recipient, text, runner=mm, **kwargs)

    def test_send_passes_typed_dbus_text_binds_and_removes_the_object(self):
        mm = FakeModemManager("CARD-A")
        tracker = MemoryTracker()
        body = "BAL, it's safe; $(touch never)"
        result = self.send(mm, text=body, local_sms_tracker=tracker)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["message_id"], 9)
        create = next(c for c in mm.calls if c[:3] == ("busctl", "--system", "call"))
        self.assertEqual(list(create[7:]),
                         ["a{sv}", "2", "number", "s", "6700", "text", "s", body])
        self.assertEqual(tracker.calls[0], ("begin", "3", "6700", body))
        self.assertEqual(tracker.calls[1], ("bind", 9, MODEM, result["sms_path"]))
        self.assertEqual(mm.deletes(), [result["sms_path"]])
        self.assertEqual(mm.objects, {})

    def test_history_row_is_written_before_create_and_bound_under_the_scan_lock(self):
        mm = FakeModemManager()
        order = []

        class Tracker(MemoryTracker):
            def begin_local_modem_sms(self, *args):
                order.append(("begin", len(mm.calls)))
                return 9

            def bind_local_modem_sms(self, *args):
                self_held = cellular_sms._local_sms_lock._is_owned()
                order.append(("bind", self_held))
                return True

        self.send(mm, local_sms_tracker=Tracker())
        self.assertEqual(order[0][0], "begin")
        self.assertEqual(order[1], ("bind", True))

    def test_send_is_refused_without_tracking(self):
        mm = FakeModemManager()
        refused = self.send(mm, local_sms_tracker=MemoryTracker(begin_error=OSError("disk")))
        self.assertEqual(refused["stage"], "track")
        self.assertFalse(any(c[:3] == ("busctl", "--system", "call") for c in mm.calls))
        self.assertEqual(self.send(mm, local_sms_tracker=None)["stage"], "track")

    def test_unbindable_object_is_deleted_and_never_sent(self):
        mm = FakeModemManager()
        result = self.send(mm, local_sms_tracker=MemoryTracker(bind=False))
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "track")
        self.assertEqual(mm.objects, {})
        self.assertFalse(any("--send" in c for c in mm.calls))

    def test_invalid_created_sms_path_is_never_sent(self):
        mm = FakeModemManager()

        def runner(args, **kwargs):
            if args[:3] == ["busctl", "--system", "call"]:
                return Result('o "/tmp/not-an-sms"\n')
            return mm(args, **kwargs)

        result = cellular_sms.send(LINE, "3", "+447700900123", "hello", runner=runner,
                                   local_sms_tracker=MemoryTracker())
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "create")
        self.assertIsNone(result["sms_path"])

    def test_send_timeout_is_unknown_not_retried_and_object_left_for_the_scanner(self):
        mm = FakeModemManager()
        sends = []

        def hang(args, kwargs):
            sends.append(args)
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        mm.send_hook = hang
        result = self.send(mm)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["uncertain"])
        self.assertEqual(len(sends), 1)
        self.assertEqual(mm.deletes(), [])

    def test_rejected_send_is_failed_and_object_removed(self):
        mm = FakeModemManager()
        mm.send_hook = lambda args, kwargs: Result(returncode=1, stderr="CMS ERROR: 500")
        result = self.send(mm)
        self.assertEqual(result["status"], "failed")
        self.assertIn("CMS ERROR", result["error"])
        self.assertEqual(mm.objects, {})

    def test_lookup_timeout_and_invalid_recipient_are_structured(self):
        def timeout_runner(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        timeout = cellular_sms.send(LINE, "3", "6700", "DATA", runner=timeout_runner,
                                    local_sms_tracker=MemoryTracker())
        self.assertEqual((timeout["status"], timeout["stage"]), ("unavailable", "lookup"))
        called = []
        invalid = cellular_sms.send(LINE, "3", "6700; reboot", "DATA",
                                    runner=lambda *a, **k: called.append(a),
                                    local_sms_tracker=MemoryTracker())
        self.assertEqual(invalid["stage"], "validate")
        self.assertEqual(called, [])


class StoreBackedTests(unittest.TestCase):
    """The scanner and sender against the real SQLite store."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(store, DATA_DIR=str(root),
                                    DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        store.init()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    @staticmethod
    def ingest(record):
        return store.ingest_message(record["instance"], record["direction"], record["peer"],
                                    record["body"], transport="cellular",
                                    sent_ts=record["ts"] or None)

    def messages(self):
        with store._conn() as c:
            return [(r["direction"], r["body"], r["status"]) for r in
                    c.execute("SELECT direction,body,status FROM messages ORDER BY id")]

    def test_renumbered_objects_after_modemmanager_restart_are_not_imported_again(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="kept on modem"))
        s = scanner(mm)
        self.assertEqual(len(s.poll(LINE, self.ingest, policy="keep")), 1)
        mm.objects = {mm.path(0): mm.objects.pop(path)}
        restarted = scanner(mm, epoch_getter=lambda: "epoch-2")
        self.assertEqual(restarted.poll(LINE, self.ingest, policy="keep"), [])
        self.assertEqual(self.messages(), [("in", "kept on modem", "ok")])

    def test_own_send_is_not_imported_even_if_its_object_survives(self):
        mm = FakeModemManager()
        mm.send_hook = lambda args, kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args, kwargs["timeout"]))
        result = cellular_sms.send(LINE, "3", "6700", "BAL", runner=mm,
                                   local_sms_tracker=store)
        self.assertEqual(result["status"], "unknown")
        path = result["sms_path"]
        mm.objects[path]["properties"]["state"] = "sent"
        stored = scanner(mm, local_sms_tracker=store).poll(LINE, self.ingest, policy="keep")
        self.assertEqual(stored, [])
        self.assertEqual(self.messages(), [("out", "BAL", "pending")])
        self.assertEqual(mm.deletes(), [path], "the gateway's own object is cleaned up")

    def test_own_object_not_yet_sent_is_left_for_send_to_submit(self):
        rid = store.begin_local_modem_sms("3", "6700", "BAL")
        mm = FakeModemManager()
        path = mm.add(1, sms_object("6700", "BAL", pdu_type="submit", state="stored",
                                    timestamp="--"))
        store.bind_local_modem_sms(rid, MODEM, path)
        now = [0.0]
        s = scanner(mm, local_sms_tracker=store, clock=lambda: now[0])
        self.assertEqual(s.poll(LINE, self.ingest, policy="delete"), [])
        self.assertEqual(mm.deletes(), [])
        now[0] += cellular_sms._OWN_OBJECT_GRACE
        s.poll(LINE, self.ingest, policy="delete")
        self.assertEqual(mm.deletes(), [path])

    def test_interrupted_bind_is_claimed_by_content_and_external_send_is_imported(self):
        rid = store.begin_local_modem_sms("3", "6700", "BAL")
        mm = FakeModemManager()
        own = mm.add(1, sms_object("6700", "BAL", pdu_type="submit", state="sent",
                                   timestamp="--"))
        other = mm.add(2, sms_object("6700", "someone else's", pdu_type="submit",
                                     state="sent", timestamp="--"))
        stored = scanner(mm, local_sms_tracker=store).poll(LINE, self.ingest, policy="delete")
        self.assertEqual([(r["direction"], r["body"]) for r in stored],
                         [("out", "someone else's")])
        self.assertEqual(sorted(mm.deletes()), sorted([own, other]))
        self.assertEqual(store.get_message(rid)["modem_sms_path"], own)

    def test_reused_path_with_other_content_is_not_mistaken_for_own_send(self):
        rid = store.begin_local_modem_sms("3", "6700", "BAL")
        store.bind_local_modem_sms(rid, MODEM, "/org/freedesktop/ModemManager1/SMS/1")
        self.assertTrue(store.owns_local_modem_sms(
            "3", MODEM, "/org/freedesktop/ModemManager1/SMS/1", "6700", "BAL"))
        self.assertFalse(store.owns_local_modem_sms(
            "3", MODEM, "/org/freedesktop/ModemManager1/SMS/1", "6700", "other"))


class LegacyTrackingMigrationTests(unittest.TestCase):
    def test_bound_local_markers_move_onto_their_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "mdd-sim-gateway.sqlite"
            import sqlite3
            with sqlite3.connect(db) as c:
                c.executescript("""
                    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        instance TEXT NOT NULL, direction TEXT NOT NULL, peer TEXT NOT NULL,
                        body TEXT NOT NULL, status TEXT DEFAULT 'ok', ts INTEGER NOT NULL,
                        error TEXT, transport TEXT DEFAULT 'vowifi');
                    INSERT INTO messages(instance,direction,peer,body,status,ts,transport)
                        VALUES ('3','out','6700','BAL','sent',100,'cellular');
                    CREATE TABLE local_modem_sms (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        instance TEXT NOT NULL, iccid TEXT NOT NULL,
                        daemon_epoch TEXT NOT NULL DEFAULT '', message_id INTEGER,
                        modem_path TEXT, sms_path TEXT, content_hash TEXT NOT NULL,
                        created_ts INTEGER NOT NULL, bound_ts INTEGER,
                        cancelled INTEGER NOT NULL DEFAULT 0);
                    INSERT INTO local_modem_sms(instance,iccid,message_id,modem_path,sms_path,
                        content_hash,created_ts) VALUES ('3','card-a',1,
                        '/org/freedesktop/ModemManager1/Modem/0',
                        '/org/freedesktop/ModemManager1/SMS/4','x',100);
                """)
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(db),
                                PREVIOUS_DB_PATH=str(root / "vowifi.sqlite")):
                store.init()
                self.assertEqual(store.get_message(1)["modem_sms_path"],
                                 "/org/freedesktop/ModemManager1/SMS/4")
                with store._conn() as c:
                    self.assertIsNone(c.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='local_modem_sms'").fetchone())


class EpochTests(unittest.TestCase):
    def test_modemmanager_epoch_combines_boot_and_unique_dbus_owner(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return Result('s ":1.14"\n')

        boot = "12345678-1234-1234-1234-123456789abc"
        first = cellular_sms._modemmanager_epoch(runner, boot_id_reader=lambda: boot)
        second = cellular_sms._modemmanager_epoch(lambda *_a, **_k: Result('s ":1.15"\n'),
                                                  boot_id_reader=lambda: boot)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)
        self.assertEqual(calls[0][0][0], "busctl")
        self.assertEqual(calls[0][1]["timeout"], 3)


if __name__ == "__main__":
    unittest.main()

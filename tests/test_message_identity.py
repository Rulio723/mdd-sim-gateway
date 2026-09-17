import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import store


class TempStore:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "mdd-sim-gateway.sqlite"
        self.patch = patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(self.db),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        self.temp.cleanup()


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.ctx = TempStore().__enter__()
        store.init()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_redelivery_with_same_network_timestamp_is_ignored(self):
        first = store.ingest_message("1", "in", "+447700900123", "hello", transport="cellular",
                                     sent_ts=1_000, received_ts=1_002)
        again = store.ingest_message("1", "in", "+447700900123", "hello", transport="cellular",
                                     sent_ts=1_000, received_ts=1_500)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(first["ts"], 1_000)
        self.assertEqual(first["received_ts"], 1_002)

    def test_copy_over_other_transport_is_folded_even_in_national_format(self):
        store.ingest_message("1", "in", "+447700900123", "code 1234", transport="vowifi",
                             sent_ts=1_000, received_ts=1_001)
        copy = store.ingest_message("1", "in", "07700900123", "code 1234",
                                    transport="cellular", sent_ts=1_004)
        self.assertIsNone(copy)
        self.assertEqual(len(store.list_threads("1")), 1)

    def test_same_text_later_is_a_new_message(self):
        store.ingest_message("1", "in", "+447700900123", "ok", transport="vowifi", sent_ts=1_000)
        later = store.ingest_message("1", "in", "+447700900123", "ok", transport="cellular",
                                     sent_ts=5_000)
        same_transport = store.ingest_message("1", "in", "+447700900123", "ok",
                                              transport="vowifi", sent_ts=1_001)
        self.assertIsNotNone(later)
        self.assertIsNotNone(same_transport)

    def test_deleted_message_is_not_resurrected(self):
        rec = store.ingest_message("1", "in", "INFO", "promo", transport="cellular",
                                   sent_ts=2_000)
        store.delete_messages("1", [rec["id"]])
        self.assertIsNone(store.ingest_message("1", "in", "INFO", "promo",
                                               transport="cellular", sent_ts=2_000))

    def test_outgoing_object_without_timestamp_has_content_identity(self):
        first = store.ingest_message("1", "out", "+447700900123", "hi", transport="cellular",
                                     received_ts=10_000)
        again = store.ingest_message("1", "out", "+447700900123", "hi", transport="cellular",
                                     received_ts=99_000)
        self.assertIsNotNone(first)
        self.assertIsNone(again)

    def test_implausible_future_network_time_falls_back_to_receipt(self):
        rec = store.ingest_message("1", "in", "+447700900123", "x", transport="vowifi",
                                   sent_ts=10**10, received_ts=1_000)
        self.assertEqual(rec["ts"], 1_000)
        self.assertIsNone(rec["sent_ts"])

    def test_grown_body_keeps_both_identities(self):
        rec = store.ingest_message("1", "in", "+447700900123", "part one[…]",
                                   transport="vowifi", sent_ts=3_000)
        store.set_message_body(rec["id"], "part one part two")
        self.assertIsNone(store.ingest_message("1", "in", "+447700900123", "part one part two",
                                               transport="cellular", sent_ts=3_001))
        self.assertIsNone(store.ingest_message("1", "in", "+447700900123", "part one[…]",
                                               transport="vowifi", sent_ts=3_000))


class IdentityMigrationTests(unittest.TestCase):
    def test_upgrade_backfills_identity_and_folds_existing_duplicates(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript("""
                    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        instance TEXT NOT NULL, direction TEXT NOT NULL, peer TEXT NOT NULL,
                        body TEXT NOT NULL, status TEXT DEFAULT 'ok', ts INTEGER NOT NULL,
                        error TEXT, transport TEXT DEFAULT 'vowifi');
                    CREATE TABLE message_imports (fingerprint TEXT PRIMARY KEY,
                        instance TEXT NOT NULL, imported_ts INTEGER NOT NULL);
                    INSERT INTO messages(instance,direction,peer,body,ts,transport) VALUES
                        ('1','in','+447700900123','?',1000,'vowifi'),
                        ('1','in','+447700900123','?',1000,'cellular'),
                        ('1','in','+447700900123','?',1000,'cellular'),
                        ('1','in','99','--',1500,'cellular'),
                        ('1','in','SHOP','code 1',2000,'cellular'),
                        ('1','out','+447700900123','again',3000,'vowifi'),
                        ('1','out','+447700900123','again',3001,'vowifi');
                    INSERT INTO message_imports VALUES ('old','1',1);
                """)
            store.init()
            store.init()  # a second start must not re-run the data step
            with sqlite3.connect(ctx.db) as db:
                rows = db.execute("SELECT peer,body,transport FROM messages ORDER BY id").fetchall()
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(rows, [
                ("+447700900123", "?", "vowifi"),
                ("SHOP", "code 1", "cellular"),
                ("+447700900123", "again", "vowifi"),
                ("+447700900123", "again", "vowifi"),
            ])
            self.assertGreaterEqual(version, 1)
            self.assertNotIn("message_imports", tables)
            # The modem still holds the imported text: the first poll after upgrade must not
            # import it again.
            self.assertIsNone(store.ingest_message("1", "in", "SHOP", "code 1",
                                                   transport="cellular", sent_ts=2000))


class SubscriberScopeTests(unittest.TestCase):
    def setUp(self):
        self.ctx = TempStore().__enter__()
        self.lines = {"1": "iccid:8900000000000000001"}
        store.set_subscriber_resolver(lambda iid: self.lines.get(iid, ""))
        store.init()

    def tearDown(self):
        store.set_subscriber_resolver(None)
        self.ctx.__exit__(None, None, None)

    def test_same_sim_under_a_new_line_id_keeps_its_identities(self):
        rec = store.ingest_message("1", "in", "INFO", "kept on modem", transport="cellular",
                                   sent_ts=5_000)
        store.delete_messages("1", [rec["id"]])
        del self.lines["1"]
        self.lines["4"] = "iccid:8900000000000000001"       # line deleted, SIM re-added
        self.assertIsNone(store.ingest_message("4", "in", "INFO", "kept on modem",
                                               transport="cellular", sent_ts=5_000))

    def test_another_sim_reusing_a_line_id_starts_clean(self):
        store.ingest_message("1", "in", "INFO", "same text", transport="cellular", sent_ts=5_000)
        self.lines["1"] = "iccid:8900000000000000002"
        self.assertIsNotNone(store.ingest_message("1", "in", "INFO", "same text",
                                                  transport="cellular", sent_ts=5_000))

    def test_line_without_sim_identity_is_scoped_to_its_id(self):
        store.ingest_message("9", "in", "INFO", "x", transport="vowifi", sent_ts=5_000)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT scope FROM message_identities WHERE instance='9'")
                             .fetchone()[0], "line:9")


class ScopeMigrationTests(unittest.TestCase):
    def test_version_three_identities_move_to_their_sim(self):
        with TempStore() as ctx:
            store.init()
            store.ingest_message("1", "in", "INFO", "old", transport="cellular", sent_ts=5_000)
            with sqlite3.connect(ctx.db) as db:
                # Rebuild the pre-scope table as a version 3 database had it.
                rows = db.execute("SELECT instance,fingerprint,content_hash,transport,ts,"
                                  "message_id,created_ts FROM message_identities").fetchall()
                db.executescript("""
                    DROP TABLE message_identities;
                    CREATE TABLE message_identities (instance TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
                        transport TEXT NOT NULL, ts INTEGER NOT NULL, message_id INTEGER,
                        created_ts INTEGER NOT NULL, PRIMARY KEY(instance, fingerprint));
                    CREATE INDEX idx_message_identities_content
                        ON message_identities(instance, content_hash, ts);
                    PRAGMA user_version=3;
                """)
                db.executemany("INSERT INTO message_identities VALUES(?,?,?,?,?,?,?)", rows)
            store.set_subscriber_resolver(lambda iid: {"1": "imsi:001010000000001"}.get(iid, ""))
            try:
                store.init()
                with store._conn() as c:
                    self.assertEqual([r[0] for r in c.execute("SELECT scope FROM message_identities")],
                                     ["imsi:001010000000001"])
                self.assertIsNone(store.ingest_message("1", "in", "INFO", "old",
                                                       transport="cellular", sent_ts=5_000))
            finally:
                store.set_subscriber_resolver(None)


class TimeZoneIndependenceTests(unittest.TestCase):
    ZONES = ("UTC", "Asia/Shanghai", "America/Los_Angeles", "Europe/Berlin")

    def each_zone(self, fn):
        import os, time
        original = os.environ.get("TZ")
        results = set()
        try:
            for zone in self.ZONES:
                os.environ["TZ"] = zone
                time.tzset()
                results.add(fn())
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()
        return results

    def test_modemmanager_timestamps_do_not_depend_on_the_host_zone(self):
        from control.app import cellular_sms
        cases = {"2026-03-14T15:09:26+02": 1773493766, "2026-03-14T15:09:26+02:00": 1773493766,
                 "2026-03-14T15:09:26+0200": 1773493766, "2026-03-14T13:09:26Z": 1773493766,
                 "2026-03-14T09:09:26-04": 1773493766}
        for raw, expected in cases.items():
            self.assertEqual(self.each_zone(lambda: cellular_sms._timestamp(raw)), {expected}, raw)
        self.assertEqual(self.each_zone(lambda: cellular_sms._timestamp("2026-03-14T15:09:26")),
                         {0}, "a zone-less value is refused, not read in local time")

    def test_vowifi_scts_does_not_depend_on_the_host_zone(self):
        from control.app import sms_pdu
        self.assertEqual(self.each_zone(
            lambda: sms_pdu.deliver_timestamp("4404812143000462304151906280")), {1773493766})


class MigrationSafetyTests(unittest.TestCase):
    def test_a_step_interrupted_midway_rolls_back_and_runs_again(self):
        with TempStore() as ctx:
            store.init()
            store.ingest_message("1", "in", "INFO", "x", transport="vowifi", sent_ts=1_000)
            with sqlite3.connect(ctx.db) as db:
                rows = db.execute("SELECT instance,fingerprint,content_hash,transport,ts,"
                                  "message_id,created_ts FROM message_identities").fetchall()
                db.executescript("""
                    DROP TABLE message_identities;
                    CREATE TABLE message_identities (instance TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
                        transport TEXT NOT NULL, ts INTEGER NOT NULL, message_id INTEGER,
                        created_ts INTEGER NOT NULL, PRIMARY KEY(instance, fingerprint));
                    PRAGMA user_version=3;
                """)
                db.executemany("INSERT INTO message_identities VALUES(?,?,?,?,?,?,?)", rows)
            with patch.object(store, "identity_scope", side_effect=RuntimeError("power cut")):
                with self.assertRaises(RuntimeError):
                    store.init()
            with sqlite3.connect(ctx.db) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
                self.assertNotIn("message_identities_old", names)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM message_identities")
                                 .fetchone()[0], 1)
            store.init()
            with sqlite3.connect(ctx.db) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                                 len(store._MIGRATIONS))

    def test_start_after_running_an_older_version_repairs_what_it_left(self):
        with TempStore() as ctx:
            store.init()
            mms = store.create_outgoing_mms("1", "+447700900123", to_addrs=["+447700900123"],
                                            subject="", body="pic", transaction_id="T")
            store.save_mms_content(mms["id"], [{"content_type": "image/png", "data": b"x"}])
            with sqlite3.connect(ctx.db) as db:
                # What 1.9.5 does on this database: recreate its tables, write rows without
                # identities, delete a message without its MMS state.
                db.executescript("""
                    CREATE TABLE message_imports (fingerprint TEXT PRIMARY KEY,
                        instance TEXT NOT NULL, imported_ts INTEGER NOT NULL);
                    INSERT INTO message_imports VALUES ('oldfp','1',1);
                    CREATE TABLE local_modem_sms (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        instance TEXT NOT NULL, iccid TEXT NOT NULL,
                        daemon_epoch TEXT NOT NULL DEFAULT '', message_id INTEGER,
                        modem_path TEXT, sms_path TEXT, content_hash TEXT NOT NULL,
                        created_ts INTEGER NOT NULL, bound_ts INTEGER,
                        cancelled INTEGER NOT NULL DEFAULT 0);
                    INSERT INTO messages(instance,direction,peer,body,status,ts,transport)
                        VALUES ('1','in','INFO','written by 1.9.5','ok',7000,'cellular');
                """)
                db.execute("DELETE FROM messages WHERE id=?", (mms["id"],))
            store.init()
            self.assertIsNone(store.ingest_message("1", "in", "INFO", "written by 1.9.5",
                                                   transport="cellular", sent_ts=7000))
            with sqlite3.connect(ctx.db) as db:
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
                self.assertEqual(db.execute("SELECT COUNT(*) FROM mms").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT fingerprint FROM legacy_message_imports")
                                 .fetchall(), [("oldfp",)])
            self.assertNotIn("local_modem_sms", names)
            self.assertNotIn("message_imports", names)
            self.assertFalse((Path(store.mms_dir()) / str(mms["id"])).exists())

    def test_message_deleted_under_the_old_import_marker_stays_deleted(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript("""
                    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        instance TEXT NOT NULL, direction TEXT NOT NULL, peer TEXT NOT NULL,
                        body TEXT NOT NULL, status TEXT DEFAULT 'ok', ts INTEGER NOT NULL,
                        error TEXT, transport TEXT DEFAULT 'vowifi');
                    CREATE TABLE message_imports (fingerprint TEXT PRIMARY KEY,
                        instance TEXT NOT NULL, imported_ts INTEGER NOT NULL);
                    INSERT INTO message_imports VALUES ('deleted-by-user','1',1);
                """)
            store.init()
            self.assertIsNone(store.ingest_message(
                "1", "in", "INFO", "deleted long ago", transport="cellular", sent_ts=4_000,
                legacy_fingerprint="deleted-by-user"))
            self.assertIsNone(store.ingest_message(
                "1", "in", "INFO", "deleted long ago", transport="cellular", sent_ts=4_000))
            self.assertIsNotNone(store.ingest_message(
                "1", "in", "INFO", "new", transport="cellular", sent_ts=4_000,
                legacy_fingerprint="unknown"))
            self.assertEqual(len(store.list_threads("1")), 1)


class UpgradeStoragePolicyTests(unittest.TestCase):
    def run_with(self, version, settings, env=None):
        from control.app import main
        saved = []
        with patch.object(main.cfg, "get_settings", return_value=settings), \
                patch.object(main.cfg, "update_settings", side_effect=saved.append), \
                patch.dict("os.environ", env or {}, clear=False):
            changed = main._keep_modem_storage_on_upgrade(version)
        return changed, saved

    def test_only_an_upgraded_installation_without_a_choice_gets_keep(self):
        self.assertEqual(self.run_with(0, {}), (True, [{"cellular_sms_storage": "keep"}]))
        self.assertEqual(self.run_with(None, {}), (False, []), "new installation")
        self.assertEqual(self.run_with(4, {}), (False, []), "already on this version")
        self.assertEqual(self.run_with(0, {"cellular_sms_storage": "delete"}), (False, []))
        self.assertEqual(self.run_with(0, {}, {"MDD_CELLULAR_SMS_STORAGE": "delete"}),
                         (False, []))

    def test_schema_version_is_read_without_creating_the_database(self):
        with TempStore() as ctx:
            self.assertIsNone(store.schema_version())
            self.assertFalse(ctx.db.exists())
            sqlite3.connect(ctx.db).close()
            self.assertEqual(store.schema_version(), 0)
            store.init()
            self.assertEqual(store.schema_version(), len(store._MIGRATIONS))


class MigrationBackupTests(unittest.TestCase):
    OLD_SCHEMA = """
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, instance TEXT NOT NULL,
            direction TEXT NOT NULL, peer TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT DEFAULT 'ok', ts INTEGER NOT NULL, error TEXT,
            transport TEXT DEFAULT 'vowifi');
        INSERT INTO messages(instance,direction,peer,body,ts,transport) VALUES
            ('1','in','+447700900123','twice',1000,'vowifi'),
            ('1','in','+447700900123','twice',1000,'cellular');
    """

    def test_database_is_backed_up_before_the_first_destructive_step(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript(self.OLD_SCHEMA)
            store.init()
            backups = sorted(Path(store.backup_dir()).glob("*.sqlite"))
            self.assertEqual(len(backups), 1)
            self.assertIn(".v0-before-v", backups[0].name)
            with sqlite3.connect(backups[0]) as copy:
                self.assertEqual(copy.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2,
                                 "the copy holds the rows the migration folded")
                self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 0)
            with sqlite3.connect(ctx.db) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(oct(backups[0].stat().st_mode & 0o777), "0o600")
            store.init()
            self.assertEqual(len(list(Path(store.backup_dir()).glob("*.sqlite"))), 1,
                             "a current database is not copied again")

    def test_new_installation_needs_no_backup(self):
        with TempStore():
            store.init()
            self.assertFalse(Path(store.backup_dir()).exists())

    def test_failed_backup_stops_the_upgrade_before_anything_changes(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript(self.OLD_SCHEMA)
            Path(store.backup_dir()).parent.mkdir(parents=True, exist_ok=True)
            Path(store.backup_dir()).write_text("not a directory")    # makedirs fails
            with self.assertRaises(store.MigrationBackupError):
                store.init()
            with sqlite3.connect(ctx.db) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
            self.assertNotIn("message_identities", tables)

    def test_a_copy_that_does_not_verify_is_discarded_and_stops_the_upgrade(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript(self.OLD_SCHEMA)
            with patch.object(store, "_verify_backup",
                              side_effect=OSError("the backup does not match the database")):
                with self.assertRaises(store.MigrationBackupError):
                    store.init()
            self.assertEqual(list(Path(store.backup_dir()).iterdir()), [],
                             "no partial or unverified copy is left behind")
            with sqlite3.connect(ctx.db) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    def test_a_failed_migration_reuses_one_backup_across_service_restarts(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript(self.OLD_SCHEMA)
            with patch.object(store, "_migrate", side_effect=RuntimeError("migration failed")), \
                    patch.object(store.time, "strftime",
                                 side_effect=["20260101T000000Z", "20260101T000001Z"]):
                with self.assertRaisesRegex(RuntimeError, "migration failed"):
                    store.init()
                with self.assertRaisesRegex(RuntimeError, "migration failed"):
                    store.init()
            backups = list(Path(store.backup_dir()).glob("*.sqlite"))
            self.assertEqual(len(backups), 1,
                             "a service restart must not copy the database again")
            self.assertIn("20260101T000000Z", backups[0].name)

    def test_a_damaged_existing_backup_is_never_silently_replaced(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript(self.OLD_SCHEMA)
            backup = Path(store._backup_before_migration())
            backup.write_bytes(b"not a sqlite database")
            with self.assertRaisesRegex(store.MigrationBackupError,
                                        "existing migration backup.*refusing to overwrite"):
                store.init()
            self.assertEqual(list(Path(store.backup_dir()).glob("*.sqlite")), [backup])
            self.assertEqual(backup.read_bytes(), b"not a sqlite database")


if __name__ == "__main__":
    unittest.main()

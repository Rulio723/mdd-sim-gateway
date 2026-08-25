import asyncio
import tempfile
import time
from datetime import datetime
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import store

try:
    from control.app import config as cfg, main
except ImportError:                      # control-plane deps absent (fastapi et al.)
    cfg = main = None


class KeepaliveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_patch = patch.multiple(
            store, DATA_DIR=str(root), DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
            PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.store_patch.start()
        store.init()

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def test_an_unconfigured_line_reads_as_defaults_not_as_an_absence(self):
        record = store.get_keepalive("1")
        self.assertEqual(record["action"], "sms")
        self.assertEqual(record["interval_days"], 30)
        self.assertFalse(record["enabled"])

    def test_saving_config_never_clobbers_scheduler_state(self):
        store.save_keepalive_config("1", {"enabled": 1, "interval_days": 30})
        store.save_keepalive_state("1", {"last_status": "ok", "next_due_ts": 4242})
        store.save_keepalive_config("1", {"enabled": 1, "interval_days": 7})
        record = store.get_keepalive("1")
        self.assertEqual(record["interval_days"], 7)
        self.assertEqual(record["last_status"], "ok")
        self.assertEqual(record["next_due_ts"], 4242)

    def test_a_run_is_claimed_exactly_once_per_due_date(self):
        # The scheduled action costs the user real money, so a restart mid-sweep must not
        # be able to charge the SIM twice for one due date.
        self.assertTrue(store.claim_keepalive_run("1", "2026-09-21"))
        self.assertFalse(store.claim_keepalive_run("1", "2026-09-21"))
        self.assertTrue(store.claim_keepalive_run("1", "2026-10-21"))
        self.assertTrue(store.claim_keepalive_run("2", "2026-09-21"))

    def test_registration_stamp_survives_the_three_day_timeline_retention(self):
        # line_states is pruned after 3 days and hub.ok_since dies with the process; this
        # stamp is the only durable answer to "when was this number last on a network".
        store.touch_line_registered("1", 1_700_000_000)
        self.assertEqual(store.get_keepalive("1")["last_registered_ts"], 1_700_000_000)
        store.touch_line_registered("1", 1_700_003_600)
        self.assertEqual(store.get_keepalive("1")["last_registered_ts"], 1_700_003_600)

    def test_deleting_a_line_takes_its_keepalive_config_with_it(self):
        # Line ids are reused by the next created line; a new SIM must never inherit another
        # SIM's schedule and start paying for it.
        store.save_keepalive_config("1", {"enabled": 1, "sms_to": "+447700900456"})
        store.claim_keepalive_run("1", "2026-09-21")
        store.clear_allowance_data("1")
        self.assertFalse(store.get_keepalive("1")["enabled"])
        self.assertEqual(store.get_keepalive("1")["sms_to"], "")
        self.assertTrue(store.claim_keepalive_run("1", "2026-09-21"))

    def test_calls_are_indexed_by_line(self):
        with store._conn() as c:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='calls'")]
        self.assertIn("idx_calls_inst", names)


@unittest.skipIf(main is None, "control-plane dependencies are unavailable")
class KeepaliveConfigApiTests(unittest.TestCase):
    """The configured action spends the user's money on a real SIM, so the API validates it
    here rather than trusting the browser."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_patch = patch.multiple(
            store, DATA_DIR=str(root), DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
            PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.store_patch.start()
        store.init()
        self.inst = {"id": "1", "name": "UK SIM", "msisdn": "+447700900123", "enabled": True}
        self.cfg_patch = patch.multiple(
            cfg,
            list_instances=lambda: [self.inst],
            get_instance=lambda iid: self.inst if str(iid) == "1" else None,
            get_settings=lambda: {"timezone": "Asia/Shanghai"})
        self.cfg_patch.start()

    def tearDown(self):
        self.cfg_patch.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    def _save(self, body):
        return main.api_keepalive_save("1", body)["keepalive"]

    def test_a_chargeable_sms_without_a_destination_is_refused(self):
        for body in ({"enabled": True, "action": "sms", "sms_to": "", "sms_body": "x"},
                     {"enabled": True, "action": "sms", "sms_to": "+447700900456",
                      "sms_body": ""},
                     {"enabled": True, "action": "sms", "sms_to": "not a number",
                      "sms_body": "x"}):
            with self.assertRaises(Exception) as caught:
                self._save(body)
            self.assertEqual(getattr(caught.exception, "status_code", None), 422)

    def test_an_explicit_zero_interval_is_refused_rather_than_defaulted(self):
        # `or 30` would read 0 as "unset" and quietly schedule a run the user never asked for.
        for value in (0, -5, "abc"):
            with self.assertRaises(Exception) as caught:
                self._save({"enabled": True, "action": "balance_watch",
                            "interval_days": value})
            self.assertEqual(getattr(caught.exception, "status_code", None), 422)
        self.assertEqual(
            self._save({"enabled": True, "action": "balance_watch"})["interval_days"], 30)

    def test_watching_a_balance_needs_no_recipient(self):
        record = self._save({"enabled": True, "action": "balance_watch", "threshold": "10"})
        self.assertEqual(record["action"], "balance_watch")
        self.assertEqual(record["threshold"], "10")

    def test_the_first_run_is_a_date_the_user_picks(self):
        # "When does it first run" and "how often after that" are different questions; folding
        # them into one interval field made the first run unpredictable.
        record = self._save({"enabled": True, "action": "balance_watch", "threshold": "10",
                             "interval_days": 30, "next_run_date": "2026-09-15"})
        self.assertEqual(record["next_run_date"], "2026-09-15")
        # Anchored to the start of the execution window, so the stored time is the one shown
        # back rather than whatever moment the form happened to be submitted.
        due = datetime.fromtimestamp(record["next_due_ts"], main._local_tz())
        self.assertEqual((due.hour, due.minute), (main.KEEPALIVE_WINDOW[0], 0))
        self.assertEqual(main.api_keepalive("1")["keepalive"]["next_run_date"], "2026-09-15")

    def test_changing_the_interval_does_not_move_the_chosen_date(self):
        self._save({"enabled": True, "action": "balance_watch", "threshold": "10",
                    "interval_days": 30, "next_run_date": "2026-09-15"})
        record = self._save({"enabled": True, "action": "balance_watch", "threshold": "10",
                             "interval_days": 7, "next_run_date": "2026-09-15"})
        self.assertEqual(record["next_run_date"], "2026-09-15")
        self.assertEqual(record["interval_days"], 7)

    def test_an_unparseable_date_is_refused(self):
        for bad in ("15/09/2026", "2026-13-01", "tomorrow"):
            with self.assertRaises(Exception) as caught:
                self._save({"enabled": True, "action": "balance_watch", "next_run_date": bad})
            self.assertEqual(getattr(caught.exception, "status_code", None), 422)

    def test_switching_it_on_starts_the_clock_and_switching_it_off_stops_it(self):
        record = self._save({"enabled": True, "action": "sms", "sms_to": "+447700900456",
                             "sms_body": "keepalive"})
        self.assertGreater(record["next_due_ts"], 0)
        self.assertEqual(self._save({"enabled": False, "action": "sms"})["next_due_ts"], 0)

    def test_a_sim_that_is_not_in_the_gateway_is_marked_as_such(self):
        # There are always more configured lines than card slots. A SIM sitting in a drawer
        # cannot be sent anything, so offering it a keepalive switch would promise something
        # undeliverable — but its expiry still matters, so it is marked, not hidden.
        lines = [{"id": "1", "name": "in a reader", "iccid": "8901240000000001"},
                 {"id": "2", "name": "in a drawer", "iccid": "8901240000000002"}]
        cards = [{"present": True, "matched": "1", "iccid": "8901240000000001"}]
        with patch.object(cfg, "list_instances", return_value=lines), \
             patch.object(main.engine, "is_running", return_value=False), \
             patch.object(main.hub, "cards_list", return_value=cards):
            rows = {r["instance"]: r for r in asyncio.run(main.api_keepalive_summary())["lines"]}
        self.assertTrue(rows["1"]["in_gateway"])
        self.assertFalse(rows["2"]["in_gateway"])

    def test_a_card_matched_only_by_iccid_still_counts_as_present(self):
        # A card detected before its line was matched carries the ICCID but no match yet.
        lines = [{"id": "1", "name": "line", "iccid": "8901240000000001"}]
        cards = [{"present": True, "matched": None, "iccid": "8901240000000001"}]
        with patch.object(cfg, "list_instances", return_value=lines), \
             patch.object(main.engine, "is_running", return_value=False), \
             patch.object(main.hub, "cards_list", return_value=cards):
            row = asyncio.run(main.api_keepalive_summary())["lines"][0]
        self.assertTrue(row["in_gateway"])

    def test_a_removed_card_does_not_count(self):
        lines = [{"id": "1", "name": "line", "iccid": "8901240000000001"}]
        cards = [{"present": False, "matched": "1", "iccid": "8901240000000001"}]
        with patch.object(cfg, "list_instances", return_value=lines), \
             patch.object(main.engine, "is_running", return_value=False), \
             patch.object(main.hub, "cards_list", return_value=cards):
            row = asyncio.run(main.api_keepalive_summary())["lines"][0]
        self.assertFalse(row["in_gateway"])

    def test_a_running_engine_counts_even_before_the_card_scan_lands(self):
        # hub.cards is rebuilt by the PC/SC monitor, so it is empty for the first seconds
        # after a control-plane restart. Without this the page would claim every line was out
        # of the gateway until the first scan completed.
        lines = [{"id": "1", "name": "line", "iccid": "8901240000000001"}]
        with patch.object(cfg, "list_instances", return_value=lines), \
             patch.object(main.engine, "is_running", return_value=True), \
             patch.object(main.hub, "cards_list", return_value=[]):
            row = asyncio.run(main.api_keepalive_summary())["lines"][0]
        self.assertTrue(row["in_gateway"])

    def test_the_summary_reports_every_line_in_one_response(self):
        store.save_allowance("1", {"balance": "£4.20", "valid_until": "2099-11-30"},
                             source="sms")
        store.touch_line_registered("1", 1_700_000_000)
        with patch.object(main.hub, "cards_list", return_value=[]), \
             patch.object(main.engine, "is_running", return_value=False):
            line = asyncio.run(main.api_keepalive_summary())["lines"][0]
        self.assertEqual(line["instance"], "1")
        self.assertEqual(line["allowance"]["balance"], "£4.20")
        self.assertEqual(line["last_registered_ts"], 1_700_000_000)
        self.assertIsInstance(line["keepalive"]["enabled"], bool)
        self.assertGreater(line["days_to_expiry"], 0)


@unittest.skipIf(main is None, "control-plane dependencies are unavailable")
class MissedCallPushTests(unittest.TestCase):
    """A call nobody answered is worth a notification; one the user declined is not."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_patch = patch.multiple(
            store, DATA_DIR=str(root), DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
            PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.store_patch.start()
        store.init()
        self.pushes = []
        inst = {"id": "1", "name": "UK SIM", "enabled": True}
        self.patches = [
            patch.object(cfg, "get_instance", side_effect=lambda iid: inst),
            patch.object(cfg, "get_settings", return_value={}),
            patch.object(main, "_dispatch_push",
                         side_effect=lambda ev, *a, **k: self.pushes.append(ev)),
            patch.object(main.hub, "broadcast", new=self._noop),
        ]
        for item in self.patches:
            item.start()

    async def _noop(self, *args, **kwargs):
        return None

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    def _event(self, event, args):
        return asyncio.run(main.api_engine_event(
            {"instance": "1", "event": event, "args": args}))

    def test_an_unanswered_inbound_call_notifies_once_however_often_it_is_reported(self):
        self._event("call_in", ["+447700900321"])
        self._event("call_result", ["in", "+447700900321", "NOANSWER", "19"])
        self.assertEqual(self.pushes.count("missed_call"), 1)
        # call_result is fired from a backgrounded process and retried; a redelivery must not
        # notify the user a second time about one call.
        self._event("call_result", ["in", "+447700900321", "NOANSWER", "19"])
        self.assertEqual(self.pushes.count("missed_call"), 1)

    def test_a_call_the_user_declined_is_not_a_missed_call(self):
        self._event("call_in", ["+442079460958"])
        self._event("call_result", ["in", "+442079460958", "BUSY", "21"])
        self.assertNotIn("missed_call", self.pushes)
        self.assertEqual(store.list_calls("1")[0]["status"], "rejected")

    def test_an_outgoing_call_that_rang_out_is_not_a_missed_call(self):
        self._event("call_out", ["+15550000"])
        self._event("call_result", ["out", "+15550000", "NOANSWER", "19"])
        self.assertNotIn("missed_call", self.pushes)


@unittest.skipIf(main is None, "control-plane dependencies are unavailable")
class KeepaliveSchedulerTests(unittest.TestCase):
    """The scheduled action is a real, billed event on a real SIM. These pin the rules that
    keep it from firing when it should not, and from going unnoticed when it does."""

    ULTRA = {"name": "Ultra/Univision", "specific": True}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_patch = patch.multiple(
            store, DATA_DIR=str(root), DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
            PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.store_patch.start()
        store.init()
        self.inst = {"id": "1", "name": "US SIM", "msisdn": "+15550001", "enabled": True}
        self.sent = []
        self.pushes = []
        self.reply = {"text": ""}

        async def fake_send(iid, to, text, transport="auto"):
            self.sent.append((to, text))
            if self.reply["text"]:
                store.add_message("1", "in", to, self.reply["text"])
            return {"ok": True, "message": {"id": len(self.sent)}}

        from control.app import carrier_id
        self.patches = [
            patch.object(cfg, "get_instance", side_effect=lambda iid: self.inst),
            patch.object(cfg, "get_settings", return_value={"timezone": "Asia/Shanghai"}),
            patch.object(carrier_id, "lookup", return_value=self.ULTRA),
            patch.object(main, "send_sms_on_line", side_effect=fake_send),
            patch.object(main, "KEEPALIVE_RECONCILE_ATTEMPTS", 1),
            patch.object(main, "KEEPALIVE_RECONCILE_INTERVAL", 0),
            patch.object(main.notify_push, "dispatch",
                         side_effect=lambda s, ev, i, src, txt=None:
                         self.pushes.append((ev, txt or ""))),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    def _run(self, record=None):
        return asyncio.run(main._run_keepalive(
            "1", self.inst, record or store.get_keepalive("1")))

    def test_a_carrier_balance_is_read_out_of_its_own_wording(self):
        for text, expected in [("£4.20", 4.2), ("$12.40", 12.4), ("4,200 JPY", 4200.0),
                               ("12.5 GBP", 12.5)]:
            self.assertEqual(main._parse_money(text), expected)
        # Unreadable means unreadable: a guess here would raise false "balance low" alarms.
        for text in ("", "active", None, "unlimited"):
            self.assertIsNone(main._parse_money(text))

    def test_the_execution_window_keeps_it_to_daytime(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Asia/Shanghai")
        from datetime import datetime as dt
        for hour in (10, 15, 21):
            self.assertTrue(main._keepalive_in_window(dt(2026, 8, 25, hour, tzinfo=zone)))
        for hour in (2, 9, 22, 23):
            self.assertFalse(main._keepalive_in_window(dt(2026, 8, 25, hour, tzinfo=zone)))

    def test_a_chargeable_sms_reports_success_as_well_as_failure(self):
        # It spent the user's money; a silent success would leave them unable to tell the
        # difference between "kept alive" and "never ran".
        store.save_keepalive_config("1", {"enabled": 1, "action": "sms",
                                          "sms_to": "+15550002", "sms_body": "ka",
                                          "verify_charge": 0})
        record = self._run()
        self.assertEqual(record["last_status"], "ok")
        self.assertEqual(self.sent, [("+15550002", "ka")])
        self.assertEqual([ev for ev, _ in self.pushes], ["keepalive_result"])
        self.assertIn("保号成功", self.pushes[0][1])
        self.assertGreater(record["next_due_ts"], 0)

    def test_a_failed_run_is_retried_soon_rather_than_next_interval(self):
        # It never produced the chargeable event the number needs, so waiting another 30 days
        # would leave the SIM unused for two months.
        store.save_keepalive_config("1", {"enabled": 1, "action": "sms", "sms_to": "",
                                          "sms_body": "", "interval_days": 30})
        record = self._run()
        self.assertEqual(record["last_status"], "failed")
        soon = record["next_due_ts"] - int(time.time())
        self.assertLessEqual(soon, main.KEEPALIVE_FAILURE_RETRY_SECONDS + 5)
        self.assertGreater(soon, 0)
        self.assertIn("保号失败", self.pushes[0][1])

    def test_a_run_that_keeps_failing_only_announces_the_first_one(self):
        # It now retries hourly; repeating the same bad news every hour is how a notification
        # channel gets muted.
        store.save_keepalive_config("1", {"enabled": 1, "action": "sms", "sms_to": "",
                                          "sms_body": ""})
        first = self._run()
        self.assertEqual(len(self.pushes), 1)
        self.pushes.clear()
        self._run(first)
        self.assertEqual(self.pushes, [])

    def test_a_retry_is_allowed_but_a_restart_mid_run_is_not(self):
        # The claim ledger exists to stop a restart from charging the SIM twice for one due
        # time; it must not also block the hourly retry, which has a new due time.
        due = int(time.time()) - 60
        self.assertTrue(store.claim_keepalive_run("1", main._keepalive_slot(due)))
        self.assertFalse(store.claim_keepalive_run("1", main._keepalive_slot(due)))
        retry_due = due + main.KEEPALIVE_FAILURE_RETRY_SECONDS
        self.assertTrue(store.claim_keepalive_run("1", main._keepalive_slot(retry_due)))

    def test_a_low_balance_is_announced_then_held_until_it_recovers(self):
        store.save_allowance_query_rule("1", "6700", "BAL")
        store.save_keepalive_config("1", {"enabled": 1, "action": "balance_watch",
                                          "threshold": "$10"})
        self.reply["text"] = "钱包余额: $3.00"
        low = self._run()
        self.assertEqual(low["last_status"], "balance_low")
        self.assertEqual([ev for ev, _ in self.pushes], ["balance_low"])
        self.assertGreater(low["balance_low_since"], 0)

        # Still low a run later: the condition has not changed, so neither has the user's
        # to-do list. Repeating it every run would train them to ignore it.
        self.pushes.clear()
        still_low = self._run(low)
        self.assertEqual(still_low["last_status"], "balance_low")
        self.assertEqual(self.pushes, [])

        # Topped up. The next dip has to notify immediately, so the episode is forgotten.
        store.clear_messages("1")
        self.reply["text"] = "钱包余额: $50.00"
        recovered = self._run(still_low)
        self.assertEqual(recovered["last_status"], "ok")
        self.assertEqual(recovered["balance_low_since"], 0)
        self.assertEqual(recovered["balance_low_last_notified"], 0)
        self.assertEqual(self.pushes, [])          # a healthy balance is not news

        store.clear_messages("1")
        self.reply["text"] = "钱包余额: $2.00"
        self._run(recovered)
        self.assertEqual([ev for ev, _ in self.pushes], ["balance_low"])

    def test_an_unreadable_balance_is_shown_and_never_judged(self):
        store.save_allowance_query_rule("1", "6700", "BAL")
        store.save_keepalive_config("1", {"enabled": 1, "action": "balance_watch",
                                          "threshold": "$10"})
        self.reply["text"] = "Your plan is active. Thanks!"
        record = self._run()
        self.assertEqual(record["last_status"], "ok")
        self.assertEqual(self.pushes, [])

    def test_watching_a_balance_without_a_query_rule_fails_loudly(self):
        # An unrecognised carrier has no built-in rule, so there is nothing to read the
        # balance with. Saying so beats reporting a hollow success every interval.
        from control.app import carrier_id
        store.save_keepalive_config("1", {"enabled": 1, "action": "balance_watch",
                                          "threshold": "$10"})
        with patch.object(carrier_id, "lookup", return_value=None):
            record = self._run()
        self.assertEqual(record["last_status"], "failed")
        self.assertIn("没有余额查询规则", record["last_detail"])

    def test_a_line_that_is_off_or_not_due_is_left_alone(self):
        asyncio.run(main._maybe_run_keepalive("1", self.inst))
        self.assertEqual(self.sent, [])            # disabled

        store.save_keepalive_config("1", {"enabled": 1, "action": "sms",
                                          "sms_to": "+15550002", "sms_body": "ka"})
        store.save_keepalive_state("1", {"next_due_ts": int(time.time()) + 86400})
        asyncio.run(main._maybe_run_keepalive("1", self.inst))
        self.assertEqual(self.sent, [])            # enabled but not due yet

    def test_one_due_date_is_charged_once_even_if_the_poller_re_enters(self):
        store.save_keepalive_config("1", {"enabled": 1, "action": "sms",
                                          "sms_to": "+15550002", "sms_body": "ka",
                                          "verify_charge": 0})
        due = int(time.time()) - 60
        store.save_keepalive_state("1", {"next_due_ts": due})
        with patch.object(main, "_keepalive_in_window", return_value=True):
            asyncio.run(main._maybe_run_keepalive("1", self.inst))
            store.save_keepalive_state("1", {"next_due_ts": due})   # simulate a lost update
            asyncio.run(main._maybe_run_keepalive("1", self.inst))
        self.assertEqual(len(self.sent), 1)

    def test_it_waits_for_the_execution_window(self):
        store.save_keepalive_config("1", {"enabled": 1, "action": "sms",
                                          "sms_to": "+15550002", "sms_body": "ka"})
        store.save_keepalive_state("1", {"next_due_ts": int(time.time()) - 60})
        with patch.object(main, "_keepalive_in_window", return_value=False):
            asyncio.run(main._maybe_run_keepalive("1", self.inst))
        self.assertEqual(self.sent, [])


@unittest.skipIf(main is None, "control-plane dependencies are unavailable")
class AllowanceHarvestTests(unittest.TestCase):
    """A query started with no browser open must still be read: reconcile() is otherwise only
    reached from GET /allowance, and the reply would expire unread two minutes later."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_patch = patch.multiple(
            store, DATA_DIR=str(root), DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
            PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.store_patch.start()
        store.init()

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def test_an_inbound_reply_settles_the_query_that_asked_for_it(self):
        store.start_allowance_query("1", "6700", "BAL", "ultramobile", "auto")
        store.add_message("1", "in", "6700", "钱包余额: $7.00")
        main._harvest_allowance_reply("1", "6700")
        self.assertEqual(store.get_allowance("1")["balance"], "$7.00")
        self.assertEqual(store.latest_allowance_query("1")["status"], "parsed")

    def test_an_unrelated_sender_does_not_settle_it(self):
        store.start_allowance_query("1", "6700", "BAL", "ultramobile", "auto")
        store.add_message("1", "in", "+15550009", "钱包余额: $999.00")
        main._harvest_allowance_reply("1", "+15550009")
        self.assertEqual(store.get_allowance("1")["balance"], "")


if __name__ == "__main__":
    unittest.main()

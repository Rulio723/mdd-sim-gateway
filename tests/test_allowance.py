import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import allowance, store

ROOT = Path(__file__).resolve().parent.parent


class AllowanceTests(unittest.TestCase):
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

    def test_carrier_rules_ignore_user_editable_line_name(self):
        ultra = {"id": "1", "name": "anything", "mcc": "310", "mnc": "240",
                 "carrier_identity": {"gid1": "value"}}
        resolved = {"name": "Ultra/Univision", "specific": True}
        self.assertEqual(allowance.query_rule(ultra, resolved)["effective"],
                         {"recipient": "6700", "body": "BAL"})

        renamed_ctexcel = {"id": "7", "name": "my travel card",
                           "carrier_identity": {"spn": "CTExcel"}}
        self.assertEqual(allowance.query_rule(renamed_ctexcel, {"name": "EE"})["effective"],
                         {"recipient": "888", "body": "BAL"})

        # A user naming an unrelated T-Mobile card "ultramobile" must never enable sending.
        mcc_only = {"id": "2", "name": "ultramobile", "mcc": "310", "mnc": "240"}
        self.assertIsNone(allowance.query_rule(
            mcc_only, {"name": "T-Mobile - US", "specific": False})["effective"])

    def test_custom_rule_overrides_and_restores_default(self):
        inst = {"id": "1", "carrier_identity": {"gid1": "value"}}
        resolved = {"name": "Ultra/Univision", "specific": True}
        store.save_allowance_query_rule("1", "123", "USAGE")
        rule = allowance.query_rule(inst, resolved)
        self.assertTrue(rule["custom"])
        self.assertEqual(rule["effective"], {"recipient": "123", "body": "USAGE"})
        store.delete_allowance_query_rule("1")
        self.assertEqual(allowance.query_rule(inst, resolved)["effective"],
                         {"recipient": "6700", "body": "BAL"})

    def test_ultra_multipart_reply_is_merged_and_cached(self):
        store.save_allowance("1", {"balance": "old"}, source="manual", updated_ts=90)
        store.start_allowance_query("1", "6700", "BAL", "ultramobile", "auto",
                                    started_ts=100)
        store.add_message("1", "in", "6700",
                          "本月剩余通话时间：100\n分钟 本月剩余短信数：89\n条 "
                          "本月剩余流量：100.0MB\n计划到期日：08/28/2026\nPayGo",
                          ts=105)
        first = allowance.reconcile("1", now=106)
        self.assertEqual(first["voice_remaining"], "100 分钟")
        self.assertEqual(first["sms_remaining"], "89 条")
        self.assertEqual(first["data_remaining"], "100.0MB")
        self.assertEqual(first["valid_until"], "08/28/2026")
        self.assertEqual(first["balance"], "old")

        store.add_message("1", "in", "6700", "钱包余额：$5", ts=108)
        complete = allowance.reconcile("1", now=109)
        self.assertEqual(complete["balance"], "$5")
        self.assertEqual(complete["sms_remaining"], "89 条")
        self.assertEqual(complete["source"], "sms")
        self.assertEqual(complete["updated_ts"], 108)

    def test_ctexcel_reply_parses_balance(self):
        parsed = allowance.parse_reply("ctexcel", [
            {"body": "Your current credit balance is £1.01."}])
        self.assertEqual(parsed, {"balance": "£1.01"})

    def test_unqueried_or_wrong_sender_message_is_not_used(self):
        store.add_message("7", "in", "888", "Your current credit balance is £99.", ts=100)
        self.assertEqual(allowance.reconcile("7", now=101)["balance"], "")
        store.start_allowance_query("7", "888", "BAL", "ctexcel", "auto",
                                    started_ts=110)
        store.add_message("7", "in", "999", "Your current credit balance is £50.", ts=111)
        self.assertEqual(allowance.reconcile("7", now=112)["balance"], "")

    def test_line_cleanup_removes_snapshot_rule_and_query(self):
        store.save_allowance("4", {"balance": "$3"})
        store.save_allowance_query_rule("4", "123", "BAL")
        store.start_allowance_query("4", "123", "BAL", "", "auto")
        store.clear_allowance_data("4")
        self.assertIsNone(store.get_allowance_query_rule("4"))
        self.assertIsNone(store.latest_allowance_query("4"))
        self.assertEqual(store.get_allowance("4")["balance"], "")

    def test_activation_date_requires_iso_format(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            allowance.clean_allowance({"activated_at": "08/01/2026"})


class AllowancePanelTests(unittest.TestCase):
    def test_query_rule_editor_is_reachable_from_the_allowance_page(self):
        source = (ROOT / "webui" / "src" / "views" /
                  "AllowancePanel.jsx").read_text(encoding="utf-8")
        self.assertIn("setEditingRule(true)", source)
        self.assertIn("{editingRule && <div", source)
        self.assertIn(">{t('Query settings')}</button>", source)
        self.assertNotIn("Configure it in Messages", source)


if __name__ == "__main__":
    unittest.main()

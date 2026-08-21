"""Keeping machine payloads out of the conversation view.

A binary SMS — SIM OTA data-download, a silent service push — is unpacked by Asterisk one
payload byte per character, so it used to land in the message list as mojibake. These cover
the classification (from the PDU header, and from the body alone for an older engine), the
diversion of the inbound event, and the sweep that files the ones already in the database.
"""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from control.app import main, sms_pdu, store

# Two real payloads from line 1, short-code 2942: the 4-byte opener and one of the encrypted
# blocks that followed it, in the byte-per-character form Asterisk handed to the manager.
OPENER = bytes.fromhex("01a8150e")
BLOCK = bytes.fromhex("42c4c3576ad6a0c96e4f9c2a011c2b385256201099ecd4df625895814da148")
# The one that got away: 8 bytes from short-code 2939 with no C0 control byte anywhere in it.
# Only the C1 range (9b, 8f) marks it as a payload.
SHORT_BLOCK = bytes.fromhex("c5e79b8fb7572767")


def widened(payload: bytes) -> str:
    """What Asterisk's 8-bit path produces: every byte becomes one code point."""
    return payload.decode("latin-1")


class DcsTests(unittest.TestCase):
    def test_general_group_reports_its_character_set(self):
        self.assertFalse(sms_pdu.dcs_is_8bit(0x00))     # GSM 7-bit, the ordinary text case
        self.assertTrue(sms_pdu.dcs_is_8bit(0x04))      # 8-bit
        self.assertFalse(sms_pdu.dcs_is_8bit(0x08))     # UCS-2 — Chinese text, still text
        self.assertTrue(sms_pdu.dcs_is_8bit(0x16))      # 8-bit with a message class

    def test_the_1111_group_reports_its_character_set(self):
        self.assertFalse(sms_pdu.dcs_is_8bit(0xF0))     # 7-bit, class 0
        self.assertTrue(sms_pdu.dcs_is_8bit(0xF6))      # 8-bit, class 2 — the SIM OTA case

    def test_reserved_and_message_waiting_groups_are_text(self):
        for dcs in (0x40, 0x80, 0xC0, 0xD0, 0xE0):
            self.assertFalse(sms_pdu.dcs_is_8bit(dcs), f"{dcs:#04x} is not an 8-bit group")

    def test_message_class_only_where_the_dcs_carries_one(self):
        self.assertIsNone(sms_pdu.dcs_message_class(0x00))   # bit 4 clear: no class present
        self.assertEqual(sms_pdu.dcs_message_class(0x12), 2)
        self.assertEqual(sms_pdu.dcs_message_class(0xF6), 2)
        self.assertEqual(sms_pdu.dcs_message_class(0xF0), 0)

    def test_each_marker_alone_is_enough_to_call_it_a_machine_payload(self):
        self.assertTrue(sms_pdu.PduMeta(tp_dcs=0x04).is_machine_payload)          # 8-bit
        self.assertTrue(sms_pdu.PduMeta(tp_dcs=0x12).is_machine_payload)          # class 2
        self.assertTrue(sms_pdu.PduMeta(tp_dcs=0x00, tp_pid=0x7F).is_machine_payload)
        self.assertFalse(sms_pdu.PduMeta(tp_dcs=0x00, tp_pid=0).is_machine_payload)
        self.assertFalse(sms_pdu.PduMeta(tp_dcs=0x08).is_machine_payload)         # UCS-2 text

    def test_an_engine_that_reports_nothing_is_not_treated_as_binary(self):
        # Falling back to looks_binary() is the caller's job; an absent DCS must never by
        # itself divert a message away from the conversation.
        meta = sms_pdu.parse_event_args(["2942", "Yg==", "", "", ""])
        self.assertFalse(meta.known)
        self.assertFalse(meta.is_machine_payload)


class EventArgTests(unittest.TestCase):
    def test_header_fields_are_read_off_the_end_of_the_event(self):
        meta = sms_pdu.parse_event_args(
            ["2942", "Yg==", "", "", "", "127", "246", "0003a80201", "400581299200f6"])
        self.assertEqual((meta.tp_pid, meta.tp_dcs), (0x7F, 0xF6))
        self.assertEqual(meta.udh_hex, "0003a80201")
        self.assertTrue(meta.is_machine_payload)

    def test_junk_in_a_field_is_ignored_rather_than_guessed_at(self):
        meta = sms_pdu.parse_event_args(
            ["2942", "Yg==", "", "", "", "x", "999", "odd-length-9", "ZZZZ"])
        self.assertIsNone(meta.tp_pid)
        self.assertIsNone(meta.tp_dcs, "an out-of-range DCS must not be trusted")
        self.assertEqual(meta.udh_hex, "")
        self.assertEqual(meta.tpdu_hex, "")


class LooksBinaryTests(unittest.TestCase):
    def test_real_payloads_are_recognised_without_a_dcs(self):
        self.assertTrue(sms_pdu.looks_binary(widened(OPENER)))
        self.assertTrue(sms_pdu.looks_binary(widened(BLOCK)))

    def test_a_short_payload_with_no_c0_byte_is_still_recognised(self):
        # Every byte here is >= 0x27, so a C0-only test misses it entirely — this is the case
        # that slipped through the first sweep on the live database.
        self.assertFalse(any(b < 0x20 for b in SHORT_BLOCK))
        self.assertTrue(sms_pdu.looks_binary(widened(SHORT_BLOCK)))

    def test_ordinary_texts_are_not(self):
        for text in ("G-874316 is your Google verification code.",
                     "Ultra Mobile: To easily manage your plan, visit u.ultra.me/account",
                     "【CTExcel】尊敬的用户，您当前使用的手机卡为学生活动专属SIM卡",
                     "本月剩余通话时间：100\n分钟 本月剩余短信数：89\n条",
                     "Telegram code: 18150\n\nYou can also tap on this link to log in:",
                     "It ' a test message",
                     ""):
            self.assertFalse(sms_pdu.looks_binary(text), text[:40])

    def test_accented_latin_is_text_even_though_every_byte_fits(self):
        # Mojibake-looking but real: high code points alone must never trigger the fallback,
        # or a French or Turkish text would be filed away as a payload.
        self.assertFalse(sms_pdu.looks_binary("Votre code est prêt — à bientôt, Café"))

    def test_the_widen_is_reversible(self):
        self.assertEqual(sms_pdu.body_to_hex(widened(BLOCK)), BLOCK.hex())
        self.assertEqual(sms_pdu.body_to_hex("你好"), "", "real characters have no byte form")


class TempStore(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        patcher = patch.multiple(store, DATA_DIR=str(root),
                                 DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                 PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()


class SweepTests(TempStore):
    def test_payloads_already_stored_are_moved_out_of_the_conversation(self):
        store.add_message("1", "in", "2942", widened(OPENER))
        store.add_message("1", "in", "2942", widened(BLOCK))
        keep = store.add_message("1", "in", "22000", "G-874316 is your Google verification code.")
        store.add_message("1", "out", "2942", "STOP")

        with store._conn() as c:
            self.assertEqual(store._sweep_binary_messages(c), 2)

        self.assertEqual([m["id"] for m in store.list_messages("1", "22000")], [keep["id"]])
        self.assertEqual([m["body"] for m in store.list_messages("1", "2942")], ["STOP"],
                         "the mojibake is gone; only our own reply is left in that thread")
        filed = store.list_binary_sms("1")
        self.assertEqual({row["body_hex"] for row in filed}, {OPENER.hex(), BLOCK.hex()},
                         "the bytes must survive the move exactly")

    def test_the_outgoing_stop_reply_stays_a_message(self):
        sent = store.add_message("1", "out", "2942", "STOP")
        with store._conn() as c:
            store._sweep_binary_messages(c)
        self.assertEqual([m["id"] for m in store.list_messages("1", "2942")], [sent["id"]])

    def test_sweeping_twice_changes_nothing_the_second_time(self):
        store.add_message("1", "in", "2942", widened(BLOCK))
        with store._conn() as c:
            self.assertEqual(store._sweep_binary_messages(c), 1)
            self.assertEqual(store._sweep_binary_messages(c), 0)
        self.assertEqual(len(store.list_binary_sms("1")), 1)


class InboundEventTests(TempStore):
    """The /api/engine/event path: a payload must be filed, never broadcast or pushed."""

    def setUp(self):
        super().setUp()
        broadcast = patch.object(main.hub, "broadcast", AsyncMock())
        self.broadcast = broadcast.start()
        self.addCleanup(broadcast.stop)
        push = patch.object(main, "_dispatch_push")
        self.push = push.start()
        self.addCleanup(push.stop)

    def _post(self, args):
        return asyncio.run(main.api_engine_event(
            {"instance": "1", "event": "sms_in", "args": args}))

    def test_a_binary_payload_is_filed_and_never_surfaces(self):
        body = base64.b64encode(widened(BLOCK).encode()).decode()
        result = self._post(["2942", body, "", "", "", "0", "246",
                             "", "400581299200f6" + BLOCK.hex()])
        self.assertEqual(result["stored"], "binary")
        self.assertEqual(store.list_messages("1", "2942"), [])
        # An open page is told to refresh its count, but the event must carry no "message":
        # the web UI's toast keys on that field, and a payload nobody can read must not pop up
        # as "SMS from 2942". No push notification at all.
        self.broadcast.assert_awaited_once()
        event = self.broadcast.await_args.args[0]
        self.assertTrue(event.get("binary"))
        self.assertNotIn("message", event)
        self.push.assert_not_called()
        filed = store.list_binary_sms("1")[0]
        self.assertEqual(filed["tp_dcs"], 0xF6)
        self.assertEqual(filed["body_hex"], BLOCK.hex())
        self.assertTrue(filed["tpdu_hex"].endswith(BLOCK.hex()),
                        "the raw PDU is what a later analysis has to work from")

    def test_a_binary_part_is_filed_instead_of_joining_the_text_buffer(self):
        body = base64.b64encode(widened(BLOCK).encode()).decode()
        self._post(["2942", body, "17", "3", "2", "0", "246", "0003110302", ""])
        with store._conn() as c:
            buffered = c.execute("SELECT COUNT(*) FROM sms_segments").fetchone()[0]
        self.assertEqual(buffered, 0, "binary parts must not be text-joined")
        filed = store.list_binary_sms("1")[0]
        self.assertEqual((filed["concat_ref"], filed["concat_total"], filed["concat_seq"]),
                         (17, 3, 2), "the triplet is kept so the bytes can be rejoined later")

    def test_an_ordinary_text_still_becomes_a_message(self):
        text = "G-874316 is your Google verification code."
        body = base64.b64encode(text.encode()).decode()
        self._post(["22000", body, "", "", "", "0", "0", "", "400581...00"])
        self.assertEqual([m["body"] for m in store.list_messages("1", "22000")], [text])
        self.assertEqual(store.list_binary_sms("1"), [])
        self.broadcast.assert_awaited()

    def test_a_ucs2_text_is_not_mistaken_for_a_payload(self):
        text = "【CTExcel】您的验证码是 8891"
        body = base64.b64encode(text.encode()).decode()
        self._post(["10086", body, "", "", "", "0", "8", "", ""])
        self.assertEqual([m["body"] for m in store.list_messages("1", "10086")], [text])

    def test_an_old_engine_without_header_fields_still_files_a_payload(self):
        body = base64.b64encode(widened(BLOCK).encode()).decode()
        result = self._post(["2942", body])
        self.assertEqual(result["stored"], "binary")
        filed = store.list_binary_sms("1")[0]
        self.assertIsNone(filed["tp_dcs"])
        self.assertEqual(filed["body_hex"], BLOCK.hex())


if __name__ == "__main__":
    unittest.main()

"""Reassembly of a multi-part (concatenated) inbound SMS.

A text longer than one SMS arrives as several SMS-DELIVER PDUs, out of order and each with its
own User Data Header. These cover the path from the sms_in event through the segment buffer to
the single stored message.
"""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import main, store

# The real CTExcel text that arrived as three separate messages, in the order the parts landed.
CTEXCEL = [
    "【CTExcel】尊敬的用户，您当前使用的手机卡为学生活动专属SIM卡，您当前仍未订购任意套餐。若激活超过7日未订购任何套餐，您的SI",
    "M卡将被自动暂停服务及终止。为避免影响正常使用，请尽快前往CTExcel官网订购套餐：https://www.ctexcel.com/",
    "uk/trail/index/1 。感谢您的支持！",
]


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

    def _buffered(self):
        with store._conn() as c:
            return c.execute("SELECT COUNT(*) FROM sms_segments").fetchone()[0]


class SegmentBufferTests(TempStore):
    def test_group_completes_only_on_the_last_part_and_comes_back_in_order(self):
        # parts arrive 2, 1, 3 — the order the carrier actually delivered them
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 2, CTEXCEL[1]))
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0]))
        parts = store.add_sms_segment("7", "888", 155, 3, 3, CTEXCEL[2])
        self.assertEqual(parts, CTEXCEL)
        self.assertEqual("".join(parts).startswith("【CTExcel】尊敬的用户"), True)
        self.assertIn("SIM卡将被自动暂停", "".join(parts))
        self.assertEqual(self._buffered(), 0, "a completed group must leave nothing behind")

    def test_redelivered_part_neither_completes_nor_duplicates_a_group(self):
        store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0])
        # the SMSC re-pushes part 1 twice more when its RP-ACK is missed
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0]))
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0]))
        self.assertEqual(self._buffered(), 1)
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 2, CTEXCEL[1]))
        self.assertEqual(store.add_sms_segment("7", "888", 155, 3, 3, CTEXCEL[2]), CTEXCEL)

    def test_a_completed_group_is_not_completed_a_second_time(self):
        store.add_sms_segment("7", "888", 155, 2, 1, "a")
        self.assertEqual(store.add_sms_segment("7", "888", 155, 2, 2, "b"), ["a", "b"])
        # a late duplicate of the final part must not republish the whole text
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 2, 2, "b"))

    def test_groups_are_isolated_by_line_sender_and_reference(self):
        self.assertIsNone(store.add_sms_segment("7", "888", 1, 2, 1, "L7"))
        self.assertIsNone(store.add_sms_segment("5", "888", 1, 2, 1, "L5"))
        self.assertIsNone(store.add_sms_segment("7", "999", 1, 2, 1, "other-sender"))
        self.assertIsNone(store.add_sms_segment("7", "888", 2, 2, 1, "other-ref"))
        self.assertEqual(self._buffered(), 4)
        self.assertEqual(store.add_sms_segment("7", "888", 1, 2, 2, "-tail"), ["L7", "-tail"])
        self.assertEqual(self._buffered(), 3, "only the completed group is cleared")


class StaleSegmentTests(TempStore):
    def test_sweep_takes_only_groups_past_the_timeout(self):
        store.add_sms_segment("7", "888", 1, 3, 1, "old", ts=1000)
        store.add_sms_segment("7", "888", 2, 2, 1, "fresh", ts=5000)
        stale = store.take_stale_sms_segments(timeout=180, now=5000)
        self.assertEqual([g["concat_ref"] for g in stale], [1])
        self.assertEqual(stale[0], {"instance": "7", "peer": "888", "concat_ref": 1,
                                    "total": 3, "first_ts": 1000, "seqs": [1],
                                    "bodies": ["old"]})
        self.assertEqual(self._buffered(), 1, "the fresh group stays buffered")

    def test_sweep_ages_a_group_by_its_first_part_not_its_last(self):
        store.add_sms_segment("7", "888", 1, 3, 1, "first", ts=1000)
        store.add_sms_segment("7", "888", 1, 3, 3, "third", ts=4990)
        stale = store.take_stale_sms_segments(timeout=180, now=5000)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["seqs"], [1, 3])
        self.assertEqual(self._buffered(), 0)

    def test_sweep_is_a_no_op_when_nothing_is_buffered(self):
        self.assertEqual(store.take_stale_sms_segments(timeout=180, now=5000), [])


class TripletParsingTests(unittest.TestCase):
    def test_valid_triplet(self):
        self.assertEqual(main._concat_triplet(["888", "Yg==", "155", "3", "2"]), (155, 3, 2))

    def test_single_part_sms_sends_empty_fields(self):
        self.assertIsNone(main._concat_triplet(["888", "Yg==", "", "", ""]))

    def test_engine_too_old_to_send_the_fields(self):
        self.assertIsNone(main._concat_triplet(["888", "Yg=="]))

    def test_malformed_values_are_ignored(self):
        for bad in (["x", "3", "1"],            # not a number
                    ["155", "1", "1"],          # total < 2 is not a concatenation
                    ["155", "3", "0"],          # sequence numbers start at 1
                    ["155", "3", "4"],          # sequence past the total
                    ["155", "3", ""]):          # partially filled
            self.assertIsNone(main._concat_triplet(["888", "Yg=="] + bad), bad)


class GapMarkingTests(unittest.TestCase):
    def test_complete_message_has_no_marker(self):
        self.assertEqual(main._join_sms_parts(CTEXCEL, [1, 2, 3], 3), "".join(CTEXCEL))

    def test_missing_middle_part_is_marked_once(self):
        joined = main._join_sms_parts(["a", "c"], [1, 3], 3)
        self.assertEqual(joined, "a" + main.SMS_GAP_MARK + "c")

    def test_consecutive_missing_parts_collapse_into_one_marker(self):
        joined = main._join_sms_parts(["a", "d"], [1, 4], 4)
        self.assertEqual(joined, "a" + main.SMS_GAP_MARK + "d")

    def test_missing_head_and_tail_are_both_marked(self):
        joined = main._join_sms_parts(["b"], [2], 3)
        self.assertEqual(joined, main.SMS_GAP_MARK + "b" + main.SMS_GAP_MARK)


def _event(instance, sender, body, triplet=None):
    args = [sender, base64.b64encode(body.encode()).decode()]
    if triplet is not None:
        args += [str(x) for x in triplet]
    return {"instance": instance, "event": "sms_in", "args": args}


class InboundEventTests(unittest.IsolatedAsyncioTestCase, TempStore):
    def setUp(self):
        TempStore.setUp(self)
        self.broadcast = AsyncMock()
        self.push = Mock()
        for target, attr, new in ((main.hub, "broadcast", self.broadcast),
                                  (main, "_dispatch_push", self.push)):
            p = patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    def _stored(self):
        with store._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT peer,body FROM messages ORDER BY id")]

    async def test_three_parts_become_one_message_pushed_once(self):
        # delivered 2, 1, 3 — exactly how the real message arrived
        for seq in (2, 1):
            result = await main.api_engine_event(
                _event("7", "888", CTEXCEL[seq - 1], (155, 3, seq)))
            self.assertIn("buffered", result)
        await main.api_engine_event(_event("7", "888", CTEXCEL[2], (155, 3, 3)))

        self.assertEqual(self._stored(), [{"peer": "888", "body": "".join(CTEXCEL)}])
        self.assertEqual(self.broadcast.await_count, 1)
        self.assertEqual(self.push.call_count, 1)
        self.assertIn("uk/trail/index/1", self.push.call_args[0][3])

    async def test_single_part_sms_is_stored_immediately(self):
        await main.api_engine_event(_event("7", "888", "Your balance is £1.01.", ("", "", "")))
        self.assertEqual(self._stored(), [{"peer": "888", "body": "Your balance is £1.01."}])
        self.assertEqual(self.push.call_count, 1)

    async def test_engine_without_the_patch_still_stores_messages(self):
        await main.api_engine_event(_event("7", "888", "legacy engine"))
        self.assertEqual(self._stored(), [{"peer": "888", "body": "legacy engine"}])

    async def test_empty_single_part_message_is_still_dropped(self):
        result = await main.api_engine_event(_event("7", "20023", "  ", ("", "", "")))
        self.assertEqual(result.get("dropped"), "empty_body")
        self.assertEqual(self._stored(), [])

    async def test_a_part_that_decoded_to_nothing_is_kept_so_the_group_can_complete(self):
        await main.api_engine_event(_event("7", "888", "", (9, 2, 1)))
        await main.api_engine_event(_event("7", "888", "tail", (9, 2, 2)))
        self.assertEqual(self._stored(), [{"peer": "888", "body": "tail"}])

    async def test_incomplete_group_is_published_by_the_sweeper_with_the_gap_marked(self):
        await main.api_engine_event(_event("7", "888", CTEXCEL[0], (155, 3, 1)))
        await main.api_engine_event(_event("7", "888", CTEXCEL[2], (155, 3, 3)))
        self.assertEqual(self._stored(), [])

        stale = store.take_stale_sms_segments(timeout=0)
        for group in stale:
            body = main._join_sms_parts(group["bodies"], group["seqs"], group["total"])
            store.add_message(group["instance"], "in", group["peer"], body,
                              ts=group["first_ts"])
        self.assertEqual(self._stored(),
                         [{"peer": "888", "body": CTEXCEL[0] + main.SMS_GAP_MARK + CTEXCEL[2]}])


if __name__ == "__main__":
    unittest.main()

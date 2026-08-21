"""Parsing the USSD reply a carrier returns for a dialled service code.

The payloads here are the real thing: captured off line 1 (T-Mobile US) on 2026-08-21, where
`#225#` was answered with application/vnd.3gpp.ussd+xml on the BYE that ended the call.
"""
import base64
import unittest

from control.app import ussd

REAL_TMOBILE = (
    "<ussd-data>\n"
    "<error-code>0</error-code>\n"
    "<language>en-US</language>\n"
    "<ussd-string>Thank you, your request is being processed. "
    "A message will be sent to your phone</ussd-string>\n"
    "</ussd-data>\n"
)


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class UssdParseTests(unittest.TestCase):
    def test_parses_the_payload_captured_from_the_carrier(self):
        got = ussd.parse(b64(REAL_TMOBILE))
        self.assertIsNotNone(got)
        self.assertEqual(
            got["text"],
            "Thank you, your request is being processed. "
            "A message will be sent to your phone")
        self.assertEqual(got["error_code"], "0")
        self.assertEqual(got["language"], "en-US")

    def test_namespaced_xml_is_understood(self):
        # Carriers differ on whether they namespace the payload; the tag name is what matters.
        got = ussd.parse(b64(
            '<ussd-data xmlns="urn:3gpp:ns:ussd">'
            "<ussd-string>Balance $5.00</ussd-string></ussd-data>"))
        self.assertEqual(got["text"], "Balance $5.00")

    def test_malformed_xml_still_yields_the_reply(self):
        # A truncated body must not cost the user a reply that is plainly readable in it.
        got = ussd.parse(b64("<ussd-data><ussd-string>Balance $5.00</ussd-string>"))
        self.assertEqual(got["text"], "Balance $5.00")

    def test_absent_payload_is_not_an_error(self):
        # Every ordinary call reaches this path with an empty field.
        for empty in ("", "   ", None):
            self.assertIsNone(ussd.parse(empty))

    def test_payload_without_a_string_is_ignored(self):
        # An error-code with no text is nothing to show the user.
        self.assertIsNone(ussd.parse(b64(
            "<ussd-data><error-code>1</error-code></ussd-data>")))

    def test_non_base64_is_treated_as_the_payload(self):
        # An engine image predating the base64 wrapping forwards the XML verbatim; a reply
        # that did arrive must not be thrown away over its encoding.
        got = ussd.parse("<ussd-data><ussd-string>Plain</ussd-string></ussd-data>")
        self.assertEqual(got["text"], "Plain")

    def test_oversized_payload_is_bounded(self):
        # Nothing unbounded may reach the database from the network.
        got = ussd.parse(b64(
            "<ussd-data><ussd-string>" + ("A" * 9000) + "</ussd-string></ussd-data>"))
        self.assertLessEqual(len(got["text"]), ussd.MAX_TEXT)

    def test_undecodable_bytes_do_not_raise(self):
        # This runs on the engine event path: a bad payload must never cost the call its
        # disposition, so it degrades to "no reply" instead of propagating.
        self.assertIsNone(ussd.parse(base64.b64encode(b"\xff\xfe\x00").decode()))

    def test_whitespace_in_the_reply_is_collapsed(self):
        got = ussd.parse(b64(
            "<ussd-data><ussd-string>Line one\n   Line two</ussd-string></ussd-data>"))
        self.assertEqual(got["text"], "Line one Line two")


class UssdPatchTests(unittest.TestCase):
    """The capture only happens if the engine patch keeps its two load-bearing properties."""

    def setUp(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        self.patch = (root / "engine" / "patches" / "asterisk"
                      / "ussd_body_capture.py").read_text(encoding="utf-8")
        self.dialplan = (root / "engine" / "templates"
                         / "extensions.conf.j2").read_text(encoding="utf-8")

    def test_capture_runs_before_the_supplements(self):
        # The BYE supplement ends the session and takes session->channel with it, so
        # capturing after the traverse would find nothing to write to.
        body = self.patch[self.patch.index("static void handle_incoming_request"):]
        self.assertLess(body.index("ussd_capture_body(session, rdata)"),
                        body.index("AST_LIST_TRAVERSE"))

    def test_only_ussd_bodies_are_touched(self):
        self.assertIn('pj_stricmp2(&body->content_type.subtype, "vnd.3gpp.ussd+xml")',
                      self.patch)

    def test_stack_copy_is_bounded(self):
        # ast_alloca puts this on the stack; an unbounded Content-Length must not reach it.
        self.assertIn("body->len > USSD_BODY_MAX", self.patch)

    def test_dialplan_base64_encodes_the_payload(self):
        # Raw XML carries newlines and quotes that would not survive the shell.
        self.assertIn('"${BASE64_ENCODE(${USSD_RESPONSE})}"', self.dialplan)

    def test_payload_is_reported_from_the_carrier_leg_not_the_caller_leg(self):
        # The payload rides an in-dialog request to the leg talking to the carrier. The 'h'
        # handler runs on the CALLER's leg and cannot read another channel's variables, so
        # reporting it there yielded an empty field on every real call. Regression guard.
        self.assertIn("Set(CHANNEL(hangup_handler_push)=ussd-report,s,1)", self.dialplan)
        self.assertIn("[ussd-report]", self.dialplan)
        self.assertIn("notify.py ussd ", self.dialplan)
        h_handler = self.dialplan[self.dialplan.index("notify.py call_result out"):]
        self.assertNotIn("USSD_RESPONSE", h_handler.split("\n")[0])

    def test_dialled_code_is_inheritable_by_the_carrier_leg(self):
        # Without the leading underscore the carrier leg cannot name what it answered, and
        # the reply could not be matched to a call record.
        self.assertIn("Set(_USSDPEER=${EXTEN})", self.dialplan)

    def test_ordinary_calls_report_nothing(self):
        # Every normal call runs this handler too; it must be a no-op when there is no reply.
        self.assertIn('GotoIf($["${USSD_RESPONSE}" = ""]?done)', self.dialplan)


class CallResultRaceTests(unittest.IsolatedAsyncioTestCase):
    """call_out and call_result are fired by separate backgrounded processes, so nothing
    orders them. A service code answered on the BYE lasts under a second, which is short
    enough for the result to land first — it used to be dropped, stranding the call on
    'dialing' with its reply already attached."""

    async def test_result_arriving_before_its_record_is_applied_on_retry(self):
        from unittest.mock import AsyncMock, patch
        from control.app import main

        calls = []

        def update(iid, direction, peer, disp):
            calls.append(peer)
            # Miss the first two lookups, as if call_out had not been processed yet.
            if len(calls) <= 2:
                return None
            return {"id": 7, "instance": iid, "peer": "#225#", "status": disp}

        with patch.object(main.store, "update_last_call", side_effect=update), \
                patch.object(main.hub, "broadcast", new=AsyncMock()) as bc, \
                patch.object(main.cfg, "get_instance", return_value={"id": "1"}), \
                patch.object(main.asyncio, "sleep", new=AsyncMock()):
            await main.api_engine_event({
                "instance": "1", "event": "call_result",
                "args": ["out", "#225#", "ANSWER", "16"]})

        self.assertGreater(len(calls), 2, "should have retried past the initial misses")
        bc.assert_awaited()
        self.assertEqual(bc.await_args.args[0]["call"]["status"], "code accepted")


if __name__ == "__main__":
    unittest.main()

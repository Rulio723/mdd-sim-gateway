"""Answer the SMSC's report on a message we submitted without filing it as an inbound SMS.

After an MO submission the SMSC reports the outcome asynchronously, as a MESSAGE carrying an
RP-ACK (network->MS, 0x03) or RP-ERROR (network->MS, 0x05).  The fork recognised neither: both
fell through to the "Unknown RP-DATA" branch, so the report was parsed as far as the hex dump
and then handed to the messaging core anyway.  With no TPDU to decode the message reached the
dialplan empty, and every submitted segment produced one bodyless "IN SMS" record, a
BASE64_ENCODE-with-no-argument warning, and a "dropping empty-body inbound SMS" line in the
control plane.  A six-segment text therefore wrote six phantom inbound messages.

Reports are now recognised: they are answered (an unanswered report is repeated), and they stop
before the messaging core instead of being queued.  RP-ERROR is logged with its RP cause, which
is the only place the refusal reason appears.  The DEBUG hex dump above is deliberately left
untouched — the control plane reads it (RPDATA_RE in control/app/main.py) to move the stored
message from 'sent' to 'delivered' or 'failed', so removing it would silently break delivery
reporting.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip_messaging.c"

MARKER = "PATCH mo_submit_report"

# parse_rpdata's signature. The constant goes above it because both parse_rpdata and
# module_on_rx_request use it; kept verbatim so an upstream rename fails the build.
SIGNATURE = "static void parse_rpdata(pjsip_rx_data *rdata, struct ast_msg *msg, int *ack_ref)\n"

SENTINEL = (
    "/* " + MARKER + ": parse_rpdata reports an RP-ACK/RP-ERROR — the SMSC's verdict on a\n"
    " * message WE submitted — through ack_ref, which otherwise carries the reference an\n"
    " * inbound RP-DATA has to be acknowledged with. A reference is never negative, and -1 is\n"
    " * already taken to mean \"nothing to acknowledge\". */\n"
    "#define MDD_RP_SUBMIT_REPORT (-2)\n"
    "\n"
)

# The branch that used to treat a submit report as an unknown message type.
UNKNOWN_BRANCH = (
    "\tcase 0x03: /* RP-ACK */\n"
    "\tcase 0x05: /* RP-ERROR */\n"
    "\tdefault:\n"
    '\t\tast_log(LOG_WARNING, "Unknown RP-DATA 0x%02x. Dropping message\\n", buf[0]);\n'
    "\t\treturn;\n"
)

REPORT_BRANCH = (
    "\tcase 0x03:\n"
    "\t\t/* " + MARKER + ": RP-ACK, the SMSC accepted a message we submitted. One\n"
    "\t\t * arrives per submitted segment, so this stays at debug level. */\n"
    "\t\t*ack_ref = MDD_RP_SUBMIT_REPORT;\n"
    '\t\tast_log(LOG_DEBUG, "SMS submit report: RP-ACK, reference %d.\\n", buf[1] & 0xff);\n'
    "\t\treturn;\n"
    "\tcase 0x05:\n"
    "\t\t/* " + MARKER + ": RP-ERROR, the SMSC refused a message we submitted. The RP\n"
    "\t\t * cause (octet 3, bit 8 is the extension flag) is the only statement of why. */\n"
    "\t\t*ack_ref = MDD_RP_SUBMIT_REPORT;\n"
    '\t\tast_log(LOG_WARNING, "SMS submit report: RP-ERROR, reference %d, RP cause %d.\\n",\n'
    "\t\t\tbuf[1] & 0xff, (len >= 4) ? (buf[3] & 0x7f) : -1);\n"
    "\t\treturn;\n"
    "\tdefault:\n"
    '\t\tast_log(LOG_WARNING, "Unknown RP-DATA 0x%02x. Dropping message\\n", buf[0]);\n'
    "\t\treturn;\n"
)

# Everything the inbound path does once the message has been built. The report has to leave
# before ast_msg_has_destination, which answers 404 when no context claims the message.
QUEUE_PROLOGUE = (
    "\tcode = rx_data_to_ast_msg(rdata, msg, is_sms, &ack_ref);\n"
    "\tif (code != PJSIP_SC_OK) {\n"
    "\t\tsend_response(rdata, code, NULL, NULL);\n"
    "\t\tast_msg_destroy(msg);\n"
    "\t\treturn PJ_TRUE;\n"
    "\t}\n"
)

STOP_REPORT = (
    "\n"
    "\t/* " + MARKER + ": a submit report is the SMSC answering us, not a message for us.\n"
    "\t * Answer it — an unanswered report is repeated — but never queue it: it carries no\n"
    "\t * TPDU, so the messaging core would file one empty inbound SMS per submitted segment.\n"
    "\t * The hex dump parse_rpdata logged is what the control plane reads to turn the stored\n"
    "\t * message from 'sent' into 'delivered' or 'failed'. */\n"
    "\tif (ack_ref == MDD_RP_SUBMIT_REPORT) {\n"
    "\t\tsend_response(rdata, PJSIP_SC_OK, NULL, NULL);\n"
    "\t\tast_msg_destroy(msg);\n"
    "\t\treturn PJ_TRUE;\n"
    "\t}\n"
)


def _replace_once(source, old, new, what):
    at = source.find(old)
    if at < 0:
        raise ValueError(f"{what} not found")
    if source.find(old, at + 1) >= 0:
        raise ValueError(f"{what} is not unique")
    return source[:at] + new + source[at + len(old):]


def patch(source):
    if MARKER in source:
        return source

    source = _replace_once(source, SIGNATURE, SENTINEL + SIGNATURE,
                           "parse_rpdata signature")
    source = _replace_once(source, UNKNOWN_BRANCH, REPORT_BRANCH,
                           "the unknown-RP-DATA branch")
    source = _replace_once(source, QUEUE_PROLOGUE, QUEUE_PROLOGUE + STOP_REPORT,
                           "the inbound MESSAGE prologue")
    return source


try:
    original = SOURCE.read_text()
    updated = patch(original)
except (OSError, ValueError) as exc:
    print(f"MO submit report patch failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

if updated == original:
    print("MO submit report handling already patched")
else:
    SOURCE.write_text(updated)
    print("patched parse_rpdata to recognise RP-ACK/RP-ERROR submit reports")

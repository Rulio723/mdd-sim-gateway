"""Log the P-Associated-URI of every successful REGISTER so the line number can be read.

The control plane learns (and re-checks) a line's phone number from the public identity the
carrier binds at registration.  Asterisk keeps that value in the transport state only, so the
only way to observe it used to be the PJSIP packet logger — which meant enabling SIP tracing
and FORCING an extra REGISTER just to produce a fresh 200 OK to read.  Some IMS cores answer
that unsolicited re-registration with "503 Service Unavailable" (issue #8, MCC 234 MNC 010),
which Asterisk reports as a rejected registration and the health policy then tears the line
down — a healthy VoWiFi connection killed by the act of asking for its own number.

The value is already parsed on the normal path (store_volte_p_associated_uri), so log it
there instead.  Ordinary registrations and their periodic refreshes emit it, nothing extra is
ever sent, and the packet logger — which also writes authentication headers to the container
log — stays off.  Every P-Associated-URI header of the response is logged, not just the first
entry Asterisk keeps: carriers that list an IMSI-derived IMPU first carry the dialable number
in a later `tel:` entry.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip_outbound_registration.c"

MARKER = "PATCH ims_public_identity_log"

# Start of the function body that already holds the parsed response. Kept verbatim so a
# refactor upstream fails the build loudly instead of silently dropping the log line.
ANCHOR = (
    "static int store_volte_p_associated_uri(struct registration_response *response)\n"
    "{\n"
    "\tstruct ast_sip_transport_state *transport_state = NULL;\n"
    "\tint ret = -1;\n"
    "\n"
)

LOG_IDENTITIES = (
    "\t/* " + MARKER + ": announce the public identities the carrier bound to this\n"
    "\t * registration.  The control plane reads the line's phone number from this NOTICE, so\n"
    "\t * it never has to enable SIP tracing or force an extra REGISTER (which some IMS cores\n"
    "\t * answer with 503 and Asterisk then reports as a rejected registration). */\n"
    "\t{\n"
    '\t\tstatic const pj_str_t str_p_associated_uri = { "P-Associated-URI", 16 };\n'
    "\t\tpjsip_generic_string_hdr *pau = NULL;\n"
    "\t\tchar identities[1024];\n"
    "\t\tsize_t used = 0;\n"
    "\n"
    "\t\twhile ((pau = pjsip_msg_find_hdr_by_name(response->rdata->msg_info.msg,\n"
    "\t\t\t\t&str_p_associated_uri, pau ? pau->next : NULL))) {\n"
    "\t\t\tsize_t len = (pau->hvalue.slen > 0) ? (size_t) pau->hvalue.slen : 0;\n"
    "\n"
    "\t\t\tif (!len || used + len + 2 > sizeof(identities)) {\n"
    "\t\t\t\tbreak;\n"
    "\t\t\t}\n"
    "\t\t\tif (used) {\n"
    "\t\t\t\tidentities[used++] = ',';\n"
    "\t\t\t}\n"
    "\t\t\tmemcpy(identities + used, pau->hvalue.ptr, len);\n"
    "\t\t\tused += len;\n"
    "\t\t}\n"
    "\t\tif (used) {\n"
    "\t\t\tidentities[used] = '\\0';\n"
    '\t\t\tast_log(LOG_NOTICE, "IMS public identity: %s\\n", identities);\n'
    "\t\t}\n"
    "\t}\n"
    "\n"
)


def patch(source: str) -> str:
    if MARKER in source:
        return source

    anchor_at = source.find(ANCHOR)
    if anchor_at < 0:
        raise ValueError("store_volte_p_associated_uri prologue not found")
    if source.find(ANCHOR, anchor_at + 1) >= 0:
        raise ValueError("store_volte_p_associated_uri prologue is not unique")

    insert_at = anchor_at + len(ANCHOR)
    return source[:insert_at] + LOG_IDENTITIES + source[insert_at:]


try:
    original = SOURCE.read_text()
    updated = patch(original)
except (OSError, ValueError) as exc:
    print(f"IMS public identity log patch failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

if updated == original:
    print("IMS public identity logging already patched")
else:
    SOURCE.write_text(updated)
    print("patched store_volte_p_associated_uri to log the registered P-Associated-URI")

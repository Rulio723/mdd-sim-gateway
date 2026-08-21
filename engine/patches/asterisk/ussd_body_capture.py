# SPDX-License-Identifier: GPL-2.0-only
#
# LICENSE EXCEPTION TO THE REPOSITORY DEFAULT. MDD Sim Gateway as a whole is GPL-3.0-only,
# but FIXED_FN below is a modified copy of Asterisk's res_pjsip_session
# handle_incoming_request(), and Asterisk is GPL-2.0-only. A derivative of GPL-2.0-only code
# cannot be relicensed to GPL-3.0, so this file — and the modified Asterisk source it produces
# at build time — stay under GPL-2.0-only. See THIRD_PARTY_LICENSES.md.

import sys

# PATCH_USSD_BODY: expose a 3GPP USSD payload (TS 24.390) carried by an in-dialog request.
#
# A carrier answers a dialled service code (#225#, *#21#) by putting the USSD text in the body
# of a SIP request inside the established dialog. T-Mobile US puts it on the BYE that tears the
# call down — which is why such a "call" lasts under a second and is silent: the answer is
# signalling, never audio. Captured from line 1:
#
#     BYE sip:...  SIP/2.0
#     Content-Type: application/vnd.3gpp.ussd+xml
#     Content-Length: 192
#
#     <ussd-data><error-code>0</error-code><language>en-US</language>
#     <ussd-string>Thank you, your request is being processed...</ussd-string></ussd-data>
#
# Stock Asterisk hands in-dialog requests to the session supplements and never looks at a body
# it has no handler for, so the text is parsed by pjsip, dropped, and the user is shown an
# empty call with no explanation. This copies the raw payload onto the channel as
# USSD_RESPONSE; the dialplan's 'h' extension forwards it to the manager, which parses the XML.
#
# It runs BEFORE the supplements deliberately: the BYE supplement ends the session and takes
# session->channel with it, so capturing afterwards would find nothing to write to.
#
# INFO takes the same path, for a carrier that answers mid-dialog rather than on the BYE. That
# branch is UNVERIFIED — no line available here returns an interactive USSD menu, so it has
# never been exercised against a real network.
#
# Nothing else changes: a request with no body, an oversized body, or any content type other
# than application/vnd.3gpp.ussd+xml takes exactly the previous code path.

FIXED_FN = r'''/* PATCH_USSD_BODY */
#define USSD_BODY_MAX 4096

static void ussd_capture_body(struct ast_sip_session *session, pjsip_rx_data *rdata)
{
	pjsip_msg_body *body;
	char *buf;

	if (!session || !session->channel || !rdata || !rdata->msg_info.msg) {
		return;
	}
	body = rdata->msg_info.msg->body;
	/* Bound the copy: this lands on the stack, and a USSD string is 182 chars at most
	 * (3GPP TS 22.030), so a body beyond USSD_BODY_MAX is not one and is ignored. */
	if (!body || !body->data || !body->len || body->len > USSD_BODY_MAX) {
		return;
	}
	if (pj_stricmp2(&body->content_type.type, "application")
		|| pj_stricmp2(&body->content_type.subtype, "vnd.3gpp.ussd+xml")) {
		return;
	}
	buf = ast_alloca(body->len + 1);
	memcpy(buf, body->data, body->len);
	buf[body->len] = '\0';
	pbx_builtin_setvar_helper(session->channel, "USSD_RESPONSE", buf);
	ast_debug(3, "USSD payload captured (%d bytes) on %s\n", (int) body->len,
		ast_channel_name(session->channel));
}

static void handle_incoming_request(struct ast_sip_session *session, pjsip_rx_data *rdata)
{
	struct ast_sip_session_supplement *supplement;
	struct pjsip_request_line req = rdata->msg_info.msg->line.req;
	SCOPE_ENTER(3, "%s: Method is %.*s\n", ast_sip_session_get_name(session), (int) pj_strlen(&req.method.name), pj_strbuf(&req.method.name));

	/* PATCH_USSD_BODY: before the supplements — the BYE supplement ends the session and
	 * takes session->channel with it. */
	ussd_capture_body(session, rdata);

	AST_LIST_TRAVERSE(&session->supplements, supplement, next) {
		if (supplement->incoming_request && does_method_match(&req.method.name, supplement->method)) {
			if (supplement->incoming_request(session, rdata)) {
				break;
			}
		}
	}

	SCOPE_EXIT("%s\n", ast_sip_session_get_name(session));
}'''

f = '/home/asterisk-build/asterisk/res/res_pjsip_session.c'
if len(sys.argv) > 1:                      # local dry-run against a checkout
    f = sys.argv[1]
s = open(f).read()

if 'PATCH_USSD_BODY' in s:
    print("already patched"); sys.exit(0)

# Match the DEFINITION, not the forward declaration: the declaration ends in ';', the
# definition in a newline followed by the opening brace.
SIG = ('static void handle_incoming_request(struct ast_sip_session *session, '
       'pjsip_rx_data *rdata)\n{')
start = s.find(SIG)
if start < 0:
    print("PATTERN NOT FOUND: handle_incoming_request definition"); sys.exit(1)
if s.find(SIG, start + 1) >= 0:
    print("PATTERN AMBIGUOUS: handle_incoming_request defined more than once"); sys.exit(1)

i = s.find('{', start)
depth = 0
end = -1
for j in range(i, len(s)):
    if s[j] == '{':
        depth += 1
    elif s[j] == '}':
        depth -= 1
        if depth == 0:
            end = j + 1
            break
if end < 0:
    print("BRACE MATCH FAILED"); sys.exit(1)

open(f, 'w').write(s[:start] + FIXED_FN + s[end:])
print("patched OK (handle_incoming_request replaced, %d -> %d bytes)" % (end - start, len(FIXED_FN)))

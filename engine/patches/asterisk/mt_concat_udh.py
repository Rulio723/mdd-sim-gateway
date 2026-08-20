# SPDX-License-Identifier: GPL-2.0-only
#
# LICENSE EXCEPTION TO THE REPOSITORY DEFAULT. MDD Sim Gateway as a whole is GPL-3.0-only,
# but FIXED_FN below is a modified copy of Asterisk's res_pjsip_messaging parse_tpdu(), and
# Asterisk is GPL-2.0-only. A derivative of GPL-2.0-only code cannot be relicensed to
# GPL-3.0, so this file — and the modified Asterisk source it produces at build time — stay
# under GPL-2.0-only. See THIRD_PARTY_LICENSES.md.

import sys

# PATCH_CONCAT_UDH: expose the concatenated-SMS segment triplet on an inbound (MT) SMS.
#
# A message longer than one SMS is split by the SMSC into several SMS-DELIVER PDUs, each
# carrying a User Data Header that says which part it is (3GPP TS 23.040 9.2.3.24.1/.8).
# Stock parse_tpdu() already unpacks that header — unpacksms() fills `udh`/`udhl` — but then
# drops it on the floor ("TODO: udh, scts") and sets the body to the fragment's own text. So
# every part surfaced as its own standalone message and the user saw one text arrive as N
# unordered pieces (the parts are not even guaranteed to arrive in order).
#
# Reassembly itself is NOT done here: it needs state across separate SIP transactions, and the
# RP-ACK for each part must go back immediately. Instead this exposes ref/total/seq as message
# variables, which the dialplan forwards to the manager (control/app/main.py), where the parts
# are buffered in SQLite and merged into a single message.
#
# The vars are set ONLY for a well-formed segment of a real multi-part message, so an ordinary
# single-part SMS leaves them empty and takes exactly the previous code path.

FIXED_FN = r'''static void parse_tpdu(struct ast_msg *msg, unsigned char *tpdu, int tpdu_len)
{
	/* PATCH_CONCAT_UDH */
	if (tpdu_len < 2)
	{
		return;
	}
	if (tpdu[0] & 3)
	{
		ast_log(LOG_WARNING, "Unhandled PDU type %x\n", tpdu[0] & 3);
		return;
	}
	/*int srr = ((tpdu[0] & 0x20) ? 1 : 0);*/
	int udhi = ((tpdu[0] & 0x40) ? 1 : 0);
	/*int rp = ((tpdu[0] & 0x80) ? 1 : 0);*/
	int p = 1;
	char oa[300];
	p += unpackaddress(oa, tpdu + p, sizeof(oa));
	if (p + 9 > tpdu_len)
		return;
	/*int pid = tpdu[p++] */p++;
	int dcs = tpdu[p++];
	struct timeval scts = unpackdate(tpdu + p);
	p += 7;
	unsigned short ud[300];
	/* Zero-initialised: on a malformed header unpacksms() can report a udhl longer than the
	 * bytes it actually copied, and the IE walk below would otherwise read uninitialised
	 * stack. Reading zeros instead just ends the walk without a match. */
	unsigned char udh[300] = { 0 };
	int udhl, udl;
	p += unpacksms(dcs, tpdu + p, udh, &udhl, ud, &udl, udhi);
	ud[udl] = 0;

	char buf2[300 * 4 + 5];
	utf16_to_utf8(ud, udl, buf2, sizeof(buf2));
	ast_log(LOG_DEBUG, "SMS UD='%s' OA='%s'.\n", buf2, oa);

	/* Concatenation IE. unpacksms() leaves `udh` holding the IE bytes AFTER the UDHL octet,
	 * and `udhl` is the length of that IE area. Walk the IEs looking for the concatenation
	 * one: IEI 0x00 = 8-bit reference (3 bytes: ref, total, seq), IEI 0x08 = 16-bit
	 * reference (4 bytes: ref-high, ref-low, total, seq). Any other IE (national language
	 * shift, port addressing, …) is skipped by its own length. */
	if (udhi && udhl > 0) {
		int k = 0;
		if (udhl > (int) sizeof(udh)) {
			udhl = (int) sizeof(udh);
		}
		while (k + 2 <= udhl) {
			int iei = udh[k];
			int iedl = udh[k + 1];
			unsigned char *ie = udh + k + 2;
			int ref = -1, total = 0, seq = 0;

			if (k + 2 + iedl > udhl) {
				break;                 /* truncated IE — nothing trustworthy follows */
			}
			if (iei == 0x00 && iedl == 3) {
				ref = ie[0];
				total = ie[1];
				seq = ie[2];
			} else if (iei == 0x08 && iedl == 4) {
				ref = (ie[0] << 8) | ie[1];
				total = ie[2];
				seq = ie[3];
			}
			/* A usable segment has total >= 2 and 1 <= seq <= total. Anything else is not a
			 * concatenation we can reassemble: leave the vars unset so the part is delivered
			 * on its own, exactly as before this patch. */
			if (ref >= 0 && total >= 2 && seq >= 1 && seq <= total) {
				char nbuf[16];
				snprintf(nbuf, sizeof(nbuf), "%d", ref);
				ast_msg_set_var(msg, "SMS_CONCAT_REF", nbuf);
				snprintf(nbuf, sizeof(nbuf), "%d", total);
				ast_msg_set_var(msg, "SMS_CONCAT_TOTAL", nbuf);
				snprintf(nbuf, sizeof(nbuf), "%d", seq);
				ast_msg_set_var(msg, "SMS_CONCAT_SEQ", nbuf);
				ast_log(LOG_DEBUG, "SMS concat ref=%d part %d/%d.\n", ref, seq, total);
				break;
			}
			k += 2 + iedl;
		}
	}

	char buf_scts[30];
	snprintf(buf_scts, sizeof(buf_scts), "%lld", (long long) scts.tv_sec);
	ast_msg_set_var(msg, "SMS_SMSC_TIMESTAMP", buf_scts);
	ast_msg_set_from(msg, "%s", oa);
	ast_msg_set_body(msg, "%s", buf2);
}'''

f = '/home/asterisk-build/asterisk/res/res_pjsip_messaging.c'
s = open(f).read()
if 'PATCH_CONCAT_UDH' in s:
    print("already patched"); sys.exit(0)

start = s.find('static void parse_tpdu(struct ast_msg *msg, unsigned char *tpdu, int tpdu_len)')
if start < 0:
    print("PATTERN NOT FOUND: parse_tpdu signature"); sys.exit(1)
# brace-match to find the end of the function
i = s.find('{', start)
depth = 0
end = -1
for j in range(i, len(s)):
    if s[j] == '{': depth += 1
    elif s[j] == '}':
        depth -= 1
        if depth == 0:
            end = j + 1
            break
if end < 0:
    print("BRACE MATCH FAILED"); sys.exit(1)

s2 = s[:start] + FIXED_FN + s[end:]
open(f, 'w').write(s2)
print("patched OK (parse_tpdu replaced, %d -> %d bytes)" % (end - start, len(FIXED_FN)))

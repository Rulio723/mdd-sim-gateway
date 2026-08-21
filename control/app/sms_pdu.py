"""Telling a text message apart from a machine payload that merely looks like one.

Not every SMS is meant for a person. Carriers and services also send binary ones — SIM
OTA data-download, silent app pushes, WAP push — whose user data is arbitrary bytes, not
characters. The PDU says which is which in TP-DCS (3GPP TS 23.038 section 4) and TP-PID
(TS 23.040 9.2.3.9), but Asterisk's parse_tpdu() unpacks an 8-bit payload one byte per
character and hands back a string, so by the time the manager sees it a binary payload is
indistinguishable from a text that happens to be mojibake. Line 1 collected 16 of these
from short-codes 2942/2939 before the engine started forwarding the header fields.

The engine's Asterisk patch (engine/patches/asterisk/mt_concat_udh.py) now exposes TP-PID,
TP-DCS, the UDH and the raw PDU; this module turns those into the one decision the manager
needs — is this a text to show, or a payload to file away — and falls back to inspecting the
decoded body for an engine image that predates the patch.
"""
from __future__ import annotations

from dataclasses import dataclass

# Characters a real text can legitimately contain despite being control codes. Everything
# else below 0x20 is a byte that happened to land in a string, not something a person typed.
_TEXT_CONTROLS = "\n\r\t\v\f"


def _is_stray_byte(ch: str) -> bool:
    """A code point that only a widened payload byte can put in a string.

    Two ranges, and the second one matters more than it looks: C0 below 0x20, and DEL plus
    the C1 block (0x7F-0x9F). Neither of Asterisk's text paths can produce a C1 code point —
    the GSM 7-bit table maps to printable characters and Greek letters, and real UCS-2 text
    does not contain C1 controls — whereas a random payload byte lands there 1 time in 8.
    Without the C1 range an 8-byte payload has roughly a one-in-three chance of containing no
    marker at all and passing as text (payload c5e79b8fb7572767 on line 1 did exactly that).
    """
    o = ord(ch)
    return (o < 0x20 and ch not in _TEXT_CONTROLS) or 0x7F <= o <= 0x9F


def dcs_is_8bit(dcs: int) -> bool:
    """Does this TP-DCS say the user data is 8-bit binary rather than characters?

    Two of the coding groups in TS 23.038 4.3 can carry 8-bit data:
      00xxxxxx  general data coding — the character set is bits 3-2 (01 = 8-bit)
      1111xxxx  data coding / message class — bit 2 (1 = 8-bit)
    The groups in between (0100-1110: compressed, reserved, message-waiting) are all text.
    """
    if dcs >> 6 == 0b00:
        return (dcs >> 2) & 0b11 == 0b01
    if dcs >> 4 == 0b1111:
        return bool(dcs & 0b100)
    return False


def dcs_message_class(dcs: int) -> int | None:
    """The message class in TP-DCS, or None when the DCS does not carry one.

    Class 2 is the interesting one: TS 23.038 calls it "(U)SIM specific message", and a
    handset hands it to the card instead of showing it. Present only when bit 4 is set in
    the general group, and always present in the 1111 group.
    """
    if dcs >> 6 == 0b00 and dcs & 0b10000:
        return dcs & 0b11
    if dcs >> 4 == 0b1111:
        return dcs & 0b11
    return None


# TP-PID 0x7F = "(U)SIM Data download": the payload is addressed to the card, not the user.
PID_SIM_DATA_DOWNLOAD = 0x7F


@dataclass(frozen=True)
class PduMeta:
    """What the engine reported about one inbound PDU. Every field is optional because an
    engine image built before the patch sends none of them."""
    tp_pid: int | None = None
    tp_dcs: int | None = None
    udh_hex: str = ""
    tpdu_hex: str = ""

    @property
    def known(self) -> bool:
        return self.tp_dcs is not None

    @property
    def message_class(self) -> int | None:
        return None if self.tp_dcs is None else dcs_message_class(self.tp_dcs)

    @property
    def is_machine_payload(self) -> bool:
        """True for a PDU that was never meant to be displayed.

        Three independent markers, any one of which is enough: 8-bit user data (it is not
        text at all), message class 2 (addressed to the SIM), and TP-PID 0x7F (SIM data
        download). A carrier setting only one of them is normal, so they are OR-ed.
        """
        if self.tp_dcs is None:
            return False
        return (dcs_is_8bit(self.tp_dcs)
                or self.message_class == 2
                or self.tp_pid == PID_SIM_DATA_DOWNLOAD)


# Why a payload was filed, as stable tags the UI turns into wording. Derived here rather than
# in the browser so the TS 23.038/23.040 rules have exactly one implementation.
TAG_8BIT = "8bit"                  # user data is bytes, not characters
TAG_SIM_CLASS = "sim_class"        # message class 2 — addressed to the card
TAG_SIM_DOWNLOAD = "sim_download"  # TP-PID 0x7F — SIM data download
TAG_UNREPORTED = "unreported"      # engine predates the PDU-header patch; classified by content


def payload_tags(tp_pid: int | None, tp_dcs: int | None) -> list[str]:
    """Which markers made this payload non-text. Empty is impossible for a filed row: a row
    with no PDU header at all gets TAG_UNREPORTED so the UI never shows a blank reason."""
    if tp_dcs is None:
        return [TAG_UNREPORTED]
    tags = []
    if dcs_is_8bit(tp_dcs):
        tags.append(TAG_8BIT)
    if dcs_message_class(tp_dcs) == 2:
        tags.append(TAG_SIM_CLASS)
    if tp_pid == PID_SIM_DATA_DOWNLOAD:
        tags.append(TAG_SIM_DOWNLOAD)
    return tags or [TAG_UNREPORTED]


def parse_event_args(args: list) -> PduMeta:
    """Read the PDU header fields trailing an sms_in event.

    Argument layout, appended by the dialplan in this order:
        0 from  1 body-b64  2 concat-ref  3 concat-total  4 concat-seq
        5 tp-pid  6 tp-dcs  7 udh-hex  8 tpdu-hex
    An older engine sends only the first five (or only the first two), and Asterisk passes an
    empty string for a variable it never set — so anything missing or unparseable yields an
    empty PduMeta and the caller falls back to looks_binary().
    """
    def number(index: int) -> int | None:
        if len(args) <= index:
            return None
        try:
            return int(str(args[index]).strip())
        except (TypeError, ValueError):
            return None

    def hexed(index: int) -> str:
        if len(args) <= index:
            return ""
        value = str(args[index]).strip().lower()
        if not value or len(value) % 2 or any(ch not in "0123456789abcdef" for ch in value):
            return ""
        return value

    dcs = number(6)
    if dcs is not None and not 0 <= dcs <= 0xFF:
        dcs = None
    pid = number(5)
    if pid is not None and not 0 <= pid <= 0xFF:
        pid = None
    return PduMeta(tp_pid=pid, tp_dcs=dcs, udh_hex=hexed(7), tpdu_hex=hexed(8))


def looks_binary(text: str) -> bool:
    """Fallback classification from the decoded body alone, for an engine that does not
    report TP-DCS yet — and for the messages already in the database from before it did.

    Asterisk's 8-bit path widens each payload byte to one character, so a binary payload
    arrives as a string whose code points are all below 0x100 and which is peppered with
    code points no text would contain. Requiring such a marker keeps this off ordinary text:
    accented Latin, Greek and CJK all pass through the first test only.

    This stays a heuristic. A short payload made entirely of printable bytes is
    indistinguishable from a text here and will be shown as a message — which is precisely
    why the engine now reports TP-DCS, and why this runs only when it does not.
    """
    if not text:
        return False
    if any(ord(ch) > 0xFF for ch in text):
        return False               # a real character set — nothing a byte widen could make
    return any(_is_stray_byte(ch) for ch in text)


def body_to_hex(text: str) -> str:
    """The bytes behind a body Asterisk widened byte-per-character, or '' if it is real text.

    This is what lets an already-stored payload be recovered exactly: the widen is
    reversible as long as every code point still fits in a byte.
    """
    try:
        return text.encode("latin-1").hex()
    except (UnicodeEncodeError, AttributeError):
        return ""

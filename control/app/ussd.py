"""Parsing a 3GPP USSD payload (TS 24.390) returned for a dialled service code.

A carrier answers a service code (#225#, *#21#) by putting the reply in the body of a SIP
request inside the established dialog rather than in audio, so the "call" is silent and lasts
under a second. The engine patch copies that body onto the channel and the dialplan forwards
it here base64-encoded, because the payload is XML whose newlines and quotes would not survive
being passed through a shell.

Observed from T-Mobile US on line 1:

    <ussd-data>
      <error-code>0</error-code>
      <language>en-US</language>
      <ussd-string>Thank you, your request is being processed...</ussd-string>
    </ussd-data>
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import xml.etree.ElementTree as ET

log = logging.getLogger("vowifi.ussd")

# 3GPP TS 22.030 caps a USSD string at 182 characters. Allow well beyond that for XML
# wrapping and multi-byte text, but never let an unbounded body through to the database.
MAX_TEXT = 2048


def _strip_ns(tag: str) -> str:
    """'{urn:3gpp:ns}ussd-string' -> 'ussd-string'. Carriers differ on namespacing."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def decode_payload(encoded: str) -> str:
    """Base64 from the dialplan -> raw XML. Empty string when there was no payload."""
    value = str(encoded or "").strip()
    if not value:
        return ""
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        # Not base64: an older engine image forwards the field verbatim. Treat it as the
        # payload rather than discarding a reply we did receive.
        return value[:MAX_TEXT]
    # Decode strictly, and try UCS-2 as well: 3GPP TS 23.038 allows a USSD string in either
    # GSM 7-bit (ASCII-compatible here) or 16-bit. Decoding with errors="replace" instead
    # would turn a non-text payload into a wall of U+FFFD and then show it to the user as
    # though the carrier had said it.
    for codec in ("utf-8", "utf-16", "utf-16-be"):
        try:
            text = raw.decode(codec)
        except (UnicodeDecodeError, ValueError):
            continue
        if "\ufffd" not in text:
            return text[:MAX_TEXT]
    log.warning("USSD payload is not decodable text (%d bytes); ignored", len(raw))
    return ""


def parse(encoded: str) -> dict | None:
    """Return {'text', 'error_code', 'language'} or None if there is no usable reply.

    Returns None rather than raising for anything malformed: this runs on the engine event
    path, where a bad payload must not cost the call its disposition.
    """
    xml = decode_payload(encoded)
    if not xml:
        return None

    text, error_code, language = "", None, ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # Some carriers send the bare string, or XML we cannot model. Salvage the one field
        # that matters instead of dropping the whole reply.
        match = re.search(r"<ussd-string>(.*?)</ussd-string>", xml, re.S | re.I)
        text = (match.group(1) if match else xml).strip()
    else:
        for node in root.iter():
            tag = _strip_ns(node.tag)
            value = (node.text or "").strip()
            if tag == "ussd-string":
                text = value
            elif tag == "error-code":
                error_code = value
            elif tag == "language":
                language = value

    text = " ".join(text.split())[:MAX_TEXT]
    if not text:
        return None
    result = {"text": text, "error_code": error_code, "language": language}
    log.info("USSD reply parsed (%d chars, error-code=%s)", len(text), error_code)
    return result

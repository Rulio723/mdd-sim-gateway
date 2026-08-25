"""Carrier allowance query rules and conservative parsing of explicit SMS replies."""
from __future__ import annotations

import re
import time
from datetime import date, datetime

from . import store

QUERY_WINDOW_SECONDS = 120
MAX_VALUE_LENGTH = 160
MAX_QUERY_BODY_LENGTH = 500

DEFAULT_RULES = {
    "ultramobile": {"carrier_label": "Ultra Mobile", "recipient": "6700", "body": "BAL"},
    "ctexcel": {"carrier_label": "CTExcel", "recipient": "888", "body": "BAL"},
}


def carrier_key(instance: dict | None, resolved: dict | None = None) -> str:
    instance = instance or {}
    resolved = resolved or {}
    # Line names are user-editable and therefore never participate in an automatic SMS action.
    # Ultra is identified by AOSP's specific MCC/MNC+GID rule. CTExcel is not currently in the
    # public table, but its SIM-provided SPN is unambiguous. A bare PLMN is deliberately rejected
    # because both are MVNOs sharing their host network with unrelated SIMs.
    carrier_name = re.sub(r"[^a-z0-9]+", "", str(resolved.get("name") or "").casefold())
    if resolved.get("specific") and carrier_name in {"ultra", "ultramobile", "ultraunivision"}:
        return "ultramobile"
    identity = instance.get("carrier_identity") or {}
    spn = re.sub(r"[^a-z0-9]+", "", str(identity.get("spn") or "").casefold())
    if spn == "ctexcel" or spn.startswith("ctexcel"):
        return "ctexcel"
    return ""


def query_rule(instance: dict | None, resolved: dict | None = None) -> dict:
    iid = str((instance or {}).get("id") or "")
    key = carrier_key(instance, resolved)
    default = DEFAULT_RULES.get(key)
    custom = store.get_allowance_query_rule(iid) if iid else None
    effective = custom or default
    return {
        "carrier_key": key,
        "carrier_label": (default or {}).get("carrier_label") or str((resolved or {}).get("name") or ""),
        "known": bool(default),
        "custom": bool(custom),
        "effective": ({"recipient": effective["recipient"], "body": effective["body"]}
                      if effective else None),
        "default": ({"recipient": default["recipient"], "body": default["body"]}
                    if default else None),
    }


def validate_rule(recipient: object, body: object) -> tuple[str, str]:
    recipient = str(recipient or "").strip()
    body = str(body or "").strip()
    if not re.fullmatch(r"\+?\d{1,32}", recipient):
        raise ValueError("recipient must be a service short code or international number")
    if not body:
        raise ValueError("query message body is required")
    if len(body) > MAX_QUERY_BODY_LENGTH:
        raise ValueError(f"query message body must be at most {MAX_QUERY_BODY_LENGTH} characters")
    return recipient, body


def clean_allowance(values: dict) -> dict:
    result = {}
    for key in store.ALLOWANCE_FIELDS:
        value = str(values.get(key) or "").strip()
        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError(f"{key} must be at most {MAX_VALUE_LENGTH} characters")
        result[key] = value
    if result["activated_at"]:
        try:
            date.fromisoformat(result["activated_at"])
        except ValueError as exc:
            raise ValueError("activated_at must use YYYY-MM-DD format") from exc
    return result


def parse_expiry_date(value: object) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    # SMS replies keep carrier formatting; manual values normally use ISO.
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            pass
    return None


def parse_reply(key: str, messages: list[dict]) -> dict:
    text = "\n".join(str(item.get("body") or "") for item in messages)
    if not text:
        return {}
    parsed = {}
    if key == "ultramobile":
        patterns = {
            "voice_remaining": r"(?:本月)?剩余通话时间[：:]\s*([\d.]+\s*(?:分钟|min(?:ute)?s?))",
            "sms_remaining": r"(?:本月)?剩余短信数[：:]\s*([\d.]+\s*(?:条|texts?|SMS))",
            "data_remaining": r"(?:本月)?剩余流量[：:]\s*([\d.]+\s*(?:KB|MB|GB|TB))",
            "valid_until": r"(?:计划到期日|到期日|有效期)[：:]\s*([^\s\n]+)",
            "balance": r"(?:PayGo\s*)?(?:钱包余额|余额)[：:]\s*([^\s\n]+)",
        }
        for field, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                parsed[field] = re.sub(r"\s+", " ", match.group(1)).strip()
    elif key == "ctexcel":
        match = re.search(r"(?:current\s+)?credit\s+balance\s+is\s+([^\s.]+(?:\.\d+)?)", text,
                          re.IGNORECASE)
        if match:
            parsed["balance"] = match.group(1).strip().rstrip(".")
    return parsed


def reconcile(instance: str, now: int | None = None) -> dict:
    """Augment the cached snapshot from replies after the latest explicit query only.

    Re-reading during the two-minute window is intentional: multipart replies may initially
    populate only usage fields and add the wallet balance a few seconds later.
    """
    now = int(now or time.time())
    query = store.latest_allowance_query(instance)
    if not query or query.get("status") not in {"pending", "sent", "unknown", "parsed"}:
        return store.get_allowance(instance)
    end = int(query["started_ts"]) + QUERY_WINDOW_SECONDS
    replies = store.allowance_query_replies(instance, query["recipient"],
                                            query["started_ts"], min(now, end))
    parsed = parse_reply(str(query.get("carrier_key") or ""), replies)
    if parsed:
        current = store.get_allowance(instance)
        merged = {key: parsed.get(key, current.get(key, "")) for key in store.ALLOWANCE_FIELDS}
        snapshot = store.save_allowance(instance, merged, source="sms",
                                        updated_ts=max(item["ts"] for item in replies))
        store.set_allowance_query_status(query["id"], "parsed")
        return snapshot
    if now > end and query.get("status") != "parsed":
        store.set_allowance_query_status(query["id"], "expired")
    return store.get_allowance(instance)

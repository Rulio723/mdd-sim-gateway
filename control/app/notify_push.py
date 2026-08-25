"""
notify_push.py - Outbound push notifications for incoming events (SMS / calls).

Three independent, separately-configurable channels, all driven from global settings:
  - webhook : GET or POST standard/custom fields to a user-supplied URL.
  - telegram: send a formatted message to a chat/channel via a Telegram bot.
  - pushplus: send through the official PushPlus HTTP API.

Both fire on the SAME internal events (incoming_sms, incoming_call) and carry the same
core fields (SIM ICCID, the line's own MSISDN, the event's source number, the event type,
and the SMS text when applicable). Delivery is best-effort and MUST NOT block or break the
engine-event path: callers dispatch fire-and-forget and every network call is wrapped so a
failing/slow endpoint only logs a warning.
"""
from __future__ import annotations

import collections
import logging
import json
import os
import re
import threading
import time
import uuid
from typing import Any

import requests

from . import egress

log = logging.getLogger("vowifi.push")

# Webhook targets are often internal hosts with self-signed TLS; we POST with verify=False
# (see _post_webhook), so silence urllib3's per-request InsecureRequestWarning to keep logs clean.
try:
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # type: ignore[attr-defined]
except Exception:  # noqa
    pass

# Event identifiers shared by both channels and the settings UI checkboxes.
EV_INCOMING_SMS = "incoming_sms"
EV_INCOMING_CALL = "incoming_call"
# The host itself became unable to do its job (brown-out, thermal throttling, a full disk).
# Every line drops together when this happens, so it is reported once for the box rather than
# once per line.
EV_HOST_ALERT = "host_alert"
# The carrier started answering registration with a different public number, which is what a
# number port looks like from here. It changes the line's caller identity, so it is announced
# rather than silently corrected.
EV_NUMBER_CHANGED = "number_changed"
# The gateway stopped trying to fix a line by itself — either every exit was tried and none
# could carry a tunnel, or the failures were never the exit's fault to begin with. Both need
# a person, and a gateway that cannot recover should say so rather than rebuild forever.
EV_LINE_UNRECOVERABLE = "line_unrecoverable"
# The scheduled number-keeping action ran. Both outcomes are announced, not just failures:
# the successful case spent the user's money on their SIM, and that deserves a receipt.
EV_KEEPALIVE_RESULT = "keepalive_result"
# A plan SIM renews itself, so what threatens the number is a balance too low to pay the next
# cycle. Repeated on a slow cadence while it lasts rather than once, because it stays true.
EV_BALANCE_LOW = "balance_low"
# An inbound call that nobody answered. Separate from EV_INCOMING_CALL because that one fires
# when the phone starts ringing (and is useful only while a browser is open to answer it),
# whereas this one is the outcome — the notification that matters when nobody was there.
# A call the user actively declined is NOT a missed call and never raises this.
EV_MISSED_CALL = "missed_call"
# Somebody left a message. Announced separately from the missed call itself because the
# action it invites is different: there is now something to listen to.
EV_VOICEMAIL = "voicemail_received"
# A new MDD Sim Gateway Release is available. This is a gateway-level event and therefore
# carries no SIM identity; update settings decide whether patch-only releases are announced.
EV_SOFTWARE_UPDATE = "software_update"

_TIMEOUT = 8  # seconds; keep short so a dead endpoint never piles up threads
_TOKEN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_HISTORY_LOCK = threading.RLock()
_PENDING: dict[str, dict] = {}
# Telegram message id of a delivered incoming-SMS notification -> the line and peer it came
# from. Kept in memory only for notification formatting and deduplication
# and bounded: a stale mapping just means the operator has to use /sms explicitly.
_REPLY_TARGETS: "collections.OrderedDict[int, dict]" = collections.OrderedDict()
_REPLY_TARGET_LIMIT = 200


def _history_path() -> str:
    root = os.environ.get("MDD_DATA", os.path.join(os.getcwd(), "data"))
    return os.path.join(root, "notifications", "deliveries.jsonl")


def _record_delivery(record: dict) -> None:
    """Append metadata only: notification bodies, numbers and credentials are never logged."""
    path = _history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe = {key: record.get(key) for key in (
        "id", "created_at", "finished_at", "channel", "event", "instance",
        "status", "attempts", "status_code", "error",
    ) if record.get(key) is not None}
    with _HISTORY_LOCK, open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def delivery_status(limit: int = 100) -> dict:
    limit = max(1, min(500, int(limit)))
    rows = []
    try:
        with _HISTORY_LOCK, open(_history_path(), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()][-limit:]
    except (OSError, ValueError):
        rows = []
    with _HISTORY_LOCK:
        pending = [dict(value) for value in _PENDING.values()]
    return {"pending": pending, "history": list(reversed(rows))}


def clear_delivery_history() -> None:
    try:
        os.remove(_history_path())
    except FileNotFoundError:
        pass


def _deliver_with_retry(channel: str, sender, channel_cfg: dict, payload: dict) -> None:
    delivery_id = uuid.uuid4().hex
    entry = {
        "id": delivery_id, "created_at": int(time.time()), "channel": channel,
        "event": payload.get("event"), "instance": payload.get("instance"),
        "status": "pending", "attempts": 0,
    }
    with _HISTORY_LOCK:
        _PENDING[delivery_id] = dict(entry)
    error = ""
    result = {}
    for attempt, delay in enumerate((0, 2, 5), start=1):
        if delay:
            time.sleep(delay)
        entry["attempts"] = attempt
        try:
            result = sender(channel_cfg, payload) or {}
            entry.update(status="delivered", status_code=result.get("status_code"))
            error = ""
            break
        except Exception as exc:  # noqa: delivery errors are deliberately sanitized
            error = f"{type(exc).__name__}: delivery failed"
            entry.update(status="retrying" if attempt < 3 else "failed", error=error)
            with _HISTORY_LOCK:
                _PENDING[delivery_id] = dict(entry)
    entry["finished_at"] = int(time.time())
    if error:
        entry["error"] = error
    with _HISTORY_LOCK:
        _PENDING.pop(delivery_id, None)
    _record_delivery(entry)


def _events_enabled(chan: dict) -> dict:
    ev = chan.get("events") or {}
    # default: both on if the key is absent (a freshly-enabled channel notifies everything)
    return {
        EV_INCOMING_SMS: ev.get(EV_INCOMING_SMS, True),
        EV_INCOMING_CALL: ev.get(EV_INCOMING_CALL, True),
        EV_HOST_ALERT: ev.get(EV_HOST_ALERT, True),
        EV_NUMBER_CHANGED: ev.get(EV_NUMBER_CHANGED, True),
        EV_LINE_UNRECOVERABLE: ev.get(EV_LINE_UNRECOVERABLE, True),
        EV_KEEPALIVE_RESULT: ev.get(EV_KEEPALIVE_RESULT, True),
        EV_BALANCE_LOW: ev.get(EV_BALANCE_LOW, True),
        EV_MISSED_CALL: ev.get(EV_MISSED_CALL, True),
        EV_VOICEMAIL: ev.get(EV_VOICEMAIL, True),
        EV_SOFTWARE_UPDATE: ev.get(EV_SOFTWARE_UPDATE, True),
    }


def has_enabled_channel(settings: dict, event: str) -> bool:
    return any(bool((settings.get(key) or {}).get("enabled"))
               and bool(_events_enabled(settings.get(key) or {}).get(event))
               for key in ("webhook", "telegram", "pushplus"))


def build_payload(event: str, instance: dict, source: str, text: str | None) -> dict:
    """The canonical event body both channels are built from. `instance` is the stored
    line config (for ICCID + MSISDN); `source` is the event's originating number; `text`
    is the SMS body (None for calls)."""
    return {
        "event": event,                                   # incoming_sms | incoming_call
        "instance": str(instance.get("id", "")),
        "sim_name": instance.get("name", "") or "",
        "iccid": instance.get("iccid", "") or "",
        "msisdn": instance.get("msisdn", "") or "",       # the line's own number (may be "")
        "from": source or "",                             # the event's source number
        "text": text if event in (EV_INCOMING_SMS, EV_HOST_ALERT, EV_NUMBER_CHANGED,
                                  EV_LINE_UNRECOVERABLE, EV_KEEPALIVE_RESULT,
                                  EV_BALANCE_LOW, EV_VOICEMAIL,
                                  EV_SOFTWARE_UPDATE) else None,
    }


# Every notification leads with this. A push arrives out of context — on a lock screen, in a
# Telegram list beside a dozen other bots — and "未接来电" alone does not say which machine is
# talking. Kept short because notification titles are truncated aggressively.
BRAND = "MDD"


def _titled(text: str) -> str:
    """Prefix once. The software-update title already carries the product name, so a blind
    prefix would render it as "MDD · MDD Sim Gateway …"."""
    return text if text.startswith(BRAND) else f"{BRAND} · {text}"


def build_notification_message(payload: dict) -> dict:
    """Build the human-readable title/content shared by templates and vendor channels."""
    event = payload.get("event")
    sim = payload.get("sim_name") or payload.get("iccid") or payload.get("instance") or "SIM"
    own = payload.get("msisdn") or ""
    sender = payload.get("from") or "unknown"
    if event == EV_HOST_ALERT:
        # Not about one SIM: the box is degraded and every line is affected at once.
        return {"title": _titled(f"网关主机异常 · {sender}"),
                "content": payload.get("text") or ""}
    if event == EV_NUMBER_CHANGED:
        return {"title": _titled(f"线路号码已变更 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_LINE_UNRECOVERABLE:
        return {"title": _titled(f"线路无法自动恢复 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_KEEPALIVE_RESULT:
        return {"title": _titled(f"保号执行结果 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_BALANCE_LOW:
        return {"title": _titled(f"余额不足 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_VOICEMAIL:
        return {"title": _titled(f"新留言 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_MISSED_CALL:
        lines = [f"SIM: {sim}"]
        if own:
            lines.append(f"本机号码: {own}")
        lines.append(f"来源号码: {sender}")
        return {"title": _titled(f"未接来电 · {sim}"), "content": "\n".join(lines)}
    if event == EV_SOFTWARE_UPDATE:
        return {"title": _titled(f"MDD Sim Gateway 新版本 · v{sender}"),
                "content": payload.get("text") or ""}
    title = _titled(f"VoWiFi {'短信' if event == EV_INCOMING_SMS else '来电'} · {sim}")
    lines = [f"SIM: {sim}"]
    if own:
        lines.append(f"本机号码: {own}")
    lines.append(f"来源号码: {sender}")
    if event == EV_INCOMING_SMS:
        lines.extend(["", payload.get("text") or ""])
    return {
        "title": title,
        "content": "\n".join(lines),
    }


def _template_context(cfg: dict, payload: dict) -> dict:
    return {**payload, **build_notification_message(payload)}


def _render(value, context: dict):
    """Render {{field}} placeholders recursively while preserving an exact field's type."""
    if isinstance(value, dict):
        return {str(_render(k, context)): _render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, context) for v in value]
    if not isinstance(value, str):
        return value
    exact = _TOKEN.fullmatch(value)
    if exact:
        return context.get(exact.group(1))
    return _TOKEN.sub(lambda m: str(context.get(m.group(1)) or ""), value)


def _json_setting(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    if not str(value or "").strip():
        return fallback
    return json.loads(str(value))


def build_webhook_request(cfg: dict, payload: dict) -> tuple[str, str, dict]:
    """Build a requests.request call for standard or custom webhooks.

    GET sends the rendered fields as query parameters. POST supports JSON, form-encoded and raw
    bodies. Custom headers and URL/body placeholders make the adapter usable with arbitrary
    webhook receivers without hard-coding each vendor's schema.
    """
    method = str(cfg.get("method") or "POST").upper()
    if method not in {"GET", "POST"}:
        raise ValueError("webhook method must be GET or POST")
    context = _template_context(cfg, payload)
    url = _render(str(cfg.get("url") or "").strip(), context)
    if not url:
        raise ValueError("webhook URL is empty")

    headers = {"User-Agent": "mdd-sim-gateway"}
    custom_headers = _json_setting(cfg.get("headers_json") or cfg.get("headers"), {})
    if not isinstance(custom_headers, dict):
        raise ValueError("webhook headers must be a JSON object")
    headers.update({str(k): str(_render(v, context)) for k, v in custom_headers.items()})

    preset = str(cfg.get("format") or "generic")
    if preset == "custom":
        raw_template = cfg.get("payload_template") or "{}"
        mode = str(cfg.get("body_mode") or "json")
        if mode == "raw":
            body = _render(str(raw_template), context)
        else:
            body = _render(_json_setting(raw_template, {}), context)
    else:
        body = payload

    kwargs = {"headers": headers, "timeout": _TIMEOUT,
              "verify": bool(cfg.get("verify_tls", True))}
    if method == "GET":
        kwargs["params"] = body if isinstance(body, dict) else {"payload": str(body)}
    else:
        mode = str(cfg.get("body_mode") or "json")
        if mode == "form":
            if not isinstance(body, dict):
                raise ValueError("form webhook payload must be a JSON object")
            kwargs["data"] = body
        elif mode == "raw":
            kwargs["data"] = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        else:
            kwargs["json"] = body
    return method, url, kwargs


def _post_webhook(cfg: dict, payload: dict):
    try:
        result = send_webhook(cfg, payload)
        log.info("webhook %s -> %s", payload.get("event"), result["status_code"])
    except Exception as e:  # noqa
        log.warning("webhook delivery failed: %r", e)


def send_webhook(cfg: dict, payload: dict) -> dict:
    """Send one webhook or raise a sanitized error. Used by delivery and the test API."""
    method, url, kwargs = build_webhook_request(cfg, payload)
    try:
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError("webhook request failed") from None
    return {"ok": True, "status_code": response.status_code}


def send_pushplus(cfg: dict, payload: dict) -> dict:
    """Send one message with PushPlus' documented JSON API."""
    token = str(cfg.get("token") or "").strip()
    if not token:
        raise ValueError("PushPlus token is required")
    template = str(cfg.get("template") or "html").strip().lower()
    if template not in {"html", "txt", "markdown", "json"}:
        raise ValueError("unsupported PushPlus template")
    channel = str(cfg.get("channel") or "wechat").strip().lower()
    if channel not in {"wechat", "webhook", "cp", "mail", "sms", "voice", "extension", "app", "clawbot"}:
        raise ValueError("unsupported PushPlus channel")
    message = build_notification_message(payload)
    body = {"token": token, **message, "template": template, "channel": channel}
    topic = str(cfg.get("topic") or "").strip()
    if topic:
        body["topic"] = topic
    try:
        response = requests.post("https://www.pushplus.plus/send", json=body, timeout=_TIMEOUT)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("PushPlus request failed") from None
    # PushPlus returns HTTP 200 for application-level errors; its documented success code is 200.
    if not isinstance(result, dict) or str(result.get("code") or "") != "200":
        raise RuntimeError("PushPlus rejected the message")
    return {"ok": True, "status_code": response.status_code}


def _telegram_headline(icon: str, text: str) -> str:
    """Telegram's own first line. The icon stays leftmost — it is what makes the event type
    scannable in a chat list — and the brand follows it, for the same reason the titles carry
    one: a bot's messages arrive with no other indication of which machine sent them."""
    text = text.removeprefix(f"{BRAND} ").removeprefix(f"{BRAND} · ")
    return f"{icon} {BRAND} · {text}" if icon else f"{BRAND} · {text}"


def _telegram_text(payload: dict) -> str:
    ev = payload.get("event")
    if ev == EV_LINE_UNRECOVERABLE:
        return "\n".join([_telegram_headline("🛑", f"线路无法自动恢复 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_NUMBER_CHANGED:
        return "\n".join([_telegram_headline("🔄", f"线路号码已变更 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_HOST_ALERT:
        # Not a SIM event: the box itself is degraded, so the SIM/From lines would be
        # meaningless (a previous version rendered this as an incoming call from the
        # hardware model).
        return "\n".join([_telegram_headline("⚠️", "网关主机异常"), str(payload.get("from") or ""), "",
                          payload.get("text") or ""])
    if ev == EV_KEEPALIVE_RESULT:
        return "\n".join([_telegram_headline("◷", f"保号执行结果 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_BALANCE_LOW:
        return "\n".join([_telegram_headline("⚠️", f"余额不足 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_VOICEMAIL:
        return "\n".join([_telegram_headline("🎙", f"新留言 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_MISSED_CALL:
        name = payload.get("sim_name") or payload.get("iccid") or payload.get("instance")
        msisdn = payload.get("msisdn")
        return "\n".join([_telegram_headline("📵", "Missed call"),
                          f"SIM: {name}" + (f" ({msisdn})" if msisdn else ""),
                          f"From: {payload.get('from') or 'unknown'}"])
    if ev == EV_SOFTWARE_UPDATE:
        return "\n".join([_telegram_headline("🆕", f"Sim Gateway 新版本 · v{payload.get('from') or ''}"),
                           "", payload.get("text") or ""])
    head = _telegram_headline("📩" if ev == EV_INCOMING_SMS else "📞",
                              "Incoming SMS" if ev == EV_INCOMING_SMS else "Incoming call")
    name = payload.get("sim_name") or payload.get("iccid") or payload.get("instance")
    msisdn = payload.get("msisdn")
    sim_line = f"SIM: {name}" + (f" ({msisdn})" if msisdn else "")
    lines = [head, sim_line, f"From: {payload.get('from') or 'unknown'}"]
    if ev == EV_INCOMING_SMS:
        lines.append("")
        lines.append(payload.get("text") or "")
    return "\n".join(lines)


def telegram_session(cfg: dict) -> requests.Session:
    mode = str(cfg.get("proxy_mode") or "direct").lower()
    session = requests.Session()
    session.trust_env = False
    if mode == "manual":
        proxy = str(cfg.get("proxy_url") or "").strip()
        if not re.match(r"^(?:https?|socks5h?)://", proxy, re.IGNORECASE):
            raise ValueError("Telegram proxy must be an HTTP(S) or SOCKS5 URL")
        session.proxies.update({"http": proxy, "https": proxy})
    elif mode == "country":
        country = egress.normalize_country(cfg.get("proxy_country"))
        state = (egress.status().get("exits") or {}).get(country) or {}
        interface = str(state.get("interface") or "")
        try:
            proxy_port = int(state.get("proxy_port") or 0)
        except (TypeError, ValueError):
            proxy_port = 0
        if (not country or not state.get("ready")
                or not re.fullmatch(r"mdd-[a-z]{2}", interface)
                or not 1 <= proxy_port <= 65535):
            raise RuntimeError("selected country exit is not ready")
        # Binding a socket to the country TUN still resolves api.telegram.org through the
        # host's DNS first. On filtered networks that can yield a poisoned address even though
        # the selected node itself reaches Telegram. The orchestrator's country SOCKS inbound
        # follows the same verified outbound, while socks5h delegates DNS to sing-box.
        proxy_host = str(state.get("proxy_host") or "172.17.0.1").strip()
        if not proxy_host:
            raise RuntimeError("selected country exit is not ready")
        proxy = f"socks5h://{proxy_host}:{proxy_port}"
        session.proxies.update({"http": proxy, "https": proxy})
    elif mode != "direct":
        raise ValueError("Telegram proxy mode must be direct, manual or country")
    return session


def remember_reply_target(message_id, payload: dict) -> None:
    """Record which line/peer a delivered SMS notification came from so a Telegram reply to
    notification delivery keeps the original sender context."""
    peer = str(payload.get("from") or "").strip()
    instance = str(payload.get("instance") or "").strip()
    if payload.get("event") != EV_INCOMING_SMS or not peer:
        return
    try:
        key = int(message_id)
    except (TypeError, ValueError):
        return
    with _HISTORY_LOCK:
        _REPLY_TARGETS[key] = {"instance": instance, "peer": peer}
        while len(_REPLY_TARGETS) > _REPLY_TARGET_LIMIT:
            _REPLY_TARGETS.popitem(last=False)


def reply_target(message_id) -> dict | None:
    """The {instance, peer} a Telegram message was a notification for, if still known."""
    if message_id is None:
        return None
    try:
        key = int(message_id)
    except (TypeError, ValueError):
        return None
    with _HISTORY_LOCK:
        target = _REPLY_TARGETS.get(key)
    return dict(target) if target else None


def send_telegram(cfg: dict, payload: dict) -> dict:
    token = (cfg.get("bot_token") or "").strip()
    chat = str(cfg.get("chat_id") or "").strip()
    if not token or not chat:
        raise ValueError("Telegram bot token and chat ID are required")
    session = telegram_session(cfg)
    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": _telegram_text(payload),
                  "disable_web_page_preview": True},
            timeout=_TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException:
        # Never include the requests exception: its URL contains the bot token.
        raise RuntimeError("Telegram request failed") from None
    except ValueError:
        body = {}
    finally:
        session.close()
    result = body.get("result") if isinstance(body, dict) else None
    if isinstance(result, dict):
        remember_reply_target(result.get("message_id"), payload)
    return {"ok": True, "status_code": response.status_code}


def _post_telegram(cfg: dict, payload: dict):
    try:
        send_telegram(cfg, payload)
        log.info("telegram %s delivered", payload.get("event"))
    except Exception as exc:  # noqa
        log.warning("telegram delivery failed: %s", type(exc).__name__)


def dispatch(settings: dict, event: str, instance: dict, source: str, text: str | None = None):
    """Fire all configured channels for one event. BLOCKING (does the HTTP itself) — the
    caller runs this off the event path (e.g. asyncio.to_thread + create_task) so a slow
    endpoint never stalls engine-event handling. Safe to call unconditionally: each channel
    is gated on its own enable flag + per-event checkbox."""
    try:
        payload = build_payload(event, instance, source, text)
        wh = settings.get("webhook") or {}
        if wh.get("enabled") and _events_enabled(wh).get(event):
            _deliver_with_retry("webhook", send_webhook, wh, payload)
        tg = settings.get("telegram") or {}
        if tg.get("enabled") and _events_enabled(tg).get(event):
            _deliver_with_retry("telegram", send_telegram, tg, payload)
        pp = settings.get("pushplus") or {}
        if pp.get("enabled") and _events_enabled(pp).get(event):
            _deliver_with_retry("pushplus", send_pushplus, pp, payload)
    except Exception as e:  # noqa
        log.warning("push dispatch error: %r", e)

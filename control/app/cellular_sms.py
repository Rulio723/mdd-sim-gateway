"""Send and import SMS objects owned by ModemManager.

VoWiFi SMS arrives through Asterisk. When a modem is also registered on the cellular network,
ModemManager independently receives ordinary 3GPP SMS and keeps them as D-Bus SMS objects.
This module uses ModemManager without taking over the modem's serial port and maps each operation
to the saved SIM line by ICCID.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime

SMS_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/SMS/\d+$")
MODEM_PATH_RE = re.compile(r"/org/freedesktop/ModemManager1/Modem/\d+")
SIM_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/SIM/\d+$")
RECIPIENT_RE = re.compile(r"^\+?\d{1,32}$")
BOOT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
DBUS_OWNER_RE = re.compile(r'^s\s+"(:\d+\.\d+)"$')

# Serializes the scanner's SMS path snapshot with send()'s Create-and-bind, so the scanner can
# never see a gateway-created object before its history row knows the path.
_local_sms_lock = threading.RLock()

# What to do with an SMS object once its message is safely in the database.
#   delete     remove it at once: modem/SIM storage is small (tens of slots) and a full store
#              stops all further delivery, while the database already holds the message.
#   when_full  leave objects in place, deleting the oldest imported ones only when the storage
#              is nearly full.
#   keep       never delete; the operator manages modem storage.
STORAGE_POLICIES = ("delete", "when_full", "keep")
STORAGE_POLICY_ENV = "MDD_CELLULAR_SMS_STORAGE"
STORAGE_LIMIT_ENV = "MDD_CELLULAR_SMS_STORAGE_LIMIT"
# Slots kept free under when_full, so the parts of a long text still have room to arrive.
_STORAGE_HEADROOM = 3
# Assumed capacity when the modem does not answer AT+CPMS? (ModemManager without --debug).
_DEFAULT_STORAGE_LIMIT = 20
_CAPACITY_TTL = 600.0
# The gateway's own object is removed once ModemManager reports it sent. One that never gets
# there -- its send timed out, or the process exited mid-send -- is removed after this long,
# which is far beyond any submit still in progress.
_OWN_OBJECT_GRACE = 600.0
# Give up after a few failures so an object that can never be deleted (a read-only storage, a
# revoked permission) does not put an mmcli call into every five-second poll forever.
_DELETE_ATTEMPTS = 3


def storage_policy(settings: dict | None = None, environ=os.environ) -> str:
    """The configured policy: settings.cellular_sms_storage, else the environment, else delete."""
    for value in ((settings or {}).get("cellular_sms_storage"), environ.get(STORAGE_POLICY_ENV)):
        text = str(value or "").strip().lower().replace("-", "_")
        if text in STORAGE_POLICIES:
            return text
    return "delete"


def storage_limit(environ=os.environ) -> int:
    try:
        value = int(str(environ.get(STORAGE_LIMIT_ENV) or "").strip())
    except ValueError:
        return _DEFAULT_STORAGE_LIMIT
    return value if value > _STORAGE_HEADROOM else _DEFAULT_STORAGE_LIMIT


def _run_json(args: list[str], runner=subprocess.run) -> dict:
    result = runner(["mmcli", *args, "--output-json"], capture_output=True, text=True,
                    timeout=10, check=False)
    if result.returncode:
        return {}
    try:
        value = json.loads(result.stdout or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _modem_paths(runner=subprocess.run) -> list[str]:
    result = runner(["mmcli", "-L"], capture_output=True, text=True, timeout=10, check=False)
    return sorted(set(MODEM_PATH_RE.findall(result.stdout or ""))) if not result.returncode else []


def _invoke(args: list[str], runner, timeout: float):
    """Run one bounded mmcli command without a shell and classify launch failures."""
    try:
        result = runner(["mmcli", *args], capture_output=True, text=True,
                        timeout=timeout, check=False)
        return result, None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "unavailable"
    except Exception:  # A custom runner must not make the HTTP path raise unexpectedly.
        return None, "error"


def _invoke_busctl(args: list[str], runner, timeout: float):
    """Run one bounded system-bus call with the same failure contract as ``_invoke``."""
    try:
        result = runner(["busctl", "--system", "call", *args], capture_output=True, text=True,
                        timeout=timeout, check=False)
        return result, None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "unavailable"
    except Exception:
        return None, "error"


def _decode_json(result) -> dict:
    try:
        value = json.loads(getattr(result, "stdout", "") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _command_error(result, fallback: str) -> str:
    detail = " ".join(str(getattr(result, "stderr", "") or "").split())
    # mmcli errors are useful to the operator, but cap them before returning them to the UI.
    return detail[:300] if detail else fallback


def _response(instance_id, *, ok: bool, status: str, error: str | None,
              stage: str, modem_path: str | None = None, sms_path: str | None = None,
              uncertain: bool = False, message_id: int | None = None) -> dict:
    response = {
        "ok": ok,
        "status": status,
        "error": error,
        "stage": stage,
        "transport": "cellular",
        "instance": str(instance_id),
        "modem_path": modem_path,
        "sms_path": sms_path,
        "unavailable": status == "unavailable",
        "uncertain": uncertain,
    }
    if message_id is not None:
        response["message_id"] = int(message_id)
    return response


def _normalize_iccid(value) -> str:
    """Return the canonical comparison/storage form for a modem or configured ICCID."""
    text = str(value or "").strip().casefold()
    # mmcli renders an unreadable property as the literal placeholder "--".
    return "" if text == "--" else text


def _sms_text(value) -> str:
    """Return the readable body of an SMS, or empty when ModemManager has none yet.

    mmcli renders an unreadable property as the literal placeholder "--", and the text of a
    multi-part SMS stays unreadable until every part has arrived. Storing that placeholder
    would both show "--" as a message and, once assembly completes, import the real text as a
    second message, because the import fingerprint covers the body. A genuine one-character
    body of "--" is indistinguishable here and is dropped with it; that is far rarer than an
    incomplete multi-part SMS, which is a routine event on every long message.
    """
    text = str(value or "")
    return "" if text.strip() == "--" else text


def _sms_data(value) -> bytes:
    """Decode the mmcli rendering of an SMS binary payload; empty when absent or unparsable."""
    if isinstance(value, (list, tuple)):
        try:
            return bytes(int(item) & 0xFF for item in value)
        except (TypeError, ValueError):
            return b""
    text = str(value or "").strip()
    if not text or text == "--":
        return b""
    # mmcli prints the payload as space-separated hex bytes; tolerate other byte separators.
    compact = re.sub(r"[^0-9A-Fa-f]", "", text)
    if not compact or len(compact) % 2:
        return b""
    try:
        return bytes.fromhex(compact)
    except ValueError:
        return b""


def _normalize_imsi(value) -> str:
    """Return the digits-only comparison form of an IMSI, or empty when absent."""
    return re.sub(r"\D", "", str(value or ""))


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            value = handle.read().strip()
        return value.lower() if BOOT_ID_RE.fullmatch(value) else ""
    except OSError:
        return ""


def _modemmanager_epoch(runner=subprocess.run, boot_id_reader=_boot_id) -> str:
    """Return a non-secret identity for this host boot and ModemManager process.

    ModemManager reuses numeric SMS object paths after its D-Bus service restarts.  Combining
    the kernel boot id with the service's unique D-Bus owner makes a persisted local-send marker
    valid only for the daemon generation that created the object.
    """
    boot_id = boot_id_reader()
    if not boot_id:
        return ""
    try:
        result = runner([
            "busctl", "--system", "call", "org.freedesktop.DBus",
            "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
            "s", "org.freedesktop.ModemManager1",
        ], capture_output=True, text=True, timeout=3, check=False)
    except Exception:
        return ""
    if getattr(result, "returncode", 1):
        return ""
    match = DBUS_OWNER_RE.fullmatch(str(getattr(result, "stdout", "") or "").strip())
    if not match:
        return ""
    return hashlib.sha256(f"{boot_id}\0{match.group(1)}".encode("ascii")).hexdigest()


def _instance_iccid(instances: list[dict], instance_id) -> str:
    iid = str(instance_id)
    for item in instances or []:
        if isinstance(item, dict) and str(item.get("id")) == iid:
            return _normalize_iccid(item.get("iccid"))
    return ""


def _instance_imsi(instances: list[dict], instance_id) -> str:
    iid = str(instance_id)
    for item in instances or []:
        if isinstance(item, dict) and str(item.get("id")) == iid:
            return _normalize_imsi(item.get("imsi"))
    return ""


def _find_modem(iccid: str, runner, timeout: float, *,
                imsi: str = "") -> tuple[str | None, str | None]:
    """Return (modem_path, problem); a problem is a stable, user-safe description."""
    listing, problem = _invoke(["-L"], runner, timeout)
    if problem == "timeout":
        return None, "Timed out while listing cellular modems."
    if problem or getattr(listing, "returncode", 1):
        return None, "ModemManager is unavailable."

    modem_paths = sorted(set(MODEM_PATH_RE.findall(getattr(listing, "stdout", "") or "")))
    if not modem_paths:
        return None, "No cellular modem is available."

    inspection_failed = False
    for modem_path in modem_paths:
        detail, problem = _invoke(["-m", modem_path, "--output-json"], runner, timeout)
        if problem or getattr(detail, "returncode", 1):
            inspection_failed = True
            continue
        modem_doc = _decode_json(detail)
        modem = modem_doc.get("modem") or {}
        sim_path = str((modem.get("generic") or {}).get("sim")
                       or modem_doc.get("modem.generic.sim") or "")
        if not SIM_PATH_RE.fullmatch(sim_path):
            inspection_failed = True
            continue

        sim_detail, problem = _invoke(["-i", sim_path, "--output-json"], runner, timeout)
        if problem or getattr(sim_detail, "returncode", 1):
            inspection_failed = True
            continue
        sim_doc = _decode_json(sim_detail)
        sim = sim_doc.get("sim") or {}
        properties = sim.get("properties") or {}
        modem_iccid = _normalize_iccid(properties.get("iccid")
                                       or sim_doc.get("sim.properties.iccid"))
        if modem_iccid:
            if modem_iccid == _normalize_iccid(iccid):
                return modem_path, None
            continue
        # Some modules (observed: CMIOT ML307X) reject the EF_ICCID read ModemManager
        # relies on, so the SIM exposes no ICCID at all. Those modules do report the
        # IMSI, which the line also stores; matching on it keeps the SIM's identity
        # authoritative instead of guessing by modem model or port.
        modem_imsi = _normalize_imsi(properties.get("imsi")
                                     or sim_doc.get("sim.properties.imsi"))
        if imsi and modem_imsi and modem_imsi == imsi:
            return modem_path, None

    if inspection_failed:
        return None, "Could not find the line's SIM among the readable cellular modems."
    return None, "No cellular modem matches this line's ICCID or IMSI."


def _created_sms_path(result) -> str:
    doc = _decode_json(result)
    modem = doc.get("modem") or {}
    path = str((modem.get("messaging") or {}).get("created-sms")
               or doc.get("modem.messaging.created-sms") or "")
    if not path:
        # The D-Bus Create reply is printed by busctl as: o "/org/.../SMS/N".
        # Keep accepting the old mmcli JSON shape for compatibility with focused callers/tests.
        match = re.search(r"/org/freedesktop/ModemManager1/SMS/\d+",
                          getattr(result, "stdout", "") or "")
        path = match.group(0) if match else ""
    return path if SMS_PATH_RE.fullmatch(path) else ""


def send(instances: list[dict], instance_id, recipient: str, text: str,
         runner=subprocess.run, *, timeout: float = 30.0, local_sms_tracker=None) -> dict:
    """Send an SMS through the modem containing ``instance_id``'s ICCID.

    This synchronous function is intended to be called with ``asyncio.to_thread``. It never
    raises for an ordinary ModemManager failure and always returns ``ok``, ``status``, ``error``,
    ``modem_path`` and ``sms_path``, plus ``message_id`` once a history row exists. A send
    timeout is deliberately reported as ``unknown``: retrying automatically could charge for
    and deliver a duplicate SMS.
    """
    recipient = str(recipient or "").strip()
    if not RECIPIENT_RE.fullmatch(recipient):
        return _response(instance_id, ok=False, status="failed",
                         error="Recipient must contain only digits with an optional leading +.",
                         stage="validate")
    if not isinstance(text, str) or not text or "\0" in text:
        return _response(instance_id, ok=False, status="failed",
                         error="Message text must be non-empty UTF-8 text.", stage="validate")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return _response(instance_id, ok=False, status="failed",
                         error="Message text must be non-empty UTF-8 text.", stage="validate")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        return _response(instance_id, ok=False, status="failed",
                         error="The ModemManager timeout must be positive.", stage="validate")

    iccid = _instance_iccid(instances, instance_id)
    if not iccid:
        return _response(instance_id, ok=False, status="unavailable",
                         error="The line has no configured ICCID.", stage="lookup")
    modem_path, problem = _find_modem(iccid, runner, timeout,
                                      imsi=_instance_imsi(instances, instance_id))
    if not modem_path:
        return _response(instance_id, ok=False, status="unavailable",
                         error=problem or "No cellular modem is available.", stage="lookup")

    status_result, problem = _invoke(
        ["-m", modem_path, "--messaging-status", "--output-json"], runner, timeout)
    if problem == "timeout":
        return _response(instance_id, ok=False, status="unavailable",
                         error="Timed out while checking cellular SMS support.", stage="check",
                         modem_path=modem_path)
    if problem or getattr(status_result, "returncode", 1):
        error = ("Cellular SMS is unavailable on this modem." if problem else
                 _command_error(status_result, "Cellular SMS is unavailable on this modem."))
        return _response(instance_id, ok=False, status="unavailable", error=error,
                         stage="check", modem_path=modem_path)

    # The object this creates is also listed by ModemManager as an outgoing SMS, and the
    # receive scanner imports outgoing objects made by other tools. The history row is written
    # before the object exists and bound to its path under the scanner's lock, so the scanner
    # always recognises the gateway's own object -- after a control-plane restart too.
    if local_sms_tracker is None:
        return _response(instance_id, ok=False, status="failed",
                         error="Durable cellular SMS tracking is unavailable.", stage="track",
                         modem_path=modem_path)
    try:
        message_id = int(local_sms_tracker.begin_local_modem_sms(
            str(instance_id), recipient, text))
    except Exception:
        return _response(instance_id, ok=False, status="failed",
                         error="Could not record the cellular SMS; it was not sent.",
                         stage="track", modem_path=modem_path)

    # mmcli 1.20 has no --messaging-create-sms-with-text option, while the underlying D-Bus
    # Create(a{sv}) method has supported Number and Text since ModemManager 1.0. Passing an argv
    # list (never a shell command) preserves punctuation and quotes as one typed string value.
    create_args = [
        "org.freedesktop.ModemManager1", modem_path,
        "org.freedesktop.ModemManager1.Modem.Messaging", "Create",
        "a{sv}", "2", "number", "s", recipient, "text", "s", text,
    ]
    sms_path = None
    with _local_sms_lock:
        create_result, problem = _invoke_busctl(create_args, runner, timeout)
        if not problem and not getattr(create_result, "returncode", 1):
            sms_path = _created_sms_path(create_result)
            if not sms_path:
                return _response(
                    instance_id, ok=False, status="failed",
                    error="ModemManager returned an invalid SMS object path.",
                    stage="create", modem_path=modem_path, message_id=message_id)
            try:
                tracked = bool(local_sms_tracker.bind_local_modem_sms(
                    message_id, modem_path, sms_path))
            except Exception:
                tracked = False
            if not tracked:
                _delete_object(modem_path, sms_path, runner)
                return _response(
                    instance_id, ok=False, status="failed",
                    error="Could not record the cellular SMS object; it was not sent.",
                    stage="track", modem_path=modem_path, sms_path=sms_path,
                    message_id=message_id)

    if problem == "timeout":
        # Create may have succeeded even though its reply timed out. The unbound history row
        # lets the scanner claim that object instead of importing it as someone else's send.
        return _response(instance_id, ok=False, status="failed",
                         error="Timed out while creating the cellular SMS.", stage="create",
                         modem_path=modem_path, message_id=message_id)
    if problem or getattr(create_result, "returncode", 1):
        error = ("Could not run ModemManager while creating the SMS." if problem else
                 _command_error(create_result, "ModemManager could not create the SMS."))
        return _response(instance_id, ok=False, status="failed", error=error,
                         stage="create", modem_path=modem_path, message_id=message_id)

    send_result, problem = _invoke(
        ["-s", sms_path, "--send", "--output-json"], runner, timeout)
    if problem == "timeout":
        # The modem may still be submitting; the scanner removes the object once it settles.
        return _response(
            instance_id, ok=False, status="unknown",
            error="Cellular SMS send timed out; delivery is unknown and was not retried.",
            stage="send", modem_path=modem_path, sms_path=sms_path, uncertain=True,
            message_id=message_id)
    # The object has served its purpose either way: the outcome lives in the history row, and
    # a finished object left behind only occupies ModemManager's list.
    _delete_object(modem_path, sms_path, runner)
    if problem or getattr(send_result, "returncode", 1):
        error = ("Could not run ModemManager while sending the SMS." if problem else
                 _command_error(send_result, "ModemManager rejected the SMS."))
        return _response(instance_id, ok=False, status="failed", error=error,
                         stage="send", modem_path=modem_path, sms_path=sms_path,
                         message_id=message_id)
    return _response(instance_id, ok=True, status="sent", error=None, stage="send",
                     modem_path=modem_path, sms_path=sms_path, message_id=message_id)


def _delete_object(modem_path: str, sms_path: str, runner, timeout: float = 10) -> bool:
    result, problem = _invoke(["-m", modem_path, f"--messaging-delete-sms={sms_path}"],
                              runner, timeout)
    return problem is None and not getattr(result, "returncode", 1)


_ISO_ZONE_RE = re.compile(r"(?:Z|([+-])(\d{2})(?::?(\d{2}))?)$")


def _timestamp(value) -> int:
    """Epoch seconds of a ModemManager timestamp, independent of the host's time zone.

    ModemManager writes the SMSC's zone as "+10" (hours only), which datetime.fromisoformat
    rejects before Python 3.11, and a value without any zone would silently be read in the
    host's local zone. The zone is normalised to "+HH:MM" first, and a value that still has
    none is refused (0, so the receipt time is used) rather than guessed.
    """
    raw = str(value or "").strip()
    match = _ISO_ZONE_RE.search(raw)
    if not raw or not match:
        return 0
    if match.group(0) == "Z":
        normalised = raw[:match.start()] + "+00:00"
    else:
        normalised = (raw[:match.start()] + f"{match.group(1)}{match.group(2)}:"
                      f"{match.group(3) or '00'}")
    try:
        parsed = datetime.fromisoformat(normalised)
    except (ValueError, OverflowError):
        return 0
    return int(parsed.timestamp()) if parsed.tzinfo is not None else 0


_CPMS_RE = re.compile(r'"(\w+)"\s*,\s*(\d+)\s*,\s*(\d+)')


def _storage_capacity(modem_path: str, runner) -> tuple[str, int, int] | None:
    """(storage, used, total) of the modem's read/delete SMS storage, or None if unreadable.

    ModemManager exposes no capacity, so this asks the modem (AT+CPMS?). The command channel
    exists only while ModemManager runs with --debug; without it the caller falls back to a
    configured object count.
    """
    result, problem = _invoke(["-m", modem_path, "--command=AT+CPMS?"], runner, 10)
    if problem or getattr(result, "returncode", 1):
        return None
    match = _CPMS_RE.search(getattr(result, "stdout", "") or "")
    if not match:
        return None
    total = int(match.group(3))
    return (match.group(1).lower(), int(match.group(2)), total) if total > 0 else None


class Scanner:
    """Incrementally mirror ModemManager's SMS objects into the message history.

    The SMS path listing stays live on every poll, so a newly arrived message is discovered
    without extra delay. Modem/SIM identity and already-read SMS details are much more stable
    and are refreshed periodically to tolerate ModemManager restarts and object-path reuse.
    """

    def __init__(self, runner=subprocess.run, *, topology_ttl: float = 60.0,
                 detail_ttl: float = 60.0, clock=time.monotonic,
                 local_sms_tracker=None, epoch_getter=_modemmanager_epoch,
                 environ=os.environ):
        self.runner = runner
        self.topology_ttl = topology_ttl
        self.detail_ttl = detail_ttl
        self.clock = clock
        self.local_sms_tracker = local_sms_tracker
        self.epoch_getter = epoch_getter
        self.environ = environ
        self._daemon_epoch = ""
        self._topology_expires = 0.0
        self._topology: list[tuple[str, str, str]] = []
        self._details: dict[tuple[str, str], tuple[float, dict]] = {}
        # Objects whose message is already in the database, keyed by path plus the content
        # read, so a reused path holding a different message is never mistaken for one.
        self._settled: dict[tuple[str, str], str] = {}
        self._delete_attempts: dict[tuple[str, str], int] = {}
        self._first_seen: dict[tuple[str, str], float] = {}
        self._capacity: dict[str, tuple[float, tuple[str, int, int] | None]] = {}

    def _forget_objects(self) -> None:
        self._details.clear()
        self._settled.clear()
        self._delete_attempts.clear()
        self._first_seen.clear()

    def _refresh_topology(self, now: float) -> None:
        topology = []
        for modem_path in _modem_paths(self.runner):
            modem = _run_json(["-m", modem_path], self.runner).get("modem") or {}
            sim_path = str((modem.get("generic") or {}).get("sim") or "")
            if not sim_path:
                continue
            sim_doc = _run_json(["-i", sim_path], self.runner).get("sim") or {}
            properties = sim_doc.get("properties") or {}
            iccid = _normalize_iccid(properties.get("iccid"))
            imsi = _normalize_imsi(properties.get("imsi"))
            if iccid or imsi:
                topology.append((modem_path, iccid, imsi))
        if topology != self._topology:
            self._forget_objects()
        self._topology = topology
        # Empty topology is retried quickly so modem hot-plug discovery stays responsive.
        self._topology_expires = now + (self.topology_ttl if topology else min(5.0, self.topology_ttl))

    def _read(self, sms_path: str) -> dict | None:
        """One SMS object's detail in the scanner's own terms; None when it cannot be read."""
        doc = _run_json(["-s", sms_path], self.runner)
        sms = doc.get("sms")
        if not isinstance(sms, dict):
            return None
        content, props = sms.get("content") or {}, sms.get("properties") or {}
        timestamp = str(props.get("timestamp") or "")
        return {
            "peer": str(content.get("number") or ""),
            "body": _sms_text(content.get("text")),
            "content": content,
            "state": str(props.get("state") or "").lower(),
            "pdu_type": str(props.get("pdu-type") or "").lower(),
            "storage": str(props.get("storage") or "").lower(),
            "ts": _timestamp(timestamp),
            "timestamp_raw": timestamp,
            "signature": hashlib.sha256("\0".join((
                str(content.get("number") or ""), str(content.get("text") or ""),
                str(content.get("data") or ""), str(props.get("pdu-type") or ""),
                timestamp)).encode("utf-8", "surrogatepass")).hexdigest(),
        }

    def _delete(self, key: tuple[str, str], signature: str) -> bool:
        """Delete one object, but only if it still holds the message that was stored.

        ModemManager renumbers objects when it restarts, so a cached path may by now name a
        message that has not been imported. Re-reading it first costs one mmcli call per
        deletion and makes deleting an unimported message impossible.
        """
        attempts = self._delete_attempts.get(key, 0)
        if attempts >= _DELETE_ATTEMPTS:
            return False
        modem_path, sms_path = key
        current = self._read(sms_path)
        if not current or current["signature"] != signature:
            self._details.pop(key, None)
            self._settled.pop(key, None)
            return False
        self._delete_attempts[key] = attempts + 1
        if not _delete_object(modem_path, sms_path, self.runner):
            return False
        self._delete_attempts.pop(key, None)
        self._details.pop(key, None)
        self._settled.pop(key, None)
        return True

    def _prune_when_full(self, modem_path: str, live: list[tuple[str, str]], now: float) -> None:
        cached = self._capacity.get(modem_path)
        if cached and now < cached[0]:
            capacity = cached[1]
        else:
            capacity = _storage_capacity(modem_path, self.runner)
            self._capacity[modem_path] = (now + _CAPACITY_TTL, capacity)
        details = {key: self._details[key][1] for key in live if key in self._details}
        if capacity:
            storage, used, total = capacity
            candidates = [key for key, detail in details.items()
                          if detail["storage"] in ("", storage)]
        else:
            used, total = len(live), storage_limit(self.environ)
            candidates = list(details)
        excess = used - (total - _STORAGE_HEADROOM)
        if excess <= 0:
            return
        oldest = sorted((key for key in candidates
                         if self._settled.get(key) == details[key]["signature"]),
                        key=lambda key: (details[key]["ts"] or 0, key[1]))
        removed = 0
        for key in oldest:
            if removed >= excess:
                break
            if self._delete(key, details[key]["signature"]):
                removed += 1
        if removed and capacity:
            # The modem's own count is authoritative; read it again next time.
            self._capacity.pop(modem_path, None)

    def poll(self, instances: list[dict], ingest=None, *, policy: str = "delete") -> list:
        """Import every readable SMS object, then apply the storage policy.

        ``ingest(record)`` stores one record and returns what it stored, or None for a message
        already held; it raises when the database cannot take it, which leaves the object
        untouched for the next poll. Without ``ingest`` this is a read-only listing that
        returns the records themselves and deletes nothing.
        """
        policy = policy if policy in STORAGE_POLICIES else "delete"
        now = self.clock()
        daemon_epoch = self.epoch_getter()
        if daemon_epoch != self._daemon_epoch:
            # Object paths and their cached details belong to one ModemManager generation.
            self._forget_objects()
            self._daemon_epoch = daemon_epoch
        if now >= self._topology_expires:
            self._refresh_topology(now)
        by_iccid = {_normalize_iccid(item.get("iccid")): str(item.get("id")) for item in instances
                    if item.get("iccid") and item.get("id") is not None}
        # Modules that expose no ICCID through ModemManager are matched on IMSI instead.
        by_imsi = {_normalize_imsi(item.get("imsi")): str(item.get("id")) for item in instances
                   if item.get("imsi") and item.get("iccid") and item.get("id") is not None}
        line_iccids = {str(item.get("id")): _normalize_iccid(item.get("iccid"))
                       for item in instances if item.get("id") is not None}
        found = []
        live_keys = set()
        for modem_path, modem_iccid, modem_imsi in self._topology:
            if modem_iccid:
                iid = by_iccid.get(modem_iccid)
            else:
                iid = by_imsi.get(modem_imsi) if modem_imsi else None
            if not iid:
                continue
            # Serialize the path snapshot with local object creation; otherwise the poller could
            # read a newly-created submit object before send() has bound its path.
            with _local_sms_lock:
                listing = _run_json(["-m", modem_path, "--messaging-list-sms"], self.runner)
            raw_paths = listing.get("modem.messaging.sms")
            paths = [str(path) for path in raw_paths
                     if SMS_PATH_RE.fullmatch(str(path))] if isinstance(raw_paths, list) else []
            modem_live = []
            for sms_path in paths:
                key = (modem_path, sms_path)
                live_keys.add(key)
                modem_live.append(key)
                self._first_seen.setdefault(key, now)
                cached = self._details.get(key)
                if cached and now < cached[0]:
                    detail = cached[1]
                else:
                    detail = self._read(sms_path)
                    if detail is None:
                        continue
                    if detail["state"] in ("receiving", "sending"):
                        # A multi-part text still collecting its parts, or a submit still in
                        # flight: its final form is not readable yet.
                        self._details.pop(key, None)
                        continue
                    self._details[key] = (now + self.detail_ttl, detail)
                if ingest is not None and self._settled.get(key) == detail["signature"]:
                    if policy == "delete":
                        self._delete(key, detail["signature"])
                    continue
                data = b""
                if not detail["body"].strip():
                    # No readable text: a binary payload -- an MMS notification (WAP Push),
                    # a SIM data download -- or nothing at all. Only a payload is imported.
                    data = _sms_data(detail["content"].get("data"))
                    if not data or detail["pdu_type"] == "submit":
                        continue
                direction = "out" if detail["pdu_type"] == "submit" else "in"
                record = {"instance": iid, "direction": direction, "peer": detail["peer"],
                          "body": detail["body"], "ts": detail["ts"], "transport": "cellular",
                          "modem_path": modem_path, "sms_path": sms_path,
                          "storage": detail["storage"], "data": data}
                if direction == "in" and not data:
                    # The marker 1.9.x recorded for this object; see ingest_message.
                    record["legacy_fingerprint"] = hashlib.sha256("\0".join((
                        line_iccids.get(iid) or modem_iccid, sms_path, "in", detail["peer"],
                        detail["body"], detail["timestamp_raw"])).encode()).hexdigest()
                if ingest is None:
                    found.append(record)
                    continue
                if direction == "out" and self.local_sms_tracker is not None:
                    try:
                        own = self.local_sms_tracker.owns_local_modem_sms(
                            iid, modem_path, sms_path, detail["peer"], detail["body"])
                    except Exception:
                        # Never import the gateway's own send as someone else's while the
                        # database cannot tell; retry on the next poll.
                        continue
                    if own:
                        # Its history row already exists; the object itself is only clutter
                        # once sent. Before that, send() may be about to submit it.
                        if (detail["state"] == "sent"
                                or now - self._first_seen[key] >= _OWN_OBJECT_GRACE):
                            self._delete(key, detail["signature"])
                        continue
                try:
                    stored = ingest(record)
                except Exception:
                    continue
                self._settled[key] = detail["signature"]
                if stored:
                    found.append(stored)
                if policy == "delete":
                    self._delete(key, detail["signature"])
            if ingest is not None and policy == "when_full" and listing.get("modem.messaging.sms") is not None:
                self._prune_when_full(modem_path, modem_live, now)
        # Bound memory when ModemManager deletes SMS objects or a SIM is no longer configured.
        self._details = {key: value for key, value in self._details.items() if key in live_keys}
        self._settled = {key: value for key, value in self._settled.items() if key in live_keys}
        self._delete_attempts = {key: value for key, value in self._delete_attempts.items()
                                 if key in live_keys}
        self._first_seen = {key: value for key, value in self._first_seen.items()
                            if key in live_keys}
        return found

    def discover(self, instances: list[dict]) -> list[dict]:
        """Return displayable cellular SMS records without storing or deleting anything."""
        return self.poll(instances)


def discover(instances: list[dict], runner=subprocess.run) -> list[dict]:
    """One-shot read used by diagnostics and callers outside the poller: side-effect free."""
    return Scanner(runner, epoch_getter=lambda: "").discover(instances)

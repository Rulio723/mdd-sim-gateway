"""Send and import SMS objects owned by ModemManager.

VoWiFi SMS arrives through Asterisk. When a modem is also registered on the cellular network,
ModemManager independently receives ordinary 3GPP SMS and keeps them as D-Bus SMS objects.
This module uses ModemManager without taking over the modem's serial port and maps each operation
to the saved SIM line by ICCID.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
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

# A locally-created ModemManager SMS is also visible to the receive poller. Remember it long
# enough for every live Scanner to claim the path, then let each Scanner suppress that object
# until ModemManager removes it. This avoids a second copy alongside the record written by the
# send API while keeping the process-wide registry bounded.
_LOCAL_SMS_TTL = 3600.0
_LOCAL_SMS_LIMIT = 512
_local_sms_paths: OrderedDict[tuple[str, str, str], float] = OrderedDict()
_local_sms_lock = threading.RLock()


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
              uncertain: bool = False, reservation_id: int | None = None) -> dict:
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
    if reservation_id is not None:
        response["_reservation_id"] = int(reservation_id)
    return response


def _content_hash(recipient: str, text: str) -> str:
    """Stable, non-plaintext identity shared by the sender and receive scanner."""
    return hashlib.sha256(f"{recipient}\0{text}".encode("utf-8")).hexdigest()


def _normalize_iccid(value) -> str:
    """Return the canonical comparison/storage form for a modem or configured ICCID."""
    return str(value or "").strip().casefold()


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


def _remember_local_sms(modem_path: str, iccid: str, sms_path: str,
                        now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    key = (modem_path, iccid, sms_path)
    with _local_sms_lock:
        for old_key, expiry in list(_local_sms_paths.items()):
            if expiry <= now:
                _local_sms_paths.pop(old_key, None)
        _local_sms_paths.pop(key, None)
        _local_sms_paths[key] = now + _LOCAL_SMS_TTL
        while len(_local_sms_paths) > _LOCAL_SMS_LIMIT:
            _local_sms_paths.popitem(last=False)


def _is_local_sms(modem_path: str, iccid: str, sms_path: str, now: float) -> bool:
    key = (modem_path, iccid, sms_path)
    with _local_sms_lock:
        expiry = _local_sms_paths.get(key)
        if expiry is None:
            return False
        if expiry <= now:
            _local_sms_paths.pop(key, None)
            return False
        return True


def _instance_iccid(instances: list[dict], instance_id) -> str:
    iid = str(instance_id)
    for item in instances or []:
        if isinstance(item, dict) and str(item.get("id")) == iid:
            return _normalize_iccid(item.get("iccid"))
    return ""


def _find_modem(iccid: str, runner, timeout: float) -> tuple[str | None, str | None]:
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
        modem_iccid = _normalize_iccid((sim.get("properties") or {}).get("iccid")
                                       or sim_doc.get("sim.properties.iccid"))
        if modem_iccid == _normalize_iccid(iccid):
            return modem_path, None

    if inspection_failed:
        return None, "Could not find the line's SIM among the readable cellular modems."
    return None, "No cellular modem matches this line's ICCID."


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
         runner=subprocess.run, *, timeout: float = 30.0, local_sms_tracker=None,
         epoch_getter=_modemmanager_epoch) -> dict:
    """Send an SMS through the modem containing ``instance_id``'s ICCID.

    This synchronous function is intended to be called with ``asyncio.to_thread``. It never
    raises for an ordinary ModemManager failure and always returns ``ok``, ``status``, ``error``,
    ``modem_path`` and ``sms_path``. A send timeout is deliberately reported as ``unknown``:
    retrying automatically could charge for and deliver a duplicate SMS.
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
    modem_path, problem = _find_modem(iccid, runner, timeout)
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

    # A locally-created submit object is also returned by ModemManager's receive listing. Make a
    # durable intent before creating it so a control-plane restart cannot turn our own outgoing
    # message into a second, apparently successful history row. Sending without this protection
    # is unsafe and therefore refused.
    if local_sms_tracker is None:
        return _response(instance_id, ok=False, status="failed",
                         error="Durable cellular SMS tracking is unavailable.", stage="track",
                         modem_path=modem_path)
    daemon_epoch = epoch_getter()
    if not daemon_epoch:
        return _response(instance_id, ok=False, status="failed",
                         error="Could not identify the active ModemManager service; SMS was not sent.",
                         stage="track", modem_path=modem_path)
    content_hash = _content_hash(recipient, text)
    try:
        reservation_id = local_sms_tracker.reserve_local_modem_sms(
            str(instance_id), iccid, content_hash, daemon_epoch, recipient, text)
    except Exception:
        return _response(instance_id, ok=False, status="failed",
                         error="Could not durably track the cellular SMS; it was not sent.",
                         stage="track", modem_path=modem_path)
    if not reservation_id:
        return _response(instance_id, ok=False, status="failed",
                         error="Could not durably track the cellular SMS; it was not sent.",
                         stage="track", modem_path=modem_path)

    # mmcli 1.20 has no --messaging-create-sms-with-text option, while the underlying D-Bus
    # Create(a{sv}) method has supported Number and Text since ModemManager 1.0. Passing an argv
    # list (never a shell command) preserves punctuation and quotes as one typed string value.
    create_args = [
        "org.freedesktop.ModemManager1", modem_path,
        "org.freedesktop.ModemManager1.Modem.Messaging", "Create",
        "a{sv}", "2", "number", "s", recipient, "text", "s", text,
    ]
    # Scanner takes this same lock while obtaining the modem's SMS path snapshot, so it cannot
    # observe the new object in the tiny gap before its path is registered.
    with _local_sms_lock:
        create_result, problem = _invoke_busctl(create_args, runner, timeout)
        if not problem and not getattr(create_result, "returncode", 1):
            sms_path = _created_sms_path(create_result)
            if sms_path:
                # The D-Bus owner must still be the one that accepted Create. A daemon restart
                # here makes the returned numeric path ambiguous, so never send it through the
                # new owner.
                if epoch_getter() != daemon_epoch:
                    return _response(
                        instance_id, ok=False, status="failed",
                        error="ModemManager restarted while creating the SMS; it was not sent.",
                        stage="track", modem_path=modem_path, sms_path=sms_path,
                        reservation_id=reservation_id)
                try:
                    tracked = bool(local_sms_tracker.bind_local_modem_sms(
                        reservation_id, daemon_epoch, modem_path, sms_path))
                except Exception:
                    tracked = False
                if not tracked:
                    return _response(
                        instance_id, ok=False, status="failed",
                        error="Could not durably bind the cellular SMS object; it was not sent.",
                        stage="track", modem_path=modem_path, sms_path=sms_path,
                        reservation_id=reservation_id)
                _remember_local_sms(modem_path, iccid, sms_path)
            else:
                try:
                    local_sms_tracker.cancel_local_modem_sms(reservation_id)
                except Exception:
                    pass
                return _response(
                    instance_id, ok=False, status="failed",
                    error="ModemManager returned an invalid SMS object path.",
                    stage="create", modem_path=modem_path, reservation_id=reservation_id)
        else:
            sms_path = None

    if problem == "timeout":
        # Create may have succeeded even though its reply timed out. Keep the reservation so a
        # restarted Scanner can claim and suppress the draft object if it appears later.
        return _response(instance_id, ok=False, status="failed",
                         error="Timed out while creating the cellular SMS.", stage="create",
                         modem_path=modem_path, reservation_id=reservation_id)
    if problem or getattr(create_result, "returncode", 1):
        try:
            local_sms_tracker.cancel_local_modem_sms(reservation_id)
        except Exception:
            pass
        error = ("Could not run ModemManager while creating the SMS." if problem else
                 _command_error(create_result, "ModemManager could not create the SMS."))
        return _response(instance_id, ok=False, status="failed", error=error,
                         stage="create", modem_path=modem_path,
                         reservation_id=reservation_id)

    send_result, problem = _invoke(
        ["-s", sms_path, "--send", "--output-json"], runner, timeout)
    if problem == "timeout":
        return _response(
            instance_id, ok=False, status="unknown",
            error="Cellular SMS send timed out; delivery is unknown and was not retried.",
            stage="send", modem_path=modem_path, sms_path=sms_path, uncertain=True,
            reservation_id=reservation_id)
    if problem or getattr(send_result, "returncode", 1):
        error = ("Could not run ModemManager while sending the SMS." if problem else
                 _command_error(send_result, "ModemManager rejected the SMS."))
        return _response(instance_id, ok=False, status="failed", error=error,
                         stage="send", modem_path=modem_path, sms_path=sms_path,
                         reservation_id=reservation_id)
    return _response(instance_id, ok=True, status="sent", error=None, stage="send",
                     modem_path=modem_path, sms_path=sms_path,
                     reservation_id=reservation_id)


def _timestamp(value) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError):
        return 0


class Scanner:
    """Incrementally inspect ModemManager without rereading stable objects every five seconds.

    The SMS path listing remains live on every poll, so a newly arrived message is discovered
    without extra delay. Modem/SIM identity and already-seen SMS details are much more stable and
    are refreshed periodically to tolerate ModemManager restarts and object-path reuse.
    """

    def __init__(self, runner=subprocess.run, *, topology_ttl: float = 60.0,
                 detail_ttl: float = 60.0, clock=time.monotonic,
                 local_sms_tracker=None, epoch_getter=_modemmanager_epoch):
        self.runner = runner
        self.topology_ttl = topology_ttl
        self.detail_ttl = detail_ttl
        self.clock = clock
        self.local_sms_tracker = local_sms_tracker
        self.epoch_getter = epoch_getter
        self._daemon_epoch = ""
        self._topology_expires = 0.0
        self._topology: list[tuple[str, str]] = []
        self._details: dict[tuple[str, str], tuple[float, dict]] = {}
        self._local_sms_keys: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    def _refresh_topology(self, now: float) -> None:
        topology = []
        for modem_path in _modem_paths(self.runner):
            modem = _run_json(["-m", modem_path], self.runner).get("modem") or {}
            sim_path = str((modem.get("generic") or {}).get("sim") or "")
            if not sim_path:
                continue
            sim_doc = _run_json(["-i", sim_path], self.runner).get("sim") or {}
            iccid = _normalize_iccid((sim_doc.get("properties") or {}).get("iccid"))
            if iccid:
                topology.append((modem_path, iccid))
        if topology != self._topology:
            self._details.clear()
            self._local_sms_keys.clear()
        self._topology = topology
        # Empty topology is retried quickly so modem hot-plug discovery stays responsive.
        self._topology_expires = now + (self.topology_ttl if topology else min(5.0, self.topology_ttl))

    def discover(self, instances: list[dict]) -> list[dict]:
        """Return displayable cellular SMS records. No message body or identity is logged."""
        now = self.clock()
        daemon_epoch = self.epoch_getter() if self.local_sms_tracker is not None else ""
        if daemon_epoch != self._daemon_epoch:
            # Object paths and their cached details belong to one ModemManager generation.
            self._details.clear()
            self._local_sms_keys.clear()
            self._daemon_epoch = daemon_epoch
        if now >= self._topology_expires:
            self._refresh_topology(now)
        by_iccid = {_normalize_iccid(item.get("iccid")): str(item.get("id")) for item in instances
                    if item.get("iccid") and item.get("id") is not None}
        found = []
        live_keys = set()
        live_local_keys = set()
        for modem_path, iccid in self._topology:
            iid = by_iccid.get(iccid)
            if not iid:
                continue
            # Serialize the path snapshot with local object creation; otherwise the poller could
            # import a newly-created submit object before send() has learned its D-Bus path.
            with _local_sms_lock:
                listing = _run_json(["-m", modem_path, "--messaging-list-sms"], self.runner)
                raw_paths = listing.get("modem.messaging.sms")
                paths = raw_paths if isinstance(raw_paths, list) else []
                # Only an explicit, fully valid list is authoritative enough for pruning. A
                # command/JSON failure also produces {}, which must never look like an empty
                # modem and erase durable local-send markers.
                listing_complete = (isinstance(raw_paths, list)
                                     and all(SMS_PATH_RE.fullmatch(str(path))
                                             for path in raw_paths))
            for sms_path in paths:
                sms_path = str(sms_path)
                if not SMS_PATH_RE.match(sms_path):
                    continue
                key = (modem_path, sms_path)
                live_keys.add(key)
                local_key = (modem_path, iccid, sms_path)
                live_local_keys.add(local_key)
                # Durable classification below also checks a content hash, so do not let the
                # process-local path-only cache hide a different object when ModemManager reuses
                # a numeric path. The path-only fallback is retained for compatibility scanners
                # that have no persistence adapter.
                if self.local_sms_tracker is None and (
                        local_key in self._local_sms_keys
                        or _is_local_sms(modem_path, iccid, sms_path, now)):
                    self._local_sms_keys.pop(local_key, None)
                    self._local_sms_keys[local_key] = None
                    while len(self._local_sms_keys) > _LOCAL_SMS_LIMIT:
                        self._local_sms_keys.popitem(last=False)
                    self._details.pop(key, None)
                    continue
                cached = self._details.get(key)
                if cached and now < cached[0]:
                    record = cached[1]
                else:
                    sms = _run_json(["-s", sms_path], self.runner).get("sms") or {}
                    content, props = sms.get("content") or {}, sms.get("properties") or {}
                    text, peer = str(content.get("text") or ""), str(content.get("number") or "")
                    if not text.strip():
                        self._details.pop(key, None)
                        continue
                    pdu_type = str(props.get("pdu-type") or "").lower()
                    direction = "out" if pdu_type == "submit" else "in"
                    timestamp = str(props.get("timestamp") or "")
                    signature_parts = [iccid, sms_path, direction, peer, text, timestamp]
                    if direction == "out":
                        # Numeric object paths restart with ModemManager. Outgoing objects often
                        # have no network timestamp, so the daemon generation is required to keep
                        # a new external send from colliding with an old import. Inbound identity
                        # stays backward-compatible and relies on its network timestamp.
                        signature_parts.append(daemon_epoch)
                    signature = "\0".join(signature_parts)
                    record = {
                        "fingerprint": hashlib.sha256(signature.encode()).hexdigest(),
                        "direction": direction, "peer": peer, "body": text,
                        "ts": _timestamp(timestamp), "transport": "cellular",
                    }
                    self._details[key] = (now + self.detail_ttl, record)
                if record["direction"] == "out" and self.local_sms_tracker is not None:
                    if not daemon_epoch:
                        # Without a daemon generation we cannot distinguish a locally-created
                        # object from a reused path. Delay outgoing import; inbound SMS remains
                        # available and classification is retried on the next poll.
                        continue
                    try:
                        is_local = self.local_sms_tracker.is_local_modem_sms(
                            daemon_epoch, iccid, modem_path, sms_path,
                            _content_hash(record["peer"], record["body"]), record["ts"])
                    except Exception:
                        # A temporary database problem must not turn an already-sent local object
                        # into a new success row. Retry classification on the next polling cycle.
                        continue
                    if is_local:
                        continue
                found.append({**record, "instance": iid})
            if listing_complete and daemon_epoch and self.local_sms_tracker is not None:
                pruner = getattr(self.local_sms_tracker, "prune_local_modem_sms", None)
                if callable(pruner):
                    try:
                        pruner(daemon_epoch, iccid, modem_path, set(map(str, paths)))
                    except Exception:
                        # Retaining an obsolete marker is safer than losing local-send identity.
                        pass
        # Bound memory when ModemManager deletes SMS objects or a SIM is no longer configured.
        self._details = {key: value for key, value in self._details.items() if key in live_keys}
        for key in list(self._local_sms_keys):
            if key not in live_local_keys:
                self._local_sms_keys.pop(key, None)
        return found


def discover(instances: list[dict], runner=subprocess.run) -> list[dict]:
    """One-shot compatibility wrapper used by diagnostics and callers outside the poller."""
    return Scanner(runner).discover(instances)

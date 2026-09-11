"""Experimental circuit-switched/VoLTE call control through ModemManager.

This module deliberately controls signalling only. It does not claim or route the modem's
PCM/USB audio interface, so callers must treat a connected call as silent unless a separate
hardware audio path is configured.
"""
from __future__ import annotations

import math
import re
import subprocess

from . import cellular_sms

CALL_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/Call/\d+$")
NUMBER_RE = re.compile(r"^\+?\d{1,32}$")
REGISTERED_STATES = {"home", "roaming"}
USABLE_MODEM_STATES = {"enabled", "searching", "registered", "connecting", "connected"}


def _response(instance_id, *, ok=False, status="failed", error=None, stage="",
              modem_path=None, call_path=None, uncertain=False, number="") -> dict:
    return {"ok": bool(ok), "status": str(status), "error": error, "stage": stage,
            "transport": "cellular", "instance": str(instance_id),
            "modem_path": modem_path, "call_path": call_path,
            "uncertain": bool(uncertain), "unavailable": status == "unavailable",
            "number": str(number or ""), "audio": False}


def _valid_timeout(timeout) -> bool:
    return (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
            and math.isfinite(timeout) and timeout > 0)


def _modem_for_line(instances, instance_id, runner, timeout):
    iccid = cellular_sms._instance_iccid(instances, instance_id)
    if not iccid:
        return None, "The line has no configured ICCID."
    return cellular_sms._find_modem(
        iccid, runner, timeout,
        imsi=cellular_sms._instance_imsi(instances, instance_id))


def _check_ready(modem_path: str, runner, timeout: float) -> str | None:
    voice, problem = cellular_sms._invoke(
        ["-m", modem_path, "--voice-status", "--output-json"], runner, timeout)
    if problem == "timeout":
        return "Timed out while checking modem voice support."
    if problem or getattr(voice, "returncode", 1):
        return ("This modem does not expose ModemManager voice calling." if problem else
                cellular_sms._command_error(
                    voice, "This modem does not expose ModemManager voice calling."))

    detail, problem = cellular_sms._invoke(
        ["-m", modem_path, "--output-json"], runner, timeout)
    if problem or getattr(detail, "returncode", 1):
        return "Could not read the cellular modem state."
    doc = cellular_sms._decode_json(detail)
    modem = doc.get("modem") or {}
    state = str((modem.get("generic") or {}).get("state")
                or doc.get("modem.generic.state") or "").casefold()
    registration = str((modem.get("3gpp") or {}).get("registration-state")
                       or doc.get("modem.3gpp.registration-state") or "").casefold()
    if state not in USABLE_MODEM_STATES:
        return "The cellular modem is disabled or not ready. Enable its radio first."
    if registration not in REGISTERED_STATES:
        return "The modem is not registered on a cellular voice network."
    return None


def _created_call_path(result) -> str:
    doc = cellular_sms._decode_json(result)
    modem = doc.get("modem") or {}
    path = str((modem.get("voice") or {}).get("created-call")
               or doc.get("modem.voice.created-call") or "")
    if not path:
        match = re.search(r"/org/freedesktop/ModemManager1/Call/\d+",
                          getattr(result, "stdout", "") or "")
        path = match.group(0) if match else ""
    return path if CALL_PATH_RE.fullmatch(path) else ""


def dial(instances: list[dict], instance_id, number: str, runner=subprocess.run,
         timeout: float = 30.0) -> dict:
    number = str(number or "").strip()
    if not NUMBER_RE.fullmatch(number):
        return _response(instance_id, status="failed", stage="validate", number=number,
                         error="Number must contain only digits with an optional leading +.")
    if not _valid_timeout(timeout):
        return _response(instance_id, status="failed", stage="validate", number=number,
                         error="The ModemManager timeout must be positive.")
    modem_path, problem = _modem_for_line(instances, instance_id, runner, timeout)
    if not modem_path:
        return _response(instance_id, status="unavailable", stage="lookup", number=number,
                         error=problem or "No matching cellular modem is available.")
    problem = _check_ready(modem_path, runner, timeout)
    if problem:
        return _response(instance_id, status="unavailable", stage="check", number=number,
                         modem_path=modem_path, error=problem)

    created, problem = cellular_sms._invoke(
        ["-m", modem_path, f"--voice-create-call=number={number}", "--output-json"],
        runner, timeout)
    if problem == "timeout":
        return _response(instance_id, status="failed", stage="create", number=number,
                         modem_path=modem_path,
                         error="Timed out while creating the cellular call; it was not started.")
    if problem or getattr(created, "returncode", 1):
        error = ("Could not run ModemManager while creating the call." if problem else
                 cellular_sms._command_error(created, "ModemManager could not create the call."))
        return _response(instance_id, status="failed", stage="create", number=number,
                         modem_path=modem_path, error=error)
    call_path = _created_call_path(created)
    if not call_path:
        return _response(instance_id, status="failed", stage="create", number=number,
                         modem_path=modem_path,
                         error="ModemManager returned an invalid call object path.")

    started, problem = cellular_sms._invoke(
        ["-o", call_path, "--start", "--output-json"], runner, timeout)
    if problem == "timeout":
        return _response(instance_id, status="unknown", stage="start", number=number,
                         modem_path=modem_path, call_path=call_path, uncertain=True,
                         error="Cellular call start timed out; use Hang up before retrying.")
    if problem or getattr(started, "returncode", 1):
        # Creation alone does not dial. Remove the unused object as a bounded rollback.
        cellular_sms._invoke(["-m", modem_path, f"--voice-delete-call={call_path}"],
                             runner, timeout)
        error = ("Could not run ModemManager while starting the call." if problem else
                 cellular_sms._command_error(started, "ModemManager rejected the call."))
        return _response(instance_id, status="failed", stage="start", number=number,
                         modem_path=modem_path, call_path=call_path, error=error)
    return _response(instance_id, ok=True, status="dialing", stage="start", number=number,
                     modem_path=modem_path, call_path=call_path)


def _call_paths(result) -> list[str]:
    doc = cellular_sms._decode_json(result)
    values = ((doc.get("modem") or {}).get("voice") or {}).get("call")
    if values is None:
        values = doc.get("modem.voice.call") or []
    if isinstance(values, str):
        values = [values]
    return [str(value) for value in values or [] if CALL_PATH_RE.fullmatch(str(value))]


def _call_detail(result, path: str) -> dict:
    doc = cellular_sms._decode_json(result)
    props = (doc.get("call") or {}).get("properties") or {}
    state = str(props.get("state") or doc.get("call.properties.state") or "unknown")
    reason = str(props.get("state-reason") or doc.get("call.properties.state-reason") or "")
    number = str(props.get("number") or doc.get("call.properties.number") or "")
    direction = str(props.get("direction") or doc.get("call.properties.direction") or "")
    return {"call_path": path, "state": state, "reason": reason,
            "number": number, "direction": direction, "audio": False}


def status(instances: list[dict], instance_id, runner=subprocess.run,
           timeout: float = 10.0) -> dict:
    if not _valid_timeout(timeout):
        return _response(instance_id, status="failed", stage="validate",
                         error="The ModemManager timeout must be positive.")
    modem_path, problem = _modem_for_line(instances, instance_id, runner, timeout)
    if not modem_path:
        return _response(instance_id, status="unavailable", stage="lookup",
                         error=problem or "No matching cellular modem is available.")
    listing, problem = cellular_sms._invoke(
        ["-m", modem_path, "--voice-list-calls", "--output-json"], runner, timeout)
    if problem or getattr(listing, "returncode", 1):
        return _response(instance_id, status="unavailable", stage="list",
                         modem_path=modem_path,
                         error="Could not read cellular call status.")
    calls = []
    for path in _call_paths(listing):
        detail, detail_problem = cellular_sms._invoke(
            ["-o", path, "--output-json"], runner, timeout)
        if not detail_problem and not getattr(detail, "returncode", 1):
            calls.append(_call_detail(detail, path))
    priority = {"active": 0, "ringing-out": 1, "dialing": 2, "ringing-in": 3,
                "waiting": 4, "held": 5, "terminated": 9, "unknown": 10}
    calls.sort(key=lambda item: priority.get(item["state"], 8))
    current = calls[0] if calls else None
    return {**_response(instance_id, ok=True, status=current["state"] if current else "idle",
                        stage="status", modem_path=modem_path,
                        call_path=current["call_path"] if current else None,
                        number=current["number"] if current else ""),
            "call": current, "calls": calls}


def hangup(instances: list[dict], instance_id, runner=subprocess.run,
           timeout: float = 15.0) -> dict:
    if not _valid_timeout(timeout):
        return _response(instance_id, status="failed", stage="validate",
                         error="The ModemManager timeout must be positive.")
    modem_path, problem = _modem_for_line(instances, instance_id, runner, timeout)
    if not modem_path:
        return _response(instance_id, status="unavailable", stage="lookup",
                         error=problem or "No matching cellular modem is available.")
    result, problem = cellular_sms._invoke(
        ["-m", modem_path, "--voice-hangup-all", "--output-json"], runner, timeout)
    if problem == "timeout":
        return _response(instance_id, status="unknown", stage="hangup",
                         modem_path=modem_path, uncertain=True,
                         error="Cellular hangup timed out; call state is unknown.")
    if problem or getattr(result, "returncode", 1):
        error = ("Could not run ModemManager while hanging up." if problem else
                 cellular_sms._command_error(result, "ModemManager could not hang up."))
        return _response(instance_id, status="failed", stage="hangup",
                         modem_path=modem_path, error=error)
    return _response(instance_id, ok=True, status="ended", stage="hangup",
                     modem_path=modem_path)

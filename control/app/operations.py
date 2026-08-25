"""Safe local administration helpers for the unified WebUI.

Backups stay on the gateway (they contain credentials). Support bundles are downloadable but
strictly redacted and contain only configuration shape, status and bounded log tails.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import tarfile
import time
import zipfile

import yaml

from . import config as cfg


_SECRET_KEYS = re.compile(
    r"pin|puk|password|secret|token|credential|imsi|iccid|imei|msisdn|eid|"
    r"carrier_identity|gid1|gid2|\bspn\b|subscription|proxy_url|webhook_url|headers?|"
    r"activation|matching_id|confirmation|smdp",
    re.I,
)
_LONG_DIGITS = re.compile(r"(?<!\d)\+?\d{10,40}(?!\d)")
_URL = re.compile(r"\b(?:https?|socks5h?)://[^\s\"'<>]+", re.I)
_ACTIVATION_CODE = re.compile(r"\bLPA:1\$[^\s\"'<>]+", re.I)
_HEX_BLOB = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{24,}(?![0-9A-Fa-f])")
_KEY_MATERIAL = re.compile(
    r"(?:\b(?:CK|IK|MK|XKEY|KENCR|KAUT|MSK|EMSK|SKEYSEED|SK_[A-Z_]+|"
    r"NONCE|RAND|AUTN|AUTS|RES)\b\s*(?::|=|\s)\s*(?:0x)?[0-9A-Fa-f]{8,}\b|"
    r"\bDIFFIE[- ]HELLMAN KEY\b)",
    re.I,
)
_MULTILINE_SECRET = re.compile(
    r"(?:IKEv2 DECRYPTION TABLE|ESP SA INFO|received decoded message|DECRYPTED DATA)", re.I
)


def redact(value, key: str = ""):
    if _SECRET_KEYS.search(key):
        return "<redacted>" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        value = _ACTIVATION_CODE.sub("<redacted-activation-code>", value)
        value = _URL.sub("<redacted-url>", value)
        value = _LONG_DIGITS.sub("<redacted-number>", value)
        return _HEX_BLOB.sub("<redacted-secret>", value)
    return value


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "gateway"


def redact_log(text: str) -> str:
    """Redact a plain log file line by line.

    Blanking the whole line is right here: an unlabelled key row carries nothing else, and a
    log line that mentions key material is not worth reconstructing. ``redact_jsonl`` exists
    because that trade is wrong for a structured record.
    """
    lines = []
    redact_following = 0
    for line in text.splitlines():
        if redact_following:
            lines.append("<redacted cryptographic material>")
            redact_following -= 1
        elif _MULTILINE_SECRET.search(line):
            lines.append("<redacted cryptographic material>")
            # Wireshark IKE/ESP tables emit two unlabelled key rows after the heading.
            redact_following = 2
        elif _KEY_MATERIAL.search(line):
            lines.append("<redacted cryptographic material>")
        else:
            lines.append(str(redact(line)))
    return "\n".join(lines)


def _redact_record(value, key: str = ""):
    """Redact inside a diagnostic record, blanking only the strings that carry key material.

    Same rules as ``redact_log``, applied per string rather than per line. A captured log tail
    is a list of log lines, so each element gets exactly the treatment it would have got in
    its own file, and the registration/SIP/host evidence beside it survives.
    """
    if _SECRET_KEYS.search(key):
        return "<redacted>" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return {str(k): _redact_record(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_record(item, key) for item in value]
    if isinstance(value, str):
        if _MULTILINE_SECRET.search(value) or _KEY_MATERIAL.search(value):
            return "<redacted cryptographic material>"
        return redact(value)
    return value


def redact_jsonl(text: str) -> str:
    """Redact a JSON-lines diagnostic file record by record.

    One record is one line, so ``redact_log``'s line rules blank an entire record — and, via
    the two-line lookahead, the two records after it — as soon as any captured log tail inside
    it mentions key material. The engine's own tunnel log says ``received decoded message`` on
    every fragmented exchange, so in practice that wiped every record of the one file written
    specifically to outlive a rebuild loop (see ``engine.capture_diagnostics``). Redact the
    parsed structure instead; a record that will not parse still falls back to the line rules.
    """
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            lines.append(redact_log(line))
            continue
        lines.append(json.dumps(_redact_record(record), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


CALL_EVENTS = ("call_out", "call_result", "ussd")


def call_event_evidence(text: str) -> str:
    """Extract just enough of the engine's call events to check a service-code verdict.

    The UI's verdict for a dialled service code ("the carrier does not support this code",
    "the carrier refused it") is derived from the Q.850 cause on call_result, and that cause
    is recorded ONLY here. Without it a user's report cannot be checked against what the
    carrier actually said — the bundle showed the conclusion but never the evidence.

    The whole file cannot be shipped: it also carries dialled subscriber numbers and message
    bodies, and the key-name redaction rules do not reach them because they sit inside an
    `args` array. So keep the diagnosis and drop the identity — a service code is a carrier's
    own public number and is precisely what has to be diagnosable, whereas a number a user
    dialled is not, and neither is the text of a reply.
    """
    out = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = str(record.get("event") or "")
        if event not in CALL_EVENTS:
            continue
        args = [str(a) for a in (record.get("args") or [])]
        # call_result is "<direction> <peer> <dialstatus> <cause>"; the others lead with peer.
        peer_at = 1 if (event == "call_result" and args and args[0] in ("in", "out")) else 0
        if peer_at < len(args) and not any(ch in args[peer_at] for ch in "*#"):
            args[peer_at] = "<number>"
        if event == "ussd" and len(args) > 1:
            # That a reply arrived, and how big it was, answers the question. Its text can
            # carry account details and answers nothing.
            args[1] = f"<{len(args[1])} bytes>"
        out.append(json.dumps({"instance": record.get("instance"), "event": event,
                               "args": args}, ensure_ascii=False, sort_keys=True))
    return "\n".join(out)


def create_local_backup(system_name: str = "gateway") -> dict:
    """Create a root-local recovery archive. It is intentionally not returned over HTTP."""
    root = Path(cfg.DATA_DIR).resolve()
    target_dir = root / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{_safe_name(system_name)}-{stamp}.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or target_dir in path.parents:
                continue
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
    os.chmod(target, 0o600)
    return {"ok": True, "name": target.name, "created_at": int(time.time()),
            "size": target.stat().st_size, "location": "gateway-local"}


def list_local_backups() -> list[dict]:
    root = Path(cfg.DATA_DIR) / "backups"
    result = []
    for path in sorted(root.glob("*.tar.gz"), reverse=True) if root.exists() else []:
        stat = path.stat()
        result.append({"name": path.name, "created_at": int(stat.st_mtime),
                       "size": stat.st_size, "location": "gateway-local"})
    return result[:50]


def delete_local_backup(name: str) -> dict:
    """Delete one named local backup without allowing paths outside the backup directory."""
    name = str(name or "")
    if (Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_.-]+\.tar\.gz", name)):
        raise ValueError("invalid backup name")
    root = (Path(cfg.DATA_DIR) / "backups").resolve()
    target = root / name
    if not target.is_file():
        raise FileNotFoundError(name)
    target.unlink()
    return {"ok": True, "name": name}


SERVICE_RESTART_SCOPES = ("control", "services", "host")
# The orchestrator polls a few times a minute; a request still sitting here after this long
# means nothing is consuming it, not that it is slow.
_RESTART_PICKUP_SECONDS = 60


def _service_restart_paths() -> tuple[Path, Path]:
    root = Path(cfg.DATA_DIR) / "orchestrator"
    return root / "service-restart-request.json", root / "service-restart-status.json"


def _write_private_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def request_service_restart(scope: str) -> dict:
    """Publish a service-restart request for the root orchestrator to carry out.

    The control plane can restart neither itself nor the host: it is unprivileged, and in two
    of the three scopes it is one of the processes being restarted. So it only states the
    intent, exactly as it does for self-updates, and the orchestrator detaches whatever would
    otherwise kill the process running it.
    """
    if scope not in SERVICE_RESTART_SCOPES:
        return {"ok": False, "error_code": "restart.error.invalid_scope"}
    request_path, status_path = _service_restart_paths()
    now = int(time.time())
    # Reset the visible status first so the previous restart's outcome cannot be read as this
    # one's while the orchestrator is still picking the request up.
    _write_private_json(status_path, {"state": "requested", "scope": scope, "updated_at": now})
    _write_private_json(request_path, {"scope": scope, "requested_at": now})
    return {"ok": True, "scope": scope}


def service_restart_status() -> dict:
    """Progress of the requested restart, as published by the host orchestrator."""
    request_path, status_path = _service_restart_paths()
    status = _read_json(status_path)
    status.setdefault("state", "idle")
    requested_at = int(_read_json(request_path).get("requested_at") or 0) \
        if request_path.exists() else 0
    if requested_at:
        status["requested"] = True
        # An unconsumed request means the orchestrator is not picking work up (stopped, or
        # never installed) — say so instead of leaving the page waiting for a restart that is
        # never going to happen.
        if time.time() - requested_at > _RESTART_PICKUP_SECONDS:
            status["state"] = "stalled"
            status["error_code"] = "restart.error.not_picked_up"
    return status


def host_diagnostics() -> dict:
    """Read the host orchestrator's published view, or say why it is absent.

    An empty section would read as "nothing wrong on the host"; a stopped or outdated
    orchestrator is itself a finding, so the absence is reported rather than omitted.
    """
    path = Path(cfg.DATA_DIR) / "orchestrator" / "host-diagnostics.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False,
                "note": "the host orchestrator has not published diagnostics; it may be "
                        "stopped, or older than the release that introduced this file"}
    if not isinstance(document, dict):
        return {"available": False, "note": "host diagnostics document is malformed"}
    return {"available": True, **document}


def support_bundle(status_documents: dict, log_lines: int = 500) -> bytes:
    log_lines = max(50, min(2000, int(log_lines)))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        settings = redact(cfg.get_settings())
        archive.writestr("settings-redacted.yaml", yaml.safe_dump(settings, allow_unicode=True))
        archive.writestr("status-redacted.json", json.dumps(
            redact(status_documents), ensure_ascii=False, indent=2))
        # The host orchestrator owns systemd, the USB tree and the bridge children; this
        # process runs in a container that can observe none of them. Without its published
        # view a modem fault is only diagnosable by asking the operator to run commands.
        archive.writestr("host-diagnostics-redacted.json", json.dumps(
            redact(host_diagnostics()), ensure_ascii=False, indent=2))
        base = Path(cfg.DATA_DIR) / "instances"
        # Include the current logs, compact rebuild snapshots and retained IKE segments. Every
        # source goes through the same redaction and contributes only its configured tail.
        # These globs are an allow-list on purpose. logs/voicemail/*.wav lives under the same
        # directory and must NEVER be collected: a bundle is meant to be shareable, and a
        # recording of a caller's voice cannot be redacted into something safe to share.
        paths = [*base.glob("*/run/*.log"), *base.glob("*/logs/diagnostics.jsonl"),
                 *base.glob("*/logs/ike/charon-*.log")]
        # The engine's call events are the only record of the Q.850 cause behind a service
        # code's verdict, so a bundle without them cannot answer "was it really unsupported?".
        # They are filtered rather than redacted; see call_event_evidence.
        for path in sorted(base.glob("*/logs/events.jsonl")):
            try:
                lines = path.read_text(errors="replace").splitlines()[-(log_lines * 4):]
            except OSError:
                continue
            evidence = call_event_evidence("\n".join(lines))
            if evidence:
                archive.writestr(f"logs/{path.parent.parent.name}-call-events.jsonl", evidence)
        for path in sorted(paths):
            try:
                lines = path.read_text(errors="replace").splitlines()[-log_lines:]
            except OSError:
                continue
            joined = "\n".join(lines)
            text = redact_jsonl(joined) if path.suffix == ".jsonl" else redact_log(joined)
            iid = path.parents[2].name if path.parent.name == "ike" else path.parent.parent.name
            archive.writestr(f"logs/{iid}-{path.name}", text)
        archive.writestr("manifest.json", json.dumps({
            "created_at": int(time.time()), "redacted": True,
            "contains_credentials": False, "review_before_sharing": True,
            "log_lines_per_file": log_lines,
        }, indent=2))
    return output.getvalue()

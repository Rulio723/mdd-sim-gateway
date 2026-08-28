"""Release checker + one-click update request publisher.

The control plane never applies files itself: ``request_apply`` publishes a request document
that the root host orchestrator picks up and hands to a detached ``systemd-run`` unit
(``host/mdd_update.py``), which downloads the tagged release, overlays the checkout and runs
``install.sh reload``. Progress comes back through ``update-status.json``.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from .version import VERSION

DEFAULT_REPOSITORY = "MddIdd/mdd-sim-gateway"
_cache: tuple[float, dict] | None = None
_stars_cache: int | None = None
_stars_checked_at = 0.0
_STARS_CACHE_SECONDS = 15 * 60
_MAX_RELEASE_NOTES_CHARS = 16_000
# How long a "running" progress document may go unrefreshed before it stops counting as proof
# that an update is alive. The orchestrator retires abandoned runs within a minute by asking
# systemd whether the updater unit still exists; these are the control plane's own fallback for
# a host whose orchestrator is down too, so they are deliberately generous — updaters before
# v1.3.12 published no heartbeat at all during downloads and service reloads.
_APPLY_STALE_SECONDS = 15 * 60
_APPLY_ABANDONED_SECONDS = 6 * 3600
_AUTOMATION_STATE_FILE = "automation-state.json"


class UpdateNetworkError(RuntimeError):
    pass


def validate_network_settings(value: dict | None) -> dict:
    """Validate and normalize the persisted update networking selection."""
    value = value or {}
    mode = str(value.get("proxy_mode") or "auto").strip().lower()
    if mode in {"manual", "country"}:
        mode = "auto"
    if mode not in {"auto", "direct", "library"}:
        raise UpdateNetworkError("update proxy mode must be auto, direct or library")
    result = {"proxy_mode": mode, "proxy_profile_id": ""}
    if mode == "library":
        profile_id = str(value.get("proxy_profile_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", profile_id):
            raise UpdateNetworkError("select a proxy from the proxy library for software updates")
        result["proxy_profile_id"] = profile_id
    return result


def validate_update_settings(value: dict | None) -> dict:
    """Validate the complete update preference document saved from System Settings."""
    value = value or {}
    result = validate_network_settings(value)
    update_mode = value.get("update_mode")
    version_scope = value.get("version_scope")
    if update_mode is None:
        # Migrate the previous independent controls into one mutually-exclusive strategy.
        legacy = value.get("auto_update")
        if legacy is not None and not isinstance(legacy, bool):
            raise UpdateNetworkError("automatic update setting must be boolean")
        update_mode = "automatic" if legacy is not False else "notify"
        if version_scope is None:
            version_scope = (value.get("notification_mode") or "all") \
                if update_mode == "notify" else "all" if legacy is True else "main"
    update_mode = str(update_mode).strip().lower()
    version_scope = str(version_scope or ("main" if update_mode == "automatic" else "all")) \
        .strip().lower()
    if version_scope == "feature":
        version_scope = "main"
    if update_mode not in {"automatic", "notify"}:
        raise UpdateNetworkError("update mode must be automatic or notify")
    if version_scope not in {"all", "main"}:
        raise UpdateNetworkError("update version scope must be all or main")
    result.update(update_mode=update_mode, version_scope=version_scope)
    return result


def _network_selection() -> dict:
    from . import config as cfg
    settings = cfg.get_settings()
    selection = validate_network_settings(settings.get("updates"))
    if selection["proxy_mode"] == "library":
        profiles = (settings.get("proxy") or {}).get("profiles") or {}
        if selection["proxy_profile_id"] not in profiles:
            raise UpdateNetworkError("selected update proxy is no longer in the proxy library")
    return selection


def _network_candidates() -> list[dict]:
    selection = _network_selection()
    if selection["proxy_mode"] != "auto":
        return [selection]
    from . import config as cfg
    profiles = ((cfg.get_settings().get("proxy") or {}).get("profiles") or {})
    return [{"proxy_mode": "direct", "proxy_profile_id": ""}] + [
        {"proxy_mode": "library", "proxy_profile_id": str(profile_id)}
        for profile_id in profiles
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(profile_id))
    ]


def _session(selection: dict) -> requests.Session:
    proxy = _proxy_url(selection)
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _socks5_profile_url(profile: dict) -> str:
    host = str(profile.get("server") or "").strip()
    try:
        port = int(profile.get("port") or 1080)
    except (TypeError, ValueError):
        port = 0
    if not host or not 1 <= port <= 65535 or any(ch in host for ch in "\r\n/@"):
        raise UpdateNetworkError("selected SOCKS5 proxy is invalid")
    username = str(profile.get("username") or "")
    password = str(profile.get("password") or "")
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" \
        if username or password else ""
    return f"socks5h://{auth}{host}:{port}"


def _proxy_url(selection: dict) -> str:
    mode = selection["proxy_mode"]
    if mode == "direct":
        return ""
    from . import config as cfg, egress
    settings = cfg.get_settings()
    profile_id = selection["proxy_profile_id"]
    profile = ((settings.get("proxy") or {}).get("profiles") or {}).get(profile_id) or {}
    if profile.get("type") == "socks5":
        return _socks5_profile_url(profile)
    exits = (settings.get("proxy") or {}).get("exits") or {}
    live = egress.status().get("exits") or {}
    candidates = [live.get(country) or {} for country, exit_cfg in exits.items()
                  if isinstance(exit_cfg, dict) and exit_cfg.get("enabled")
                  and exit_cfg.get("profile_id") == profile_id]
    state = next((item for item in candidates if item.get("ready")), {})
    try:
        port = int(state.get("proxy_port") or 0)
    except (TypeError, ValueError):
        port = 0
    host = str(state.get("proxy_host") or "").strip()
    if not state.get("ready") or not host or not 1 <= port <= 65535:
        raise UpdateNetworkError("selected proxy library entry has no ready country exit")
    return f"socks5h://{host}:{port}"


def repository() -> str:
    return os.environ.get("MDD_UPDATE_REPOSITORY", DEFAULT_REPOSITORY).strip()


def _github_headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"mdd-sim-gateway/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _version_tuple(value: str) -> tuple[int, ...]:
    core = str(value).strip().removeprefix("v").split("-", 1)[0]
    try:
        return tuple(int(part) for part in core.split("."))
    except ValueError:
        return (0,)


def _stargazers(session, headers: dict, repository_name: str) -> int | None:
    """Star count for the console's repository link, or None if it cannot be read.

    Deliberately folded into the release check rather than served from the status endpoint:
    that endpoint answers every page load and must not wait on GitHub. Failure is silent —
    a decorative count must never turn a working update check into a visible error.
    """
    global _stars_cache, _stars_checked_at
    try:
        response = session.get(f"https://api.github.com/repos/{repository_name}",
                               headers=headers, timeout=8)
        response.raise_for_status()
        count = int(response.json().get("stargazers_count"))
    except (requests.RequestException, OSError, ValueError, TypeError):
        return _stars_cache
    if count >= 0:
        _stars_cache = count
        _stars_checked_at = time.time()
    return _stars_cache


def repository_stars(force: bool = False) -> dict:
    """Read repository stars independently of the six-hour release poll.

    The UI can retry this inexpensive metadata lookup after a transient outage without also
    fetching release metadata. The last good value remains available during later failures.
    """
    now = time.time()
    if (not force and _stars_cache is not None
            and now - _stars_checked_at < _STARS_CACHE_SECONDS):
        return {"ok": True, "stars": _stars_cache,
                "checked_at": int(_stars_checked_at), "cached": True}
    try:
        candidates = _network_candidates()
    except UpdateNetworkError:
        candidates = []
    for selection in candidates:
        try:
            value = _stargazers(_session(selection), _github_headers(), repository())
            if value is not None and _stars_checked_at >= now:
                return {"ok": True, "stars": value,
                        "checked_at": int(_stars_checked_at), "cached": False}
        except (requests.RequestException, UpdateNetworkError, OSError, ValueError, TypeError):
            continue
    return {"ok": False, "stars": _stars_cache,
            "checked_at": int(_stars_checked_at or 0), "cached": _stars_cache is not None}


def _release_result(payload: dict, selection: dict, repository_name: str,
                    *, session=None, headers: dict | None = None, include_stars: bool = False) -> dict:
    """Normalize one GitHub Release response for manual or automatic installation."""
    latest = str(payload.get("tag_name") or "").removeprefix("v")
    assets = {}
    for asset in payload.get("assets") or []:
        name = str((asset or {}).get("name") or "")
        try:
            size = int((asset or {}).get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if name and 0 < size < 20 * 1024 * 1024 * 1024:
            assets[name] = size
    result = {
        "ok": bool(latest),
        "current": VERSION,
        "repository": repository_name,
        "latest": latest,
        "update_available": _version_tuple(latest) > _version_tuple(VERSION),
        "release_url": str(payload.get("html_url") or ""),
        "published_at": str(payload.get("published_at") or ""),
        "notes": str(payload.get("body") or "")[:_MAX_RELEASE_NOTES_CHARS],
        "network": selection,
        "asset_sizes": assets,
        "checked_at": int(time.time()),
    }
    if include_stars and session is not None and headers is not None:
        result["stars"] = _stargazers(session, headers, repository_name)
    return result


def _release_candidates(preferred: dict | None = None) -> list[dict]:
    candidates = _network_candidates()
    if preferred:
        candidates = [preferred] + [item for item in candidates if item != preferred]
    return candidates


def check_release(version: str, preferred_network: dict | None = None) -> dict:
    """Fetch one configured stable Release, even when a newer patch is GitHub's latest.

    Main-only devices need this tagged lookup: once a patch is newest, ``releases/latest`` can
    no longer describe the older main release they are explicitly meant to converge on.
    """
    target = str(version or "").strip().removeprefix("v")
    repository_name = repository()
    result = {"ok": False, "current": VERSION, "repository": repository_name,
              "latest": target, "update_available": False, "checked_at": int(time.time())}
    if not re.fullmatch(r"\d+\.\d+\.\d+", target):
        result.update(error="Configured update target is invalid",
                      error_code="update.error.invalid_policy")
        return result
    url = (f"https://api.github.com/repos/{repository_name}/releases/tags/"
           f"{quote('v' + target, safe='')}")
    headers = _github_headers()
    last_error: Exception | None = None
    try:
        candidates = _release_candidates(preferred_network)
    except UpdateNetworkError as exc:
        candidates, last_error = [], exc
    for selection in candidates:
        try:
            session = _session(selection)
            response = session.get(url, headers=headers, timeout=12)
            response.raise_for_status()
            payload = response.json()
            if payload.get("draft") or payload.get("prerelease"):
                raise ValueError("configured update target is not a stable Release")
            candidate = _release_result(payload, selection, repository_name)
            if candidate.get("latest") != target:
                raise ValueError("configured Release tag does not match its payload")
            return candidate
        except requests.HTTPError as exc:
            last_error = exc
            code = exc.response.status_code if exc.response is not None else 0
            if code in {401, 404}:
                break
        except (requests.RequestException, UpdateNetworkError, OSError, ValueError, TypeError) as exc:
            last_error = exc
    if isinstance(last_error, requests.HTTPError):
        code = last_error.response.status_code if last_error.response is not None else 0
        result.update(
            error=("Configured update Release is unavailable" if code in {401, 404}
                   else "GitHub update check was rate-limited" if code == 403
                   else f"GitHub returned HTTP {code}"),
            error_code=("update.error.no_release" if code in {401, 404}
                        else "update.error.rate_limited" if code == 403
                        else "update.error.github"),
        )
    elif isinstance(last_error, UpdateNetworkError):
        result.update(error=str(last_error), error_code="update.error.proxy")
    elif last_error is not None:
        result.update(error=f"Update service unavailable: {type(last_error).__name__}",
                      error_code="update.error.unavailable")
    return result


def check(force: bool = False) -> dict:
    global _cache
    now = time.time()
    if not force and _cache and now - _cache[0] < 300:
        return dict(_cache[1])
    repository_name = repository()
    url = f"https://api.github.com/repos/{repository_name}/releases/latest"
    headers = _github_headers()
    result = {"ok": False, "current": VERSION, "repository": repository_name,
              "update_available": False, "checked_at": int(now)}
    last_error: Exception | None = None
    try:
        candidates = _network_candidates()
    except UpdateNetworkError as exc:
        candidates, last_error = [], exc
    for selection in candidates:
        try:
            session = _session(selection)
            response = session.get(url, headers=headers, timeout=12)
            response.raise_for_status()
            payload = response.json()
            result.update(_release_result(payload, selection, repository_name,
                                          session=session, headers=headers,
                                          include_stars=True))
            last_error = None
            break
        except requests.HTTPError as exc:
            last_error = exc
            code = exc.response.status_code if exc.response is not None else 0
            if code in {401, 404}:
                break
        except (requests.RequestException, UpdateNetworkError, OSError, ValueError, TypeError) as exc:
            last_error = exc
    if isinstance(last_error, requests.HTTPError):
        exc = last_error
        code = exc.response.status_code if exc.response is not None else 0
        if code in {401, 404}:
            # Release checks are intentionally unauthenticated and never send a GitHub token.
            result["error"] = "No release is available from the configured repository"
            result["error_code"] = "update.error.no_release"
        elif code == 403:
            result["error"] = "GitHub update check was rate-limited"
            result["error_code"] = "update.error.rate_limited"
        else:
            result["error"] = f"GitHub returned HTTP {code}"
            result["error_code"] = "update.error.github"
    elif isinstance(last_error, UpdateNetworkError):
        result["error"] = str(last_error)
        result["error_code"] = "update.error.proxy"
    elif last_error is not None:
        result["error"] = f"Update service unavailable: {type(last_error).__name__}"
        result["error_code"] = "update.error.unavailable"
    _cache = (now, result)
    return dict(result)


def _policy_url() -> str:
    override = os.environ.get("MDD_UPDATE_POLICY_URL", "").strip()
    return override or f"https://raw.githubusercontent.com/{repository()}/main/update-policy.json"


def _policy_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _load_update_policy(info: dict) -> dict | None:
    selection = info.get("network") or _network_selection()
    try:
        response = _session(selection).get(_policy_url(), headers=_github_headers(), timeout=12)
        response.raise_for_status()
        policy = response.json()
    except (requests.RequestException, UpdateNetworkError, OSError, ValueError, TypeError):
        return None
    return policy if isinstance(policy, dict) else None


def _valid_policy(policy: dict | None) -> bool:
    try:
        return isinstance(policy, dict) and int(policy.get("schema") or 0) == 1
    except (TypeError, ValueError):
        return False


def _channel_entry(policy: dict, scope: str) -> dict | None:
    channels = policy.get("channels")
    if not isinstance(channels, dict):
        return None
    entry = channels.get(scope)
    return entry if isinstance(entry, dict) else {}


def _policy_target(policy: dict | None, scope: str, latest: str) -> str:
    """Version one preference cohort should converge on under this policy.

    ``all`` remains tied to GitHub's latest Release so a stale promotion cannot silently turn
    into a downgrade channel. ``main`` may name an older tagged Release explicitly; that is the
    capability the old latest-only design lacked once a patch had been published after it.
    """
    if not _valid_policy(policy) or scope not in {"all", "main"}:
        return ""
    entry = _channel_entry(policy, scope)
    if entry is not None:
        version = str(entry.get("version") or "").strip().removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            return ""
        return version if scope == "main" or version == latest else ""
    # Backward-compatible policy: one promotion applied to the latest Release, and the release
    # classification decided whether main-only devices considered it.
    if scope == "all":
        return latest
    release = policy.get("release") or {}
    if (isinstance(release, dict)
            and str(release.get("version") or "").removeprefix("v") == latest
            and str(release.get("kind") or "").strip().lower() == "main"):
        return latest
    return ""


def auto_update_authorization(info: dict, now: datetime | None = None, *,
                              scope: str = "all", policy: dict | None = None) -> dict:
    """Authorize one cohort's configured Release after its rollout time.

    A GitHub Release alone never authorizes unattended installation. The repository owner
    promotes the ``all`` and ``main`` targets independently, optionally with different future
    ``not_before`` times. Policies without channels retain the v1.5.0-v1.5.3 single-target
    behavior so already-installed gateways can bootstrap through v1.5.4.
    """
    latest = str(info.get("latest") or "")
    result = {"authorized": False, "version": latest, "scope": scope,
              "reason": "not_promoted"}
    if not info.get("update_available") or not latest:
        result["reason"] = "not_available"
        return result
    if policy is None:
        policy = _load_update_policy(info)
    if policy is None:
        result["reason"] = "policy_unavailable"
        return result
    if not _valid_policy(policy) or scope not in {"all", "main"}:
        result["reason"] = "invalid_policy"
        return result
    target = _policy_target(policy, scope, latest)
    result["target_version"] = target
    if target != latest:
        return result
    promoted = _channel_entry(policy, scope)
    if promoted is None:
        promoted = policy.get("auto_update") or {}
    classified = policy.get("release") or {}
    if not isinstance(promoted, dict) or not isinstance(classified, dict):
        result["reason"] = "invalid_policy"
        return result
    if str(classified.get("version") or "").removeprefix("v") == latest:
        release_kind = str(classified.get("kind") or "").strip().lower()
        if release_kind in {"main", "patch"}:
            result["release_kind"] = release_kind
    if scope == "main" and _channel_entry(policy, scope) is not None:
        result["release_kind"] = "main"
    if str(promoted.get("version") or "").removeprefix("v") != latest:
        return result
    not_before_text = str(promoted.get("not_before") or "")
    not_before = _policy_time(not_before_text)
    if not not_before:
        result["reason"] = "invalid_policy"
        return result
    current_time = now or datetime.now(timezone.utc)
    result.update(not_before=not_before.isoformat().replace("+00:00", "Z"))
    if current_time.astimezone(timezone.utc) < not_before:
        result["reason"] = "waiting"
        return result
    result.update(authorized=True, reason="promoted")
    return result


def _automation_state_path() -> str:
    from . import config as cfg
    return os.path.join(cfg.DATA_DIR, "update", _AUTOMATION_STATE_FILE)


def _read_automation_state() -> dict:
    try:
        with open(_automation_state_path(), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_automation_state(value: dict) -> None:
    _write_private_json(_automation_state_path(), value)


def automation_cycle() -> dict:
    """Run one background release check, notification and gated auto-update decision."""
    from . import config as cfg, notify_push

    latest_info = check(True)
    result = {"checked": True, "release": latest_info, "notified": False,
              "auto_update_requested": False}
    if not latest_info.get("ok"):
        return result
    settings = cfg.get_settings()
    updates = validate_update_settings(settings.get("updates"))
    state = _read_automation_state()
    scope = updates["version_scope"]
    policy = None
    if updates["update_mode"] == "automatic" or updates["version_scope"] == "main":
        policy = _load_update_policy(latest_info)
    latest = str(latest_info.get("latest") or "")
    target_version = (latest if scope == "all"
                      else _policy_target(policy, "main", latest))
    if not target_version:
        if updates["update_mode"] == "automatic":
            result["authorization"] = {
                "authorized": False, "scope": scope,
                "reason": "policy_unavailable" if policy is None else "not_promoted",
            }
        return result
    info = (latest_info if target_version == latest
            else check_release(target_version, latest_info.get("network")))
    result["release"] = info
    if info is not latest_info:
        result["latest_release"] = latest_info
    if not info.get("update_available"):
        return result
    target = str(info.get("latest") or "")
    should_notify = updates["update_mode"] == "notify"
    if (should_notify and state.get("notified_version") != target
            and notify_push.has_enabled_channel(settings, notify_push.EV_SOFTWARE_UPDATE)):
        text = f"v{VERSION} → v{target}\n{info.get('release_url') or ''}".strip()
        notify_push.dispatch(settings, notify_push.EV_SOFTWARE_UPDATE, {}, target, text)
        state["notified_version"] = target
        state["notified_at"] = int(time.time())
        _save_automation_state(state)
        result["notified"] = True
    should_auto_update = updates["update_mode"] == "automatic"
    if not should_auto_update or state.get("auto_requested_version") == target:
        return result
    authorization = auto_update_authorization(info, scope=scope, policy=policy)
    result["authorization"] = authorization
    if not authorization.get("authorized"):
        return result
    applied = request_apply(info=info)
    result["apply"] = applied
    if applied.get("ok"):
        state["auto_requested_version"] = target
        state["auto_requested_at"] = int(time.time())
        _save_automation_state(state)
        result["auto_update_requested"] = True
    return result


def _apply_paths() -> tuple[str, str]:
    from . import config as cfg
    root = os.path.join(cfg.DATA_DIR, "orchestrator")
    return os.path.join(root, "update-request.json"), os.path.join(root, "update-status.json")


def _write_private_json(path: str, value: dict):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def apply_status() -> dict:
    """Current self-update progress as published by the host-side updater."""
    request_path, status_path = _apply_paths()
    try:
        with open(status_path, encoding="utf-8") as handle:
            status = json.load(handle)
        if not isinstance(status, dict):
            status = {}
    except (OSError, ValueError):
        status = {}
    status.setdefault("state", "idle")
    if status.get("state") == "running":
        # A progress document only proves an update is alive while something keeps refreshing
        # it. An updater that died with its host — reboot, power cut, `systemctl stop` — leaves
        # this document saying "running" with nobody left to advance it, and the WebUI used to
        # resume into that dead progress view on every visit, forever.
        idle = time.time() - int(status.get("updated_at") or 0)
        status["stale"] = idle > _APPLY_STALE_SECONDS
        if idle > _APPLY_ABANDONED_SECONDS:
            status["state"] = "stalled"
            status["error_code"] = "update.error.abandoned"
    try:
        with open(request_path, encoding="utf-8") as handle:
            requested_at = int((json.load(handle) or {}).get("requested_at") or 0)
        status["requested"] = True
        # An unconsumed request means the orchestrator is not picking work up (stopped or
        # never installed) — surface that instead of letting the UI spin forever.
        if time.time() - requested_at > 120:
            status["state"] = "stalled"
            status["error_code"] = "update.error.not_picked_up"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return status


def cancel_apply() -> dict:
    """Discard a progress document the user has been left staring at.

    This cannot stop a live updater — it runs detached as root — so it refuses while the run
    still looks alive rather than blinding the UI to an update that is genuinely mid-flight.
    The host orchestrator fails abandoned runs on its own; this is the manual escape hatch for
    a host whose orchestrator is down too.
    """
    request_path, status_path = _apply_paths()
    status = apply_status()
    if status.get("state") == "running" and not status.get("stale"):
        return {"ok": False, "error": "An update is already in progress",
                "error_code": "update.error.in_progress", "status": status}
    for path in (request_path, status_path):
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"ok": True}


def request_apply(info: dict | None = None) -> dict:
    """Publish a one-click update request for the host orchestrator."""
    status = apply_status()
    if status.get("state") == "running" and not status.get("stale"):
        return {"ok": False, "error": "An update is already in progress",
                "error_code": "update.error.in_progress", "status": status}
    info = dict(info) if info is not None else check(True)
    if not info.get("update_available"):
        return {"ok": False, "error": info.get("error") or "No update is available",
                "error_code": info.get("error_code") or "update.error.not_available"}
    request_path, status_path = _apply_paths()
    now = int(time.time())
    network = info.get("network") or _network_selection()
    configured = _network_selection()
    if configured["proxy_mode"] == "auto":
        candidates = _network_candidates()
        networks = [network] + [item for item in candidates if item != network]
    else:
        networks = [network]
    # Reset the visible status first so a stale success/failure from a previous run cannot be
    # mistaken for this run's outcome while the orchestrator picks the request up.
    _write_private_json(status_path, {"state": "running", "phase": "requested",
                                      "target": info["latest"], "updated_at": now})
    _write_private_json(request_path, {"version": info["latest"], "repository": repository(),
                                       "requested_at": now,
                                       "network": network,
                                       "networks": networks,
                                       "asset_sizes": info.get("asset_sizes") or {}})
    return {"ok": True, "version": info["latest"]}

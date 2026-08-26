#!/usr/bin/env python3
"""Detached self-update runner for MDD Sim Gateway.

The WebUI publishes an update request; the host orchestrator stages a COPY of this script
under ``<data>/update/`` and launches it as a transient systemd unit (``systemd-run``).
Both indirections are required for the update to survive itself:

  - ``install.sh reload`` restarts the control plane AND the orchestrator, so an updater
    running inside either service would be killed halfway through;
  - the repository checkout this file ships in is overwritten while the updater runs, so it
    must execute from a copy outside the checkout.

Stdlib only (it runs before any requirements are reinstalled). Progress is published to
``<data>/orchestrator/update-status.json`` for the WebUI to poll.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from pathlib import Path

# Top-level entries that belong to the installation, not to a release: never replaced and
# never deleted (the default MDD_DATA_DIR lives at <repo>/data).
PRESERVE = {"data", ".env", ".git"}
# Locally-built artifacts nested inside release-managed directories. webui/dist is kept so
# the old UI keeps being served if the reload's WebUI rebuild fails; on success the rebuild
# replaces it wholesale anyway.
NESTED_PRESERVE = {"control": {".venv"}, "webui": {"node_modules", "dist"}}
BACKUP_EXCLUDE = {"data", ".git", ".venv", "node_modules", "__pycache__"}

VERSION_RE = re.compile(r"\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ENGINE_REGISTRY_IMAGE = "ghcr.io/mddidd/mdd-sim-gateway-engine"
ENGINE_HANDOFF_MANIFEST = Path("engine/release-image.SHA256SUMS")


class UpdateError(RuntimeError):
    pass


def host_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    raise UpdateError(f"unsupported CPU architecture: {machine or 'unknown'}")


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_network_config(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateError("could not read update network configuration") from exc
    return value if isinstance(value, dict) else {}


class Status:
    def __init__(self, path: Path, target: str):
        self.path, self.target = path, target
        self.started = int(time.time())
        self.phase = "requested"
        self.extra: dict = {}

    def publish(self, state: str, phase: str, **fields):
        self.phase = phase
        self.extra.update(fields)
        atomic_json(self.path, {"state": state, "phase": phase, "target": self.target,
                                "started_at": self.started, "updated_at": int(time.time()),
                                **self.extra})


def network_environment(proxy_url: str) -> dict[str, str]:
    """Return a clean download environment, optionally carrying a validated proxy."""
    env = dict(os.environ)
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "ALL_PROXY"):
        env.pop(name, None)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    if not proxy_url:
        return env
    parsed = urlsplit(proxy_url)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} \
            or not parsed.hostname or any(ch in proxy_url for ch in "\r\n"):
        raise UpdateError("invalid update proxy configuration")
    env.update({"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "ALL_PROXY": proxy_url,
                "http_proxy": proxy_url, "https_proxy": proxy_url, "all_proxy": proxy_url})
    return env


def _redact_proxy_error(message: str, proxy_url: str) -> str:
    redacted = str(message or "")
    if proxy_url:
        redacted = redacted.replace(proxy_url, "[update proxy]")
        parsed = urlsplit(proxy_url)
        if parsed.password:
            redacted = redacted.replace(parsed.password, "***")
    return redacted


def redact_log(path: Path, proxy_url: str):
    if not proxy_url or not path.is_file():
        return
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
        redacted = _redact_proxy_error(original, proxy_url)
        if redacted != original:
            path.write_text(redacted, encoding="utf-8")
            os.chmod(path, 0o600)
    except OSError:
        pass


def download(url: str, destination: Path, env: dict[str, str], proxy_url: str = "",
             *, status: Status | None = None, artifact: str = "release package",
             total_bytes: int = 0, route: str = "direct", route_name: str = "",
             phase: str = "downloading", route_attempt: int = 1, route_total: int = 1,
             allow_fallback: bool = False):
    """Download through curl while publishing byte, speed and heartbeat details."""
    started = time.monotonic()
    # The compressed Engine is the largest Release asset. Keep a finite ceiling, but do not
    # apply the ten-minute source-package budget to a transfer that can legitimately be several
    # hundred megabytes on a Raspberry Pi connection.
    max_time = "1800" if phase == "engine_image" else "600"
    command = [
        "curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
        "--tlsv1.2", "--retry", "0" if allow_fallback else "3", "--retry-all-errors",
        "--connect-timeout", "20", "--max-time", max_time, "--silent", "--show-error",
        "--user-agent", "mdd-sim-gateway-updater", "--output", str(destination), url,
    ]
    # A route that cannot sustain a useful asset-transfer rate should yield to the next Auto
    # candidate. The final/only route keeps the generous timeout for genuinely slow links.
    if allow_fallback:
        command[1:1] = ["--speed-limit", "65536", "--speed-time", "45"]
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
    rate, sampled_at, sampled_bytes = 0.0, started, 0
    while process.poll() is None:
        now = time.monotonic()
        elapsed = max(0, int(now - started))
        try:
            downloaded = destination.stat().st_size
        except OSError:
            downloaded = 0
        # Rate over the recent window, not since the start. A route that spends its first
        # minute inside curl's connect retries would otherwise report a permanently depressed
        # speed, and an estimate the transfer never catches up to is worse than none. Smoothed
        # so the reading does not jump between polls.
        window = now - sampled_at
        if window >= 1:
            sample = max(0, downloaded - sampled_bytes) / window
            rate = sample if rate <= 0 else rate * 0.6 + sample * 0.4
            sampled_at, sampled_bytes = now, downloaded
        if status:
            status.publish("running", phase, url=url, artifact=artifact,
                           downloaded_bytes=downloaded, total_bytes=max(0, int(total_bytes or 0)),
                           bytes_per_second=int(rate),
                           elapsed_seconds=elapsed, route=route, route_name=route_name,
                           route_attempt=route_attempt, route_total=route_total)
        time.sleep(2)
    _, stderr = process.communicate()
    if process.returncode:
        detail = _redact_proxy_error(stderr, proxy_url).strip().splitlines()
        tail = detail[-1] if detail else f"curl exited with {process.returncode}"
        raise UpdateError(f"release download failed: {tail}")


def validated_download_routes(proxy_url: str = "", *, route: str = "direct",
                              route_name: str = "", routes: list[dict] | None = None
                              ) -> list[dict]:
    """Validate and redact the route list before any release file is created."""
    candidates = routes if isinstance(routes, list) and routes else [
        {"proxy_url": proxy_url, "route": route, "route_name": route_name}]
    clean = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        candidate_url = str(item.get("proxy_url") or "")
        network_environment(candidate_url)
        clean.append({"proxy_url": candidate_url,
                      "route": "library" if candidate_url else "direct",
                      "route_name": str(item.get("route_name") or "")[:120]})
    if not clean:
        raise UpdateError("no usable update download route")
    return clean


def fetch_release_asset(url: str, destination: Path, artifact: str,
                        clean_routes: list[dict], active_route: int = 0, *,
                        asset_sizes: dict | None = None, status: Status | None = None,
                        phase: str = "downloading") -> int:
    """Download one Release asset and return the successful route index."""
    sizes = asset_sizes or {}
    order = [active_route] + [i for i in range(len(clean_routes)) if i != active_route]
    failures = []
    for attempt, index in enumerate(order, 1):
        candidate = clean_routes[index]
        try:
            destination.unlink(missing_ok=True)
            download(url, destination,
                     network_environment(candidate["proxy_url"]), candidate["proxy_url"],
                     status=status, artifact=artifact,
                     total_bytes=sizes.get(artifact, 0),
                     route=candidate["route"], route_name=candidate["route_name"],
                     phase=phase, route_attempt=attempt, route_total=len(order),
                     allow_fallback=attempt < len(order))
        except UpdateError as exc:
            failures.append(str(exc))
            continue
        return index
    raise UpdateError("all update download routes failed: " + "; ".join(failures)[-1500:])


def _last_log_line(path: Path, proxy_url: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return _redact_proxy_error(next((line for line in reversed(lines) if line.strip()), ""),
                               proxy_url)[-300:]


def reload_services(command: list[str], cwd: Path, env: dict[str, str], log_path: Path,
                    status: Status, proxy_url: str) -> int:
    """Run the disruptive reload while keeping the progress document alive and useful."""
    started = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=log,
                                   stderr=subprocess.STDOUT)
        while process.poll() is None:
            status.publish("running", "reloading",
                           elapsed_seconds=max(0, int(time.monotonic() - started)),
                           detail=_last_log_line(log_path, proxy_url))
            time.sleep(3)
    return int(process.returncode or 0)


def verify_release_archive(archive: Path, sums: Path):
    verify_release_file(archive, sums, "update archive")


def verify_release_file(artifact: Path, sums: Path, description: str):
    expected = ""
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lstrip("*") == artifact.name \
                and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            expected = parts[0].lower()
            break
    if not expected:
        raise UpdateError(f"release checksum file does not name the {description}")
    digest = hashlib.sha256()
    with open(artifact, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise UpdateError(f"release {description} checksum mismatch")


def installed_mode(data: Path) -> str:
    try:
        mode = (data / "install-mode").read_text(encoding="utf-8").strip().lower()
    except OSError:
        mode = ""
    return mode if mode in {"local", "docker"} else "local"


def load_control_image(artifact: Path, version: str):
    """Load a verified image archive without changing or restarting the Docker daemon."""
    image = "mdd-sim-gateway/control"
    previous = f"{image}:previous"
    inspect = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    had_previous = inspect.returncode == 0 and bool(inspect.stdout.strip())
    if had_previous:
        tagged = subprocess.run(["docker", "tag", image, previous],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if tagged.returncode:
            raise UpdateError(f"could not preserve the current control image: {tagged.stderr.strip()}")
    loaded = subprocess.run(["docker", "load", "--input", str(artifact)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if loaded.returncode:
        raise UpdateError(f"could not load Release control image: {loaded.stderr.strip()}")
    expected_arch = host_arch()
    checked = subprocess.run(
        ["docker", "image", "inspect", image, "--format",
         '{{.Architecture}}|{{index .Config.Labels "org.opencontainers.image.version"}}'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    actual = checked.stdout.strip() if checked.returncode == 0 else ""
    if actual != f"{expected_arch}|{version}":
        if had_previous:
            subprocess.run(["docker", "tag", previous, image], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        raise UpdateError(f"Release control image identity mismatch: {actual or 'unreadable'}")


def prune_dangling_images() -> bool:
    """Reclaim images orphaned by a successful update, without touching rollback tags.

    Docker refuses to prune images used by containers, and the installer's explicit
    ``:previous`` Engine/control tags are not dangling. This therefore removes superseded
    untagged images and obsolete build stages while preserving both the live images and the
    intentional one-version rollback point. Cleanup is best-effort: a completed service reload
    must not be reported as failed only because Docker could not reclaim optional cache space.
    """
    result = subprocess.run(
        ["docker", "image", "prune", "--force"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def release_engine_fingerprints(source_root: Path) -> tuple[str, str] | None:
    """Return the verified release's Engine inputs, or None for a transitional old release."""
    script = source_root / "tools" / "engine-fingerprint.sh"
    if not script.is_file():
        return None
    values = []
    for kind in ("runtime", "base"):
        result = subprocess.run(
            ["sh", str(script), kind], cwd=str(source_root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        value = result.stdout.strip() if result.returncode == 0 else ""
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise UpdateError(f"could not calculate release Engine {kind} fingerprint")
        values.append(value)
    return values[0], values[1]


def engine_image_matches_inputs(image: str, runtime_fp: str, base_fp: str) -> bool:
    checked = subprocess.run(
        ["docker", "image", "inspect", image, "--format",
         '{{index .Config.Labels "io.mdd-sim-gateway.runtime-fp"}}|'
         '{{index .Config.Labels "io.mdd-sim-gateway.base-fp"}}'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return checked.returncode == 0 and checked.stdout.strip() == f"{runtime_fp}|{base_fp}"


def load_release_engine(artifact: Path, version: str, runtime_fp: str, base_fp: str,
                        status: Status | None, log_path: Path) -> str:
    """Import and identify the verified ARM64 Engine without replacing the running image yet."""
    image = f"{ENGINE_REGISTRY_IMAGE}:v{version}"
    started = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(["docker", "load", "--input", str(artifact)], stdout=log,
                                   stderr=subprocess.STDOUT, text=True)
        while process.poll() is None:
            if status:
                status.publish("running", "engine_image", artifact=artifact.name,
                               engine_image_required=True,
                               elapsed_seconds=max(0, int(time.monotonic() - started)),
                               detail="importing verified ARM64 Engine image")
            time.sleep(3)
    if process.returncode:
        raise UpdateError(
            f"could not load Release Engine image: {_last_log_line(log_path, '') or 'docker load failed'}")
    checked = subprocess.run(
        ["docker", "image", "inspect", image, "--format",
         '{{.Architecture}}|{{index .Config.Labels "org.opencontainers.image.version"}}|'
         '{{index .Config.Labels "io.mdd-sim-gateway.runtime-fp"}}|'
         '{{index .Config.Labels "io.mdd-sim-gateway.base-fp"}}'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    expected = f"arm64|{version}|{runtime_fp}|{base_fp}"
    actual = checked.stdout.strip() if checked.returncode == 0 else ""
    if actual != expected:
        raise UpdateError(f"Release Engine image identity mismatch: {actual or 'unreadable'}")
    return image


def extract(archive: Path, destination: Path) -> Path:
    """Unpack the GitHub source tarball and return its single top-level directory."""
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(destination, filter="data")
        except TypeError:  # Python without the extraction-filter backport
            base = destination.resolve()
            for member in tar.getmembers():
                target = (destination / member.name).resolve()
                if base != target and base not in target.parents:
                    raise UpdateError(f"unsafe path in release archive: {member.name}")
                if member.islnk() or member.issym():
                    raise UpdateError(f"link member in release archive: {member.name}")
            tar.extractall(destination)
    roots = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(roots) != 1 or not (roots[0] / "install.sh").is_file():
        raise UpdateError("release archive does not look like a gateway source tree")
    return roots[0]


def backup(repo: Path, data: Path, current: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = data / "backups" / f"pre-update-{current or 'unknown'}-{stamp}.tar.gz"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def keep(info: tarfile.TarInfo):
        return None if any(part in BACKUP_EXCLUDE for part in Path(info.name).parts) else info

    with tarfile.open(destination, "w:gz") as tar:
        tar.add(repo, arcname="mdd-sim-gateway", filter=keep)
    os.chmod(destination, 0o600)
    return destination


def apply_tree(source_root: Path, repo: Path):
    """Replace release-managed content in the checkout with the new release's files."""
    for entry in sorted(source_root.iterdir(), key=lambda item: item.name):
        if entry.name in PRESERVE:
            continue
        target = repo / entry.name
        if not entry.is_dir():
            if target.is_dir():
                shutil.rmtree(target)
            shutil.copy2(entry, target)
            continue
        preserved = NESTED_PRESERVE.get(entry.name) or set()
        if preserved and target.is_dir():
            for child in target.iterdir():
                if child.name in preserved:
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            for child in entry.iterdir():
                release_dist = child.name == "dist" and \
                    (child / ".mdd-release-version").is_file()
                if child.name in preserved and not release_dist:
                    continue
                child_target = target / child.name
                if child_target.is_dir():
                    shutil.rmtree(child_target)
                elif child_target.exists() or child_target.is_symlink():
                    child_target.unlink()
                if child.is_dir():
                    shutil.copytree(child, child_target, symlinks=True)
                else:
                    shutil.copy2(child, child_target)
        else:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            shutil.copytree(entry, target, symlinks=True)


def perform_engine_handoff(repo: Path, data: Path, version: str, repo_name: str,
                           network_path: Path | None = None) -> str:
    """Give pre-distribution updaters a one-release hook into the new Engine channel.

    v1.4.1 downloads and verifies the new source archive before it invokes that archive's
    ``install.sh reload --no-engines``. The generated manifest is therefore already covered by
    the old updater's archive checksum. While its root-only network file still exists, this
    helper reuses the same route list to fetch and import the Engine Release asset.
    """
    if not VERSION_RE.fullmatch(version):
        raise UpdateError(f"invalid target version: {version!r}")
    if not REPOSITORY_RE.fullmatch(repo_name):
        raise UpdateError(f"invalid repository: {repo_name!r}")
    manifest = repo / ENGINE_HANDOFF_MANIFEST
    if not manifest.is_file():
        raise UpdateError("release has no Engine handoff manifest")
    engine_name = f"mdd-sim-gateway-engine-v{version}-arm64.tar.gz"
    if not any(line.strip().split(None, 1)[-1].lstrip("*") == engine_name
               for line in manifest.read_text(encoding="utf-8").splitlines()
               if len(line.strip().split(None, 1)) == 2):
        raise UpdateError("Engine handoff manifest does not name this release")
    update_dir = data / "update"
    update_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(update_dir).free < 2 * 1024 * 1024 * 1024:
        raise UpdateError("not enough persistent disk space to import the Engine image")

    network = read_network_config(network_path) if network_path else {}
    fallback_proxy = str(network.get("proxy_url") or os.environ.get("HTTPS_PROXY")
                         or os.environ.get("https_proxy") or "")
    clean_routes = validated_download_routes(
        fallback_proxy,
        route=str(network.get("route") or ("library" if fallback_proxy else "direct")),
        route_name=str(network.get("route_name") or ""),
        routes=network.get("routes") if isinstance(network.get("routes"), list) else None)
    asset_sizes = network.get("asset_sizes") if isinstance(
        network.get("asset_sizes"), dict) else {}
    staging = Path(tempfile.mkdtemp(prefix="engine-handoff.", dir=str(update_dir)))
    try:
        artifact = staging / engine_name
        base = f"https://github.com/{repo_name}/releases/download/v{version}"
        fetch_release_asset(f"{base}/{engine_name}", artifact, engine_name, clean_routes,
                            asset_sizes=asset_sizes, phase="engine_image")
        verify_release_file(artifact, manifest, "ARM64 Engine image")
        fingerprints = release_engine_fingerprints(repo)
        if not fingerprints:
            raise UpdateError("release has no Engine identity metadata")
        return load_release_engine(
            artifact, version, *fingerprints, None, update_dir / "engine-image.log")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def perform(repo: Path, data: Path, version: str, repo_name: str, status: Status,
            proxy_url: str = "", *, route: str = "direct", route_name: str = "",
            asset_sizes: dict | None = None, routes: list[dict] | None = None):
    if not VERSION_RE.fullmatch(version):
        raise UpdateError(f"invalid target version: {version!r}")
    if not REPOSITORY_RE.fullmatch(repo_name):
        raise UpdateError(f"invalid repository: {repo_name!r}")
    (data / "update").mkdir(mode=0o700, parents=True, exist_ok=True)
    clean_routes = validated_download_routes(
        proxy_url, route=route, route_name=route_name, routes=routes)
    active_route = 0
    staging = Path(tempfile.mkdtemp(prefix="mdd-update.", dir=str(data / "update")))
    try:
        mode = installed_mode(data)
        asset_sizes = asset_sizes or {}
        archive_name = f"mdd-sim-gateway-v{version}.tar.gz"
        control_name = f"mdd-sim-gateway-control-v{version}-arm64.tar.gz"
        engine_name = f"mdd-sim-gateway-engine-v{version}-arm64.tar.gz"
        base = f"https://github.com/{repo_name}/releases/download/v{version}"
        url = f"{base}/{archive_name}"
        # Publish the transfer skeleton before curl starts so the dialog shows a sized bar from
        # the first poll instead of a bare file name until the first heartbeat lands.
        status.publish("running", "downloading", url=url, install_mode=mode,
                       route=clean_routes[active_route]["route"],
                       route_name=clean_routes[active_route]["route_name"], artifact=archive_name,
                       downloaded_bytes=0, total_bytes=int(asset_sizes.get(archive_name) or 0),
                       bytes_per_second=0, elapsed_seconds=0,
                       route_attempt=1, route_total=len(clean_routes))
        archive = staging / archive_name
        sums = staging / "SHA256SUMS"

        active_route = fetch_release_asset(
            url, archive, archive_name, clean_routes, active_route,
            asset_sizes=asset_sizes, status=status)
        active_route = fetch_release_asset(
            f"{base}/SHA256SUMS", sums, "SHA256SUMS", clean_routes, active_route,
            asset_sizes=asset_sizes, status=status)

        status.publish("running", "verifying", artifact=archive_name, downloaded_bytes=0,
                       total_bytes=0, bytes_per_second=0, elapsed_seconds=0, detail="")
        verify_release_archive(archive, sums)
        source_root = extract(archive, staging / "tree")
        version_file = source_root / "VERSION"
        packaged = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
        if packaged != version:
            raise UpdateError(f"release archive reports version {packaged!r}, expected {version!r}")
        release_dist = source_root / "webui" / "dist"
        dist_version = (release_dist / ".mdd-release-version").read_text(
            encoding="utf-8").strip() if release_dist.is_dir() else ""
        if dist_version != version or not (release_dist / "index.html").is_file():
            raise UpdateError("release archive has no matching prebuilt WebUI")

        distributed_engine = ""
        engine_fingerprints = release_engine_fingerprints(source_root)
        engine_refresh_required = bool(
            engine_fingerprints and not engine_image_matches_inputs(
                "mdd-sim-gateway/engine", *engine_fingerprints))
        release_images_supported = host_arch() == "arm64"
        if engine_refresh_required and release_images_supported:
            if shutil.disk_usage(data / "update").free < 2 * 1024 * 1024 * 1024:
                raise UpdateError("not enough persistent disk space to import the Engine image")
            status.publish("running", "engine_image", artifact=engine_name,
                           engine_image_required=True, detail="",
                           downloaded_bytes=0,
                           total_bytes=int(asset_sizes.get(engine_name) or 0),
                           bytes_per_second=0, elapsed_seconds=0)
            engine_archive = staging / engine_name
            active_route = fetch_release_asset(
                f"{base}/{engine_name}", engine_archive, engine_name,
                clean_routes, active_route, asset_sizes=asset_sizes, status=status,
                phase="engine_image")
            status.publish("running", "engine_image", artifact=engine_name,
                           engine_image_required=True, detail="verifying ARM64 Engine image",
                           downloaded_bytes=0, total_bytes=0,
                           bytes_per_second=0, elapsed_seconds=0)
            verify_release_file(engine_archive, sums, "ARM64 Engine image")
            distributed_engine = load_release_engine(
                engine_archive, version, *engine_fingerprints, status,
                data / "update" / "engine-image.log")

        if mode == "docker" and release_images_supported:
            if shutil.disk_usage(data / "update").free < 1024 * 1024 * 1024:
                raise UpdateError("not enough persistent disk space to import the control image")
            status.publish("running", "control_image", artifact=control_name, detail="",
                           downloaded_bytes=0,
                           total_bytes=int(asset_sizes.get(control_name) or 0),
                           bytes_per_second=0, elapsed_seconds=0)
            control_archive = staging / control_name
            active_route = fetch_release_asset(
                f"{base}/{control_name}", control_archive, control_name,
                clean_routes, active_route, asset_sizes=asset_sizes, status=status,
                phase="control_image")
            status.publish("running", "control_image", artifact=control_name,
                           downloaded_bytes=0, total_bytes=0, bytes_per_second=0,
                           elapsed_seconds=0, detail="")
            verify_release_file(control_archive, sums, "ARM64 control image")
            load_control_image(control_archive, version)

        status.publish("running", "backup")
        try:
            current = (repo / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        saved = backup(repo, data, current)

        status.publish("running", "applying", backup=str(saved))
        apply_tree(source_root, repo)

        # Reload rebuilds the WebUI + venv (or the control image in docker mode) and restarts
        # the control plane and orchestrator — this unit outlives both restarts.
        status.publish("running", "reloading")
        log_path = data / "update" / "reload.log"
        selected_proxy_url = clean_routes[active_route]["proxy_url"]
        env = network_environment(selected_proxy_url)
        env["MDD_REUSE_WEBUI"] = "1"
        if mode == "docker" and release_images_supported:
            env["MDD_REUSE_CONTROL_IMAGE"] = "1"
        if distributed_engine:
            env["MDD_ENGINE_DISTRIBUTION_IMAGE"] = distributed_engine
        reload_command = ["sh", str(repo / "install.sh"), "reload"]
        # Release images are ARM64-only. On amd64, leave Engine preservation disabled when
        # its inputs changed so install.sh refreshes or builds the native image locally;
        # Docker-mode control is likewise rebuilt locally instead of loading an ARM64 asset.
        if not distributed_engine and not engine_refresh_required:
            reload_command.append("--no-engines")
        result_code = reload_services(
            reload_command, repo, env,
            log_path, status, selected_proxy_url)
        redact_log(log_path, selected_proxy_url)
        if result_code != 0:
            with open(log_path, encoding="utf-8", errors="replace") as log:
                tail = "".join(log.readlines()[-40:])
            raise UpdateError(f"install.sh reload exited with {result_code}\n{tail}")
        prune_dangling_images()
        status.publish("success", "done", elapsed_seconds=int(time.time()) - status.started,
                       detail="")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--network-config", type=Path)
    parser.add_argument("--engine-handoff", action="store_true")
    args = parser.parse_args()
    data = args.data.resolve()
    network_path = args.network_config.resolve() if args.network_config else None
    if args.engine_handoff:
        try:
            image = perform_engine_handoff(
                args.repo.resolve(), data, args.version, args.repository, network_path)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        print(image)
        return
    status = Status(data / "orchestrator" / "update-status.json", args.version)
    try:
        network = read_network_config(network_path)
        perform(args.repo.resolve(), data, args.version, args.repository, status,
                str(network.get("proxy_url") or ""),
                route=str(network.get("route") or "direct"),
                route_name=str(network.get("route_name") or ""),
                asset_sizes=network.get("asset_sizes") if isinstance(
                    network.get("asset_sizes"), dict) else {},
                routes=network.get("routes") if isinstance(network.get("routes"), list) else None)
    except Exception as exc:  # published for the WebUI; the unit exit code is for journalctl
        status.publish("failed", status.phase or "error", error=str(exc)[:4000])
        raise SystemExit(1)
    finally:
        if args.network_config:
            try:
                args.network_config.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()

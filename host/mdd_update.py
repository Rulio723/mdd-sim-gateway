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


class UpdateError(RuntimeError):
    pass


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
    command = [
        "curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
        "--tlsv1.2", "--retry", "0" if allow_fallback else "3", "--retry-all-errors",
        "--connect-timeout", "20", "--max-time", "600", "--silent", "--show-error",
        "--user-agent", "mdd-sim-gateway-updater", "--output", str(destination), url,
    ]
    # A route that cannot sustain a useful asset-transfer rate should yield to the next Auto
    # candidate. The final/only route keeps the generous timeout for genuinely slow links.
    if allow_fallback:
        command[1:1] = ["--speed-limit", "65536", "--speed-time", "45"]
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
    while process.poll() is None:
        elapsed = max(0, int(time.monotonic() - started))
        try:
            downloaded = destination.stat().st_size
        except OSError:
            downloaded = 0
        if status:
            status.publish("running", phase, url=url, artifact=artifact,
                           downloaded_bytes=downloaded, total_bytes=max(0, int(total_bytes or 0)),
                           bytes_per_second=int(downloaded / max(1, elapsed)),
                           elapsed_seconds=elapsed, route=route, route_name=route_name,
                           route_attempt=route_attempt, route_total=route_total)
        time.sleep(2)
    _, stderr = process.communicate()
    if process.returncode:
        detail = _redact_proxy_error(stderr, proxy_url).strip().splitlines()
        tail = detail[-1] if detail else f"curl exited with {process.returncode}"
        raise UpdateError(f"release download failed: {tail}")


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
    expected_arch = "arm64" if platform.machine().lower() in {"aarch64", "arm64"} else "amd64"
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


def perform(repo: Path, data: Path, version: str, repo_name: str, status: Status,
            proxy_url: str = "", *, route: str = "direct", route_name: str = "",
            asset_sizes: dict | None = None, routes: list[dict] | None = None):
    if not VERSION_RE.fullmatch(version):
        raise UpdateError(f"invalid target version: {version!r}")
    if not REPOSITORY_RE.fullmatch(repo_name):
        raise UpdateError(f"invalid repository: {repo_name!r}")
    (data / "update").mkdir(mode=0o700, parents=True, exist_ok=True)
    routes = routes if isinstance(routes, list) and routes else [
        {"proxy_url": proxy_url, "route": route, "route_name": route_name}]
    clean_routes = []
    for item in routes:
        if not isinstance(item, dict):
            continue
        candidate_url = str(item.get("proxy_url") or "")
        network_environment(candidate_url)  # validate before creating the staging directory
        clean_routes.append({"proxy_url": candidate_url,
                             "route": "library" if candidate_url else "direct",
                             "route_name": str(item.get("route_name") or "")[:120]})
    if not clean_routes:
        raise UpdateError("no usable update download route")
    active_route = 0
    staging = Path(tempfile.mkdtemp(prefix="mdd-update.", dir=str(data / "update")))
    try:
        mode = installed_mode(data)
        asset_sizes = asset_sizes or {}
        archive_name = f"mdd-sim-gateway-v{version}.tar.gz"
        control_name = f"mdd-sim-gateway-control-v{version}-arm64.tar.gz"
        base = f"https://github.com/{repo_name}/releases/download/v{version}"
        url = f"{base}/{archive_name}"
        status.publish("running", "downloading", url=url, install_mode=mode,
                       route=clean_routes[active_route]["route"],
                       route_name=clean_routes[active_route]["route_name"], artifact=archive_name,
                       route_attempt=1, route_total=len(clean_routes))
        archive = staging / archive_name
        sums = staging / "SHA256SUMS"

        def fetch(download_url: str, destination: Path, artifact: str,
                  *, phase: str = "downloading"):
            nonlocal active_route
            order = [active_route] + [i for i in range(len(clean_routes)) if i != active_route]
            failures = []
            for attempt, index in enumerate(order, 1):
                candidate = clean_routes[index]
                try:
                    destination.unlink(missing_ok=True)
                    download(download_url, destination,
                             network_environment(candidate["proxy_url"]), candidate["proxy_url"],
                             status=status, artifact=artifact,
                             total_bytes=asset_sizes.get(artifact, 0),
                             route=candidate["route"], route_name=candidate["route_name"],
                             phase=phase, route_attempt=attempt, route_total=len(order),
                             allow_fallback=attempt < len(order))
                except UpdateError as exc:
                    failures.append(str(exc))
                    continue
                active_route = index
                return
            raise UpdateError("all update download routes failed: " + "; ".join(failures)[-1500:])

        fetch(url, archive, archive_name)
        fetch(f"{base}/SHA256SUMS", sums, "SHA256SUMS")

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

        if mode == "docker":
            if shutil.disk_usage(data / "update").free < 1024 * 1024 * 1024:
                raise UpdateError("not enough persistent disk space to import the control image")
            status.publish("running", "control_image", artifact=control_name, detail="")
            control_archive = staging / control_name
            fetch(f"{base}/{control_name}", control_archive, control_name,
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
        if mode == "docker":
            env["MDD_REUSE_CONTROL_IMAGE"] = "1"
        result_code = reload_services(
            ["sh", str(repo / "install.sh"), "reload", "--no-engines"], repo, env,
            log_path, status, selected_proxy_url)
        redact_log(log_path, selected_proxy_url)
        if result_code != 0:
            with open(log_path, encoding="utf-8", errors="replace") as log:
                tail = "".join(log.readlines()[-40:])
            raise UpdateError(f"install.sh reload exited with {result_code}\n{tail}")
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
    args = parser.parse_args()
    data = args.data.resolve()
    status = Status(data / "orchestrator" / "update-status.json", args.version)
    try:
        network_path = args.network_config.resolve() if args.network_config else None
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

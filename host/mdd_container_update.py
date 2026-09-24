#!/usr/bin/env python3
"""Detached, transactional updater for the three-service container deployment.

Control launches this file in a short-lived sibling made from the currently running Control
image.  The sibling survives replacement of Control, owns no host namespace, and can touch only
the project data directory and Docker socket.  Release image archives are verified against the
Release SHA256SUMS before they are loaded.  The Compose file is changed only after all four
images pass their architecture/component/version checks.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import docker

try:
    import mdd_update
except ModuleNotFoundError:  # Imported as host.mdd_container_update by tests.
    from host import mdd_update


COMPONENTS = ("control", "hardware", "egress", "engine")
BASE_COMPONENTS = ("hardware", "egress", "control")
MANAGED = "io.mdd-sim-gateway.managed"
COMPONENT = "io.mdd-sim-gateway.component"
VERSION = "org.opencontainers.image.version"
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def host_arch() -> str:
    return mdd_update.host_arch()


def find_compose(project: Path) -> Path:
    configured = os.environ.get("MDD_COMPOSE_FILE", "").strip()
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = project / candidate
        candidate = candidate.resolve()
        if candidate.parent != project.resolve() or candidate.name not in COMPOSE_NAMES:
            raise mdd_update.UpdateError("MDD_COMPOSE_FILE must name a Compose file in /data")
        if not candidate.is_file():
            raise mdd_update.UpdateError(f"Compose file does not exist: {candidate.name}")
        return candidate
    matches = [project / name for name in COMPOSE_NAMES if (project / name).is_file()]
    if len(matches) != 1:
        raise mdd_update.UpdateError(
            "container update requires exactly one docker-compose.yml/compose.yaml in /data")
    return matches[0]


def canonical_images(repository: str, version: str) -> dict[str, str]:
    owner = repository.split("/", 1)[0].lower()
    return {component: f"ghcr.io/{owner}/mdd-sim-gateway-{component}:v{version}"
            for component in COMPONENTS}


def rewrite_compose(source: str, images: dict[str, str]) -> str:
    """Replace only the four release image references, preserving user edits and comments."""
    changed: set[str] = set()
    output = []
    image_line = re.compile(
        r"^(?P<indent>\s*)image:\s*(?P<quote>['\"]?)"
        r"ghcr\.io/[^/\s'\"]+/mdd-sim-gateway-(?P<component>control|hardware|egress)"
        r"(?::[^\s'\"]+|@sha256:[0-9a-f]{64})(?P=quote)"
        r"(?P<suffix>\s*(?:#.*)?)$")
    engine_line = re.compile(
        r"^(?P<indent>\s*)MDD_ENGINE_IMAGE:\s*(?P<quote>['\"]?)"
        r"ghcr\.io/[^/\s'\"]+/mdd-sim-gateway-engine"
        r"(?::[^\s'\"]+|@sha256:[0-9a-f]{64})(?P=quote)"
        r"(?P<suffix>\s*(?:#.*)?)$")
    for line in source.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        newline = line[len(raw):]
        match = image_line.match(raw)
        if match:
            component = match.group("component")
            changed.add(component)
            quote = match.group("quote")
            line = (f"{match.group('indent')}image: {quote}{images[component]}{quote}"
                    f"{match.group('suffix')}{newline}")
        else:
            match = engine_line.match(raw)
            if match:
                changed.add("engine")
                quote = match.group("quote")
                line = (f"{match.group('indent')}MDD_ENGINE_IMAGE: "
                        f"{quote}{images['engine']}{quote}{match.group('suffix')}{newline}")
        output.append(line)
    if changed != set(COMPONENTS):
        missing = ", ".join(sorted(set(COMPONENTS) - changed))
        raise mdd_update.UpdateError(f"Compose image references are incomplete: {missing}")
    return "".join(output)


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 600) -> str:
    completed = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout)
    if completed.returncode:
        raise mdd_update.UpdateError(
            f"{' '.join(command[:3])} failed: {completed.stdout[-2000:].strip()}")
    return completed.stdout


def local_loaded_image(component: str, version: str, repository: str) -> str:
    if component == "engine":
        owner = repository.split("/", 1)[0].lower()
        return f"ghcr.io/{owner}/mdd-sim-gateway-engine:v{version}"
    return f"mdd-sim-gateway/{component}:v{version}"


def verify_and_tag_image(client, component: str, version: str, repository: str,
                         target: str) -> str:
    image = client.images.get(local_loaded_image(component, version, repository))
    labels = (image.attrs.get("Config") or {}).get("Labels") or {}
    actual_arch = str(image.attrs.get("Architecture") or "")
    if actual_arch != host_arch() or labels.get(MANAGED) != "true" \
            or labels.get(VERSION) != version:
        raise mdd_update.UpdateError(
            f"Release {component} image identity mismatch: "
            f"{actual_arch or 'unknown'}|{labels.get(VERSION) or 'unknown'}")
    if component in {"control", "hardware", "egress"} \
            and labels.get(COMPONENT) != component:
        raise mdd_update.UpdateError(f"Release {component} image has the wrong component label")
    if component == "engine" and "socks5" not in str(
            labels.get("io.mdd-sim-gateway.egress-transports") or "").split(","):
        raise mdd_update.UpdateError("Release Engine image lacks the container egress capability")
    repository_name, tag = target.rsplit(":", 1)
    if not image.tag(repository_name, tag):
        raise mdd_update.UpdateError(f"could not tag the verified {component} image")
    return image.id


def wait_container(client, name: str, image_id: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    last = "missing"
    while time.monotonic() < deadline:
        try:
            container = client.containers.get(name)
            container.reload()
            labels = (container.attrs.get("Config") or {}).get("Labels") or {}
            health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
            last = health or (container.attrs.get("State") or {}).get("Status") or "unknown"
            if labels.get(MANAGED) == "true" and container.image.id == image_id \
                    and last in {"healthy", "running"}:
                return
        except docker.errors.NotFound:
            last = "missing"
        time.sleep(3)
    raise mdd_update.UpdateError(f"{name} did not become healthy ({last})")


def compose_up(compose: Path) -> None:
    run(["docker", "compose", "-p", "mdd-sim-gateway", "-f", str(compose),
         "up", "-d", "--no-build", "--force-recreate", *BASE_COMPONENTS],
        cwd=compose.parent, timeout=600)


def docker_root_free_bytes(client) -> int:
    """Measure the daemon's image store from a read-only bind in a disposable container."""
    root = str((client.info() or {}).get("DockerRootDir") or "").strip()
    if not root.startswith("/"):
        raise mdd_update.UpdateError("Docker did not report an absolute image-store path")
    current = client.containers.get(socket.gethostname())
    output = client.containers.run(
        current.image.id,
        ["python", "-c",
         "import os; s=os.statvfs('/docker-root'); print(s.f_bavail*s.f_frsize)"],
        remove=True, network_disabled=True, read_only=True, cap_drop=["ALL"],
        volumes={root: {"bind": "/docker-root", "mode": "ro"}},
    )
    try:
        return int(output.decode().strip() if isinstance(output, bytes) else str(output).strip())
    except ValueError as exc:
        raise mdd_update.UpdateError("could not measure free Docker image-store space") from exc


def recreate_engine(client, name: str) -> None:
    """Ask the freshly started Control container to recreate one previously running line."""
    prefix = "mdd-sim-gateway-engine-"
    iid = name.removeprefix(prefix)
    if name == iid or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", iid):
        raise mdd_update.UpdateError(f"invalid managed Engine name: {name}")
    control = client.containers.get("mdd-sim-gateway-control")
    command = [
        "python", "-c",
        ("import os,sys; from app import config as cfg, engine; "
         "i=cfg.get_instance(sys.argv[1]); "
         "assert i is not None, 'saved line is missing'; "
         "engine.start(i, cfg.get_settings(), "
         "dev_mounts=os.environ.get('MDD_DEV_MOUNTS','') == '1', "
         "reason='release_update')"),
        iid,
    ]
    result = control.exec_run(command)
    if hasattr(result, "exit_code"):
        exit_code, output = int(result.exit_code), result.output
    else:
        exit_code, output = int(result[0]), result[1]
    if exit_code:
        detail = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
        raise mdd_update.UpdateError(f"could not recreate {name}: {detail[-1000:].strip()}")


def roll_engines(client, target_image_id: str, status: mdd_update.Status) -> None:
    engines = [item for item in client.containers.list(filters={"label": [
        f"{MANAGED}=true", f"{COMPONENT}=engine"]})]
    for index, old in enumerate(sorted(engines, key=lambda item: item.name), 1):
        name = old.name
        status.publish("running", "engine_rollout", artifact=name,
                       engine_index=index, engine_total=len(engines))
        old.remove(force=True)
        recreate_engine(client, name)
        wait_container(client, name, target_image_id, timeout=240)


def perform(project: Path, version: str, repository: str, network_path: Path,
            status: mdd_update.Status) -> None:
    staging = Path(tempfile.mkdtemp(prefix="container-update.", dir=str(project / "update")))
    client = None
    compose = None
    compose_backup = project / "update" / "compose.previous.yaml"
    switched = False
    old_base_ids = {}
    old_engine_ids = {}
    try:
        compose = find_compose(project)
        request_network = mdd_update.read_network_config(network_path)
        fallback = str(request_network.get("proxy_url") or "")
        routes = mdd_update.validated_download_routes(
            fallback,
            route=str(request_network.get("route") or ("library" if fallback else "direct")),
            route_name=str(request_network.get("route_name") or ""),
            routes=request_network.get("routes")
            if isinstance(request_network.get("routes"), list) else None)
        sizes = request_network.get("asset_sizes") if isinstance(
            request_network.get("asset_sizes"), dict) else {}
        if shutil.disk_usage(project / "update").free < 6 * 1024 * 1024 * 1024:
            raise mdd_update.UpdateError(
                "not enough persistent disk space for a transactional container update")
        arch = host_arch()
        names = {component: f"mdd-sim-gateway-{component}-v{version}-{arch}.tar.gz"
                 for component in COMPONENTS}
        base_url = f"https://github.com/{repository}/releases/download/v{version}"
        client = docker.from_env()
        if docker_root_free_bytes(client) < 6 * 1024 * 1024 * 1024:
            raise mdd_update.UpdateError(
                "not enough Docker image-store space for a transactional container update")
        old_base_ids = {
            component: client.containers.get(f"mdd-sim-gateway-{component}").image.id
            for component in BASE_COMPONENTS
        }
        # Only lines which are running at the start of the transaction belong in the rollout.
        # Disabled, PIN-frozen and manually stopped lines must remain stopped.
        old_engine_ids = {
            item.name: item.image.id
            for item in client.containers.list(filters={"label": [
                f"{MANAGED}=true", f"{COMPONENT}=engine"]})
        }
        status.publish("running", "downloading", install_mode="container",
                       engine_image_required=True)
        sums = staging / "SHA256SUMS"
        active = mdd_update.fetch_release_asset(
            f"{base_url}/SHA256SUMS", sums, "SHA256SUMS", routes,
            asset_sizes=sizes, status=status)
        archives = {}
        archive_digests = {}
        for component in COMPONENTS:
            name = names[component]
            archive = staging / name
            active = mdd_update.fetch_release_asset(
                f"{base_url}/{name}", archive, name, routes, active,
                asset_sizes=sizes, status=status, phase=f"{component}_image")
            archive_digests[component] = mdd_update.verify_release_file(
                archive, sums, f"{arch} {component} image")
            archives[component] = archive

        targets = canonical_images(repository, version)
        image_ids = {}
        for component in COMPONENTS:
            status.publish("running", f"{component}_image", artifact=names[component],
                           detail=f"importing verified {arch} {component} image")
            run(["docker", "load", "--input", str(archives[component])], timeout=1800)
            image_ids[component] = verify_and_tag_image(
                client, component, version, repository, targets[component])
        verified_images = {
            component: {"reference": targets[component], "image_id": image_ids[component],
                        "archive_sha256": archive_digests[component]}
            for component in COMPONENTS
        }

        # Use the application's SQLite snapshot backup while the old Control is still running.
        sys.path.insert(0, "/app/control")
        try:
            from app import operations  # type: ignore  # pylint: disable=import-outside-toplevel
        except ModuleNotFoundError:
            from control.app import operations  # pylint: disable=import-outside-toplevel
        status.publish("running", "backup")
        saved = operations.create_local_backup("pre-container-update")

        original = compose.read_text(encoding="utf-8")
        updated = rewrite_compose(original, targets)
        status.publish("running", "applying", backup=saved.get("name", ""))
        compose_backup.write_text(original, encoding="utf-8")
        os.chmod(compose_backup, 0o600)
        temporary = compose.with_suffix(compose.suffix + ".tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.chmod(temporary, compose.stat().st_mode & 0o777)
        os.replace(temporary, compose)
        switched = True

        status.publish("running", "reloading", backup=saved.get("name", ""))
        compose_up(compose)
        for component in BASE_COMPONENTS:
            wait_container(client, f"mdd-sim-gateway-{component}", image_ids[component])
        roll_engines(client, image_ids["engine"], status)
        mdd_update.atomic_json(project / "update" / "installed-images.json", {
            "version": version, "architecture": arch, "installed_at": int(time.time()),
            "images": verified_images})
        status.publish("success", "done", backup=saved.get("name", ""),
                       elapsed_seconds=int(time.time()) - status.started)
    except Exception as exc:
        rollback_ok = False
        rollback_error = ""
        if switched and compose is not None and compose_backup.is_file() and client is not None:
            try:
                status.publish("running", "rollback", error=str(exc)[:1000])
                shutil.copy2(compose_backup, compose)
                compose_up(compose)
                for component in BASE_COMPONENTS:
                    wait_container(client, f"mdd-sim-gateway-{component}",
                                   old_base_ids[component])
                for name, old_image_id in old_engine_ids.items():
                    try:
                        current = client.containers.get(name)
                        current.reload()
                        if current.image.id == old_image_id \
                                and (current.attrs.get("State") or {}).get("Status") == "running":
                            continue
                        current.remove(force=True)
                    except docker.errors.NotFound:
                        pass
                    recreate_engine(client, name)
                    wait_container(client, name, old_image_id, timeout=240)
                rollback_ok = True
            except Exception as rollback_exc:  # preserve both causes in the private status
                rollback_error = str(rollback_exc)[:1000]
        status.publish("failed", "rollback" if switched else status.phase,
                       error=str(exc)[:2000], rollback_succeeded=rollback_ok,
                       rollback_error=rollback_error)
        raise
    finally:
        if client is not None:
            client.close()
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--network-config", required=True, type=Path)
    args = parser.parse_args()
    if not mdd_update.VERSION_RE.fullmatch(args.version) \
            or not mdd_update.REPOSITORY_RE.fullmatch(args.repository):
        raise SystemExit("invalid update target")
    project = args.data.resolve()
    (project / "update").mkdir(mode=0o700, parents=True, exist_ok=True)
    status = mdd_update.Status(project / "orchestrator" / "update-status.json", args.version)
    try:
        perform(project, args.version, args.repository, args.network_config.resolve(), status)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

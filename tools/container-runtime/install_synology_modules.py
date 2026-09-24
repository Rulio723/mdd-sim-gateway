#!/usr/bin/env python3
"""Install or remove the exact validated DS1621+ modem modules and DSM boot hook."""
import argparse
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "runtime" / "synology-v1000-7.4-modules.json"
LOADER = ROOT / "runtime" / "synology-load-modules.sh"
INSTALL_DIR = "/usr/local/lib/mdd-sim-gateway-modules"
RC_PATH = "/usr/local/etc/rc.d/mdd-sim-gateway-modules.sh"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", required=True)
    # Absolute path on the NAS holding the modules built from the vendor toolchain.
    # Deliberately required: it is a per-operator build location, not a project default.
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=6", "-i", args.identity, "-p", str(args.port), args.host]

    if args.uninstall:
        command = ["sudo", "-n", "sh", "-c",
                   f"rm -f {shlex.quote(RC_PATH)}; rm -rf {shlex.quote(INSTALL_DIR)}"]
        subprocess.run(ssh + [shlex.join(command)], check=True)
        return

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    compatibility = manifest["compatibility"]
    modules = manifest["modules"]
    checksum_text = "".join(f"{digest}  {name}\n" for name, digest in sorted(modules.items()))
    compat_text = "\n".join((
        "MDD_ARCH=" + shlex.quote(compatibility["architecture"]),
        "MDD_KERNEL=" + shlex.quote(compatibility["kernel_release"]),
        "MDD_DSM=" + shlex.quote(compatibility["dsm"]),
        "MDD_PLATFORM_MARKER=" + shlex.quote(
            "synology_" + compatibility["platform"] + "_1621+"),
    )) + "\n"

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name, data, mode in (
                ("loader.sh", LOADER.read_bytes(), 0o755),
                ("manifest.json", MANIFEST.read_bytes(), 0o644),
                ("SHA256SUMS", checksum_text.encode(), 0o644),
                ("compatibility.env", compat_text.encode(), 0o644)):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), mode
            archive.addfile(member, io.BytesIO(data))
    stage = subprocess.check_output(
        ssh + ["mktemp -d /tmp/mdd-module-install.XXXXXX"], text=True).strip()
    if not stage.startswith("/tmp/mdd-module-install."):
        raise RuntimeError("unexpected remote staging directory")
    try:
        subprocess.run(ssh + [f"tar xzf - -C {shlex.quote(stage)}"],
                       input=payload.getvalue(), check=True)
        source = shlex.quote(args.source_dir)
        install = shlex.quote(INSTALL_DIR)
        rc_path = shlex.quote(RC_PATH)
        module_args = " ".join(shlex.quote(name) for name in modules)
        script = (
            f"set -eu; mkdir -p {install}; "
            f"for name in {module_args}; do install -m 644 {source}/$name {install}/$name; done; "
            f"install -m 644 {shlex.quote(stage)}/manifest.json {install}/manifest.json; "
            f"install -m 644 {shlex.quote(stage)}/SHA256SUMS {install}/SHA256SUMS; "
            f"install -m 644 {shlex.quote(stage)}/compatibility.env {install}/compatibility.env; "
            f"install -m 755 {shlex.quote(stage)}/loader.sh {rc_path}; "
            f"{rc_path} start; {rc_path} status")
        subprocess.run(ssh + [shlex.join(["sudo", "-n", "sh", "-c", script])], check=True)
    finally:
        subprocess.run(ssh + [shlex.join(["rm", "-rf", "--", stage])], check=False)


if __name__ == "__main__":
    main()

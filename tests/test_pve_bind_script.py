import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "pve-bind-ec25-modems.sh"


class PveBindScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.sys_usb = self.root / "sys"
        self.configs = self.root / "configs"
        self.usb_dev = self.root / "dev"
        self.proc = self.root / "proc"
        self.bin = self.root / "bin"
        for path in (self.sys_usb, self.configs, self.usb_dev, self.proc, self.bin):
            path.mkdir()

        self._modem("3-1", bus=3, port="1", devnum=39)
        self._modem("3-2", bus=3, port="2", devnum=38)
        (self.configs / "104.conf").write_text(
            "args: -device qemu-xhci,id=x1 -device qemu-xhci,id=x2 "
            "-device usb-host,hostbus=3,hostport=1,bus=x1.0 "
            "-device usb-host,hostbus=3,hostport=2,bus=x2.0\n"
        )
        self._command(
            "qm",
            """#!/usr/bin/env bash
if [[ "$1" == status ]]; then echo 'status: stopped'; exit 0; fi
if [[ "$1" == config ]]; then cat "$MDD_PVE_CONFIG_ROOT/$2.conf"; exit 0; fi
exit 1
""",
        )
        self._command("fuser", "#!/usr/bin/env bash\nexit 1\n")

    def tearDown(self):
        self.tempdir.cleanup()

    def _modem(self, name, *, bus, port, devnum):
        path = self.sys_usb / name
        path.mkdir()
        values = {
            "idVendor": "2c7c",
            "idProduct": "0125",
            "busnum": str(bus),
            "devpath": port,
            "devnum": str(devnum),
        }
        for filename, value in values.items():
            (path / filename).write_text(value + "\n")

    def _command(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def _run(self):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "MDD_PVE_SYS_USB_ROOT": str(self.sys_usb),
                "MDD_PVE_CONFIG_ROOT": str(self.configs),
                "MDD_PVE_USB_DEV_ROOT": str(self.usb_dev),
                "MDD_PVE_PROC_ROOT": str(self.proc),
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT), "104"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_accepts_exclusive_target_binding(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already bound", result.stdout)

    def test_rejects_other_vm_usb_topology(self):
        (self.configs / "103.conf").write_text("usb0: host=3-1,usb3=1\n")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("VM 103", result.stderr)
        self.assertIn("another VM is configured", result.stderr)

    def test_rejects_other_vm_vendor_product_binding(self):
        (self.configs / "103.conf").write_text("usb0: host=2c7c:0125\n")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("VM 103", result.stderr)

    def test_rejects_other_vm_custom_args(self):
        (self.configs / "103.conf").write_text(
            "args: -device usb-host,hostbus=3,hostport=2,id=usb1\n"
        )
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("VM 103 args", result.stderr)

    def test_rejects_foreign_runtime_holder(self):
        (self.proc / "777").mkdir()
        (self.proc / "777" / "cmdline").write_bytes(
            b"/usr/bin/kvm\0-id\0" + b"103\0-name\0Vocat\0"
        )
        self._command(
            "fuser",
            """#!/usr/bin/env bash
if [[ "$1" == */003/038 ]]; then echo 777; exit 0; fi
exit 1
""",
        )
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("PID 777 VM 103", result.stderr)
        self.assertIn("already holds", result.stderr)


if __name__ == "__main__":
    unittest.main()

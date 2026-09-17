"""install.sh: the udev rule that releases a Quectel module's secondary AT port for MMS.

The shell functions are cut out of install.sh and run against a temporary rules directory,
with udevadm and systemctl replaced by stubs that record how they were called.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL = (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")


def shell_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL, re.M | re.S)
    assert match, name
    return match.group(0)


class InstallMmsRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.rules = root / "rules.d"
        self.rules.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        for tool in ("udevadm", "systemctl"):
            stub = self.bin / tool
            stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{self.log}"\nexit 0\n')
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.temp.cleanup()

    def run_installer(self, *calls: str, rules_dir: Path | None = None) -> list[str]:
        self.log.write_text("")
        script = "\n".join([
            "set -e",
            'have() { command -v "$1" >/dev/null 2>&1; }',
            "info() { :; }",
            re.search(r"^MMS_AT_PORT_RULE=.*$", INSTALL, re.M).group(0),
            shell_function("reapply_modem_port_rules"),
            shell_function("ensure_mms_at_port_rule"),
            shell_function("remove_mms_at_port_rule"),
            *calls,
        ])
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "MDD_UDEV_RULES_DIR": str(rules_dir or self.rules)}
        subprocess.run(["sh", "-c", script], check=True, env=env)
        return [line for line in self.log.read_text().splitlines() if line]

    @property
    def rule(self) -> Path:
        return self.rules / "78-mdd-mms-at-port.rules"

    def test_install_writes_the_rule_and_reapplies_it(self):
        calls = self.run_installer("ensure_mms_at_port_rule")
        text = self.rule.read_text()
        self.assertIn('ATTRS{idVendor}=="2c7c"', text)
        self.assertIn('ENV{ID_MM_PORT_TYPE_AT_SECONDARY}=="1"', text)
        self.assertIn('ENV{ID_MM_PORT_IGNORE}="1"', text)
        self.assertNotIn("bInterfaceNumber", text, "never keyed on an interface number")
        self.assertNotIn("ID_MM_PORT_TYPE_AT_PRIMARY", text)
        self.assertEqual(oct(self.rule.stat().st_mode & 0o777), "0o644")
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty", calls)
        self.assertIn("systemctl restart ModemManager.service", calls)

    def test_reinstall_with_an_unchanged_rule_touches_nothing(self):
        self.run_installer("ensure_mms_at_port_rule")
        self.assertEqual(self.run_installer("ensure_mms_at_port_rule"), [])

    def test_a_changed_rule_is_rewritten_and_reapplied(self):
        self.rule.write_text("# an earlier, interface-number based rule\n")
        calls = self.run_installer("ensure_mms_at_port_rule")
        self.assertIn("ID_MM_PORT_TYPE_AT_SECONDARY", self.rule.read_text())
        self.assertIn("systemctl restart ModemManager.service", calls)

    def test_uninstall_removes_the_rule_and_returns_the_port(self):
        self.run_installer("ensure_mms_at_port_rule")
        calls = self.run_installer("remove_mms_at_port_rule")
        self.assertFalse(self.rule.exists())
        # Without a change event the udev database would keep ID_MM_PORT_IGNORE until reboot.
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty", calls)
        self.assertIn("systemctl restart ModemManager.service", calls)
        self.assertEqual(self.run_installer("remove_mms_at_port_rule"), [],
                         "uninstalling twice is a no-op")

    def test_host_without_udev_rules_directory_is_left_alone(self):
        missing = self.rules / "absent"
        self.assertEqual(self.run_installer("ensure_mms_at_port_rule", rules_dir=missing), [])
        self.assertFalse(missing.exists())

    def test_uninstall_command_uses_the_removal(self):
        body = shell_function("cmd_uninstall") if re.search(r"^cmd_uninstall\(\) \{", INSTALL,
                                                             re.M) else INSTALL
        self.assertIn("remove_mms_at_port_rule", body)


if __name__ == "__main__":
    unittest.main()

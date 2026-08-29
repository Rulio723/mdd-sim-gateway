"""Regression coverage for per-device capability operation state."""
import re
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parent.parent
          / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")


class CapabilitySwitchStateTests(unittest.TestCase):
    def test_every_capability_switch_is_keyed_by_device_and_capability(self):
        switches = re.findall(r"<CapabilitySwitch\b[^>]*>", SOURCE)
        self.assertTrue(switches)
        for switch in switches:
            kind = re.search(r'kind="([^"]+)"', switch)
            key = re.search(r'key=\{`\$\{d\.id\}:([^`]+)`\}', switch)
            self.assertIsNotNone(kind, switch)
            self.assertIsNotNone(key, switch)
            self.assertEqual(key.group(1), kind.group(1), switch)


if __name__ == "__main__":
    unittest.main()

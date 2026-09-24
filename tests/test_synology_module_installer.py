import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SynologyModuleInstallerTests(unittest.TestCase):
    def test_loader_is_valid_shell_and_covers_exact_manifest(self):
        loader = ROOT / "runtime" / "synology-load-modules.sh"
        subprocess.run(["sh", "-n", str(loader)], check=True)
        text = loader.read_text(encoding="utf-8")
        manifest = json.loads((ROOT / "runtime" /
                               "synology-v1000-7.4-modules.json").read_text())
        for filename in manifest["modules"]:
            self.assertIn(Path(filename).stem.replace("-", "_"), text)
        self.assertIn("sha256sum -c SHA256SUMS", text)
        self.assertIn('\"$(uname -r)\" = \"$MDD_KERNEL\"', text)

    def test_uninstall_does_not_force_unload_live_modules(self):
        installer = (ROOT / "tools" / "container-runtime" /
                     "install_synology_modules.py").read_text(encoding="utf-8")
        uninstall = installer.split("if args.uninstall:", 1)[1].split("return", 1)[0]
        self.assertNotIn("rmmod", uninstall)
        self.assertNotIn("docker", uninstall)


if __name__ == "__main__":
    unittest.main()

"""The distributable Engine image must not contain its build toolchain.

The image is copied from the Mac to a small Raspberry Pi system disk.  Keeping Asterisk's
compiler, headers and source checkout in the final stage roughly triples both transfer and
unpacked size, even though none of them are used after the binaries have been installed.
"""
import re
import unittest
from pathlib import Path


DOCKERFILE = (
    Path(__file__).resolve().parent.parent / "engine" / "Dockerfile"
).read_text(encoding="utf-8")


class EngineImageLayoutTests(unittest.TestCase):
    def test_pinned_sources_use_the_reviewed_github_mirrors(self):
        self.assertIn(
            "PJPROJECT_REPOSITORY=https://github.com/MddIdd/pjproject-sysmocom-mirror.git",
            DOCKERFILE,
        )
        self.assertIn(
            "ASTERISK_REPOSITORY=https://github.com/MddIdd/asterisk-sysmocom-mirror.git",
            DOCKERFILE,
        )
        self.assertNotIn("gitea.sysmocom.de", DOCKERFILE)

    def test_engine_uses_separate_build_and_runtime_stages(self):
        stages = re.findall(r"(?im)^FROM\s+\S+\s+AS\s+(\S+)", DOCKERFILE)
        self.assertEqual(stages, ["build", "runtime"])

    def test_runtime_installs_a_derived_library_closure(self):
        self.assertIn("ldd \"$binary\"", DOCKERFILE)
        self.assertIn("rpm -qf --qf '%{NAME}'", DOCKERFILE)
        self.assertIn("> /runtime-packages.txt", DOCKERFILE)
        self.assertIn("COPY --from=build /runtime-packages.txt", DOCKERFILE)

    def test_runtime_copies_installed_outputs_not_the_build_tree(self):
        runtime = DOCKERFILE.split(" AS runtime", 1)[1]
        self.assertIn("COPY --from=build /usr/sbin/asterisk", runtime)
        self.assertIn("COPY --from=build /usr/lib/asterisk/", runtime)
        self.assertIn("COPY --from=build /usr/local/", runtime)
        self.assertNotIn("/home/asterisk-build", runtime)
        self.assertNotRegex(runtime, r"(?m)^RUN .*\b(make|gcc|git clone)\b")

    def test_build_fails_for_a_missing_shared_library_or_python_module(self):
        runtime = DOCKERFILE.split(" AS runtime", 1)[1]
        self.assertIn("grep -q 'not found'", runtime)
        self.assertIn("missing runtime dependency", runtime)
        self.assertIn("import Crypto, cryptography, jinja2, panoramisk", runtime)


if __name__ == "__main__":
    unittest.main()

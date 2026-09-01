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


class AsteriskKeepListTests(unittest.TestCase):
    """The image ships only the Asterisk modules the keep-list names.

    The runtime package closure is derived by running ldd over every modules/*.so, so an
    unloadable module still drags its library closure into the runtime image: libicu came in
    on res_calendar_icalendar, lpcnetfreedv on codec_codec2, perl-libs + net-snmp on res_snmp.
    modules.conf.j2 already noloads all three — noload simply does not reach the disk. Pruning
    is therefore only worth anything if it happens BEFORE the closure is derived, which is the
    opposite of where the strip/staging layer belongs.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def keep_list(self):
        text = (self.ROOT / "engine" / "asterisk-keep-modules.txt").read_text(encoding="utf-8")
        modules = []
        for line in text.splitlines():
            name = line.split("#", 1)[0].strip()
            if name:
                modules.append(name)
        return modules

    def test_keep_list_is_a_clean_list_of_module_filenames(self):
        modules = self.keep_list()
        self.assertGreater(len(modules), 100, "keep-list looks truncated")
        self.assertEqual(len(modules), len(set(modules)), "duplicate entries in keep-list")
        for module in modules:
            self.assertRegex(module, r"^[a-z0-9_]+\.so$")

    def test_pruning_happens_before_the_closure_is_derived(self):
        self.assertIn("COPY asterisk-keep-modules.txt", DOCKERFILE)
        prune = DOCKERFILE.index("asterisk-keep-modules.txt")
        closure = DOCKERFILE.index("> /runtime-packages.txt")
        install = DOCKERFILE.index("make install")
        self.assertLess(install, prune, "pruning must run after the modules are installed")
        self.assertLess(
            prune, closure,
            "pruning after the closure derivation would not drop a single package",
        )

    def test_a_keep_list_entry_that_was_not_built_fails_the_build(self):
        self.assertIn("keep-list names modules this Asterisk did not build", DOCKERFILE)
        self.assertIn("expected $expected modules after pruning, found $remaining", DOCKERFILE)

    def test_keep_list_agrees_with_the_module_load_policy(self):
        policy = (
            self.ROOT / "engine" / "templates" / "modules.conf.j2"
        ).read_text(encoding="utf-8")
        modules = set(self.keep_list())

        # Whatever the load policy insists on loading must still be on disk to be loaded.
        for required in ("chan_pjsip.so", "codec_amr.so", "res_http_websocket.so",
                         "res_pjsip_messaging.so", "res_rtp_asterisk.so"):
            self.assertIn(required, modules)

        # ...and nothing the policy refuses to load is worth the disk or the ldd closure.
        noloaded = set(re.findall(r"noload => ([a-z0-9_]+\.so)", policy))
        self.assertTrue(noloaded, "module policy no longer noloads anything")
        self.assertEqual(set(), noloaded & modules)

    def test_the_dialplan_and_ami_bridge_keep_the_modules_they_call(self):
        modules = set(self.keep_list())
        # Applications and functions used by extensions.conf.j2, the codecs the IMS and
        # browser legs negotiate, and the PJSIP pieces IMS registration cannot work without.
        # A missing one of these is a silent call or a failed REGISTER, not an error message.
        for module in ("app_dial.so", "app_exec.so", "app_playback.so", "app_record.so",
                       "app_stack.so", "app_system.so", "app_waitforprecondition.so",
                       "func_base64.so", "func_callerid.so", "func_channel.so", "func_cut.so",
                       "func_env.so", "func_logic.so", "func_strings.so",
                       "pbx_config.so", "chan_pjsip.so", "bridge_softmix.so",
                       "codec_amr.so", "codec_alaw.so", "codec_ulaw.so", "codec_resample.so",
                       "format_wav.so", "format_pcm.so", "format_sln.so",
                       "res_format_attr_amr.so", "res_format_attr_opus.so",
                       "res_pjsip.so", "res_pjsip_session.so", "res_pjsip_sdp_rtp.so",
                       "res_pjsip_outbound_registration.so", "res_pjsip_messaging.so",
                       "res_pjsip_header_funcs.so", "res_pjsip_rfc3329.so",
                       "res_pjsip_transport_websocket.so", "res_rtp_asterisk.so",
                       "res_srtp.so", "res_http_websocket.so", "res_resolver_unbound.so",
                       "res_sorcery_config.so", "res_pjproject.so"):
            self.assertIn(module, modules)



    def test_the_workflows_assert_the_number_of_modules_the_list_names(self):
        """CI and the release both count the modules in the built image and fail on a mismatch.

        That assertion is what proves the published image is the one the build checked, so it
        has to move with the keep-list — otherwise editing the list turns a deliberate change
        into a red release, or worse, a stale number nobody notices is no longer meaningful.
        """
        expected = len(self.keep_list())
        for name in ("ci.yml", "release.yml"):
            workflow = (self.ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            counts = re.findall(r"^\s*modules:\s*(\d+)\s*$", workflow, re.MULTILINE)
            self.assertTrue(counts, f"{name} no longer asserts a module count")
            for count in counts:
                self.assertEqual(int(count), expected,
                                 f"{name} expects {count} modules, the keep-list names "
                                 f"{expected}")

if __name__ == "__main__":
    unittest.main()

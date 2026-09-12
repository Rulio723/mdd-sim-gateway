"""A line may present its own SIP User-Agent (issue #83).

Some carriers gate IMS registration on a User-Agent whitelist and answer 403 to a terminal
they do not recognise, which no amount of IMEI configuration fixes -- the IMEI only reaches
the ePDG's DEVICE_IDENTITY. The value is rendered verbatim into pjsip.conf's [global]
user_agent, so the tests that matter here are the ones proving a saved line cannot use it to
write additional Asterisk configuration.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config


def engine_render():
    """Load engine/render.py the way tests/test_engine_paths.py does (it is not a package)."""
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "mdd_render_ua", root / "engine" / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instance(**sip) -> dict:
    return {
        "id": "3", "index": 0, "imsi": "001010000000000",
        "mcc": "001", "mnc": "01", "iccid": "test-card",
        "imei": "490154203237518", "ami_secret": "test-secret",
        "sip": {"webrtc": {"enable": True, "password": "test-password"}, **sip},
    }


class RenderedUserAgentTests(unittest.TestCase):
    def test_a_line_without_an_override_still_identifies_as_the_product(self):
        rendered = config.render_instance_json(instance(), {})

        self.assertEqual(rendered["sip"]["user_agent"], "MDD-Sim-Gateway")

    def test_an_explicit_user_agent_reaches_the_engine(self):
        rendered = config.render_instance_json(
            instance(user_agent="SM-G975F Build/QP1A.190711.020"), {})

        self.assertEqual(rendered["sip"]["user_agent"],
                         "SM-G975F Build/QP1A.190711.020")

    def test_a_blank_override_falls_back_instead_of_emptying_the_header(self):
        # An empty user_agent= in pjsip.conf is not "no override", it is a line that registers
        # without identifying itself at all.
        rendered = config.render_instance_json(instance(user_agent="   "), {})

        self.assertEqual(rendered["sip"]["user_agent"], "MDD-Sim-Gateway")


class UserAgentSanitisingTests(unittest.TestCase):
    """Both sanitisers must agree: the control plane writes instance.json, but a
    hand-authored one reaches the same template through engine/render.py."""

    def sanitisers(self):
        return (("control", config.sanitize_user_agent),
                ("engine", engine_render().sanitize_user_agent))

    def test_a_newline_cannot_append_asterisk_configuration(self):
        attack = "Phone/1.0\nuser_agent=evil\n[volte_ims]\ntype=endpoint"

        for name, sanitize in self.sanitisers():
            with self.subTest(name):
                cleaned = sanitize(attack)
                self.assertNotIn("\n", cleaned)
                self.assertEqual(cleaned, "Phone/1.0 user_agent=evil [volte_ims] type=endpoint")

    def test_a_semicolon_cannot_comment_out_the_rest_of_the_line(self):
        for name, sanitize in self.sanitisers():
            with self.subTest(name):
                self.assertEqual(sanitize("Phone/1.0;evil=yes"), "Phone/1.0 evil=yes")

    def test_control_characters_and_padding_are_dropped(self):
        for name, sanitize in self.sanitisers():
            with self.subTest(name):
                self.assertEqual(sanitize("  Phone\t/\r1.0\x00  "), "Phone / 1.0")

    def test_an_absurd_value_is_capped(self):
        for name, sanitize in self.sanitisers():
            with self.subTest(name):
                cleaned = sanitize("A" * 500)
                self.assertEqual(len(cleaned), 64)
                self.assertFalse(cleaned.endswith(" "))

    def test_nothing_usable_means_use_the_default(self):
        for name, sanitize in self.sanitisers():
            with self.subTest(name):
                self.assertEqual(sanitize(None), "")
                self.assertEqual(sanitize("\n\t"), "")


class SavedLineTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        root = self._temp.name
        self._patch = patch.multiple(config, DATA_DIR=root,
                                     CONFIG_PATH=str(Path(root) / "config.yaml"))
        self._patch.start()
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(self._patch.stop)

    def test_the_stored_value_is_what_the_line_will_present(self):
        # Sanitising only at render time would leave the WebUI showing text that pjsip.conf
        # never receives, with no hint that it had been rewritten.
        saved = config.upsert_instance({
            "id": "1", "imsi": "001010000000000",
            "sip": {"user_agent": " Phone/1.0;evil \n"},
        })

        self.assertEqual(saved["sip"]["user_agent"], "Phone/1.0 evil")

    def test_saving_an_unrelated_field_does_not_invent_a_user_agent(self):
        saved = config.upsert_instance({"id": "1", "imsi": "001010000000000",
                                        "sip": {"listen_addr": "0.0.0.0"}})

        self.assertNotIn("user_agent", saved["sip"])


class EngineFallbackTests(unittest.TestCase):
    def test_a_hand_authored_instance_json_without_a_user_agent_gets_the_default(self):
        render = engine_render()
        ctx = render.build_context({
            "id": "3", "imsi": "001010000000000", "mcc": "001", "mnc": "01",
            "ami_secret": "test-secret", "local_addr": "172.17.0.2",
            "sip": {"webrtc": {"enable": True, "password": "test-password"}},
        })

        self.assertEqual(ctx["user_agent"], "MDD-Sim-Gateway")

    def test_the_engine_honours_and_sanitises_an_override(self):
        render = engine_render()
        ctx = render.build_context({
            "id": "3", "imsi": "001010000000000", "mcc": "001", "mnc": "01",
            "ami_secret": "test-secret", "local_addr": "172.17.0.2",
            "sip": {"webrtc": {"enable": True, "password": "test-password"},
                    "user_agent": "Phone/1.0\nbind=0.0.0.0"},
        })

        self.assertEqual(ctx["user_agent"], "Phone/1.0 bind=0.0.0.0")


if __name__ == "__main__":
    unittest.main()

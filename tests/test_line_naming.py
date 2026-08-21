import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config

try:
    from control.app import main
except ImportError:      # the Docker SDK is a manager runtime dependency naming does not need
    main = None

ICCID_A = "8944000000000004409"
ICCID_B = "8944000000000001234"


class TempConfig:
    """Run one test against a throwaway config.yaml."""

    def __init__(self):
        self._temp = tempfile.TemporaryDirectory()

    def __enter__(self):
        root = self._temp.name
        self._patch = patch.multiple(config, DATA_DIR=root,
                                     CONFIG_PATH=str(Path(root) / "config.yaml"))
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._temp.cleanup()
        return False

    def add(self, iid, name, **fields):
        return config.upsert_instance({"id": iid, "name": name, **fields})


class GeneratedNameTests(unittest.TestCase):
    def test_the_generated_name_carries_the_carrier_and_the_sim_tail(self):
        self.assertEqual(config.default_instance_name("234", "10", ICCID_A), "234-10-4409")
        self.assertEqual(config.default_instance_name("310", "240", ICCID_B), "310-240-1234")

    def test_a_sim_read_before_its_carrier_still_gets_a_distinguishing_name(self):
        # MCC/MNC can be missing at creation; the ICCID never is, so it carries the name.
        self.assertEqual(config.default_instance_name("", "", ICCID_A), "SIM-4409")
        self.assertEqual(config.default_instance_name("234", "", ICCID_A), "SIM-4409")
        self.assertEqual(config.default_instance_name("", "", ""), "New SIM")

    def test_two_sims_of_one_carrier_no_longer_share_a_name(self):
        first = config.default_instance_name("234", "10", ICCID_A)
        second = config.default_instance_name("234", "10", ICCID_B)
        self.assertNotEqual(first, second)


class UniquenessTests(unittest.TestCase):
    def test_a_generated_name_that_collides_is_suffixed(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "234-10-4409")
            # Same carrier AND same last four digits — rare, but only four digits exist.
            second = config.upsert_instance({"id": "2", "name": "234-10-4409"},
                                            unique_name=True)
            third = config.upsert_instance({"id": "3", "name": "234-10-4409"},
                                           unique_name=True)
        self.assertEqual(second["name"], "234-10-4409 (2)")
        self.assertEqual(third["name"], "234-10-4409 (3)")

    def test_suffixing_ignores_case_the_way_the_bot_matches_names(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            clash = config.upsert_instance({"id": "2", "name": "giff"}, unique_name=True)
        self.assertEqual(clash["name"], "giff (2)")

    def test_an_explicit_name_is_never_silently_altered(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            same = cfg_temp.add("2", "Giff")           # no unique_name -> stored verbatim
        self.assertEqual(same["name"], "Giff")

    def test_updating_a_line_keeps_its_own_name(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            again = config.upsert_instance({"id": "1", "name": "Giff", "msisdn": "+4477"},
                                           unique_name=True)
        self.assertEqual(again["name"], "Giff")

    def test_name_taken_excludes_the_line_itself_and_ignores_blanks(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            cfg_temp.add("2", "")
            self.assertTrue(config.instance_name_taken("giff"))
            self.assertTrue(config.instance_name_taken("Giff", exclude_iid="9"))
            self.assertFalse(config.instance_name_taken("Giff", exclude_iid="1"))
            self.assertFalse(config.instance_name_taken(""))
            self.assertFalse(config.instance_name_taken("   "))


@unittest.skipIf(main is None, "manager runtime dependencies are unavailable")
class RenameApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_renaming_onto_another_lines_name_is_refused(self):
        from fastapi import HTTPException
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            cfg_temp.add("2", "310-240-1234")
            with patch.object(main.asyncio, "to_thread", return_value=False):
                with self.assertRaises(HTTPException) as refused:
                    await main.api_instance_upsert({"id": "2", "name": "giff"})
                self.assertEqual(refused.exception.status_code, 409)

                # Saving a line under its own name, and a free name, both still work.
                kept = await main.api_instance_upsert({"id": "2", "name": "310-240-1234"})
                self.assertEqual(kept["name"], "310-240-1234")
                renamed = await main.api_instance_upsert({"id": "2", "name": "Home"})
                self.assertEqual(renamed["name"], "Home")

    async def test_an_edit_that_carries_no_name_is_unaffected(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            with patch.object(main.asyncio, "to_thread", return_value=False):
                saved = await main.api_instance_upsert({"id": "1", "proxy_country": "gb"})
        self.assertEqual(saved["name"], "Giff")


if __name__ == "__main__":
    unittest.main()

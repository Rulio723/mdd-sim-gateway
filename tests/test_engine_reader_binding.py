import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _iccid_apdu_bytes(iccid):
    """Encode an ICCID the way EF.ICCID carries it: BCD with swapped nibbles, 'f'-padded."""
    padded = iccid if len(iccid) % 2 == 0 else iccid + "f"
    swapped = "".join(x + y for x, y in zip(padded[1::2], padded[0::2]))
    return list(bytes.fromhex(swapped))


class _Connection:
    def __init__(self, name, openable=True, iccid=None):
        self.name = name
        self.openable = openable
        self.connected = False
        self.disconnected = False
        # None models a card that will not answer EF.ICCID at all — a fault, not a swap.
        self.iccid = iccid

    def connect(self):
        if not self.openable:
            raise RuntimeError("card unavailable")
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    def transmit(self, apdu):
        if self.iccid is None:
            raise RuntimeError("card does not answer")
        if str(apdu).lower().startswith("00b0"):
            return _iccid_apdu_bytes(self.iccid), 0x90, 0x00
        return [], 0x90, 0x00


class _Reader:
    def __init__(self, name, openable=True, iccid=None):
        self.name = name
        self.openable = openable
        self.iccid = iccid

    def __str__(self):
        return self.name

    def createConnection(self):
        return _Connection(self.name, self.openable, self.iccid)


def _load_engine_module(filename, module_name):
    """Load a standalone engine script with tiny PC/SC/AMI stubs for selector tests."""
    smartcard = types.ModuleType("smartcard")
    system = types.ModuleType("smartcard.System")
    system.readers = lambda: []
    util = types.ModuleType("smartcard.util")
    util.toBytes = lambda value: value
    util.toHexString = lambda value: ""
    exceptions = types.ModuleType("smartcard.Exceptions")
    exceptions.NoCardException = type("NoCardException", (Exception,), {})
    exceptions.CardConnectionException = type("CardConnectionException", (Exception,), {})
    scard = types.ModuleType("smartcard.scard")
    scard.SCardBeginTransaction = lambda *_: None
    scard.SCardEndTransaction = lambda *_: None
    scard.SCARD_LEAVE_CARD = 0
    panoramisk = types.ModuleType("panoramisk")
    panoramisk.Manager = object
    modules = {
        "smartcard": smartcard,
        "smartcard.System": system,
        "smartcard.util": util,
        "smartcard.Exceptions": exceptions,
        "smartcard.scard": scard,
        "panoramisk": panoramisk,
    }
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "engine" / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class EngineReaderBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pin_keeper = _load_engine_module("pin_keeper.py", "test_pin_keeper")
        cls.ami_usim = _load_engine_module("ami_usim.py", "test_ami_usim")

    def test_pin_keeper_resolves_exact_reader_name_instead_of_index_zero(self):
        first = _Reader("VoWiFi Modem first 00 00", openable=False)
        target = _Reader("VoWiFi Modem second 00 00")
        with patch.object(self.pin_keeper, "readers", return_value=[first, target]), \
                patch.object(self.pin_keeper, "index_for_port", return_value=None), \
                patch.dict(self.pin_keeper.os.environ, {"USIM_READER_PORT": ""}):
            reader, connection, _iccid = self.pin_keeper.find_reader(str(target))
        self.assertIs(reader, target)
        self.assertTrue(connection.connected)

    def test_ami_usim_resolves_exact_reader_name_instead_of_index_zero(self):
        first = _Reader("VoWiFi Modem first 00 02", openable=False)
        target = _Reader("VoWiFi Modem second 00 02")
        with patch.object(self.ami_usim, "readers", return_value=[first, target]), \
                patch.object(self.ami_usim, "index_for_port", return_value=None), \
                patch.dict(self.ami_usim.os.environ, {"USIM_READER_PORT": ""}):
            connection = self.ami_usim.open_usim(str(target))
        self.assertEqual(connection.name, str(target))
        self.assertTrue(connection.connected)

    def test_ami_usim_uses_the_only_reader_when_the_stored_index_is_out_of_range(self):
        """A one-reader host has exactly one card this line can mean. Refusing an index past
        the end reported USIM = NO_CARD while the SIM sat readable in the only reader present
        (issue #8: a line rendered with the old fixed ami_reader of 2)."""
        only = _Reader("Alcor Link AK9563 00 00")
        with patch.object(self.ami_usim, "readers", return_value=[only]), \
                patch.object(self.ami_usim, "index_for_port", return_value=None), \
                patch.dict(self.ami_usim.os.environ, {"USIM_READER_PORT": ""}):
            connection = self.ami_usim.open_usim("2")
        self.assertEqual(connection.name, str(only))
        self.assertTrue(connection.connected)

    def test_ami_usim_still_refuses_an_out_of_range_index_when_readers_are_ambiguous(self):
        readers_list = [_Reader("reader one"), _Reader("reader two")]
        with patch.object(self.ami_usim, "readers", return_value=readers_list), \
                patch.object(self.ami_usim, "index_for_port", return_value=None), \
                patch.dict(self.ami_usim.os.environ, {"USIM_READER_PORT": ""}):
            self.assertIsNone(self.ami_usim.open_usim("5"))

    def test_ami_usim_uses_the_only_reader_for_an_imsi_binding(self):
        """IMSI cannot be read before the PIN is verified, so scanning for it on the single
        card present would only burn that card's PIN tries."""
        only = _Reader("Alcor Link AK9563 00 00")
        with patch.object(self.ami_usim, "readers", return_value=[only]), \
                patch.object(self.ami_usim, "index_for_port", return_value=None), \
                patch.dict(self.ami_usim.os.environ, {"USIM_READER_PORT": ""}):
            connection = self.ami_usim.open_usim("imsi:234100000000000")
        self.assertEqual(connection.name, str(only))
        self.assertTrue(connection.connected)

    def test_ami_usim_honours_the_usb_port_binding_on_a_single_reader_host(self):
        only = _Reader("Alcor Link AK9563 00 00")
        with patch.object(self.ami_usim, "readers", return_value=[only]), \
                patch.object(self.ami_usim, "index_for_port", return_value=0), \
                patch.dict(self.ami_usim.os.environ, {"USIM_READER_PORT": "1-1.4.2"}):
            connection = self.ami_usim.open_usim("2")
        self.assertEqual(connection.name, str(only))
        self.assertTrue(connection.connected)

    def test_unknown_exact_reader_name_fails_closed(self):
        available = _Reader("VoWiFi Modem first 00 00")
        with patch.object(self.pin_keeper, "readers", return_value=[available]), \
                patch.dict(self.pin_keeper.os.environ, {"USIM_READER_PORT": ""}):
            reader, connection, _iccid = self.pin_keeper.find_reader("missing reader")
        self.assertIsNone(reader)
        self.assertIsNone(connection)


class UsimSelectFailClosedTests(unittest.TestCase):
    """EF.DIR is the only proof of which application is the USIM. When the scan yields no AID,
    the engine selectors must fail closed (origin/develop behavior), NOT blindly SELECT the
    standard 3GPP USIM AID: the maintainer requires raw card evidence before that fallback, and
    live node2 shows EF.DIR is readable, so the fallback is unproven. An AID that IS present
    still selects normally."""

    @classmethod
    def setUpClass(cls):
        cls.pin_keeper = _load_engine_module("pin_keeper.py", "select_pin_keeper")
        cls.ami_usim = _load_engine_module("ami_usim.py", "select_ami_usim")

    class Connection:
        """SELECT EF.DIR answers 9000 with an empty FCP, so the real _usim_aid_from_dir scan
        finds no application and returns None. Records every APDU; a direct ADF.USIM SELECT
        (00A40404 ...) appears only if the code blindly falls back to the standard USIM AID."""

        def __init__(self, select_status=0x90):
            self.select_status = select_status
            self.commands = []

        def transmit(self, command):
            self.commands.append(command)
            if str(command).lower().startswith("00a40404"):
                return [], self.select_status, 0x00
            return [], 0x90, 0x00

    def _selected_aid(self, connection):
        return any(str(c).lower().startswith("00a40404") for c in connection.commands)

    def test_pin_keeper_fails_closed_when_ef_dir_names_no_aid(self):
        connection = self.Connection()

        self.assertFalse(self.pin_keeper.select_adf_usim(connection))
        self.assertFalse(
            self._selected_aid(connection),
            "no AID from EF.DIR must not blindly SELECT the standard USIM AID")

    def test_ami_usim_fails_closed_when_ef_dir_names_no_aid(self):
        connection = self.Connection()

        self.assertFalse(self.ami_usim.select_adf_usim(connection))
        self.assertFalse(
            self._selected_aid(connection),
            "no AID from EF.DIR must not blindly SELECT the standard USIM AID")

    def test_pin_keeper_selects_an_available_aid(self):
        connection = self.Connection(select_status=0x90)

        with patch.object(self.pin_keeper, "_usim_aid_from_dir",
                          return_value=(7, "A0000000871002")):
            self.assertTrue(self.pin_keeper.select_adf_usim(connection))
        self.assertTrue(self._selected_aid(connection))

    def test_ami_usim_selects_an_available_aid(self):
        connection = self.Connection(select_status=0x90)

        with patch.object(self.ami_usim, "_usim_aid_from_dir",
                          return_value=(7, "A0000000871002")):
            self.assertTrue(self.ami_usim.select_adf_usim(connection))
        self.assertTrue(self._selected_aid(connection))

    def test_pin_keeper_rejects_a_failed_direct_select_of_a_present_aid(self):
        connection = self.Connection(select_status=0x6A)

        with patch.object(self.pin_keeper, "_usim_aid_from_dir",
                          return_value=(7, "A0000000871002")):
            self.assertFalse(self.pin_keeper.select_adf_usim(connection))

    def test_ami_usim_rejects_a_failed_direct_select_of_a_present_aid(self):
        connection = self.Connection(select_status=0x6A)

        with patch.object(self.ami_usim, "_usim_aid_from_dir",
                          return_value=(7, "A0000000871002")):
            self.assertFalse(self.ami_usim.select_adf_usim(connection))


class AmiAuthenticateCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ami_usim = _load_engine_module("ami_usim.py", "auth_ami_usim")

    class Connection:
        def disconnect(self):
            return None

        def transmit(self, command):
            text = str(command).lower()
            if text.startswith("00880081"):
                data = [0xDB, 0x04, 1, 2, 3, 4, 0x10]
                data += list(range(16))
                data += [0x10] + list(range(16, 32))
                return data, 0x90, 0x00
            return [], 0x90, 0x00

    def test_aka_accepts_inline_data_with_9000_status(self):
        connection = self.Connection()
        written = {}
        with patch.object(self.ami_usim, "open_usim", return_value=connection), \
                patch.object(self.ami_usim, "select_adf_usim", return_value=True), \
                patch.object(self.ami_usim, "verify_pin", return_value=True), \
                patch.object(self.ami_usim, "toHexString",
                             side_effect=lambda value: " ".join(
                                 f"{byte:02X}" for byte in value)), \
                patch.object(self.ami_usim, "write_status",
                             side_effect=lambda **value: written.update(value)):
            res, ck, ik, auts = self.ami_usim.read_res_ck_ik(
                "reader", "00" * 16, "11" * 16)

        self.assertEqual(res, "01020304")
        self.assertEqual(len(ck), 32)
        self.assertEqual(len(ik), 32)
        self.assertIsNone(auts)
        self.assertEqual(written.get("state"), "AUTH_OK")


class ForeignCardRefusalTests(unittest.TestCase):
    """A binding names a SLOT; only EF.ICCID says which CARD is in it.

    Two identical serial-less modems that swap USB paths — or an engine image predating a
    binding fix — leave a line opening its sibling's SIM. The single symptom is the carrier's
    AKA challenge failing with SW=9862, which is indistinguishable from a carrier rejecting the
    subscriber, so the fault gets attributed upstream and the line rebuilds forever.
    """

    OURS = "8900000000000000022"
    THEIRS = "8900000000000000031"

    @classmethod
    def setUpClass(cls):
        cls.pin_keeper = _load_engine_module("pin_keeper.py", "test_pin_keeper_cards")
        cls.ami_usim = _load_engine_module("ami_usim.py", "test_ami_usim_cards")

    def _find(self, spec, readers_list, iccid):
        env = {"USIM_READER_PORT": "", "USIM_ICCID": iccid}
        with patch.object(self.pin_keeper, "readers", return_value=readers_list), \
                patch.object(self.pin_keeper, "index_for_port", return_value=None), \
                patch.dict(self.pin_keeper.os.environ, env):
            return self.pin_keeper.find_reader(spec)

    def test_named_reader_holding_another_lines_sim_is_refused(self):
        target = _Reader("VoWiFi Modem second 00 00", iccid=self.THEIRS)
        with self.assertRaises(self.pin_keeper.WrongCard) as caught:
            self._find(str(target), [target], self.OURS)
        self.assertEqual(caught.exception.expected, self.OURS)
        self.assertEqual(caught.exception.actual, self.THEIRS)

    def test_the_search_reports_the_card_it_read_so_nobody_asks_the_card_twice(self):
        """ensure_pin records which CARD answered, and takes that from here.

        Reading EF.ICCID again in the caller would repeat an exchange this search already
        performed, and would do it outside the PC/SC transaction the rest of ensure_pin's card
        I/O is wrapped in. A path that did not read reports None, so a name that drifted is
        still judged by the name when no card identified itself."""
        ours = _Reader("VoWiFi Modem second 00 00", iccid=self.OURS)
        self.assertEqual(self._find(str(ours), [ours], self.OURS)[2], self.OURS)
        self.assertEqual(self._find("0", [ours], self.OURS)[2], self.OURS)
        self.assertEqual(self._find("iccid:" + self.OURS, [ours], "")[2], self.OURS)
        # Nothing was read: no configured ICCID, and an unreadable card.
        self.assertIsNone(self._find(str(ours), [ours], "")[2])
        mute = _Reader("VoWiFi Modem second 00 00", iccid=None)
        self.assertIsNone(self._find(str(mute), [mute], self.OURS)[2])

    def test_named_reader_holding_our_own_sim_is_accepted(self):
        target = _Reader("VoWiFi Modem second 00 00", iccid=self.OURS)
        reader, connection, _iccid = self._find(str(target), [target], self.OURS)
        self.assertIs(reader, target)
        self.assertTrue(connection.connected)

    def test_an_unreadable_iccid_is_not_treated_as_a_swapped_card(self):
        # A card that will not answer EF.ICCID is a card fault. Convicting on it would strand
        # a line whose binding is perfectly correct, so the read has to succeed to accuse.
        target = _Reader("VoWiFi Modem second 00 00", iccid=None)
        reader, connection, _iccid = self._find(str(target), [target], self.OURS)
        self.assertIs(reader, target)

    def test_no_configured_iccid_leaves_the_binding_untouched(self):
        target = _Reader("VoWiFi Modem second 00 00", iccid=self.THEIRS)
        reader, _conn, _iccid = self._find(str(target), [target], "")
        self.assertIs(reader, target)

    def test_index_binding_is_checked_too(self):
        # The legacy numeric fallback is exactly how a stale engine image lands on index 0.
        ours = _Reader("VoWiFi Modem second 00 00", iccid=self.OURS)
        theirs = _Reader("VoWiFi Modem first 00 00", iccid=self.THEIRS)
        with self.assertRaises(self.pin_keeper.WrongCard):
            self._find("0", [theirs, ours], self.OURS)

    def test_usb_port_binding_is_checked_too(self):
        theirs = _Reader("VoWiFi Modem first 00 00", iccid=self.THEIRS)
        env = {"USIM_READER_PORT": "1-1", "USIM_ICCID": self.OURS}
        with patch.object(self.pin_keeper, "readers", return_value=[theirs]), \
                patch.object(self.pin_keeper, "index_for_port", return_value=0), \
                patch.dict(self.pin_keeper.os.environ, env):
            with self.assertRaises(self.pin_keeper.WrongCard):
                self.pin_keeper.find_reader("whatever")

    def test_imsi_search_refuses_to_guess_when_every_card_identified_itself(self):
        # Falling back to "the first card-bearing reader" is the same silent mis-bind by
        # another route: every card said who it was, and none of them was ours.
        one = _Reader("Reader A", iccid=self.THEIRS)
        two = _Reader("Reader B", iccid="8900000000000000040")
        with self.assertRaises(self.pin_keeper.WrongCard):
            self._find("imsi:310260123456789", [one, two], self.OURS)

    def test_ensure_pin_reports_wrong_card_with_both_iccids(self):
        target = _Reader("VoWiFi Modem second 00 00", iccid=self.THEIRS)
        written = {}
        env = {"USIM_READER_PORT": "", "USIM_ICCID": self.OURS}
        with patch.object(self.pin_keeper, "readers", return_value=[target]), \
                patch.object(self.pin_keeper, "index_for_port", return_value=None), \
                patch.object(self.pin_keeper, "write_status",
                             side_effect=lambda *a, **k: written.update(
                                 {"state": a[0] if a else k.get("state"), **k})), \
                patch.dict(self.pin_keeper.os.environ, env):
            self.assertIsNone(self.pin_keeper.ensure_pin(str(target), ""))
        self.assertEqual(written.get("state"), "WRONG_CARD")
        self.assertIn(self.THEIRS, written.get("detail", ""))
        self.assertIn(self.OURS, written.get("detail", ""))

    def test_pin_status_records_which_card_answered(self):
        # The manager needs the ICCID to tell a reader name that merely drifted (USB-port
        # binding opening a renamed slot) from one pointing at the wrong card. Without this
        # field it can only compare names, and a correctly bound line gets held forever.
        with tempfile.TemporaryDirectory() as run_dir:
            with patch.object(self.pin_keeper, "RUNDIR", run_dir), \
                    patch.object(self.pin_keeper, "STATUS_PATH",
                                 os.path.join(run_dir, "pin_status.json")):
                self.pin_keeper.write_status("PIN_DISABLED", tries_left=3,
                                             reader="VoWiFi Modem x 00 00", iccid=self.OURS)
                written = json.loads(
                    Path(run_dir, "pin_status.json").read_text(encoding="utf-8"))
        self.assertEqual(written["iccid"], self.OURS)
        self.assertEqual(written["state"], "PIN_DISABLED")

    def test_ami_usim_spots_the_foreign_card_on_the_ims_path(self):
        connection = _Connection("Reader A", iccid=self.THEIRS)
        with patch.dict(self.ami_usim.os.environ, {"USIM_ICCID": self.OURS}):
            self.assertEqual(self.ami_usim.foreign_iccid(connection), self.THEIRS)

    def test_ami_usim_accepts_its_own_card(self):
        connection = _Connection("Reader A", iccid=self.OURS)
        with patch.dict(self.ami_usim.os.environ, {"USIM_ICCID": self.OURS}):
            self.assertIsNone(self.ami_usim.foreign_iccid(connection))

    def test_ami_usim_does_not_convict_an_unreadable_card(self):
        connection = _Connection("Reader A", iccid=None)
        with patch.dict(self.ami_usim.os.environ, {"USIM_ICCID": self.OURS}):
            self.assertIsNone(self.ami_usim.foreign_iccid(connection))


class ReselectAdfAfterImsiReadTests(unittest.TestCase):
    """make_reselect_adf re-selects ADF.USIM after the imsi: scan reads EF_IMSI (that read
    leaves EF_IMSI current, not ADF.USIM). It must use the same inline SELECT ADF.USIM by AID
    the sibling selectors (make_connection_index, select_adf_usim) use; a call to an undefined
    _select_usim_aid once raised NameError the moment an imsi:-bound reader matched its card, so
    the reselect must stay a plain inline SELECT. When EF.DIR names no AID it fails closed
    rather than blindly selecting the standard USIM AID."""

    @classmethod
    def setUpClass(cls):
        cls.ami_usim = _load_engine_module("ami_usim.py", "reselect_ami_usim")

    class Connection:
        """Records every APDU. Answers the EF_IMSI READ BINARY (00B0000009) with encoded IMSI
        bytes so the imsi: scan finds its target; every other command (including SELECT EF.DIR)
        returns a bare 9000 with no FCP, so the real _usim_aid_from_dir scan yields no AID."""

        def __init__(self, imsi_bytes=None):
            self.commands = []
            self.imsi_bytes = imsi_bytes

        def transmit(self, command):
            self.commands.append(command)
            if str(command).lower() == "00b0000009" and self.imsi_bytes is not None:
                return list(self.imsi_bytes), 0x90, 0x00
            return [], 0x90, 0x00

    def _selected_adf_usim(self, connection):
        return any(str(c).lower().startswith("00a40404") for c in connection.commands)

    def test_make_reselect_adf_selects_an_available_adf_usim_by_aid(self):
        connection = self.Connection()

        with patch.object(self.ami_usim, "_usim_aid_from_dir",
                          return_value=(7, "A0000000871002")):
            self.ami_usim.make_reselect_adf(connection)

        self.assertTrue(
            self._selected_adf_usim(connection),
            "make_reselect_adf must SELECT ADF.USIM by AID (00A40404 ...) when EF.DIR names one")

    def test_make_reselect_adf_fails_closed_when_ef_dir_names_no_aid(self):
        connection = self.Connection()

        self.ami_usim.make_reselect_adf(connection)

        self.assertFalse(
            self._selected_adf_usim(connection),
            "no AID from EF.DIR must not blindly SELECT the standard USIM AID")

    def test_imsi_binding_reselects_adf_usim_after_matching_the_target(self):
        target = "234100000000000"
        # EF_IMSI as the card returns it: length byte 08, then nibble-swapped BCD carrying a
        # parity nibble ahead of the 15 IMSI digits. dec_imsi(...) decodes this back to target.
        imsi_bytes = bytes.fromhex("082943010000000000")
        connection = self.Connection(imsi_bytes=imsi_bytes)
        reader = _Reader("Alcor Link AK9563 00 00")
        with patch.object(self.ami_usim, "readers", return_value=[reader]), \
                patch.object(self.ami_usim, "make_connection_index",
                             return_value=connection), \
                patch.object(self.ami_usim, "_usim_aid_from_dir",
                             return_value=(7, "A0000000871002")):
            result = self.ami_usim.make_connection_name("imsi:" + target)

        self.assertIs(result, connection)
        self.assertTrue(
            self._selected_adf_usim(connection),
            "the imsi: match must reselect ADF.USIM before handing back the connection")


if __name__ == "__main__":
    unittest.main()

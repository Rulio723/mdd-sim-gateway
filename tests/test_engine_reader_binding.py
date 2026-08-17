import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class _Connection:
    def __init__(self, name, openable=True):
        self.name = name
        self.openable = openable
        self.connected = False

    def connect(self):
        if not self.openable:
            raise RuntimeError("card unavailable")
        self.connected = True


class _Reader:
    def __init__(self, name, openable=True):
        self.name = name
        self.openable = openable

    def __str__(self):
        return self.name

    def createConnection(self):
        return _Connection(self.name, self.openable)


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
            reader, connection = self.pin_keeper.find_reader(str(target))
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

    def test_unknown_exact_reader_name_fails_closed(self):
        available = _Reader("VoWiFi Modem first 00 00")
        with patch.object(self.pin_keeper, "readers", return_value=[available]), \
                patch.dict(self.pin_keeper.os.environ, {"USIM_READER_PORT": ""}):
            reader, connection = self.pin_keeper.find_reader("missing reader")
        self.assertIsNone(reader)
        self.assertIsNone(connection)


if __name__ == "__main__":
    unittest.main()

"""Bare EAP-Request/Identity handling (Lebara UK 234-87 opens EAP with it, issue #43)."""
import ast
import struct
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parent.parent / "engine" / "swu_ike.py"
WANTED = {"build_eap_identity_response"}


def _load():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted_constants = {"EAP_REQUEST", "EAP_RESPONSE", "EAP_IDENTITY", "EAP_AKA"}
    assignments = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in wanted_constants
                for target in node.targets)
    ]
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in WANTED
    ]
    names = {"struct": struct}
    exec(compile(ast.Module(body=assignments + functions, type_ignores=[]), str(SOURCE), "exec"), names)  # noqa: S102
    return {name: names[name] for name in WANTED}, names, tree


FUNCTIONS, CONSTANTS, TREE = _load()
NAI = "0234870000000000@nai.epc.mnc087.mcc234.3gppnetwork.org"


class EapIdentityResponseTests(unittest.TestCase):
    def test_response_layout_matches_rfc3748(self):
        payload = FUNCTIONS["build_eap_identity_response"](7, NAI)
        self.assertEqual(payload[0], CONSTANTS["EAP_RESPONSE"])
        self.assertEqual(payload[1], 7)
        self.assertEqual(struct.unpack("!H", payload[2:4])[0], len(payload))
        self.assertEqual(payload[4], CONSTANTS["EAP_IDENTITY"])

    def test_type_data_is_the_bare_nai_without_attribute_framing(self):
        payload = FUNCTIONS["build_eap_identity_response"](7, NAI)
        self.assertEqual(payload[5:], NAI.encode())
        self.assertEqual(len(payload), 5 + len(NAI))

    def test_identifier_is_echoed_across_its_full_range(self):
        for identifier in (0, 1, 255):
            payload = FUNCTIONS["build_eap_identity_response"](identifier, NAI)
            self.assertEqual(payload[1], identifier)


class State2WiringTests(unittest.TestCase):
    def test_state_2_answers_a_bare_identity_request(self):
        state_2 = next(
            node for cls in TREE.body if isinstance(cls, ast.ClassDef)
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "state_2"
        )
        calls = {
            call.func.id
            for call in ast.walk(state_2)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        self.assertIn("build_eap_identity_response", calls)


if __name__ == "__main__":
    unittest.main()

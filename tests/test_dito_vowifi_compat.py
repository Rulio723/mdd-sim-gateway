"""Carrier-scoped DITO IKE and EAP-AKA compatibility rules."""
import ast
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parent.parent / "engine" / "swu_ike.py"
WANTED = {"ike_proposals_for_plmn", "requests_permanent_eap_identity"}


def _load_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted_constants = {
        "IKE", "ENCR", "PRF", "INTEG", "D_H", "ENCR_AES_CBC", "KEY_LENGTH", "TV",
        "PRF_HMAC_SHA1", "PRF_HMAC_SHA2_256", "AUTH_HMAC_SHA1_96",
        "AUTH_HMAC_SHA2_256_128", "MODP_1024_bit", "MODP_2048_bit",
        "AT_PERMANENT_ID_REQ", "AT_ANY_ID_REQ", "AT_FULLAUTH_ID_REQ", "AT_IDENTITY",
    }
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
    names = {}
    exec(compile(ast.Module(body=assignments + functions, type_ignores=[]), str(SOURCE), "exec"), names)  # noqa: S102
    return {name: names[name] for name in WANTED}, names


FUNCTIONS, CONSTANTS = _load_functions()


class IkeProposalTests(unittest.TestCase):
    def test_dito_uses_the_suite_observed_from_its_epdg(self):
        proposals = FUNCTIONS["ike_proposals_for_plmn"]("515", "66")
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertIn([CONSTANTS["ENCR"], CONSTANTS["ENCR_AES_CBC"],
                       [CONSTANTS["KEY_LENGTH"], 128]], proposal)
        self.assertIn([CONSTANTS["PRF"], CONSTANTS["PRF_HMAC_SHA1"]], proposal)
        self.assertIn([CONSTANTS["INTEG"], CONSTANTS["AUTH_HMAC_SHA1_96"]], proposal)
        self.assertIn([CONSTANTS["D_H"], CONSTANTS["MODP_1024_bit"]], proposal)

    def test_non_dito_carriers_keep_the_existing_strong_proposals(self):
        proposals = FUNCTIONS["ike_proposals_for_plmn"]("262", "02")
        expected = [
            [[CONSTANTS["IKE"], 0], [CONSTANTS["ENCR"], CONSTANTS["ENCR_AES_CBC"],
              [CONSTANTS["KEY_LENGTH"], 256]],
             [CONSTANTS["PRF"], CONSTANTS["PRF_HMAC_SHA2_256"]],
             [CONSTANTS["INTEG"], CONSTANTS["AUTH_HMAC_SHA2_256_128"]],
             [CONSTANTS["D_H"], CONSTANTS["MODP_2048_bit"]]],
            [[CONSTANTS["IKE"], 0], [CONSTANTS["ENCR"], CONSTANTS["ENCR_AES_CBC"],
              [CONSTANTS["KEY_LENGTH"], 128]],
             [CONSTANTS["PRF"], CONSTANTS["PRF_HMAC_SHA2_256"]],
             [CONSTANTS["INTEG"], CONSTANTS["AUTH_HMAC_SHA2_256_128"]],
             [CONSTANTS["D_H"], CONSTANTS["MODP_2048_bit"]]],
            [[CONSTANTS["IKE"], 0], [CONSTANTS["ENCR"], CONSTANTS["ENCR_AES_CBC"],
              [CONSTANTS["KEY_LENGTH"], 256]],
             [CONSTANTS["PRF"], CONSTANTS["PRF_HMAC_SHA1"]],
             [CONSTANTS["INTEG"], CONSTANTS["AUTH_HMAC_SHA1_96"]],
             [CONSTANTS["D_H"], CONSTANTS["MODP_2048_bit"]]],
            [[CONSTANTS["IKE"], 0], [CONSTANTS["ENCR"], CONSTANTS["ENCR_AES_CBC"],
              [CONSTANTS["KEY_LENGTH"], 128]],
             [CONSTANTS["PRF"], CONSTANTS["PRF_HMAC_SHA1"]],
             [CONSTANTS["INTEG"], CONSTANTS["AUTH_HMAC_SHA1_96"]],
             [CONSTANTS["D_H"], CONSTANTS["MODP_2048_bit"]]],
        ]
        self.assertEqual(proposals, expected)
        self.assertNotEqual(proposals, FUNCTIONS["ike_proposals_for_plmn"]("515", "66"))

    def test_mnc_padding_accepts_the_two_digit_dito_mnc(self):
        self.assertEqual(
            FUNCTIONS["ike_proposals_for_plmn"]("515", "66"),
            FUNCTIONS["ike_proposals_for_plmn"]("515", "066"),
        )


class EapIdentityTests(unittest.TestCase):
    def test_all_permanent_identity_requests_are_supported(self):
        check = FUNCTIONS["requests_permanent_eap_identity"]
        for attribute in (10, 13, 17, 14):
            with self.subTest(attribute=attribute):
                self.assertTrue(check([(attribute, b"")]))

    def test_unrelated_or_empty_attributes_are_not_identity_requests(self):
        check = FUNCTIONS["requests_permanent_eap_identity"]
        self.assertFalse(check([]))
        self.assertFalse(check([(1, b"")]))


if __name__ == "__main__":
    unittest.main()

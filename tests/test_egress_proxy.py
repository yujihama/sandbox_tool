from __future__ import annotations

import unittest

from sandbox_tool.egress_proxy import (
    create_egress_token,
    domain_matches,
    is_global_address,
    verify_egress_token,
)


class EgressProxyTests(unittest.TestCase):
    def test_signed_token_round_trips_allowed_domains(self) -> None:
        token = create_egress_token(
            allowed_domains=["example.com", "*.example.org"],
            secret="unit-secret",
            purpose="test",
            ttl_seconds=60,
        )

        self.assertEqual(
            verify_egress_token(token, "unit-secret"),
            ["example.com", "*.example.org"],
        )

    def test_signed_token_rejects_wrong_secret(self) -> None:
        token = create_egress_token(
            allowed_domains=["example.com"],
            secret="unit-secret",
            purpose="test",
            ttl_seconds=60,
        )

        with self.assertRaises(PermissionError):
            verify_egress_token(token, "wrong-secret")

    def test_domain_matching_allows_subdomains_but_not_siblings(self) -> None:
        self.assertTrue(domain_matches("www.example.com", ["example.com"]))
        self.assertTrue(domain_matches("a.example.org", ["*.example.org"]))
        self.assertFalse(domain_matches("example.net", ["example.com"]))

    def test_private_addresses_are_not_global(self) -> None:
        self.assertFalse(is_global_address("127.0.0.1"))
        self.assertFalse(is_global_address("10.1.2.3"))
        self.assertFalse(is_global_address("::1"))
        self.assertTrue(is_global_address("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()

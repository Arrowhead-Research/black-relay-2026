"""Credential-free safety assertions for protected OpenTofu adoption."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOFU = ROOT / "tofu"
MAIN = (TOFU / "main.tf").read_text(encoding="utf-8")
VERSIONS = (TOFU / "versions.tf").read_text(encoding="utf-8")
VARIABLES = (TOFU / "variables.tf").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs/runbooks/phase3-opentofu-adoption.md").read_text(
    encoding="utf-8"
)


def hcl_block(source, kind, resource_type, name):
    """Return a named top-level HCL block using brace balancing."""
    pattern = re.compile(
        rf'{re.escape(kind)}\s+"{re.escape(resource_type)}"\s+'
        rf'"{re.escape(name)}"\s*\{{'
    )
    match = pattern.search(source)
    if not match:
        raise AssertionError(f"missing {kind} {resource_type}.{name}")

    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    raise AssertionError(f"unterminated {kind} {resource_type}.{name}")


class PhaseThreeSafetyTests(unittest.TestCase):
    def test_critical_resources_cannot_be_destroyed_by_ordinary_apply(self):
        protected = (
            ("hcloud_server", "production"),
            ("hcloud_primary_ip", "production"),
            ("hcloud_firewall", "production"),
            ("hcloud_firewall_attachment", "production"),
            ("hcloud_storage_box", "backups"),
        )
        for resource_type, name in protected:
            with self.subTest(resource=f"{resource_type}.{name}"):
                block = hcl_block(MAIN, "resource", resource_type, name)
                self.assertIn("prevent_destroy = true", block)

    def test_provider_side_protections_and_independent_ipv4_are_enabled(self):
        server = hcl_block(MAIN, "resource", "hcloud_server", "production")
        primary_ip = hcl_block(
            MAIN, "resource", "hcloud_primary_ip", "production"
        )
        storage_box = hcl_block(MAIN, "resource", "hcloud_storage_box", "backups")

        self.assertIn("delete_protection  = true", server)
        self.assertIn("rebuild_protection = true", server)
        self.assertIn('server_type = "cpx32"', server)
        self.assertIn('location    = "hel1"', server)
        self.assertIn("backups            = var.server_backups_enabled", server)
        self.assertNotRegex(server, r"(?m)^\s*public_net\s*\{")
        self.assertIn('contains(["", "<nil>"], self.ipv6_address)', server)
        self.assertIn('contains(["", "<nil>"], self.ipv6_network)', server)

        self.assertRegex(primary_ip, r"auto_delete\s*=\s*false")
        self.assertIn("assignee_id   = tonumber(hcloud_server.production.id)", primary_ip)
        self.assertIn('assignee_type = "server"', primary_ip)
        self.assertIn("delete_protection = true", primary_ip)
        self.assertIn("expected_primary_ipv4_id", primary_ip)
        self.assertIn("primary_ipv4_address", primary_ip)
        self.assertIn("delete_protection = true", storage_box)
        self.assertIn('storage_box_type = "bx11"', storage_box)

    def test_imported_server_identity_is_checked(self):
        server = hcl_block(MAIN, "resource", "hcloud_server", "production")
        self.assertIn("expected_server_id", server)
        self.assertIn('self.server_type == "cpx32"', server)
        self.assertIn('self.location == "hel1"', server)
        primary_ip = hcl_block(
            MAIN, "resource", "hcloud_primary_ip", "production"
        )
        self.assertIn("self.ip_address == var.primary_ipv4_address", primary_ip)
        self.assertIn("self.assignee_id == tonumber(hcloud_server.production.id)", primary_ip)

    def test_firewall_is_ipv4_default_deny_ingress(self):
        firewall = hcl_block(MAIN, "resource", "hcloud_firewall", "production")
        for port in ("22", "80", "443"):
            self.assertEqual(firewall.count(f'port        = "{port}"'), 1)
        self.assertEqual(firewall.count('protocol    = "icmp"'), 1)
        self.assertNotIn("::/0", firewall)
        self.assertNotRegex(firewall, r'direction\s*=\s*"out"')

    def test_cloudflare_records_are_dns_only(self):
        dns = hcl_block(MAIN, "resource", "cloudflare_dns_record", "public")
        self.assertIn('type    = "A"', dns)
        self.assertIn("content = var.primary_ipv4_address", dns)
        self.assertIn("ttl     = 300", dns)
        self.assertIn("proxied = false", dns)
        self.assertIn("cloudflare_zone_name", dns)

    def test_state_and_plans_use_enforced_client_side_encryption(self):
        self.assertIn('backend "s3" {}', VERSIONS)
        self.assertIn('key_provider "pbkdf2" "state"', VERSIONS)
        self.assertIn('method "aes_gcm" "state"', VERSIONS)
        self.assertEqual(VERSIONS.count("enforced = true"), 2)
        self.assertIn('variable "state_passphrase"', VARIABLES)
        self.assertNotRegex(
            VARIABLES,
            r'variable\s+"state_passphrase"\s*\{[^}]*default\s*=',
        )

    def test_storage_box_location_requires_provider_codes(self):
        self.assertIn(
            'contains(["fsn1", "nbg1", "hel1"], var.storage_box_location)',
            VARIABLES,
        )
        example = (TOFU / "production.tfvars.example").read_text(encoding="utf-8")
        self.assertIn('storage_box_location = "hel1"', example)
        self.assertNotIn('storage_box_location = "Helsinki"', example)

    def test_secret_inputs_are_sensitive_and_have_no_defaults(self):
        for name in ("state_passphrase", "storage_box_password"):
            match = re.search(
                rf'variable\s+"{name}"\s*\{{(.*?)\n\}}',
                VARIABLES,
                re.DOTALL,
            )
            self.assertIsNotNone(match)
            block = match.group(1)
            self.assertIn("sensitive   = true", block)
            self.assertNotRegex(block, r"\bdefault\s*=")

    def test_runbook_imports_only_existing_server_and_primary_ip(self):
        import_addresses = set(
            re.findall(r"^\s+(hcloud_[a-z_]+\.[a-z_]+)\s+\\?$", RUNBOOK, re.MULTILINE)
        )
        self.assertEqual(
            import_addresses,
            {"hcloud_primary_ip.production", "hcloud_server.production"},
        )
        self.assertIn("single-writer", RUNBOOK)
        self.assertIn('index("delete")', RUNBOOK)
        self.assertIn("trusted operator workstation only", RUNBOOK.lower())

    def test_backend_example_has_no_credentials_and_disables_lock_claim(self):
        backend = (TOFU / "backend.hcl.example").read_text(encoding="utf-8")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("use_lockfile                 = false", backend)
        self.assertIn("use_path_style               = true", backend)
        self.assertNotIn("AWS_ACCESS_KEY_ID =", backend)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY =", backend)
        self.assertIn("tofu/backend.hcl", gitignore.splitlines())


if __name__ == "__main__":
    unittest.main()

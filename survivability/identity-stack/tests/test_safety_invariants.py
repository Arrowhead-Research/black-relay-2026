"""The invariants whose loss would be a real security or recovery regression.

Everything here guards something that is expensive or impossible to undo: an
ordinary apply destroying an adopted resource, the rebuild script deleting the
server it is meant to preserve, a credential-free check reaching production, or
a secret reaching the repository. Assertions about implementation detail,
documentation wording, and phase status deliberately do not live here.
"""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOFU = ROOT / "tofu"
MAIN = (TOFU / "main.tf").read_text(encoding="utf-8")
VERSIONS = (TOFU / "versions.tf").read_text(encoding="utf-8")
VARIABLES = (TOFU / "variables.tf").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")
SOPS_CONFIG_TEXT = (ROOT / ".sops.yaml").read_text(encoding="utf-8")
SOPS_CONFIG = yaml.safe_load(SOPS_CONFIG_TEXT)
AGE_RECIPIENT = re.compile(r"^age1[0-9a-z]{58}$")
# Split so this file never contains the literal it searches for.
AGE_PRIVATE_KEY = "AGE-SECRET-" + "KEY-"


def sops_creation_rules():
    """Each rule as (compiled path_regex, the age recipients it demands)."""
    rules = []
    for rule in SOPS_CONFIG["creation_rules"]:
        recipients = set()
        for group in rule.get("key_groups") or []:
            recipients.update(group.get("age") or [])
        inline = rule.get("age")
        if inline:
            recipients.update(part.strip() for part in str(inline).split(","))
        rules.append((re.compile(rule["path_regex"]), frozenset(recipients)))
    return rules


def committed_secret_files():
    """Existing files a creation rule claims, with the recipients it demands."""
    rules = sops_creation_rules()
    found = []
    for path in sorted(ROOT.rglob("*.sops.*")):
        relative = path.relative_to(ROOT).as_posix()
        for pattern, recipients in rules:
            if pattern.search(relative):
                found.append((path, relative, recipients))
                break
    return found


def sops_payload(path):
    """Return (value assignments, recipients) for an encrypted file.

    Handles both shapes SOPS emits here: YAML with a `sops:` metadata mapping,
    and dotenv where the same metadata is flattened onto `sops_`-prefixed keys.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".env":
        assignments, recipients = {}, set()
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.startswith("sops_"):
                if key.endswith("_map_recipient"):
                    recipients.add(value.strip())
            else:
                assignments[key] = value
        return assignments, recipients
    document = yaml.safe_load(text) or {}
    metadata = document.pop("sops", {}) or {}
    recipients = {entry["recipient"] for entry in metadata.get("age") or []}
    return document, recipients


def hcl_block(source, kind, resource_type, name):
    """Return a named top-level HCL block using brace balancing.

    Scoping matters: `prevent_destroy` somewhere in main.tf proves nothing, so
    every assertion below is made against the specific resource's own block.
    """
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


PROTECTED_RESOURCES = (
    ("hcloud_server", "production"),
    ("hcloud_primary_ip", "production"),
    ("hcloud_firewall", "production"),
    ("hcloud_firewall_attachment", "production"),
    ("hcloud_storage_box", "backups"),
)


class AdoptedResourceTests(unittest.TestCase):
    def test_protected_resources_survive_an_ordinary_apply(self):
        for resource_type, name in PROTECTED_RESOURCES:
            with self.subTest(resource=f"{resource_type}.{name}"):
                block = hcl_block(MAIN, "resource", resource_type, name)
                self.assertIn("prevent_destroy = true", block)

    def test_provider_side_protection_and_independent_ipv4(self):
        server = hcl_block(MAIN, "resource", "hcloud_server", "production")
        primary_ip = hcl_block(MAIN, "resource", "hcloud_primary_ip", "production")
        storage_box = hcl_block(MAIN, "resource", "hcloud_storage_box", "backups")

        self.assertIn("delete_protection  = true", server)
        self.assertIn("rebuild_protection = true", server)
        # A public_net block makes the provider try to delete the attached
        # protected Primary IP on import.
        self.assertNotRegex(server, r"(?m)^\s*public_net\s*\{")

        # auto_delete=false is what keeps the irreplaceable address alive
        # through a server lifecycle change.
        self.assertRegex(primary_ip, r"auto_delete\s*=\s*false")
        self.assertIn("delete_protection = true", primary_ip)
        self.assertIn("delete_protection = true", storage_box)

    def test_adopted_identity_is_asserted_before_apply(self):
        server = hcl_block(MAIN, "resource", "hcloud_server", "production")
        primary_ip = hcl_block(MAIN, "resource", "hcloud_primary_ip", "production")
        self.assertIn("expected_server_id", server)
        self.assertIn('self.server_type == "cpx32"', server)
        self.assertIn('self.location == "hel1"', server)
        self.assertIn("self.ip_address == var.primary_ipv4_address", primary_ip)

    def test_ingress_is_ipv4_only_and_limited_to_public_service_ports(self):
        firewall = hcl_block(MAIN, "resource", "hcloud_firewall", "production")
        for port in ("22", "80", "443"):
            self.assertEqual(firewall.count(f'port        = "{port}"'), 1)
        self.assertEqual(firewall.count('protocol    = "icmp"'), 1)
        self.assertNotIn("::/0", firewall)

    def test_public_dns_is_unproxied_and_points_at_the_adopted_address(self):
        dns = hcl_block(MAIN, "resource", "cloudflare_dns_record", "public")
        self.assertIn('type    = "A"', dns)
        self.assertIn("content = var.primary_ipv4_address", dns)
        self.assertIn("proxied = false", dns)


class SecretHandlingTests(unittest.TestCase):
    def test_state_and_plans_use_enforced_client_side_encryption(self):
        self.assertIn('key_provider "pbkdf2" "state"', VERSIONS)
        self.assertIn('method "aes_gcm" "state"', VERSIONS)
        self.assertEqual(VERSIONS.count("enforced = true"), 2)

    def test_secret_inputs_are_sensitive_and_have_no_defaults(self):
        for name in (
            "state_passphrase",
            "storage_box_password",
            "storage_box_subaccount_password",
        ):
            with self.subTest(variable=name):
                match = re.search(
                    rf'variable\s+"{name}"\s*\{{(.*?)\n\}}', VARIABLES, re.DOTALL
                )
                self.assertIsNotNone(match, f"missing variable {name}")
                block = match.group(1)
                self.assertIn("sensitive   = true", block)
                self.assertNotRegex(block, r"\bdefault\s*=")

    def test_opentofu_configuration_carries_no_provider_credentials(self):
        config = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(TOFU.glob("*.tf"))
        )
        for name in (
            "HCLOUD_TOKEN",
            "CLOUDFLARE_API_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ):
            self.assertNotIn(name, config)

    def test_tooling_environment_example_lists_names_without_values(self):
        example = (ROOT / "secrets/tooling.env.example").read_text(encoding="utf-8")
        assignments = [
            line for line in example.splitlines()
            if line and not line.startswith("#")
        ]
        self.assertTrue(assignments)
        for line in assignments:
            with self.subTest(line=line):
                self.assertRegex(line, r"^[A-Za-z_][A-Za-z0-9_]*=$")

    def test_committed_secret_examples_hold_no_key_material(self):
        examples = list(ROOT.rglob("*secrets.example.yml")) + [
            ROOT / "secrets/tooling.env.example"
        ]
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example=example.name):
                text = example.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

    def test_committed_sops_files_are_encrypted(self):
        """The one catastrophic mistake: committing a secret in plaintext."""
        files = committed_secret_files()
        self.assertTrue(files, "no encrypted file matched a .sops.yaml rule")
        for path, relative, _ in files:
            with self.subTest(secret_file=relative):
                assignments, recipients = sops_payload(path)
                self.assertTrue(recipients, f"{relative} carries no SOPS metadata")
                self.assertTrue(assignments, f"{relative} holds no values")
                for name, value in assignments.items():
                    self.assertTrue(
                        str(value).startswith("ENC["),
                        f"{relative}: {name} is not encrypted",
                    )

    def test_sops_config_declares_only_public_age_recipients(self):
        rules = sops_creation_rules()
        self.assertTrue(rules)
        for pattern, recipients in rules:
            with self.subTest(rule=pattern.pattern):
                self.assertTrue(recipients, "rule names no recipient")
                for recipient in recipients:
                    self.assertRegex(recipient, AGE_RECIPIENT)

    def test_no_age_private_key_material_is_present(self):
        skip = {
            ".git", ".terraform", "__pycache__", "packer_cache",
            "packer-output", "operator-output", ".ansible", ".pi",
        }
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if set(path.relative_to(ROOT).parts) & skip:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if AGE_PRIVATE_KEY in text:
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_every_declared_recipient_can_decrypt_its_secret_file(self):
        """Catches a recipient added to .sops.yaml without `sops updatekeys`.

        That mistake is otherwise silent: the config claims the teammate can
        read the file, and nobody finds out until they are locked out.
        """
        files = committed_secret_files()
        self.assertTrue(files)
        for path, relative, declared in files:
            with self.subTest(secret_file=relative):
                _, actual = sops_payload(path)
                self.assertEqual(
                    declared,
                    actual,
                    f"{relative} is stale; run: sops updatekeys {relative}",
                )

    def test_sensitive_artifacts_are_ignored(self):
        patterns = {
            line.strip()
            for line in GITIGNORE.splitlines()
            if line.strip() and not line.startswith("#")
        }
        required = {
            ".env",
            ".env.*",
            "*.key",
            "*.agekey",
            ".terraform/",
            "*.tfstate",
            "*.tfstate.*",
            "*.tfplan",
            "*.tfvars",
            "*.tfvars.json",
        }
        self.assertEqual(required - patterns, set())


class CredentialFreeCheckTests(unittest.TestCase):
    def make_targets(self):
        targets = []
        for line in MAKEFILE.splitlines():
            if line.startswith(("\t", ".PHONY:", "#")) or not line.strip():
                continue
            match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s.*)?", line)
            if match:
                targets.append(match.group(1))
        return targets

    def test_default_target_is_credential_free_help(self):
        targets = self.make_targets()
        self.assertTrue(targets)
        self.assertEqual(targets[0], "help")

    def test_no_make_target_can_reach_production(self):
        targets = set(self.make_targets())
        forbidden = {"apply", "deploy", "destroy", "prod-audit", "rebuild", "restore"}
        self.assertTrue(forbidden.isdisjoint(targets), targets & forbidden)

    def test_checks_force_the_empty_inventory(self):
        """CI must not be able to resolve a production host."""
        self.assertIn(
            "SAFE_ANSIBLE_INVENTORY := ansible/inventory/production/hosts.yml",
            MAKEFILE,
        )
        self.assertGreaterEqual(
            MAKEFILE.count("ANSIBLE_INVENTORY=$(SAFE_ANSIBLE_INVENTORY)"), 2
        )
        inventory = (
            ROOT / "ansible" / "inventory" / "production" / "hosts.yml"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(inventory, r"ansible_host\s*:")

    def test_validation_cannot_reuse_the_authoritative_backend_cache(self):
        self.assertIn('tofu_data_dir="$$(mktemp -d)"', MAKEFILE)
        self.assertIn("init -backend=false", MAKEFILE)


class RebuildScriptTests(unittest.TestCase):
    """The one script that can destroy the running host."""

    SCRIPT = (
        ROOT / "scripts" / "operator" / "rebuild-production.sh"
    ).read_text(encoding="utf-8")

    def test_rebuild_requires_the_exact_server_id(self):
        self.assertIn("CONFIRM_REBUILD:-", self.SCRIPT)
        self.assertIn('== "${EXPECTED_SERVER_ID}"', self.SCRIPT)

    def test_rebuild_never_deletes_the_server_or_its_address(self):
        self.assertNotIn("server delete", self.SCRIPT)
        self.assertNotIn("primary-ip delete", self.SCRIPT)
        self.assertNotIn(
            'disable-protection "${EXPECTED_SERVER_ID}" delete', self.SCRIPT
        )

    def test_only_rebuild_protection_is_dropped_and_it_is_always_restored(self):
        self.assertIn("trap cleanup EXIT INT TERM", self.SCRIPT)
        self.assertIn("restore_rebuild_protection()", self.SCRIPT)
        self.assertIn(
            'hcloud server enable-protection "${EXPECTED_SERVER_ID}" delete rebuild',
            self.SCRIPT,
        )

    def test_identity_is_verified_before_any_protection_is_dropped(self):
        first_mutation = self.SCRIPT.index(
            'hcloud server disable-protection "${EXPECTED_SERVER_ID}" rebuild'
        )
        for check in (
            "server_json=",
            "primary_ip_json=",
            "image_json=",
            "IMPORTED_STATE_VERIFIED",
            "CONSOLE_RECOVERY_VERIFIED",
        ):
            with self.subTest(check=check):
                self.assertLess(self.SCRIPT.index(check), first_mutation)

    def test_rebuild_uses_an_explicit_snapshot_never_latest(self):
        self.assertIn('--image "${SNAPSHOT_ID}"', self.SCRIPT)
        self.assertNotIn("latest", self.SCRIPT.lower())


class ToolchainPinTests(unittest.TestCase):
    def test_opentofu_and_providers_are_pinned_to_exact_versions(self):
        """Assert the shape of the constraint, not a version that will move."""
        constraints = re.findall(
            r"(?:required_version|version)\s*=\s*\"([^\"]+)\"", VERSIONS
        )
        self.assertTrue(constraints)
        for constraint in constraints:
            with self.subTest(constraint=constraint):
                self.assertRegex(constraint, r"^=\s*\d+\.\d+\.\d+$")

    def test_packer_plugins_are_pinned_to_exact_versions(self):
        config = (ROOT / "packer" / "versions.pkr.hcl").read_text(encoding="utf-8")
        constraints = re.findall(
            r"(?:required_version|version)\s*=\s*\"([^\"]+)\"", config
        )
        self.assertTrue(constraints)
        for constraint in constraints:
            with self.subTest(constraint=constraint):
                self.assertRegex(constraint, r"^=\s*\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()

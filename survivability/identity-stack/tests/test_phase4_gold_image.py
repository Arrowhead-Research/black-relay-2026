"""Credential-free assertions for the Phase 4 gold-image implementation."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKER = ROOT / "packer" / "debian13-gold.pkr.hcl"
RUNBOOK = ROOT / "docs" / "runbooks" / "gold-image.md"


class Phase4GoldImageTests(unittest.TestCase):
    def test_packer_template_is_generic_debian13_hcloud_image(self):
        config = PACKER.read_text(encoding="utf-8")
        self.assertIn('default     = "debian-13"', config)
        self.assertIn('default     = "cx23"', config)
        self.assertNotIn('"cx22"', config)
        self.assertIn('location     = var.location', config)
        self.assertIn('snapshot_name = local.snapshot_name', config)
        self.assertIn('ssh_username = "root"', config)
        self.assertNotIn('ssh_private_key', config)
        self.assertNotIn('ssh_agent_auth', config)

    def test_no_provider_token_or_secret_value_is_committed(self):
        config = PACKER.read_text(encoding="utf-8")
        self.assertIn('default     = env("HCLOUD_TOKEN")', config)
        self.assertNotRegex(config, r"hcloud_[A-Za-z0-9]{20,}")
        for forbidden in ("CLOUDFLARE_API_TOKEN", "AWS_SECRET_ACCESS_KEY", "TF_VAR_state_passphrase"):
            self.assertNotIn(forbidden, config)

    def test_docker_compose_and_fluent_bit_versions_are_required_exact_pins(self):
        config = PACKER.read_text(encoding="utf-8")
        for variable in (
            "docker_package_version",
            "docker_cli_package_version",
            "containerd_package_version",
            "docker_buildx_package_version",
            "docker_compose_package_version",
            "fluent_bit_package_version",
        ):
            block_match = re.search(rf'variable "{variable}" \{{(?P<body>.*?)\n\}}', config, re.S)
            self.assertIsNotNone(block_match, variable)
            self.assertNotIn("default", block_match.group("body"), variable)
        self.assertIn('"docker-compose-plugin=${DOCKER_COMPOSE_PACKAGE_VERSION}"', (ROOT / "packer" / "scripts" / "20-docker.sh").read_text(encoding="utf-8"))
        self.assertIn('"fluent-bit=${FLUENT_BIT_PACKAGE_VERSION}"', (ROOT / "packer" / "scripts" / "30-fluent-bit.sh").read_text(encoding="utf-8"))

    def test_archive_keys_are_fingerprint_checked(self):
        docker = (ROOT / "packer" / "scripts" / "20-docker.sh").read_text(encoding="utf-8")
        fluent = (ROOT / "packer" / "scripts" / "30-fluent-bit.sh").read_text(encoding="utf-8")
        self.assertIn("9DC858229FC7DD38854AE2D88D81803C0EBFCD88", docker)
        self.assertIn("C3C0A28534B9293EAF51FABD9F9DDC083888C1CD", fluent)
        self.assertIn("gpg --show-keys --with-colons", docker)
        self.assertIn("gpg --show-keys --with-colons", fluent)

    def test_baseline_hardening_and_validation_are_documented(self):
        hardening = (ROOT / "packer" / "scripts" / "40-hardening.sh").read_text(encoding="utf-8")
        validation = (ROOT / "packer" / "scripts" / "validate-gold-image.sh").read_text(encoding="utf-8")
        self.assertIn("PasswordAuthentication no", hardening)
        self.assertIn("net.ipv6.conf.all.disable_ipv6 = 1", hardening)
        self.assertIn('Automatic-Reboot "false"', hardening)
        self.assertIn("Docker is installed", validation)
        self.assertIn("Fluent Bit is disabled", validation)
        self.assertIn("systemctl is-enabled", validation)
        self.assertNotIn("systemctl is-disabled", validation)
        self.assertIn("Fluent Bit is not running", validation)
        self.assertIn("systemctl show --property=ActiveState --value", validation)
        self.assertNotIn("systemctl is-inactive", validation)

    def test_runbook_labels_operator_only_steps_and_snapshot_retention(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("trusted operator workstation only", runbook)
        self.assertIn("must not rebuild or modify the production CPX32", runbook)
        self.assertIn("previous validated rollback snapshot", runbook)
        self.assertIn("Never select an image using `latest`", runbook)

    def test_operator_scripts_fail_closed_and_are_used_by_runbook(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        script_names = (
            "discover-package-versions.sh",
            "build-gold-image.sh",
            "validate-gold-image.sh",
            "promote-gold-image.sh",
        )
        for name in script_names:
            script = (ROOT / "scripts" / "operator" / name).read_text(encoding="utf-8")
            self.assertIn("set -euo pipefail", script)
            self.assertIn("HCLOUD_TOKEN", script)
            self.assertIn(f"scripts/operator/{name}", runbook)
        discovery = (ROOT / "scripts" / "operator" / "discover-package-versions.sh").read_text(encoding="utf-8")
        self.assertIn("trap cleanup EXIT INT TERM", discovery)
        self.assertIn('hcloud server delete "${DISCOVERY_ID}"', discovery)
        self.assertIn('wc -l <"${TEMP_OUTPUT}"', discovery)
        discovery_payload = (ROOT / "packer" / "scripts" / "discover-package-versions.sh").read_text(encoding="utf-8")
        self.assertIn("cloud-init status --wait >&2", discovery_payload)
        self.assertIn('--identity-file', discovery)
        self.assertIn('-o IdentityFile="${IDENTITY_FILE}"', discovery)
        self.assertNotIn('BatchMode=yes', discovery)
        validation = (ROOT / "scripts" / "operator" / "validate-gold-image.sh").read_text(encoding="utf-8")
        self.assertIn("trap cleanup EXIT INT TERM", validation)
        self.assertIn('hcloud server delete "${VALIDATION_ID}"', validation)
        self.assertIn('--identity-file', validation)
        self.assertNotIn('BatchMode=yes', validation)
        promotion = (ROOT / "scripts" / "operator" / "promote-gold-image.sh").read_text(encoding="utf-8")
        self.assertIn('.result == "PASS"', promotion)
        self.assertNotIn("server rebuild", promotion)


if __name__ == "__main__":
    unittest.main()

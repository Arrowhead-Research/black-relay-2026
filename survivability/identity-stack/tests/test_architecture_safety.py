"""Credential-free assertions for the current workspace safety boundary."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
GITIGNORE = ROOT / ".gitignore"


class ArchitectureSafetyTests(unittest.TestCase):
    def make_targets(self):
        targets = []
        for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
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

    def test_no_rebuild_or_production_target_exists_yet(self):
        targets = set(self.make_targets())
        forbidden = {"apply", "deploy", "destroy", "prod-audit", "rebuild"}
        self.assertTrue(forbidden.isdisjoint(targets), targets & forbidden)

    def test_credential_free_checks_force_safe_inventory(self):
        makefile = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn(
            "SAFE_ANSIBLE_INVENTORY := ansible/inventory/production/hosts.yml",
            makefile,
        )
        self.assertEqual(
            makefile.count("ANSIBLE_INVENTORY=$(SAFE_ANSIBLE_INVENTORY)"),
            2,
        )

    def test_validation_cannot_reuse_the_authoritative_backend_cache(self):
        makefile = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn('tofu_data_dir="$$(mktemp -d)"', makefile)
        self.assertEqual(makefile.count('TF_DATA_DIR="$$tofu_data_dir" tofu'), 2)
        self.assertIn("init -backend=false", makefile)

    def test_sensitive_opentofu_files_are_ignored(self):
        patterns = {
            line.strip()
            for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
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


if __name__ == "__main__":
    unittest.main()

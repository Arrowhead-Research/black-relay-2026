"""Ensure Phase 2 toolchain constraints remain explicit and credential-free."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ToolchainPinTests(unittest.TestCase):
    def test_cli_versions_are_pinned_with_checksums(self):
        containerfile = (ROOT / "dev/Containerfile").read_text(encoding="utf-8")
        for assignment in (
            "ARG PACKER_VERSION=1.16.0",
            "ARG OPENTOFU_VERSION=1.12.6",
        ):
            self.assertIn(assignment, containerfile)
        self.assertEqual(containerfile.count("packer_sha='"), 2)
        self.assertEqual(containerfile.count("tofu_sha='"), 2)

    def test_packer_plugin_is_exactly_pinned(self):
        config = (ROOT / "packer/versions.pkr.hcl").read_text(encoding="utf-8")
        self.assertIn('required_version = "= 1.16.0"', config)
        self.assertIn('source  = "github.com/hetznercloud/hcloud"', config)
        self.assertIn('version = "= 1.8.0"', config)

    def test_opentofu_and_providers_are_exactly_pinned(self):
        config = (ROOT / "tofu/versions.tf").read_text(encoding="utf-8")
        lock = (ROOT / "tofu/.terraform.lock.hcl").read_text(encoding="utf-8")
        self.assertIn('required_version = "= 1.12.6"', config)
        self.assertIn('version = "= 5.24.0"', config)
        self.assertIn('version = "= 1.68.0"', config)
        self.assertIn('provider "registry.opentofu.org/cloudflare/cloudflare"', lock)
        self.assertIn('provider "registry.opentofu.org/hetznercloud/hcloud"', lock)

    def test_phase_two_has_no_resources_or_backend(self):
        tofu_config = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "tofu").glob("*.tf")
        )
        self.assertIsNone(re.search(r'(?m)^\s*resource\s+"', tofu_config))
        self.assertIsNone(re.search(r'(?m)^\s*backend\s+"', tofu_config))
        self.assertNotIn("HCLOUD_TOKEN", tofu_config)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", tofu_config)

    def test_operator_example_contains_names_without_values(self):
        example = (ROOT / "operator.env.example").read_text(encoding="utf-8")
        assignments = [
            line for line in example.splitlines()
            if line and not line.startswith("#")
        ]
        self.assertTrue(assignments)
        self.assertTrue(all(re.fullmatch(r"[A-Z][A-Z0-9_]*=", line) for line in assignments))


if __name__ == "__main__":
    unittest.main()

"""Credential-free assertions for the declarative Phase 6 host baseline."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible" / "roles" / "host_baseline"
TASKS = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
DEFAULTS = (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
JOURNALD = (ROLE / "templates" / "journald-bounds.conf.j2").read_text(
    encoding="utf-8"
)
UPDATES = (ROLE / "templates" / "unattended-upgrades.conf.j2").read_text(
    encoding="utf-8"
)
SYSCTL = (ROLE / "templates" / "sysctl.conf.j2").read_text(encoding="utf-8")
SITE = (ROOT / "ansible" / "playbooks" / "site.yml").read_text(encoding="utf-8")


class Phase6HostBaselineTests(unittest.TestCase):
    def test_site_composes_semantic_baseline_after_access(self):
        access = SITE.index("role: operator_access")
        baseline = SITE.index("role: host_baseline")
        self.assertLess(access, baseline)

    def test_required_packages_and_updates_are_managed(self):
        for package in (
            "ca-certificates",
            "curl",
            "gnupg",
            "jq",
            "needrestart",
            "unattended-upgrades",
        ):
            self.assertIn(f"- {package}", DEFAULTS)
        self.assertIn("upgrade: safe", TASKS)
        self.assertIn("host_baseline_apply_package_updates", TASKS)

    def test_unattended_updates_are_enabled_without_reboots(self):
        self.assertIn('Automatic-Reboot "false"', UPDATES)
        self.assertIn("apt-daily.timer", TASKS)
        self.assertIn("apt-daily-upgrade.timer", TASKS)
        self.assertIn("APT::Periodic::Unattended-Upgrade", TASKS)
        self.assertNotIn("ansible.builtin.reboot", TASKS)

    def test_journal_is_persistent_and_bounded(self):
        self.assertIn("Storage=persistent", JOURNALD)
        self.assertIn("SystemMaxUse=", JOURNALD)
        self.assertIn("RuntimeMaxUse=", JOURNALD)
        self.assertIn("MaxRetentionSec=", JOURNALD)
        self.assertIn("/var/log/journal", TASKS)

    def test_kernel_and_network_hardening_is_declarative(self):
        for setting in (
            "net.ipv6.conf.all.disable_ipv6 = 1",
            "net.ipv4.conf.all.rp_filter = 1",
            "net.ipv4.tcp_syncookies = 1",
            "kernel.kptr_restrict = 2",
            "kernel.dmesg_restrict = 1",
        ):
            self.assertIn(setting, SYSCTL)
        handlers = (ROLE / "handlers" / "main.yml").read_text(encoding="utf-8")
        self.assertIn("/usr/sbin/sysctl --system", handlers)

    def test_reboot_and_disk_pressure_are_reported_not_mutated(self):
        self.assertIn("/run/reboot-required", TASKS)
        self.assertIn("Report pending reboot without performing one", TASKS)
        self.assertIn("Report root filesystem pressure", TASKS)
        self.assertIn("host_baseline_disk_free_warning_percent", DEFAULTS)
        self.assertNotIn("state: rebooted", TASKS)


if __name__ == "__main__":
    unittest.main()

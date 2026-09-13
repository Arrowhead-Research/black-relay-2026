"""Credential-free assertions for guarded Phase 6 host firewall management."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible" / "roles" / "host_firewall"
TASKS = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
POLICY = (ROLE / "templates" / "survivability.nft.j2").read_text(
    encoding="utf-8"
)
ROLLBACK = (ROLE / "templates" / "rollback-host-firewall.sh.j2").read_text(
    encoding="utf-8"
)
SITE = (ROOT / "ansible" / "playbooks" / "site.yml").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "runbooks" / "phase6-host-firewall.md").read_text(
    encoding="utf-8"
)


class Phase6FirewallTests(unittest.TestCase):
    def test_site_composes_firewall_after_access_and_baseline(self):
        access = SITE.index("role: operator_access")
        baseline = SITE.index("role: host_baseline")
        firewall = SITE.index("role: host_firewall")
        self.assertLess(access, baseline)
        self.assertLess(baseline, firewall)

    def test_input_is_ipv4_default_deny_with_only_public_service_ports(self):
        self.assertIn("type filter hook input priority filter; policy drop", POLICY)
        self.assertIn("meta nfproto ipv6 drop", POLICY)
        self.assertIn("iifname \"lo\" accept", POLICY)
        self.assertIn("ct state { established, related } accept", POLICY)
        self.assertIn("ip protocol icmp icmp type", POLICY)
        self.assertIn("tcp dport { 22, 80, 443 }", POLICY)
        self.assertNotIn("udp dport", POLICY)

    def test_docker_dnat_cannot_publish_backend_ports(self):
        self.assertIn("hook forward priority filter - 1; policy drop", POLICY)
        self.assertIn("ct status dnat", POLICY)
        self.assertIn("ct original proto-dst { 80, 443 } accept", POLICY)
        self.assertIn("ct status dnat drop", POLICY)
        self.assertIn('iifname "docker0" accept', POLICY)
        self.assertIn('iifname "br-*" accept', POLICY)
        self.assertIn("icc | bool", TASKS)
        self.assertIn("['userland-proxy'] | bool", TASKS)

    def test_policy_never_flushes_docker_or_complete_ruleset(self):
        self.assertIn("table inet survivability_filter", POLICY)
        self.assertIn("flush table inet survivability_filter", POLICY)
        self.assertNotIn("flush ruleset", POLICY)
        self.assertNotIn("table ip filter", POLICY)
        self.assertIn('include "/etc/nftables.d/*.nft"', TASKS)

    def test_changed_policy_requires_exact_host_confirmation(self):
        self.assertIn("'APPLY_HOST_FIREWALL_' ~ inventory_hostname", TASKS)
        self.assertIn("host_firewall_apply_confirmation", TASKS)
        self.assertIn("host_firewall_activation_required", TASKS)
        self.assertIn("confirmation is needed only", TASKS.lower())

    def test_activation_has_timed_rollback_and_fresh_ssh_proof(self):
        schedule = TASKS.index("Schedule automatic rollback before activation")
        apply_policy = TASKS.index("Apply candidate policy atomically")
        reconnect = TASKS.index("Prove fresh SSH and sudo access")
        persist = TASKS.index("Persist the verified policy")
        cancel = TASKS.index("Cancel automatic rollback")
        self.assertLess(schedule, apply_policy)
        self.assertLess(apply_policy, reconnect)
        self.assertLess(reconnect, persist)
        self.assertLess(persist, cancel)
        self.assertIn("ansible.builtin.meta: reset_connection", TASKS)
        self.assertIn("ansible.builtin.wait_for_connection", TASKS)
        self.assertIn("last-known-good.nft", TASKS)
        self.assertIn("delete table inet survivability_filter", ROLLBACK)

    def test_runbook_uses_site_and_documents_outer_firewall(self):
        self.assertIn("trusted operator workstation only", RUNBOOK.lower())
        self.assertIn("APPLY_HOST_FIREWALL_identity_production", RUNBOOK)
        self.assertIn("ansible/playbooks/site.yml", RUNBOOK)
        self.assertIn("hetzner firewall", RUNBOOK.lower())
        self.assertIn("automatic rollback", RUNBOOK)


if __name__ == "__main__":
    unittest.main()

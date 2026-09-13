"""Credential-free assertions for simplified Phase 6 operator access."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible" / "roles" / "operator_access"
TASKS = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
SSHD = (ROLE / "templates" / "sshd-operator-access.conf.j2").read_text(
    encoding="utf-8"
)
BOOTSTRAP = (
    ROOT / "ansible" / "playbooks" / "phase6-bootstrap-access.yml"
).read_text(encoding="utf-8")
SITE = (ROOT / "ansible" / "playbooks" / "site.yml").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "runbooks" / "phase6-access-bootstrap.md").read_text(
    encoding="utf-8"
)


class Phase6AccessTests(unittest.TestCase):
    def test_bootstrap_hands_off_to_normal_convergence(self):
        self.assertIn("operator_access_allow_root_ssh: true", BOOTSTRAP)
        self.assertIn("ansible.builtin.meta: reset_connection", BOOTSTRAP)
        self.assertIn("ansible.builtin.import_playbook: site.yml", BOOTSTRAP)
        self.assertIn('ansible_user: "{{ operator_access_user }}"', SITE)
        self.assertIn(
            '"{{ operator_access_ansible_ssh_private_key_file }}"', SITE
        )
        self.assertIn("operator_access_allow_root_ssh: false", SITE)

    def test_normal_convergence_proves_named_operator_sudo(self):
        self.assertIn("Prove named-operator passwordless elevation", SITE)
        self.assertIn("ansible.builtin.command: /usr/bin/id -u", SITE)
        self.assertIn("become: true", SITE)
        self.assertIn("check_mode: false", SITE)
        self.assertIn("operator_access_elevated_identity.stdout == '0'", SITE)

    def test_one_operator_has_distinct_manual_and_ansible_keys(self):
        self.assertIn("operator_access_user", TASKS)
        self.assertIn("operator_access_manual_ssh_private_key_file + '.pub'", TASKS)
        self.assertIn("operator_access_ansible_ssh_private_key_file + '.pub'", TASKS)
        self.assertIn("operator_access_public_keys | unique | length == 2", TASKS)
        self.assertEqual(TASKS.count("lookup('ansible.builtin.file'"), 2)
        self.assertNotIn("phase6_admin_users", TASKS)
        self.assertNotIn("length >= 2", TASKS)

    def test_named_account_uses_both_exclusive_keys_and_no_password(self):
        self.assertIn("password_lock: true", TASKS)
        self.assertIn("ansible.posix.authorized_key", TASKS)
        self.assertIn("operator_access_public_keys | join", TASKS)
        self.assertIn("exclusive: true", TASKS)
        self.assertIn("operator_access_user not in ['root', 'debian']", TASKS)
        self.assertIn("PRIVATE KEY", TASKS)
        self.assertIn("NOPASSWD: ALL", TASKS)

    def test_ssh_is_key_only_without_interactive_server_enrollment(self):
        self.assertIn("PasswordAuthentication no", SSHD)
        self.assertIn("KbdInteractiveAuthentication no", SSHD)
        self.assertIn("AuthenticationMethods publickey", SSHD)
        self.assertNotIn("google_authenticator", TASKS.lower())
        self.assertFalse((ROLE / "templates" / "pam-sshd.j2").exists())

    def test_root_is_disabled_only_after_policy_validation(self):
        self.assertIn(
            "'prohibit-password' if operator_access_allow_root_ssh else 'no'", SSHD
        )
        self.assertIn("/root/.ssh/authorized_keys", TASKS)
        self.assertIn("when: not operator_access_allow_root_ssh | bool", TASKS)
        flush = TASKS.index("ansible.builtin.meta: flush_handlers")
        remove_root = TASKS.index("Remove the bootstrap root authorized key")
        self.assertLess(flush, remove_root)

    def test_external_inventory_needs_only_host_user_and_two_identity_paths(self):
        inventory = (
            ROOT / "ansible" / "inventory" / "production" / "hosts.example.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("identity_stack:", inventory)
        self.assertIn("identity_production:", inventory)
        self.assertIn("ansible_host:", inventory)
        self.assertIn("operator_access_user:", inventory)
        self.assertIn("operator_access_manual_ssh_private_key_file:", inventory)
        self.assertIn("operator_access_ansible_ssh_private_key_file:", inventory)
        self.assertIn(
            "ansible_ssh_private_key_file: *ansible_ssh_identity", inventory
        )
        # Phase 6 contributes the four access placeholders; Phase 7 adds the
        # three non-secret Storage Box placeholders (the known_hosts line
        # carries the fourth REPLACE_WITH token).
        self.assertEqual(inventory.count("REPLACE_WITH"), 8)
        self.assertIn("backup_restic_storage_box_server:", inventory)
        self.assertIn(
            "backup_restic_storage_box_subaccount_username:", inventory
        )
        self.assertIn("backup_restic_storage_box_known_hosts:", inventory)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", inventory)
        self.assertFalse(
            (
                ROOT
                / "ansible"
                / "inventory"
                / "production"
                / "group_vars"
                / "all"
                / "phase6.example.yml"
            ).exists()
        )

    def test_runbook_creates_key_then_uses_one_bootstrap_command(self):
        self.assertIn("ssh-keygen", RUNBOOK)
        self.assertIn("survivability-ansible", RUNBOOK)
        self.assertIn("ssh-copy-id", RUNBOOK)
        self.assertIn("one authoritative Ansible command", RUNBOOK)
        self.assertIn("ansible/playbooks/phase6-bootstrap-access.yml", RUNBOOK)
        self.assertIn("site.yml", RUNBOOK)
        self.assertIn("There is no second-person\ncheck", RUNBOOK)
        self.assertNotIn("google-authenticator", RUNBOOK)
        self.assertFalse(
            (ROOT / "ansible" / "playbooks" / "phase6-finalize-access.yml").exists()
        )


if __name__ == "__main__":
    unittest.main()

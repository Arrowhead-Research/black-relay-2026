"""Credential-free assertions for the Phase 7 backup foundation."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOFU = ROOT / "tofu"
MAIN = (TOFU / "main.tf").read_text(encoding="utf-8")
VARIABLES = (TOFU / "variables.tf").read_text(encoding="utf-8")
OUTPUTS = (TOFU / "outputs.tf").read_text(encoding="utf-8")
ROLE = ROOT / "ansible" / "roles" / "backup_restic"
DEFAULTS = (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
TASKS = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
WRAPPER = (ROLE / "templates" / "survivability-backup.sh.j2").read_text(
    encoding="utf-8"
)
BACKUP_SERVICE = (ROLE / "templates" / "survivability-backup.service.j2").read_text(
    encoding="utf-8"
)
MAINT_SERVICE = (
    ROLE / "templates" / "survivability-backup-maintenance.service.j2"
).read_text(encoding="utf-8")
BACKUP_TIMER = (ROLE / "templates" / "survivability-backup.timer.j2").read_text(
    encoding="utf-8"
)
MAINT_TIMER = (
    ROLE / "templates" / "survivability-backup-maintenance.timer.j2"
).read_text(encoding="utf-8")
SITE = (ROOT / "ansible" / "playbooks" / "site.yml").read_text(encoding="utf-8")
RESTORE_PLAY = (
    ROOT / "ansible" / "playbooks" / "phase7-restore-test.yml"
).read_text(encoding="utf-8")
RESTORE_SCRIPT = (
    ROOT / "scripts" / "operator" / "test-backup-restore.sh"
).read_text(encoding="utf-8")
SECRETS_EXAMPLE = (
    ROOT / "ansible" / "inventory" / "production" / "group_vars" / "all"
    / "secrets.example.yml"
).read_text(encoding="utf-8")
HOSTS_EXAMPLE = (
    ROOT / "ansible" / "inventory" / "production" / "hosts.example.yml"
).read_text(encoding="utf-8")
RUNBOOK = (
    ROOT / "docs" / "runbooks" / "phase7-backup-foundation.md"
).read_text(encoding="utf-8")
ROADMAP = (ROOT / "ARCHITECTURE_ROADMAP.md").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
SOPS_CONFIG = (ROOT / ".sops.yaml").read_text(encoding="utf-8")
OPERATOR_ENV = (ROOT / "operator.env.example").read_text(encoding="utf-8")


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


class OpenTofuBackupTests(unittest.TestCase):
    def test_subaccount_is_protected_and_least_privilege(self):
        subaccount = hcl_block(
            MAIN, "resource", "hcloud_storage_box_subaccount", "restic"
        )
        self.assertIn("prevent_destroy = true", subaccount)
        self.assertIn("storage_box_id = hcloud_storage_box.backups.id", subaccount)
        self.assertIn('home_directory = "backups/"', subaccount)
        self.assertIn("password       = var.storage_box_subaccount_password", subaccount)
        self.assertIn("ssh_enabled          = false", subaccount)
        self.assertIn(
            "reachable_externally = var.storage_box_bootstrap_external_reachability",
            subaccount,
        )
        self.assertIn("samba_enabled        = false", subaccount)
        self.assertIn("webdav_enabled       = false", subaccount)
        self.assertIn("readonly             = false", subaccount)

    def test_storage_box_disables_optional_port23_ssh(self):
        box = hcl_block(MAIN, "resource", "hcloud_storage_box", "backups")
        subaccount = hcl_block(
            MAIN, "resource", "hcloud_storage_box_subaccount", "restic"
        )
        self.assertIn("prevent_destroy = true", box)
        for resource in (box, subaccount):
            self.assertIn("ssh_enabled          = false", resource)
            self.assertIn(
                "reachable_externally = var.storage_box_bootstrap_external_reachability",
                resource,
            )
            self.assertIn("samba_enabled        = false", resource)
            self.assertIn("webdav_enabled       = false", resource)
        self.assertIn("zfs_enabled          = false", box)

    def test_external_reachability_defaults_to_false(self):
        match = re.search(
            r'variable\s+"storage_box_bootstrap_external_reachability"\s*\{(.*?)\n\}',
            VARIABLES,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        block = match.group(1)
        self.assertIn("type        = bool", block)
        self.assertIn("default     = false", block)

    def test_subaccount_password_is_sensitive_without_default(self):
        match = re.search(
            r'variable\s+"storage_box_subaccount_password"\s*\{(.*?)\n\}',
            VARIABLES,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        block = match.group(1)
        self.assertIn("sensitive   = true", block)
        self.assertNotRegex(block, r"\bdefault\s*=")
        self.assertIn("20", block)

    def test_outputs_expose_subaccount_identity_without_secrets(self):
        for name in (
            "storage_box_subaccount_username",
            "storage_box_subaccount_server",
        ):
            match = re.search(
                rf'output\s+"{name}"\s*\{{(.*?)\n\}}',
                OUTPUTS,
                re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            self.assertNotIn("sensitive", match.group(1), name)
        self.assertIn(
            "hcloud_storage_box_subaccount.restic.username", OUTPUTS
        )
        self.assertIn("hcloud_storage_box_subaccount.restic.server", OUTPUTS)

    def test_operator_env_documents_the_subaccount_secret(self):
        self.assertIn("STORAGE_BOX_SUBACCOUNT_PASSWORD=", OPERATOR_ENV)
        self.assertIn("TF_VAR_storage_box_subaccount_password", OPERATOR_ENV)


class BackupRoleTests(unittest.TestCase):
    def test_site_composes_the_backup_role(self):
        self.assertIn("- role: backup_restic", SITE)

    def test_retention_and_schedule_follow_the_roadmap(self):
        self.assertIn("backup_restic_keep_daily: 7", DEFAULTS)
        self.assertIn("backup_restic_keep_weekly: 5", DEFAULTS)
        self.assertIn("backup_restic_keep_monthly: 12", DEFAULTS)
        self.assertIn("backup_restic_backup_oncalendar: \"*-*-* 02:00\"", DEFAULTS)
        self.assertIn(
            'backup_restic_maintenance_oncalendar: "Mon *-*-* 03:30"', DEFAULTS
        )
        self.assertIn("backup_restic_stale_after_hours: 26", DEFAULTS)
        self.assertIn("backup_restic_sftp_port: 22", DEFAULTS)
        self.assertIn("backup_restic_init_repository: false", DEFAULTS)
        self.assertIn("backup_restic_required_prune_confirmation", DEFAULTS)
        self.assertIn("backup_restic_backup_randomized_delay: 30min", DEFAULTS)

    def test_backup_scope_includes_managed_cryptographic_and_firewall_state(self):
        self.assertIn("- /var/lib/survivability-backup", DEFAULTS)
        self.assertIn("- /var/lib/survivability-firewall", DEFAULTS)

    def test_secrets_are_loaded_from_sops_and_never_have_defaults(self):
        self.assertIn("community.sops.load_vars", TASKS)
        self.assertIn("no_log: true", TASKS)
        self.assertIn("diff: false", TASKS)
        self.assertIn("backup_restic_secrets_file:", DEFAULTS)
        self.assertIn("secrets.sops.yml", DEFAULTS)
        for secret in (
            "backup_restic_repository_password",
            "backup_restic_ssh_private_key",
            "backup_restic_healthchecks_backup_url",
            "backup_restic_healthchecks_maintenance_url",
        ):
            self.assertNotIn(f"{secret}:", DEFAULTS)
            self.assertIn(secret, TASKS)

    def test_secret_files_are_rendered_root_only(self):
        self.assertIn('dest: "{{ backup_restic_password_file }}"', TASKS)
        self.assertIn('dest: "{{ backup_restic_ssh_key_file }}"', TASKS)
        self.assertIn('dest: "{{ backup_restic_env_file }}"', TASKS)
        for secret_task in (
            "Render root-only restic repository password",
            "Render root-only backup Storage Box SSH key",
            "Render root-only restic environment file",
        ):
            start = TASKS.index(f"- name: {secret_task}")
            end = TASKS.index("- name:", start + 1)
            block = TASKS[start:end]
            self.assertIn('mode: "0600"', block, secret_task)
            self.assertIn("no_log: true", block, secret_task)
            self.assertIn("diff: false", block, secret_task)

    def test_initialization_is_confirmed_and_repository_loss_fails_closed(self):
        self.assertIn("when: backup_restic_init_repository | bool", TASKS)
        self.assertIn("Require exact confirmation before repository initialization", TASKS)
        self.assertIn("backup_restic_required_init_confirmation", TASKS)
        self.assertIn("Require the configured restic repository to exist", TASKS)
        self.assertIn("require-repository", TASKS)
        self.assertIn(
            '''changed_when: "'repository initialized' in backup_restic_repository_init.stdout"''',
            TASKS,
        )
        self.assertIn("when: backup_restic_manage_schedule | bool", TASKS)
        self.assertIn("survivability-backup.timer", TASKS)
        self.assertIn("survivability-backup-maintenance.timer", TASKS)

    def test_sftp_uses_an_isolated_pinned_ssh_config(self):
        ssh_config = (ROLE / "templates" / "ssh-config.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn("sftp.args=-F ${SSH_CONFIG}", WRAPPER)
        self.assertIn("IdentityFile {{ backup_restic_ssh_key_file }}", ssh_config)
        self.assertIn("StrictHostKeyChecking yes", ssh_config)
        self.assertIn("UserKnownHostsFile {{ backup_restic_known_hosts_file }}", ssh_config)
        self.assertIn("ServerAliveInterval 60", ssh_config)
        self.assertIn("ServerAliveCountMax 240", ssh_config)
        self.assertNotIn("dest: /root/.ssh/config", TASKS)

    def test_wrapper_signals_healthchecks_and_stages_sqlite(self):
        self.assertIn('hc_ping "${SURVIVABILITY_HC_BACKUP_URL}/start"', WRAPPER)
        self.assertIn('hc_ping "${SURVIVABILITY_HC_BACKUP_URL}"', WRAPPER)
        self.assertIn('hc_ping "${SURVIVABILITY_HC_BACKUP_URL}/fail"', WRAPPER)
        self.assertIn(
            'hc_ping "${SURVIVABILITY_HC_MAINTENANCE_URL}/start"', WRAPPER
        )
        self.assertIn('hc_ping "${SURVIVABILITY_HC_MAINTENANCE_URL}/fail"', WRAPPER)
        self.assertIn('sqlite3 "${db}" ".backup', WRAPPER)
        self.assertIn('rm -rf "${STAGING_DIR}/sqlite"', WRAPPER)
        self.assertIn('return 1', WRAPPER)
        self.assertIn("--retry-lock", WRAPPER)
        self.assertIn("--one-file-system", WRAPPER)
        self.assertIn("sftp.args=-F ${SSH_CONFIG}", WRAPPER)
        self.assertIn("restic_cmd \"$@\"", WRAPPER)

    def test_automatic_backup_and_verification_never_prune(self):
        self.assertIn("cmd_backup()", WRAPPER)
        self.assertIn("cmd_verify()", WRAPPER)
        self.assertIn("cmd_prune()", WRAPPER)
        self.assertIn("cmd_init()", WRAPPER)
        backup_block = WRAPPER[WRAPPER.index("cmd_backup()"):WRAPPER.index("cmd_verify()")]
        verify_block = WRAPPER[WRAPPER.index("cmd_verify()"):WRAPPER.index("cmd_prune()")]
        self.assertNotIn("forget", backup_block)
        self.assertNotIn("prune", backup_block)
        self.assertNotIn("forget", verify_block)
        self.assertNotIn("prune", verify_block)
        prune_block = WRAPPER[WRAPPER.index("cmd_prune()"):WRAPPER.index("cmd_init()")]
        self.assertIn("EXPECTED_PRUNE_CONFIRMATION", prune_block)
        self.assertIn("forget --prune", prune_block)
        init_block = WRAPPER[WRAPPER.index("cmd_init()"):]
        self.assertIn(
            '"survivability-backup: repository already present"', init_block
        )
        self.assertIn(
            '"survivability-backup: repository initialized"', init_block
        )

    def test_systemd_units_are_hardened_oneshot_with_utc_timers(self):
        for service in (BACKUP_SERVICE, MAINT_SERVICE):
            self.assertIn("Type=oneshot", service)
            self.assertIn("NoNewPrivileges=yes", service)
            self.assertIn("ProtectSystem=full", service)
            self.assertIn("PrivateTmp=yes", service)
        self.assertIn("ExecStart={{ backup_restic_script_path }} backup", BACKUP_SERVICE)
        self.assertIn(
            "ExecStart={{ backup_restic_script_path }} verify", MAINT_SERVICE
        )
        self.assertIn("Persistent=true", BACKUP_TIMER)
        self.assertIn("Persistent=true", MAINT_TIMER)
        self.assertIn("RandomizedDelaySec={{", BACKUP_TIMER)
        self.assertIn("UTC", BACKUP_TIMER)
        self.assertIn("UTC", MAINT_TIMER)

    def test_freshness_helper_enforces_the_26_hour_objective(self):
        helper = (ROLE / "files" / "check-restic-freshness.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("limit_hours", helper)
        self.assertIn("limit_hours * 3600", helper)


class RestoreTestTests(unittest.TestCase):
    def test_restore_play_requires_exact_confirmation_and_never_enables(self):
        self.assertIn(
            "'CONFIRM_RESTORE_TEST_' ~ inventory_hostname", RESTORE_PLAY
        )
        self.assertIn("backup_restic_manage_schedule: false", RESTORE_PLAY)
        self.assertIn("backup_restic_init_repository: false", RESTORE_PLAY)
        self.assertNotIn("forget", RESTORE_PLAY)
        self.assertNotIn("prune", RESTORE_PLAY)
        self.assertNotIn("systemd_service", RESTORE_PLAY)
        self.assertIn("backup_restore_test_target: /var/tmp/survivability-restore-test", RESTORE_PLAY)
        self.assertIn("/etc/ssh/sshd_config", RESTORE_PLAY)
        self.assertIn("repository-password", RESTORE_PLAY)
        self.assertIn("PRAGMA integrity_check", RESTORE_PLAY)

    def test_restore_script_guards_disposable_infrastructure(self):
        for required in (
            "HCLOUD_TOKEN",
            "SOPS_AGE_KEY_FILE",
            "ANSIBLE_INVENTORY",
            'read -r -p "Type restore-test to continue: "',
            "[[ \"${confirmation}\" == \"restore-test\" ]]",
            "purpose=backup-restore-test",
            "expires-at",
            "trap cleanup EXIT INT TERM",
            "hcloud server delete",
            "phase7-restore-test.yml",
            "CONFIRM_RESTORE_TEST_",
            "phase7-restore-test.json",
            "ssh_pwauth: false",
            "operator-output",
            'cd "${ROOT}"',
        ):
            self.assertIn(required, RESTORE_SCRIPT, required)
        self.assertNotIn("server rebuild", RESTORE_SCRIPT)
        self.assertNotIn("hcloud server delete \"${EXPECTED_SERVER_ID}\"", RESTORE_SCRIPT)
        self.assertIn("purpose=backup-restore-test", RESTORE_SCRIPT)


class DocumentationAndStatusTests(unittest.TestCase):
    def test_secrets_example_has_placeholders_only(self):
        self.assertIn("REPLACE_WITH", SECRETS_EXAMPLE)
        self.assertNotRegex(SECRETS_EXAMPLE, r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
        for name in (
            "backup_restic_repository_password",
            "backup_restic_ssh_private_key",
            "backup_restic_healthchecks_backup_url",
            "backup_restic_healthchecks_maintenance_url",
        ):
            self.assertIn(name, SECRETS_EXAMPLE)

    def test_hosts_example_documents_backup_placeholders(self):
        for name in (
            "backup_restic_storage_box_server",
            "backup_restic_storage_box_subaccount_username",
            "backup_restic_storage_box_known_hosts",
        ):
            self.assertIn(name, HOSTS_EXAMPLE)
        self.assertNotIn(":23 ssh-ed25519", HOSTS_EXAMPLE)

    def test_sops_creation_rule_covers_the_backup_secrets_path(self):
        self.assertIn("secrets\\.sops\\.yml$", SOPS_CONFIG)

    def test_runbook_is_operator_only_and_covers_the_sequence(self):
        self.assertIn("Status: pending trusted-operator execution", RUNBOOK)
        self.assertIn("trusted operator workstation only", RUNBOOK.lower())
        self.assertIn("never run", RUNBOOK.lower())
        self.assertIn("this procedure in pi or github actions", RUNBOOK.lower())
        for topic in (
            "Healthchecks.io",
            "sops ansible/inventory/production/group_vars/all/secrets.sops.yml",
            "server_backups_enabled = true",
            "storage_box_bootstrap_external_reachability = true",
            "storage_box_bootstrap_external_reachability = false",
            "phase7-private.tfplan",
            "rfc4716",
            "ssh-keyscan",
            "SHA256:XqONwb1S0zuj5A1CDxpOSuD2hnAArV1A3wKY7Z3sdgM",
            "PRUNE_RESTIC_",
            "INITIALIZE_RESTIC_",
            "test-backup-restore.sh",
            "survivability-backup.service",
            "staleness",
        ):
            self.assertIn(topic, RUNBOOK, topic)

    def test_roadmap_and_status_documents_record_phase7_readiness(self):
        phase7 = ROADMAP[
            ROADMAP.index("### Phase 7:"):ROADMAP.index("### Phase 8:")
        ]
        self.assertIn("Status: next.", phase7)
        self.assertIn("Operator-run sequence:", phase7)
        self.assertIn("hcloud_storage_box_subaccount", phase7)
        self.assertIn("storage_box_bootstrap_external_reachability", phase7)
        self.assertIn("External Storage Box\nreachability is disabled", ROADMAP)
        self.assertIn("Phase 7, backup foundation, is implemented", AGENTS)
        self.assertIn("Phase 7, backup foundation, is implemented", README)
        self.assertIn("CONFIRM_RESTORE_TEST_", AGENTS)
        self.assertIn("phase7-backup-foundation.md", README)


if __name__ == "__main__":
    unittest.main()

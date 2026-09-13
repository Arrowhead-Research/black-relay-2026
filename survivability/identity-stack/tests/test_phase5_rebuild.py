"""Credential-free assertions for the guarded Phase 5 production rebuild."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "operator" / "rebuild-production.sh"
RUNBOOK = ROOT / "docs" / "runbooks" / "phase5-cpx32-rebuild.md"


class Phase5RebuildTests(unittest.TestCase):
    def test_script_requires_exact_resource_specific_confirmation(self):
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('CONFIRM_REBUILD:-', script)
        self.assertIn('== "${EXPECTED_SERVER_ID}"', script)
        self.assertIn("EXPECTED_PRIMARY_IPV4_ID", script)
        self.assertIn("EXPECTED_PRIMARY_IPV4", script)
        self.assertNotIn("latest", script.lower())

    def test_script_checks_server_ip_and_snapshot_identity_before_mutation(self):
        script = SCRIPT.read_text(encoding="utf-8")
        for expected in (
            '.server_type.name == "cpx32"',
            '.server_type.architecture == "x86"',
            '.location.name == "hel1"',
            '.protection.delete == true',
            '.protection.rebuild == true',
            '.auto_delete == false',
            '.assignee_type == "server"',
            '.labels.status == "validated"',
            '.labels.os == "debian-13"',
            '.result == "PASS"',
        ):
            self.assertIn(expected, script)

        self.assertNotIn(".datacenter.location.name", script)

        first_disable = script.index(
            'hcloud server disable-protection "${EXPECTED_SERVER_ID}" rebuild'
        )
        for check in (
            "server_json=",
            "primary_ip_json=",
            "image_json=",
            "IMPORTED_STATE_VERIFIED",
            "CONSOLE_RECOVERY_VERIFIED",
        ):
            self.assertLess(script.index(check), first_disable)

    def test_only_rebuild_protection_is_temporarily_disabled_and_restored(self):
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("trap cleanup EXIT INT TERM", script)
        self.assertIn("restore_rebuild_protection()", script)
        self.assertIn(
            'hcloud server enable-protection "${EXPECTED_SERVER_ID}" delete rebuild',
            script,
        )
        self.assertIn("REBUILD_PROTECTION_DISABLED=true", script)
        self.assertNotIn("disable-protection \"${EXPECTED_SERVER_ID}\" delete", script)
        self.assertNotIn("server delete", script)
        self.assertNotIn("primary-ip delete", script)

    def test_rebuild_uses_explicit_image_and_suppresses_generated_password(self):
        script = SCRIPT.read_text(encoding="utf-8")
        rebuild = (
            'hcloud server rebuild --quiet \\\n'
            '  --image "${SNAPSHOT_ID}" \\\n'
            '  --user-data-from-file "${USER_DATA}" \\\n'
            '  "${EXPECTED_SERVER_ID}" >/dev/null'
        )
        self.assertIn(rebuild, script)
        self.assertIn('chmod 0600 "${USER_DATA}" "${KNOWN_HOSTS}"', script)
        self.assertIn("ssh_pwauth: false", script)
        self.assertIn("lock_passwd: true", script)

    def test_post_rebuild_checks_boot_access_and_unchanged_objects(self):
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertGreaterEqual(script.count("hcloud primary-ip describe"), 2)
        self.assertIn('(.image.id | tostring) == $image_id', script)
        self.assertIn('.status == "running"', script)
        self.assertIn('cloud-init status --wait', script)
        self.assertIn('test "${VERSION_ID}" = 13', script)
        self.assertIn('test "$(dpkg --print-architecture)" = amd64', script)
        self.assertIn('emergency_ssh: "PASS"', script)

    def test_runbook_keeps_execution_operator_only_and_requires_convergence(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("trusted operator workstation only", runbook.lower())
        self.assertIn("never run", runbook.lower())
        self.assertIn("this procedure in pi or github actions", runbook.lower())
        self.assertIn("CONFIRM_REBUILD", runbook)
        self.assertIn("all(.resource_changes[]?; .change.actions == [\"no-op\"])", runbook)
        self.assertIn("same server object", runbook)
        self.assertIn("There is no application data to archive", runbook)
        self.assertIn("do not issue a second rebuild", runbook)


if __name__ == "__main__":
    unittest.main()

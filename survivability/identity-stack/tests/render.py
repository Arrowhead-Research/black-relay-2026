"""Render Ansible role templates so tests can assert on real output.

The previous test suite asserted substrings against unrendered Jinja source,
which meant a broken conditional or an undefined variable shipped green. This
helper resolves a role's defaults the way Ansible does (lazily, with
interdependent values) and renders a template, so tests can check the bash and
configuration that actually reaches the host.

Fixture values are placeholders standing in for the operator's external
inventory and SOPS-backed variables. They are never real credentials.
"""
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "ansible" / "roles"

# Non-secret stand-ins for variables that live in the operator's external
# inventory or in SOPS. Shapes match what the roles expect; values do not
# resemble anything real.
FIXTURES = {
    "edge": {
        "playbook_dir": "/workspace/ansible/playbooks",
        "edge_acme_email": "acme@example.test",
        "pocket_id_domain": "id.example.test",
        "headscale_domain": "hs.example.test",
        "crowdsec_bouncer_api_key": "fixture-bouncer-api-key-not-a-real-value",
    },
    "identity_stack": {
        "playbook_dir": "/workspace/ansible/playbooks",
        "lldap_base_dn": "dc=example,dc=test",
        "lldap_admin_username": "operator",
        "lldap_admin_email": "operator@example.test",
        "pocket_id_domain": "id.example.test",
        "headscale_domain": "hs.example.test",
        "headscale_magic_dns_domain": "tail.example.test",
        "lldap_jwt_secret": "fixture-jwt-secret-not-a-real-value-0001",
        "lldap_key_seed": "fixture-key-seed-not-a-real-value-0002",
        "lldap_admin_password": "fixture-admin-password-not-real",
        "lldap_pocket_id_bind_password": "fixture-bind-password-not-real",
        "pocket_id_encryption_key": (
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        ),
        "headscale_oidc_client_id": "fixture-client-id",
        "headscale_oidc_client_secret": "fixture-client-secret-not-real",
        "pocket_id_smtp_username": "fixture-smtp-user",
        "pocket_id_smtp_password": "fixture-smtp-password-not-real",
        "pocket_id_smtp_from": "identity@example.test",
    },
    "backup_restic": {
        "inventory_hostname": "identity.example.test",
        "playbook_dir": "/workspace/ansible/playbooks",
        "backup_restic_storage_box_server": "u000000.your-storagebox.de",
        "backup_restic_storage_box_subaccount_username": "u000000-sub1",
        # Supplied from SOPS at run time. These match the shape the role
        # validates (backup_restic_healthchecks_url_pattern) and address the
        # discard prefix, so a rendered fixture can never ping anything real.
        "backup_restic_healthchecks_backup_url": (
            "https://hc-ping.example.invalid/00000000-0000-4000-8000-000000000001"
        ),
        "backup_restic_healthchecks_maintenance_url": (
            "https://hc-ping.example.invalid/00000000-0000-4000-8000-000000000002"
        ),
    },
    "host_firewall": {
        "inventory_hostname": "identity.example.test",
    },
    "host_baseline": {
        "inventory_hostname": "identity.example.test",
    },
    "operator_access": {
        "inventory_hostname": "identity.example.test",
        "operator_access_user": "survivor",
    },
}


def _environment():
    # Match ansible.builtin.template defaults: trim_blocks on, lstrip_blocks off.
    return Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=False,
    )


def role_vars(role, extra=None):
    """Resolve a role's defaults, including defaults that reference each other.

    Ansible evaluates variables lazily, so `defaults/main.yml` may define a
    value in terms of another. Render string values repeatedly until they stop
    changing; anything still unresolved raises through StrictUndefined.
    """
    defaults_file = ROLES / role / "defaults" / "main.yml"
    resolved = yaml.safe_load(defaults_file.read_text(encoding="utf-8")) or {}
    resolved.update(FIXTURES.get(role, {}))
    resolved.update(extra or {})

    env = _environment()
    for _ in range(10):
        changed = False
        for key, value in list(resolved.items()):
            if isinstance(value, str) and "{{" in value:
                rendered = env.from_string(value).render(**resolved)
                if rendered != value:
                    resolved[key] = rendered
                    changed = True
        if not changed:
            break
    else:  # pragma: no cover - only reachable on a circular reference
        raise AssertionError(f"{role} defaults did not converge")

    unresolved = [k for k, v in resolved.items() if isinstance(v, str) and "{{" in v]
    if unresolved:
        raise AssertionError(f"{role} defaults unresolved: {sorted(unresolved)}")
    return resolved


def render_template(role, template, extra=None):
    """Render one template from a role against its resolved defaults."""
    source = (ROLES / role / "templates" / template).read_text(encoding="utf-8")
    variables = role_vars(role, extra)
    return _environment().from_string(source).render(**variables)

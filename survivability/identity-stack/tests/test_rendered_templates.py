"""Execute what the roles actually install, rather than grepping their source.

Every role template is rendered with StrictUndefined, so an undefined variable
or a broken conditional fails here instead of on the host. Rendered shell is
parsed and linted, and the backup wrapper is run against stub commands to check
the behaviour that matters: that failures propagate and that scheduled work
never deletes snapshots.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

import render

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "ansible" / "roles"
PRODUCTION_VARS = (
    ROOT / "ansible" / "inventory" / "production" / "production-vars.yml"
)

# Templates that render to shell and must parse and lint cleanly.
SHELL_TEMPLATES = [
    ("backup_restic", "survivability-backup.sh.j2"),
]
# Templates containing no Jinja at all, lintable exactly as committed.
STATIC_SHELL_TEMPLATES = [
    ROLES / "host_firewall" / "templates" / "rollback-host-firewall.sh.j2",
]


# Aliases Headscale resolves itself, so they need no declaration in the policy.
AUTOGROUPS = {
    "autogroup:self",
    "autogroup:member",
    "autogroup:tagged",
    "autogroup:nonroot",
    "autogroup:internet",
}


def load_policy():
    """Parse the committed HuJSON policy as Headscale's parser would.

    HuJSON is JSON plus comments and trailing commas. The committed policy uses
    comments but no trailing commas, so stripping comments outside strings
    leaves strict JSON.
    """
    text = (ROLES / "identity_stack" / "files" / "headscale-policy.hujson").read_text()
    out, in_string, escaped, index = [], False, False, 0
    while index < len(text):
        char = text[index]
        if in_string:
            in_string = not (char == '"' and not escaped)
            escaped = char == "\\" and not escaped
        elif char == '"':
            in_string = True
        elif text[index:index + 2] == "//":
            index = text.find("\n", index)
            if index == -1:
                break
            continue
        out.append(char)
        index += 1
    return json.loads("".join(out))


def all_templates():
    for role_dir in sorted(ROLES.iterdir()):
        template_dir = role_dir / "templates"
        if not template_dir.is_dir():
            continue
        for template in sorted(template_dir.iterdir()):
            yield role_dir.name, template.name


class TemplatesRenderTests(unittest.TestCase):
    def test_every_role_template_renders_with_no_undefined_variables(self):
        for role, template in all_templates():
            with self.subTest(role=role, template=template):
                output = render.render_template(role, template)
                self.assertNotIn("{{", output)
                self.assertNotIn("{%", output)


class RenderedShellTests(unittest.TestCase):
    def assert_shell_is_clean(self, path):
        syntax = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        if shutil.which("shellcheck"):
            lint = subprocess.run(
                ["shellcheck", str(path)], capture_output=True, text=True
            )
            self.assertEqual(lint.returncode, 0, lint.stdout)

    def test_rendered_shell_templates_parse_and_lint(self):
        for role, template in SHELL_TEMPLATES:
            with self.subTest(role=role, template=template):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "rendered.sh"
                    path.write_text(render.render_template(role, template))
                    self.assert_shell_is_clean(path)

    def test_static_shell_templates_parse_and_lint_as_committed(self):
        for template in STATIC_SHELL_TEMPLATES:
            with self.subTest(template=template.name):
                source = template.read_text(encoding="utf-8")
                self.assertNotIn("{{", source, "expected no Jinja in this template")
                self.assert_shell_is_clean(template)


class ServiceConfigurationTests(unittest.TestCase):
    def compose_config(self, role, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            compose_path = tmp_path / "compose.yml"
            compose_path.write_text(
                render.render_template(role, "compose.yml.j2", extra=extra),
                encoding="utf-8",
            )
            if role == "identity_stack":
                for name, template in (
                    ("lldap.env", "lldap.env.j2"),
                    ("pocket-id.env", "pocket-id.env.j2"),
                ):
                    (tmp_path / name).write_text(
                        render.render_template(role, template, extra=extra),
                        encoding="utf-8",
                    )
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_path), "config", "--format", "json"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_edge_compose_publishes_only_public_web_and_loopback_ldap_ui(self):
        model = self.compose_config("edge")
        services = model["services"]
        self.assertEqual(set(services), {"caddy"})
        ports = services["caddy"]["ports"]
        public = {
            int(port["published"])
            for port in ports
            if port.get("host_ip") == "0.0.0.0"
        }
        loopback = {
            int(port["published"])
            for port in ports
            if port.get("host_ip") == "127.0.0.1"
        }
        self.assertEqual(public, {80, 443})
        self.assertEqual(loopback, {17170})
        self.assertTrue(services["caddy"]["read_only"])
        self.assertEqual(services["caddy"]["cap_drop"], ["ALL"])
        self.assertRegex(services["caddy"]["image"], r"@sha256:[0-9a-f]{64}$")

    def test_identity_services_publish_no_ports_and_backend_is_internal(self):
        model = self.compose_config("identity_stack")
        self.assertEqual(set(model["services"]), {"lldap", "pocket-id", "headscale"})
        for name, service in model["services"].items():
            with self.subTest(service=name):
                self.assertFalse(service.get("ports"))
                self.assertIn("ALL", service["cap_drop"])
                self.assertIsNotNone(service.get("healthcheck"))
                self.assertRegex(service["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertTrue(model["networks"]["identity_backend"]["external"])
        self.assertTrue(model["networks"]["identity_proxy"]["external"])

    def test_pocket_id_sync_is_group_gated_and_excludes_the_bind_user(self):
        environment = render.render_template("identity_stack", "pocket-id.env.j2")
        self.assertIn(
            "(memberOf=cn=black-relay,ou=groups,dc=example,dc=test)", environment
        )
        self.assertIn("(!(uid=pocket-id-bind))", environment)
        self.assertIn("LDAP_ADMIN_GROUP_NAME=pocketid-admins", environment)
        self.assertIn("LDAP_SOFT_DELETE_USERS=true", environment)
        self.assertIn("ALLOW_OWN_ACCOUNT_EDIT=false", environment)

    def test_unauthenticated_email_login_is_never_enabled(self):
        """Email must never become a standing alternative to a passkey.

        EMAIL_ONE_TIME_ACCESS_AS_UNAUTHENTICATED_ENABLED lets anyone at the
        sign-in page name a username and have a working login code mailed to
        that user's address. It is a tenant-wide passkey bypass gated only on a
        mailbox, so it must stay off whether or not SMTP is configured.
        """
        for enabled in (False, True):
            with self.subTest(smtp_enabled=enabled):
                environment = render.render_template(
                    "identity_stack",
                    "pocket-id.env.j2",
                    {"identity_stack_pocket_id_smtp_enabled": enabled},
                )
                self.assertIn(
                    "EMAIL_ONE_TIME_ACCESS_AS_UNAUTHENTICATED_ENABLED=false",
                    environment,
                )
                self.assertIn("WEBAUTHN_USER_VERIFICATION=required", environment)

    def test_smtp_is_absent_until_explicitly_enabled(self):
        """A half-configured relay must render no SMTP keys at all.

        Pocket ID treats an SMTP_HOST with no working credentials as a
        configured mailer, so a partially rendered block turns every one-time
        access send into a runtime failure instead of an unavailable feature.
        """
        environment = render.render_template("identity_stack", "pocket-id.env.j2")
        for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
            with self.subTest(key=key):
                self.assertNotIn(f"{key}=", environment)
        self.assertNotIn("EMAIL_ONE_TIME_ACCESS_AS_ADMIN_ENABLED", environment)

    def test_enabled_smtp_renders_a_complete_encrypted_relay(self):
        environment = render.render_template(
            "identity_stack",
            "pocket-id.env.j2",
            {"identity_stack_pocket_id_smtp_enabled": True},
        )
        settings = dict(
            line.split("=", 1)
            for line in environment.splitlines()
            if "=" in line and not line.startswith("#")
        )
        self.assertEqual(settings["SMTP_HOST"], "mail.smtp2go.com")
        self.assertEqual(settings["SMTP_PORT"], "587")
        self.assertEqual(settings["SMTP_TLS"], "starttls")
        self.assertEqual(settings["SMTP_USER"], "fixture-smtp-user")
        self.assertEqual(settings["SMTP_SKIP_CERT_VERIFY"], "false")
        self.assertEqual(settings["EMAIL_ONE_TIME_ACCESS_AS_ADMIN_ENABLED"], "true")
        self.assertEqual(settings["EMAIL_LOGIN_NOTIFICATION_ENABLED"], "true")
        # A credential carrying a line break would truncate here and turn the
        # rest of the file into unparseable keys, because Compose reads this
        # env_file with `format: raw`.
        self.assertEqual(settings["SMTP_PASSWORD"], "fixture-smtp-password-not-real")
        self.assertNotIn("none", {settings["SMTP_TLS"]})

    def test_group_model_keeps_the_membership_tier_distinct_from_teams(self):
        """The sync filter must gate on the membership tier, never on a team.

        A team-gated filter silently excludes every other team, and the failure
        presents as "user does not exist" rather than as a denial.
        """
        variables = render.role_vars("identity_stack")
        membership = variables["identity_stack_membership_group"]
        required = variables["identity_stack_required_groups"]
        teams = variables["identity_stack_team_groups"]
        capabilities = variables["identity_stack_capability_groups"]

        self.assertNotIn(membership, teams)
        self.assertIn(membership, required)
        self.assertIn(membership, variables["identity_stack_admin_memberships"])
        self.assertEqual(len(required), len(set(required)))
        for group in teams + capabilities:
            with self.subTest(group=group):
                self.assertIn(group, required)

        environment = render.render_template("identity_stack", "pocket-id.env.j2")
        self.assertIn(f"(memberOf=cn={membership},ou=groups,", environment)

    def test_ldap_search_filters_name_only_valid_attributes(self):
        """An underscore makes a filter unparseable, not merely unmatched.

        RFC 4512 allows letters, digits and hyphens in an attribute
        description. A filter naming anything else is rejected by the client
        before it reaches the directory, so the sync fails outright instead of
        returning no results, and the cause is invisible from the directory
        side.
        """
        environment = render.render_template("identity_stack", "pocket-id.env.j2")
        prefixes = ("LDAP_USER_SEARCH_FILTER=", "LDAP_USER_GROUP_SEARCH_FILTER=")
        filters = [
            line.split("=", 1)[1]
            for line in environment.splitlines()
            if line.startswith(prefixes)
        ]
        self.assertEqual(len(filters), len(prefixes))
        for ldap_filter in filters:
            attributes = re.findall(r"\(([^()=<>~]+)[=<>~]", ldap_filter)
            self.assertTrue(attributes, ldap_filter)
            for attribute in attributes:
                with self.subTest(attribute=attribute):
                    self.assertRegex(attribute, r"^[A-Za-z][A-Za-z0-9-]*$")

    def test_headscale_configuration_is_oidc_gated_and_default_deny(self):
        config = yaml.safe_load(
            render.render_template("identity_stack", "headscale-config.yml.j2")
        )
        self.assertEqual(config["node"]["expiry"], "24h")
        self.assertEqual(config["prefixes"]["v6"], "")
        self.assertTrue(config["oidc"]["only_start_if_oidc_is_available"])
        self.assertEqual(config["oidc"]["pkce"], {"enabled": True, "method": "S256"})
        self.assertEqual(
            config["oidc"]["scope"], ["openid", "profile", "email", "groups"]
        )
        self.assertEqual(config["oidc"]["allowed_groups"], ["headscale-users"])


    def test_policy_declares_every_principal_it_authorizes(self):
        policy = load_policy()

        # Omitting grants entirely is allow-all, so its presence is the
        # default-deny invariant; the contents are reviewed in the diff.
        self.assertIn("grants", policy)

        declared_groups = set(policy.get("groups", {}))
        declared_tags = set(policy.get("tagOwners", {}))
        declared_hosts = set(policy.get("hosts", {}))
        known = declared_groups | declared_tags | declared_hosts | AUTOGROUPS

        for rule in policy["grants"] + policy.get("ssh", []):
            self.assertTrue(rule["src"], "a rule with no source authorizes nothing")
            self.assertTrue(rule["dst"], "a rule with no destination authorizes nothing")
            for alias in rule["src"] + rule["dst"]:
                # Emails and CIDRs resolve at the host; every symbolic name
                # must be declared in this same file.
                if alias.startswith(("group:", "tag:", "autogroup:")) or (
                    "@" not in alias and "/" not in alias
                ):
                    self.assertIn(alias, known, f"{alias} is used but never declared")

    def test_detection_vlan_uses_its_dedicated_router_and_group(self):
        policy = load_policy()

        self.assertIn("group:detection", policy["groups"])
        self.assertEqual(policy["hosts"]["detection-vlan"], "10.73.200.0/24")
        self.assertEqual(
            policy["tagOwners"]["tag:detection-subnet-router"],
            ["group:survivability"],
        )
        self.assertEqual(
            policy["autoApprovers"]["routes"]["10.73.200.0/24"],
            ["tag:detection-subnet-router"],
        )
        self.assertEqual(
            policy["autoApprovers"]["routes"]["10.73.66.0/24"],
            ["tag:proxmox-subnet-router"],
            "the existing Proxmox route must remain independently owned",
        )

        detection_grants = [
            grant
            for grant in policy["grants"]
            if "detection-vlan" in grant["dst"]
        ]
        self.assertEqual(
            detection_grants,
            [
                {
                    "src": ["group:detection", "group:survivability"],
                    "dst": ["detection-vlan"],
                    "ip": ["*"],
                }
            ],
        )

    def test_survivability_reaches_every_shared_resource(self):
        policy = load_policy()

        tagged_resource_grants = [
            grant
            for grant in policy["grants"]
            if "autogroup:tagged" in grant["dst"]
            and "group:survivability" in grant["src"]
            and "*" in grant.get("ip", [])
        ]
        self.assertTrue(
            tagged_resource_grants,
            "tagged resources must remain reachable by group:survivability",
        )

        for destination in policy.get("hosts", {}):
            grants = [
                grant
                for grant in policy["grants"]
                if destination in grant["dst"]
                and "group:survivability" in grant["src"]
            ]
            self.assertTrue(
                grants,
                f"shared resource {destination} excludes group:survivability",
            )

    def test_no_grant_opens_the_whole_tailnet_or_the_internet(self):
        for grant in load_policy()["grants"]:
            for destination in grant["dst"]:
                self.assertNotIn(
                    destination,
                    ("*", "autogroup:internet", "autogroup:danger-all"),
                    "a wildcard destination defeats the default-deny policy",
                )
            self.assertTrue(
                grant.get("ip") or grant.get("app"),
                "a grant with neither ip nor app is rejected by Headscale",
            )

    def test_tailscale_ssh_never_grants_root_and_always_has_transport(self):
        policy = load_policy()

        def identities(aliases):
            """Resolve group aliases to the identities they contain.

            An `ssh` rule naming one person and a grant covering their whole
            group describe the same flow, so the two must be compared after
            expansion rather than as literal strings.
            """
            resolved = set()
            for alias in aliases:
                resolved.update(policy.get("groups", {}).get(alias, [alias]))
            return resolved

        for rule in policy.get("ssh", []):
            self.assertIn(rule["action"], ("accept", "check"))
            self.assertNotIn("root", rule["users"])
            self.assertNotIn("*", rule["users"])
            # Against a tagged host, autogroup:nonroot lets everyone the rule
            # admits log in as every other non-root account, including the
            # break-glass operator, whose passwordless sudo makes that
            # root-equivalent. It would also cover accounts added later.
            if any(destination.startswith("tag:") for destination in rule["dst"]):
                self.assertNotIn(
                    "autogroup:nonroot",
                    rule["users"],
                    "name the accounts a tagged destination may be entered as",
                )
            # Tailscale SSH refuses a destination the grants do not also reach
            # on TCP 22, and the failure is silent on the client.
            reachable = any(
                set(rule["dst"]) & set(grant["dst"])
                and identities(rule["src"]) & identities(grant["src"])
                and {"*", "tcp:22"} & set(grant.get("ip", []))
                for grant in policy["grants"]
            )
            self.assertTrue(reachable, f"no grant carries SSH to {rule['dst']}")

    def test_caddy_access_log_explicitly_deletes_credentials(self):
        caddyfile = render.render_template("edge", "Caddyfile.j2")
        for field in (
            "request>headers>Authorization delete",
            "request>headers>Cookie delete",
            "request>headers>Proxy-Authorization delete",
            "resp_headers>Set-Cookie delete",
        ):
            self.assertIn(field, caddyfile)
        self.assertIn("roll_keep_for 336h", caddyfile)


class BackupWrapperBehaviourTests(unittest.TestCase):
    """Run the rendered wrapper against stub commands.

    The wrapper refuses to run as non-root and requires its environment file, so
    `id` is shadowed on PATH and the file paths are redirected into a temporary
    directory through the render fixtures.
    """

    def build(self, tmp, restic_exit=0):
        tmp = Path(tmp)
        for name in ("bin", "stage", "srv"):
            (tmp / name).mkdir(parents=True, exist_ok=True)

        env_file = tmp / "restic.env"
        env_file.write_text(
            "SURVIVABILITY_HC_BACKUP_URL=http://127.0.0.1:9/backup\n"
            "SURVIVABILITY_HC_MAINTENANCE_URL=http://127.0.0.1:9/maint\n"
        )

        argv_log = tmp / "restic-argv.log"
        stubs = {
            "id": "#!/bin/sh\necho 0\n",
            "curl": "#!/bin/sh\nexit 0\n",
            "sqlite3": "#!/bin/sh\nexit 0\n",
            "restic": (
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >>"{argv_log}"\n'
                f"exit {restic_exit}\n"
            ),
        }
        for name, body in stubs.items():
            stub = tmp / "bin" / name
            stub.write_text(body)
            stub.chmod(0o755)

        wrapper = tmp / "survivability-backup"
        wrapper.write_text(
            render.render_template(
                "backup_restic",
                "survivability-backup.sh.j2",
                extra={
                    "backup_restic_env_file": str(env_file),
                    "backup_restic_staging_dir": str(tmp / "stage"),
                    "backup_restic_sqlite_roots": [str(tmp / "srv")],
                    "backup_restic_backup_paths": [str(tmp / "srv")],
                },
            )
        )
        return wrapper, argv_log

    def run_wrapper(self, wrapper, *args, path_prefix):
        env = dict(os.environ, PATH=f"{path_prefix}:{os.environ['PATH']}")
        return subprocess.run(
            ["bash", str(wrapper), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_failed_backup_exits_non_zero(self):
        """A failing restic must fail the unit, not just ping Healthchecks.

        Reading `$?` after an `if` block returns the status of the compound
        command, so a failed backup previously exited 0 and systemd recorded the
        timer as successful.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wrapper, _ = self.build(tmp, restic_exit=7)
            result = self.run_wrapper(
                wrapper, "backup", path_prefix=str(Path(tmp) / "bin")
            )
            self.assertEqual(result.returncode, 7, result.stderr)

    def test_successful_backup_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper, _ = self.build(tmp, restic_exit=0)
            result = self.run_wrapper(
                wrapper, "backup", path_prefix=str(Path(tmp) / "bin")
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_scheduled_backup_never_deletes_snapshots(self):
        """Assert on what restic was actually asked to do, not on source text."""
        with tempfile.TemporaryDirectory() as tmp:
            wrapper, argv_log = self.build(tmp, restic_exit=0)
            self.run_wrapper(wrapper, "backup", path_prefix=str(Path(tmp) / "bin"))
            invocations = argv_log.read_text()
            self.assertIn("backup", invocations)
            self.assertNotIn("forget", invocations)
            self.assertNotIn("prune", invocations)

    def test_prune_requires_the_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper, argv_log = self.build(tmp, restic_exit=0)
            bin_dir = str(Path(tmp) / "bin")

            refused = self.run_wrapper(
                wrapper, "prune", "WRONG", path_prefix=bin_dir
            )
            self.assertEqual(refused.returncode, 64)
            self.assertFalse(
                argv_log.exists() and "forget" in argv_log.read_text(),
                "restic must not be invoked without the confirmation",
            )

            expected = render.role_vars("backup_restic")[
                "backup_restic_required_prune_confirmation"
            ]
            accepted = self.run_wrapper(
                wrapper, "prune", expected, path_prefix=bin_dir
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("forget --prune", argv_log.read_text())

    def test_unknown_subcommand_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper, _ = self.build(tmp)
            result = self.run_wrapper(
                wrapper, "definitely-not-a-command", path_prefix=str(Path(tmp) / "bin")
            )
            self.assertEqual(result.returncode, 64)
            self.assertIn("usage:", result.stderr)


class OperatorAccessTests(unittest.TestCase):
    """The public SSH port is the break-glass path, not a second front door."""

    def sshd_config(self, extra=None):
        return render.render_template(
            "operator_access", "sshd-operator-access.conf.j2", extra
        )

    def allow_users(self, config):
        allowed = [
            line.split()[1:]
            for line in config.splitlines()
            if line.startswith("AllowUsers ")
        ]
        self.assertEqual(len(allowed), 1, "expected exactly one AllowUsers line")
        return allowed[0]

    def test_committed_tailnet_accounts_have_matching_policy_rules(self):
        production = yaml.safe_load(PRODUCTION_VARS.read_text(encoding="utf-8"))
        policy = load_policy()
        policy_users = [user for rule in policy["ssh"] for user in rule["users"]]
        managed_users = [production["operator_access_user"]] + [
            item["name"] for item in production["operator_access_tailnet_operators"]
        ]
        self.assertCountEqual(policy_users, managed_users)
        self.assertFalse(
            set(managed_users) & set(production["operator_access_removed_users"])
        )

    def test_public_ssh_admits_only_the_break_glass_operator(self):
        config = self.sshd_config(
            {
                "operator_access_tailnet_operators": [
                    {"name": "jake", "sudo": True},
                    {"name": "marcel", "sudo": True},
                ]
            }
        )
        self.assertEqual(self.allow_users(config), ["survivor"])
        # A teammate reaches this host over the tailnet or not at all.
        self.assertNotIn("jake", config)
        self.assertNotIn("marcel", config)

    def test_root_is_admitted_only_during_bootstrap(self):
        bootstrap = self.sshd_config({"operator_access_allow_root_ssh": True})
        self.assertIn("root", self.allow_users(bootstrap))
        self.assertIn("PermitRootLogin prohibit-password", bootstrap)

        converged = self.sshd_config({"operator_access_allow_root_ssh": False})
        self.assertNotIn("root", self.allow_users(converged))
        self.assertIn("PermitRootLogin no", converged)

    def test_password_authentication_is_never_available(self):
        """Tailnet accounts carry a locked password and no key of their own."""
        config = self.sshd_config()
        self.assertIn("PasswordAuthentication no", config)
        self.assertIn("AuthenticationMethods publickey", config)


class TailnetNodeTests(unittest.TestCase):
    """The VPS enrolls as a tagged node, and the tag is what authorizes it."""

    def defaults(self):
        return render.role_vars("tailnet_node")

    def test_package_source_is_signed_and_pinned(self):
        source = render.render_template("tailnet_node", "tailscale.list.j2")
        entry = [
            line for line in source.splitlines() if line.startswith("deb ")
        ]
        self.assertEqual(len(entry), 1, "expected exactly one apt source entry")
        # An unsigned or plaintext source would let anything on the path choose
        # what tailscaled is built from.
        self.assertIn("signed-by=/usr/share/keyrings/", entry[0])
        self.assertIn("https://", entry[0])
        self.assertNotIn("http://", entry[0])
        self.assertIn(self.defaults()["tailnet_node_repository_suite"], entry[0])

    def test_client_version_is_pinned_exactly(self):
        version = self.defaults()["tailnet_node_package_version"]
        # A floating or ranged pin would let the client outrun the capability
        # versions the deployed Headscale knows.
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_declared_tag_is_owned_by_the_committed_policy(self):
        """A tag Headscale does not recognise yields an unusable node.

        The preauthorized key carries this tag, and the grant and SSH rule are
        written against it. If the two files drift apart, enrollment produces a
        node that reaches nothing and Tailscale SSH refuses silently.
        """
        tag = self.defaults()["tailnet_node_tag"]
        policy = load_policy()
        self.assertIn(tag, policy["tagOwners"])
        self.assertTrue(
            policy["tagOwners"][tag], f"{tag} is declared with no owner"
        )
        reachable = any(
            tag in rule["dst"] for rule in policy["grants"] + policy.get("ssh", [])
        )
        self.assertTrue(reachable, f"{tag} is declared but authorizes nothing")

    def test_the_host_never_delegates_its_own_dns_to_the_tailnet(self):
        """ACME, backups, and apt must not depend on tailscaled resolving."""
        self.assertFalse(self.defaults()["tailnet_node_accept_dns"])


class FirewallPolicyTests(unittest.TestCase):
    def policy_path(self):
        return ROLES / "host_firewall" / "templates" / "survivability.nft.j2"

    @unittest.skipUnless(
        shutil.which("nft") and os.geteuid() == 0,
        "nft --check needs the nftables binary and netlink access (root)",
    )
    def test_policy_passes_nft_check(self):
        """Only runs for a root operator.

        `nft --check` initialises a netlink cache, so it cannot run in the
        unprivileged Pi container or in CI. The host_firewall role already runs
        this same check on the target host before activating a candidate policy,
        which is the authoritative gate; this is a convenience for local work.
        """
        result = subprocess.run(
            ["nft", "--check", "--file", str(self.policy_path())],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_policy_scopes_itself_and_never_flushes_docker(self):
        policy = self.policy_path().read_text(encoding="utf-8")
        self.assertIn("table inet survivability_filter", policy)
        self.assertIn("flush table inet survivability_filter", policy)
        # `flush ruleset` would drop Docker's own tables along with ours.
        self.assertNotIn("flush ruleset", policy)


if __name__ == "__main__":
    unittest.main()

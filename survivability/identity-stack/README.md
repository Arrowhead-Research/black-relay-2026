# Survivability Identity Stack

Infrastructure as code for the Survivability research project's cloud identity
and private-access platform, running on one Helsinki CPX32 for roughly 30-50
research users:

- LLDAP provides users and groups.
- Pocket ID synchronizes LDAP identities and provides passkey-based OIDC.
- Headscale authenticates through Pocket ID and coordinates private access.
- Caddy provides the public HTTPS edge.

OpenTofu adopts and manages the protected infrastructure, a frozen Packer
snapshot provides the base image for disaster recovery, and Ansible owns host
configuration and service deployment.

See [`ARCHITECTURE_ROADMAP.md`](ARCHITECTURE_ROADMAP.md) for the architecture,
accepted risks, phase status, and recovery objectives. It is the single source
of truth for what is done and what is next.

## Trust boundary

The Pi development container has no production authority. It must not receive:

- Hetzner, Cloudflare, Backblaze, SMTP, or monitoring credentials.
- OpenTofu state credentials or encryption keys.
- SSH private keys or agent sockets.
- age private keys, decrypted SOPS values, TOTP seeds, or passkey recovery data.
- The host Docker socket or production environment files.

Pi may edit code, documentation, tests, safe examples, and public recipient
configuration. Privileged Packer, OpenTofu, SOPS, Ansible, backup, restore, and
rebuild commands run only from a trusted operator workstation.

Read [`AGENTS.md`](AGENTS.md) before making changes.

## Start Pi Web

Run from the trusted host:

```bash
cd /home/robbie/pi-dev-coding/workspaces/black-relay-2026/survivability/identity-stack
make dev-build
make dev-up
make dev-status
```

Open <http://127.0.0.1:8504>. Configure only the AI-provider authentication
needed by Pi through `/login`.

## Credential-free development commands

```bash
make lint       # Formatting plus YAML, shell, JSON, and Ansible linting
make validate   # Compose, Packer, OpenTofu, Ansible, and executable safety checks
```

These may run inside Pi. The `make dev-*` commands run on the host because Pi
has no Docker socket: `dev-build`, `dev-up`, `dev-status`, `dev-logs`,
`dev-doctor`, `dev-shell`, `dev-restart`, `dev-down`.

No default Make target contacts production or performs infrastructure changes.

## Runbooks

Operator procedures live in [`docs/runbooks/`](docs/runbooks/). Every command is
labeled with where it runs.

| Runbook | Covers |
| --- | --- |
| [`operator-access.md`](docs/runbooks/operator-access.md) | SSH operator bootstrap, key rotation, recovery |
| [`host-configuration.md`](docs/runbooks/host-configuration.md) | Host baseline and guarded nftables activation |
| [`backups.md`](docs/runbooks/backups.md) | Backup setup, retention, verification, restore test |
| [`opentofu-adoption.md`](docs/runbooks/opentofu-adoption.md) | B2 backend, resource import, protected planning |
| [`server-rebuild.md`](docs/runbooks/server-rebuild.md) | Guarded CPX32 rebuild and console recovery |
| [`gold-image.md`](docs/runbooks/gold-image.md) | Frozen base image; rebuild only when the base changes |
| [`secrets.md`](docs/runbooks/secrets.md) | age identities, SOPS files, teammate onboarding, rotation |
| [`service-deployment.md`](docs/runbooks/service-deployment.md) | Caddy, CrowdSec, LLDAP, Pocket ID, Headscale deployment |
| [`user-lifecycle.md`](docs/runbooks/user-lifecycle.md) | user onboarding, denial checks, offboarding, passkey recovery |

## Targeted convergence

`ansible/playbooks/site.yml` converges every role and is the documented way to
apply a change. A full run installs packages, patches the host, pulls images,
reproves the identity chain, and takes on the order of fifteen minutes, which is
more than a routine edit needs. Tags narrow a run to the roles and tasks a
change can actually have affected.

Run the whole playbook with no `--tags` whenever you are unsure, after a
rebuild, or before recording that a change is deployed.

### Selection tags

| Tag | Converges |
| --- | --- |
| `access` | `operator_access`: the named account, its keys, sudo, and SSH policy |
| `baseline` | `host_baseline`: packages, journald, sysctl, unattended upgrades |
| `firewall` | `host_firewall`: the guarded nftables policy |
| `backup` | `backup_restic`: restic configuration, wrapper, timers |
| `edge` | `edge`: Caddy, CrowdSec, the bouncer, and the shared networks |
| `identity` | `identity_stack` in full |
| `identity-config` | Identity directories, rendered credentials, Headscale policy and configuration, the Compose project, and the service convergence — everything except the LLDAP directory bootstrap |
| `identity-policy` | The Headscale policy and configuration only: install, validate with `configtest`, then restart Headscale |
| `identity-lldap` | The LLDAP groups, bind user, and group memberships, the read-only proof, and the service convergence |
| `tailnet` | `tailnet_node`: the pinned Tailscale client, its declared preferences, and the proof that this host is enrolled under its tag |

Every identity selection first decrypts the SOPS store and revalidates the
configuration, and the host and elevation gates in `site.yml` run under every
selection. Tags compose, so `--tags edge,identity` converges both.

### Skip tags

These two narrow a run without changing which roles it covers. They only ever
remove work, so use them with `--skip-tags`.

| Tag | Removes |
| --- | --- |
| `verify` | The proof tasks: the Headscale `configtest`, the LLDAP read-only `ldapsearch`/`ldapmodify` probes, the Headscale health check, and the restic repository check |
| `baseline-updates` | `apt upgrade`, normally the longest single task in a run |

### Examples

All of these run from a trusted operator workstation, through the secrets
wrapper described in [`secrets.md`](docs/runbooks/secrets.md).

```bash
# Full convergence. The default, and what the runbooks mean by "apply".
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml

# Apply an edited ansible/roles/identity_stack/files/headscale-policy.hujson.
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --tags identity-policy \
  ansible/playbooks/site.yml

# Add or change LLDAP groups and memberships.
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --tags identity-lldap \
  ansible/playbooks/site.yml

# Redeploy the services after a Caddyfile or Compose change.
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --tags edge,identity-config \
  ansible/playbooks/site.yml

# Iterate quickly: skip package patching and the proof tasks.
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" \
  --skip-tags verify,baseline-updates ansible/playbooks/site.yml
```

Preview any selection without contacting the host, from Pi or a workstation:

```bash
ansible-playbook -i "$ANSIBLE_INVENTORY" --list-tags ansible/playbooks/site.yml
ansible-playbook -i "$ANSIBLE_INVENTORY" --list-tasks --tags identity-policy \
  ansible/playbooks/site.yml
```

### What tags do not do

- **They assume a converged host.** A selection skips the roles that create the
  Docker networks, the operator account, and the firewall, so it is only valid
  once a full run has succeeded. After a rebuild, run `site.yml` untagged.
- **`verify` is skip-only.** Running `--tags verify` alone fails: the proof
  tasks consume values registered by the tasks they verify.
- **Skipping `verify` weakens the exit-criteria evidence.** A run recorded
  against `ARCHITECTURE_ROADMAP.md` or a runbook should not skip it.
- **A policy change restarts Headscale.** The policy and configuration files are
  bind-mounted and Headscale reads them only at startup, so `identity-policy`
  ends by restarting the service. Established tunnels are unaffected; node
  registration and reauthentication pause until it is healthy again.

## Safe repository inputs

`ansible/inventory/production/hosts.yml` intentionally contains no production
host details and exists only for credential-free Pi and CI checks. Reviewable,
non-secret desired state lives in the committed
`ansible/inventory/production/production-vars.yml`. Before running production
Ansible, an operator copies `ansible/inventory/production/hosts.example.yml` to
a mode-`0600` file outside the repository, such as
`~/.config/survivability/production-hosts.yml`, and supplies only the production
address and workstation-specific SSH key paths. Ansible reads the adjacent
`.pub` files itself; private-key content never belongs in inventory variables.

`secrets.example.yml` files contain variable names and placeholder values only.
Operators create and edit encrypted `*.sops.yml` files from trusted
workstations; plaintext values are never committed or sent to Pi.

SOPS with age is the authoritative secrets system. Git stores only ciphertext
and the public age recipients in `.sops.yaml`; every operator has an individual
age identity, and 1Password, if used at all, protects only that personal key.
`secrets/tooling.env.example` documents the infrastructure credential names with
empty values — never populate that tracked file. The real values live encrypted
in `secrets/tooling.sops.env`, and `scripts/operator/with-secrets.sh` injects
them into a single child process. See
[`docs/runbooks/secrets.md`](docs/runbooks/secrets.md).

OpenTofu state uses a manually bootstrapped, versioned Backblaze B2 bucket with
enforced client-side encryption. The design deliberately accepts no dependable
distributed lock, so only one operator may run OpenTofu at a time.

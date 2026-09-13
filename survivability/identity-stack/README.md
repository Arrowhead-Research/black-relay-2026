# Survivability Identity Stack

Infrastructure as code for the Survivability research project's cloud identity
and private-access platform:

- LLDAP provides users and groups.
- Pocket ID synchronizes LDAP identities and provides passkey-based OIDC.
- Headscale authenticates through Pocket ID and coordinates private access.
- Caddy with Coraza provides the public HTTPS edge.

The repository owns the lifecycle of an existing empty Helsinki CPX32 and its
Primary IPv4. Packer will build a Debian 13 gold image, OpenTofu will adopt and
manage protected infrastructure, and Ansible will harden the host and deploy the
services. See [`ARCHITECTURE_ROADMAP.md`](ARCHITECTURE_ROADMAP.md) for the
complete architecture, accepted risks, phases, and recovery objectives.

## Current status

Phases 1 through 6 are complete. The obsolete migration workflow is gone, the
pinned credential-free toolchain passed trusted-host and GitHub Actions checks,
and a trusted operator completed protected OpenTofu adoption. A generic Debian
13 x86 gold image was built and validated, and the existing CPX32 was rebuilt in
place without losing its protected Primary IPv4.

The operator completed Phase 6 production verification: dedicated-key named
access and sudo work, the manual FIDO2 recovery key remains authorized, root and
password SSH are disabled, and the declarative host baseline and guarded
nftables policy are active. TCP 22/80/443 plus required ICMP exposure,
Docker-DNAT enforcement, timed rollback safety, fresh SSH, and final no-change
`site.yml` convergence were confirmed.

Phase 7, backup foundation, is implemented and awaits trusted-operator
execution per
[`docs/runbooks/phase7-backup-foundation.md`](docs/runbooks/phase7-backup-foundation.md):
restic over SFTP to a home-scoped Storage Box subaccount with external
reachability disabled after reviewed key bootstrap, root-only
credentials rendered from SOPS, guarded daily backup and weekly verification
timers with separate Healthchecks.io signals, exact operator confirmations for
repository initialization and retention pruning, and an isolated disposable-
server restore test. CrowdSec moves to Phase 8 edge/log integration, while
email delivery for host status moves to Phase 11 monitoring. No application
deployment is available yet.

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

GitHub Actions may run credential-free checks and publish the Caddy/Coraza image
using repository-scoped GHCR permission. It receives no provider, state, SSH,
SOPS, or deployment credentials.

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

A useful first prompt is:

```text
Read AGENTS.md and ARCHITECTURE_ROADMAP.md. Summarize the current phase and
propose the smallest next change without accessing production or requesting
credentials.
```

## Credential-free development commands

```bash
make lint       # Packer/OpenTofu formatting plus YAML, shell, JSON, and Ansible linting
make validate   # Compose, Packer, OpenTofu, Ansible, and safety validation
```

These commands may run inside Pi. The `make dev-*` commands below run on the
host because Pi has no Docker socket:

```bash
make dev-build
make dev-up
make dev-status
make dev-logs
make dev-doctor
make dev-shell
make dev-restart
make dev-down
```

No default Make target contacts production or performs infrastructure changes.
Future authoritative commands must be clearly labeled as operator-only and
protected according to the roadmap.

## Safe repository inputs

`ansible/inventory/production/hosts.yml` intentionally contains no production
host details and is only for credential-free Pi/CI checks. Before production
Ansible, an operator copies
`ansible/inventory/production/hosts.example.yml` to a mode-`0600` file such as
`~/.config/survivability/production-hosts.yml` and replaces the access
placeholders: the host, named operator, existing manual/FIDO2 key path, and
dedicated Ansible key path. Phase 7 adds three non-secret backup
placeholders in the same file: the Storage Box endpoint and subaccount
username from `tofu output`, plus the pinned port 22 known_hosts entry. Ansible
reads the two adjacent `.pub` files, so public-key content is
not copied into variables. Only local paths—not key content—belong in the
inventory. After the dedicated public key is authorized for the existing root
account, the one-time bootstrap uses it for both root and named-operator
connections. See
[`docs/runbooks/phase6-access-bootstrap.md`](docs/runbooks/phase6-access-bootstrap.md)
for the concise operator procedure. Routine host configuration is documented in
[`docs/runbooks/phase6-host-baseline.md`](docs/runbooks/phase6-host-baseline.md),
and guarded nftables activation is documented in
[`docs/runbooks/phase6-host-firewall.md`](docs/runbooks/phase6-host-firewall.md).

`secrets.example.yml` files contain variable names and placeholder values only.
Operators create and edit encrypted `*.sops.yml` files from trusted workstations;
plaintext values are never committed or sent to Pi. The Phase 7 backup secrets
are created from
`ansible/inventory/production/group_vars/all/secrets.example.yml`; the
committed `secrets.sops.yml` file stays SOPS-encrypted and is rendered
root-only by the `backup_restic` role.

OpenTofu state uses a manually bootstrapped, versioned Backblaze B2 bucket with
enforced client-side encryption. The design deliberately accepts no dependable
distributed lock, so only one operator may run OpenTofu at a time. See
[`docs/runbooks/phase3-opentofu-adoption.md`](docs/runbooks/phase3-opentofu-adoption.md)
for the trusted-workstation procedure. The separately confirmed rebuild is
specified in
[`docs/runbooks/phase5-cpx32-rebuild.md`](docs/runbooks/phase5-cpx32-rebuild.md).

`operator.env.example` documents trusted-workstation variable names with empty
values. Never populate that tracked file; copy it outside the repository and
restrict its permissions before entering credentials.

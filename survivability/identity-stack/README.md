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

Phase 1, architecture and workspace reset, is complete. The previous
personal-account migration and server-audit workflow has been removed. No Packer
build, OpenTofu import, server rebuild, or production deployment is available
yet.

Phase 2's repository changes pin the credential-free Packer, OpenTofu, Ansible,
provider, and linting toolchain. A trusted-host development-image rebuild,
`make dev-doctor`, and the GitHub Actions run remain before the phase is marked
complete. Work proceeds one roadmap phase at a time.

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
cd /home/robbie/pi-dev-coding/workspaces/hetzner-identity-stack
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
host details. `secrets.example.yml` files contain variable names and placeholder
values only. Operators create and edit encrypted `*.sops.yml` files from trusted
workstations; plaintext values are never committed or sent to Pi.

OpenTofu state will use a manually bootstrapped, versioned Backblaze B2 bucket
with client-side encryption. The design deliberately accepts no dependable
distributed lock, so only one operator may run OpenTofu at a time.

`operator.env.example` documents trusted-workstation variable names with empty
values. Never populate that tracked file; copy it outside the repository and
restrict its permissions before entering credentials.

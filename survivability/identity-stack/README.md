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

Phases 1 through 4 are complete. The obsolete migration workflow is gone, the
pinned credential-free toolchain passed trusted-host and GitHub Actions checks,
and a trusted operator completed protected OpenTofu adoption. The existing
CPX32 and independent Primary IPv4 are managed without replacement, and the
firewall, DNS records, encrypted B2 state, and protected BX11 have converged. A
generic Debian 13 x86 gold image was built, validated before and after reboot,
and promoted by explicit numeric snapshot ID; all disposable servers were
removed.

Phase 5, the separately confirmed in-place CPX32 rebuild from that validated
snapshot, is next. No application deployment is available yet.

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
host details. `secrets.example.yml` files contain variable names and placeholder
values only. Operators create and edit encrypted `*.sops.yml` files from trusted
workstations; plaintext values are never committed or sent to Pi.

OpenTofu state uses a manually bootstrapped, versioned Backblaze B2 bucket with
enforced client-side encryption. The design deliberately accepts no dependable
distributed lock, so only one operator may run OpenTofu at a time. See
[`docs/runbooks/phase3-opentofu-adoption.md`](docs/runbooks/phase3-opentofu-adoption.md)
for the trusted-workstation procedure.

`operator.env.example` documents trusted-workstation variable names with empty
values. Never populate that tracked file; copy it outside the repository and
restrict its permissions before entering credentials.

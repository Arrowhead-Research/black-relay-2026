# Agent Instructions

## Purpose

This workspace defines the complete infrastructure lifecycle for the
Survivability research project's identity and private-access stack. Read
`ARCHITECTURE_ROADMAP.md` before proposing or implementing infrastructure
changes.

The target is the existing empty Helsinki CPX32 and its assigned Primary IPv4.
Packer builds a Debian 13 gold image, OpenTofu adopts and manages infrastructure,
and Ansible hardens the host and deploys LLDAP, Pocket ID, Headscale, and the
Caddy/Coraza edge.

## Current Phase

Phases 1 through 6 are complete. The obsolete personal-account migration
workflow was removed, the credential-free tooling foundation passed its checks,
a trusted operator completed protected OpenTofu adoption, the generic Debian 13
gold image was built and validated, and the existing CPX32 was rebuilt in place
without losing its protected Primary IPv4.

A trusted operator completed Phase 6 production verification: dedicated-key
named access and sudo work, the manual FIDO2 recovery key remains authorized,
root and password SSH are disabled, the host baseline is active, and the guarded
nftables policy permits only TCP 22/80/443 plus required ICMP while enforcing
the Docker-DNAT boundary. Candidate validation, timed rollback, fresh SSH proof,
and final no-change `site.yml` convergence succeeded. Do not recreate the old
production audit/migration workflow or the discarded multi-person SSH
bootstrap.

Phase 7, backup foundation, is implemented and awaits trusted-operator
execution per `docs/runbooks/phase7-backup-foundation.md`. OpenTofu adds the
home-scoped SFTP-only restic subaccount on the protected BX11 over port 22.
External Storage Box reachability stays disabled after its short reviewed key-
enrollment bootstrap. The `backup_restic` role renders root-only restic
credentials from SOPS and
schedules guarded daily backup and weekly verification timers with separate
Healthchecks.io signals, while repository initialization and retention pruning
require exact operator confirmations. The isolated restore test runs only on
a disposable server with an exact confirmation value. CrowdSec is assigned to
Phase 8 alongside SSH/Caddy log integration, and email delivery for
pending-reboot and disk-pressure status is assigned to Phase 11 monitoring.
Only a trusted operator may use SSH, SOPS, production Ansible, OpenTofu,
Healthchecks.io, or BX11 credentials.

## Credential and authority boundary

This Pi container is an untrusted development environment. It must never receive
production authority.

- Never mount or request an age private key.
- Never mount or request an SSH agent socket or SSH private key.
- Never mount the host Docker socket.
- Never request or accept Hetzner, Cloudflare, Backblaze, SMTP, monitoring, or
  other production credentials in chat.
- Never request or persist an OpenTofu state-encryption key, restic repository
  password, TOTP seed, passkey recovery material, or decrypted secret.
- Never print, log, or persist decrypted secret values.
- Never edit `*.sops.yml` files directly. Operators edit them with SOPS from a
  trusted workstation.
- Work with `secrets.example.yml`, variable names, and non-sensitive fixture
  values only.
- Never connect to, audit, import, rebuild, or change the Hetzner server or any
  production provider account.
- Never initialize or access the Backblaze state backend.
- Never run authoritative Packer, OpenTofu, or production Ansible commands.

The public age recipients in `.sops.yaml`, public SSH keys, resource labels, and
non-sensitive variable names may be committed when intentionally reviewed.
Private keys and provider credentials may not.

All secret-bearing and authoritative commands are run manually by an operator
from a trusted workstation. Do not suggest bypassing this boundary for
convenience.

## Engineering rules

- Prefer small, idempotent Ansible roles, explicit playbooks, and narrowly scoped
  OpenTofu modules.
- Keep Packer images generic. Put users, access policy, service configuration,
  and secrets in Ansible.
- Use OpenTofu, not Terraform.
- Treat the existing CPX32 and Primary IPv4 as adopted protected resources.
  Normal plans must not replace or delete them.
- Put `prevent_destroy` and provider-side protection on critical resources where
  supported.
- Keep destructive actions outside default workflows and require exact,
  resource-specific confirmation values.
- Model Backblaze B2 state as single-writer because dependable distributed
  locking is not assumed. Do not claim that local locking coordinates multiple
  workstations.
- Pin Packer plugins, OpenTofu providers, Ansible collections, package sources,
  and container images. Production containers use reviewed immutable digests.
- Use `no_log: true` and `diff: false` for every task that handles secrets.
- Render secrets root-owned with mode `0600` or stricter.
- Keep edge and identity Compose projects separate and expose application HTTP
  only through Caddy.
- Preserve default-deny firewall and Headscale policy behavior.
- Start Coraza in detection mode; do not enable blocking without representative
  flow tests and reviewed exclusions.
- Do not add Watchtower, unattended production container updates, embedded DERP,
  public backend ports, or an unnecessary database service.
- Keep Fluent Bit installed but disabled until an external destination is
  approved and configured.
- Keep v1 within the roadmap boundary; do not introduce Infisical, CoreDNS,
  Proxmox routes, PostgreSQL, HA, or other deferred features early.
- Add credential-free tests for templates, policy, validation, and destructive
  safeguards.
- Run `make lint` and `make validate` before presenting implementation changes.
- Update the roadmap whenever a phase completes, an accepted risk changes, or an
  architectural decision changes.

## GitHub Actions boundary

GitHub Actions may run credential-free formatting, linting, policy checks,
template tests, and dependency-update proposals. It may also build, scan,
generate an SBOM for, and publish the Caddy/Coraza image using only
repository-scoped GHCR package permission.

GitHub Actions must never receive Hetzner, Cloudflare, Backblaze, SOPS, SSH,
restic, OpenTofu-state, or production deployment credentials. Publishing an
artifact does not authorize deploying it; production uses a reviewed digest in
an operator-run Ansible deployment.

## Operator-only operations

The following always remain outside Pi:

- Creating the B2 bucket, application key, and state-encryption material.
- Importing the CPX32 and Primary IPv4 into OpenTofu state.
- Running Packer builds or disposable Hetzner test infrastructure.
- Running OpenTofu plan/apply against provider accounts.
- Rebuilding the CPX32 from the validated snapshot.
- Creating or decrypting SOPS files.
- Generating backup credentials or creating Healthchecks.io checks.
- Rotating production SSH credentials or enrolling Pocket ID passkeys.
- Running production Ansible check/deploy.
- Accessing BX11 or performing production backup/restore operations.
- Running the isolated Phase 7 restore test.

Documentation must clearly label commands by execution location. A command that
needs production authority must not be represented as runnable from Pi.

## Safety requirements for future implementation

- The CPX32 rebuild requires `CONFIRM_REBUILD=<expected-server-id>` and checks
  server identity, type, Helsinki location, and Primary IPv4 before proceeding.
- The rebuild must preserve the server object and independent Primary IPv4 and
  must restore provider protection immediately.
- Separate confirmations are required for destructive firewall replacement,
  protected-resource deletion, backup pruning, restore-over-production, and
  identity-database reset.
- Disposable test resources require owner/expiry labels and documented teardown.
- Quarterly restore tests use isolated temporary infrastructure, never
  production.
- The Phase 7 restore test requires CONFIRM_RESTORE_TEST_<inventory
  hostname>, targets only a disposable server, and restores into a temporary
  directory; it must never run against the production host.
- No production deployment is complete until OpenTofu and Ansible are
  idempotent and recovery, external monitoring, and backup alerts are tested.

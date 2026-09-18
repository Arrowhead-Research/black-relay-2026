# Agent Instructions

## Purpose

This workspace defines the complete infrastructure lifecycle for the
Survivability research project's identity and private-access stack. Read
`ARCHITECTURE_ROADMAP.md` before proposing or implementing infrastructure
changes.

The target is one Helsinki CPX32 and its protected Primary IPv4, serving roughly
30-50 research users. OpenTofu adopts and manages the infrastructure, a frozen
Packer snapshot provides the base image for disaster recovery, and Ansible owns
the host configuration and deploys LLDAP, Pocket ID, Headscale, and the Caddy
edge.

`ARCHITECTURE_ROADMAP.md` is the single source of truth for phase status. Do not
restate phase status here or in `README.md`.

## Proportion

This is one server for a small research project with a 24-hour RPO and a
manual 4-hour RTO. Guard rails must earn their keep.

Cheap machinery that prevents irreversible loss is welcome: `prevent_destroy`,
provider-side delete protection, fail-closed defaults, and automatic rollback.
Machinery that only restates intent is not: per-phase runbooks, per-phase test
modules, typed confirmations on reversible operations, and status prose
duplicated across documents.

When proposing work, prefer the smaller surface and say what you are leaving
out. Do not add a new layer per capability.

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
- Never edit `*.sops.yml` or `secrets/*.sops.env` files directly. Operators edit
  them with SOPS from a trusted workstation.
- Work with `secrets.example.yml`, variable names, and non-sensitive fixture
  values only.
- Never connect to, audit, import, rebuild, or change the Hetzner server or any
  production provider account.
- Never initialize or access the Backblaze state backend.
- Never run authoritative Packer, OpenTofu, or production Ansible commands.

The public age recipients in `.sops.yaml`, public SSH keys, resource labels, and
non-sensitive variable names may be committed when intentionally reviewed.
Private keys and provider credentials may not.

SOPS with age is the authoritative secrets system; see
`docs/runbooks/secrets.md`. Every operator holds an individual age identity and
1Password, when used, protects only that personal key — project credentials are
never duplicated into it. Neither Pi nor GitHub Actions ever holds an age
identity, so CI can validate the repository but can never decrypt it.

All secret-bearing and authoritative commands are run manually by an operator
from a trusted workstation. Do not suggest bypassing this boundary for
convenience.

## Engineering rules

- Prefer small, idempotent Ansible roles and explicit playbooks. Ansible owns
  users, access policy, service configuration, secrets, and all drift.
- Use OpenTofu, not Terraform. Treat the CPX32 and Primary IPv4 as adopted
  protected resources: normal plans must never replace or delete them, and
  critical resources carry `prevent_destroy` plus provider-side protection.
- The Packer gold image is frozen. Keep it generic and rebuild it only when the
  base image itself must change, not on a schedule.
- Require a typed confirmation only for operations that destroy data or the
  running host. Everything else relies on fail-closed defaults and automatic
  rollback.
- Tests must execute the thing under test: render the template, check the
  policy, call the function. Do not assert on source text or documentation
  prose.
- Pin OpenTofu providers, Ansible collections, Packer plugins, and package
  sources. Production containers use reviewed immutable digests.
- Use `no_log: true` and `diff: false` for every task that handles secrets, and
  render secrets root-owned with mode `0600` or stricter.
- Keep edge and identity Compose projects separate. Publish only 80/443 from
  Caddy; every other container port stays on an internal network. No Watchtower,
  no unattended production image updates, no unnecessary database service.
- Preserve default-deny host firewall and Headscale policy behavior.
- Model Backblaze B2 state as single-writer. Do not claim that local locking
  coordinates multiple workstations.
- Keep v1 within the roadmap boundary; do not introduce deferred capabilities
  early.
- Run `make lint` and `make validate` before presenting implementation changes.

## GitHub Actions boundary

GitHub Actions may run credential-free formatting, linting, template rendering,
policy checks, and dependency-update proposals.

GitHub Actions must never receive Hetzner, Cloudflare, Backblaze, SOPS, SSH,
restic, OpenTofu-state, or production deployment credentials.

## Operator-only operations

The following always remain outside Pi:

- Creating the B2 bucket, application key, and state-encryption material.
- Running OpenTofu plan/apply against provider accounts.
- Rebuilding the CPX32 from the validated snapshot.
- Running Packer builds or disposable Hetzner test infrastructure.
- Creating or decrypting SOPS files.
- Generating backup credentials or creating Healthchecks.io checks.
- Rotating production SSH credentials or enrolling Pocket ID passkeys.
- Running production Ansible check/deploy.
- Accessing the Storage Box or performing backup/restore operations.

Documentation must clearly label commands by execution location. A command that
needs production authority must not be represented as runnable from Pi.

## Destructive-operation safeguards

Two operations destroy something that cannot be recovered, and both require an
exact typed value:

- Rebuilding the CPX32 requires `CONFIRM_REBUILD=<expected-server-id>` and
  verifies server identity, type, Helsinki location, and Primary IPv4 first. It
  must preserve the server object and the independent Primary IPv4, and restore
  provider protection immediately.
- Pruning backup generations requires `PRUNE_RESTIC_<inventory hostname>` and is
  never scheduled.

Everything else is protected structurally rather than by a typed value: the host
firewall activates behind a timed rollback and a fresh SSH proof, the restore
test asserts it is not targeting the production host and restores into a
temporary directory on a disposable server, and protected resources carry
`prevent_destroy`.

Disposable test resources require owner/expiry labels and documented teardown.
No production deployment is complete until OpenTofu and Ansible are idempotent
and recovery, external monitoring, and backup alerts are tested.

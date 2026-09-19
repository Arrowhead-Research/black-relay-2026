# Architecture Roadmap

## Mission

Build a reproducible, reviewable, and secure identity and private-access stack
for the Survivability research project. The repository owns the complete
lifecycle of one production Hetzner Cloud server.

The identity flow is LLDAP -> Pocket ID -> Headscale. LLDAP is authoritative for
users and groups, Pocket ID synchronizes LDAP identities and provides
passkey-based OIDC, and Headscale authenticates users through Pocket ID.

## Scope and objectives

The deployment supports approximately 30-50 research-project users:

- One production VPS, with no permanent staging environment.
- Disposable, short-lived test infrastructure in the same Hetzner project.
- RPO no greater than 24 hours and manually executed RTO no greater than four
  hours.
- Public Pocket ID, Headscale control, and hardened SSH access.
- Default-deny private-network authorization managed as code.
- Daily encrypted off-server backups and quarterly isolated restore tests.
- Reproducible rebuilding rather than high availability or automatic failover.

The accepted v1 risks are a single compute node, one Hetzner control-plane
account, one Hetzner-hosted backup target, no regional failover, manual
recovery, and fully trusted Survivability operators. Backblaze B2 state storage
does not provide a proven distributed-lock implementation for this design, so
OpenTofu is strictly single-operator.

### Proportion

This is one server for a small research project. Guard rails must earn their
keep. Cheap machinery that prevents irreversible loss is welcome; machinery that
only restates intent is not. Concretely: no per-phase test modules, no per-phase
runbooks, no typed confirmations on reversible operations, and no status prose
duplicated across documents. This file is the single source of truth for phase
status.

## Decisions

| Area | Decision |
|---|---|
| Production location | Existing CPX32 in Helsinki (`hel1`) |
| Public addressing | Existing protected Primary IPv4; IPv6 disabled |
| Base OS | Debian 13 gold image built with Packer, then frozen |
| Image use | Disaster recovery only; Ansible owns all routine change and drift |
| Infrastructure | OpenTofu manages adopted server/IP, firewall, BX11, DNS |
| State | Client-side-encrypted OpenTofu state in a versioned Backblaze B2 bucket |
| State concurrency | No distributed lock; one operator may run OpenTofu at a time |
| Host configuration | Small, idempotent Ansible roles and explicit playbooks |
| Runtime | Separate Docker Compose projects for edge and identity services |
| Edge | Official Caddy image pinned by digest. No WAF |
| Host security | Pragmatic Debian hardening, nftables, CrowdSec, Hetzner Firewall |
| Identity flow | LLDAP -> Pocket ID -> Headscale |
| Databases | SQLite for LLDAP, Pocket ID, and Headscale |
| Public DNS | Cloudflare DNS-only records with a 300-second TTL |
| Private DNS | Headscale MagicDNS initially; CoreDNS split DNS deferred |
| Secrets | SOPS with individual age recipients and an offline recovery recipient; both application secrets and infrastructure tooling credentials |
| Backups | Daily restic over SFTP to protected BX11 plus Hetzner server backups |
| Monitoring | External HTTPS polling, Healthchecks.io job monitoring, email alerts |
| Operations | Privileged commands run manually from trusted operator workstations |

Actual domain names, resource IDs, recipients, and other environment-specific
values belong in documented variables or operator-controlled files, not here.

### Why no WAF

Coraza and a custom-built Caddy image were considered and rejected for v1. For
30-50 known users behind passkey-only authentication, a WAF in front of Pocket
ID adds a GitHub Actions build pipeline, image scanning, SBOM generation, digest
promotion, a detection-mode rollout, and ongoing CRS exclusion tuning against
Headscale protocol traffic -- in exchange for protection that passkeys and a
three-port attack surface already largely provide. CrowdSec covers the credible
threat: automated scanning and brute force against SSH and the edge. Revisit if
a genuinely untrusted user population is ever onboarded.

## Trust and credential boundaries

The Pi environment is long-running and network-connected, and GitHub Actions is
third-party automation. Neither receives production authority.

| Action | Location | Credentials |
|---|---|---|
| Write code, docs, tests, and safe examples; lint and render | Pi or GitHub Actions | None |
| Provision, import, plan, and apply infrastructure | Trusted operator workstation | Provider and state credentials |
| Create or edit encrypted SOPS files; deploy Ansible | Trusted operator workstation | Operator age key and SSH identity |
| Rebuild the server; back up, restore, or prune | Trusted operator workstation | Hetzner authority plus exact confirmation |

`AGENTS.md` states the complete boundary and the full list of operator-only
operations; it and this section must not drift.

The committed Ansible inventory remains empty for credential-free validation.
Reviewable, non-secret production desired state is committed separately. A
trusted operator creates a mode-`0600` inventory outside the repository from the
committed example, containing only the production host, absolute paths to the
operator's manual and dedicated Ansible SSH identities (never key content), the
bootstrap connection user, Python interpreter, and safe SSH options.

## Infrastructure architecture

### Existing server adoption

OpenTofu imports the existing CPX32 and Primary IPv4 rather than creating
replacements. The server and address use provider-side deletion protection,
rebuild protection where applicable, `prevent_destroy`, and an IPv4 lifecycle
that does not automatically delete the address with the server.

The one-time rebuild is separate from normal `apply` and requires a typed value
containing the exact expected server ID. Ordinary OpenTofu commands must not be
capable of replacing the CPX32.

### Gold image

The frozen image contains a minimal patched Debian 13 base, required base
packages and time synchronization, Docker Engine and Compose from verified
pinned sources, basic SSH and OS hardening, and cloud-init compatibility without
operator-specific credentials.

Ansible owns named users, authorized keys, SSH access policy, nftables,
CrowdSec, service configuration, and all environment-specific values. Ansible
applies current security updates because snapshots age. Unattended security
updates may run, but unattended reboots may not.

The image is rebuilt only when the base itself must change, not on a schedule.
Production references an explicit snapshot ID, never `latest`. Keep the snapshot
you replace as a rollback.

### Disposable testing

Disposable tests use an explicit name prefix, owner/expiry labels, a short
default lifetime such as four hours, and a documented teardown command.

### State

The operator manually creates a dedicated versioned B2 bucket and a
bucket-scoped application key. OpenTofu encrypts state client-side; operators
supply B2 credentials and encryption material at runtime from trusted secret
storage, and the state-encryption key has an offline recovery copy.

B2 conditional-write locking is not relied upon. Only one operator may run
OpenTofu against an environment at a time. State, plans, and credentials are
never committed.

## Network and host security

Ingress permits TCP 22, 80, and 443 from the Internet plus required ICMP. TCP 22
is open because administrator addresses are dynamic; public SSH is an
independent break-glass path and is never restricted solely to Headscale. There
are no public UDP services. Outbound traffic is initially allowed. IPv6 is
disabled deliberately and must not be enabled without equivalent policy and
testing.

Two layers enforce this. The Hetzner Firewall sits outside the host and cannot
be self-inflicted into a lockout. The nftables layer additionally constrains
Docker's packet handling so a published container port cannot bypass host input
policy. That second layer is defence in depth behind a Compose convention:
**only Caddy publishes ports, and it publishes only 80 and 443**; everything
else stays on an internal network.

Host firewall activation is guarded structurally rather than by a typed
confirmation. Ansible validates the candidate policy, schedules a timed
rollback, applies the policy, and proves a fresh SSH connection before
persisting it and cancelling the rollback. A bad policy self-heals.

SSH uses one named, fully trusted operator account with two keys. The manual
FIDO2 identity is a manual recovery path; a distinct dedicated Ed25519 key
performs all normal convergence. Root login and password authentication are
disabled. That account is the only identity the public SSH port admits, and
`AllowUsers` names it explicitly.

Other operators hold tailnet-only accounts: a Unix account with a locked
password and no `authorized_keys`, reachable solely through Tailscale SSH, which
authenticates in `tailscaled` against the committed policy. The policy carries
one `ssh` rule per person naming only that person's own account, so an account
is never shared and `autogroup:nonroot` never appears against a tagged host.
Sudo is passwordless and therefore root-equivalent, so each account states it
deliberately. The accepted consequence is that these operators depend on
Headscale being up; break-glass recovery belongs to the named operator alone.
All supported cloud and repository accounts use strong provider MFA and offline
recovery codes.

CrowdSec runs on the host, consumes SSH and Caddy logs, and enforces decisions
through a host firewall bouncer.

Caddy obtains public certificates using normal ACME HTTP/TLS challenges, so no
Cloudflare API token is stored on the VPS. Embedded DERP and public UDP 3478 are
deferred until measurements demonstrate a need.

## Container and service architecture

Production roots are `/srv/edge` for Caddy and `/srv/identity-stack` for LLDAP,
Pocket ID, and Headscale. Use an external `identity_proxy` network and a private
internal `identity_backend` network. Caddy, Pocket ID, and Headscale attach only
where needed; Pocket ID and LLDAP communicate over the backend network. LDAP is
never published publicly. The LLDAP administration UI binds to host loopback and
is accessed through an SSH tunnel.

Containers use immutable image digests, health checks, restart policies,
resource limits, dropped capabilities, read-only filesystems, and non-root users
where upstream support permits. Automated tools may propose dependency updates,
but Watchtower and unattended production image updates are prohibited.

The VPS joins its own Headscale-managed tailnet as the tagged node
`blackrelay-vps`, which is what makes Tailscale SSH available to Survivability
operators. That route is never the only recovery mechanism: public break-glass
SSH stays open and is never restricted to Headscale.

## Identity and authorization

### LLDAP and Pocket ID

Bootstrap only: one named human administrator, one noninteractive read-only
Pocket ID LDAP bind account, and the groups `pocketid-admins`,
`headscale-users`, and `survivability`.

LLDAP is authoritative for usernames, verified email attributes, group
membership, disablement, and deletion. Pocket ID synchronizes using the
read-only account. Membership in `pocketid-admins` maps to Pocket ID
administration; ordinary synchronized users receive no administrative role. The
initial Pocket ID bootstrap mechanism is reduced to documented recovery
scaffolding after synchronized administration is verified.

A user who loses their only passkey requires administrator-assisted CLI
recovery. At least two separate operators must retain the ability to perform it.

SMTP is not required for technical bootstrap. A small external relay is required
before broader onboarding if recovery or notification workflows need email.

### Headscale

Headscale uses Pocket ID as a confidential OIDC provider with PKCE S256 and
requests `openid`, `profile`, `email`, and `groups`. The `headscale-users` group
controls admission. Removing or disabling a user blocks future authentication; a
documented offboarding command immediately expires or deletes existing nodes.
Node expiry limits residual access to no more than 24 hours.

That expiry bounds people, not machines. Headscale exempts tagged nodes from
`node.expiry`, and a tagged node is owned by its tag rather than by a user, so
neither the 24-hour ceiling nor a user's offboarding revokes an infrastructure
node. Revoking one means deleting the node or changing the committed policy.
This is the intended trade: infrastructure does not silently fall off the
tailnet, and in exchange its removal must be deliberate.

Network authorization uses a committed Headscale policy with Grants. The Grants
list is always present and always explicit, because omitting it is allow-all. It
began empty, and every flow added since is reviewable in the repository history.
OIDC groups are not assumed to be policy principals; the policy declares its own
groups and is maintained manually in v1.

Every permitted tag is explicit and follows a convention such as
`tag:<team>-<role>`. Survivability owns every declared tag; application teams own
only their own scoped tags. Noninteractive infrastructure nodes enroll with
short-lived, single-use, preauthorized keys generated just in time and never
committed or baked into images.

Headscale MagicDNS is sufficient for v1. No private addresses are published
through public Cloudflare DNS.

## Backups, recovery, and monitoring

OpenTofu provisions a protected BX11 Storage Box. The VPS accesses a dedicated
least-privilege backup subaccount using its own SSH key. Restic uses a separate
repository password and encrypts data client-side. Both are delivered through
SOPS-backed Ansible variables and rendered root-only. External Storage Box
reachability is disabled in steady state.

Daily backups run around 02:00 UTC with randomized delay and overlap prevention.
They capture consistent copies of all SQLite databases, cryptographic material,
Headscale policy, configuration, and deployment files. Retention is 7 daily, 5
weekly, and 12 monthly snapshots. Alert if no successful snapshot exists within
26 hours.

Hetzner server backups provide a convenient secondary rollback but do not
replace restic. The accepted v1 limitation is that the server, native backups,
and BX11 share a Hetzner account boundary.

Quarterly restore tests create an isolated temporary server in Helsinki. They do
not use production DNS or clients. The restore playbook refuses to run against a
host that already has a converged firewall policy, so it cannot target
production. Tests restore the coordinated stack, validate data and health,
record recovery time against the four-hour RTO, and destroy the server
afterward.

Healthchecks.io receives job start/success/failure signals through separate
daily backup and weekly verification checks. A separate external service polls
Pocket ID and Headscale HTTPS endpoints. Both paths send email alerts. Local
journald and Caddy logs are bounded to 7-14 days and must exclude authorization
headers, cookies, LDAP credentials, OIDC tokens, and request bodies.

## Destructive-operation policy

Normal commands fail closed. Two operations destroy something unrecoverable and
require an exact typed value:

- Rebuilding the server requires `CONFIRM_REBUILD=<expected-server-id>`.
- Pruning backup generations requires `PRUNE_RESTIC_<inventory hostname>`.

Everything else is protected structurally: `prevent_destroy` and provider-side
protection on adopted resources, timed rollback and a fresh SSH proof on
firewall activation, and a converged-host check on the restore test. Operator
checks must verify resource identity and console recovery immediately before a
rebuild.

## Implementation phases

Phases are a schedule, not an architecture. Files are named for what they do.

### Phases 1-6: complete

1. **Architecture and workspace reset** -- obsolete migration workflow removed;
   credential-free validation passes.
2. **Reproducible tooling foundation** -- pinned toolchain, Pi development
   image, `make lint` and `make validate`, credential-free GitHub Actions.
3. **Protected OpenTofu adoption** -- encrypted B2 backend bootstrapped; the
   existing CPX32 and independent Primary IPv4 imported and protected.
   Runbook: `docs/runbooks/opentofu-adoption.md`.
4. **Debian 13 gold image** -- generic image built, validated on a disposable
   server, and promoted by explicit snapshot ID. Now frozen.
   Runbook: `docs/runbooks/gold-image.md`.
5. **Explicit CPX32 rebuild** -- guarded in-place rebuild from the promoted
   snapshot; server object and Primary IPv4 preserved.
   Runbook: `docs/runbooks/server-rebuild.md`.
6. **Host baseline and access** -- `operator_access`, `host_baseline`, and
   `host_firewall` converged in production. Named-operator access with a
   dedicated Ansible key, root and password SSH disabled, TCP 22/80/443 plus
   ICMP, Docker-DNAT boundary enforced.
   Runbooks: `docs/runbooks/operator-access.md`,
   `docs/runbooks/host-configuration.md`.

### Phase 7: backup foundation

Status: complete. The backup, alert, retention, and isolated restore procedures
have been executed and tested per `docs/runbooks/backups.md`.

- Provision the home-scoped SFTP-only BX11 subaccount and render root-only
  restic configuration from SOPS.
- Schedule the guarded daily backup and weekly verification timers with separate
  Healthchecks.io signals.
- Enable optional Hetzner server backups.
- Run an isolated restore test before identity data becomes important.

Exit criteria: a daily encrypted backup succeeds without plaintext leakage;
failure and staleness alerts are tested; a full restore completes within four
hours.

### Phase 8: services

Status: complete. Converged in production and exercised by the operator per
`docs/runbooks/service-deployment.md` and `docs/runbooks/user-lifecycle.md`.
LLDAP, Pocket ID, and Headscale run behind Caddy; passkey login, LDAP
synchronization, and OIDC-gated node enrollment are working.

Deploy the whole stack in one phase. The identity chain cannot be validated
piecemeal -- LLDAP alone does nothing, Pocket ID needs LLDAP, Headscale needs
Pocket ID, and all three need Caddy for TLS.

- `edge` role -> `/srv/edge`: official Caddy image by digest, Caddyfile, bounded
  safe logs, and the proxy network. Install pinned CrowdSec and the host-firewall
  bouncer, consuming SSH and Caddy logs.
- `identity_stack` role -> `/srv/identity-stack`: LLDAP, Pocket ID, and
  Headscale by digest on the proxy and backend networks.
- Bootstrap the minimum LLDAP users and groups; deploy Pocket ID with read-only
  LDAP synchronization; verify stable identity attributes, group mappings,
  disablement, and passkey login.
- Deploy Headscale with a confidential Pocket ID OIDC client and a committed
  default-deny Grants policy.

Exit criteria: Caddy validates before reload and only 80/443 are published; LLDAP
remains authoritative and bind credentials cannot modify the directory;
authorized users enroll and reauthenticate through Pocket ID; unauthorized users
and undeclared flows are denied; offboarding revokes active access within the
defined objective.

### Phase 9: production verification

Status: complete. The operator completed the verification externally and
accepted the results as sufficient for v1. Detailed operational evidence is
retained outside the repository.

The completed verification covered:

- Restart and reboot resilience of both Compose projects and the host.
- Idempotence of repeat `tofu plan` and `site.yml` runs.
- The RPO/RTO objective against a restore containing real identity data,
  including restored-stack health.
- Unattended delivery of pending-reboot and disk-pressure alerts.
- Application, host-image, and data-recovery procedures.

### Phase 10: deferred capabilities

Status: production-ready v1 baseline complete by operator acceptance. The
operator completed the following operational work externally and accepted it as
sufficient: least-privilege Detection team access, reproducible Proxmox subnet
routing, onboarding and offboarding exercises, a full-stack restore using real
identity data, and external service, host, and route alerting. Detailed evidence
is retained outside the repository.

The remaining capabilities are optional and trigger-driven. Consider separately,
in approximate dependency order: external SMTP if broader onboarding relies on
email; central log shipping to an external service such as Grafana Cloud;
CoreDNS split DNS for `internal.example.com`; additional private applications;
a secrets service; a second backup provider or administrative account;
PostgreSQL if measured scale requires it; high availability or regional
failover; formal hardening benchmarks; a WAF if an untrusted user population is
onboarded.

Each deferred capability requires its own threat-model and recovery update
before implementation.

## Runbooks

Maintain these six, in `docs/runbooks/`:

| Runbook | Covers |
|---|---|
| `operator-access.md` | SSH operator bootstrap, key rotation, recovery |
| `host-configuration.md` | Host baseline and guarded nftables activation |
| `backups.md` | Backup setup, retention, verification, restore test |
| `opentofu-adoption.md` | B2 backend, resource import, protected planning |
| `server-rebuild.md` | Guarded rebuild and console recovery |
| `gold-image.md` | Frozen base image; rebuild only when the base changes |
| `service-deployment.md` | Caddy, CrowdSec, and identity-service deployment |
| `user-lifecycle.md` | User onboarding, denial tests, offboarding, passkey recovery |

Every command must identify whether it runs in credential-free Pi, GitHub
Actions, or a trusted operator workstation. Destructive commands must not be
ordinary default Make targets.

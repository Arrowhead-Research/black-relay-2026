# Architecture Roadmap

## Mission

Build a reproducible, reviewable, and secure identity and private-access stack
for the Survivability research project. The repository owns the complete
lifecycle of one production Hetzner Cloud server:

1. Packer builds a generic Debian 13 gold image.
2. OpenTofu adopts and manages the protected Hetzner infrastructure.
3. Ansible hardens the host, deploys the services, and applies in-place updates.
4. Operator-run verification and recovery procedures prove the deployment.

The identity flow is LLDAP -> Pocket ID -> Headscale. LLDAP is authoritative for
users and groups, Pocket ID synchronizes LDAP identities and provides
passkey-based OIDC, and Headscale authenticates users through Pocket ID.

The existing Helsinki CPX32 and its assigned Primary IPv4 are valuable
allocations but contain no data or services that need migration. The placeholder
Ubuntu installation will be destructively rebuilt from the validated Debian 13
snapshot without deleting or replacing the server object.

## Scope and objectives

The initial deployment supports approximately 30-50 research-project users and
has these objectives:

- One production VPS, with no permanent staging environment.
- Disposable, short-lived test infrastructure in the same Hetzner project.
- RPO no greater than 24 hours and manually executed RTO no greater than four
  hours.
- Public Pocket ID, Headscale control, and hardened SSH access.
- Default-deny private-network authorization managed as code.
- Daily encrypted off-server backups and quarterly isolated restore tests.
- Reproducible rebuilding rather than high availability or automatic failover.

The accepted v1 risks are a single compute node, one Hetzner control-plane
account, one Hetzner-hosted backup target, no regional failover, manual recovery,
and fully trusted Survivability operators. Backblaze B2 state storage does not
provide a proven distributed-lock implementation for this design, so OpenTofu
is strictly single-operator.

## Decisions

| Area | Decision |
|---|---|
| Production location | Existing CPX32 in Helsinki (`hel1`) |
| Public addressing | Existing protected Primary IPv4; IPv6 disabled |
| Base OS | Debian 13 gold image built with Packer |
| Image use | Initial provisioning and disaster recovery; Ansible performs routine in-place updates |
| Infrastructure | OpenTofu manages adopted server/IP, firewall, BX11, DNS, and disposable tests |
| State | Client-side-encrypted OpenTofu state in a versioned Backblaze B2 bucket |
| State concurrency | No distributed lock; one operator may run OpenTofu at a time |
| Host configuration | Small, idempotent Ansible roles and explicit playbooks |
| Runtime | Separate Docker Compose projects for edge and identity services |
| Edge | Caddy with Coraza; pinned custom image built in GitHub Actions |
| Host security | Pragmatic Debian hardening, nftables, CrowdSec, and Hetzner Firewall |
| Identity flow | LLDAP -> Pocket ID -> Headscale |
| Databases | SQLite for LLDAP, Pocket ID, and Headscale |
| Public DNS | Cloudflare DNS-only records with a moderate TTL such as 300 seconds |
| Public names | Role-based names such as `id.example.com` and `headscale.example.com` |
| Private DNS | Headscale MagicDNS initially; CoreDNS split DNS deferred |
| Secrets | SOPS with individual age recipients and an offline recovery recipient |
| Backups | Daily restic over SFTP to protected BX11 plus Hetzner server backups |
| Monitoring | External HTTPS polling, Healthchecks.io job monitoring, and email alerts |
| Central logging | Fluent Bit installed but disabled until an external destination is selected |
| Operations | Privileged commands run manually from trusted operator workstations |

Actual domain names, resource IDs, recipients, and other environment-specific
values belong in documented variables or operator-controlled files, not in this
roadmap.

## Trust and credential boundaries

The Pi environment is long-running and network-connected, and GitHub Actions is
third-party automation. Neither receives production authority.

| Action | Execution location | Credential access |
|---|---|---|
| Write code, documentation, tests, and safe examples | Pi development container | None |
| Format, lint, policy-check, and render test templates | Pi or GitHub Actions | None |
| Build, scan, create an SBOM for, and publish Caddy/Coraza | GitHub Actions | Repository-scoped GHCR package permission only |
| Create the B2 state bucket and application key | Trusted operator workstation | B2 credentials and state-encryption material |
| Build Packer snapshots and disposable Hetzner tests | Trusted operator workstation | Hetzner project token and operator SSH material |
| Import, plan, and apply OpenTofu | Trusted operator workstation | Provider and state credentials |
| Create or edit encrypted SOPS files | Trusted operator workstation | Operator's individual age key |
| Rebuild the CPX32 | Trusted operator workstation | Hetzner authority plus explicit server-ID confirmation |
| Check and deploy Ansible | Trusted operator workstation | SSH/MFA and required SOPS recipients |
| Restore production or test backups | Trusted operator workstation | SSH, SOPS, and backup credentials |

Never give Pi a production API token, B2 key, state-encryption key, age private
key, SSH key or agent socket, TOTP seed, Docker socket, production environment
file, or decrypted secret. Operators must not paste credentials into chat.
Secret-bearing Ansible tasks use `no_log: true` and `diff: false`.

GitHub Actions may publish the edge image but must receive no Hetzner,
Cloudflare, B2, SOPS, SSH, or deployment credential. Production consumes only a
reviewed immutable image digest.

## Infrastructure architecture

### Existing server adoption

OpenTofu imports the existing CPX32 and Primary IPv4 rather than creating
replacements. The server and address use provider-side deletion protection,
rebuild protection where applicable, `prevent_destroy`, and an IPv4 lifecycle
that does not automatically delete the address with the server.

The one-time rebuild is separate from normal `apply` and requires a typed value
containing the exact expected server ID. The operation verifies the server type,
location, and Primary IPv4, temporarily disables rebuild protection, rebuilds
without deleting the server object, and restores protection immediately.
Ordinary OpenTofu commands must not be capable of replacing the CPX32.

### Gold image

The generic image contains:

- A minimal, patched Debian 13 base.
- Required base packages and time synchronization.
- Docker Engine and Compose from verified, pinned sources.
- A pinned Fluent Bit installation, disabled by default.
- Basic SSH, logging, and operating-system hardening.
- Cloud-init compatibility without operator-specific credentials.

Ansible owns named users, authorized keys, SSH MFA policy, nftables, CrowdSec,
Fluent Bit configuration, service configuration, and all environment-specific
values. Ansible applies current security updates because snapshots age.
Unattended security updates may run, but unattended reboots may not.

Image builds occur on a schedule and for critical base fixes. A disposable
currently available server type validates a candidate in Helsinki before
promotion. Retain the current and one previous validated gold-image snapshot.
Production never consumes an implicit `latest` snapshot.

### Disposable testing

Disposable tests use an explicit name prefix, owner/expiry labels, a short
default lifetime such as four hours, and a documented teardown command. They
may create short-lived DNS records under a delegated test namespace for HTTPS
and OIDC validation. GitHub Actions may report expired resources using
credential-free committed metadata but has no deletion authority.

### State

The operator manually creates a dedicated versioned B2 bucket and a
bucket-scoped application key. OpenTofu encrypts state client-side; operators
supply B2 credentials and encryption material at runtime from trusted secret
storage. The state-encryption key also has an offline recovery copy.

B2 conditional-write locking is not relied upon. Only one operator may run
OpenTofu against an environment at a time, and the runbook requires manual
coordination. Version history is retained for interrupted-run recovery. State,
plans, and credentials are never committed.

## Network and host security

The initial Hetzner and host-firewall ingress policy permits:

- TCP 22 from the Internet because administrator addresses are dynamic.
- TCP 80 and 443 from the Internet.
- Required ICMP.
- No public UDP services and no other inbound traffic.

Outbound traffic is initially allowed. IPv6 is disabled deliberately and must
not be enabled without equivalent policy and testing. nftables rules must be
validated against Docker's packet handling so published container ports cannot
bypass policy. Only Caddy publishes application HTTP ports.

SSH uses named accounts with no shared human login, disabled root login, and no
password-only authentication. Per-user policy supports either:

- A software SSH key followed by PAM TOTP; or
- A FIDO2 SSH key with enforced user verification such as PIN/touch.

Initial MFA enrollment occurs through a controlled console/bootstrap sequence.
At least two operators must remain able to perform CLI recovery. Public SSH is
an independent break-glass path and is never restricted solely to Headscale.
All supported cloud and repository accounts use strong provider MFA and offline
recovery codes.

CrowdSec runs on the host, consumes SSH and Caddy logs, and enforces decisions
through a host firewall bouncer. Coraza initially observes Pocket ID in
detection mode. Blocking is enabled only after representative registration,
login, passkey, synchronization, recovery, and logout flows have been reviewed
and tested. Generic OWASP CRS inspection is not applied indiscriminately to
Headscale protocol traffic.

Caddy obtains public certificates using normal ACME HTTP/TLS challenges, so no
Cloudflare API token is stored on the VPS. Embedded DERP and public UDP 3478 are
deferred until measurements demonstrate a need; clients initially use public
DERP infrastructure.

## Container and service architecture

Use these production roots:

- `/srv/edge` for Caddy/Coraza.
- `/srv/identity-stack` for LLDAP, Pocket ID, and Headscale.

Use an external `identity_proxy` Docker network and a private internal
`identity_backend` network. Caddy, Pocket ID, and Headscale attach only where
needed; Pocket ID and LLDAP communicate over the backend network. LDAP is never
published publicly. The LLDAP administration UI binds to host loopback and is
accessed through an SSH tunnel.

Containers use immutable image digests, health checks, restart policies,
resource limits, dropped capabilities, read-only filesystems, and non-root
users where upstream support permits. Automated tools may propose dependency
updates, but Watchtower and unattended production image updates are prohibited.

The VPS may join its own Headscale-managed tailnet for private-service access,
but that route is never the only recovery mechanism. Tailscale SSH is deferred
for this VPS.

## Identity and authorization

### LLDAP and Pocket ID

Bootstrap only:

- One named human administrator.
- One noninteractive, read-only Pocket ID LDAP bind account.
- `pocketid-admins`.
- `headscale-users`.
- `survivability`.

LLDAP is authoritative for usernames, verified email attributes, group
membership, disablement, and deletion. Pocket ID synchronizes using the
read-only account. Membership in `pocketid-admins` maps to Pocket ID
administration; ordinary synchronized users receive no administrative role.
The initial Pocket ID bootstrap mechanism is removed or reduced to documented
recovery scaffolding after synchronized administration is verified.

A user who loses their only passkey requires administrator-assisted CLI
recovery. This is accepted for the research project. At least two separate
operators must retain the ability to perform that recovery.

SMTP is not required for technical bootstrap. A small external relay is
required before broader onboarding if recovery or notification workflows need
email. Credentials are entered directly into SOPS by an operator and are never
sent to Pi.

### Headscale

Headscale uses Pocket ID as a confidential OIDC provider with PKCE S256 and
requests `openid`, `profile`, `email`, and `groups`. The `headscale-users` group
controls admission. Removing or disabling a user blocks future authentication;
a documented offboarding command immediately expires or deletes existing nodes.
Node expiry/reauthentication limits residual access to no more than 24 hours.

Network authorization uses a committed Headscale policy with Grants. An
explicit empty Grants list is the default-deny starting point. OIDC groups are
not assumed to be policy principals. The policy declares its own groups and is
maintained manually in v1.

Every permitted tag is explicit and follows a convention such as
`tag:<team>-<role>`. Survivability owns every declared tag; application teams
own only their own scoped tags. Infrastructure tags such as
`tag:proxmox-router` remain Survivability-only. Noninteractive infrastructure
nodes enroll with short-lived, single-use, preauthorized keys generated just in
time and never committed or baked into images.

Headscale MagicDNS is sufficient for v1. When the first private application
requires `internal.example.com`, add a tailnet-only CoreDNS authority and later
a second on-prem resolver. No private addresses are published through public
Cloudflare DNS.

## Backups, recovery, and monitoring

OpenTofu provisions a protected BX11 Storage Box. The VPS accesses a dedicated
least-privilege backup subaccount using its own SSH key. Restic uses a separate
repository password and encrypts data client-side. Both are delivered through
SOPS-backed Ansible variables and rendered root-only.

Daily backups run around 02:00 UTC with randomized delay and overlap
prevention. They capture consistent copies of all SQLite databases,
cryptographic material, Headscale policy, configuration, and deployment files.
Use online application/SQLite backup methods where safe; otherwise permit a
brief coordinated pause. Retention begins at 7 daily, 5 weekly, and 12 monthly
snapshots. Alert if no successful snapshot exists within 26 hours.

Hetzner server backups provide a convenient secondary rollback but do not
replace restic. The accepted v1 limitation is that the server, native backups,
and BX11 share a Hetzner account/provider boundary.

Quarterly restore tests create an isolated temporary server of an available
type in Helsinki. They do not use production DNS or clients. Tests restore the
coordinated stack, validate data and health, record recovery time against the
four-hour RTO, and destroy the temporary server afterward.

Healthchecks.io receives job start/success/failure signals. A separate external
service polls Pocket ID and Headscale HTTPS endpoints. Both send email alerts.
Local journald and Caddy logs are bounded to approximately 7-14 days and must
exclude authorization headers, cookies, LDAP credentials, OIDC tokens, and
request bodies. Fluent Bit remains disabled until an external logging service,
initially expected to be evaluated against Grafana Cloud, is approved.

## Destructive-operation policy

Normal commands fail closed. Separate exact confirmation values are required
for:

- Rebuilding or destroying a server.
- Replacing firewall policy in a way that risks SSH access.
- Deleting the protected Primary IPv4 or BX11.
- Pruning backup generations.
- Restoring over production.
- Resetting an identity database.

The CPX32 rebuild specifically requires `CONFIRM_REBUILD=<expected-server-id>`.
Operator checks must verify resource identity and console recovery immediately
before execution.

## Implementation phases

### Phase 1: Architecture and workspace reset

Status: complete. The obsolete audit and migration workflow has been removed;
credential-free validation passes.

- Replace obsolete personal-account migration assumptions with this greenfield
  architecture.
- Update agent instructions and trust boundaries.
- Identify obsolete audit/migration code and documentation for later removal or
  repurposing.
- Preserve safe example variables; do not create or request credentials.

Exit criteria:

- Roadmap and repository instructions agree on the greenfield lifecycle.
- Credential-free `make lint` and `make validate` pass.
- No existing target or default command can access or mutate production.

### Phase 2: Reproducible tooling foundation

Status: complete. The trusted-host development image, `make dev-doctor`, and
GitHub Actions credential-free checks were operator-verified.

- Pin Packer, OpenTofu, Ansible, collections, providers, and linters.
- Add credential-free formatting, validation, policy, and template tests.
- Add GitHub Actions without production secrets.
- Document trusted-workstation environment variables without values.

Exit criteria:

- Local and CI checks use consistent versions.
- Build and production credentials remain outside Pi and CI.

### Phase 3: Protected OpenTofu adoption

Status: complete. A trusted operator bootstrapped the encrypted B2 backend,
imported only the existing CPX32 and independent Primary IPv4, removed the
unneeded IPv6 allocation through the reviewed procedure, applied the protected
resources, and verified convergence without replacement or deletion of the
adopted resources.

- Define the CPX32, independent Primary IPv4, firewall, Cloudflare records, and
  BX11 with deletion safeguards.
- Document manual B2 backend bootstrap and single-operator coordination.
- Import only the existing CPX32 and Primary IPv4.
- Require the operator to review a plan containing no replacement or deletion.

Exit criteria:

- The protected imported resources show no destructive plan.
- The IPv4 survives server lifecycle changes.
- No ordinary apply can delete the CPX32, IPv4, or BX11.

### Phase 4: Debian 13 gold image

- Build the generic image on a temporary server.
- Pin and verify Docker, Compose, and Fluent Bit sources.
- Add pragmatic baseline hardening without credentials.
- Validate boot, cloud-init, SSH bootstrap, package state, and architecture.
- Retain current and previous validated snapshots.

Exit criteria:

- A disposable server passes image validation.
- The builder is removed and costs are accounted for.
- Production references an explicit snapshot ID, never `latest`.

### Phase 5: Explicit CPX32 rebuild

Operator-run sequence:

1. Confirm imported resource state and protections.
2. Confirm the selected snapshot passed disposable testing.
3. Verify Hetzner console recovery.
4. Record harmless current resource metadata; no application archive is needed.
5. Validate the expected server ID, CPX32 type, `hel1` location, and Primary IPv4.
6. Temporarily disable rebuild protection.
7. Rebuild from the Debian 13 snapshot without deleting the server object.
8. Re-enable protections immediately.
9. Verify boot and emergency access.

Exit criteria:

- CPX32 allocation and Primary IPv4 are unchanged.
- Debian 13 boots from the validated image.
- Protection and console recovery are verified.

### Phase 6: Host baseline and access

- Manage named administrators, per-user SSH mode, MFA bootstrap, nftables,
  updates, journald bounds, and CrowdSec.
- Disable root and password-only SSH after both authentication modes and console
  recovery are tested.
- Surface pending reboot and disk-pressure conditions by email.

Exit criteria:

- Public exposure matches TCP 22/80/443 plus required ICMP.
- Software-key/TOTP and FIDO2 operator paths work as configured.
- Docker cannot bypass backend-port restrictions.
- A second run is idempotent.

### Phase 7: Backup foundation

- Provision/protect BX11 and render root-only restic configuration.
- Add consistent backup, retention, verification, and Healthchecks.io signals.
- Enable optional Hetzner server backups.
- Run an isolated initial restore before identity data becomes important.

Exit criteria:

- Daily encrypted backup succeeds without plaintext leakage.
- Failure and staleness alerts are tested.
- A full restore completes within four hours.

### Phase 8: Edge deployment

- Build and publish the pinned Caddy/Coraza image with scan and SBOM evidence.
- Deploy `/srv/edge`, public TLS, bounded safe logs, and proxy networking.
- Integrate CrowdSec decisions.
- Exercise Coraza in detection mode before reviewed blocking rules are enabled.

Exit criteria:

- Caddy validates before reload.
- Only intended edge ports are public.
- Existing test routes and Headscale protocol behavior survive WAF testing.

### Phase 9: LLDAP and Pocket ID

- Deploy LLDAP and bootstrap the minimum users/groups.
- Deploy Pocket ID with read-only LDAP synchronization.
- Verify stable identity attributes, group mappings, disablement, and passkey
  login.
- Establish synchronized administration and reduce bootstrap access.

Exit criteria:

- LLDAP remains authoritative.
- Bind credentials cannot modify the directory or log in interactively.
- A synchronized administrator can authenticate and perform recovery.

### Phase 10: Headscale and policy

- Deploy a fresh Headscale instance and confidential Pocket ID OIDC client.
- Apply a committed default-deny Grants policy.
- Verify group admission, explicit tags, node expiry, offboarding, and
  short-lived infrastructure enrollment.
- Test Headscale control traffic through Caddy without generic CRS interference.

Exit criteria:

- Authorized users enroll and reauthenticate through Pocket ID.
- Unauthorized users and undeclared flows are denied.
- Offboarding revokes active access within the defined objective.

### Phase 11: Production verification

- Reboot and restart services individually.
- Verify authentication, synchronization, policy, enrollment, SSH recovery,
  firewall exposure, certificates, monitoring, and backups.
- Re-run OpenTofu and Ansible and require no unexpected changes.
- Test application, host-image, and data-recovery runbooks.

Exit criteria:

- Functional, security, restart, recovery, and idempotence checks pass.
- External uptime and job-failure email alerts work.
- The accepted RPO/RTO are demonstrated.

### Phase 12: Deferred capabilities

Consider separately, in this approximate dependency order:

1. External SMTP before broad user onboarding.
2. Fluent Bit activation and external log storage such as Grafana Cloud.
3. CoreDNS split DNS for `internal.example.com`.
4. Proxmox subnet routers and application-specific team grants.
5. Additional private applications.
6. Infisical Cloud evaluation and eventual self-hosted secrets service.
7. A second backup provider or administrative account.
8. PostgreSQL if measured scale or recovery requirements justify it.
9. High availability or regional failover.
10. Lynis automation or formal hardening benchmarks.

Each deferred capability requires its own threat-model and recovery update before
implementation.

## Required runbooks

Maintain concise operator runbooks for:

- Workstation and B2 bootstrap.
- Resource import and protected planning.
- Gold-image build, validation, promotion, and pruning.
- CPX32 rebuild and console recovery.
- Host and application deployment.
- SSH MFA enrollment and recovery.
- User onboarding, passkey recovery, and offboarding.
- Backup, retention, verification, and quarterly restore.
- Routine and emergency updates.
- Disposable test creation and teardown.
- Application, image, and data rollback.

Every command must identify whether it runs in credential-free Pi, GitHub
Actions, or a trusted operator workstation. Destructive commands must not be
ordinary default Make targets.

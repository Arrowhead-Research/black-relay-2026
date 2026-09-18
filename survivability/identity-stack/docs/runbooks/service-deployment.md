# Service deployment

Except for the explicitly labelled validation command, **every command in this
runbook runs on a trusted operator workstation**. Never expose the age identity,
SSH key, decrypted variables, Pocket ID client secret, or one-time access links
to Pi or GitHub Actions.

The deployment has one intentional bootstrap boundary. The first convergence
starts LLDAP and Pocket ID, creates the minimum directory objects, and lets the
named administrator register a passkey. The administrator then creates Pocket
ID's confidential Headscale client. A second, normal convergence starts
Headscale with that client and the default-deny policy. This is one stack
deployment, not independent partial production services.

## 1. Credential-free validation

**Pi or a trusted operator workstation, with no production credentials:**

```bash
make lint
make validate
```

## 2. Check prerequisites

**Trusted operator workstation:**

- OpenTofu has created DNS-only A records for the Pocket ID and Headscale names,
  each pointing to the protected Primary IPv4 with TTL 300.
- TCP 80 and 443 are reachable. No public UDP service or IPv6 record exists.
- The backup and isolated restore test in `backups.md` have passed.
- The external inventory is mode `0600` and contains the service variables shown
  in `hosts.example.yml`. The MagicDNS suffix is different from the Headscale
  public name.

```bash
stat -c '%a %n' "$ANSIBLE_INVENTORY"
getent ahostsv4 "REPLACE_WITH_POCKET_ID_FQDN"
getent ahostsv4 "REPLACE_WITH_HEADSCALE_FQDN"
```

## 3. Create service secrets

**Trusted operator workstation.** Edit only through SOPS:

```bash
sops ansible/inventory/production/group_vars/all/secrets.sops.yml
```

Populate the service variable names from `secrets.example.yml`:

- independent 32-character-or-longer `lldap_jwt_secret` and `lldap_key_seed`;
- independent 20-character-or-longer LLDAP administrator and bind passwords;
- `pocket_id_encryption_key`, generated as 32 random bytes encoded in base64;
- `crowdsec_bouncer_api_key`, at least 32 URL-safe characters.

Generate the CrowdSec bouncer key with:

```bash
openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 48; echo
```

The edge role registers this exact key with the CrowdSec local API and writes it
into the bouncer's `.local` override. It deliberately does not use the key the
Debian package postinst generates: that key exists only on the host, cannot be
reproduced on a rebuild, and survives a restore only if `/etc/crowdsec` and the
local API database under `/var/lib/crowdsec` are restored together and in step.
Convergence prunes any postinst-generated bouncer registration so no unmanaged
local API credential remains.

Leave the two Headscale OIDC values as placeholders until step 6. Keep offline
recovery copies of the LLDAP key seed and Pocket ID encryption key: losing either
breaks recovery of protected data. Do not add SMTP values in this phase; outbound
mail is optional and is configured separately in the section below.

## 3a. Optional: enable outbound mail for one-time access codes

Pocket ID can mail an administrator-issued one-time access code instead of
requiring the operator to carry the code out of band from the host. This is
optional and may be enabled at any time after the stack is running; nothing
below is required for deployment.

**The relay is on the account-recovery path once enabled.** Keep the CLI
procedure in `user-lifecycle.md` as the fallback: it needs nothing but SSH, and
it is the only path that still works when the relay, the sending domain, or the
recipient's mail provider is unavailable.

Create a **dedicated SMTP user**, not an account login, under *Sending > SMTP
Users* in the SMTP2GO dashboard, and verify the sending domain under *Sending >
Verified Senders*. An unverified sender is rejected by the relay. Then store the
issued pair through SOPS:

```bash
sops ansible/inventory/production/group_vars/all/secrets.sops.yml
```

- `pocket_id_smtp_username` — the SMTP user, not the SMTP2GO account email.
- `pocket_id_smtp_password` — that SMTP user's password.

Neither value may contain a line break. The env file is consumed with
`format: raw`, so an embedded newline truncates the password and silently
corrupts every setting after it.

Set the two non-secret values in the external inventory, and enable the relay:

```yaml
identity_stack_pocket_id_smtp_enabled: true
pocket_id_smtp_from: "identity@REPLACE_WITH_VERIFIED_SENDING_DOMAIN"
```

The host, port and TLS mode default to `mail.smtp2go.com:587` with STARTTLS. If
587 is blocked upstream, override `identity_stack_pocket_id_smtp_port` to
`2525`, which SMTP2GO also serves with STARTTLS. Converge normally, then send
one code to yourself from the Pocket ID admin UI and confirm it arrives and
works before relying on it for anyone else.

`EMAIL_ONE_TIME_ACCESS_AS_UNAUTHENTICATED_ENABLED` is pinned off by the role and
must stay off. It would let anyone at the sign-in page name a username and have
a working login code mailed to that user, which is a tenant-wide passkey bypass
gated only on a mailbox.

## 4. Bootstrap LLDAP and Pocket ID

**Trusted operator workstation.** The role validates all candidate Compose and
Caddy configuration. It starts LLDAP first, creates the project groups and the
strict-read-only Pocket ID bind user through LLDAP's API, and executes both a
read and a refused LDAP write with that bind identity.

```bash
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --check --diff \
    --extra-vars '{"identity_stack_bootstrap_only": true}' \
    ansible/playbooks/site.yml

./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" \
    --extra-vars '{"identity_stack_bootstrap_only": true}' \
    ansible/playbooks/site.yml
```

The bootstrap creates only:

- the named human administrator;
- the noninteractive `pocket-id-bind` account;
- the membership tier `black-relay`, which is the admission filter for the
  Pocket ID LDAP sync;
- the capability groups `pocketid-admins` and `headscale-users`;
- the team groups `survivability`, `tak`, `detection`, and `fabrication`;
- membership of the bind account in `lldap_strict_readonly`;
- membership of the named administrator in `black-relay`, both capability
  groups, and the `survivability` team.

The group model, and which kind of group to use when onboarding, is documented
in `user-lifecycle.md`. Team groups grant nothing on their own.

Do not enable cleanup in LLDAP's upstream bootstrap tooling. This role uses the
API directly and never deletes directory objects.

## 5. Register the first administrator passkey

**Trusted operator workstation.** Generate a single-use Pocket ID link over the
SSH session. Its output grants temporary administrator access: do not save it in
shell history, tickets, chat, or the repository.

```bash
ssh -t "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec pocket-id \
   /app/pocket-id one-time-access-token REPLACE_WITH_LLDAP_ADMIN_USERNAME'
```

Open the returned link directly on the administrator's trusted device, register
a passkey, sign out, and prove a fresh passkey login. Register a second
independent passkey or ensure a second operator can run the CLI recovery before
broader onboarding.

## 6. Create the confidential Headscale client

**Trusted operator workstation, in Pocket ID's administration UI:**

1. Create an OIDC client named `Headscale`.
2. Set the exact callback URL to
   `https://REPLACE_WITH_HEADSCALE_FQDN/oidc/callback`.
3. Enable PKCE and require the S256 challenge method.
4. Copy the generated client ID and client secret directly into their
   `headscale_oidc_*` fields by reopening the SOPS editor:

```bash
sops ansible/inventory/production/group_vars/all/secrets.sops.yml
```

Headscale requests `openid`, `profile`, `email`, and `groups`. Pocket ID's
`headscale-users` group is the admission filter; OIDC groups are deliberately
not policy principals.

## 7. Perform full convergence

**Trusted operator workstation:**

```bash
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --check --diff \
    ansible/playbooks/site.yml

./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml

./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
```

The final run must report no unexpected changes. Headscale refuses to start if
Pocket ID discovery is unavailable, uses PKCE S256, admits only
`headscale-users`, and expires ordinary nodes after 24 hours.

## 8. Verify exposure, health, and enforcement

**Trusted operator workstation:**

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose -f /srv/edge/compose.yml ps; \
   sudo docker compose -f /srv/identity-stack/compose.yml ps; \
   sudo ss -lntup; \
   sudo cscli metrics show acquisition; \
   sudo cscli metrics show bouncers; \
   sudo cscli bouncers list; \
   sudo nft list table ip crowdsec'

curl --fail --silent --show-error \
  "https://REPLACE_WITH_POCKET_ID_FQDN/.well-known/openid-configuration" \
  >/dev/null
curl --fail --silent --show-error \
  "https://REPLACE_WITH_HEADSCALE_FQDN/health" >/dev/null
```

Expected results:

- only TCP 22, 80, and 443 listen publicly;
- TCP 17170 binds only to `127.0.0.1` and reaches the LLDAP UI through Caddy;
- LLDAP and all identity application ports have no public container mapping;
- both Compose projects report healthy;
- CrowdSec consumes `ssh.service` journal records and Caddy JSON records, and
  the nftables bouncer has input and forward hooks;
- `cscli bouncers list` shows exactly one bouncer, `survivability-firewall-bouncer`,
  with a recent pull and no `crowdsec-firewall-bouncer-*` entry beside it;
- Caddy's bounded JSON logs omit Authorization, Proxy-Authorization, Cookie,
  and Set-Cookie fields and never contain request bodies.

Open the LLDAP UI only through a tunnel:

```bash
ssh -N -L 17170:127.0.0.1:17170 \
  "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST"
```

Then browse to `http://127.0.0.1:17170`. Never create a public LLDAP DNS record.

Complete the authorization and offboarding checks in `user-lifecycle.md` before
calling service deployment complete.

## Policy changes

The committed policy is
`ansible/roles/identity_stack/files/headscale-policy.hujson`, and it is the only
authority on private-network authorization. Never edit the host copy at
`/srv/identity-stack/headscale-config/policy.hujson`: convergence overwrites it,
so a hand edit survives until the next Ansible run and then disappears without
warning.

Convergence validates the policy before it reaches the running service. The role
mounts it into an isolated `configtest` container built from the pinned
Headscale image, and the run fails if the policy names a user Headscale cannot
resolve, references an undeclared tag, or carries a malformed prefix. A bad
policy therefore fails the play rather than the tailnet.

Validation runs after the file is installed, not before it. A policy that fails
`configtest` stops the play with the host copy already replaced and Headscale
still serving the policy it loaded at startup. The tailnet is unaffected at that
moment, but the next restart would load the rejected file, so correct the policy
and converge again rather than leaving the run failed.

### Applying a change

Edit the committed policy, then converge. The `identity-policy` tag installs and
validates the policy and its configuration without re-running host baseline,
backups, the edge, or the LLDAP bootstrap; README.md documents the full set of
selections.

```bash
export ANSIBLE_INVENTORY="$HOME/.config/survivability/production-hosts.yml"
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --tags identity-policy \
  ansible/playbooks/site.yml
```

The policy and configuration are bind-mounted and Headscale reads both only at
startup, so a changed policy notifies a Headscale restart and the run ends with
`RUNNING HANDLER [identity_stack : Restart Headscale]`. Established tunnels are
unaffected; node registration and reauthentication pause until Headscale is
healthy again. No handler in the recap means the policy did not actually change.

The restart is what applies the policy. A run that reports the policy unchanged
therefore leaves Headscale exactly as it was, which is why a host whose policy
was ever changed outside Ansible must be reconciled once — see below — before
this loop can be trusted.

Confirm the change took effect before treating it as deployed:

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec -T headscale headscale health'
```

Then exercise the flow the change was meant to allow or deny from an enrolled
node. `configtest` proves only that the policy parses and resolves, never that
it authorizes what you intended.

### Reconciling a hand-edited host policy

If the host copy was ever edited directly, it has diverged from the committed
file and the next convergence will overwrite it without warning. Reconcile once,
before relying on the tagged loop:

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo cat /srv/identity-stack/headscale-config/policy.hujson' \
  > /tmp/host-policy.hujson
diff /tmp/host-policy.hujson \
  ansible/roles/identity_stack/files/headscale-policy.hujson
```

Fold anything the host has that the committed file lacks into the committed
file, commit it so the authorization is reviewable in the diff, and converge.
Delete the local copy afterwards. Until this is done the committed file is not
the authority the rest of this section assumes it is.

### What the policy authorizes

- `group:survivability` is declared in the policy itself. OIDC group claims are
  not policy principals, so adding someone to the LLDAP `survivability` team
  does not grant them anything here; the two lists are maintained separately and
  deliberately.
- Each member reaches only their own devices, through `autogroup:self`. Members
  cannot reach each other's laptops. Shared infrastructure is the common ground.
- Every member reaches the Proxmox subnet named by the `proxmox-lan` host alias,
  through the node holding `tag:proxmox-subnet-router`.
- A grant and an SSH rule authorize `tag:blackrelay-vps`, the VPS itself. See
  "Enroll the VPS as `blackrelay-vps`" below.

Identities are the verified email addresses Pocket ID issues. Confirm the exact
strings before editing the policy, because Headscale matches on the provider
identifier first and then on email or name:

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec -T headscale \
   headscale users list'
```

### Add a member

Add the person to `group:survivability` in the committed policy, commit the
change so the authorization is reviewable in the diff, and converge. They must
already hold `headscale-users` in LLDAP, which is what admits them to
authenticate at all; the policy decides only what they may reach afterwards.

### Enroll the Proxmox subnet router

Noninteractive infrastructure nodes never authenticate as a human. Generate a
short-lived, single-use preauthorized key carrying the tag, use it immediately,
and never commit or bake it into an image:

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec -T headscale \
   headscale preauthkeys create \
   --expiration 1h --reusable=false --tags tag:proxmox-subnet-router'
```

There is deliberately no `--user`. A key created with `--tags` and no user
produces a node the tag owns, listed under `TaggedDevices`, which is what
Tailscale's model calls for and what keeps a person's offboarding from taking
infrastructure down with them. Note also that `--user` in Headscale 0.29 takes
a numeric user ID, not a username; `headscale users list` shows the IDs.

On the router, advertise the same range the policy names:

```bash
tailscale up --login-server "https://REPLACE_WITH_HEADSCALE_FQDN" \
  --authkey "REPLACE_WITH_PREAUTH_KEY" \
  --advertise-routes "REPLACE_WITH_PROXMOX_CIDR"
```

The `autoApprovers` block approves that route for any node holding the tag, so a
rebuilt or restored router does not need its route re-approving by hand. Confirm
the tag and the route actually took effect, and check the expiry column while
you are there:

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec -T headscale \
   headscale nodes list --output json' \
  | jq -r '.[] | [.id, .givenName, (.forcedTags | join(",")), .expiry] | @tsv'
```

A router that shows no `tag:` enrolled without one. Re-enroll it with a tagged
key rather than adding grants for the human user who owns it, or offboarding
that person will take the route down with them.

### Enroll the VPS as `blackrelay-vps`

The `tailnet_node` role owns the client: the pinned package, the signed package
source, the daemon, and the preferences the host runs with. It deliberately does
not enroll the host, because the preauthorized key is short-lived and single-use
and is generated just in time by an operator. Enrollment is therefore three
steps: converge, enroll by hand, converge again.

**1. Install and configure the client.** From the trusted workstation:

```bash
ansible-playbook ansible/playbooks/site.yml --tags tailnet
```

A host that has not joined yet is not a failure: the run installs and starts
`tailscaled` and prints a warning naming the backend state.

**2. Enroll it with a tag-owned key.** Generate the key on the host and use it
immediately. Treat the printed key as a live credential: it is single-use and
expires in an hour, and it belongs in neither a file nor a chat window.

```bash
ssh -t "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec -T headscale \
   headscale preauthkeys create \
   --expiration 1h --reusable=false --tags tag:blackrelay-vps'
```

Then, on the VPS itself:

```bash
sudo tailscale up --login-server "https://REPLACE_WITH_HEADSCALE_FQDN" \
  --authkey "REPLACE_WITH_PREAUTH_KEY" \
  --ssh --hostname blackrelay-vps --accept-dns=false --accept-routes=false
```

MagicDNS is refused deliberately. Accepting it would repoint `/etc/resolv.conf`
at `100.100.100.100` and make ACME renewal, backups, and `apt` depend on
`tailscaled` being healthy. The VPS publishes names; it does not resolve them.

**3. Converge again to prove the result.** The second run asserts that the node
is online and that it carries `tag:blackrelay-vps`, and it reconciles the
preferences against what the role declares:

```bash
ansible-playbook ansible/playbooks/site.yml --tags tailnet
```

That assertion is the point of the second run. A node enrolled with an untagged
key still works, but it is authorized as whoever ran `tailscale up` rather than
as infrastructure: the grant and SSH rule written for the tag do not reach it.
Delete such a node and re-enroll it rather than widening the policy.

Confirm the tag from the control server's own view as well:

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec -T headscale \
   headscale nodes list --output json' \
  | jq -r '.[] | [.id, .givenName, (.forcedTags | join(",")), .expiry] | @tsv'
```

### Test Tailscale SSH

From an enrolled member device, as a member of `group:survivability`, using
your own account name:

```bash
ssh REPLACE_WITH_YOUR_OWN_ACCOUNT@blackrelay-vps
```

What each layer contributes, and what to check when it fails:

- The LLDAP `headscale-users` group admits the member to the tailnet at all.
- `group:survivability` in the committed policy is what lets them reach
  anything. A member absent from it can reach nothing, including this host.
- The grant on `tcp:22` carries the transport and the `ssh` rule authorizes the
  session. Both are required; a rule without a matching grant refuses the
  connection, and the refusal is silent on the client.
- The `ssh` rules are one per person and each names only that person's own
  account, so `ssh` as a teammate's account is refused and so is root. Both
  refusals are the policy working. Adding an account means adding both the
  inventory entry and the rule; see "Add a teammate who reaches the host only
  over Tailscale SSH" in `operator-access.md`.

Two properties worth knowing before you debug a failure:

- Tailscale SSH terminates in `tailscaled`, not in `sshd`. `tailscaled` claims
  port 22 for the Tailscale address only, so the session never reaches the host
  SSH daemon and the nftables input chain never sees it. Changing the host
  firewall will not fix a refused Tailscale SSH session, and public break-glass
  SSH is unaffected by anything here. It must never become the only way in.
- Anything else served over the tailnet is ordinary traffic and does hit the
  host firewall, whose input chain is default-deny and admits only 22, 80, and
  443. A future private service reached over the tailnet needs an explicit rule
  in `host_firewall`; this change adds none.

Because the Hetzner firewall publishes no UDP, peers cannot open a direct
WireGuard path to this host and will relay through DERP. That is expected, costs
some latency, and is the deliberate consequence of the no-public-UDP rule.

## Failure and rollback

- A Caddy candidate is validated with the pinned Caddy image before promotion.
  A failed validation leaves the running configuration untouched.
- A Headscale candidate passes `headscale configtest` before startup.
- If full convergence fails at OIDC discovery, verify Pocket ID health, public
  DNS, the exact issuer URL, and the confidential client callback. Do not disable
  `only_start_if_oidc_is_available`.
- If LDAP sync fails, keep Headscale unavailable, inspect Pocket ID logs for
  `SyncLdap`, and correct the read-only bind configuration. Do not substitute an
  LLDAP administrator bind.
- If a new image fails, restore the previously reviewed digest in Git and rerun
  Ansible. Never use a floating tag or Watchtower.

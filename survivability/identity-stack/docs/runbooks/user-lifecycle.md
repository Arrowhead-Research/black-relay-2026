# User lifecycle

**Every command here runs on a trusted operator workstation.** User details,
one-time access links, passkeys, and Headscale node records must never be copied
to Pi, GitHub Actions, chat, or the repository.

LLDAP is authoritative. Do not create or edit synchronized users in Pocket ID.
Pocket ID's hourly LDAP sync mirrors directory users and groups, admitting only
members of `black-relay`; Headscale uses `headscale-users` only as an OIDC
admission filter. Network flows remain separately default-deny in the committed
Headscale policy.

## The group model

Groups come in three kinds and are not interchangeable.

- **Membership tier — `black-relay`.** Everyone with an account here. It is the
  admission filter for the Pocket ID LDAP sync, so it is load-bearing rather
  than descriptive: a user outside it is never imported and cannot authenticate
  to anything downstream.
- **Capabilities — `pocketid-admins`, `headscale-users`.** Each grants one
  specific power: Pocket ID administration, and Headscale node enrollment.
- **Teams — `survivability`, `tak`, `detection`, `fabrication`.** These grant
  nothing on their own. They synchronize into Pocket ID and appear in the OIDC
  `groups` claim so that Headscale grants and future OIDC clients can authorize
  on them. `survivability` is the infrastructure team and holds no special
  standing in the directory.

Every human belongs to `black-relay` plus exactly one team, and then to whatever
capabilities their role warrants. Adding a team never means editing the LDAP
search filter; create the LLDAP group, and it synchronizes on its own because
`LDAP_USER_GROUP_SEARCH_FILTER` admits every group.

A user placed in a team but not in `black-relay` cannot log in to anything, and
the failure presents as "user does not exist" rather than as a denial. Add
`black-relay` first, every time.

## Onboard a user

1. Open the LLDAP administration UI through an SSH tunnel:

   ```bash
   ssh -N -L 17170:127.0.0.1:17170 \
     "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST"
   ```

   Browse to `http://127.0.0.1:17170` and authenticate as a named LLDAP
   administrator.

2. Create one human user with a stable lowercase username, verified project
   email, and correct display/first/last names. Add `black-relay`, without
   which nothing else takes effect. Add exactly one team: `survivability`,
   `tak`, `detection`, or `fabrication`. Add `headscale-users` only if the
   person is approved to enroll private-access nodes. Add `pocketid-admins`
   only for a fully trusted Pocket ID operator.

3. Trigger immediate synchronization by restarting only Pocket ID, then verify
   its startup sync succeeds. This is reversible and requires no typed
   confirmation:

   ```bash
   ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
     'sudo docker compose --project-directory /srv/identity-stack \
      --file /srv/identity-stack/compose.yml restart pocket-id; \
      sudo docker compose --project-directory /srv/identity-stack \
      --file /srv/identity-stack/compose.yml logs --since 2m pocket-id'
   ```

   The logs must contain a successful `SyncLdap` job and no LDAP credentials.
   Confirm the user attributes and group names match LLDAP exactly.

4. Issue the user a one-time access credential, by either route below. The
   user then registers a passkey, signs out, and proves fresh passkey login.
   No password-based Pocket ID login exists.

   **By email, when the relay is enabled.** In the Pocket ID admin UI, open the
   user and send the login code to their address. The address is whatever you
   entered in LLDAP in step 2 and Pocket ID trusts it unconditionally, because
   `EMAILS_VERIFIED=true` marks every synchronized address as verified. A typo
   there mails a working credential to a stranger, so confirm the address in
   LLDAP against an out-of-band source before sending. Never send to an address
   you have not verified this way.

   **By CLI, always available.** This is the fallback whenever the relay, the
   sending domain, or the recipient's provider is unavailable, and it is the
   only route during an outage. Treat the terminal output as a short-lived
   credential and deliver it over an approved direct channel:

   ```bash
   ssh -t "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
     'sudo docker compose --project-directory /srv/identity-stack \
      --file /srv/identity-stack/compose.yml exec pocket-id \
      /app/pocket-id one-time-access-token REPLACE_WITH_USERNAME'
   ```

5. For a `headscale-users` member, enroll a normal client against the private
   control server:

   ```bash
   tailscale up --login-server \
     "https://REPLACE_WITH_HEADSCALE_FQDN" --force-reauth
   ```

   Complete Pocket ID authorization in the browser. Verify the node expires in
   no more than 24 hours and receives no undeclared connectivity.

6. Authorize what the new member may reach. `headscale-users` admits them to
   the tailnet; it grants no connectivity on its own. A member absent from
   `group:survivability` in the committed policy can reach nothing at all --
   not even their own other devices. To give a Survivability operator the
   team's access, add their Headscale identity to that group in
   `ansible/roles/identity_stack/files/headscale-policy.hujson`, commit the
   change so the authorization is reviewable, and converge. See "Policy
   changes" in `service-deployment.md`.

## Verify denial paths

Use a disposable test identity and test node, never a production operator:

- A synchronized user outside `headscale-users` can log in to Pocket ID but is
  denied Headscale enrollment.
- Removing `headscale-users`, running a Pocket ID sync, and forcing reauth denies
  a previously eligible user.
- Two enrolled nodes cannot exchange traffic while `grants` is empty.
- A tag absent from `tagOwners` cannot be assigned. Infrastructure preauth keys
  are short-lived, single-use, tag-owned, and generated just in time; they are
  never committed or put in an image.

Delete or disable the disposable LLDAP user and expire its node IDs when the
test finishes.

## Offboard or suspend a user

1. In LLDAP, remove `headscale-users` immediately. For suspension, also remove
   `black-relay`: that drops the user out of Pocket ID's LDAP search filter, so
   the next sync soft-disables the synchronized record. Removing only a team
   group suspends nothing. For permanent departure, delete the user only after
   preserving any required audit attribution. Removing `black-relay` is the
   suspension control because LLDAP exposes no login-enabled attribute that an
   LDAP filter can read.

2. Force Pocket ID synchronization rather than waiting up to one hour:

   ```bash
   ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
     'sudo docker compose --project-directory /srv/identity-stack \
      --file /srv/identity-stack/compose.yml restart pocket-id'
   ```

3. List every Headscale node attributed to the user and review the IDs:

   ```bash
   ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
     'sudo docker compose --project-directory /srv/identity-stack \
      --file /srv/identity-stack/compose.yml exec -T headscale \
      headscale nodes list --user REPLACE_WITH_USERNAME --output json' \
     | jq -r '.[] | [.id, .givenName, .expiry] | @tsv'
   ```

4. Expire each reviewed node immediately. Expiry is reversible by a future
   successful reauthentication, so it deliberately has no typed confirmation:

   ```bash
   NODE_ID="REPLACE_WITH_REVIEWED_NODE_ID"
   ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
     "sudo docker compose --project-directory /srv/identity-stack \
      --file /srv/identity-stack/compose.yml exec -T headscale \
      headscale nodes expire --identifier '${NODE_ID}'"
   ```

   Repeat for every node. Use `nodes delete --identifier` only when permanent
   record deletion is explicitly intended. Removal or disablement blocks future
   authentication; immediate expiry closes existing access without waiting for
   the 24-hour maximum node expiry.

   Infrastructure nodes such as `blackrelay-vps` and the Proxmox subnet router
   are owned by their tag rather than by a user, so they never appear in the
   listing above and offboarding leaves them running. That is the point of
   tagging them: a departure must not take the tailnet down. Their access is
   revoked by editing the committed policy or deleting the node, and the
   departing operator loses their own reach the moment they leave
   `group:survivability`.

5. If the person held a Unix account on the VPS, remove their `ssh` rule and
   their identity from `group:survivability` in the committed policy, move their
   account name from `operator_access_tailnet_operators` to
   `operator_access_removed_users` in the external inventory, and converge with
   `--tags access,identity-policy`. Moving the name rather than deleting the
   entry is what actually removes the account and its home directory; a name
   merely dropped from the list leaves a sudo-capable account behind on the host
   with nothing managing it. Prune the removed entry on a later pass.

6. Prove the former client cannot reach any reviewed destination and cannot
   reauthenticate. Re-list the user's nodes and record the offboarding result in
   the operator's approved audit system, not this repository.

## Lost passkey recovery

At least two separate operators must retain the ability to issue Pocket ID's
one-time access token. Verify the user's identity out of band, issue the token
with the onboarding command, and have the user register a replacement passkey.
Revoke the lost authenticator in Pocket ID after the replacement works. Never
send a one-time link through an unverified email address.

Prefer the CLI route here even when the relay is enabled. A lost authenticator
and a compromised mailbox are not distinguishable from the outside, and mailing
a recovery credential to an address an attacker may already hold converts one
lost device into a full account takeover. Verifying the person out of band is
the control that matters, and the CLI route forces it.

## Administrator changes

- `black-relay` grants nothing by itself but gates everything: without it no
  other group has any effect.
- `pocketid-admins` grants Pocket ID administration after LDAP sync.
- `lldap_admin` grants directory control and must remain tightly limited.
- `headscale-users` grants enrollment only, not network flow authorization.
- Team groups grant nothing; they only carry into the OIDC `groups` claim.
- Keep at least two recovery-capable human operators after bootstrap, but do not
  create shared administrator identities.

After any administrator change, force LDAP sync, require fresh passkey login,
and run the offboarding node-expiry procedure for removed operators.

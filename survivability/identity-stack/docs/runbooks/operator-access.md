# Operator access

**Location: trusted operator workstation only.** Pi and GitHub Actions may run
only credential-free syntax, lint, and policy checks.

One named operator holds two SSH keys: the manual FIDO2 identity, kept for
deliberate manual access and recovery, and a dedicated Ed25519 key used for the
initial Ansible connection and all normal convergence, so Ansible never needs a
hardware touch or PIN.

The dedicated Ansible private key is production authority. It stays only on the
trusted workstation, must be mode `0600`, and must never be committed, mounted
into Pi, pasted into chat, or copied to the server. A non-interactive key accepts
workstation file protection in place of hardware-backed user presence.

The bootstrap below is complete on the current server. Sections 1-4 are the
procedure to repeat after a rebuild; section 5 onward is routine.

## 1. Create the dedicated Ansible key

```bash
umask 077
ssh-keygen -t ed25519 -N '' -C 'survivability-ansible' \
  -f "$HOME/.ssh/survivability-ansible"
chmod 0600 "$HOME/.ssh/survivability-ansible"
chmod 0644 "$HOME/.ssh/survivability-ansible.pub"
```

The empty passphrase permits non-interactive runs. Protect the workstation with
full-disk encryption and strong local access controls. If unattended operation is
not needed, omit `-N ''` and use the workstation's normal protected-key workflow.

## 2. Authorize the dedicated key for root (one-time after a rebuild)

Needed only when the rebuild authorized only the FIDO2 key. Putting a new private
key path in the inventory cannot make the server trust it -- authenticate once
with the key the server already trusts and install the new **public** key:

```bash
MANUAL_IDENTITY="$HOME/.ssh/REPLACE_WITH_EXISTING_FIDO2_KEY"
ANSIBLE_IDENTITY="$HOME/.ssh/survivability-ansible"
PRODUCTION_HOST="REPLACE_WITH_PRODUCTION_IPV4_OR_DNS_NAME"

ssh-copy-id -i "${ANSIBLE_IDENTITY}.pub" \
  -o "IdentityFile=${MANUAL_IDENTITY}" -o IdentitiesOnly=yes \
  "root@${PRODUCTION_HOST}"
```

This is the only step that should request the FIDO2 PIN or touch. Do not work
around the prompt with an exported PIN or an askpass script. Pass the dedicated
identity to the rebuild script and future rebuilds skip this step entirely.

## 3. Create the external inventory

```bash
install -d -m 0700 "$HOME/.config/survivability"
install -m 0600 ansible/inventory/production/hosts.example.yml \
  "$HOME/.config/survivability/production-hosts.yml"
export ANSIBLE_INVENTORY="$HOME/.config/survivability/production-hosts.yml"
```

Replace three placeholders: the production address and absolute paths to the
manual and dedicated private keys. The named operator and all other non-secret
desired state live in the committed `ansible/inventory/production/production-vars.yml`.
Each identity needs an adjacent `.pub` file, which is what Ansible actually reads
-- private-key content is never placed in variables or copied to the server.
Keep SSH host-key checking enabled; the connection inventory stays outside the
repository.

## 4. Run the one-time bootstrap

```bash
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/bootstrap-access.yml
```

Do not use check mode here: the second play must connect to the account the first
play creates.

The playbook connects as root with the dedicated key, creates one
password-locked named operator with both public keys and passwordless sudo, then
imports `site.yml`, which reconnects as that operator and proves root-equivalent
sudo. Only after that reconnection succeeds does it validate and reload the
key-only SSH policy, disable root SSH, remove the temporary root authorized keys,
and lock the root password.

The ordering is the safety property: if the dedicated-key connection or sudo
fails, the tasks that disable root SSH are never reached; if SSH validation
fails, its handler does not reload the daemon and root keys are not removed.

## 5. Routine convergence

`site.yml` is the only normal entry point. It selects the named operator and
dedicated key itself, overriding the root username retained in the inventory:

```bash
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --check --diff ansible/playbooks/site.yml
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
```

A second run must report no changes.

## Add a teammate who reaches the host only over Tailscale SSH

Teammates do not get a key on the public SSH port. They get a Unix account with
a locked password and no `authorized_keys`, and they reach it through Tailscale
SSH, which authenticates in `tailscaled` against the committed Headscale policy
and never consults `authorized_keys` at all. The public port stays the named
operator's break-glass path alone.

Two files change together, and neither works without the other. Tailscale never
creates accounts, so a policy rule naming an account that does not exist refuses
the session; an account with no rule is unreachable.

1. **Prerequisite.** The person already holds `headscale-users` in LLDAP and has
   signed in to Pocket ID at least once, so Headscale knows their identity. Read
   the exact string back rather than assuming the address:

   ```bash
   ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
     'sudo docker compose --project-directory /srv/identity-stack \
      --file /srv/identity-stack/compose.yml exec -T headscale \
      headscale users list'
   ```

2. **Create the account.** Add an entry to `operator_access_tailnet_operators`
   in `ansible/inventory/production/production-vars.yml`. `sudo: true` is
   passwordless and therefore root-equivalent; state it deliberately for each
   person.

   ```yaml
   operator_access_tailnet_operators:
     - name: jake
       sudo: true
   ```

3. **Authorize the session.** Add one `ssh` rule per person to
   `ansible/roles/identity_stack/files/headscale-policy.hujson`, naming only
   their own account, and add their Headscale identity to
   `group:survivability` so the `tcp:22` grant carries the transport. A rule
   without a matching grant refuses the connection, and the refusal is silent
   on the client.

   ```json
   {
     "action": "accept",
     "src": ["jake.blackrelay@arrowheadresearch.org"],
     "dst": ["tag:blackrelay-vps"],
     "users": ["jake"]
   }
   ```

   Do not collapse these into one rule over `group:survivability` with
   `autogroup:nonroot`. Against a tagged host that lets every member log in as
   every other non-root account, including the break-glass operator, and
   passwordless sudo makes that root-equivalent. A test enforces this.

4. **Converge and verify.** Commit the policy change so the authorization is
   reviewable in the diff, then:

   ```bash
   ./scripts/operator/with-secrets.sh --age -- \
     ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml \
     --tags access,identity-policy
   ```

   Have the teammate confirm `ssh jake@blackrelay-vps` works, that
   `ssh robbie@blackrelay-vps` is refused, and that the public port refuses them
   outright. Accept that if Headscale or `tailscaled` is down they have no way
   in: recovery is the break-glass operator's job, and that is the trade this
   model makes deliberately.

## Failure and recovery

- **`ssh-askpass` or `ED25519-SK` signing failure:** Ansible is still using the
  FIDO2 identity. Complete section 2, then point
  `ansible_ssh_private_key_file` at the dedicated key.
- **Failure before dedicated-key reconnection:** correct the reported inventory,
  key, or account issue and rerun. Root key access remains enabled.
- **Named connection or sudo failure:** rerun after correcting the dedicated key
  path. Root has not been disabled.
- **SSH validation failure:** correct the managed template and rerun; the daemon
  was not reloaded and the root keys were not removed.
- **Interruption after root is disabled:** run `site.yml` with the dedicated key,
  or log in to the named account with the retained FIDO2 key. Use the
  already-established Hetzner console path only if both fail.

Key rotation is a reviewed change. Never overwrite the active dedicated private
key before its replacement public key has been installed and tested.

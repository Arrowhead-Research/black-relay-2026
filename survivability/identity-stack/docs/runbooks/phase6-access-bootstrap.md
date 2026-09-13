# Phase 6: operator access bootstrap

Status: complete. The trusted operator completed the bootstrap, verified both
the dedicated Ansible path and retained manual recovery key, disabled root SSH,
and confirmed idempotent named-account convergence.

Phase 6 uses one named operator with two SSH keys owned by that operator:

- the existing manual/FIDO2 identity remains available for deliberate manual
  access and recovery; and
- a separate dedicated Ed25519 key is used for the initial Ansible connection
  and all normal convergence, so Ansible never needs a hardware-key touch or
  PIN.

Ansible performs the root-to-operator transition. There is no second-person
check, TOTP enrollment, verification Boolean, inventory switch, or finalization
playbook.

The dedicated Ansible private key is production authority. It stays only on the
trusted operator workstation, must be mode `0600`, and must never be committed,
mounted into Pi, pasted into chat, or copied to the server. Using a
non-interactive key accepts workstation file protection as the control instead
of hardware-backed user presence.

The semantic `host_baseline` and `host_firewall` roles now run under normal
`site.yml` convergence and were operator-verified. CrowdSec is deferred to Phase
8 edge/log integration, and email delivery for host status is deferred to Phase
11 monitoring.

All production Ansible and SSH operations run on a **trusted operator
workstation only**. Pi and GitHub Actions may run credential-free syntax, lint,
and policy tests.

## How the transition works

The one-time bootstrap playbook:

1. Connects as root with the dedicated Ansible identity.
2. Reads the `.pub` files for both operator identities on the trusted
   workstation.
3. Creates one password-locked named operator, installs both public keys
   exclusively, and configures passwordless sudo.
4. Keeps root key access enabled and closes the bootstrap connection.
5. Imports `site.yml`, which reconnects as the named operator using the same
   dedicated Ansible key and proves root-equivalent sudo access.
6. Only after that reconnection succeeds, validates and reloads the key-only SSH
   policy, disables root SSH, removes the temporary root authorized keys, and
   locks the root password.

If the dedicated-key connection or sudo fails, `site.yml` cannot reach the tasks
that disable root SSH. If SSH configuration validation fails, its handler does
not reload SSH and the root keys are not removed.

After bootstrap, `site.yml` is the only normal host-convergence entry point. It
overrides the root username retained in the external inventory, so routine runs
use the named account and dedicated Ansible key automatically.

## 1. Create the dedicated Ansible key

**Location: trusted operator workstation only.**

Create a project-specific, non-interactive key. Do not reuse it for other hosts:

```bash
umask 077
ssh-keygen \
  -t ed25519 \
  -N '' \
  -C 'survivability-ansible' \
  -f "$HOME/.ssh/survivability-ansible"
chmod 0600 "$HOME/.ssh/survivability-ansible"
chmod 0644 "$HOME/.ssh/survivability-ansible.pub"
```

The empty passphrase permits non-interactive Ansible runs. This is an explicit
tradeoff: protect the trusted workstation with full-disk encryption and strong
local access controls. If unattended operation is not needed, omit `-N ''` and
use the workstation's normal protected-key workflow instead.

Do not print or paste the private key. It is not a SOPS input and never belongs
in this repository.

## 2. Authorize the dedicated key for the current root bootstrap

**Location: trusted operator workstation only. One-time transition for the
already-rebuilt server.**

The Phase 5 rebuild authorized only the existing FIDO2 key for root. Merely
putting a new private-key path in Ansible inventory cannot make the server trust
that key. Authenticate once with the key the server already trusts and install
only the new public key:

```bash
MANUAL_IDENTITY="$HOME/.ssh/REPLACE_WITH_EXISTING_FIDO2_KEY"
ANSIBLE_IDENTITY="$HOME/.ssh/survivability-ansible"
PRODUCTION_HOST="REPLACE_WITH_PRODUCTION_IPV4_OR_DNS_NAME"

ssh-copy-id \
  -i "${ANSIBLE_IDENTITY}.pub" \
  -o "IdentityFile=${MANUAL_IDENTITY}" \
  -o IdentitiesOnly=yes \
  "root@${PRODUCTION_HOST}"
```

This command is the only step that should request the existing FIDO2 PIN/touch.
It copies the dedicated **public** key; it does not copy either private key. Do
not work around the prompt by exporting a PIN or embedding it in an askpass
script.

For future rebuilds, pass the dedicated Ansible identity to the guarded Phase 5
rebuild script so this compatibility step is unnecessary.

## 3. Create the external inventory

**Location: trusted operator workstation only.**

Create one protected inventory from the committed example:

```bash
cd /path/to/survivability/identity-stack
install -d -m 0700 "$HOME/.config/survivability"
install -m 0600 \
  ansible/inventory/production/hosts.example.yml \
  "$HOME/.config/survivability/production-hosts.yml"
```

Replace four placeholders in the copy:

- production IPv4 address or DNS name;
- desired named operator username;
- absolute path to the existing manual/FIDO2 private-key stub; and
- absolute path to the dedicated Ansible private key created above.

Each identity must have an adjacent `.pub` file. For example,
`/home/operator/.ssh/survivability-ansible` requires
`/home/operator/.ssh/survivability-ansible.pub`. Ansible reads only those public
files when managing the server. Private-key content is never placed in
variables or copied to the server.

The bootstrap connection must select the dedicated key:

```yaml
ansible_user: root
ansible_ssh_private_key_file: *ansible_ssh_identity
```

Keep SSH host-key checking enabled. The inventory intentionally remains outside
the repository.

```bash
export ANSIBLE_INVENTORY="$HOME/.config/survivability/production-hosts.yml"
```

## 4. Run the one-time bootstrap

**Location: trusted operator workstation only.**

Run one authoritative Ansible command. It should not request the FIDO2 key's
PIN/touch because root now accepts the dedicated key.

```bash
ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  ansible/playbooks/phase6-bootstrap-access.yml
```

Do not use check mode for this first transition: the second play must connect to
the account created by the first play. The playbook itself verifies the Debian
13 host, both distinct public keys, named SSH connection, sudo, sudoers syntax,
and SSH daemon configuration before it removes bootstrap access.

A successful recap means Ansible connected first as root and then as the named
operator with the dedicated key. No inventory edit, interactive server-side
enrollment, second SSH session, server-ID confirmation, or manual finalization
step is required.

## 5. Use normal declarative convergence

**Location: trusted operator workstation only.**

All subsequent Phase 6 and application configuration runs through `site.yml`,
which selects the named operator and dedicated key itself:

```bash
ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
```

A second run must be idempotent. Check mode may be used for reviewed changes
after the one-time bootstrap:

```bash
ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  --check --diff \
  ansible/playbooks/site.yml
```

## Failure and recovery

- **`ssh-askpass` or `ED25519-SK` signing failure:** Ansible is still trying to
  use the manual FIDO2 identity. Complete Step 2, then set
  `ansible_ssh_private_key_file` to the dedicated key as shown in the example.
- **Failure before dedicated-key reconnection:** correct the reported inventory,
  key, or account issue and rerun bootstrap. Root key access remains enabled.
- **Named connection or sudo failure:** rerun bootstrap after correcting the
  dedicated key path. Root has not been disabled.
- **SSH validation failure:** correct the managed template and rerun bootstrap;
  the daemon was not reloaded and the root keys were not removed.
- **Interruption after root is disabled:** run `site.yml` with the dedicated key,
  or log in to the named account with the retained manual/FIDO2 key. Use the
  already-established Hetzner console path only if both fail.

Key rotation is a later reviewed change. Never overwrite the active dedicated
private key before its replacement public key has been installed and tested.

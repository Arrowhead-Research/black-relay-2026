# VyOS on Proxmox

This project preserves the supplied, currently working VyOS network and service
configuration as a small idempotent Ansible role using
`vyos.vyos.vyos_config`. The WAN uses DHCP for both its address and default
route. SSH uses public-key authentication and rejects password authentication.

## Ownership boundary

Proxmox owns the VM, bridges, virtual NIC attachment, NIC order, and VM
lifecycle. This repository does **not** create or alter those resources.

The `vyos_router` role owns configuration inside the existing VyOS guest. The
expected VM NIC identity is:

| VyOS interface | MAC address | Purpose | Address |
| --- | --- | --- | --- |
| `eth0` | `bc:24:11:be:a9:5a` | `BR-LAB-LAN` | `10.73.100.1/24` |
| `eth1` | `bc:24:11:fd:0c:5e` | `WAN` and SSH management | DHCP |

The WAN DHCP lease supplies both the address and default route; no static
upstream gateway is configured. Proxmox must present the two NICs with the MAC
addresses and bridge attachment expected by this table before the role is used.

The role ensures its commands exist. When it changes the active configuration,
a handler saves it to disk. It does not purge unrelated commands already on the
router, but it does remove the three previously managed static-WAN commands
(address, default route, and address-specific SSH listener) during migration.

## Authentication model

The role installs one or more public keys for the existing `vyos` user and
configures:

```text
set service ssh disable-password-authentication
```

The existing local password hash is not stored or managed by Ansible. It remains
on the router for Proxmox console recovery but cannot be used for SSH after the
role is applied. Because the WAN address is dynamic, SSH is not bound to one IP;
it listens on the router's addresses and remains key-only. Private SSH keys
remain on the trusted operator workstation and must never be copied into this
repository or the Pi environment.

## Layout

- `site.yml` applies the role to `vyos_routers`.
- `inventory/hosts.yml` contains the management endpoint and VyOS `network_cli`
  connection settings.
- `inventory/group_vars/vyos_routers/operator.yml.example` documents the local
  public-key variables.
- `roles/vyos_router/defaults/main.yml` is the non-secret source of truth.
- `roles/vyos_router/templates/vyos_config.set.j2` renders VyOS `set` commands.
- `tests/render_config.yml` renders and parses the commands without contacting a
  router.

## Trusted-workstation setup

The Pi development environment must not receive credentials or access the live
router. Run deployment commands from a trusted operator workstation.

Install the pinned collection:

```bash
ansible-galaxy collection install -r requirements.yml
```

Create the ignored operator variable file:

```bash
cp inventory/group_vars/vyos_routers/operator.yml.example \
  inventory/group_vars/vyos_routers/operator.yml
```

A normal public-key file has three fields:

```text
ssh-ed25519 AAAAC3... operator@workstation
```

Set `type` to the first field, `key` to only the second/base64 field, and choose
a stable `name` such as `operator`. Set `vyos_router_management_host` to a local
DNS name tracking the DHCP lease, or to the currently discovered lease address.
Public keys are not secrets, but this project keeps deployment-specific identity
and addressing in the ignored `operator.yml` file.

Do not configure `ansible_password` in inventory. Ansible should use the
matching private key from the trusted workstation after the transition.

## Validate locally

The render test uses a fake public key and does not connect to VyOS:

```bash
ansible-playbook tests/render_config.yml
ansible-playbook site.yml --syntax-check
```

## First key-only deployment

Changing the WAN from `10.73.66.50/24` to DHCP can interrupt the active SSH
session and changes the management endpoint. Confirm the matching private key,
Proxmox console access, and a way to discover the new DHCP lease before the
first application. Review the DHCP server's leases after applying, then update
`vyos_router_management_host` unless local DHCP/DNS already tracks it.

If the public key is not already installed, bootstrap through the current SSH
password without storing it:

```bash
# Preview using an interactive SSH password prompt.
ansible-playbook site.yml --check --diff --ask-pass --limit vyos

# Install the key and disable SSH password authentication.
ansible-playbook site.yml --ask-pass --limit vyos
```

After that succeeds, close the original SSH connection and verify a fresh
key-authenticated connection without `--ask-pass`:

```bash
ansible-playbook site.yml --check --diff --limit vyos
```

The final check should report no changes. Applying and saving are separate so a
VyOS `compare saved` output-format difference cannot create a false-positive
change on an already converged router. The role does not remove unlisted public
keys in this initial implementation.

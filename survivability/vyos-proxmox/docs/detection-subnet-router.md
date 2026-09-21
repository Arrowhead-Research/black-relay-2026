# Detection VLAN subnet router

This procedure creates the dedicated Headscale subnet-router LXC for
`10.73.200.0/24`. It does not modify the existing Proxmox-management subnet
router. Every command in this document runs from a trusted operator workstation
or from the Proxmox host; never supply Proxmox, VyOS, SSH, or Headscale authority
to Pi.

## Fixed topology

| Component | Value |
| --- | --- |
| Detection VLAN | VLAN 200, `10.73.200.0/24` |
| VyOS Detection gateway | `10.73.200.1` |
| Transit VLAN | VLAN 210, `10.73.210.0/29` |
| VyOS transit address | `10.73.210.1` |
| New LXC transit address | `10.73.210.2` |
| Headscale client prefix | `100.64.0.0/10` |
| New Headscale tag | `tag:detection-subnet-router` |
| Advertised route | `10.73.200.0/24` only |
| Validated VyOS release | rolling `2026.09.17.0028` |

The new LXC has exactly one network interface, tagged VLAN 210. It must never
have a VLAN 200 interface. Tailscale subnet-route SNAT is disabled, and VyOS has
a return route for `100.64.0.0/10` through `10.73.210.2`.

## 1. Apply the fail-closed control planes first

Before creating the LXC, deploy the reviewed VyOS configuration from this
repository and the Headscale policy from the sibling `identity-stack`
repository. Keep `group:detection` empty at this point. Have the Proxmox console
open before changing VyOS.

From the trusted workstation, in `vyos-proxmox`:

```bash
ansible-playbook site.yml --check --diff --limit vyos
ansible-playbook site.yml --limit vyos
ansible-playbook site.yml --check --diff --limit vyos
```

The final check must report no changes. On VyOS, confirm the running release is
`2026.09.17.0028`, `eth0.210` is `10.73.210.1/29`, and the route for
`100.64.0.0/10` points to `10.73.210.2`. Do not continue if the existing
`10.73.66.0/24` Proxmox route or management access changed.

Apply the Headscale policy with the `identity-policy` tag as documented in
`identity-stack/docs/runbooks/service-deployment.md`. It declares the new tag
and auto-approver while granting access only to the existing Survivability
infrastructure owners because `group:detection` is empty.

## 2. Clone without cloning the machine identity

Run on the Proxmox host. Choose an unused container ID and substitute the actual
source LXC, storage, and VLAN-aware bridge names. A full clone reads the source but does not reconfigure or stop it. If the
storage backend refuses to clone a running source, cancel and create a fresh
LXC from the same Debian template; do not stop the production source router for
this work.

```bash
SOURCE_CTID=REPLACE_WITH_EXISTING_SUBNET_ROUTER_CTID
NEW_CTID=REPLACE_WITH_UNUSED_CTID
BRIDGE=REPLACE_WITH_VLAN_AWARE_BRIDGE
STORAGE=REPLACE_WITH_TARGET_STORAGE

test "$SOURCE_CTID" != "$NEW_CTID"
pct status "$SOURCE_CTID"
pct config "$SOURCE_CTID"
pct clone "$SOURCE_CTID" "$NEW_CTID" \
  --full 1 \
  --hostname detection-subnet-router \
  --storage "$STORAGE"
pct set "$NEW_CTID" --onboot 0
```

The clone contains the source node's Tailscale identity and must not boot until
that state has been removed offline:

```bash
pct mount "$NEW_CTID"
ROOT="/var/lib/lxc/${NEW_CTID}/rootfs"
test -d "$ROOT/etc" && test -d "$ROOT/var/lib"

if test -d "$ROOT/var/lib/tailscale"; then
  find "$ROOT/var/lib/tailscale" -mindepth 1 -maxdepth 1 \
    -exec rm -rf -- {} +
fi
truncate -s 0 "$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id"
rm -f "$ROOT"/etc/ssh/ssh_host_*
pct unmount "$NEW_CTID"
```

These paths contain `NEW_CTID`; verify that variable before the `find` or `rm`.
Never run the cleanup against `SOURCE_CTID`.

## 3. Attach only VLAN 210

Replace the cloned `net0` and configure a public resolver. Do not copy the
source container's address or MAC explicitly.

```bash
pct set "$NEW_CTID" \
  --net0 "name=eth0,bridge=${BRIDGE},tag=210,ip=10.73.210.2/29,gw=10.73.210.1,type=veth"
pct set "$NEW_CTID" --nameserver 1.1.1.1
pct config "$NEW_CTID"
```

Review every `netN` line. Delete any inherited extra NICs before first boot:

```bash
# Run only for each extra interface actually shown by `pct config`.
pct set "$NEW_CTID" --delete net1
```

The final configuration must show exactly one NIC, `net0`, on VLAN 210. Confirm
that the TUN device configuration inherited from the source is present. Then
start the clone and regenerate its local identities:

```bash
pct start "$NEW_CTID"
pct exec "$NEW_CTID" -- systemd-machine-id-setup
pct exec "$NEW_CTID" -- ssh-keygen -A
pct exec "$NEW_CTID" -- systemctl restart ssh
pct exec "$NEW_CTID" -- test -c /dev/net/tun
pct exec "$NEW_CTID" -- tailscale version
pct exec "$NEW_CTID" -- tailscale status
```

`tailscale status` must report that this new node needs login. If it displays the
source router's node, stop the clone immediately and repeat the offline state
cleanup.

## 4. Enable routing and prove the underlay

```bash
pct exec "$NEW_CTID" -- sh -c \
  "printf '%s\n' 'net.ipv4.ip_forward=1' 'net.ipv6.conf.all.forwarding=0' > /etc/sysctl.d/99-detection-subnet-router.conf"
pct exec "$NEW_CTID" -- sysctl --system
pct exec "$NEW_CTID" -- sysctl -n net.ipv4.ip_forward
pct exec "$NEW_CTID" -- ip -4 address show dev eth0
pct exec "$NEW_CTID" -- ip route
pct exec "$NEW_CTID" -- ip route get 10.73.200.1
pct exec "$NEW_CTID" -- ping -c 3 10.73.200.1
pct exec "$NEW_CTID" -- curl -fsS https://headscale.l8s.dev/health
```

The route lookup for `10.73.200.1` must use `10.73.210.1` on `eth0`. The ping
proves forwarding through VyOS to its VLAN 200 address. The HTTPS request proves
the LXC's own `10.73.210.2/32` WAN NAT path. Do not continue if either fails.

## 5. Enroll the new router

Generate a one-hour, single-use key carrying
`tag:detection-subnet-router` using the command in the identity-stack service
deployment runbook. Do not reuse the original router's key and do not store the
new key in a file or shell history.

On the Proxmox host, read the key without echo and pass it to the new LXC:

```bash
read -rsp 'Detection router preauth key: ' TS_AUTHKEY; printf '\n'
pct exec "$NEW_CTID" -- tailscale up \
  --login-server https://headscale.l8s.dev \
  --auth-key "$TS_AUTHKEY" \
  --hostname detection-subnet-router \
  --advertise-routes 10.73.200.0/24 \
  --snat-subnet-routes=false \
  --accept-routes=false \
  --accept-dns=false
unset TS_AUTHKEY
```

Then verify locally and from Headscale:

```bash
pct exec "$NEW_CTID" -- tailscale status
pct exec "$NEW_CTID" -- tailscale ip -4
pct exec "$NEW_CTID" -- tailscale debug prefs
```

The Headscale node record must have `tag:detection-subnet-router`, advertise only
`10.73.200.0/24`, and show the route approved. Stop if it appears under a human
user, has no forced tag, advertises another prefix, or enables an exit route.

Finally enable start at Proxmox boot:

```bash
pct set "$NEW_CTID" --onboot 1 --startup order=30,up=30
```

## 6. Test with one disposable Detection identity

Add one disposable user's verified Headscale identity to `group:detection` in
the committed policy, converge `identity-policy`, and enroll that user's test
client. On Linux clients, accept subnet routes explicitly:

```bash
tailscale set --accept-routes=true
```

Test all of the following before adding real Detection members:

1. The disposable Detection client can reach a VLAN 200 target on multiple TCP
   and UDP ports and with ICMP, subject only to the target's own firewall.
2. A packet capture on the VLAN 200 target sees the client's `100.64.0.0/10`
   address. Seeing `10.73.210.2` means subnet-route SNAT is still active.
3. A Headscale user outside both `group:detection` and
   `group:survivability` cannot reach VLAN 200.
4. VLAN 200 cannot initiate a new connection to the client, while replies to a
   client-initiated connection work.
5. The Detection client cannot reach `10.73.100.0/24`, `10.73.210.0/29`, or use
   the LXC for Internet access.
6. Stopping only the new LXC removes the VLAN 200 route without affecting the
   existing Proxmox subnet router or hypervisor access.

Remove and expire the disposable identity after the test. Add real Detection
identities to `group:detection` only after every denial test succeeds.

## Immediate rollback

Withdrawing the new route does not affect VLAN 200's existing Internet access or
the original Proxmox subnet router:

```bash
pct exec "$NEW_CTID" -- tailscale down
pct stop "$NEW_CTID"
pct set "$NEW_CTID" --onboot 0
```

Do not delete the LXC until its Headscale node has been reviewed and removed and
the failure has been understood.

# Camera node onboarding: AP stack, services, and image assembly

Builds the SD image for a camera edge node and defines how to check it in the field.

**Nothing here has been verified on hardware.** Every check in the
[field verification checklist](#field-verification-checklist) is labeled NOT YET RUN.

**Covered by Plan 3, not here:** Frigate, go2rtc, camera link setup (the `eth0` static address), bench configuration of the camera, and the central relay. Plan 3 must meet
[these requirements](#requirements-on-plan-3).

## Files

| File | Installed as | Purpose |
|---|---|---|
| `black-relay-setup-ap.nmconnection.tmpl` | `/etc/NetworkManager/system-connections/black-relay-setup-ap.nmconnection` (by `seed-kit.sh`) | Setup AP profile |
| `seed-kit.sh` | `/opt/black-relay/ops/` | Renders the profile, prints the kit label |
| `black-relay-captive.conf` | `/etc/NetworkManager/dnsmasq-shared.d/` | Captive DNS |
| `black-relay.nft` | `/etc/nftables.conf` | Firewall |
| `central-hosts.nft` | `/etc/black-relay/central-hosts.nft` | The element of the `central_hosts` set, the address admitted to SSH, Frigate and RTSP. None in the image, and replaced by each portal save |
| `ssh-needs-firewall.conf` | `/etc/systemd/system/ssh.service.d/black-relay.conf` | Stops sshd starting with no firewall table |
| `camera-provision.service` | `/etc/systemd/system/` | Provisioning daemon, the only watchdog |
| `camerabridge.service` | `/etc/systemd/system/` | Sample bridge unit |
| `camerabridge-config.path`, `camerabridge-config-changed.service` | `/etc/systemd/system/` | Start or restart the bridge when `bridge.yaml` changes |
| `mosquitto-black-relay.conf` | `/etc/mosquitto/conf.d/black-relay.conf` | Node-local broker |
| `mosquitto-service-override.conf` | `/etc/systemd/system/mosquitto.service.d/black-relay.conf` | Broker restart policy |
| `journald-persistent.conf` | `/etc/systemd/journald.conf.d/90-black-relay.conf` | Keeps the journal across reboots, capped at 256 MB |
| `field-run-kit1.md` | Not installed | The ordered field run for kit 1, with checks C49 onward |
| `../../cameralink/ops/install-node.sh` | Run from the source tree | Installs all of this and `cameralink/ops`. See build step 9 |
| `../../cameralink/ops/reach-check.sh` | Run from the source tree | Tests each daemon's reach to docker and systemd, inside its hardening. See C53 |
| `../../cameralink/ops/*.service`, `*.mount`, `*.nmconnection`, `daemon.json`, `hailo_pci.conf` | By `install-node.sh`, as each file's first line says | Camera link, Frigate, Docker and Hailo settings |

## Design decisions

### AP stack

NetworkManager already owns `wlan0` on Raspberry Pi OS, so the AP is a NetworkManager profile with
`ipv4.method=shared`, not separate hostapd and dnsmasq. NetworkManager starts its own dnsmasq
for the AP, which needs the `dnsmasq-base` package. Do not install the full `dnsmasq` package:
its system service would take port 53.

Balena wifi-connect could run the WiFi-join flow, but its fixed UI collects only WiFi
credentials. This plan runs its own portal because it also collects hostname, `node_id`,
covered zones, the Frigate address, the time server, and central endpoints (DESIGN 7.2).

NetworkManager must be at least 1.28 for `802-11-wireless.ap-isolation`, and at least 1.40 for the
`nmcli --offline` call in `seed-kit.sh`. C01 checks the installed version.

Changing `10.42.0.1` means changing the profile, captive DNS, firewall, and the daemon's `--ap-ip`
together. A site LAN that uses `10.42.0.0/24` cannot host this node: the firewall treats that range
as AP clients.

### Node-local broker

**Choice: mosquitto listens on `127.0.0.1:1883` only, and Frigate runs with
`network_mode: host`, so it reaches the broker at `127.0.0.1:1883`.** The bridge's default
`frigate.mqtt_host` is already `127.0.0.1`.

Frigate in a bridged Docker network cannot reach the host's loopback. The other fix is a second
listener on the Docker gateway address. It is rejected because of RTSP: a bridged container's pull
from the camera is forwarded out `eth0`, and the rule set must drop all forwarding to and from
`eth0`. A carve-out would contradict that requirement. Under host networking, the pull is the
node's own traffic and is not forwarded.

**How it is enforced:**

1. mosquitto binds loopback only, so no other interface has a listener.
2. The input chain accepts `lo` first, then drops TCP and UDP port 1883 from every other
   interface, and drops packets from other interfaces addressed to `127.0.0.0/8` or `::1`. If
   the listener is ever widened by mistake, the firewall still blocks it.

The listener is anonymous because the bridge's local connection has no credentials. Any process
on the node can use it, and nothing off the node can.

### Firewall policy

Input and forward default to drop. Output is open, so the node can reach the camera and the site
LAN. A reply to the node's own traffic is accepted on any interface. `wlan0` has two
roles, told apart by address: anything from `10.42.0.0/24` or to `10.42.0.1` is the AP.

| Traffic that starts from | Allowed in |
|---|---|
| `lo` | Everything, including the broker and Frigate's internal ports |
| `wlan0`, AP client | Portal (TCP 80), DNS (TCP/UDP 53), and DHCP (UDP 67), to `10.42.0.1` only |
| `wlan0`, the central node (`ip saddr @central_hosts`) | SSH (22), Frigate (8971), go2rtc RTSP (8554) |
| `wlan0`, site LAN | DHCP replies, mDNS (so `<host>.local` in `snapshot_url` resolves), ping, IPv6 neighbor discovery. Link-local IPv6 cannot reach SSH, Frigate or RTSP, and only addresses held on `wlan0` itself are reachable |
| `eth0` | Nothing |
| Forwarding | Nothing. Explicit drops to and from `eth0`, and a default drop for everything else |

- **Camera link:** the camera is statically addressed (DESIGN 11), so `eth0` needs no DHCP. The node
  opens RTSP to the camera, so the reply is accepted. RTSP must use TCP: UDP RTP from the camera
  starts a new flow and is dropped.
- **Only the central node reaches the node (owner decision, DESIGN 4.2).** `central_hosts` is declared in
  `black-relay.nft` and holds one address, the broker's, added by `/etc/black-relay/central-hosts.nft`
  through a glob. A missing or empty file therefore means the firewall with nobody admitted. The portal saves that file together
  with the rest of the config, so the set follows the broker address and the two cannot disagree. The
  daemon then reloads the firewall. A failed reload is logged as `reloading the firewall failed`
  and retried every 30 s, the join is never held up, and until it works the old set stays in force.
- **The broker address must be an IPv4 literal.** The set can hold nothing else, so the portal refuses a
  hostname and an IPv6 address. It also refuses `100.64.0.0/10`, because fusion must not depend on
  Tailscale (DESIGN 7.2), and addresses that can never be a central node, by name: unspecified, loopback,
  link-local, multicast, reserved, broadcast, the setup AP's `10.42.0.0/24` and the camera link's
  `10.77.0.0/24`. A config saved before this rule still loads and runs, and only a new save must
  meet it.
- **Frigate:** port 8971 has Frigate's login off, and a proxy secret shared with the central node protects
  it (DESIGN 5.6). No rule here opens any other Frigate port, so they are reachable only from the node
  itself, except go2rtc's RTSP on 8554, which the central node pulls live view from.
- **A blank node admits nobody over the network,** since its set is empty. The setup AP and a console do
  not depend on the set.
- **Reloads:** the file replaces only its own table, so `systemctl reload nftables` keeps the table
  that NetworkManager owns. **Do not run `systemctl stop nftables` or
  `systemctl restart nftables`:** the Debian unit's `ExecStop` runs `nft flush ruleset`, which removes
  every table, including that one, and restart runs it too.
- **A hung dockerd (review finding F7):** Frigate's container runs outside `frigate.service`'s cgroup. If dockerd
  hangs so that `docker stop` outlasts the unit's `TimeoutStopSec=45`, systemd kills only the client and calls the
  unit stopped, while the container may still run. If nftables is then stopped, which the rule above forbids, that
  container runs with no firewall. So after a stop of Frigate that took the full 45 s, run `sudo docker ps` before
  trusting that it stopped, and restart Docker or the node if `frigate` is still listed. No small unit change closes this.
- **If nftables fails at boot:** this takes a damaged `central-hosts.nft`, not a missing one, because nft
  loads all or nothing. The node then has no firewall table, and the guards hold: sshd
  (`ssh-needs-firewall.conf`), Frigate (`Requires=nftables.service` and its table check) and the
  provisioning daemon all refuse to start, so no port answers and no AP opens. Detection stops, and the
  bridge's heartbeat, which has no such guard, reports it false. Recover from a console, then re-provision
  to save the broker address again:

  ```bash
  systemctl status nftables --no-pager
  sudo install -m 0644 /opt/black-relay/ops/central-hosts.nft /etc/black-relay/central-hosts.nft
  sudo systemctl start nftables ssh
  sudo systemctl restart camera-provision
  ```

  A damaged file met by a reload rather than a boot leaves the old rules in force.
- **Sessions already open survive a change of central host.** The rule accepting established connections
  comes first, and a reload does not flush conntrack, so an SSH or RTSP session from the old address carries
  on until it ends. C45 shows this. There is no flush on purpose: nothing about the old host's session is
  unsafe enough to justify dropping the new one's too.

### SSH policy

- **Allowed:** the central node's address on `wlan0`, and nothing else. To reach a node from anywhere
  else, use the central node as a jump host: `ssh -J <user>@<central> <user>@<node-lan-ip>`. The node's
  address is in its heartbeat, as the `node_address` attribute.
- **Denied:** every other site LAN host, AP clients and `eth0`. With the central node down, or while the
  AP is up, the only access is a console (micro-HDMI and USB keyboard).
- **Authentication:** keys only, with root login off (image build step 7).

### Service ordering

| Unit | Starts after | Why |
|---|---|---|
| `nftables.service` | Early boot, before `network-pre.target` | Firewall exists before any interface is up |
| `NetworkManager.service` | `network-pre.target` | |
| `camera-provision.service` | `NetworkManager`, `nftables` | `ExecStartPre` refuses to start without the `black_relay` table, because the AP turns on IP forwarding |
| `mosquitto.service` | `network-online.target`, from the Debian unit | Instant once wait-online is masked, see below |
| `camerabridge.service` | `NetworkManager`, `mosquitto`, ordering only | Wants mosquitto but does not require it |

- **No `network-online.target` wait:** image build step 8 masks
  `NetworkManager-wait-online.service`. Otherwise, on a boot with WiFi down, the Debian mosquitto unit
  and the bridge behind it wait up to 60 s, while detect and record should continue. Docker's own
  unit would wait the same way. Disabling is not enough: `NetworkManager.service` has
  `Also=NetworkManager-wait-online.service`, so enabling NetworkManager enables it again.
- **No dependency between the bridge and the daemon:** provisioning state does not affect the
  bridge once `bridge.yaml` exists.
- **Restart policy:** `camera-provision`, `camerabridge`, and mosquitto (by drop-in) use
  `Restart=always`, `RestartSec=5`, and `StartLimitIntervalSec=0`.
- **Daemon hardening:** `ConfigurationDirectory=` gives it `/etc/black-relay` and
  `/etc/systemd/timesyncd.conf.d` writable and mode 0755, so timesyncd, which does not run as root, can still read the drop-in under `UMask=0077`.
  It runs as root with a reduced capability set. `CAP_DAC_OVERRIDE` is
  required: after `bridge.yaml` is given to `camerabridge` with mode 0600, root can no longer read
  it otherwise, and the node would act as if it had no config.

### Time

**The time server comes from the portal** (DESIGN 7.5, decided by the owner). `ntp_server` is on the
full form only, and blank means the broker host. The value is saved atomically with the rest of the
config, to `/etc/systemd/timesyncd.conf.d/black-relay.conf` as `NTP=<server>` (root, 0644), never to
`bridge.yaml`. The daemon restarts `systemd-timesyncd` whenever a saved config enters `joining`. A
failure logs `restarting systemd-timesyncd failed`, is retried every 30 s, and never holds up the join
or opens the AP. The WiFi-only portal has no such field and leaves the drop-in alone.

On a button re-provision the field starts with the saved value. If it is left at the old broker
address while the broker changes, it follows the new broker, as the note on the form says, and the
saved page names the server used. Any other typed value is kept. The one result this cannot give in a
single save is a time server that stays on the old broker's address: re-provision once more and type
it. To change the server, hold the button at boot and resubmit: a hand edit to the drop-in lasts only
until the next full save.

**If the server cannot be reached, the node never syncs.** That includes a node booting during a
central outage, if the server is the default. Until `timedatectl show -p NTPSynchronized --value` says
`yes` (DESIGN 7.5), the bridge:

- Logs `NTP not yet synchronized; holding durable events until it is`. Look for this line in
  `journalctl -u camerabridge`.
- Holds `object_start`, `object_end`, and cue-acks **in RAM only**, capped at 10,000 with the
  oldest dropped first, and **lost on any restart or power cut**.
- Puts **nothing in the outbox**, so the outbox gives these events no protection.
- Drops `object_update` and **sends no heartbeat**, so the node looks dead to the central node,
  though it is still detecting and recording.

### Requirements on Plan 3

1. Frigate, and go2rtc if run apart from it, use `network_mode: host`. Frigate's `mqtt.host` is
   `127.0.0.1` and its port is 1883.
2. Frigate pulls RTSP over TCP. No container may rely on forwarding: the forward chain drops it.
3. Docker runs with no bridge of its own, in `/etc/docker/daemon.json`:
   `{"bridge": "none", "iptables": false, "ip6tables": false}`. This is not required for
   isolation, since a drop in this rule set is final, but it keeps Docker's rules out of the way.
   It also rotates container logs at 3 files of 10 MB, so that `camera-link`'s first read of Frigate's
   log after a restart finishes inside its 20 s timeout. `cameralink/ops/daemon.json` is the file.
4. `eth0` has a static NetworkManager profile with `ipv4.method=manual`, `ipv4.never-default=yes`,
   `ipv6.method=disabled`, never `shared`. Delete the automatic `Wired connection 1` profile.
5. go2rtc reads `MEDIAMTX_ADDRESS` from `EnvironmentFile=/etc/camerabridge/mediamtx.env`, never
   `bridge.yaml`.
6. Opening another port from outside the node means editing `black-relay.nft`.
7. The Frigate container is not privileged, and gets only the devices it needs.
8. The unit that starts Frigate has `Requires=nftables.service` and `After=nftables.service`, so
   Frigate never runs without the firewall. Stopping nftables then stops Frigate too.
9. `frigate.camera` in `bridge.yaml` always matches Frigate's camera name. Either render Frigate's
   config from the saved `node_id` and `covered_zones`, again after every re-provision, or give
   the image one fixed camera name and write that name to `frigate.camera`. The portal writes
   `frigate.camera` as the `node_id` today, so a fixed name also needs that writer changed. A
   mismatch makes `camera_online` and `recording_ok` report false forever. C43 checks it.
10. The central node serves NTP on the site LAN, at its broker address, from its own clock and with
    no dependency on the WAN. It is every node's default time server, so without this no node
    syncs, and none sends a heartbeat or fills its outbox.
11. Frigate's own login is off, and a proxy secret shared with the central node protects port 8971, which
    only the central node's IP can reach (DESIGN 5.6). Nothing outside the node depends on port 5000.

## Build the image

The image starts as the size of the card. A small build card saves time, and step 11 shrinks a large one. Commands
run on the Pi unless marked. `<CC>`, `<build-pi>`, and `<sdX>` are placeholders. Kit 1 is already past step 8 on an
old build, so it starts from [`field-run-kit1.md`](field-run-kit1.md) instead, and comes back here at step 10.

1. **Flash** Raspberry Pi OS (64-bit), Trixie. Lite is preferred. On a Desktop image, also run
   `sudo systemctl set-default multi-user.target`. In Raspberry Pi Imager set a user and SSH
   key, and **no WiFi**, since Imager's WiFi setting leaves a `preconfigured` profile behind. Boot
   with `eth0` on a network with internet access.

2. **Install packages.**

   ```bash
   sudo apt-get update
   sudo apt-get install -y network-manager dnsmasq-base nftables gpiod python3-venv \
     mosquitto mosquitto-clients systemd-timesyncd avahi-daemon iw rsync curl
   ```

3. **Set the WiFi country.** The radio stays blocked until this is set.

   ```bash
   sudo raspi-config nonint do_wifi_country <CC>
   ```

4. **Create the service user.** Every full portal save fails at chown without it.

   ```bash
   sudo useradd --system --home-dir /var/lib/camerabridge --shell /usr/sbin/nologin camerabridge
   ```

5. **Copy the code and install both packages.** Run the `rsync` on the build host, from the
   repository root.

   ```bash
   # On the build host:
   rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
     detection/camera-subsystem/ <build-pi>:camera-subsystem/
   ```

   ```bash
   # On the Pi:
   sudo install -d /opt/black-relay
   sudo mv ~/camera-subsystem /opt/black-relay/src
   sudo chown -R root:root /opt/black-relay/src
   sudo python3 -m venv /opt/black-relay/provision
   sudo /opt/black-relay/provision/bin/pip install /opt/black-relay/src/provision
   sudo python3 -m venv /opt/black-relay/bridge
   sudo /opt/black-relay/bridge/bin/pip install /opt/black-relay/src/bridge
   /opt/black-relay/provision/bin/camera-provision run --help
   /opt/black-relay/bridge/bin/camerabridge run --help
   ```

6. **Install the ops files.** Nothing is enabled yet.

   ```bash
   cd /opt/black-relay/src/provision/ops
   sudo install -d /opt/black-relay/ops /etc/NetworkManager/dnsmasq-shared.d \
     /etc/systemd/system/mosquitto.service.d /etc/systemd/system/ssh.service.d /etc/black-relay
   sudo install -m 0644 README.md black-relay-setup-ap.nmconnection.tmpl central-hosts.nft \
     /opt/black-relay/ops/
   sudo install -m 0755 seed-kit.sh /opt/black-relay/ops/
   sudo install -m 0644 black-relay-captive.conf /etc/NetworkManager/dnsmasq-shared.d/
   sudo install -m 0644 mosquitto-black-relay.conf /etc/mosquitto/conf.d/black-relay.conf
   sudo install -m 0644 mosquitto-service-override.conf \
     /etc/systemd/system/mosquitto.service.d/black-relay.conf
   sudo install -m 0644 black-relay.nft /etc/nftables.conf
   sudo install -m 0644 central-hosts.nft /etc/black-relay/central-hosts.nft
   sudo install -m 0644 ssh-needs-firewall.conf /etc/systemd/system/ssh.service.d/black-relay.conf
   sudo install -m 0644 camera-provision.service camerabridge.service camerabridge-config.path \
     camerabridge-config-changed.service /etc/systemd/system/
   sudo nft -c -f /etc/nftables.conf
   sudo systemctl daemon-reload
   ```

7. **Set the SSH policy.** Confirm your key logs in first, because this turns off passwords.

   ```bash
   printf 'PasswordAuthentication no\nPermitRootLogin no\n' \
     | sudo tee /etc/ssh/sshd_config.d/10-black-relay.conf
   sudo sshd -t
   ```

8. **Enable the services.** Do not use `--now`: applying the firewall here would cut the build
   session on `eth0`, and starting the daemon would write state into the image.

   ```bash
      sudo systemctl enable nftables.service NetworkManager.service mosquitto.service \
     systemd-timesyncd.service camera-provision.service \
     camerabridge.service camerabridge-config.path
   # After the enable, which turns wait-online back on through Also=.
   sudo systemctl mask NetworkManager-wait-online.service
   ```

9. **Install the camera link,** without rebooting. A reboot would apply the firewall and cut the build session on
   `eth0`. First the two things the installer checks but does not supply: Docker with the Frigate image, and the
   Hailo driver. **Never install `hailo-all`:** Frigate needs driver 4.21.0, and Trixie's package is 5.1.1.

   ```bash
   sudo apt-get install -y docker.io docker-cli
   sudo docker pull "$(grep -Eo 'ghcr\.io/[^ ]+' /opt/black-relay/src/cameralink/ops/frigate.service)"
   # Frigate's own driver installer, at the Frigate version in use. It installs the headers for the
   # running kernel, and dkms on Trixie, builds driver 4.21.0, and adds its firmware and udev rule.
   cd /tmp
   curl -fsSL -o user_installation.sh \
     https://raw.githubusercontent.com/blakeblackshear/frigate/v0.18.2/docker/hailo8l/user_installation.sh
   bash user_installation.sh
   ```

   After any kernel update, check that `modinfo -F version hailo_pci` still prints `4.21.0`, and run that script again
   if it does not. Then run the installer, giving it a copy of the model:

   ```bash
   sudo /opt/black-relay/src/cameralink/ops/install-node.sh --hef <path>/yolov6n.hef; echo "rc=$?"
   ```

   It repeats steps 5, 6 and 8 safely, and adds what those leave out: the third package, `cameralink/ops` including
   the literal-backslash `run-frigate\x2dcache.mount`, `daemon.json`, `hailo_pci.conf`, the `eth0` profile, deleting
   `Wired connection 1`, the model with its SHA-256 checked, and the persistent journal. `central-hosts.nft` goes in
   only if absent, so a re-run never erases a saved broker. It never reboots, and starts nothing without `--start`.
   With an SSH session on `eth0`, it leaves `Wired connection 1` live until the reboot, so the session is not cut.

   Expect `rc=0` and `READY`. `rc=3` means all is installed, and lists what it cannot supply itself. Any other exit
   names the failed step. Add `--purge-tailscale` on a node from an old image (C48). `tests/container_install_node.sh`
   tests this script on Debian Trixie, without any Pi hardware.

10. **Remove anything site-specific, then power off.** This is every piece of state a clone must not inherit, so run
    it last, whatever bench tests the card has seen. Do not boot the card again before imaging.

    ```bash
    # Stop everything that writes what follows. Nothing here reloads the firewall, so a session stays up.
    sudo systemctl stop camera-link.service frigate.service camera-link-dhcp.service \
      camerabridge-config.path camerabridge.service camera-provision.service
    sudo docker rm -f frigate frigate-spike 2>/dev/null || true
    sudo rm -rf /var/lib/frigate-spike

    sudo rm -f /etc/NetworkManager/system-connections/preconfigured.nmconnection
    # Stops first-boot setup from running again on a clone.
    sudo touch /etc/cloud/cloud-init.disabled
    # Left by any bench test of the build card. Every clone would share them.
    sudo rm -f /etc/NetworkManager/system-connections/black-relay-uplink.nmconnection \
      /etc/NetworkManager/system-connections/black-relay-setup-ap.nmconnection \
      /etc/systemd/timesyncd.conf.d/black-relay.conf
    # The kit's bridge.yaml, mediamtx.env and frigate.env, its provisioning state and its outbox.
    sudo rm -rf /var/lib/camera-provision /var/lib/camerabridge /etc/camerabridge
    # The camera's password, link.json, the proxy secret and the camera leases.
    sudo rm -rf /var/lib/camera-link
    # Frigate's config, database and recordings. The model stays: nothing else puts it there.
    sudo find /var/lib/frigate/config -mindepth 1 -maxdepth 1 ! -name model_cache -exec rm -rf {} +
    sudo find /var/lib/frigate/media -mindepth 1 -delete
    # A bench test may have saved a broker address here. Clones must admit nobody.
    sudo install -m 0644 /opt/black-relay/ops/central-hosts.nft /etc/black-relay/central-hosts.nft
    # WiFi and AP leases, and the networks this card has seen.
    # In a root shell, because that directory is unreadable to the glob otherwise.
    sudo sh -c 'rm -f /var/lib/NetworkManager/*.lease /var/lib/NetworkManager/*.leases \
      /var/lib/NetworkManager/seen-bssids /var/lib/NetworkManager/timestamps'
    # The node's name from the portal, which a clone would otherwise announce over mDNS until its own save.
    sudo hostnamectl set-hostname black-relay-node
    sudo rm -f /root/.bash_history "$HOME/.bash_history" /var/lib/systemd/random-seed
    sudo rm -rf "$HOME/camera-subsystem" /root/kit1-uplink-saved
    sudo journalctl --rotate
    sudo journalctl --vacuum-time=1s
    ```

    Then confirm, before powering off:

    ```bash
    # Each of these must say "No such file or directory".
    sudo ls -d /etc/camerabridge/bridge.yaml /var/lib/camera-link /var/lib/camera-provision /var/lib/frigate-spike \
      /var/lib/frigate/config/config.yml /var/lib/frigate/config/frigate.db
    # Must print only the file's comments, so no address.
    cat /etc/black-relay/central-hosts.nft
    # Must print the model's hash, 6a20a395...124e, and the camera profile, and no site WiFi profile or name.
    sudo sha256sum /var/lib/frigate/config/model_cache/yolov6n.hef
    sudo ls /etc/NetworkManager/system-connections/
    sudo sh -c 'grep -ls ssid /boot/firmware/network-config /etc/netplan/*' || echo "no WiFi in cloud-init or netplan"
    sudo docker images --format '{{.Repository}}:{{.Tag}}'
    sudo poweroff
    ```

    The machine ID and SSH host keys are replaced per kit, in the next section. C41 checks all of it.

11. **Capture the image once,** with the card in the build host. Kits 2-4 are identical to kit 1, so one image
    serves all four. Writing an uncompressed image takes as long as the card is large, and a 128 GB image will not fit
    a card a few sectors smaller, which "128 GB" cards can be.

    ```bash
    # On the build host. Check with lsblk that <sdX> is the card: dd overwrites whatever it is told to.
    lsblk
    sudo dd if=/dev/<sdX> of=black-relay-camera-node.img bs=4M status=progress conv=fsync
    ```

    **Shrink it.** This fits the root filesystem to its data plus 2 GB, cuts the partition and the file to match, and
    changes only the image. The 2 GB is room for the clone's first boot, before seeding expands the filesystem.

    ```bash
    # On the build host, as root. Needs e2fsprogs and fdisk.
    img=black-relay-camera-node.img
    start=$(partx -g -n 2 -o START "$img"); start=$((start))
    sectors=$(partx -g -n 2 -o SECTORS "$img"); sectors=$((sectors))
    loop=$(losetup --find --show --offset $((start * 512)) --sizelimit $((sectors * 512)) "$img")
    e2fsck -f -y "$loop"
    block_size=$(dumpe2fs -h "$loop" 2>/dev/null | awk '/^Block size:/ {print $3}')
    minimum=$(resize2fs -P "$loop" 2>/dev/null | awk -F': ' '{print $2}')
    blocks=$((minimum + 2 * 1024 * 1024 * 1024 / block_size))
    resize2fs "$loop" "$blocks"
    losetup -d "$loop"
    sectors=$((blocks * block_size / 512))
    echo "$start,$sectors" | sfdisk --no-reread --no-tell-kernel -N 2 "$img"
    truncate -s $(((start + sectors) * 512)) "$img"
    ```

    Expect `sfdisk` to report the new size, and the file to be a few GB more than the data. PiShrink does the same
    job: use `pishrink.sh -s`, as `-s` leaves the expansion to the seeding step below. An image too large for the
    next card means this step did not take.

12. **Write the image to each kit's card,** the same size as the build card or larger, and to kit 1's own card if it
    is reused. Then seed each kit, including kit 1, on its own Pi: step 10 removed kit 1's passphrase too.

    ```bash
    # On the build host, for each card. Check with lsblk that <sdX> is the card.
    lsblk
    sudo dd if=black-relay-camera-node.img of=/dev/<sdX> bs=4M status=progress conv=fsync
    ```

## Seed each kit

Do this once per kit, on the kit's own Pi, with the kit's own card. This is what makes clones of one image into
separate kits: each gets its own passphrase, SSH host keys and machine ID, and its serial sets its default name. Use
a console (micro-HDMI and USB keyboard), because the firewall is now active and `eth0` allows no SSH. Until seeding,
the daemon logs failures to start the AP. That is expected.

```bash
# Give this kit its own identity. The clone otherwise shares all of these with every other kit.
sudo raspi-config nonint do_expand_rootfs
sudo rm -f /etc/ssh/ssh_host_*
sudo ssh-keygen -A
sudo rm -f /etc/machine-id /var/lib/dbus/machine-id
sudo systemd-machine-id-setup

# Generate the passphrase, install the AP profile as root-only 0600, and show the label.
sudo /opt/black-relay/ops/seed-kit.sh
```

The script prints the label text once, and the only stored copy is the 0600 profile. It refuses
to run twice, because a second passphrase would make the label wrong. `--force` overrides that.

**Print the label:** the image has no printing stack, so enter the four printed lines into your
label printer's workstation, and do not save them to a file. Fix the label to the kit. Then reboot,
which also finishes the root filesystem expansion, and join the setup WiFi from a phone using only
the printed label, to catch typing errors:

```bash
sudo reboot
```

To reprint a lost label, from a console:

```bash
sudo nmcli --get-values 802-11-wireless.ssid connection show black-relay-setup-ap
sudo nmcli --show-secrets --get-values 802-11-wireless-security.psk connection show black-relay-setup-ap
```

## GPIO setup button

- **Pin:** GPIO17, header pin 11. `--button-line GPIO17` in `camera-provision.service` sets the
  line name.
- **Wiring:** a normally open momentary button between header pin 11 and ground on header pin 9.
  Add no resistor: the read enables the internal pull-up. Never connect it to 3V3 or 5V.
- **With the AI HAT+:** wire to the stacking header above the HAT, and confirm the case leaves
  room for it. Whether GPIO17 is free with the HAT fitted is a field check (C06).
- **To use it:** hold the button, apply power, and keep holding until the setup WiFi appears. The
  daemon reads it once per boot, at startup, and never later.

## First-boot behavior

Timers: join timeout 120 s, AP window 300 s (restarted by each real portal form request), and AP
hard cap 1800 s.

| Situation | What the node does |
|---|---|
| Blank node boots | Scans, then opens the AP and the full portal. `node_id` and hostname start as `cam-<last 6 of serial>`, and the Frigate address follows, all editable. If the serial cannot be read they start blank, and `reading the serial failed` is logged |
| Valid form submitted | Saves all files together and sends the response. The save starts the bridge. It then closes the portal, reloads the firewall set, stops the AP, and joins |
| Normal reboot with a config | Joins silently. No AP |
| Join fails (wrong password, router away) | After 120 s opens the AP with a WiFi-only portal. Rejoins after the window, and alternates until it works |
| AP window ends with no config | Stays on the AP with the full portal |
| Button held at boot | Opens the AP with the full portal, showing the saved non-secret settings. The WiFi and broker passwords stay blank and must be typed again, and the form says so. With a config and no submission, rejoins after the window |
| WiFi lost after provisioning | No AP. NetworkManager keeps retrying, and the daemon re-issues the join every 180 s |
| Central broker or central node down | No effect on provisioning. Once the clock has synced, the bridge queues durable events in the outbox |
| Clock not synced yet | The bridge logs `NTP not yet synchronized; holding durable events until it is`. Durable events are held in RAM only, capped at 10,000, and lost on restart. Nothing enters the outbox, and no heartbeat is sent |

Detect and record do not depend on any of this.

## Field verification checklist

**Status of every check: NOT YET RUN.** Change a check's status only after running it on the real
hardware, and record the date and output.

Bench: one kit with its camera, a second kit for C41, a site router you can power off, a laptop, an
iPhone, an Android phone, a console on the node, and a central broker you can stop. Two commands
recur:

```bash
sudo cat /var/lib/camera-provision/state.json                 # current state
journalctl -b -u camera-provision --no-pager -g ' -> |portal' # state changes this boot
```

The **heartbeat check** also recurs. Run it on a host that reaches the central broker, with the broker
username and password typed into the portal, and the `node_id` typed there. The node's clock must be
synced, or it sends no heartbeat (C31). If the broker's ACL stops this user subscribing, watch the
topic as an administrator instead, and look for the typed username in the broker's connection log.

```bash
mosquitto_sub -h <central> -u <typed-user> -P <typed-password> -t detection/camera/status/nodes \
  -W 90 | grep -m1 '<typed-node-id>'
```

### Coverage

| Required item | Check |
|---|---|
| Review Focus: power cut, node boots before router | C14 |
| Review Focus: wrong password, or AP out of range | C11 |
| Review Focus: central broker or node down | C15 |
| Review Focus: malformed or incomplete submission | C09 |
| Review Focus: `node_id` with newline, uppercase, or empty | C10 |
| Review Focus: re-provision only by held button | C16 |
| Blank boot opens AP | C07 |
| Valid submit joins and persists across reboot | C08 |
| Wrong password falls back | C11 |
| Power cut with the router returning late recovers | C14 |
| Central outage does not open the AP | C15 |
| Button opens AP | C16 |
| WIFI_ONLY fallback | C12 |
| Portal unreachable from `eth0` | C21 |
| AP client cannot reach the camera | C23 |
| `ss -ltnp` shows no portal once provisioned | C22 |
| Real phone captive webviews, datalist, Origin null | C25 |
| `SO_BINDTODEVICE` on `wlan0` | C20 |
| `getfqdn` delay | C26 |
| Real nmcli output against the parsers | C03 |
| Keyfile round trips on Trixie | C05 |
| Full suites on the Pi's Python and NetworkManager | C02 |
| Frigate publish QoS | C32 |
| `camera_fps` on a dead camera | C33 |
| `detector_ok` idle values | C34 |
| Outbox drain time on SD | C35 |
| AP liveness check | C37 |
| Rejoin retry every 180 s | C38 |
| Default identity from the serial | C07, C41 |
| No typed secret in the journal | C40 |
| Services never restarted, and a heartbeat under the typed credentials | C08, C14, C29 |
| mDNS before any reboot | C08 |
| Two kits differ, and a clone holds no golden-image state | C41 |
| `frigate.camera` and zones match Frigate | C43 |
| Time server from the portal, and a move to another broker | C44 |
| Only the central node reaches SSH, Frigate and RTSP, and the set follows a re-provision | C24, C45 |
| The heartbeat does not claim a detector it cannot show working | C34, C46 |
| The heartbeat does not claim recording it cannot show happening | C47 |
| Tailscale is gone from a node re-provisioned from the old image | C48 |
| Camera link, adoption, video, heartbeat truth table, the 24 h run | C49 to C78, in [`field-run-kit1.md`](field-run-kit1.md) |

C19, C39 and C42 are retired, and their numbers are not reused. C45 to C48 above came first, so the plan's Task 8
check numbers moved up. `field-run-kit1.md` has the table. Run kit 1's checks in that file's order, and record results
there.

### Static checks and adapters

**C01. Artifacts parse on the Pi's tool versions. PASSED 2026-11-05, kit 1, Trixie:** nftables 1.1.3,
NetworkManager 1.52.1, systemd 257, dnsmasq 2.91; `nft rc=0`, `verify rc=0`, `syntax check OK`,
and `network-pre.target` in `Before=`.

```bash
sudo nft --version; nmcli --version; systemctl --version | head -1; sudo dnsmasq --version | head -1
sudo nft -c -f /etc/nftables.conf; echo "nft rc=$?"
cd /etc/systemd/system && systemd-analyze verify camera-provision.service camerabridge.service \
  camerabridge-config.path camerabridge-config-changed.service; echo "verify rc=$?"
sudo dnsmasq --test --conf-file=/etc/NetworkManager/dnsmasq-shared.d/black-relay-captive.conf
systemctl cat nftables.service | grep -E 'Before|ExecStop'
```

Expect: NetworkManager at least 1.40 (1.28 for `ap-isolation`, 1.40 for `nmcli --offline`), `nft rc=0`,
`verify rc=0` with no errors about these units, `syntax check OK`, and `network-pre.target` in the
nftables `Before=`. The `nft` check also proves the kernel has the `fib` expression the rule set
uses: without it the whole table fails to load.

**C02. Full suites on the Pi's Python. PASSED 2026-11-05, kit 1, Python 3.13.5, with one skip:** provision
965 passed and 1 skipped, bridge 244 passed. The skip is `test_packaging.py`, because the commands below
leave `uv` off `PATH`. It passes on the build host. Run on a build card, before step 10.

```bash
python3 --version
python3 -m venv /tmp/uv && /tmp/uv/bin/pip install uv
cp -r /opt/black-relay/src /tmp/suite
cd /tmp/suite/provision && /tmp/uv/bin/uv run --python /usr/bin/python3 pytest -rs
cd /tmp/suite/bridge && /tmp/uv/bin/uv run --python /usr/bin/python3 pytest -rs
sudo rm -rf /tmp/uv /tmp/suite
```

Expect: every test passing, and only the `uv` skip. Compare the counts with `uv run pytest -rs`
in each package on the build host, at the same commit; when this line was written they were
provision 966 and bridge 244. A skip of the real-broker tests means mosquitto is missing.

**C03. Real nmcli output against the parsers. PARTLY RUN 2026-11-05, kit 1:** the activated uplink case
parsed and the node reached `provisioned`, on nmcli 1.52.1. Activating, inactive, and AP cases are not run. This is the highest risk: a format
difference means the probe never sees an uplink and the node never reaches `provisioned`.

```bash
nmcli --terse --mode multiline --fields GENERAL.STATE,802-11-wireless.mode,DHCP4 \
  connection show black-relay-uplink
```

Run it with the uplink active, activating, and inactive, and once with `black-relay-setup-ap` as
the profile while the AP is up. Expect, in order: `GENERAL.STATE:activated` with
`802-11-wireless.mode:infrastructure` and a `DHCP4.OPTION[n]:ip_address = ...` line; a state other
than `activated`; no `GENERAL.STATE` line; and mode `ap`. Also run the AP liveness query with the AP
up and down: `nmcli --terse --mode multiline --fields GENERAL.STATE connection show black-relay-setup-ap`
prints `GENERAL.STATE:activated`, then nothing.

Compare all of it with `tests/fixtures/nmcli/`, which was recorded on a laptop with nmcli 1.52.1, the
same version as Trixie on the Pi. Its `README.md` lists the commands. If the text differs, replace the fixtures with the
Pi's output and re-run the suite.

**C04. Other nmcli behavior. NOT YET RUN.**

```bash
sudo nmcli connection down black-relay-setup-ap; echo "rc=$?"        # while it is inactive
time sudo nmcli --wait 0 connection up black-relay-uplink; echo "rc=$?"
nmcli --terse --fields SSID device wifi list --rescan yes ifname wlan0
```

Expect: `rc=10`, a near-instant return with `rc=0`, and one SSID per line with `\:` escaping for a
colon. Run the scan again while the uplink is activating, and record whether it works. A failure
leaves the list empty or stale, and typing the SSID must still work.

**C05. Keyfile round trips on Trixie. NOT YET RUN.** Submit the portal once for each WiFi name
and password pair below, using a router configured to match. Pairs: a name with a leading space;
a name with `;`; a name with a backslash; a non-ASCII name; a password with `#`, `\`, and `;`.

```bash
sudo nmcli --show-secrets --get-values 802-11-wireless.ssid,802-11-wireless-security.psk \
  connection show black-relay-uplink
journalctl -b -u NetworkManager --no-pager -g 'keyfile|black-relay'
```

Expect: values identical to what was typed, the node reaching `provisioned`, and no keyfile
warnings. Then test that staged files are ignored:

```bash
cd /etc/NetworkManager/system-connections
sudo sed 's/^id=.*/id=decoy/; /^uuid=/d' black-relay-uplink.nmconnection \
  | sudo tee .black-relay-uplink.nmconnection.staged.tmp >/dev/null
sudo chmod 600 .black-relay-uplink.nmconnection.staged.tmp
sudo nmcli connection reload
nmcli --fields NAME connection show
sudo rm .black-relay-uplink.nmconnection.staged.tmp
```

Expect: no `decoy` in the list. Also read the AP profile back and confirm `ap-isolation`:
`sudo nmcli --get-values 802-11-wireless.ap-isolation connection show black-relay-setup-ap`
prints `1` or `true`. With the AP up, look for a warning that isolation was not applied:

```bash
sudo journalctl -b -u NetworkManager -u wpa_supplicant --no-pager -g 'isolat'
```

Expect: no warning. No warning does not prove isolation works: the two-client test in C24 is the
proof.

**C06. Button and libgpiod. NOT YET RUN.** With the AI HAT+ fitted.

```bash
gpioinfo GPIO17
gpioget --numeric --active-low --bias=pull-up GPIO17   # released
gpioget --numeric --active-low --bias=pull-up GPIO17   # held
```

Expect: a chip and offset, then `0`, then `1`. Also expect no `reading the setup button failed` in
`journalctl -b -u camera-provision`, which would mean the unit's device rules block libgpiod.

### Onboarding

**C07. Blank boot opens the AP. MOSTLY PASSED 2026-11-05, kit 1:** `ap_up`, full-mode portal, WPA2 SSID
visible within 30 s, joined with the label passphrase, and the form opened. The wrong-passphrase case is
not run. Freshly seeded kit, no config.

```bash
journalctl -b -u camera-provision --no-pager -g ' -> |portal'
nmcli --fields NAME,TYPE,DEVICE connection show --active
sudo iw dev wlan0 info | grep type
```

Expect: `unprovisioned + boot -> ap_up`, `portal listening in full mode`, the profile
`black-relay-setup-ap` active on `wlan0`, and `type AP`. The SSID `BlackRelay-CAM-<serial>` is
visible and advertises WPA2, joined with the label passphrase. The wrong passphrase is refused.

Open the form. Expect `node_id` and hostname to read `cam-` plus the last 6 characters of the SSID's
serial, in lowercase, the Frigate address to read `https://<that name>.local:8971`, all three
editable, and no `reading the serial failed` in the journal.

**C08. Valid submit joins and persists across reboot. MOSTLY PASSED 2026-11-05, kit 1, Ethernet unplugged:**
saved, joined in 10 s, and after a reboot rejoined in 7 s with no `ap_up`, no setup SSID in any scan,
`NRestarts` 0, and heartbeats resuming once NTP synced over WiFi, 60 s after the bridge started. The
first save used a mistyped broker address, so `bridge.yaml` was corrected by hand before the heartbeat
check. A form save reaching a live broker is not yet shown. Submit from a phone with valid values.

```bash
sudo cat /var/lib/camera-provision/state.json
journalctl -b -u camera-provision --no-pager -g ' -> |portal'
sudo ls -la /etc/camerabridge /var/lib/camera-provision /etc/NetworkManager/system-connections
systemctl is-active camerabridge.service
systemctl show -p NRestarts --value camera-provision.service camerabridge.service
# Before the reboot, from a site LAN host:
ping -c 3 <hostname>.local
# Also run the heartbeat check, then:
sudo reboot
```

Expect: the phone shows the saved page before the AP drops; `ap_up + config_received -> joining`,
then `portal closed`, then `joining + uplink_up -> provisioned`. `bridge.yaml` is 0600 and owned
by `camerabridge`, `mediamtx.env` is 0644, the keyfile is 0600, the bridge is `active`, and both
`NRestarts` values are `0`, since this counts only automatic restarts: an `active` unit may be crash
looping. The heartbeat arrives under the typed credentials. After the reboot: `joining` then
`provisioned`, with no `ap_up`, no setup SSID visible at any point (watch from a phone during boot),
and no operator action. Repeat the `NRestarts` and heartbeat checks.

`<hostname>.local` must resolve **before** the reboot, under the typed name. **Unverified:** the daemon
runs `systemctl --no-block try-restart avahi-daemon.service` after `hostnamectl`, on the assumption
that avahi needs it to take the new name. A failure is only logged, as `restarting avahi failed`.
Record whether the name resolves, and whether that log line appears. If it resolves only after the
reboot, snapshot URLs are wrong until then, so raise it.

Also check the hostname, which the daemon sets on entering `joining`: `hostnamectl --static` prints
what was typed, including after the reboot, and `journalctl -b -u camera-provision -g 'hostname'`
shows no `setting the hostname failed`.

**C09. Malformed or incomplete submission. NOT YET RUN.** From a laptop on the AP, on a blank node.
Save a listing first, then compare after each POST.

```bash
# On the node:
sudo ls -la /etc/camerabridge /var/lib/camera-provision /etc/NetworkManager/system-connections > /tmp/before
```

```bash
# On the laptop. Each must end in HTTP 400, with an errors block naming the field:
post() { curl -s -w '\nHTTP %{http_code}\n' -d "$1" http://10.42.0.1/; }
GOOD='wifi_psk=password123&node_id=cam-01&hostname=cam-01&covered_zones=driveway&frigate_base_url=https://cam-01.local:8971&broker_host=10.0.0.2&broker_port=1883&broker_username=u&broker_password=p&mediamtx_address=10.0.0.2:8554'
post "$GOOD"                                                   # missing SSID
post "wifi_ssid=site&${GOOD/broker_host=10.0.0.2/broker_host=broker.lan}"
post "wifi_ssid=site&${GOOD/node_id=cam-01/node_id=-bad}"
post "wifi_ssid=site&${GOOD/hostname=cam-01/hostname=bad_name}"
post "wifi_ssid=site&${GOOD/covered_zones=driveway/covered_zones=}"
post "wifi_ssid=site&${GOOD/broker_host=10.0.0.2/broker_host=100.64.0.1}"
post "wifi_ssid=site&$GOOD&ntp_server=bad_name"
post "wifi_ssid=site&${GOOD/covered_zones=driveway/covered_zones=front%20door}"
post "wifi_ssid=site&${GOOD/broker_password=p/broker_password=pa%24%24word}"
```

```bash
# On the node:
sudo ls -la /etc/camerabridge /var/lib/camera-provision /etc/NetworkManager/system-connections | diff /tmp/before -
```

Expect: nine `HTTP 400`s with a clear message in each body, the state still `ap_up`, and no
difference apart from the `state.json` time. Also try one bad submission from a phone.

Run C10 now, then finish with this positive control, which proves the rejections above came from
validation and not from a broken request:

```bash
# On the laptop:
post "wifi_ssid=site&$GOOD"
```

Expect: `HTTP 200` with the `Settings saved` page, and the state moving to `joining`.

**C10. `node_id` with a newline, uppercase, or empty. NOT YET RUN.** Same setup as C09, before its
positive control.

```bash
post "wifi_ssid=site&${GOOD/node_id=cam-01/node_id=cam-01%0A}"
post "wifi_ssid=site&${GOOD/node_id=cam-01/node_id=CAM-01}"
post "wifi_ssid=site&${GOOD/node_id=cam-01/node_id=}"
```

Expect: three `HTTP 400`s naming `node_id`, no `/etc/camerabridge/bridge.yaml`, and the same empty
`diff` as C09.

**C11. Wrong password or router out of range falls back within the timeout. NOT YET RUN.**

Submit a valid form with a wrong WiFi password. Separately, on a provisioned node, power the
router off and reboot.

```bash
journalctl -b -u camera-provision --no-pager -o short-precise -g ' -> |portal'
sudo iw dev wlan0 info | grep type
```

Expect, in both cases: `joining + uplink_timeout -> fallback_ap` about 120 s after entering
`joining`, then `portal listening in wifi_only mode` and `type AP`. The node then alternates
every 300 s of AP and up to 120 s of joining. It is never left without either.

**C12. WIFI_ONLY fallback. NOT YET RUN.** In `fallback_ap` from C11, connect to the AP.

```bash
curl -s http://10.42.0.1/ | grep -o 'name="[a-z_]*"'
sudo sha256sum /etc/camerabridge/bridge.yaml        # on the node, before
```

Expect: only `wifi_ssid` and `wifi_psk` input names (plus the `viewport` meta name). Submit the
right password, adding `&broker_host=evil.example` by curl: the node joins and reaches
`provisioned`, and `bridge.yaml` has the same hash, so the bridge does not restart.

**C13. Fallback AP with no config serves the full form. NOT YET RUN.** "No config" means no WiFi
profile or no hostname. In `fallback_ap` on the bench only, first show that a damaged bridge config
does not count:

```bash
sudo mv /etc/camerabridge/bridge.yaml /tmp/bridge.yaml.saved
# The daemon checks this file when it starts and at each state change, not continuously.
sudo systemctl restart camera-provision
sleep 5
journalctl -u camera-provision -n 20 --no-pager
sudo mv /tmp/bridge.yaml.saved /etc/camerabridge/bridge.yaml
```

Expect: `the bridge config is missing or invalid; the bridge cannot run`, and the portal stays in
`wifi_only` mode. The node keeps its WiFi and its fallback cycle. Then remove the WiFi profile:

```bash
sudo rm /etc/NetworkManager/system-connections/black-relay-uplink.nmconnection
sudo nmcli connection reload
journalctl -u camera-provision -f -g 'portal'
```

Expect: `portal closed`, then `portal listening in full mode`. A phone sees the full form, and the
node stays on the AP past the 300 s window.

**C14. Power cut, router returns late. NOT YET RUN.** On a provisioned node with a working camera:
cut power to node and router together, restore node power only, wait 10 minutes (long enough to
see at least one `fallback_ap`), then restore the router. Touch nothing on the node.

```bash
journalctl -b -u camera-provision --no-pager -o short-precise -g ' -> '
systemctl is-active camerabridge.service mosquitto.service
systemctl show -p NRestarts --value camerabridge.service mosquitto.service
```

Expect: the node reaches `provisioned` without operator action, within about 7 minutes of the
router returning (up to 300 s of AP and 120 s of joining). The bridge and mosquitto stay `active`
throughout, with `NRestarts` `0` for both, and detection and recording continue. Once the clock has
synced, the heartbeat check passes. Repeat 3 times with the router returning at different points in
the cycle.

**C15. Central outage does not open the AP. NOT YET RUN.** On a provisioned node with a synced clock,
stop the central broker, or the whole central node, for 15 minutes.

```bash
sudo cat /var/lib/camera-provision/state.json
sudo iw dev wlan0 info | grep type
timedatectl show -p NTPSynchronized --value
sudo wc -l /var/lib/camerabridge/outbox.jsonl
journalctl -b -u camera-provision --no-pager -g 'ap_up|fallback_ap'
```

Expect: state `provisioned`, `type managed`, sync `yes`, the outbox growing as events occur, no AP
lines, and no setup SSID visible. Restart the central broker: the outbox drains.

Then reboot during an outage: `joining` then `provisioned` again, with no AP. **The outbox only grows
if the clock syncs after that reboot.** The default time server is the broker host, so stopping the
whole central node stops the sync source too. With the sync source down, expect sync `no`, the journal
line `NTP not yet synchronized; holding durable events until it is`, nothing entering the outbox, and
no heartbeat: events are held in RAM (C31).

**C16. Re-provision only by held button. NOT YET RUN.**

1. Reboot a provisioned node normally. Expect: no AP (C08).
2. Press and release the button while it is running. Expect: nothing happens.
3. Hold it through power-on, per [the button steps](#gpio-setup-button).

```bash
journalctl -b -u camera-provision --no-pager -g ' -> |portal'
```

Expect, for step 3: `boot -> joining`, `joining + reprovision_button -> ap_up`, and
`portal listening in full mode`. The form shows the saved WiFi name, node, zone, Frigate, broker, and
MediaMTX values, and the upside-down box as it was saved. Every password field is blank, and submitting
those blank is refused. Submit a changed `broker_host`: the node joins, and
`systemctl show -p ActiveEnterTimestamp camerabridge` shows the bridge restarted. Then repeat
step 3 and submit nothing: after 300 s the node rejoins by itself with its old config.

**C17. WiFi loss while running does not open the AP. NOT YET RUN.** On a provisioned node, power
the router off for 10 minutes, then on.

```bash
sudo cat /var/lib/camera-provision/state.json
sudo iw dev wlan0 info | grep type
nmcli --fields NAME,DEVICE connection show --active
systemctl is-active camerabridge.service
```

Expect: `provisioned` and `type managed` throughout, the bridge `active`, and no setup SSID. The
uplink returns by itself once the router is back.

**C18. Power loss during a save. NOT YET RUN.** The save is only simulated in tests. On the bench,
cut power within a second of submitting the form, 5 times, on a blank node and on a re-provision.

```bash
sudo ls -la /etc/camerabridge /var/lib/camera-provision /etc/NetworkManager/system-connections
journalctl -b -u camera-provision --no-pager -g ' -> |failed'
```

Expect: every boot ends with the whole old config or the whole new one, including
`/etc/systemd/timesyncd.conf.d/black-relay.conf`, and either a full portal
or a join. No `apply.journal` or `.tmp` files remain after startup. On a blank node left with no
saved config, the bridge is off: no `/etc/camerabridge/bridge.yaml`, and
`systemctl is-active camerabridge.service` prints `inactive`.

### Exposure

**C20. Portal bound to `wlan0`. NOT YET RUN.** With the AP up.

```bash
sudo ss -ltnp 'sport = :80'
journalctl -b -u camera-provision --no-pager -g 'failed|portal'
```

Expect: `10.42.0.1%wlan0:80` owned by the daemon's Python process, `portal listening`, and no
`OSError` failures. An AP client gets the form from `http://10.42.0.1/`.

**C21. Portal unreachable from `eth0`. NOT YET RUN.**

From `eth0`, with the AP up: unplug the camera, plug in a laptop, and give its interface an address
on the camera link, with `<nic>` as its interface.

```bash
# On the laptop:
sudo ip route add 10.42.0.1/32 dev <nic>
curl -m 5 http://10.42.0.1/; echo "curl rc=$?"
dig +time=2 +tries=1 @10.42.0.1 example.com
```

```bash
# On the node:
sudo nft list chain inet black_relay input | grep 'iifname "eth0" counter'
```

Expect: `curl rc=28`, `dig` timing out, and the `eth0` drop counter rising.

From the site LAN: test the firewall alone on a provisioned node, with a temporary wildcard listener
that the portal itself never opens.

```bash
# On the node:
sudo python3 -m http.server 80 --bind 0.0.0.0
```

```bash
# From a site LAN host:
curl -m 5 http://<node-lan-ip>/; echo "curl rc=$?"
dig +time=2 +tries=1 @<node-lan-ip> example.com
```

Expect: `curl rc=28`, no request logged by the listener, and `dig` timing out. Stop the
listener. C20's device binding is the second layer.

**C22. Nothing listens once provisioned. NOT YET RUN.**

```bash
sudo ss -ltnp
sudo ss -lunp
pgrep -a dnsmasq
```

Expect: nothing on port 80, nothing on ports 53 or 67 from NetworkManager's dnsmasq, and `pgrep`
empty. The `camera-provision` process has no listening socket. Port 1883 shows `127.0.0.1` only.

**C23. AP client cannot reach the camera. NOT YET RUN.** With the AP up and the camera connected,
from a laptop on the AP, using `<camera-ip>` and `<pi-eth0-ip>` from Plan 3's addressing.

```bash
# On the laptop:
sudo ip route add <camera-ip>/32 via 10.42.0.1
ping -c 3 -W 2 <camera-ip>
nc -zv -w 3 <camera-ip> 554
nc -zv -w 3 <pi-eth0-ip> 8971
```

```bash
# On the node:
sudo sysctl net.ipv4.ip_forward
sudo nft list chain inet black_relay forward
ffprobe -rtsp_transport tcp -v error -show_entries stream=codec_name "rtsp://<user>:<password>@<camera-ip>/h264Preview_01_sub"
```

Expect: all three laptop commands fail, `oifname "eth0"` counter rising, even if `ip_forward` is 1,
and the node's own RTSP pull still works. Use the camera's real RTSP path from Plan 3 if it
differs, and run `ffprobe` from the Frigate container if it is not on the host.

**C24. Firewall behavior on real interfaces. NOT YET RUN.**

```bash
# From the central node, the address saved as broker_host:
curl -sk -o /dev/null -w '%{http_code}\n' https://<node-lan-ip>:8971/api/config
nc -zv -w 3 <node-lan-ip> 8554
nc -zv -w 3 <node-lan-ip> 5000
nc -zv -w 3 <node-lan-ip> 1883; nc -zv -w 3 <node-lan-ip> 1984
ssh <user>@<node-lan-ip> true; echo "ssh rc=$?"
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password <user>@<node-lan-ip> true
# From any other site LAN host. Each must time out:
nc -zv -w 3 <node-lan-ip> 22; nc -zv -w 3 <node-lan-ip> 8971; nc -zv -w 3 <node-lan-ip> 8554
nc -zv -w 3 <node-lan-ip> 5000; nc -zv -w 3 <node-lan-ip> 1883
ping -c 3 <hostname>.local
# From an AP client, with the AP up, and from a laptop on eth0:
nc -zv -w 3 10.42.0.1 22; nc -zv -w 3 10.42.0.1 8971; nc -zv -w 3 10.42.0.1 1883
```

Expect, from the central node: Frigate answering on 8971, 8554 open, key SSH passing and password SSH
refused, and ports 5000, 1883 and 1984 timing out. From any other LAN host: every port timing out, and
`.local` resolving. All ports failing from the AP and `eth0`. Also from the AP, with two clients: one
cannot ping the other. That depends on the `brcmfmac` driver honoring `ap-isolation`, and this test is
the proof that it does.

Then confirm that 8971 refuses a request without the proxy secret's headers, and that it serves TLS, from
the central node. If 8971 hands out a snapshot to a plain request, stop and raise it.

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://<node-lan-ip>:8971/api/events/none/snapshot.jpg
curl -skv https://<node-lan-ip>:8971/ 2>&1 | grep -E 'subject:|issuer:'
```

Then test the IPv6 and strong-host rules:

```bash
# On the node, with the AP up. Expect no output:
ip -6 addr show wlan0
# From a site LAN host, with the node provisioned. All must fail:
nc -6 -zv -w 3 <node-link-local>%<nic> 8971
nc -6 -zv -w 3 <node-link-local>%<nic> 8554
nc -6 -zv -w 3 <node-link-local>%<nic> 22
sudo ip route add <pi-eth0-ip>/32 via <node-lan-ip>
nc -zv -w 3 <pi-eth0-ip> 8971
sudo ip route del <pi-eth0-ip>/32
```

```bash
# On the node:
sudo ss -ltnp 'sport = :1883'
docker inspect -f '{{.HostConfig.NetworkMode}}' frigate
mosquitto_sub -h 127.0.0.1 -t 'frigate/available' -C 1 -W 10
sudo nft list tables
sudo systemctl reload nftables && sudo nft list tables
```

Expect: `127.0.0.1:1883` only, `host`, `online`, and the same tables, including
`inet black_relay`, before and after the reload.

**C25. Real phone captive webviews. FAILED ONCE 2026-11-05, Pixel 9a:** no captive sheet appeared, and
the form opened and submitted from `http://10.42.0.1/`. Cause unknown, because the journal does not survive
a reboot. iPhone is not run. On an iPhone and an Android phone, in each
one's captive sheet and in its full browser.

From a laptop on the AP first:

```bash
dig +short @10.42.0.1 connectivitycheck.gstatic.com A
dig @10.42.0.1 connectivitycheck.gstatic.com AAAA | grep status
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'Host: captive.apple.com' http://10.42.0.1/hotspot-detect.html
```

Expect: `10.42.0.1`, `status: NOERROR` with no answer, and `302 http://10.42.0.1/`. These results
come from dnsmasq 2.92 here, so confirm them on the Pi's version (2.91), under NetworkManager's real
arguments.

On each phone, expect:

1. Joining with the label passphrase raises the captive sheet by itself. Android with Private DNS
   set to strict may not: record what happens.
2. The form renders and scrolls, and every field is usable.
3. The `<datalist>` lists scanned SSIDs. If a webview does not show it, typing the name must work.
4. Submitting succeeds. **The portal answers 403 to `Origin: null`** by design, so a webview that
   sends it cannot submit at all. If the submit shows "This form must be sent from its own page",
   record the phone and OS, and raise it: this blocks onboarding on that phone and needs a portal
   change.
5. The saved page reaches the phone before the AP stops. The AP stops about one tick later.

**C26. `getfqdn` delay at listener start. NOT YET RUN.** With the AP up, on a blank node:

```bash
sudo systemctl restart camera-provision
journalctl -u camera-provision --no-pager -o short-precise -n 20
time python3 -c "import socket; print(socket.getfqdn('10.42.0.1'))"
```

Record the time between the start and `portal listening`, and the `getfqdn` time. Expect a delay of
no more than a few seconds. There is no upstream DNS on the AP, so a long stall here would hold up
the listener. Record it as a defect if the portal takes over 10 s.

### Services

**C27. Restart policy and hardening. NOT YET RUN.**

```bash
systemctl show -p Restart,RestartUSec,StartLimitIntervalUSec camera-provision camerabridge mosquitto
sudo systemctl kill -s SIGKILL camera-provision
sleep 7; systemctl is-active camera-provision
journalctl -u camera-provision --no-pager -n 10
systemd-analyze security --no-pager camera-provision camerabridge | tail -3
```

Also confirm that the daemon runs as root, which `nmcli` authorization depends on:

```bash
ps -o user= -p "$(systemctl show -p MainPID --value camera-provision)"
```

Expect: `root`; `Restart=always`, `RestartUSec=5s`, and `StartLimitIntervalUSec=0` for all three. The
daemon returns within about 5 s, with `restored state ... after a daemon restart`, and without a new boot
event. Repeat the kill for the other two. C06, C08, and C20 prove the hardening leaves libgpiod,
nmcli, and port 80 working. Also look for `SECCOMP` or `Permission denied` in the unit's journal.

**C28. The AP never starts without its firewall. NOT YET RUN.** On the bench, with a console.

```bash
sudo nft delete table inet black_relay
sudo systemctl restart camera-provision
sleep 12; systemctl status camera-provision --no-pager | head -8
sudo nft -f /etc/nftables.conf
sleep 12; systemctl is-active camera-provision
```

Expect: `activating (auto-restart)` with a failed `ExecStartPre`, and no AP, then `active` after the
table returns.

**C29. Bridge and broker run with WiFi down. NOT YET RUN.** Power the router off, then reboot.

```bash
systemctl is-active mosquitto.service camerabridge.service
systemctl show -p NRestarts --value mosquitto.service camerabridge.service
systemd-analyze critical-chain camerabridge.service
systemctl is-enabled NetworkManager-wait-online.service
mosquitto_sub -h 127.0.0.1 -t 'frigate/stats' -C 1 -W 90 | head -c 200
```

Expect: both `active` within seconds of `NetworkManager`, `NRestarts` `0` for both, no `network-online`
wait in the chain, and `masked`. Frigate stats arrive with WiFi down, because detect and record
continue. Power the router back on: once the clock has synced, the heartbeat check passes, and
`NRestarts` is still `0`.

**C30. Bridge starts and restarts on config. NOT YET RUN.**

```bash
# Blank node, before submitting:
systemctl status camerabridge.service --no-pager | grep -E 'Active|Condition'
systemctl is-active camerabridge-config.path
# After submitting, and again after a re-provision:
systemctl show -p ActiveEnterTimestamp camerabridge
```

Expect: `inactive` with the condition unmet, the path unit `active`, the bridge starting after the
save, and its timestamp moving on a re-provision. It stays put across a WiFi-only repair (C12).

**C31. Time. NOT YET RUN.**

```bash
systemctl is-enabled systemd-timesyncd.service
timedatectl show -p NTPSynchronized --value
timedatectl timesync-status
journalctl -b -u systemd-timesyncd --no-pager | tail -5
journalctl -b -u camerabridge --no-pager -g 'NTP'
```

Expect: `enabled`, then `yes` shortly after the uplink, with the portal's time server as the server, and
`NTP synchronized; clock marked ready`. Repeat on a site with no WAN.

**No-server test.** The kernel's sync flag only clears at boot, so this needs a reboot. Set a server
that cannot answer, with no fallback:

```bash
printf '[Time]\nNTP=192.0.2.1\nFallbackNTP=\n' | sudo tee /etc/systemd/timesyncd.conf.d/black-relay.conf
sudo reboot
```

After the boot, walk in front of the camera, then:

```bash
timedatectl show -p NTPSynchronized --value
journalctl -b -u camerabridge --no-pager -g 'NTP not yet synchronized'
sudo wc -l /var/lib/camerabridge/outbox.jsonl
mosquitto_sub -h <central> -u <user> -P <password> -t 'detection/camera/status/nodes' -W 120 -C 1; echo "rc=$?"
```

Expect: `no`, the journal line, the outbox count unchanged by the walk, and no heartbeat before the
timeout. Other nodes' heartbeats share this topic, so filter by `node_id` on a multi-node site.

Now show that the events are held. Restore the portal's value, as `<portal-ntp>`:

```bash
printf '[Time]\nNTP=<portal-ntp>\n' | sudo tee /etc/systemd/timesyncd.conf.d/black-relay.conf
sudo systemctl restart systemd-timesyncd
journalctl -u camerabridge --no-pager -f -g 'NTP'
```

Expect: `yes`, `NTP synchronized; clock marked ready`, the held events reaching the central broker
with timestamps from the walk, and heartbeats resuming.

Finally, repeat the no-server steps, and run `sudo systemctl restart camerabridge` before restoring the
server. Expect: the events from that walk never arrive. That loss is documented in DESIGN 7.5.

### Frigate and bridge on live hardware

**C32. Frigate publish QoS. NOT YET RUN.** Subscribing at QoS 2 shows the QoS Frigate published at.

```bash
mosquitto_sub -h 127.0.0.1 -t 'frigate/events' -q 2 -d -C 2 | grep PUBLISH
```

Walk in front of the camera. Record the `q` value in `PUBLISH (d0, q..., ...)`. Also record the
retain flag on `frigate/stats`:

```bash
mosquitto_sub -h 127.0.0.1 -t 'frigate/stats' -v -d -C 1 | grep PUBLISH
```

Expect: `r0` for stats. The bridge ignores retained stats. If `q0` on events, record it: a local
broker restart can then lose events in flight, as DESIGN 4.3 already assumes.

**C33. `camera_fps` on a dead camera. NOT YET RUN.** Unplug the camera, then wait 2 stats intervals.
With `<cam>` the Frigate camera name, and the central broker's address and credentials:

```bash
mosquitto_sub -h 127.0.0.1 -t 'frigate/stats' -C 1 | python3 -c "import json,sys; c=json.load(sys.stdin)['cameras']; print(c.get('<cam>',{}).get('camera_fps','missing'))"
mosquitto_sub -h <central> -u <user> -P <password> -t 'detection/camera/status/nodes' -C 1
```

Expect: `0`, `0.0`, or `missing`, with `camera_online=false` and `recording_ok=false` in the node
status. With the camera back: a positive value and both `true`. If the value stays positive,
record it: `camera_online` would then be wrong.

**C34. `detector_ok` idle values. NOT YET RUN.** After a person has walked through view since Frigate
last started, with nothing moving in view for 10 minutes:

```bash
mosquitto_sub -h 127.0.0.1 -t 'frigate/stats' -C 1 | python3 -c "import json,sys; print(json.load(sys.stdin)['detectors'])"
mosquitto_sub -h <central> -u <user> -P <password> -t 'detection/camera/status/nodes' -C 1
```

Expect: each detector with a non-empty `pid`, an `inference_speed` above 0, under 100 and not `10.0`, a
`detection_start` of 0 or under 10 s old, and `detector_ok=true`. If the speed reads 0 when idle, `detector_ok` is falsely false, and the bridge needs
a change. Record the Frigate version, because the bridge reads `inference_speed`, `detection_enabled`,
`processes.recording.pid`, `service.uptime` and `service.last_updated` as v0.18.2 sends them.

**C35. Outbox drain time on SD. NOT YET RUN.** No target has been set. A laptop drained 10,000
entries in 97.4 s, with zero of 185 live events lost, and an SD card may be far slower. Use a
synced clock. With the central broker stopped, publish synthetic events to the local broker:

```bash
python3 -c "
import json
for i in range(10000):
    a={'id':f'drain-{i}','camera':'<cam>','label':'person','score':0.8,'box':[1,2,3,4],'area':100,'stationary':False,'current_zones':['<zone>']}
    print(json.dumps({'type':'new','before':a,'after':a}))
" | mosquitto_pub -h 127.0.0.1 -t frigate/events -q 1 -l
sudo wc -l /var/lib/camerabridge/outbox.jsonl
```

Start the central broker, then:

```bash
time sh -c 'while [ "$(sudo wc -l < /var/lib/camerabridge/outbox.jsonl)" -gt 0 ]; do sleep 5; done'
journalctl -u camerabridge --no-pager --since '-15min' -g 'disconnect|WARNING|ERROR'
```

During the drain, publish some events with new ids, and walk in front of the camera. Expect: the
outbox reaches 0, no local broker disconnects, and the new events all arrive at the central broker.
Record the card model, entry count, and elapsed time. Repeat at the 64 MiB cap if the drain is slow.

**C36. Frigate state returns after a broker restart. NOT YET RUN.** The broker runs without
persistence, on the assumption that Frigate republishes its retained state when it reconnects.

```bash
sudo systemctl restart mosquitto.service
sleep 30
mosquitto_sub -h 127.0.0.1 -t 'frigate/available' -C 1 -W 10
mosquitto_sub -h 127.0.0.1 -t 'frigate/<cam>/recordings/state' -C 1 -W 10
```

Expect: `online` and `ON`. If either never arrives, `recording_ok` stays false after a restart: set
`persistence true` in `/etc/mosquitto/conf.d/black-relay.conf` and record it.

### Daemon behavior added after the first checklist

**C37. AP liveness check. NOT YET RUN.** With the AP up, stop it behind the daemon's back:

```bash
sudo nmcli connection down black-relay-setup-ap
journalctl -u camera-provision -f -g 'no longer active|portal'
```

Expect: within about 10 s, `the AP is no longer active, so restarting it`, then `portal closed`, the
AP returning, and `portal listening`. A phone can reach the form again.

**C38. Rejoin retry. NOT YET RUN.** NetworkManager can stop autoconnecting after a no-secrets failure,
so a provisioned node re-issues the join every 180 s. On a provisioned node, change the router's WiFi
password, wait 5 minutes, and change it back. Touch nothing on the node.

```bash
journalctl -b -u NetworkManager --no-pager -o short-precise -g 'black-relay-uplink' | tail -20
journalctl -b -u camera-provision --no-pager -g ' -> |re-issuing'
sudo cat /var/lib/camera-provision/state.json
```

Expect: activation attempts about 180 s apart while the password is wrong, the uplink back within
about 180 s of restoring it, the state `provisioned` throughout, and no AP.

### Checks added by the final review

**C40. No typed secret in the journal. NOT YET RUN.** After C08 and C09, so the journal
holds a save and rejected submissions. Run this once each for the WiFi password and the broker
password, and once for the typed hostname as a control. `read -s` keeps the
value off the screen, and `printf` keeps it out of any command line.

```bash
read -rs VALUE
sudo journalctl --no-pager | grep -c -F -f <(printf '%s\n' "$VALUE")
unset VALUE
```

Expect: `0` for both secrets, and more than `0` for the hostname, which proves the search works.
The search covers every unit and every saved boot, including NetworkManager and wpa_supplicant. Any
other result is a defect: rotate the secret and raise it.

**C41. Two kits are different, and a clone holds no golden-image state. NOT YET RUN.** Two kits, both
cloned from one image and seeded, with neither submitted yet. On each:

```bash
sudo ls -la /etc/camerabridge/bridge.yaml /var/lib/camera-provision/node.json \
  /etc/systemd/timesyncd.conf.d/black-relay.conf
sudo ls -la /etc/NetworkManager/system-connections/
cat /etc/black-relay/central-hosts.nft
cat /etc/machine-id
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
sudo nmcli --show-secrets --get-values 802-11-wireless-security.psk connection show black-relay-setup-ap
```

Expect: no `bridge.yaml`, `node.json`, or time server drop-in, no `black-relay-uplink` profile, and a `central_hosts` set with no elements.
Between the two kits, a different machine ID, SSH key fingerprint, AP passphrase, and form default for
`node_id`. Then provision both with their defaults, on one site: both kits' heartbeats keep arriving, with no repeated connect and disconnect for either
`node_id` in the central broker's log. Two kits sharing an ID would share an MQTT client ID, and
disconnect each other.

**C43. `frigate.camera` and zones match Frigate. NOT YET RUN.** Needs Plan 3. On the node, with
[requirement 9](#requirements-on-plan-3) in place. The endpoint is from memory of Frigate's API, as in C24.

```bash
sudo /opt/black-relay/provision/bin/python -c "import yaml; c=yaml.safe_load(open('/etc/camerabridge/bridge.yaml')); print(c['frigate']['camera'], sorted(c['covered_zones']))"
curl -s http://127.0.0.1:5000/api/config | python3 -c "import json,sys; c=json.load(sys.stdin)['cameras']; print({n: sorted(v.get('zones', {})) for n, v in c.items()})"
```

Expect: Frigate has exactly one camera, named as `frigate.camera`, and its zones are the same as
`covered_zones`. The first command prints no secrets. Repeat after a re-provision that changes `node_id`
or the zones. In the node status, `camera_online` and `recording_ok` read true.

**C44. Time server from the portal. NOT YET RUN.** Needs [requirement 10](#requirements-on-plan-3), or
any NTP server on the site LAN. Submit a full form with the time server left blank.

```bash
stat -c '%a %U %n' /etc/systemd/timesyncd.conf.d /etc/systemd/timesyncd.conf.d/black-relay.conf
cat /etc/systemd/timesyncd.conf.d/black-relay.conf
timedatectl timesync-status
systemctl show -p ActiveEnterTimestamp systemd-timesyncd
journalctl -b -u camera-provision --no-pager -g 'timesyncd'
```

Expect: `755 root` and `644 root`, `NTP=<broker host>`, `timesync-status` naming that server, timesyncd
restarted after the save, and no `restarting systemd-timesyncd failed`, which would mean the unit's
hardening blocks it. Then:

1. **Explicit value:** hold the button at boot. The form shows the broker host in the time server
   field. Type another server, and submit. Expect the saved page, the drop-in, and `timesync-status`
   all to name it.
2. **Move to another broker:** repeat from a node on the default, changing `broker_host` and leaving
   the time server field as it was. Expect the saved page, the drop-in, and `timesync-status` all to
   name the new broker. Repeat with an explicit server: it is kept.
3. **WiFi repair:** in `fallback_ap`, note `sha256sum` of the drop-in, submit the right WiFi password
   adding `&ntp_server=evil.example` by curl, and expect the same hash.
4. **No reachable server:** with the server stopped, on a fresh boot, expect sync `no`, the bridge's
   `NTP not yet synchronized; holding durable events until it is`, and the node still joining
   and staying off the AP.

**C45. The firewall set follows a re-provision. NOT YET RUN.** Keep a console attached: this changes who
can reach the node.

```bash
cat /etc/black-relay/central-hosts.nft
sudo nft list set inet black_relay central_hosts
journalctl -b -u camera-provision --no-pager -g 'firewall'
```

1. Hold the button at boot, change `broker_host` to a second address, and submit. Expect the file and the
   live set both to name the new address, no `reloading the firewall failed`, SSH working from the new
   address and timing out from the old one.
2. Make the reload fail, with `sudo mv /etc/nftables.conf /etc/nftables.conf.saved`, then submit another
   change. Expect `reloading the firewall failed` every 30 s, the node still joining and reaching
   `provisioned`, and the old set still live. Restore the file: the next retry loads the new set.
   Also confirm what a change leaves open: an SSH session opened from the old address before the
   save carries on, because a reload does not flush conntrack, and a new one from that address times out.
3. The camera-link daemon's unit reaches docker: `journalctl -b -u camera-link --no-pager` shows no
   failure, and C46 below passes.

**C46. `detector_ok` on kit 1's two false signals. NOT YET RUN.**

```bash
mosquitto_sub -h <central> -u <user> -P <password> -t 'detection/camera/status/nodes' -C 1
cat /run/camera-link/state.json
```

1. Set `detect.enabled: false` in Frigate's config by hand, and restart it. Expect `detector_ok=false`.
2. After a fresh Frigate start with nothing moving in view: expect `detector_ok=false` until the first
   person or motion, then `true`, and still `true` with nothing moving afterwards.
3. Make the Hailo detector fail, for instance by removing `force_desc_page_size=4096` and rebooting.
   Save the real `docker logs frigate` lines: the tests' fixtures are built from Frigate's source, and
   these should replace them. Within about 30 s of a line, expect `detector_error: true` in the state
   file, a `reason`, and `detector_ok=false`.
4. **Recovery is automatic.** `camera-link` restarts Frigate once the error has lasted 120 s, at most once
   per 10 minutes, even across its own restarts. Only the new container clears the error. With the fault
   still there, expect a restart about every 10 minutes and `detector_ok=false` throughout, and with it
   cleared, the error gone after the next restart. To retry sooner, an operator can run
   `sudo systemctl restart frigate`. Record any line that sets the error on a clean start or stop:
   each costs a restart.
5. Stop `camera-link`, or make docker unreachable to it. Expect `detector_ok=false` at once when the state
   file goes, or within 90 s of the last good read if the daemon is alive but blind, with `log_read_at`
   standing still in the state file. After 5 minutes of a frozen file, the `camera_link_state` attribute
   drops from the heartbeat. `node_address` stays, and is `wlan0`'s address.

**C47. `recording_ok` follows real segments. NOT YET RUN.**

```bash
sudo ls -la --time-style=full-iso /run/frigate-cache | tail -5
findmnt /run/frigate-cache
python3 -c "import json; print(json.load(open('/run/camera-link/state.json'))['record_writing'])"
mosquitto_sub -h <central> -u <user> -P <password> -t 'detection/camera/status/nodes' -C 1
```

Expect: a `tmpfs` of 256 MB, mode 0700, a `<cam>@<time>.mp4` whose time keeps moving, even with nothing
moving in view and motion-only retention, and `record_writing` and `recording_ok` true. Record how far
behind the clock the in-use segment's time ever falls: the limit is 60 s. Then:

1. **Dead main stream:** block the record stream only, for instance by giving the camera input a wrong
   path for `h264Preview_01_main` on the bench. Within 60 s expect `record_writing=false` and
   `recording_ok=false`, with `camera_online=true`, because detection's sub stream carries on.
2. **Full disk:** fill the media disk to under 5% free with a scratch file, and remove it afterwards.
   Expect the same two false, and both true again once it is gone.
3. `camera-link` must also see the mount from inside its sandbox. If `record_writing` stays false with
   segments visible on the host, its unit is the place to look.

**C48. Tailscale removed from a node on the old image. NOT YET RUN.** Kit 1 is re-provisioned, not
re-imaged, so the firewall change leaves the package, daemon and login behind. The installer removes them, and
refuses if the SSH session it runs in is on `tailscale0`:

```bash
sudo /opt/black-relay/src/cameralink/ops/install-node.sh --purge-tailscale
```

Then check:

```bash
dpkg -s tailscale 2>&1 | head -1
systemctl status tailscaled.service --no-pager 2>&1 | head -1
ip link show tailscale0 2>&1 | head -1
sudo ls -la /var/lib/tailscale /var/lib/camera-provision/tailscale.key 2>&1
sudo python3 -c "import json; print(sorted(json.load(open('/var/lib/camera-provision/node.json'))))"
```

Run the last line after the portal save, which rewrites the file. Expect: the package not installed, the unit not found, no `tailscale0`, both paths missing, and no
`vpn_requested` in `node.json`. Also remove the node from the tailnet's admin console.

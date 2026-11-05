# Camera node onboarding: AP stack, services, and image assembly

Builds the SD image for a camera edge node and defines how to check it in the field.

**Nothing here has been verified on hardware.** Every check in the
[field verification checklist](#field-verification-checklist) is labeled NOT YET RUN.

**Covered by Plan 3, not here:** Frigate, go2rtc, camera link setup (the `eth0` static address), and bench configuration of the camera. Plan 3 must meet
[these requirements](#requirements-on-plan-3).

## Files

| File | Installed as | Purpose |
|---|---|---|
| `black-relay-setup-ap.nmconnection.tmpl` | `/etc/NetworkManager/system-connections/black-relay-setup-ap.nmconnection` (by `seed-kit.sh`) | Setup AP profile |
| `seed-kit.sh` | `/opt/black-relay/ops/` | Renders the profile, prints the kit label |
| `black-relay-captive.conf` | `/etc/NetworkManager/dnsmasq-shared.d/` | Captive DNS |
| `black-relay.nft` | `/etc/nftables.conf` | Firewall |
| `camera-provision.service` | `/etc/systemd/system/` | Provisioning daemon, the only watchdog |
| `camerabridge.service` | `/etc/systemd/system/` | Sample bridge unit |
| `camerabridge-config.path`, `camerabridge-config-changed.service` | `/etc/systemd/system/` | Start or restart the bridge when `bridge.yaml` changes |
| `mosquitto-black-relay.conf` | `/etc/mosquitto/conf.d/black-relay.conf` | Node-local broker |
| `mosquitto-service-override.conf` | `/etc/systemd/system/mosquitto.service.d/black-relay.conf` | Broker restart policy |

## Design decisions

### AP stack

NetworkManager already owns `wlan0` on Bookworm, so the AP is a NetworkManager profile with
`ipv4.method=shared`, not separate hostapd and dnsmasq. NetworkManager starts its own dnsmasq
for the AP, which needs the `dnsmasq-base` package. Do not install the full `dnsmasq` package:
its system service would take port 53.

Balena wifi-connect could run the WiFi-join flow, but its fixed UI collects only WiFi
credentials. This plan runs its own portal because it also collects VPN, hostname, `node_id`,
and central endpoints (DESIGN 7.2).

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

Input and forward default to drop. Output is open, so the node can reach the camera, site LAN,
and tailnet. A reply to the node's own traffic is accepted on any interface. `wlan0` has two
roles, told apart by address: anything from `10.42.0.0/24` or to `10.42.0.1` is the AP.

| Traffic that starts from | Allowed in |
|---|---|
| `lo` | Everything, including the broker and Frigate's internal ports |
| `wlan0`, AP client | Portal (TCP 80), DNS (TCP/UDP 53), and DHCP (UDP 67), to `10.42.0.1` only |
| `wlan0`, site LAN | SSH (22), Frigate (5000, 8971), DHCP replies, Tailscale direct (UDP 41641), mDNS (so `<host>.local` in `snapshot_url` resolves), ping, IPv6 neighbor discovery. Link-local IPv6 cannot reach SSH or Frigate, and only addresses held on `wlan0` itself are reachable |
| `tailscale0` | SSH (22), Frigate (5000, 8971), ping |
| `eth0` | Nothing |
| Forwarding | Nothing. Explicit drops to and from `eth0`, and a default drop for everything else |

- **Camera link:** the camera is statically addressed (DESIGN 11), so `eth0` needs no DHCP. The node
  opens RTSP to the camera, so the reply is accepted. RTSP must use TCP: UDP RTP from the camera
  starts a new flow and is dropped.
- **Port 5000 has no authentication.** Any host on the site LAN and any tailnet peer gets Frigate's
  unauthenticated API on port 5000. That may include writing Frigate's config: this is unverified,
  and C24 records the answer. `frigate.base_url` and DESIGN 5.6 use this port for snapshots, so it
  is open today. **Whether it stays open is an owner decision that is still open.** Port 8971 is the
  authenticated UI.
- **Other Frigate ports:** the firewall leaves go2rtc (1984), RTSP (8554), and WebRTC (8555) closed
  from outside the node.
- **Reloads:** the file replaces only its own table, so `systemctl reload nftables` keeps the tables
  that NetworkManager and tailscaled own. **Do not run `systemctl stop nftables` or
  `systemctl restart nftables`:** the Debian unit's `ExecStop` runs `nft flush ruleset`, which removes
  every table, including theirs, and restart runs it too.
- **If nftables fails at boot:** with host networking, Frigate's ports are then open on every
  interface, including port 5000 on `eth0` and to AP clients, and nothing signals it. The daemon
  refuses to start, so no AP opens, but that does not protect Frigate. Plan 3's `Requires=nftables.service`
  on the Frigate unit covers this: Frigate does not start without the firewall.

### SSH policy

- **Allowed:** site LAN on `wlan0`, and `tailscale0`. A node without Tailscale would otherwise
  have no remote access.
- **Denied:** AP clients and `eth0`. While the AP is up, the only access is a console (micro-HDMI
  and USB keyboard).
- **Authentication:** keys only, with root login off (image build step 7).

### Service ordering

| Unit | Starts after | Why |
|---|---|---|
| `nftables.service` | Early boot, before `network-pre.target` | Firewall exists before any interface is up |
| `NetworkManager.service` | `network-pre.target` | |
| `camera-provision.service` | `NetworkManager`, `nftables` | `ExecStartPre` refuses to start without the `black_relay` table, because the AP turns on IP forwarding |
| `mosquitto.service` | `network-online.target`, from the Debian unit | Instant once wait-online is masked, see below |
| `camerabridge.service` | `NetworkManager`, `mosquitto`, ordering only | Wants mosquitto but does not require it |
| `tailscaled.service` | Independent | Enabled on its own |

- **No `network-online.target` wait:** image build step 9 masks
  `NetworkManager-wait-online.service`. Otherwise, on a boot with WiFi down, the Debian mosquitto unit
  and the bridge behind it wait up to 60 s, while detect and record should continue. Docker's own
  unit would wait the same way. Disabling is not enough: `NetworkManager.service` has
  `Also=NetworkManager-wait-online.service`, so enabling NetworkManager enables it again.
- **No dependency on `tailscaled`:** the bridge uses the site LAN, and the daemon only calls
  `tailscale up` once provisioned and retries with backoff. The daemon must not wait on it,
  because it reads the setup button once at startup.
- **No dependency between the bridge and the daemon:** provisioning state does not affect the
  bridge once `bridge.yaml` exists.
- **Restart policy:** `camera-provision`, `camerabridge`, and mosquitto (by drop-in) use
  `Restart=always`, `RestartSec=5`, and `StartLimitIntervalSec=0`.
- **Daemon hardening:** it runs as root with a reduced capability set. `CAP_DAC_OVERRIDE` is
  required: after `bridge.yaml` is given to `camerabridge` with mode 0600, root can no longer read
  it otherwise, and the node would act as if it had no config.

### Time

`systemd-timesyncd` syncs the clock once there is an uplink. The portal has no NTP field, so the
server is set in image build step 8. The bridge reads
`timedatectl show -p NTPSynchronized --value`, and until that says `yes` (DESIGN 7.5):

- It logs `NTP not yet synchronized; holding durable events until it is`.
- `object_start`, `object_end`, and cue-acks are held **in RAM only**, capped at 10,000 with the
  oldest dropped first, and **lost on any restart or power cut**.
- **Nothing enters the outbox**, so the outbox gives these events no protection.
- `object_update` is dropped, and **no heartbeat is sent**, so the node looks dead to the central
  node, though it is still detecting and recording.

**Do not use the central node as the only NTP source.** A node that boots during a central outage
would never sync, so its events stay in RAM for the whole outage, which is when the outbox is needed.
List the site gateway first, then the central node.

To change the server on a running node:

```bash
sudo install -d /etc/systemd/timesyncd.conf.d
printf '[Time]\nNTP=<site-ntp>\n' | sudo tee /etc/systemd/timesyncd.conf.d/black-relay.conf
sudo systemctl restart systemd-timesyncd
timedatectl timesync-status
```

`<site-ntp>` is one address or a space-separated list.

### Requirements on Plan 3

1. Frigate, and go2rtc if run apart from it, use `network_mode: host`. Frigate's `mqtt.host` is
   `127.0.0.1` and its port is 1883.
2. Frigate pulls RTSP over TCP. No container may rely on forwarding: the forward chain drops it.
3. Docker runs with no bridge of its own, in `/etc/docker/daemon.json`:
   `{"bridge": "none", "iptables": false, "ip6tables": false}`. This is not required for
   isolation, since a drop in this rule set is final, but it keeps Docker's rules out of the way.
4. `eth0` has a static NetworkManager profile with `ipv4.method=manual`, `ipv4.never-default=yes`,
   `ipv6.method=disabled`, never `shared`. Delete the automatic `Wired connection 1` profile.
5. go2rtc reads `MEDIAMTX_ADDRESS` from `EnvironmentFile=/etc/camerabridge/mediamtx.env`, never
   `bridge.yaml`.
6. Opening another port from outside the node means editing `black-relay.nft`.
7. The Frigate container is not privileged, and gets only the devices it needs.
8. The unit that starts Frigate has `Requires=nftables.service` and `After=nftables.service`, so
   Frigate never runs without the firewall. Stopping nftables then stops Frigate too.

## Build the image

Use a small card for the build, since the image is the size of the card. Commands run on the Pi
unless marked. `<CC>`, `<build-pi>`, `<site-ntp>`, and `<sdX>` are placeholders.

1. **Flash** Raspberry Pi OS Lite (64-bit), Bookworm. In Raspberry Pi Imager set a user and SSH
   key, and **no WiFi**, since Imager's WiFi setting leaves a `preconfigured` profile behind. Boot
   with `eth0` on a network with internet access.

2. **Install packages.**

   ```bash
   sudo apt-get update
   sudo apt-get install -y network-manager dnsmasq-base nftables gpiod python3-venv \
     mosquitto mosquitto-clients systemd-timesyncd avahi-daemon iw rsync curl
   curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
     | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
   curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
     | sudo tee /etc/apt/sources.list.d/tailscale.list
   sudo apt-get update
   sudo apt-get install -y tailscale
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
     /etc/systemd/system/mosquitto.service.d
   sudo install -m 0644 README.md black-relay-setup-ap.nmconnection.tmpl /opt/black-relay/ops/
   sudo install -m 0755 seed-kit.sh /opt/black-relay/ops/
   sudo install -m 0644 black-relay-captive.conf /etc/NetworkManager/dnsmasq-shared.d/
   sudo install -m 0644 mosquitto-black-relay.conf /etc/mosquitto/conf.d/black-relay.conf
   sudo install -m 0644 mosquitto-service-override.conf \
     /etc/systemd/system/mosquitto.service.d/black-relay.conf
   sudo install -m 0644 black-relay.nft /etc/nftables.conf
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

8. **Set the NTP servers for the site:** the site gateway first, then the central node
   ([why](#time)). Fusion must not depend on the WAN, so do not rely on the fallback public pool.

   ```bash
   sudo install -d /etc/systemd/timesyncd.conf.d
   printf '[Time]\nNTP=<site-ntp>\n' | sudo tee /etc/systemd/timesyncd.conf.d/black-relay.conf
   ```

9. **Enable the services.** Do not use `--now`: applying the firewall here would cut the build
   session on `eth0`, and starting the daemon would write state into the image.

   ```bash
      sudo systemctl enable nftables.service NetworkManager.service mosquitto.service \
     tailscaled.service systemd-timesyncd.service camera-provision.service \
     camerabridge.service camerabridge-config.path
   # After the enable, which turns wait-online back on through Also=.
   sudo systemctl mask NetworkManager-wait-online.service
   ```

10. **Do the Plan 3 steps** that belong in the image, without rebooting. A reboot would apply the
    firewall and cut the build session on `eth0`.

11. **Remove anything site-specific, then power off.** Do not boot the card again before imaging.

    ```bash
    sudo rm -f /etc/NetworkManager/system-connections/preconfigured.nmconnection
    sudo rm -rf /var/lib/camera-provision /var/lib/camerabridge /etc/camerabridge
    sudo systemctl stop tailscaled.service
    sudo rm -rf /var/lib/tailscale
    sudo poweroff
    ```

12. **Capture the image** with the card in the build host, then write it to each kit's card.

    ```bash
    # On the build host:
    sudo dd if=/dev/<sdX> of=black-relay-camera-node.img bs=4M status=progress conv=fsync
    ```

## Seed each kit

Do this once per kit, on the kit's own Pi, with the kit's own card. Use a console (micro-HDMI and
USB keyboard), because the firewall is now active and `eth0` allows no SSH. Until seeding, the
daemon logs failures to start the AP. That is expected.

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
| Blank node boots | Scans, then opens the AP and the full portal |
| Valid form submitted | Saves all files together and sends the response. The save starts the bridge. It then closes the portal, stops the AP, and joins. On L3 uplink (associated, with a DHCP lease) it logs in to Tailscale if requested |
| Normal reboot with a config | Joins silently. No AP |
| Join fails (wrong password, router away) | After 120 s opens the AP with a WiFi-only portal. Rejoins after the window, and alternates until it works |
| AP window ends with no config | Stays on the AP with the full portal |
| Button held at boot | Opens the AP with the full portal. With a config and no submission, rejoins after the window |
| WiFi lost after provisioning | No AP. NetworkManager keeps retrying, and the daemon re-issues the join every 180 s |
| Central broker, central node, or Tailscale down | No effect on provisioning. Once the clock has synced, the bridge queues durable events in the outbox |
| Clock not synced yet | The bridge logs `NTP not yet synchronized; holding durable events until it is`. Durable events are held in RAM only, capped at 10,000, and lost on restart. Nothing enters the outbox, and no heartbeat is sent |

Detect and record do not depend on any of this.

## Field verification checklist

**Status of every check: NOT YET RUN.** Change a check's status only after running it on the real
hardware, and record the date and output.

Bench: one kit with its camera, a site router you can power off, a laptop, an iPhone, an Android
phone, a console on the node, and a central broker you can stop. Two commands recur:

```bash
sudo cat /var/lib/camera-provision/state.json                 # current state
journalctl -b -u camera-provision --no-pager -g ' -> |portal' # state changes this boot
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
| Portal unreachable from `eth0` and `tailscale0` | C21 |
| AP client cannot reach the camera | C23 |
| `ss -ltnp` shows no portal once provisioned | C22 |
| Real phone captive webviews, datalist, Origin null | C25 |
| `SO_BINDTODEVICE` on `wlan0` | C20 |
| `getfqdn` delay | C26 |
| Real nmcli output against the parsers | C03 |
| Keyfile round trips on Bookworm | C05 |
| Full suites on Python 3.11 and Bookworm's NetworkManager | C02 |
| Frigate publish QoS | C32 |
| `camera_fps` on a dead camera | C33 |
| `detector_ok` idle values | C34 |
| Outbox drain time on SD | C35 |
| AP liveness check | C37 |
| Rejoin retry every 180 s | C38 |
| Moving a node to another tailnet | C39 |

### Static checks and adapters

**C01. Artifacts parse on Bookworm's tool versions. NOT YET RUN.**

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
Earlier offline checks used newer versions than Bookworm ships.

**C02. Full suites on Bookworm's Python 3.11. NOT YET RUN.** Run on a build card, before step 11.

```bash
python3 --version
python3 -m venv /tmp/uv && /tmp/uv/bin/pip install uv
cp -r /opt/black-relay/src /tmp/suite
cd /tmp/suite/provision && /tmp/uv/bin/uv run --python /usr/bin/python3 pytest -rs
cd /tmp/suite/bridge && /tmp/uv/bin/uv run --python /usr/bin/python3 pytest -rs
sudo rm -rf /tmp/uv /tmp/suite
```

Expect: Python 3.11, and every test passing with no skips. Compare with the counts in the latest
Task 6 report. A skip of the real-broker tests means mosquitto is missing.

**C03. Real nmcli output against the parsers. NOT YET RUN.** This is the highest risk: a format
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

Compare all of it with `tests/fixtures/nmcli/`, which was recorded on a laptop with nmcli 1.52.1, not
on Bookworm. Its `README.md` lists the commands. If the text differs, replace the fixtures with the
Bookworm output and re-run the suite.

**C04. Other nmcli behavior. NOT YET RUN.**

```bash
sudo nmcli connection down black-relay-setup-ap; echo "rc=$?"        # while it is inactive
time sudo nmcli --wait 0 connection up black-relay-uplink; echo "rc=$?"
nmcli --terse --fields SSID device wifi list --rescan yes ifname wlan0
```

Expect: `rc=10`, a near-instant return with `rc=0`, and one SSID per line with `\:` escaping for a
colon. Run the scan again while the uplink is activating, and record whether it works. A failure
leaves the list empty or stale, and typing the SSID must still work.

**C05. Keyfile round trips on Bookworm. NOT YET RUN.** Submit the portal once for each WiFi name
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
gpiofind GPIO17
gpioget --active-low --bias=pull-up $(gpiofind GPIO17)   # released
gpioget --active-low --bias=pull-up $(gpiofind GPIO17)   # held
```

Expect: a chip and offset, then `0`, then `1`. Also expect no `reading the setup button failed` in
`journalctl -b -u camera-provision`, which would mean the unit's device rules block libgpiod.

### Onboarding

**C07. Blank boot opens the AP. NOT YET RUN.** Freshly seeded kit, no config.

```bash
journalctl -b -u camera-provision --no-pager -g ' -> |portal'
nmcli --fields NAME,TYPE,DEVICE connection show --active
sudo iw dev wlan0 info | grep type
```

Expect: `unprovisioned + boot -> ap_up`, `portal listening in full mode`, the profile
`black-relay-setup-ap` active on `wlan0`, and `type AP`. The SSID `BlackRelay-CAM-<serial>` is
visible and advertises WPA2, joined with the label passphrase. The wrong passphrase is refused.

**C08. Valid submit joins and persists across reboot. NOT YET RUN.** Submit from a phone with
valid values.

```bash
sudo cat /var/lib/camera-provision/state.json
journalctl -b -u camera-provision --no-pager -g ' -> |portal'
sudo ls -la /etc/camerabridge /var/lib/camera-provision /etc/NetworkManager/system-connections
systemctl is-active camerabridge.service
sudo reboot
```

Expect: the phone shows the saved page before the AP drops; `ap_up + config_received -> joining`,
then `portal closed`, then `joining + uplink_up -> provisioned`. `bridge.yaml` is 0600 and owned
by `camerabridge`, `mediamtx.env` is 0644, the keyfile is 0600, and the bridge is `active`. After
the reboot: `joining` then `provisioned`, with no `ap_up`, no setup SSID visible at any point
(watch from a phone during boot), and no operator action.

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
GOOD='wifi_psk=password123&node_id=cam-01&hostname=cam-01&covered_zones=driveway&frigate_base_url=http://cam-01.local:5000&broker_host=10.0.0.2&broker_port=1883&broker_username=u&broker_password=p&mediamtx_address=10.0.0.2:8554'
post "$GOOD"                                                   # missing SSID
post "wifi_ssid=site&$GOOD&vpn_requested=on"                   # VPN with empty key
post "wifi_ssid=site&${GOOD/node_id=cam-01/node_id=-bad}"
post "wifi_ssid=site&${GOOD/hostname=cam-01/hostname=bad_name}"
post "wifi_ssid=site&${GOOD/covered_zones=driveway/covered_zones=}"
```

```bash
# On the node:
sudo ls -la /etc/camerabridge /var/lib/camera-provision /etc/NetworkManager/system-connections | diff /tmp/before -
```

Expect: five `HTTP 400`s with a clear message in each body, the state still `ap_up`, and no
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

Repeat with the Tailscale key already used, so `/var/lib/camera-provision/tailscale.key` is gone:
the repair must still succeed.

**C13. Fallback AP with no config serves the full form. NOT YET RUN.** "No config" means no WiFi
profile or no hostname. In `fallback_ap` on the bench only, first show that a damaged bridge config
does not count:

```bash
sudo mv /etc/camerabridge/bridge.yaml /tmp/bridge.yaml.saved
journalctl -u camera-provision -n 5 --no-pager
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
```

Expect: the node reaches `provisioned` without operator action, within about 7 minutes of the
router returning (up to 300 s of AP and 120 s of joining). The bridge and mosquitto stay `active`
throughout, and detection and recording continue. Repeat 3 times with the router returning at
different points in the cycle.

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
if the clock syncs after that reboot.** With the sync source also down, expect sync `no`, the journal
line `NTP not yet synchronized; holding durable events until it is`, nothing entering the outbox, and
no heartbeat: events are held in RAM (C31).

Repeat with Tailscale logged out or `tailscaled` stopped.

**C16. Re-provision only by held button. NOT YET RUN.**

1. Reboot a provisioned node normally. Expect: no AP (C08).
2. Press and release the button while it is running. Expect: nothing happens.
3. Hold it through power-on, per [the button steps](#gpio-setup-button).

```bash
journalctl -b -u camera-provision --no-pager -g ' -> |portal'
```

Expect, for step 3: `boot -> joining`, `joining + reprovision_button -> ap_up`, and
`portal listening in full mode`. Submit a changed `broker_host`: the node joins, and
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

Expect: every boot ends with the whole old config or the whole new one, and either a full portal
or a join. No `apply.journal` or `.tmp` files remain after startup. On a blank node left with no
saved config, the bridge is off: no `/etc/camerabridge/bridge.yaml`, and
`systemctl is-active camerabridge.service` prints `inactive`.

**C19. Tailscale login. NOT YET RUN.** Submit with VPN on and a real key.

```bash
tailscale status
sudo ls -la /var/lib/camera-provision/tailscale.key
tailscale version
journalctl -b -u camera-provision --no-pager -g 'tailscale'
```

Expect: logged in with the portal's hostname, the key file gone, and the login inside the daemon's
60 s command timeout. With a bad key: `tailscale login failed, retrying`, backing off from 30 s,
the state stays `provisioned`, and the AP stays down.

### Exposure

**C20. Portal bound to `wlan0`. NOT YET RUN.** With the AP up.

```bash
sudo ss -ltnp 'sport = :80'
journalctl -b -u camera-provision --no-pager -g 'failed|portal'
```

Expect: `10.42.0.1%wlan0:80` owned by the daemon's Python process, `portal listening`, and no
`OSError` failures. An AP client gets the form from `http://10.42.0.1/`.

**C21. Portal unreachable from `eth0` and `tailscale0`. NOT YET RUN.**

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

From `tailscale0`: Tailscale cannot be up while the radio serves the AP, so test the firewall alone
on a provisioned node, with a temporary wildcard listener that the portal itself never opens.

```bash
# On the node:
sudo python3 -m http.server 80 --bind 0.0.0.0
```

```bash
# On a tailnet peer, then with <node-lan-ip> from a site LAN host:
curl -m 5 http://<node-tailscale-ip>/; echo "curl rc=$?"
dig +time=2 +tries=1 @<node-tailscale-ip> example.com
curl -m 5 http://<node-lan-ip>/; echo "curl rc=$?"
```

Expect: `curl rc=28` each time, no request logged by the listener, and `dig` timing out. Stop the
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
nc -zv -w 3 <pi-eth0-ip> 5000
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
# From a site LAN host:
curl -s -o /dev/null -w '%{http_code}\n' http://<node-lan-ip>:5000/api/version
nc -zv -w 3 <node-lan-ip> 1883; nc -zv -w 3 <node-lan-ip> 1984; nc -zv -w 3 <node-lan-ip> 8554
ssh <user>@<node-lan-ip> true; echo "ssh rc=$?"
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password <user>@<node-lan-ip> true
ping -c 3 <hostname>.local
# From a tailnet peer:
curl -s -o /dev/null -w '%{http_code}\n' http://<node-tailscale-ip>:5000/api/version
ssh <user>@<node-tailscale-ip> true; echo "ssh rc=$?"
# From an AP client, with the AP up, and from a laptop on eth0:
nc -zv -w 3 10.42.0.1 22; nc -zv -w 3 10.42.0.1 5000; nc -zv -w 3 10.42.0.1 1883
```

Expect: `200` from the LAN and the tailnet; the broker, go2rtc, and RTSP ports refused or timed out
from the LAN; key SSH passing and password SSH refused; `.local` resolving; all ports failing from
the AP and `eth0`. Also from the AP, with two clients: one cannot ping the other. That depends on
the `brcmfmac` driver honoring `ap-isolation`, and this test is the proof that it does.

Record what port 5000 exposes, from a site LAN host on the bench. The endpoint is from memory of
Frigate's API, so confirm it against the deployed version. Report the result to the owner, for the
[open decision](#firewall-policy):

```bash
curl -s http://<node-lan-ip>:5000/api/config | head -c 300; echo
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' \
  http://<node-lan-ip>:5000/api/config/set
```

Then test the IPv6 and strong-host rules:

```bash
# On the node, with the AP up. Expect no output:
ip -6 addr show wlan0
# From a site LAN host, with the node provisioned. All must fail:
nc -6 -zv -w 3 <node-link-local>%<nic> 5000
nc -6 -zv -w 3 <node-link-local>%<nic> 22
sudo ip route add <pi-eth0-ip>/32 via <node-lan-ip>
nc -zv -w 3 <pi-eth0-ip> 5000
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

**C25. Real phone captive webviews. NOT YET RUN.** On an iPhone and an Android phone, in each
one's captive sheet and in its full browser.

From a laptop on the AP first:

```bash
dig +short @10.42.0.1 connectivitycheck.gstatic.com A
dig @10.42.0.1 connectivitycheck.gstatic.com AAAA | grep status
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'Host: captive.apple.com' http://10.42.0.1/hotspot-detect.html
```

Expect: `10.42.0.1`, `status: NOERROR` with no answer, and `302 http://10.42.0.1/`. These results
come from dnsmasq 2.92 here, so confirm them on Bookworm's version, under NetworkManager's real
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

Also confirm that the daemon runs as root, which `nmcli` and `tailscale` authorization depend on:

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
systemd-analyze critical-chain camerabridge.service
systemctl is-enabled NetworkManager-wait-online.service
mosquitto_sub -h 127.0.0.1 -t 'frigate/stats' -C 1 -W 90 | head -c 200
```

Expect: both `active` within seconds of `NetworkManager`, no `network-online` wait in the chain, and
`masked`. Frigate stats arrive with WiFi down, because detect and record continue.

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

Expect: `enabled`, then `yes` shortly after the uplink, with a `<site-ntp>` address as the server, and
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

Now show that the events are held. Restore the real server:

```bash
printf '[Time]\nNTP=<site-ntp>\n' | sudo tee /etc/systemd/timesyncd.conf.d/black-relay.conf
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

**C34. `detector_ok` idle values. NOT YET RUN.** With nothing moving in view for 10 minutes:

```bash
mosquitto_sub -h 127.0.0.1 -t 'frigate/stats' -C 1 | python3 -c "import json,sys; print(json.load(sys.stdin)['detectors'])"
mosquitto_sub -h <central> -u <user> -P <password> -t 'detection/camera/status/nodes' -C 1
```

Expect: each detector with a non-empty `pid` and `inference_speed` above 0, and `detector_ok=true`.
If the speed reads 0 when idle, `detector_ok` is falsely false, and the bridge needs a change.
Record the Frigate version, because the fields came from the `dev` branch source.

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

**C39. Moving a node to another tailnet. NOT YET RUN.** The daemon runs `tailscale up` without
`--force-reauth`, so a node already logged in may stay on its old tailnet, even though the login
reports success and the key is deleted. Hold the button at boot, and submit a key from a second
tailnet.

```bash
tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['CurrentTailnet'])"
journalctl -b -u camera-provision --no-pager -g 'tailscale'
```

Expect: record which tailnet the node is on. If it did not move, run `sudo tailscale logout` from a
console, then re-provision with a new key, and add that step to the re-provisioning procedure.

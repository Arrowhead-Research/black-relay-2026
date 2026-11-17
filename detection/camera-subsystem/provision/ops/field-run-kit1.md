# Field run: kit 1, camera link and relay

The ordered run for when the owner is back on the site network. It installs the current code on kit 1, then checks it,
riskiest and most basic first, so that a basic failure stops the run early instead of spending the camera's login
attempts or a day of waiting.

**Status of every check: NOT YET RUN.** Nothing in this file has been run on a Pi, a Hailo or the camera. A check
passes only on the output described, recorded in its blank. A failed check is a defect to report, not a check to
relabel, and it is not reworded to fit. Thresholds marked *proposed* are mine, not the owner's or the plan's: they are
there so that a result can be read, and the owner may change them.

**Who.** *Owner* does what needs hands: cables, the camera's pinhole, the phone, the console, walking in view, power.
*Agent* runs commands from the laptop over SSH and reads the results. Anything that changes who can reach the node
waits until both are ready.

**Stop rule.** A part ends at its first failed check unless the check says otherwise. Report it with the recorded
output. Part A must pass before the camera is plugged in for adoption, because each login the node tries uses up
one of the camera's 10.

## Numbers

C01 to C18, C20 to C38, C40, C41 and C43 to C48 are in [`README.md`](README.md). C19, C39 and C42 are retired. The
plan's Task 8 list used C45 to C53, which those collide with, so its checks have new numbers here:

| Plan Task 8 | Here | What |
|---|---|---|
| C45 | C57 | Factory camera adopted with no operator action |
| C46 | C61 | A camera with a set password gets one attempt, `needs_reset`, and re-adopts after a reset |
| C47 | C70 | Power pulled during adoption, 5 times |
| C48 | C71 | A second factory camera swapped in |
| C49 | C72 | No internet at first start |
| C50 | C73 | A person produces `camera.detection.v0` on the central broker |
| C51 | C74 | Stream, snapshot and who may reach the node |
| C53 | C76 | Heartbeat truth table |
| C52 | C78 | 24 h run |

The plan's C51 also asks for the Frigate UI through the central API. A later ruling took that out of v0, so C74 tests
the SSH tunnel that replaced it.

| Part | Checks |
|---|---|
| A. Install and get back in | C49 installer, C50 relay, C51 backup way in, C52 first reboot through the portal, C53 docker and systemd reach, C54 DHCP unit, C55 HailoRT lines, C56 re-provision with no button |
| B. Adoption and what it needs | C57 factory adoption, C58 `set_streams` and h264, C59 `frigate.service` and segments, C60 field of view, C61 one attempt, C62 failure counter, C63 session limits, C64 on demand, C65 15 fps, C66 WiFi, C67 16 KB, C68 browser WebRTC, C69 Pixel captive sheet |
| C. Faults and the long run | C70 power, C71 swap, C72 no internet, C73 detection, C74 video and access, C45 and C46 from the README, C75 damaged `central-hosts.nft`, C76 truth table, C77 measured costs, C78 24 h |

[Binding items](#where-each-binding-item-is-checked) are mapped to checks at the end.

## What the laptop tests did not show

`cameralink/tests/test_install_node.py` runs the installer on a fake root, and `tests/container_install_node.sh` runs
it on Debian Trixie amd64 in a privileged throwaway container with systemd as PID 1. These have never been exercised:
Raspberry Pi OS and its netplan and cloud-init, arm64 wheels, the Hailo driver and `/dev/hailo0`, the real Frigate
image and its digest, WiFi, a real reboot, boot-time ordering, an SSH session arriving through a physical `eth0`, the live
deletion of a profile holding `eth0`, and hardening under a host's root rather than a container's. So C49, C52 and
C53 are real tests of the installer, not formalities. An NM profile generated in memory is expected not to return
once the camera profile exists, from how NetworkManager is documented to work. C52 checks that.

## Before you start: laptop

*Agent, in the repository's `detection/camera-subsystem`.* Site values stay out of git: fill them in
`field-run.local.env`, which `.gitignore` covers and no `rsync` here copies.

```bash
cat > field-run.local.env <<'LOCAL'
SITE_WIFI='<site WiFi name>'
NODE_ID='<node id: cam- and the last 6 of the Pi serial>'
NODE='<node address. Its DHCP lease may move it: GET /nodes has node_address>'
SITE_SUBNET='<site LAN in CIDR form. The relay requires a private range>'
CENTRAL='<the laptop address read below>'
LOCAL
```

**The node's firewall admits one address, the broker's, so all of central's traffic must come from that address.**
MediaMTX and the API leave their containers through Docker's masquerade, which takes the route's source address. So
the broker address is **the laptop's real address, read now,** and it goes in the portal, `RELAY_CENTRAL_IP` and
everywhere else. Do not add a second address for this: traffic out still leaves from the primary, so SSH-style checks
would pass inbound while live view and the Frigate proxy are refused, and it would look like a node fault.

```bash
. ./field-run.local.env
ip -4 -br addr
ip -4 -o route get "$NODE"
```

Put the `src` address from the second command in `CENTRAL`. Stop if its interface shows more than one IPv4 address, and
remove the extra one.

**Reserve that address for the laptop in the site's DHCP, before C52.** If the lease moves after the portal save, the
node refuses central until it is provisioned again.

Load these in every shell. `central_check` says whether the laptop still has the address the node was given:

```bash
. ./field-run.local.env
# Each helper reads relay/.env in a subshell, and passes secrets by file or stdin, so none is in any argv.
api() { ( . relay/.env; printf 'header = "Authorization: Bearer %s"\n' "$RELAY_API_TOKEN" | curl -s -K - "$@" ); }
nodes() { api "http://$CENTRAL:8081/nodes" | python3 -m json.tool; }
central_sub() { ( . relay/.env; umask 077; dir=$(mktemp -d); trap 'rm -rf "$dir"' EXIT
  printf -- '-u %s\n-P %s\n' "$RELAY_BROKER_USER" "$RELAY_BROKER_PASSWORD" >"$dir/mosquitto_sub"
  XDG_CONFIG_HOME=$dir mosquitto_sub -h "$CENTRAL" "$@" ); }
# relay_read ffprobe <options>, or relay_read ffplay. The tool reads the URL from a file. This needs ffmpeg 5 or later.
relay_read() { ( . relay/.env; umask 077; list=$(mktemp); trap 'rm -f "$list"' EXIT
  printf "ffconcat version 1.0\nfile 'rtsp://%s:%s@%s:8554/%s'\noption rtsp_transport tcp\n" \
    "$RELAY_READ_USER" "$RELAY_READ_PASSWORD" "$CENTRAL" "$NODE_ID" >"$list"
  "$@" -f concat -safe 0 -protocol_whitelist file,rtsp,rtp,tcp,udp,crypto -i "$list" ); }
central_check() {
  local now; now=$(ip -4 -o route get "$NODE" | sed -n 's/.* src \([0-9.]*\).*/\1/p')
  if [ "$now" != "$CENTRAL" ]; then
    echo "CENTRAL CHANGED: the laptop now sends from ${now:-nothing}, and the node admits only $CENTRAL"; return 1
  fi
  if [ -e relay/.env ] && ! ( . relay/.env; [ "$RELAY_CENTRAL_IP" = "$CENTRAL" ] ); then
    echo "CENTRAL CHANGED: RELAY_CENTRAL_IP in relay/.env is not $CENTRAL"; return 1
  fi
  echo "central ok: $CENTRAL"
}
command -v curl mosquitto_sub ffprobe ffplay
```

**Run `central_check` at the start of each part, and first whenever SSH or video times out.** C52's pass also checks
what the node holds. If it reports a change, get the old address back, by its reservation, or if that cannot be done:
at a console or with C56's procedure, save the new address in the portal, set it in `CENTRAL` and `relay/.env`, and run
`docker compose up -d`. Do not read a refused connection as a node fault until `central_check` passes.

---

# Part A. Install and get back in

## C49. Copy the code and run the installer. PASS 2026-11-13.

*Agent.* The node is still on its old build, and the old firewall is live, so SSH works as before. First, the state
it starts from:

```bash
ssh kit-1@$NODE '
  sudo docker ps -a --format "{{.Names}} {{.Status}}"
  sudo docker inspect -f "{{.HostConfig.RestartPolicy.Name}}" frigate-spike
  sudo sha256sum /var/lib/frigate-spike/config/model_cache/yolov6n.hef
  modinfo -F version hailo_pci; ls -l /dev/hailo0; cat /etc/modprobe.d/hailo_pci.conf
  sudo cat /etc/black-relay/central-hosts.nft 2>&1
  sudo nft list tables
  sudo ls -la /etc/NetworkManager/system-connections/
  nmcli -f NAME,DEVICE connection show
  dpkg -s tailscale 2>&1 | head -2
  sudo sshd -T | grep -E "^(passwordauthentication|permitrootlogin) "
'
```

Pass: `frigate-spike` exited, with restart policy `no` (if not, `sudo docker update --restart=no frigate-spike`); the
hash is `6a20a3952732c7ea5f3f94a586f004015d3ae7d8f6ec5f7b2fcaa4126237124e`; driver `4.21.0`; `/dev/hailo0` present;
`black-relay-uplink` and `black-relay-setup-ap` profiles present. Note what `central-hosts.nft` holds, because the
installer keeps it. Copy the code. The literal backslash survives `rsync`:

```bash
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
  --exclude docs --exclude relay --exclude .env --exclude '.env.*' --exclude '*.local.env' \
  ./ kit-1@$NODE:camera-subsystem/
ssh kit-1@$NODE 'sudo rsync -a --delete --chown=root:root ~/camera-subsystem/ /opt/black-relay/src/ &&
  ls /opt/black-relay/src/cameralink/ops/'
```

Keep this session open, and run the installer in it. It needs the internet, for the Python packages:

```bash
ssh kit-1@$NODE
sudo /opt/black-relay/src/cameralink/ops/install-node.sh \
  --hef /var/lib/frigate-spike/config/model_cache/yolov6n.hef --purge-tailscale; echo "rc=$?"
```

Pass: `rc=0` and `READY`, or `rc=3` only for a line that can be fixed now, such as the `sshd` line, which step 7 of the
README fixes. The session is still up. Any other exit names the failed step. Run it once more, without flags:

```bash
sudo /opt/black-relay/src/cameralink/ops/install-node.sh; echo "rc=$?"
nmcli -f NAME connection show
ls '/etc/systemd/system/run-frigate\x2dcache.mount'
sudo sha256sum /var/lib/frigate/config/model_cache/yolov6n.hef
systemd-analyze cat-config systemd/journald.conf | grep -E '^(Storage|#.*conf)'
dpkg -s tailscale 2>&1 | head -1; ip link show tailscale0 2>&1 | head -1; sudo ls /var/lib/tailscale 2>&1
```

Pass: `Files changed: 0`; no `Wired connection 1`, and `black-relay-camera` listed; the file exists with its backslash;
the hash matches; the last `Storage=` is `persistent`, after any Raspberry Pi OS drop-in; Tailscale gone, as C48
describes. **Do not reboot, and leave `frigate-spike` in place until C55.** Also run C01 of the README now.

```
C49  date 2026-11-13  rc, first run 0  rc, second run 0  Files changed 0 (17 on the first run)
     output READY on both runs; hef hash matches; driver 4.21.0; Tailscale gone; journald persistent; no `Wired connection 1`
     notes: run at a different site from the first bring-up, with the uplink profile edited on the card. The laptop's
     Ethernet share on eth0 took the default route and blocked NTP (clock 8 days behind) until eth0 was disconnected.
     `sudo` prints "unable to resolve host" for the node name (no /etc/hosts line). `modinfo` needs /usr/sbin for kit-1.
     C01 rerun with the new firewall file: nft rc=0, verify rc=0, syntax check OK, network-pre.target.  result PASS
```

## C50. Relay bundle on the laptop. PASS 2026-11-13.

*Agent.* A wildcard bind fails on any port a host service already holds, on any address. Check what this laptop runs.
The commands below assume it holds some of these, so they use API port 8081 and bind the broker and NTP to the LAN
address, as `relay/README.md` describes. Touch no other container.

```bash
sudo ss -lntup | grep -E ':(123|1883|8080|8081|8554|8889|8189)\b'
cd relay
./setup-credentials.sh
sed -i -e "s|^RELAY_SITE_SUBNET=.*|RELAY_SITE_SUBNET='$SITE_SUBNET'|" \
  -e "s|^RELAY_CENTRAL_IP=.*|RELAY_CENTRAL_IP='$CENTRAL'|" \
  -e "s|^RELAY_WEBRTC_HOSTS=.*|RELAY_WEBRTC_HOSTS='$CENTRAL'|" \
  -e "s|^RELAY_BROKER_BIND_ADDRESS=.*|RELAY_BROKER_BIND_ADDRESS='$CENTRAL'|" \
  -e "s|^RELAY_NTP_BIND_ADDRESS=.*|RELAY_NTP_BIND_ADDRESS='$CENTRAL'|" \
  -e "s|^RELAY_API_PORT=.*|RELAY_API_PORT=8081|" .env
docker compose up -d --build
docker compose ps --format '{{.Service}} {{.Status}}'
cd ..
central_check
nodes
```

`setup-credentials.sh` refuses an existing `.env`. If there is one, read it, rather than using `--force`. `relay/.env`
is ignored by git and is never committed.

Pass: `api`, `netguard`, `mediamtx`, `chrony` and `mosquitto` all up, `central_check` passes, and `nodes` prints `broker.connected` true,
`paths.guard_ready` true, and an empty `nodes` list. The laptop is amd64, so the bundle's arm64 run stays open. It is
a separate check if a Pi becomes the central host.

```
C50  date 2026-11-13  output api, netguard, mediamtx, chrony, mosquitto up; central_check ok;
     broker.connected true, guard_ready true, nodes []. API on port 8082, because bettercap holds 127.0.0.1:8081
     on this laptop, so the helpers use 8082.  result PASS
```

## C51. A backup way back in. NOT YET RUN.

After the portal save, SSH admits **only** `$CENTRAL`, on `wlan0`. After the coming reboot, if the set is empty,
it admits nobody. `eth0` and the AP never admit SSH. So prove a way in that does not depend on that, first.

*Owner, with Agent reading out the commands.*

1. **Console, the main route.** Plug in a micro-HDMI screen and USB keyboard. At the `login:` prompt on tty1, log in
   as `kit-1`, and run `sudo whoami`. SSH is key only, so the account may have no password. In the SSH session from
   C49 run `sudo passwd -S kit-1`: `P` means it has one, and `L` or `NP` means set one now with `sudo passwd kit-1`.
   Prove it at the console before going on. That setting affects only the console and `sudo`, not SSH.
2. **The SD card, the second route.** With the node powered off, in a card reader on the laptop. The commands are
   below, and only need running if the console also fails. Confirm that a reader is to hand.
3. **The setup AP.** While the node has no WiFi profile, the AP's portal can save a corrected broker address, as C52
   and C56 use. It cannot help a node that has joined WiFi with a wrong broker, which needs route 1 or 2.

Recovery by card, if ever needed. Replace `<sdX>` after checking `lsblk`:

```bash
sudo mkdir -p /mnt/kit1 && sudo mount /dev/<sdX>2 /mnt/kit1
printf 'add element inet black_relay central_hosts { %s }\n' "$CENTRAL" \
  | sudo tee /mnt/kit1/etc/black-relay/central-hosts.nft
sudo umount /mnt/kit1
```

Pass: `kit-1` logged in at the console, `sudo whoami` printed `root`, and a card reader is present. **Stop here if the
console login fails.** The next check can leave a node nobody can reach without it.

```
C51  date ________  console login ___  sudo ___  card reader ___  result ________
```

## C52. First reboot, through the portal, with no button. PASS 2026-11-13, on a seeded clone of the image.

**The setup button is unwired,** so the README's "hold the button at boot" cannot be done. The daemon opens the full
portal on a boot with no saved config, and "no config" is defined as no WiFi profile or no hostname. So moving the WiFi
profile aside before the reboot opens it, with these properties:

- NetworkManager does not watch that directory. It keeps the profile until it is told to reload, or the node reboots,
  so **this does not cut the SSH session.** Do not run `nmcli connection reload` between the move and the reboot.
- With no config, the AP stays up past its 300 s window until a form is saved, so the node cannot wander off.
- Unlike the button, doing nothing does **not** rejoin by itself. The way out is a save, or the console step below.
- Do it only with the phone and the passphrase in hand.

The reboot also applies the firewall, and this order is why C51 came first. It also reaches SSH through the broker
address the form saves, so no part of this hand-applies the set, as the plan requires.

*Owner:* **unplug the camera from `eth0`** and leave it unplugged. The camera-link daemon starts at this reboot, and
must not spend a login before C53 and C54 pass.

*Agent, in the open session.* If the AP profile is missing, the block below stops and moves nothing. Then run
`sudo /opt/black-relay/ops/seed-kit.sh --kit 1` at the console, which prints the label. It also sets the console
password to the default and replaces the host keys, so afterwards run `ssh-keygen -R "$NODE"` on the laptop, and run
the block again.

```bash
# Read the last two out to the owner, once. They are the kit's label.
if sudo ls -l /etc/NetworkManager/system-connections/black-relay-setup-ap.nmconnection \
     /etc/NetworkManager/system-connections/black-relay-uplink.nmconnection &&
   sudo nmcli --get-values 802-11-wireless.ssid connection show black-relay-setup-ap &&
   sudo nmcli --show-secrets --get-values 802-11-wireless-security.psk connection show black-relay-setup-ap; then
  sudo install -d -m 0700 /root/kit1-uplink-saved
  sudo mv /etc/NetworkManager/system-connections/black-relay-uplink.nmconnection /root/kit1-uplink-saved/
  sudo reboot
else
  echo 'STOP: a profile is missing or unreadable. Nothing was moved, and the node was not rebooted.'
fi
```

*Owner, within a minute or two of the reboot:* the screen shows the login prompt, and the SSID appears on the phone.
Join it with the passphrase, open `http://10.42.0.1/`, and fill in the form. Check every field, as old values may show.

| Field | Value |
|---|---|
| WiFi | `$SITE_WIFI` and its password |
| Node ID, hostname | `$NODE_ID` |
| Covered zones | The site's zone name, at least two letters |
| Frigate address | `https://$NODE_ID.local:8971` |
| Broker address, port | `$CENTRAL`, `1883`, TLS off, anonymous off. **Read it out with `central_check` first** |
| Broker username, password | `RELAY_BROKER_USER`, `RELAY_BROKER_PASSWORD` from `relay/.env` |
| Time server | Blank |
| Upside down | As mounted |

*Agent reads out the password, with:* `grep -E '^RELAY_BROKER_(USER|PASSWORD)=' relay/.env`

If no SSID appears within 3 minutes, at the console:

```bash
sudo systemctl status camera-provision nftables --no-pager | head -30
rfkill list wifi
# To go back to joining WiFi. The firewall then admits whatever central-hosts.nft holds, so add the laptop if needed.
sudo mv /root/kit1-uplink-saved/black-relay-uplink.nmconnection /etc/NetworkManager/system-connections/
sudo chmod 600 /etc/NetworkManager/system-connections/black-relay-uplink.nmconnection
sudo reboot
```

*Agent, once the phone shows the saved page, from the laptop:*

```bash
central_check
nodes                            # Within 90 s or so. node_address gives the new $NODE if it moved.
ssh kit-1@$NODE '
  echo "the node sees this SSH from: ${SSH_CONNECTION%% *}"
  sudo cat /var/lib/camera-provision/state.json; echo
  journalctl -b -u camera-provision --no-pager -g " -> |portal|firewall"
  cat /etc/black-relay/central-hosts.nft; sudo nft list set inet black_relay central_hosts
  sudo grep -E "^ *host:" /etc/camerabridge/bridge.yaml
  nmcli -f NAME,DEVICE connection show; ip -4 -br addr
  ip link show docker0 2>&1 | head -1
  systemctl is-active nftables docker mosquitto camera-provision camerabridge camera-link
  systemctl is-enabled frigate NetworkManager-wait-online
  timedatectl show -p NTPSynchronized --value; timedatectl timesync-status | head -3
  journalctl --list-boots --no-pager | tail -2; journalctl --disk-usage
  sudo python3 -c "import json; print(sorted(json.load(open(\"/var/lib/camera-provision/node.json\"))))"
'
```

Pass, all of these:

- `ap_up + config_received -> joining`, `joining + uplink_up -> provisioned`, and no `reloading the firewall failed`.
  That also shows the firewall reload works from inside the provision unit's hardening.
- `central_check` passes. The set and the file both hold `$CENTRAL`, the node sees the SSH session from `$CENTRAL`, and
  `bridge.yaml`'s central host is `$CENTRAL`.
- `black-relay-uplink` on `wlan0`, and **no `Wired connection 1`**. `eth0` has no carrier, and no default route uses it.
- No `docker0`, which shows `daemon.json` applied. The services are `active`, `frigate` is `static`, wait-online is
  `masked`, NTP is `yes` with the laptop as server, and the journal is on disk. `vpn_requested` is not in `node.json`.
- `nodes` has `$NODE_ID`: `stale` false, `link_online` true, `camera_online` false (no camera yet), `node_address` set,
  `attributes.camera_link_state` `no_camera`. This also closes C08's open item, a form save reaching a live broker. On the node, no
  `SECCOMP` or `Operation not permitted` in `journalctl -b -u camerabridge`, as the address comes from an ioctl under
  the bridge's system call filter.

**If SSH times out:** run `central_check`. Either the save has the wrong address, or the laptop has moved off it. Use
C51, and stop.

```
C52  date 2026-11-13  AP seen after n/a  saved, joined after n/a  heartbeat after under 60 s
     output The portal was NOT used, so this check stays open. The node was power-cycled before C51, which applied the
     firewall with an empty set and locked SSH out. Recovery was by SD card (C51's second route): the laptop's address
     went into central-hosts.nft. The broker was then saved over SSH through NodeStore.save, the portal's own code
     path, followed by a reboot. After it: state provisioned; the set, the file and bridge.yaml hold the laptop;
     SSH seen from the laptop; uplink on wlan0, no `Wired connection 1`, no docker0; services active, frigate static,
     wait-online masked; NTP yes from the laptop; journal on disk; node in `nodes` with camera_link_state no_camera.
     Still to show: the portal form itself, and `ap_up + config_received -> joining`.  result OPEN
     Later the same day, after the image was captured: kit 1's own card, scrubbed by step 10 and given its kit
     number, seeded itself on its first boot with no screen or keyboard, and its setup WiFi came up. The owner joined
     it from a phone with the default passphrase and saved the form. The journal: `unprovisioned + boot -> ap_up`,
     `ap_up + config_received -> joining`, `joining + uplink_up -> provisioned`, and no firewall failure. After the
     save: SSH seen from central, the set and bridge.yaml hold central, NTP yes, heartbeat fresh with
     camera_link_state no_camera. The times were not taken.  result PASS
```

## C53. Docker and systemd reach, under the hardening, as real root. PASS 2026-11-13.

*Agent.* If this fails, `detector_ok` stays false for good, with a reason, and Frigate may never start. It has been
shown only under a container's root. The camera stays out. The script takes the sandbox from the installed units,
so it tests what is installed.

```bash
ssh kit-1@$NODE 'sudo /opt/black-relay/src/cameralink/ops/reach-check.sh; echo "failures=$?"
  cat /run/camera-link/state.json; echo
  journalctl -b -u camera-link --no-pager | tail -15
  systemctl show -p NRestarts --value camera-link camera-provision camerabridge
  sudo journalctl -b -k --no-pager -g "SECCOMP|apparmor=.DENIED" | tail -5'
```

Pass: `failures=0`; each line starts `ok`; the state file shows `no_camera` and a recent `updated`; no `systemctl` or
`SystemdError` failures in the journal; no denials; each `NRestarts` is `0` and stays there. `camerabridge` alone may
show restarts from before the save, if it could not use the old `bridge.yaml`: record the number, and it must not rise. Nothing here has a real Frigate container
to read yet: C59 shows `log_read_at` advancing on the running daemon.

```
C53  date 2026-11-13  failures 0  output six `ok` lines; state no_camera; NRestarts 0 0 0; no SECCOMP or
     AppArmor denials  result PASS
```

## C54. The DHCP unit, under its hardening, as real root. PARTLY RUN 2026-11-13, WITH A DEFECT.

*Owner:* plug a **laptop's USB Ethernet dongle** into `eth0`, not the camera. A lease costs the camera nothing if this
fails, but this way no adoption starts at all. Any device that asks for DHCP will do. If there is none, use the
camera after resetting it, and go straight on to C57, since adoption starts by itself.

*Agent:* the unit has `--dhcp-option=3 --dhcp-option=6`, so the dongle gets no gateway or DNS, and the laptop's route
out stays as it was.

```bash
ssh kit-1@$NODE '
  ip -4 -br addr show eth0
  systemctl show -p ActiveState,SubState,NRestarts,MainPID camera-link-dhcp
  pid=$(systemctl show -p MainPID --value camera-link-dhcp)
  ps -o user=,args= -p "$pid"; grep -E "^Cap(Eff|Bnd)" /proc/$pid/status
  sudo ss -lunp "sport = :67"
  sudo cat /var/lib/camera-link/dnsmasq.leases; sudo ls -la /var/lib/camera-link
  journalctl -b -u camera-link-dhcp --no-pager -o short-precise | tail -15'
```

Then, on rising carrier, whether the restart waits on the backoff. *Owner* unplugs the cable, waits 30 s, and plugs it
in, noting the time by the clock on `date`. *Agent:* run `journalctl -u camera-link-dhcp -o short-precise -f` first.
Renew once from the dongle's host (`sudo nmcli device reapply <nic>`), and read the lease file's time again.

Pass: `eth0` is `10.77.0.1/24`; the process runs as a non-root user (`nobody` in the container test) with only
`cap_net_admin` and `cap_net_raw` left in `CapEff`, which reads `0000000000003000`; it listens on `eth0` for port 67;
the dongle gets `10.77.0.10` with no route and no DNS; the lease file updates on renewal, even though the directory
is 0700 root; no `Permission denied` or `SECCOMP`. dnsmasq is serving within about 10 s of carrier rising, not after
the 60 s backoff. *Proposed:* 10 s. Record the real number either way.

**Unplug the dongle when done.** `cameras.json` may gain a record of its MAC: harmless, and step 10 of the README
removes it.

```
C54  date 2026-11-13  user not read  CapEff not read  lease 10.77.0.10 to the camera  carrier to serving not timed
     output No dongle was to hand, so the camera was used. The unit serves, and gave 10.77.0.10 with the link up.
     DEFECT: the daemon wipes a lease the camera has just been given. dnsmasq is already running when the carrier
     rises, the camera asks within seconds, and the next tick (5 s) then resets DHCP and deletes the lease file.
     Seen three times: ACK at :49.42 and reset at :51.49; ACK at :51.37 and reset at :51.69; OFFER at :04.99 and
     reset in the same second, where the camera's retry then won. After a lost race the camera holds the address
     and does not ask again until it renews, at 30 min, so the node reports no_camera for that long.
     With no link the unit exits 2 with "unknown interface eth0" and restarts on its backoff, as expected.
     Process user, CapEff and the renewal are still to read.  result FAIL (lease wiped after the grant)
     FIXED the same day, in cameralink/daemon.py: before a wipe the daemon dials the lease's address and reads the
     kernel's ARP table, and keeps the lease of a camera that answers with the lease's MAC. Rerun on kit 1 under
     the unit's sandbox, with the link taken down and up from the node: ACK at :20.78, and the tick at :22.74 logged
     "answers at 10.77.0.10, so its lease is kept"; adopting 2 s later and adopted after 51 s, with no restart of
     dnsmasq. A factory camera's first plug-in is still to rerun.
```

## C55. Real HailoRT lines at clean start and stop. PASS WITH A NOTE 2026-11-13.

A line matching camera-link's pattern at a clean start or stop **restarts Frigate every 10 minutes** on a healthy node,
and the pattern was written from Frigate's source, not its real output. So read the real thing, before adoption, with
the spike container that is already there. It has the same image, driver and model, and needs no camera to bring up
its detector. Its old camera address will not answer: that is fine, and its errors are about the camera, not Hailo.
If `docker start` refuses because Docker has no bridge now, which `daemon.json` removed, the spike was not on host
networking: record that, and take these readings in C59 instead, once adoption has passed.

*Agent:*

```bash
ssh kit-1@$NODE '
  PATTERN="\[HailoRT\] \[error\]|HailoRTException|Inference error:|\[INIT\] Failed to initialize HailoAsyncInference|Timeout waiting for inference results|HailoRT inference thread has stopped|Detection appears to be stuck"
  sudo docker start frigate-spike; sleep 120
  sudo docker logs frigate-spike 2>&1 | grep -iE "hailo" | tail -30
  echo "--- matches after start:"; sudo docker logs --since 3m frigate-spike 2>&1 | grep -cE "$PATTERN"
  sudo docker stop -t 30 frigate-spike; sleep 2
  echo "--- matches after stop:"; sudo docker logs --since 3m frigate-spike 2>&1 | grep -E "$PATTERN"
  sudo docker logs --since 3m frigate-spike 2>&1 | grep -iE "hailo" | tail -30
  sudo docker inspect -f "{{.State.Status}}" frigate-spike; lsmod | grep hailo'
```

Save every line with `hailo` in it, whichever way this goes: they should replace the test fixtures, which were built
from Frigate's source. Pass: `0` matches after start, and none after stop, and the Hailo init lines look healthy. A
match at a clean start or stop **stops the run**: the pattern or Frigate needs changing, and it must be fixed first.
C59 repeats this with the real unit.

```
C55  date 2026-11-13  matches at start 0  at stop 0
     hailo lines none at all: Frigate 0.18.2 printed no line with `hailo` in it in 120 s of a clean run or at stop,
     and no line naming a detection process, so nothing here shows the detector came up. Frigate itself started
     cleanly. C59 must show the detector on the real unit.  result PASS (no false match), detector unproven
```

## C56. Re-provision with no button, on a provisioned node. NOT YET RUN.

C52 did this as part of the first boot. This proves the path as an operator would use it, and the AP's dnsmasq beside
the camera link's. *Owner:* plug the dongle back into `eth0`, and have the phone ready. *Agent:*

```bash
ssh kit-1@$NODE 'sudo install -d -m 0700 /root/kit1-uplink-saved
  sudo mv /etc/NetworkManager/system-connections/black-relay-uplink.nmconnection /root/kit1-uplink-saved/
  sudo reboot'
```

*Owner:* with the AP up, at the console:

```bash
pgrep -a dnsmasq
sudo iw dev wlan0 info | grep type
journalctl -b -u camera-provision --no-pager -g ' -> |portal'
sudo cat /var/lib/camera-link/dnsmasq.leases
```

Still on the AP, the phone gets an address in `10.42.0.0/24`, and the dongle one on `10.77.0.0/24`: both directions
at once, with `--dhcp-authoritative` on one. Fill in the form as in C52, submit, and check as C52 does.

Pass: two `dnsmasq` processes, one with `--interface=eth0` and one from NetworkManager for `wlan0`; `type AP`;
`unprovisioned + boot -> ap_up`; the phone and the dongle each get the right
range; after the save, `central_check` passes, SSH works, and the heartbeat is back. Nothing was edited by hand after the save.
Once `nodes` is healthy, run `sudo rm -rf /root/kit1-uplink-saved`. **Unplug the dongle.**

**Physical button, for the record.** The button is a short between header pins 11 and 9. A jumper wire does the same
when held through power-on, but a slip onto a 5 V pin can kill the board, so this run does not use it. C06 and C16
stay open until the button is wired.

```
C56  date ________  output ___________________________________________________  result ________
```

**End of part A. Report it before going on. If anything above failed, do not plug in the camera.**

---

# Part B. Adoption, from a factory reset

All of these are on kit 1 with the camera, unless they say otherwise. One helper reads the camera's own settings with
the password the node holds, without printing it. A read logs in with the right password, so it uses no attempts:

```bash
# On the node.
cam_read() { sudo /opt/black-relay/cameralink/bin/python -c '
import json, sys
from pathlib import Path
from cameralink.camera_api import ReolinkClient
from cameralink.store import CameraStore
record = CameraStore(Path("/var/lib/camera-link/cameras.json")).load()
client = ReolinkClient("10.77.0.10")
client.login("admin", record.password)
try:
    for name in sys.argv[1:]:
        param = {"channel": 0} if name in ("Enc", "Isp") else {}
        print(name, json.dumps(client._read("Get" + name, name, param), sort_keys=True))
finally:
    client.logout()
' "$@"; }

# On the node. Runs a command in the Frigate container with $input set to read one camera stream. The URL is in a
# root-only file there, and the password expands only inside that shell's builtins, so it is in no argv.
cam_stream() { sudo docker exec -e STREAM="$1" -e ACTION="$2" frigate sh -c '
  umask 077; list=$(mktemp); trap "rm -f $list" EXIT
  { echo "ffconcat version 1.0"
    echo "file '"'"'rtsp://admin:$FRIGATE_CAMERA_PASSWORD@10.77.0.10:554/h264Preview_01_$STREAM'"'"'"
    echo "option rtsp_transport tcp"; } >"$list"
  input="-f concat -safe 0 -protocol_whitelist file,rtsp,rtp,tcp,udp,crypto -i $list"
  eval "$ACTION"'; }
```

## C57. A factory camera is adopted with no operator action, with timings. PASS 2026-11-13, on the third run, after three fixes.

Plan C45. Also: real `SetNetPort`, `disable_onvif`, `set_clock`, login right after `ModifyUser`, and the 45 s settle.

*Agent:* start two logs on the node, in `tmux`, or `nohup` with a file. Nothing in them holds a password.

```bash
sudo journalctl -u camera-link -u camera-link-dhcp -u frigate -o short-precise -f | tee /tmp/adopt.log
```

```bash
# Prints each change in which camera ports answer, with the time.
last=; while :; do now=
  for p in 9000 80 443 554 8000; do timeout 1 bash -c "</dev/tcp/10.77.0.10/$p" 2>/dev/null && now="$now $p"; done
  if [ "$now" != "$last" ]; then echo "$(date +%T.%N | cut -c1-12) open:$now" | tee -a /tmp/ports.log; last=$now; fi
  sleep 0.5; done
```

*Owner:* the camera was reset and then had HTTP and RTSP turned on by hand, which is not a factory state. **With the
camera powered, press the pinhole reset, and hold until it resets,** wait for it to come up, and note the time. Plug it
into `eth0`, noting the time. Touch nothing else.

*Agent, once `state.json` reads `adopted`:*

```bash
cat /run/camera-link/state.json; echo
# The record's repr leaves out both passwords. Do not cat this file: it holds them.
sudo /opt/black-relay/cameralink/bin/python -c 'from pathlib import Path; from cameralink.store import CameraStore; print(CameraStore(Path("/var/lib/camera-link/cameras.json")).load())'
systemctl is-active frigate; sudo docker ps --format '{{.Names}} {{.Status}}'
cam_read NetPort P2p Upnp Ntp Time Enc
sudo stat -c '%a %U %n' /var/lib/camera-link/cameras.json /etc/camerabridge/frigate.env /var/lib/frigate/config/config.yml
```

Pass: from the plug-in, no operator action; the state goes `no_camera`, `adopting`, then `adopted`, not
`adopted_pending_settings`, and `reason` is null; before adoption only `9000` is open, which is the factory signature;
Frigate is running. In `NetPort`: RTSP on, ONVIF off. P2P and UPnP off. `Ntp.enable` is 0, and `Time` matches
the node's UTC, which is what a node without an RTC set by NTP gives it. The files are 0600 or as listed. If
`adopted_pending_settings` or `failed` shows, record the journal: the settings it names have not reached the camera.

Fill in the timings. They are the `Tracker for Task 8 bench` items, and later code may depend on them:

| Timing | Seconds |
|---|---|
| Cable in to lease | |
| First port open after lease | |
| Lease to first login attempt (should be at least the 45 s settle) | |
| `enable_services` start to port 80 open | |
| Login right after `ModifyUser`: did it work at first try? | |
| Cable in to `adopted` | |
| `adopted` to Frigate's first segment | |

```
C57  date 2026-11-13  ports before 9000 only, after the pinhole reset  final state adopted, reason null
     output Not unattended. (1) C54's defect left the node at no_camera for 5 min, until the wiped lease line was
     written back by hand. (2) The first pass then stopped: `password changed` at +51 s, `adoption stopped: ApiError`
     0.9 s later, state failed. The camera rebooted by itself 17 s after that, and the second pass, on the new link
     generation, reached adopted 46 s after its lease. So the login right after ModifyUser did not work at first try,
     and the log names no call. (3) The camera's overlay clock read 8 h ahead of UTC after adoption: Time is still
     to read with cam_read. After adoption: Frigate healthy, camera_fps 15.0, Hailo inference 8.25 ms, segments
     written, and camera_online, recording_ok and detector_ok all true at central 64 s after `adopted`.
     Live view through central MediaMTX: h264, 2304x1296, 25 fps. Timings table not filled: no clock on the cable.
     The node's WiFi address also moved during this check (a new DHCP lease after a reassociation), and central
     followed it from the heartbeat.  result FAIL
     Since then: (3) is explained and fixed. A SetTime that also moves the zone from the factory 28800 lands 8 h
     out, and the same write again reads right (shown on the camera), so set_clock now reads the clock back and
     writes once more if it is out. (2) is not explained: SetNetPort in either direction, on the adopted camera,
     was accepted, the login after it worked, and the camera did not reboot. The log now names the refused call
     and its code, so the next factory adoption will say which it is.
     Second run, after the lease and clock fixes: unattended, adopted 2 min 32 s after the pinhole reset, but
     the first pass again went failed, and the log named it: `GetIsp failed, rspCode -6`. By hand, twice: a SetEnc
     that changes the mode is accepted, the next call gets -6, RTSP and port 9000 close within a second, and the
     camera restarts about 15 s later and is back at about 34 s. Fix: set_streams is now the last step.
     Third run, with that fix: no_camera, adopting (+22 s), `password changed` (+70 s), adopted (+73 s), with no
     failed and no warning; then the camera's own restart: no_camera (+88 s), "its lease is kept", adopting,
     adopted (+149 s). Ports before adoption: 9000 only. After it: camera clock within 1 s of the node's UTC, zone 0;
     RTSP on, ONVIF off, P2P, UPnP and NTP off; main 2304*1296 at 25, sub 640*480 at 15; NRestarts 0 0 0; Frigate
     healthy at 15 fps; camera_online, recording_ok and detector_ok true at central 38 s after the last `adopted`.
     Not read: the file modes, and the timings table, which needs a clock on the cable.  result PASS
```

## C58. `set_streams` on the real camera, and h264 kept after the mode change. NOT YET RUN.

*Agent:* SetEnc has only been seen accepted at the mode the camera was already in, so this is new. The camera may have
restarted its encoder.

```bash
cam_read Enc
for s in main sub; do cam_stream "$s" 'ffprobe -v error $input -select_streams v:0 \
  -show_entries stream=codec_name,width,height,avg_frame_rate -of csv=p=0'; done
journalctl -u camera-link --no-pager -g 'failed|error' | tail
```

Pass: `Enc` shows main `2304*1296`, 25 fps, 6144 kbps, and sub `640*480`, 15 fps, 512 kbps, each with `vType` `h264`.
`ffprobe` says `h264,2304,1296,25/1` and `h264,640,480,15/1`. If the main stream's codec changes to `hevc`, the
`h264Preview_01_main` path is wrong: stop and raise it. Also run this after a reboot, to show that the settings stay.

```
C58  date ________  main ________  sub ________  after a reboot ________  result ________
```

## C59. `frigate.service` on hardware, segments, and HailoRT lines from the real unit. NOT YET RUN.

*Agent.* Includes the README's C47 setup, and needs C47's numbered steps run too. The bind is a 0700 tmpfs.

```bash
systemctl status frigate --no-pager | head -8
findmnt /run/frigate-cache; sudo stat -c '%a %U' /run/frigate-cache
sudo docker inspect -f '{{.HostConfig.NetworkMode}} {{.HostConfig.Privileged}} {{.HostConfig.Devices}}' frigate
sudo ls -la --time-style=full-iso /run/frigate-cache | tail -5; date
python3 -c "import json; print(json.load(open('/run/camera-link/state.json')))"
sudo systemctl restart frigate; sleep 180
sudo docker logs --since 4m frigate 2>&1 | grep -iE 'hailo' | tail -30
sudo docker logs --since 4m frigate 2>&1 | grep -iE 'fps|warn' | head -20
```

To see the stop's lines, which a `--rm` container takes with it, follow the log first in a second session:
`sudo docker logs -f frigate 2>&1 | grep -iE hailo`, and then run `sudo systemctl stop frigate` and
`sudo systemctl start frigate`.

Pass: `active`; a 256 MB tmpfs, `700 root`; `host false`, with `/dev/hailo0` only; a `<cam>@<time>.mp4` whose time
keeps moving, and **the in-use segment's mtime is under 60 s behind the clock whenever it is sampled**, 10 samples
over 10 minutes with nothing moving; `record_writing` true; no pattern line from C55 at start or stop. After 15 quiet
minutes, `NRestarts` for `frigate` is unchanged, `detector_error` is false, and `log_read_at` keeps moving, which is
camera-link really reading docker. Record Frigate's warning about detect above 10 fps, if any. A warning is not a
failure, as C65 decides that.

Then run C47's numbered steps. Also time the stop above: if it takes near 45 s, the
[F7 note](README.md#firewall-policy) applies, so run `sudo docker ps`, and record it.

```
C59  date ________  segment age samples (s) _______________________  restarts in 15 min ___
     warnings ___________________________________________________________  result ________
```

## C60. Field of view, 16:9 main against 4:3 sub. NOT YET RUN.

Zones and boxes are drawn on detect frames and shown over recordings. *Agent:* one frame of each, with nothing moving:

```bash
for s in main sub; do cam_stream "$s" "ffmpeg -y -v error \$input -frames:v 1 /tmp/fov-$s.jpg"
  sudo docker cp frigate:/tmp/fov-$s.jpg /tmp/fov-$s.jpg; done
sudo chmod 644 /tmp/fov-main.jpg /tmp/fov-sub.jpg
```

*Owner:* `scp` both to the laptop, and put a person or marker at each frame edge. Compare. Pass is a *decision*, not a
number: record whether the sub crops, squeezes or matches, with the images kept. A crop or a squeeze means boxes
are offset over recordings, and the owner decides whether that matters.

```
C60  date ________  sub is: same / cropped / squeezed  ______________________  owner's call ________
```

## C61. One attempt, `needs_reset`, and re-adoption after a reset. NOT YET RUN.

Plan C46. *Agent:* make the camera one the node cannot log in to: it keeps the password it was given, and the node
forgets it. No guess goes to the camera but the one blank login under test.

```bash
sudo systemctl stop camera-link
sudo install -d -m 0700 /root/kit1-camera-saved
sudo mv /var/lib/camera-link/cameras.json /root/kit1-camera-saved/
sudo systemctl start camera-link
journalctl -u camera-link -o short-precise -f | tee /tmp/one-attempt.log
```

Wait 10 minutes, including the 45 s settle. Then:

```bash
grep -cE 'login rejected|login outcome unknown' /tmp/one-attempt.log
grep -E 'login rejected|locked out|login outcome' /tmp/one-attempt.log
cat /run/camera-link/state.json; echo
systemctl is-active frigate
```

Pass: **exactly 1** attempt in 10 minutes, with `login rejected, N tries left`, where the camera stated `N` (record it,
for C62); state `needs_reset` with a reason saying a factory reset is needed, and Frigate stopped, as its password is
in doubt. Or state `failed` if the camera's wording is unknown, and then Frigate stays up, which two rulings allow. The
heartbeat shows the same. **Run C62's first half
now, before the reset.** Then *Owner* presses the pinhole, and touches nothing else.

Pass, after the reset: without any command, the node sees the factory signature, adopts, starts Frigate, and
`nodes` is healthy. Record how long this took. Then `sudo rm -rf /root/kit1-camera-saved`, as those passwords are dead.

```
C61  date ________  attempts in 10 min ___  tries left ___  state ________  reset to adopted ___ s
     output ______________________________________________________________  result ________
```

## C62. The camera's failure counter. NOT YET RUN.

Whether the count of remaining logins ever goes back up is unknown. If it never does, the worst case is 10 failures by
day 2, and a lockout. The node stops by itself at 3 left, at 3 failures per 24 h, and at 1 per 24 h on the factory
signature, and `camera-link clear-limits` clears the last two. *Budget: 4 wrong logins before the reset, and 1 after.*
Stop at once if `tries left` reaches 3 or `locked out` appears, and reset the camera.

*Run this inside C61, before the reset.* *Owner* flaps the cable, which starts a new link generation:

| When | Expect | `tries left`, from `journalctl -u camera-link -g 'tries left'` | `retry_after` in state.json |
|---|---|---|---|
| T0, C61's attempt | One attempt | | |
| T0 + 1 h, a flap | One attempt. Has the count fallen by one, or gone back up? | | |
| Soon after, a flap | One attempt, the third, and `retry_after` set | | |
| Soon after, a flap | **No attempt:** the limit holds, and the count stays | | |
| `sudo /opt/black-relay/cameralink/bin/camera-link clear-limits`, then a flap | It waits up to 130 s and prints `Cleared the login limits`. One attempt | | |

Then the C61 reset. With the right password the camera does not report the count, so one more reading needs one more
wrong login, in **C78's last hour**: move the record aside as C61 does, flap the cable, and record `tries left`. Then
reset the camera again, and it adopts.

Pass is a *recorded answer* to two questions: does the count fall by one per failure, and does a day or a reset restore
it? If neither restores it, raise it: a kit could lock its camera for good.

```
C62  date ________  T0 ___  +1 h ___  third ___  after clear-limits ___  after reset, +24 h ___
     answer ______________________________________________________________
```

## C63. Camera session limits, and a leak on the first dial. NOT YET RUN.

*Agent:* watch the node's sessions to the camera, which all use TCP.

```bash
sudo ss -tn state established 'dst 10.77.0.10' | tail -n +2 | wc -l
sudo ss -tn state established 'dst 10.77.0.10 and dport = :554'
sudo ss -un 'dst 10.77.0.10'
```

Idle first. Then one reader, from the laptop, using the login from `relay/.env`, then two at once:

```bash
relay_read ffplay
```

While one reader is on, on the node. This is what the firewall sees, so it must show `$CENTRAL`:

```bash
sudo ss -tn state established 'sport = :8554'
```

Then 10 rounds of a reader starting and stopping, watching for a count that does not come back down: if the camera
refuses go2rtc's backchannel DESCRIBE, each first dial leaks one socket. The fix, only if this shows it, is
`#backchannel=0` on the template's source, and it goes in the tracker for a decision.

Pass: the pull comes from `$CENTRAL`; 2 sessions when idle, 3 while viewing, and still 3 with two viewers; video plays in each; back to 2 within about
10 s of the last reader leaving; no UDP; no growth across 10 rounds; the camera refuses none of it.

```
C63  date ________  idle ___  one viewer ___  two viewers ___  after 10 rounds ___  result ________
```

## C64. On-demand behaviour, and recovery. NOT YET RUN.

*Agent:*

```bash
# Time to first frame, 5 times, from the laptop. Each time, first wait for the idle count from C63.
time relay_read ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width -of csv=p=0
nodes | grep -A6 '"paths"'
```

Then the case left open by go2rtc dropping an error: *Owner* unplugs the camera. Restart Frigate, wait 2 minutes, and
start a reader (it should fail cleanly). *Owner* plugs it back in. Without restarting anything, start another reader.

Pass: an idle node sends no video, so with no reader the node's `wlan0` traffic falls by the stream's rate, seen in
`ip -s link show wlan0` across a minute; a reader gets `h264,2304`; MediaMTX drops the node 10 s after the last reader;
after the camera returns, a reader gets video with no restart of anything. Record each time to first frame.
*Proposed:* under 5 s, the owner's call to change.

```
C64  date ________  first frame (s) ___ ___ ___ ___ ___  recovery ________  result ________
```

## C65. CPU and tracking at 15 fps detect. NOT YET RUN.

*Agent*, with the owner walking through the view a few times:

```bash
mosquitto_sub -h 127.0.0.1 -t frigate/stats -C 3 | python3 -c "
import json, sys
for line in sys.stdin:
    s = json.loads(line)
    print({k: v for c in s['cameras'].values() for k, v in c.items() if k in ('camera_fps','detection_fps','process_fps','skipped_fps')}, s['detectors'], s.get('cpu_usages', {}).get('frigate.full_system'))"
top -bn1 | head -15
vcgencmd measure_temp; vcgencmd get_throttled
# This endpoint is from memory of Frigate's API, as in C43.
curl -s 'http://127.0.0.1:5000/api/events?limit=10' | python3 -c "import json,sys; [print(e['id'], e['label'], e['start_time'], e['end_time']) for e in json.load(sys.stdin)]"
```

Pass: `camera_fps` and `process_fps` near 15, `skipped_fps` near 0, `inference_speed` steady and near the spike's
7.65 ms, `throttled=0x0`, and a single walk gives one track, not several. *Proposed:* CPU under 85% and under 80 C.
Frigate warns above 10 fps by design: if it runs well, the warning stays and is recorded, and if not, the owner decides
between 10 and 15.

```
C65  date ________  fps ___  skipped ___  inference ___ ms  CPU ___ %  temp ___  tracks per walk ___  result ________
```

## C66. WiFi at 4 x 6 Mbps. NOT YET RUN.

Four kits are planned. Only kit 1 is here, so this measures whether one radio's path can carry the total, which is a
limit on what it shows: it does not make four radios contend. *Agent:* install `iperf3` only for this, and purge it.

```bash
# On the laptop:
iperf3 -s -1
# On the node:
sudo apt-get install -y iperf3
iw dev wlan0 link | grep -E 'signal|tx bitrate'
iperf3 -c <central> -t 60 -b 24M
iperf3 -c <central> -t 60 -u -b 24M
sudo apt-get purge -y iperf3
```

Then one real stream: with one reader on, sample `cat /sys/class/net/wlan0/statistics/tx_bytes` twice, 60 s apart.

Pass: at least 24 Mbps sustained for 60 s; under 1% UDP loss (*proposed*); the real stream is near 6 Mbps. Record the
signal and the place, because the result belongs to them. Heartbeats keep arriving through it. A shortfall means the
owner's on-demand choice limits how many can be watched at once.

```
C66  date ________  signal ___  TCP ___ Mbps  UDP loss ___ %  one stream ___ Mbps  result ________
```

## C67. The relay's 16 KB limit against real events. NOT YET RUN.

*Agent*, while the owner walks through the view with several zones in use. The node's login can read `detection/`:

```bash
central_sub -t 'detection/camera/#' -W 600 -F '%l %t' | tee /tmp/sizes.log
sort -n /tmp/sizes.log | tail -3
nodes | grep -A8 '"broker"'
```

Pass: the largest message is under 16,384 bytes, with a margin that the owner is happy with, and `broker.dropped` has
no refused message from this run.

```
C67  date ________  largest ___ bytes, on topic ________  dropped ___  result ________
```

## C68. Browser WebRTC. NOT YET RUN.

*Owner*, in a browser on the laptop, or any device on the LAN: open `http://<central>:8889/<node-id>`, with the values from `field-run.local.env`, and log in
with `RELAY_READ_USER` and `RELAY_READ_PASSWORD`. Agent can read `chrome://webrtc-internals` bytes received.

Pass: moving video with no plugin, audio not needed, `bytesReceived` rising on the UDP candidate, time to first frame
recorded, and the node's sessions as in C63. Repeat over Tailscale if the owner uses it from outside: add the laptop's
Tailscale address to `RELAY_WEBRTC_HOSTS`, and run `docker compose up -d`, or media will not flow.

```
C68  date ________  browser ________  first frame ___ s  over Tailscale ___  result ________
```

## C69. Pixel 9a captive sheet, with a persistent journal. NOT YET RUN.

This is C25, which failed once with its cause lost in a reboot, plus the plan's Step 2. *Agent:* make dnsmasq log its
queries for this check only. The file must not stay in the image.

```bash
printf 'log-queries\nlog-dhcp\n' | sudo tee /etc/NetworkManager/dnsmasq-shared.d/zz-field-debug.conf
sudo install -d -m 0700 /root/kit1-uplink-saved
sudo mv /etc/NetworkManager/system-connections/black-relay-uplink.nmconnection /root/kit1-uplink-saved/
sudo reboot
```

*Owner:* join from the Pixel with the passphrase, and note what appears, with the time. Do not open a browser for the
first minute. Then follow C25's phone list. At the console, or after the save:

```bash
journalctl -b -o short-precise --no-pager -t dnsmasq | grep -E 'query|dhcp' | tail -60
journalctl -b -u camera-provision --no-pager -o short-precise | tail -60
sudo rm -f /etc/NetworkManager/dnsmasq-shared.d/zz-field-debug.conf
sudo rm -rf /root/kit1-uplink-saved
```

Pass: the sheet appears by itself, and C25's list holds. If it does not, the record shows what the phone asked for,
and what it got: that is the deliverable. Fix what it shows if it is small, and otherwise it goes in the tracker. The
iPhone half of C25 stays open until an iPhone is on the bench.

```
C69  date ________  sheet: yes / no  after ___ s  queries ___________________  zz file removed ___  result ________
```

**End of part B. Report it.**

---

# Part C. Faults and the long run

## C70. Pull power during adoption, 5 times. NOT YET RUN.

Plan C47. *Owner*, with *Agent* following the journal. Before each cut, the camera is factory reset (pinhole), and the
node is up. Cut **the node's** power at these points. Each may spend the factory-signature attempt, in which case
`retry_after` applies, and `clear-limits` recovers it, as a ruling accepts.

| # | Cut at | After power returns: state, and the camera usable? | Record |
|---|---|---|---|
| 1 | During the 45 s settle | | |
| 2 | Just after `enable_services` starts | | |
| 3 | At `password changed`, which is the risky one | | |
| 4 | During settings, before `adopted` | | |
| 5 | Just after Frigate starts | | |

Pass: after each, with no tools and at most a pinhole, the camera is adopted and streaming, and no password is lost.
Case 3 must recover through the pending password, without a reset. Frigate never holds a password that the camera
refuses. Record any case that needs `clear-limits`.

```
C70  date ________  output ___________________________________________________  result ________
```

## C71. A second factory camera swapped in. NOT YET RUN.

Plan C48. If there is no second camera, reset the same one out of sight of the node's record, and say so: then the MAC
is the same, and the "different camera" half stays open. *Owner:* factory reset the second camera, unplug the first
with the system running, and plug the second in. Do it three ways:

1. After a wait, so normal.
2. **Within seconds**, inside the lease-wipe window, so that Frigate has the old password until a new lease shows.
3. With the lease still running, and carrier never falling, if a small switch allows it. Per a ruling, a camera that does
   not re-DHCP on link-up may wait up to 30 min.

*Agent:* `journalctl -u camera-link -o short-precise -f`, and `cat /run/camera-link/state.json`.

Pass: Frigate stops as soon as a lease names the new MAC, since its password is the old camera's; the new camera gets `10.77.0.10` with no
hour-long wait; adopted; Frigate restarts with the new password; `mac` changes in the state file; a heartbeat with
`camera_online` true; no operator action. Record how long each took. For the false factory signature tracker
(`adopt.py:203`): if Frigate is held until a replug, record it.

```
C71  date ________  1: ___ s  2: ___ s  3: ___ s  second camera: yes / no  result ________
```

## C72. No internet at first start. NOT YET RUN.

Plan C49. *Owner:* turn off the router's WAN, not its WiFi. *Agent:* confirm no way out, then power cycle the node:

```bash
ssh kit-1@$NODE 'curl -m 5 -sI https://ghcr.io | head -1; echo "curl rc=$?"'
```

Pass: the node joins, NTP syncs from the laptop, Frigate starts with no pull, since the image is present by digest,
detection runs on a walk, and a heartbeat reaches `nodes`. No journal line waits on the WAN.

```
C72  date ________  output ___________________________________________________  result ________
```

## C73. A person produces `camera.detection.v0` on the central broker. PARTLY RUN 2026-11-13: the event arrives.

Plan C50. *Owner* walks through. *Agent*:

```bash
central_sub -t detection/camera/detections -W 300 | tee /tmp/detections.jsonl
( cd bridge && uv run python - <<'PY'
import json
from pathlib import Path
from jsonschema.validators import Draft202012Validator
validator = Draft202012Validator(json.loads(Path("schemas/detection.v0.schema.json").read_text()))
rows = [json.loads(line) for line in open("/tmp/detections.jsonl")]
for row in rows:
    validator.validate(row)
print(len(rows), "valid", sorted({r["event"] for r in rows}), sorted({r["label"] for r in rows}))
print(rows[0]["attributes"].get("snapshot_url"))
PY
)
api "http://$CENTRAL:8081/events?limit=5" | python3 -m json.tool | head -40
```

Pass: `object_start`, `object_end`, and some `object_update`, with `label` `person`, all valid; the `zones` are the
typed name, or empty out of view; the API has the same `event_id`s; times right to a second or two.

```
C73  date 2026-11-13  count 6 in the first 2 s  events object_update, label person  valid not checked against the schema
     One walk in view. Frigate made one person event, top score 0.84, in the node's zone. The central API held six
     `camera.detection.v0` messages for it within 2 s, confidence 0.71 to 0.82, with bbox, zone and a snapshot URL.
     Model yolov6n.hef at 320x320 on the Hailo. Still to do: read the broker with central_sub, validate against the
     schema, and see the end event.  result PARTLY RUN, the path works
```

## C74. Stream, snapshot, and who can reach the node. PARTLY RUN 2026-11-13: central's side passes.

Plan C51, with the Frigate UI replaced by the tunnel, per the ruling. *Agent:*

```bash
EVENT=$(python3 -c "import json; print([json.loads(l) for l in open('/tmp/detections.jsonl')][0]['object_id'])")
relay_read ffprobe -v error -show_entries stream=codec_name -of csv=p=0
api -o /tmp/snap.jpg -w '%{http_code} %{content_type} %{size_download}\n' \
  "http://$CENTRAL:8081/nodes/$NODE_ID/snapshot/$EVENT.jpg"
api "http://$CENTRAL:8081/nodes/$NODE_ID/frigate/api/version"; echo
curl -s -o /dev/null -w '%{http_code}\n' "http://$CENTRAL:8081/nodes"
# The laptop is central, so it needs no jump host. Then open http://localhost:15000. Kill it afterwards.
ssh -L 15000:127.0.0.1:5000 kit-1@$NODE -N &
```

A site LAN host that is not central, by *Owner* or any other machine on the network:

```bash
nc -zv -w 3 <node> 22; nc -zv -w 3 <node> 8971; nc -zv -w 3 <node> 8554
```

Over Tailscale, the API and MediaMTX on the laptop's Tailscale address, as for a remote user, but no route to the node.

Pass: `h264`; a real JPEG with HTTP 200; the Frigate API answers through the proxy, with what a real Frigate does with
`X-Ingress-Path` recorded, as nothing has tested that; 401 with no token; the tunnel opens the Frigate UI; the other
host times out on all three; Tailscale works for both, and `ping` from there to the node fails. C24 of the README is
the full version, and run it now.

```
C74  date 2026-11-13  video h264  snapshot 200, image/jpeg, 25639 bytes  proxy 200, Frigate 0.18.2-cfa08c5
     no token 401 on /nodes and on the snapshot  other host NOT RUN  Tailscale NOT RUN
     The tunnel reached Frigate's API on the node's port 5000. From central, the node's 22, 8971 and 8554 are open,
     and 5000, 1883 and 80 are not. The set holds central's address alone, and its three rules have counted
     packets. X-Ingress-Path was not looked at. Still to do: a second site host, which must time out on all three
     ports, and a remote user over Tailscale. The API port here is 8082, as in C50.  result PARTLY RUN
```

## C45 and C46, from the README. NOT YET RUN.

Run them now, and record them there. For "hold the button at boot", use C56's procedure. C45 needs a second broker
address. An address added beside the laptop's first does not do, as [above](#before-you-start-laptop). With the owner's
OK that a second address is free, make it the laptop's only address, update `CENTRAL` and `relay/.env`, and run `docker
compose up -d`, or use a second machine as the broker. To see the old address time out, change the laptop back, then
re-provision to the original and confirm `central_check` passes before going on. Its step 3 is proved by C53 and C59.
C46's step 3 overlaps C76, so one run serves both, and record in both.

```
C45  date ________  result ________        C46  date ________  result ________
```

## C75. A damaged `central-hosts.nft` stops detection at boot, and a console recovers it. NOT YET RUN.

*Owner at the console, with the Agent reading.* This takes SSH away on purpose, so a working C51 is required.

```bash
sudo cp /etc/black-relay/central-hosts.nft /root/central-hosts.saved
echo 'this is not nft' | sudo tee /etc/black-relay/central-hosts.nft
sudo reboot
```

After the boot, at the console:

```bash
systemctl status nftables --no-pager | head -6
systemctl is-active ssh frigate camera-provision
sudo nft list tables
sudo install -m 0644 /root/central-hosts.saved /etc/black-relay/central-hosts.nft
sudo systemctl start nftables ssh
sudo systemctl restart camera-provision
sudo rm /root/central-hosts.saved
```

Pass, before the repair: `nftables` failed; `ssh`, `frigate` and `camera-provision` not active, as their guards hold; no
table; no port answers and no AP opens; the heartbeat, if any, shows detection false. After it: SSH works,
Frigate is back, and `nodes` is healthy, with no reboot. The README's recovery installs the empty file and re-provisions;
this uses the saved one to save a re-provision. An unsaved node needs that route, so read it too.

```
C75  date ________  before ________  after ________  result ________
```

## C76. Heartbeat truth table. NOT YET RUN.

Plan C53. Every line needs recorded output. *Agent:* cause the fault, wait a status interval, and read `nodes`, which
counts from what central received. Then undo it, and confirm a return to all true. For each, also keep the
raw `attributes.reason`.

```bash
truth() { api "http://$CENTRAL:8081/nodes" | python3 -c '
import json, sys
for n in json.load(sys.stdin)["nodes"]:
    keys = ("camera_online", "link_online", "recording_ok", "detector_ok", "stale", "heartbeat_age_s", "attributes")
    print({k: n.get(k) for k in keys})'; }
while :; do date +%T; truth; sleep 10; done
```

| Fault, and how | `camera_online` | `recording_ok` | `detector_ok` | `link_online` | Expected | Output, date |
|---|---|---|---|---|---|---|
| Nothing: a person has walked through since the last start | | | | | All true | |
| Unplug the camera | | | | | `camera_online`, `recording_ok` false | |
| `sudo systemctl stop frigate` (camera-link starts it again, so `sudo systemctl stop camera-link` first) | | | | | All three false | |
| `detect.enabled: false` in `/var/lib/frigate/config/config.yml`, restart Frigate | | | | | `detector_ok` false. camera-link rewrites a changed file, so record if it does | |
| `sudo mv /etc/modprobe.d/hailo_pci.conf /root/` and reboot: the kit 1 crash | | | | | `detector_ok` false within one interval, a `reason`, and a restart every 10 min | |
| WiFi off, with `sudo systemd-run --on-active=180 nmcli radio wifi on; sudo nmcli radio wifi off` | | | | | No heartbeat, `stale` true after 90 s, and `link_online` true after it returns | |
| Fresh Frigate start, nothing moving | | | | | `detector_ok` false, no `reason`, until first motion | |
| Stop camera-link for 6 minutes | | | | | `detector_ok`, `recording_ok` false, and `camera_link_state` drops from the attributes after 5 min | |

Put the option back after its row, with `sudo /opt/black-relay/src/cameralink/ops/install-node.sh`, and reboot. Save the
real `docker logs frigate` lines of that crash: they replace the fixtures, as C46 asks.

**Pass is the table filled with what it expects.** A wrong cell is a defect.

```
C76  date ________  all rows recorded ___  result ________
```

## C77. Measured costs, kept until now by a ruling. NOT YET RUN.

Run during C78, in an ordinary hour. These decide whether to change the 5 s `systemctl` poll, the 240 docker calls per
hour, and the outbox's rewrite at the cap, so measure, do not assume. *Agent*, one hour apart:

```bash
for u in camera-link camerabridge frigate mosquitto; do systemctl show -p CPUUsageNSec,MemoryCurrent "$u" | tr '\n' ' '; echo "$u"; done
sudo cat /proc/$(systemctl show -p MainPID --value camerabridge)/io | grep write_bytes
sudo cat /sys/block/mmcblk0/stat
```

Pass is a recorded number for each: CPU per hour for camera-link, and bytes written per hour by the bridge and in all.
*Proposed:* camera-link under 1% of a core. For the outbox's rewrite, a central outage during it shows the cap's cost
only if it fills, so note whether it did.

```
C77  date ________  camera-link CPU ___ s/h  bridge writes ___ MB/h  card writes ___ MB/h  decision ________
```

## C78. 24 h run. NOT YET RUN.

Plan C52. Runs only after the owner agrees to leave it up for 24 h. Disk use against retention, thermal state, and
inference speed. *Agent:* on the node, start a sampler that outlives the SSH session, every 10 minutes. It ends at a
reboot, so a short log means there was one, and that is a finding. The commands below delete its file afterwards.

```bash
sudo systemd-run --unit=field-sampler --on-unit-active=600 --on-boot=60 /bin/sh -c '
  { date -Is; df -h /var/lib/frigate/media | tail -1; du -sh /var/lib/frigate/media /var/lib/docker /var/log/journal
    vcgencmd measure_temp; vcgencmd get_throttled
    mosquitto_sub -h 127.0.0.1 -t frigate/stats -C 1 -W 90 | python3 -c "import json,sys; s=json.load(sys.stdin); print(s[\"detectors\"], {k:v.get(\"camera_fps\") for k,v in s[\"cameras\"].items()})"
    systemctl show -p NRestarts --value camera-link camerabridge frigate mosquitto camera-provision | tr "\n" " "; echo
    cat /run/camera-link/state.json; echo; } >> /var/tmp/field-24h.log 2>&1'
```

Afterwards, stop the sampler, and read it:

```bash
sudo systemctl stop field-sampler.timer field-sampler.service
grep -c . /var/tmp/field-24h.log; tail -30 /var/tmp/field-24h.log
journalctl --since -24h --no-pager -u camera-link -u frigate -u camerabridge -g 'detector_error|restart|failed' | tail -30
api "http://$CENTRAL:8081/events?limit=5" | python3 -m json.tool | head
sudo rm /var/tmp/field-24h.log
```

Then C62's 24 h reading, C57's camera `Time` against the node (`cam_read Time`, for clock drift with NTP off and no
RTC: the camera has no way to correct it but `set_clock`, so record the drift, and its effect on OSD and records).

Pass: 24 h with Frigate's restarts at 0 beyond those caused on purpose, `throttled=0x0`, inference steady, disk use
levelled at retention and leaving at least 5% free, and every heartbeat true for the walks through.
A long-running count of restarts is a failure, even with a healthy end.

```
C78  date started ________  ended ________  disk ___  max temp ___  inference ___  restarts ___  camera clock drift ___  result ________
```

---

# After the run

1. Record results here, and in README's C-numbers where they overlap. Replace `tests/fixtures` Hailo lines with the
   real ones from C55, C59 and C76.
2. Remove `frigate-spike` and everything else site-specific with README build step 10, which also clears every file
   this run may have left, on a kit that has passed,
   then capture, shrink, write and seed, steps 11 and 12, and "Seed each kit", which makes each kit's console user. C41 checks two of the clones.
3. Report: every failed check, with its output.

# Where each binding item is checked

From `progress.md`'s `Task 8 needs`, `Tracker` and Ruling lines.

| Item | Check |
|---|---|
| Install every unit, including the literal-backslash mount | C49 |
| Console or broker host ready before the portal save | C51, C52 |
| Docker and systemd under camera-link's hardening; root's systemctl reach | C53, then C59 on the daemon |
| `systemctl reload nftables` under the provision unit | C52, C53 |
| Bridge's ioctl under its system call filter | C52 |
| DHCP hardening on real root; dnsmasq's user drop | C54 |
| dnsmasq beside the AP's; `--dhcp-authoritative` beside it | C56 |
| dnsmasq's restart on rising carrier and the backoff | C54 |
| Real HailoRT lines at clean start and stop | C55, C59 |
| `frigate.service` on hardware; the 0700 tmpfs bind | C59 |
| In-use segment mtime under 60 s | C59, C47 |
| A damaged `central-hosts.nft` | C75 |
| Re-adoption or reset to apply `set_streams`; h264 after the mode change | C57, C58 |
| Field of view, 16:9 main and 4:3 sub | C60 |
| `SetNetPort`, `disable_onvif`, forced TCP; login after `ModifyUser`; time to port 80; `set_clock` with no RTC | C57, C63, C78 |
| One attempt per link; the failure counter; `clear-limits` | C61, C62 |
| Camera session limit: 2 idle, 3 viewing; the backchannel socket | C63 |
| On-demand behaviour; time to first frame; recovery after camera unreachable at start | C64 |
| CPU and tracking at 15 fps; Frigate's warning above 10 fps | C65, C59 |
| WiFi at 4 x 6 Mbps | C66 |
| 16 KB limit against real events | C67 |
| Browser WebRTC media | C68 |
| Pixel 9a captive sheet, journal persistent | C69 |
| A swap inside the lease-wipe window; a camera that does not re-DHCP on link-up; false factory signature | C71 |
| `X-Ingress-Path` against real Frigate | C74 |
| Disk use | C78 |
| Systemctl every 5 s, docker 240 calls per hour, the outbox's cap rewrite | C77 |
| Restart limit (wall clock), docker blind after an error | C76, C78 |
| F7, a hung dockerd | README firewall note; timed in C59 |
| F18, Tailscale on kit 1 | C49, C48 |
| arm64 | Node side: C49 onward. Central side: open while the laptop is central |
| Superseded, nothing to check: go2rtc publish syntax and RTMP (owner's on-demand decision) | None |
| Left in the tracker, with no field check: `reolink.py:408`, `test_adopt.py:2005`, `store.py:95`, `adopt.py:135`, `test_daemon.py:2322`, relay F17, power-loss durability on the central API, guard ready file on reboot, iptables-legacy, and a redirect to 8554 on another site host | None |

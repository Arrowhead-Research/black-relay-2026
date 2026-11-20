# Physical subsystem

Sensor bridge for the detection system: reads PowerG sensor state from a Qolsys IQ5 Hub panel and publishes it to the
team broker and to local consumers.

## Ownership

Subsystem lead: Jayden Bulexa

## Purpose and Scope

The physical subsystem is the detection system's view of the building's own sensors: glass break, motion, and
door/window contacts, all PowerG devices enrolled on a Qolsys IQ5 Hub panel. A software bridge on a small host reads the
panel's state, normalizes it, and serves it out. The fusion service combines it with the RF and camera subsystems.

What the bridge does:

- Reads zone state from the panel, strictly read-only. After the session handshake it sends nothing to the panel except
  a fixed allow-list of read messages (connect, ping, pair-status, database sync request for IQ Remote; INFO and SUMMARY
  for Control4). Anything else raises an error before bytes leave the process, and the adapters' network egress is
  restricted in the kernel by `deploy/nftables/blackrelay-egress.nft`. The bridge never arms, disarms or changes
  anything on the panel. The complete list is under "What the bridge ever sends to the panel".
- Normalizes zones to a kind (`contact`, `motion`, `glass_break`, `other`), tracks stale flags, and after every
  reconnect compares a full snapshot with its cache and emits synthetic events for the differences.
- Takes its data from the IQ Remote adapter (primary, the panel's local MQTT broker over TLS). If the primary is not
  live for `timers.fallback_after_s`, the Control4 adapter (fallback, JSON over TLS on port 12345) takes over, and the
  bridge goes back to the primary when it recovers.
- Serves: a 30-day SQLite event log with a gap-free `seq`; a local TLS MQTT broker (read-only for consumers, retained
  zone state, Home Assistant discovery); a REST/SSE API on HTTPS 8443 with API keys; Prometheus metrics on port 9100;
  and the team MQTT output described in [MQTT-CONTRACT.md](MQTT-CONTRACT.md) (draft v0, off by default).
- Operator commands for installation and field verification (`br2 pair`, `br2 snapshot`, `br2 watch`; see Startup).

Zone removal rule on failover: a full snapshot from the primary is authoritative, so every zone absent from it is
removed (`zone.removed`, retained topics cleared), whichever source last updated it. A Control4 snapshot is
authoritative for every zone it reports (its summary lists all panel zones); zones absent from it are removed too. The
exceptions go stale (`stale: true`) instead and are refreshed when the primary returns: zones whose kind is listed in
`control4.uncovered_kinds` (default empty) and zones configured with `zones.<id>.control4_visible: false`. A zone
Control4 itself owns, or reported earlier, is always removed.

Partitions the fallback does not own: a Control4 snapshot only removes partitions that Control4 itself reported earlier.
A partition that only the primary has reported is kept and flagged `stale` (a `partition.stale` log event) until the
primary's next snapshot, which is authoritative and removes whatever it omits (`partition.removed`).

Keypads: the panel lists every paired keypad-type device in its zone table, including this bridge (it pairs as an
IQ Remote and shows up as a zone named like "IQ Remote 1"). Such zones are not sensors. The bridge classifies them as kind
`other` and, by default, does not publish them: they never appear in `detection/physical/events`, in the zone status or
in the REST/MQTT zone lists. Set `zones.include_keypads: true` to publish them anyway.

Motion delay: the panel applies its own motion delay. In the one field test it was 310 s: after a motion sensor trips,
the panel does not report it idle again until that time has passed, so `motion_cleared` (and the zone going `idle`)
arrives that long after the movement stopped, and a second movement inside the window may produce no new event. The
bridge publishes what the panel reports and adds no delay of its own. Treat the motion delay as a property of the panel
and read it from the panel's settings; do not infer sensor behaviour from the timing alone.

Out of scope: arming, disarming, any control of the panel, alarm-state decisions, Z-Wave devices, the fusion logic
itself, and anything on the RF or camera sides.

**Only partly verified on real hardware.** The bridge's own code (`br2 pair` and the IQ Remote adapter) has been
exercised only against a simulator (`harness/sim`) and an in-process fake panel. The only real-panel result so far was
obtained with the reference tool `tools/pair_reference.py`. See "What has and has not been verified" under Testing and
Verification, and do not rely on the bridge before its own commands have been run against the real panel. The Control4
fallback additionally depends on the Control4 integration being enabled on the panel (and on a Control4 token and the
panel's certificate fingerprint); if that is not enabled, the fallback cannot work, and the bridge has no second source.

## File Tree

```
physical-subsystem/
  README.md                      this file
  MQTT-CONTRACT.md               team MQTT contract (draft v0)
  HARNESS.md                     how the conformance harness starts the bridge
  harness/                       panel simulator and the 28 conformance tests (failover, reconciliation, one-way, TLS)
  harness_start.sh               harness entry point (creates .venv on first call)
  pyproject.toml                 package and dependencies
  schema.sql                     SQLite schema (event log, cached state)
  schemas/                       JSON Schemas (draft 2020-12) for the team messages
    event.v0.schema.json
    panel-status.v0.schema.json
    zone-status.v0.schema.json
  src/br2/
    cli.py  supervisor.py        entry points; `run` starts all services as separate processes
    pairing.py                   `br2 pair`: one-time IQ Remote pairing behind its own write guard
    pin.py                       panel certificate leaf pin (SPKI sha256); `br2 repin`
    keyperm.py                   refuses group/world-accessible private keys at startup
    field.py                     `br2 snapshot` / `br2 watch` / `br2 repin`: read-only session through the adapter's guard
    config.py                    configuration loading and defaults
    adapters/                    iq_remote.py, control4.py (the only code that talks to the panel), beacon.py
    normalizer/                  core.py (state engine), store.py (SQLite), service.py
    outbound/
      api.py                     REST/SSE and Prometheus metrics
      mqtt_pub.py                local broker publisher (blackrelay/v1/...)
      formats.py                 ALL team wire formatting (topics, envelope, payloads, event mapping)
      outbox.py                  durable bounded outbox for the team output
      fusion.py                  team MQTT publisher (log cursor, status heartbeat, paho sender)
      state.py  service.py
  deploy/
    docker-compose.yml           services, hardening, optional `fusion` profile and network
    config/config.example.yaml   copy to deploy/config/config.yaml
    mosquitto/                   local broker config and ACL
    nftables/                    egress policy for the panel adapters
    scripts/gen-certs.sh         generates local CA, certificates, secrets
  docker/                        Dockerfile per service (base image by digest, build-time import smoke test), requirements,
                                 hashed lock files, relock.sh (regenerates the locks with a real resolver run)
  tools/pair_reference.py        reference pairing tool (qolsys-controller based); NOT used at runtime, not in the wheel or images
  .dockerignore                  keeps tools/, tests/, keys and data out of every image build context
  tests/                         unit tests; tests/integration runs through the shared harness fixtures
```

## Physical Assembly

### Parts

| Part | Qty | Notes |
|---|---|---|
| Qolsys IQ5 Hub panel (7-inch touchscreen, software 5.0.x; reports product type IQ_HUB_5) | 1 | The panel that holds the sensor enrollment |
| QC000B-840 Ethernet card | optional | Shown in the installation manual's diagram. Whether IQ Remote pairing works over Ethernet is not documented and has not been tested (see Assembly Steps) |
| DSC PG9922 glass break detector (PowerG) | 5 | |
| DSC PG9914 PIR motion detector (PowerG) | 4 | |
| DSC PGP9303 door/window contacts (PowerG+) | as supplied in the kit | ID 103-XXXX on the label; CR2450 battery |
| Bridge host | 1 | Raspberry Pi 5 or an N100 mini PC, with Docker and the Compose plugin, Wi-Fi and wired Ethernet |
| Ethernet cable, power supplies | | For the panel (if the card is used) and for the host |

### Required Tools and Conditions

- Small screwdriver set for the panel back plate and sensor brackets; whatever mounting hardware the sensor
  instruction sheets call for.
- The IQ5 Hub installation manual and the instruction sheets for each sensor type. Mounting distances, coverage areas
  and test procedures for the glass break and PIR detectors are theirs; this document does not repeat them.
- The panel's installer code or dealer code, for the Advanced Settings menus. The factory defaults are 1111 (installer)
  and 2222 (dealer); the master code (factory default 1234) does not reach the sensor menus by default. **Change every
  default code before the panel is in use, and never write a code into this repository, a config file or a commit.**
- The bridge host on the panel's own Wi-Fi access point, or on the same Wi-Fi network as the panel, for pairing and for
  the IQ Remote link (see Assembly Steps for the recommended topology). No internet access is needed by the bridge.
- Optional: a phone with the Qolsys "IQ Installer" app, as an alternative to the panel's touchscreen for configuration.

### Assembly Steps

All panel steps below are done on the panel's touchscreen. Swipe down from the top of the screen, tap Settings, then
Advanced Settings, and enter the installer or dealer code. (The IQ Installer phone app can do the same configuration:
the panel shows a QR code under Advanced Settings > IQ Installer; scan it with the app, which then acts as the
"IQ Installer Interface". It is an optional alternative, not a requirement.)

1. Set up the panel as the installation manual describes and give it network access. The panel has a built-in Wi-Fi
   access point; the recommended topology (step 5) uses it. Note the panel's address: it becomes `iq_remote.panel_ip`.
2. Enroll the PowerG sensors: Advanced Settings > Installation > Devices > Security Sensors > Auto Learn Sensor. The
   first time Installation is opened the panel may ask for dealer contact information (company name, phone, website,
   ZIP code) before it shows the menu. The kit's door/window contacts are DSC PGP9303 (PowerG+): they have no enroll
   button. With Auto Learn open, power the contact up (pull the battery tab, or take the CR2450 cell out for ten seconds
   and put it back); if nothing appears, open and close its cover. Enroll with the ID printed on the label in the form
   103-XXXX (the 109-XXXX "fallback ID" is for older panels) and enter the label's PIN if the panel asks. A contact
   that has been powered for more than two hours must be reset (battery out and in) before the panel will recognise it.
   A zone that shows "Not networked" has been added but has not completed its radio handshake: reset the contact near
   the panel, or open its cover. For 15 minutes after power-up the contact is in test mode: a red blink on opening means
   it detected the change, and three blinks two seconds later show signal strength (green strong, yellow good, red poor,
   none means no link). The panel chimes and shows the sensor's code; tap OK, then set Sensor Type, Sensor Group and
   Sensor Name, and tap Add New. Door/window contacts default to group 10; glass break detectors use group 13 (active in
   stay and away) or 17 (away only). For the other sensor types the manual says only
   "Open/Close or Tamper a sensor to enroll"; follow each sensor's own instruction sheet. Give each sensor a descriptive
   name and a group that matches its job: the bridge classifies zones from the panel's sensor type and group, and you
   can override a zone's name or kind in config (`zones:`).
3. Mount each sensor where it will be used and test it. Live status is under Settings > Status, and under
   Advanced Settings > Installation > Devices > Sensor Status. A sensor that does not register on the panel will not register in the
   bridge. Remember the panel's motion delay (see Purpose and Scope): a tripped motion sensor stays active until it
   elapses.
4. Access point: Advanced Settings > Installation > Devices > Wi-Fi Devices > Access Point Settings has the enable switch, the SSID, the
   DHCP range (default 50) and the password. In the one field test the panel's address on its own access point was
   192.168.50.1 (observed in one test; confirm it on your panel). Set a strong access point password and do not record
   it in the repository.
5. Recommended topology: the bridge host's Wi-Fi joins the panel's access point (that is the link to the panel), and its
   Ethernet port goes to the Detection VLAN (that is the link to the team broker and the consumers). The host must not
   route between the two: IP forwarding stays off. Check it, and keep it checked:

   ```sh
   sysctl net.ipv4.ip_forward           # must print: net.ipv4.ip_forward = 0
   sysctl net.ipv6.conf.all.forwarding  # must print 0 as well
   echo 'net.ipv4.ip_forward=0' | sudo tee /etc/sysctl.d/90-no-forward.conf   # persist it
   test "$(sysctl -n net.ipv4.ip_forward)" = 0 || echo "FORWARDING IS ON: fix before loading the nftables file"
   ```

   Docker enables forwarding for its bridge networks by itself; if `ip_forward` reads 1 while Docker is running,
   check that it is only Docker's and that no route joins the panel's Wi-Fi to the Detection VLAN. Joining the same Wi-Fi
   network as the panel (instead of its access point) also works for pairing and the link; the access point topology is
   simply the one that keeps the panel off the building LAN.
6. Pair the bridge as an IQ Remote. On the panel: Advanced Settings > Installation > Devices > Wi-Fi Devices > IQ Remote Devices >
   Pair. Then start the bridge's pair command on the host (see Startup, "Pairing"). The bridge host must be on the
   panel's access point or on the same Wi-Fi network as the panel. If the exchange times out, start the command first
   and tap Pair second, or the other way round. Up to 3 IQ Remotes can be paired; each pairs as Sensor Type Keypad, in
   groups 0, 1 and 2, and shows up in the panel's zone list as a zone named like "IQ Remote 1". The bridge treats that
   zone as a keypad: kind `other`, not published (see Purpose and Scope). The pairing exchange produces a key, a client
   certificate signed by the panel, and the panel's CA certificate; `br2 pair` stores them in `iq_remote.pki_dir`, laid out
   as `<12 hex digits>/<id>.key`, `.cer`, `.csr`, `.secure`, `.qolsys` (files 0600, directories 0700). Back the directory up
   and keep it out of git: the panel issues one client certificate per virtual IQ Remote, so a lost key may mean the remote
   cannot be paired again.
7. Ethernet card: the installation manual's diagram shows it. Whether IQ Remote pairing works over Ethernet is not
   documented and has not been tested; the tested path is Wi-Fi to the panel's access point. If you fit the card, treat
   pairing over it as an experiment.
8. Control4 fallback (optional): Advanced Settings > Installation > Devices > Wi-Fi Devices > 3rd Party Connections, check Control4. The
   panel requires 6-digit user codes to be enabled first, and enabling Control4 reboots the panel; it then generates a
   token. Enable the fallback only if you need a second source and have the token and certificate fingerprint.
9. Install the host, copy this directory to it, and follow Startup.

## Configuration

Copy `deploy/config/config.example.yaml` to `deploy/config/config.yaml`. Secrets never go in this file: the local MQTT
password and the Control4 token are read from files (Docker secrets). The table lists the keys that matter; the
example file documents all of them.

| Key | Default | Meaning |
|---|---|---|
| `iq_remote.panel_ip` | none | Panel address. Required for the primary adapter |
| `iq_remote.panel_port` | 8883 | Panel's local MQTT port |
| `iq_remote.pki_dir` | `/data/pki` | Pairing material (see Assembly Steps) |
| `iq_remote.ca_file` | null | Panel CA override (used with a simulated panel) |
| `iq_remote.pin_mode` | `required` | `required`: a pin must already be recorded (`br2 pair` / `br2 repin`), else the adapter refuses to connect. `tofu`: opt-in, the first verified connection records the pin. `preset` is implied by `panel_spki_sha256` (see "Panel identity") |
| `iq_remote.panel_spki_sha256` | null | Strict preset of the panel certificate's SPKI SHA-256 (64 hex); makes `pin_mode` `preset`: the pin file is ignored and nothing is ever recorded |
| `iq_remote.owner` | `10001:10000` | uid:gid that the pin and the pairing material are handed to when `br2 pair` / `br2 repin` run as root (the uid the `iq-remote` container runs as). `dir` = whoever owns `pki_dir`. `br2 pair --owner` overrides |
| `iq_remote.pin_accept_rotation` | false | Follow a changed panel certificate instead of refusing it. Harness/dev only; `harness_start.sh` sets it through `BR2_IQ_PIN_ACCEPT_ROTATION=1` (and `tofu` through `BR2_IQ_PIN_MODE=tofu`); nothing else opts into either. Whenever `pin_mode` is `tofu` or this flag is on, the adapter logs a prominent warning banner at every start |
| `control4.enabled` | false | Turn on the fallback adapter |
| `control4.host`, `.port` | null, 12345 | Panel address for Control4 |
| `control4.token_file` | null | File holding the Control4 token |
| `control4.cert_sha256` | null | Pinned panel certificate fingerprint; null means refuse to connect |
| `control4.uncovered_kinds` | `[]` | Zone kinds Control4 cannot see; they go stale rather than being removed |
| `timers.fallback_after_s` | 120 | Primary down this long: switch to Control4 |
| `timers.silence_probe_s` | 60 | No panel message for this long: probe the link |
| `zones.<id>.name`, `.kind`, `.ignore` | | Per-zone overrides |
| `zones.include_keypads` | false | Publish keypad / IQ Remote zones (including the bridge's own) like sensors. They are hidden by default; only a real boolean `true` turns this on |
| `mqtt.*` | TLS 8883 | Local read-only broker (`tls: false` is refused unless `insecure_dev: true`) |
| `api.port`, `api.keys` | 8443, none | REST/SSE; each key is a record `{name, key_id, argon2_hash}` made by `docker compose run --rm bridge hash-key` (the key is shown once and never stored). `key_id` is an HMAC-SHA256 prefix of the key: the API looks the record up by it and runs argon2 only on a match, so garbage keys cost nothing and a valid key is never held up by failed attempts. `key_id` is required on every record: with a record that lacks it the API service refuses to start and says to re-run `hash-key` |
| `api.metrics_port`, `.metrics_host` | 9100, 127.0.0.1 | Prometheus |
| `storage.data_dir`, `.retention_days` | `/data`, 30 | SQLite event log |
| `site.id`, `site.name` | `default`, `default` | Identifies this installation in team messages |
| `fusion.enabled` | false | Team MQTT output |
| `fusion.host`, `.port` | `rf-mqtt`, 1883 (example: `reg.bulexa.com`, 8883) | The central broker (repo-root `compose.yaml` on the central server) |
| `fusion.tls` | null | null = plaintext (a warning is logged). Otherwise `{ca_file, certfile?, keyfile?}` |
| `fusion.username`, `.password_file` | null | Optional; the password is read from the file |
| `fusion.topic_prefix` | `detection/physical` | |
| `fusion.client_id` | `physical-subsystem` | Must be unique on the team broker |
| `fusion.status_interval_s` | 60 | Heartbeat for panel and per-zone status |
| `fusion.outbox.max_bytes`, `.max_messages`, `.max_age_s` | 10 MiB, 20000, 86400 | Outbox caps; oldest dropped and counted |

Plaintext to the team broker is allowed only because that broker is plaintext today. The bridge's own TLS keys and
certificates (`deploy/certs`, `deploy/secrets`, `deploy/data/pki`) are generated on the host and are git-ignored.

### Panel identity (certificate pinning)

The panel's TLS certificate is checked against the panel CA, but the panel CA also signs the certificate of every paired
device, so the CA check alone would accept any of them as "the panel". The bridge therefore pins the panel's leaf
certificate by the SHA-256 of its SubjectPublicKeyInfo (`src/br2/pin.py`), inside the TLS handshake, before a single MQTT
byte is written. The recorded pin lives in `<pki_dir>/panel_pin.json` (mode 0600, owned by the service uid); the compose
`iq-remote` mounts the pki dir read-only and only ever reads it.

`iq_remote.pin_mode`:

- `required` (default): the pin must already be recorded. With no pin the adapter **refuses to connect**, sends nothing,
  logs `PANEL PIN MISSING ... run br2 pair or br2 repin`, and reports the link down with reason `pin_missing`. Nothing is
  ever recorded automatically, so a restart never re-trusts whatever answers at the panel address. The pin is recorded
  during `br2 pair` (its verification session; the operator is physically at the panel, tapping Pair) or by
  `br2 repin --yes`.
- `tofu`: opt-in (development and the conformance harness only). With no pin at all, the first verified connection
  records it. `harness_start.sh` is the only place that enables it (with rotation following).
- `preset`: implied when `iq_remote.panel_spki_sha256` is set (`openssl s_client -connect <panel>:8883 </dev/null 2>/dev/null | openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum`).
  The file is then ignored and nothing is ever recorded.

Fail closed: a pin file that **exists** but cannot be read and validated (empty, truncated or corrupt JSON, a bad hash,
unreadable by the service uid) is never treated as "no pin". The adapter refuses to connect in every mode except
`preset`, logs `PANEL PIN UNREADABLE` loudly, and reports link reason `pin_unreadable` (`/v1/health` -> `link_reasons`).
Only a file that is truly absent means "no pin yet". Fix the file's owner/mode, or verify the panel and re-record the pin
with `br2 repin --yes` (it replaces a damaged file).

- Mismatch: the connection is refused, nothing is sent, the log says `PANEL PIN MISMATCH` with both fingerprints, the link
  reason is `pin_mismatch`, and the adapter keeps retrying with backoff.
- Legitimate rotation (panel factory reset or firmware change; rare): **refused until you accept it**. Check the new
  fingerprint is expected, then `br2 repin --config ... --pki-dir ... --yes` (without `--yes` it only prints the old and new
  fingerprints) and restart `iq-remote`. If a preset is set, update it as well: `repin` warns and exits 4 otherwise.
- Ownership: `br2 pair` and `br2 repin` run as root write the pin and the pairing material owned by `iq_remote.owner`
  (default `10001:10000`, the uid of the `iq-remote` container), mode 0600, because the container has no capabilities and
  could not read root-owned files. Run as any other user they leave the files owned by that user (see Startup for the
  non-sudo way).
- Control4 has its own leaf pin (`control4.cert_sha256`, the SHA-256 of the whole DER certificate; null = refuse to connect).

## Startup

Bring-up order with Docker Compose (the intended deployment). Build before the first `docker compose run`, pair before the
first `docker compose up`:

```sh
cd deploy
sudo ./scripts/gen-certs.sh <host-ip-or-dns>   # 1. local CA, broker/API certificates, secrets, pki dir (owned by uid 10001)
cp config/config.example.yaml config/config.yaml   # 2. edit panel_ip, zones
docker compose --profile '*' build            # 3. builds every image, control4 included (the key tool and field tools need them)
docker compose run --rm bridge hash-key        # 4. prints a new API key (shown once), its key_id and argon2_hash:
                                               #    put key_id and argon2_hash (never the key) under api.keys in config.yaml
# 5. check `sysctl net.ipv4.ip_forward` is 0 (Assembly Steps, step 5); edit PANEL_IP in nftables/blackrelay-egress.nft, then load it
# 6. pair once (below): creates the pairing material and records the panel pin in deploy/data/pki/
docker compose up -d                           # 7. works with fusion.enabled: false; the fusion network is not needed
docker compose --profile fallback up -d        #    only if control4.enabled: true
```

Expected from step 4 (the values differ every time):

```
API key (shown once): br_...
key_id: <16 hex digits>
argon2_hash: $argon2id$v=19$m=65536,t=3,p=4$...
```

Expected after step 7: `docker compose ps` lists `normalizer`, `iq-remote`, `outbound` and `mosquitto` running, `normalizer` and
`outbound` `healthy`; `curl --cacert certs/ca.crt https://127.0.0.1:8443/v1/health` answers with a JSON status
(`online` once the panel link is up). Without a recorded pin `iq-remote` stays up but refuses to connect and
`/v1/health` shows `link_reasons: {"iq_remote": "pin_missing"}`.

Pairing (once, before the first `docker compose up`). `br2 pair` has to run on the host, not in a service container:
the panel connects back to it (an inbound TCP port in 50000-55000 or `--pairing-port`, plus mDNS on UDP 5353), which the
isolated adapter container cannot receive and the egress rules for uid 10001 would not allow. It needs the `pair` extra
(`cryptography`, `zeroconf`) and root, because `deploy/data/pki` is owned by uid 10001 with mode 0700:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[pair]'
sudo .venv/bin/python -m br2 pair --config deploy/config/config.yaml --pki-dir deploy/data/pki
```

The host must be on the panel's access point or the same Wi-Fi network. On the panel, with the installer or dealer code:
Advanced Settings > Installation > Devices > Wi-Fi Devices > IQ Remote Devices > Pair, then run the command (it waits 300 s;
`--timeout` changes that). If it times out, try the other order (command first, then Pair). It prints the identity it
created, validates what the panel sends back (the signed certificate must be for our key), stores it, then opens a
read-only session to prove it works, records the panel's certificate as the pin (this is the one trust-on-first-use
moment, with you at the panel) and prints the negotiated TLS version and cipher. It refuses to run if a complete
pairing already exists in the directory (do not delete one: see Assembly Steps). Run as root it hands the pairing
material and the pin to `iq_remote.owner` (default `10001:10000`, what the `iq-remote` container runs as); `--owner uid:gid`
overrides it. On a firewalled host allow inbound TCP from the panel to the pairing port and UDP 5353. `--plugin-ip`
selects this host's LAN address on multi-homed hosts. With `--no-verify` no session is opened and no pin is recorded:
run `br2 repin --yes` afterwards.

Install the egress firewall (once, and whenever the panel's address changes). The file ships with a placeholder
`PANEL_IP`; the adapters are cut off if it is wrong:

```sh
test "$(sysctl -n net.ipv4.ip_forward)" = 0 && echo "ip_forward=0 ok" || echo "net.ipv4.ip_forward is NOT 0: stop"
$EDITOR deploy/nftables/blackrelay-egress.nft          # set  define PANEL_IP = <your panel's address>   (marked EDIT THIS)
sudo nft -c -f deploy/nftables/blackrelay-egress.nft   # syntax check
sudo nft -f deploy/nftables/blackrelay-egress.nft      # load; persist with an include in /etc/nftables.conf
```

Field verification (any time after pairing; same read-only guard as the adapter, nothing else is ever sent). `snapshot`,
`watch` and `repin` need no pairing extra and no root: run them as the service user in a container, which keeps every
file they write owned by uid 10001 (the `iq-tools` service uses the `iq-remote` image, host networking and uid 10001, so
the nftables rule that lets `iq-remote` reach only `PANEL_IP:8883` covers it too):

```sh
cd deploy
docker compose run --rm iq-tools snapshot --config /config/config.yaml                 # zones/partitions as JSON on stdout
docker compose run --rm iq-tools watch --minutes 10 --config /config/config.yaml        # one JSON line per change
docker compose run --rm iq-tools repin --config /config/config.yaml                     # prints old/new pin; add --yes to record
```

The same commands work from the host venv (`.venv/bin/python -m br2 snapshot --config deploy/config/config.yaml --pki-dir deploy/data/pki`);
that needs root to read the 0700 pki dir, and as root what `repin` writes is handed to `iq_remote.owner`, so ownership
stays right. Do not run `br2 pair` or `br2 repin` with `sudo` from a directory tree you want to keep owned by someone else
and then start the stack: the pin and the pairing files must end up readable by uid 10001 (check with
`ls -ln deploy/data/pki`).

`br2 repin` is the one deliberate way to accept a new panel certificate or to record the first pin without pairing (see
"Panel identity"). `snapshot`, `watch` and `repin` print `TLS to the panel: version=... cipher=...` on stderr (keep it as
field evidence). `snapshot` writes the JSON to stdout; `watch` writes one JSON line per change (zone status/battery, zone
added/removed, partition arming/alarm) for the requested time. With `pin_mode: required` and no recorded pin `snapshot` and
`watch` stop with `NO PANEL PIN RECORDED ... Run br2 pair ... or br2 repin --yes` and send nothing. None of them prints key
material.

Team output (optional): the central broker runs on the central server (repo-root `compose.yaml`). Put its physical
password in `deploy/secrets/fusion/fusion_password` (uid 10003, 0600). Automated: on the central server run
`detection/central/share-broker-login.sh <device>` (seals it for this bridge in the secrets store), then here
`deploy/scripts/fetch-broker-login.sh` (needs `central-store/secrets.sh setup` once). By hand:
`sudo FUSION_PASSWORD=... ./scripts/gen-certs.sh <host>`. Then set `fusion.enabled: true`
in `config.yaml` (the example already points at `reg.bulexa.com:8883` with TLS and the `physical-subsystem` login), then:

```sh
docker compose --profile fusion up -d
```

This starts one more container from the same image, attached only to its own outbound network (`fusion-out`). It has a healthcheck
(`python -m br2 healthcheck fusion`, backed by `GET /healthz` on `fusion.metrics_host:fusion.metrics_port`, default
127.0.0.1:9101 inside the container) and serves `/metrics` there: outbox depth, drops, last PUBACK age (section 11 of
MQTT-CONTRACT.md). `docker compose ps` shows it `healthy` once running. It is separate because the
`outbound` container uses host networking, and the publisher should not.

Without Docker (development, and how the harness runs it): `./harness_start.sh <config.yaml>` creates `.venv` and runs
all services as separate processes under a supervisor. The script sets `BR2_IQ_PIN_MODE=tofu` and
`BR2_IQ_PIN_ACCEPT_ROTATION=1`, because the harness has no pairing step and rotates the simulated panel's certificate on
purpose; no shipped config or compose file does. Run just the team output with `python -m br2 service fusion --config <config.yaml>`.

### Bench run without Docker

For a developer machine (Linux or macOS) with a panel that is already paired and pinned, `scripts/bench.sh` runs the whole
bridge (normalizer, outbound API, event log, consumer broker, team broker, fusion output) with no Docker:

```sh
scripts/bench.sh --panel-ip <ip> --pki-dir <dir>      # <dir> as given to the pairing tool; see Troubleshooting
```

It needs python3, openssl, curl and mosquitto (`brew install mosquitto` or `apt install mosquitto`; it also looks in the
Homebrew `opt/mosquitto` folders). It uses the tree's own `.venv` (created and installed on first use; that is the only
step that needs the network) and puts everything else in the git-ignored `bench/` folder: a test CA and server certificate,
a fresh API key (printed once; only its key_id and argon2 hash are written), a TLS consumer broker on 127.0.0.1:18883, a
plaintext "team" broker on 127.0.0.1:11883 standing in for the fusion broker, the generated config (`fusion.enabled: true`,
API on 127.0.0.1:18443) and the event log. It keeps `iq_remote.pin_mode: required`: with no pin recorded it refuses to start
and says to run `br2 repin --yes`. When the API is healthy it prints copy-paste commands for health and zones (curl), the SSE
stream, `mosquitto_sub` on `detection/physical/#`, and a sqlite3 query of the last 10 events. Ctrl-C stops everything it
started. `--simulator --ca-file <sim CA>` (harness simulator only, never a real panel) switches to `pin_mode: tofu` and
trusts that CA. Tested here on Linux against the harness simulator (a simulated door open and close reached the API, the
SQLite log and the team broker, and the team messages validated against `schemas/event.v0.schema.json`); macOS itself has
not been tested.

## What the bridge ever sends to the panel

Everything is enforced by an exact-match allow-list in code (`GuardedPanel`, `GuardedC4Writer`, `PairGuard`), checked
on the exact serialized frame before bytes leave the process (exact key set, exact fixed values, scalar types only; a
duplicate JSON key, an extra key or a nested object is refused), and the adapters are also fenced by nftables. Nothing
else exists.

| Channel | Sent | When |
|---|---|---|
| IQ Remote, MQTT topic `mastermeid` | `connect_v204`, `pingevent`, `pair_status_request`, `syncdatabase`; each with an exact key set, our own identity, QoS 0 | Session start, keep-alive every 300 s and as a silence probe, resync. Also used by `br2 snapshot`, `br2 watch` and the check at the end of `br2 pair` |
| IQ Remote, MQTT subscribe | `iq2meid`, `response_<our mac>` | Session start |
| Control4, TLS JSON | exactly `{"action":"INFO","info_type":"SUMMARY","token":<our token, printable string>,"nonce":"qolsys","source":"C4","version":0}`, one line; any other key, value or type is refused | Fallback adapter only: snapshot and keep-alive |
| Pairing, TLS socket the panel opens to us (`br2 pair` only) | Exactly three frames, in order and byte-exact: our random MAC (2-byte length + 17 chars), our CSR (PEM), the literal `sent` | Once, while the operator taps Pair on the panel |
| Pairing, mDNS (`br2 pair` only) | One service record, `NsdPairService._http._tcp.local.`, naming our own address and pairing port | While waiting for the panel to connect |

Never sent: arm, disarm, bypass, user codes, any database insert/update/delete, any `ipcCall`, time sync, Z-Wave or
camera commands. The pairing frames are the only messages added for `br2 pair`; the MQTT allow-list was not extended
for it (the post-pairing check reuses the four read messages above).

## Testing and Verification

Run one suite at a time (the integration suite starts several brokers and processes).

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[test,pair]'
.venv/bin/python -m pytest tests --ignore=tests/integration -q      # unit tests
BR_HARNESS_DIR=$PWD/harness .venv/bin/python -m pytest tests/integration -q   # integration (skipped when BR_HARNESS_DIR is unset)
harness/run.sh .                                                    # conformance harness: 28 tests against the simulated panel
```

The conformance harness is in `harness/` (panel simulator in `harness/sim`, tests in `harness/conformance`, protocol
notes in `harness/PANEL_PROTOCOL.md`). Set `BR_HARNESS_DIR` to it for the integration suite. Both use mosquitto (`/usr/sbin/mosquitto`) and a simulated panel.

Team output tests (`tests/test_fusion_*.py`, a few seconds in total): every message type validated against
`schemas/`; envelope, topic per type, id stability on re-send and timestamp format; outbox crash mid-write, SIGKILL
while appending, caps and counters; replay after a broker outage with a real mosquitto, byte-identical re-send; the
client never subscribes (broker log, a client that refuses `subscribe`, and a source scan); the JSON examples in
MQTT-CONTRACT.md validate.

Manual checks:

1. With the bridge running, `curl --cacert deploy/certs/ca.crt https://<host>:8443/v1/health` returns status
   `online`, `active_source: iq_remote`, and the zone count.
2. Trip each sensor once. `GET /v1/zones` shows the state change and `GET /v1/events?after=0` shows the event with a
   new `seq`.
3. With fusion enabled, on the central server: `mosquitto_sub -h <address> -p 1883 -u fusion -P <BROKER_FUSION_PASSWORD> -v -t 'detection/physical/#'`. Expect a panel status and one
   zone status per zone within 60 s, and an event within about a second of each sensor trip.
4. Stop the team broker, trip a sensor, start the broker: the event arrives with its original `id` and `timestamp`.
5. Disconnect the panel from the LAN: after `fallback_after_s` the bridge uses Control4 (if enabled) or reports
   `offline`, and zones are flagged `stale`.

### What has and has not been verified

Against the simulator (`harness/sim`), an in-process fake panel and throwaway local brokers: everything in the test
suites (unit, integration, harness): the interfaces, session handling, failover and failback, the pin modes, the event
log (replaying the log from seq 0 rebuilds the cache exactly; a seeded model test checks it), keypad exclusion, the team
output against a real mosquitto.

Against a real IQ5 Hub (software 5.0.x), over the panel's own access point, in November 2026: the reference tool
`tools/pair_reference.py` paired. The bridge's own code (`br2 repin`, `br2 snapshot`, `br2 watch`), reusing that pairing,
connected with TLS 1.3 (TLS_AES_256_GCM_SHA384), completed the read-only sync, listed a PGP9303 door contact and the IQ
Remote keypad zone, and reported one event per door open and one per door close.

Not yet run on real hardware: `br2 pair`; the Docker Compose stack, the event log, the brokers and the API; motion and
glass break sensors; the Control4 fallback; failover and reconnect behaviour (simulator only).

The harness simulator does not implement pairing, so `br2 pair` is tested only against a small in-process fake panel
(`tests/test_pairing.py`); `br2 snapshot` and `br2 watch` are tested against the simulator
(`tests/integration/test_field_commands.py`).

What the tests exercise, all against the simulator, an in-process fake panel or throwaway local brokers: the interfaces
above (REST, SSE, local MQTT, metrics, SQLite log), IQ Remote and Control4 session handling, failover and failback, the
pin modes, and the team MQTT output against a real mosquitto. **The bridge's own code has not been
verified on a real IQ5 Hub** (see "What has and has not been verified"), nor against the real team broker.

Compose smoke test (no panel hardware): build the images, point `iq_remote.panel_ip`, `panel_port` and `ca_file` of
`config.yaml` at the harness simulator, record the simulator's certificate as the pin with
`docker compose run --rm iq-tools repin --config /config/config.yaml --yes` (nothing is trusted automatically), start the
default and `fusion` profiles with a throwaway plaintext mosquitto on an external `fusion` network, then:

```sh
docker compose --profile fusion ps                       # expect every service running; normalizer, outbound, fusion: healthy
mosquitto_sub -h <broker> -p 1883 -C 1 -t 'detection/physical/status/panel'    # expect one JSON message, schema physical.panel-status.v0
```

Validate that message against `schemas/panel-status.v0.schema.json` (`jsonschema`). Run this on your own host before
relying on the stack; no result of such a run is recorded in this document.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/v1/health` says `offline`, no zones | Panel address, pairing files in `pki_dir`, `docker compose logs iq-remote`. A `FileNotFoundError: no pairing key` means the pairing material is missing |
| Pairing made with `tools/pair_reference.py`: "no pairing key" or "no pin" | Point `--pki-dir` (or `iq_remote.pki_dir`) at the folder you gave that tool (the material is found in `<dir>/pki/<12hex>/`; `<dir>/<12hex>/` and a flat folder work too, and the pin file lives next to the material, so either spelling finds the same pin). Private key files must be mode 600 |
| TLS errors to the panel | The panel CA file (`<id>.qolsys`) must be the one from pairing. Certificates from another panel are refused by design |
| Want to see the TLS version the panel negotiated | `snapshot`, `watch` and `repin` print it on stderr; in the real-hardware test the panel negotiated TLS 1.3 (TLS_AES_256_GCM_SHA384) |
| Zones show `stale: true` | The adapter link is down or data is not being refreshed. See `links` in `/v1/health` |
| Control4 never takes over | `control4.enabled`, host, `token_file`, `cert_sha256` all set; the integration enabled on the panel |
| Bridge refuses to start: "mqtt.tls is false" | Intended. Use TLS, or `mqtt.insecure_dev: true` on a throw-away broker only |
| `fusion` container `unhealthy` | `docker compose exec fusion python -m br2 healthcheck fusion` and `docker compose logs fusion`; `/healthz` says whether the producer stalled or the oldest message is stuck (team broker unreachable) |
| `PANEL PIN MISSING` in the iq-remote log, `link_reasons: {"iq_remote": "pin_missing"}` | `iq_remote.pin_mode` is `required` and no pin is recorded. Run `br2 pair` (or `docker compose run --rm iq-tools repin --config /config/config.yaml --yes` after checking you are talking to the panel), then restart `iq-remote` |
| `PANEL PIN UNREADABLE`, `link_reasons: {"iq_remote": "pin_unreadable"}` | `deploy/data/pki/panel_pin.json` exists but is empty, corrupt, or not readable by uid 10001 (`ls -ln`; a root-owned 0600 file after a `sudo` run with a different `--owner`). Fix the owner/mode, or `repin --yes` to replace it |
| `PANEL PIN MISMATCH` in the iq-remote log, `link_reasons: {"iq_remote": "pin_mismatch"}` | Something other than the paired panel answers at `panel_ip` (or the panel's certificate changed). Verify, then `br2 repin` (see "Panel identity"). Nothing was sent to it |
| Refusing to start: "private key ... must not be accessible to group/others" | `chmod 600` the key (the IQ pairing key, `api.tls_key`, `fusion.tls.keyfile`) |
| `physical_fusion_*` shows `write_errors`, `/healthz` says "outbox journal write failing" | Disk full or I/O error on the fusion state volume. Nothing is lost: each failed append is rolled back and retried; free space |
| `docker compose run --rm bridge hash-key` says the image is missing | `docker compose --profile '*' build` first (Startup, step 3) |
| `br2 pair` fails on the real panel | Fallback: `tools/pair_reference.py` (the qolsys-controller based tool this project was specified from; header comment says how to use its output). It bypasses the bridge's write guard, is never used at runtime and is not in the wheel or the images, and the library it imports contains arm/disarm functions (the script never calls them), so use it for pairing only, then `br2 snapshot` |
| `br2 pair` times out | Host on the panel's access point or the same Wi-Fi? Try the other order: start the command, then Pair on the panel (or the reverse). Inbound TCP to the printed port and UDP 5353 allowed? |
| The API service refuses to start: "api key ... has no key_id" | Re-run `docker compose run --rm bridge hash-key` and put both `key_id` and `argon2_hash` of every key under `api.keys` |
| A motion zone stays `active` for minutes after the movement ended | The panel's motion delay (310 s observed once): the panel does not report idle until it elapses. Not a bridge fault |
| "IQ Remote 1" is missing from the zone lists | Intended: keypad zones are hidden unless `zones.include_keypads: true` |
| `blackrelay_adapter_bad_zone_ids_total` rising | The panel sent a zone row whose id is not a plain integer; the row is dropped and one resync is requested. Look at `docker compose logs iq-remote` |
| Log banner "WEAKENED PANEL CERTIFICATE PINNING" | `pin_mode` is `tofu` or `pin_accept_rotation` is on. Development only; switch back to the defaults |
| `br2 pair`: "no panel completed the pairing" | Pair tapped on the panel? Same subnet? Inbound TCP to the printed port and UDP 5353 allowed on this host? `--plugin-ip` right on a multi-homed host? |
| `fusion` container cannot connect | `reg.bulexa.com` resolves to the central server from this host? Port 8883 reachable? Password in `secrets/fusion/fusion_password` matches `BROKER_PHYSICAL_PASSWORD`? |
| Log line "fusion: TLS is OFF" | `fusion.tls` is null: use port 8883 with the `tls` block from the example |
| No messages on `detection/physical/#` | `fusion.enabled: true`? Correct host? `physical_fusion_connected` metric (in-process mode) or the container log. Client id clash with another client on the broker makes both disconnect repeatedly |
| Messages arrive late or twice | Normal after an outage: at-least-once; dedupe on `id` |
| `physical_fusion_outbox_dropped_total` rising | The broker was unreachable longer than the outbox caps allow; raise `fusion.outbox.*` |
| Outbox seems stuck | Depth is in the metric `physical_fusion_outbox_depth`; the journal is `<state dir>/fusion/outbox.journal` (safe to delete only if you accept losing the queued messages) |

## Interfaces and Dependencies

Inputs:

- Panel, IQ Remote: TLS MQTT to `panel_ip:8883` with the pairing client certificate.
- Panel, Control4 (fallback): TLS JSON to `host:12345` with a token and a pinned certificate.

Outputs:

- Team broker: `detection/physical/events`, `detection/physical/status/panel`, `detection/physical/status/zones`
  (QoS 1, never retained, plaintext TCP in v0). See [MQTT-CONTRACT.md](MQTT-CONTRACT.md) and `schemas/`.
- Local broker: `blackrelay/v1/...` and Home Assistant discovery, TLS 8883.
- REST/SSE: HTTPS 8443 (`/v1/zones`, `/v1/events`, `/v1/stream`, `/v1/health`). Metrics: `:9100/metrics`.

Dependencies: Python 3.11 or newer; `aiomqtt`, `paho-mqtt`, `aiohttp`, `pyyaml`, `argon2-cffi`; for tests `pytest`,
`pytest-asyncio`, `cryptography`, `jsonschema`, `httpx`, `setuptools`, `wheel` (the wheel-contents test); for `br2 pair` (host only) `cryptography` and `zeroconf`; Docker with Compose; Eclipse Mosquitto (container) for the
local broker. The fusion service and the other subsystems are consumers on the team broker; the Docker network
`fusion` is created outside this project.

## Change Notes

- 2026-11-15: Last-audit LOW fixes and hardware corrections. API keys must carry a `key_id` (the service refuses to start
  otherwise; the legacy path is gone) and a cancelled first verification no longer fails concurrent verifications of
  the same key. Event log: `zone.added` carries tamper/battery/online, kind and partition changes are events
  (`zone.kind`, `zone.partition`), partitions have `partition.added/renamed/stale/removed`, every event's block is the
  zone/partition as of that event, so replaying the log from seq 0 reproduces the cache exactly (seeded model test in
  `tests/test_event_log_replay.py`); a fallback snapshot keeps partitions it does not own, stale. A non-numeric zone id from
  the panel is rejected (counter, one coalesced resync), never mapped to zone 0. A dangling symlink or any non-regular file
  at the pin path is "unreadable" (fail closed); a warning banner is logged when `pin_mode` is `tofu` or
  `pin_accept_rotation` is on. Keypad / IQ Remote zones are hidden by default (`zones.include_keypads`). Tests: fusion
  `_on_connect` generation check, `tests/test_confirm_api.py` no longer takes minutes, the wheel and images exclude
  `tools/`. README: rewritten for the IQ5 Hub (touchscreen menus, access point, topology with IP forwarding off, Control4,
  Ethernet card caveat, verification status, motion delay). `tools/pair_reference.py` header warns about the library's
  arm/disarm functions.
- 2026-11-15: Confirming-audit fixes. Images: the lock files are regenerated by `docker/relock.sh` (a real resolver run on
  the image's Python; the outbound lock lacked `typing_extensions`) and every Dockerfile imports its service modules at build
  time. Pin: a pin file that exists but is unreadable or invalid is refused (`pin_unreadable`), `iq_remote.pin_mode`
  (`required` default, `tofu`, `preset`), `br2 pair` records the pin, root-run `pair`/`repin` write files owned by
  `iq_remote.owner`, new `iq-tools` compose service for `snapshot`/`watch`/`repin` as uid 10001. Control4 write guard: exact
  frame. Fusion sender: callbacks are tied to a client generation, the old client's loop is joined before a new one starts.
  Outbox: journal offset correct after a failed reopen; a corrupt mid-journal record rewinds the event cursor (counted in
  `physical_fusion_outbox_cursor_rewinds_total`). API: keys carry a `key_id` (`hash-key` prints it) so valid keys are not
  blocked by failures from the same address; a steadily reading SSE client is not dropped. IQ adapter: an insert without
  `zoneid` triggers a resync; `Engine.restore()` is transactional. Tamper/battery/stale/renamed log rows record the zone
  `state` as of the event (MQTT-CONTRACT.md updated).
- 2026-11-15: Final-audit fixes. Outbox: appends are atomic in effect (rolled back on any write/fsync error, retried, health
  degraded), compaction failure is non-fatal, a single message over the cap is rejected instead of evicting the queue.
  Fusion sender: every paho callback is wrapped (no exception can kill paho's thread), a watchdog reconnects a dead or stuck
  publisher, an unclaimed PUBACK can never ack a different message after a mid wrap, inbound PUBLISH from the broker is
  refused (the client subscribes to nothing) and packet size is bounded. IQ link: leaf pin of the panel certificate,
  `br2 repin`. Normalizer: deltas for unknown zones no longer create zones; partial snapshots do not remove zones; rename
  produces `zone.renamed` (team event `zone_renamed`); log rows store zone name/kind/partition/stale as of the event.
  SSE: a client that never reads is dropped (flow-control probe + small send buffers); the auth-failure table is a bounded
  LRU and attempts are rate-limited before any argon2 work. Wire guard rejects duplicate-key JSON. Deployment: base images
  by digest, hashed lock files, mosquitto hardened and non-root, fusion has its own state volume and a read-only store mount
  (**migration: move `out-state/fusion/` to the new `fusion-state` volume**), key-permission check, `PANEL_IP` marked in the
  nftables file, integration tests find the harness only through `BR_HARNESS_DIR`, `tools/pair_reference.py`.

- 2026-11-14: Added the team MQTT output (draft v0): `formats.py`, durable outbox, publisher, JSON Schemas,
  `MQTT-CONTRACT.md`, `fusion` config block and compose profile, `site` config, outbox metrics, tests. Existing
  outputs (SQLite, local broker, REST/SSE, metrics) are unchanged; the output is off unless `fusion.enabled` is true.
- 2026-11-14: Added `br2 pair`, `br2 snapshot`, `br2 watch` (pairing behind its own exact-frame guard; read-only
  field commands that print the negotiated TLS version and cipher), `br2 healthcheck fusion`, the fusion container's
  `/metrics` + `/healthz` and compose healthcheck, and the "What the bridge ever sends to the panel" table.
- 2026-11-14: Team output: `seq` on events is now the event log's own seq (plus `prev_seq` for loss detection),
  status messages carry no `seq`, zone stale/fresh flips are recorded in the SQLite log by the normalizer (new event
  type `zone.stale`, in the same transaction as the state change), tamper/battery events, and the contract lists the
  panel facts that are not published.
- 2026-11-14: README rewritten to the team format; `.gitignore` added.
- Derived from the F2 (Python microservices) BlackRelay implementation; the panel code paths and their allow-lists are
  untouched.
